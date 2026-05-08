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

import structlog
from structlog.types import Processor


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

    structlog.configure(
        processors=[
            *pre_chain,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
