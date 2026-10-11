"""Structured-logging configuration — JSON to stdout in production, console for dev.

Both stdlib `logging.getLogger(...)` calls and `structlog.get_logger(...)` calls
feed into the same renderer chain via `structlog.stdlib.ProcessorFormatter`. This
means uvicorn, SQLAlchemy, APScheduler, and the modules still on stdlib `logging`
all emit the same JSON shape — without needing per-module migration.

Request-id propagation is automatic: middleware binds `request_id` via
`structlog.contextvars.bind_contextvars`, and `merge_contextvars` (first in the
processor chain) pulls it into every log line — including from background tasks
inside the same request scope.

Idempotent — safe to call multiple times. Test fixtures use `caplog`
unaffected because the stdlib root logger is preserved as the destination.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog
from structlog.types import EventDict, Processor

from .url_redaction import redact_text, redact_url


def _shared_processors() -> list[Processor]:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
    ]


# httpx (and httpcore below it) log every request at INFO as
# `HTTP Request: GET <full URL> "<status>"`, query string included. That URL can
# carry a feed credential of auth type `query` (`?apikey=…`, app/credentials.py)
# or the text typed in the journey form's station fields (/api/geocode → MOTIS
# `?text=…`). WARNING keeps their warnings and errors and drops those lines
# (#339 review). Nothing in VIATOR reads them.
QUIET_HTTP_CLIENT_LOGGERS = ("httpx", "httpcore")

# Every line that still names a URL — uvicorn's access line, a traceback
# quoting an httpx error, a message — passes through app.url_redaction
# (RedactAccessLogTarget, redact_urls, RedactingFormatter below): no query
# value outside its allow-list and no token path reaches the log.


def quiet_http_client_loggers() -> None:
    """Raise httpx's and httpcore's loggers to WARNING; see above."""
    for name in QUIET_HTTP_CLIENT_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


class RedactAccessLogTarget(logging.Filter):
    """On `uvicorn.access`, pass the request target through
    `app.url_redaction.redact_url`: every query value outside the allow-list
    and every token path segment becomes REDACTED, for every route — a new
    route's query is redacted without anyone remembering it. uvicorn's
    access record carries `(client, method, path_with_query, http_version,
    status)` as its args."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) == 5 and isinstance(args[2], str):
            record.args = (args[0], args[1], redact_url(args[2]), args[3], args[4])
        return True


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: _redact_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(v) for v in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    # An object the renderer would print with repr() (an httpx.URL, an
    # exception): print it here, redacted.
    return redact_text(repr(value))


def redact_urls(_logger: Any, _method: str, event_dict: EventDict) -> EventDict:
    """structlog processor: every string of the line — the message, each
    field, the formatted traceback — with its URLs passed through
    `app.url_redaction.redact_text`. Runs last before the renderer, so it
    sees the stdlib records (uvicorn, SQLAlchemy, APScheduler, httpx's
    warnings) and the structlog ones alike."""
    return {k: _redact_value(v) for k, v in event_dict.items()}


class RedactingFormatter(logging.Formatter):
    """A plain-text formatter whose whole output, traceback included, goes
    through `redact_text`. For the worker, which logs with basicConfig."""

    def format(self, record: logging.LogRecord) -> str:
        return redact_text(super().format(record))


def install_redacting_formatter(fmt: str) -> None:
    """Give every handler of the root logger a RedactingFormatter."""
    for handler in logging.getLogger().handlers:
        handler.setFormatter(RedactingFormatter(fmt))


def setup_logging(
    level: str | None = None,
    *,
    json: bool | None = None,
) -> None:
    """Configure stdlib + structlog with a shared JSON (or console) renderer.

    `level` and `json` default to `settings.log_level` / `settings.log_format`.
    Passing them explicitly is intended for tests.
    """
    from .settings import settings

    actual_level = (level or settings.log_level).upper()
    actual_json = json if json is not None else (settings.log_format == "json")

    renderer: Processor = (
        structlog.processors.JSONRenderer()
        if actual_json
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    pre_chain = _shared_processors()

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=pre_chain,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            redact_urls,
            renderer,
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(actual_level)

    # Uvicorn applies its own dictConfig before importing the app, which sets
    # propagate=False on its loggers and gives them their own handlers.
    # Strip those so uvicorn / fastapi / sqlalchemy / apscheduler logs flow
    # through our JSON-formatting root handler instead.
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error", "fastapi"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True

    quiet_http_client_loggers()
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, RedactAccessLogTarget) for f in access.filters):
        access.addFilter(RedactAccessLogTarget())

    structlog.configure(
        processors=[
            *pre_chain,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def one_line(value: object, max_len: int = 200) -> str:
    """`value` as a single, capped log field.

    Line breaks become visible `\\r` / `\\n`, so a value that came from a
    request or a config row cannot forge an extra log line. The final
    `.replace("\\n", ...)` is the form CodeQL's py/log-injection query
    recognises as a sanitiser — keep it last.
    """
    return str(value)[:max_len].replace("\r", "\\r").replace("\n", "\\n")
