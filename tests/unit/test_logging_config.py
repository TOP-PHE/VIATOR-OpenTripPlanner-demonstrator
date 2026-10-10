"""setup_logging() — JSON output, stdlib + structlog parity, idempotency."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator

import httpx
import pytest
import structlog

from app.logging_config import setup_logging


@pytest.fixture(autouse=True)
def _reset_logging_state() -> None:
    """Each test gets a fresh root logger + structlog default config."""
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.WARNING)
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()


def test_idempotent_handler_count() -> None:
    setup_logging(json=True)
    setup_logging(json=True)
    setup_logging(json=True)
    assert len(logging.getLogger().handlers) == 1


def test_stdlib_logger_emits_json(capsys: pytest.CaptureFixture[str]) -> None:
    setup_logging(json=True)
    logging.getLogger("test.stdlib").info("hello", extra={"foo": "bar"})
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert payload["event"] == "hello"
    assert payload["level"] == "info"
    assert payload["logger"] == "test.stdlib"
    assert "timestamp" in payload


def test_structlog_logger_emits_json(capsys: pytest.CaptureFixture[str]) -> None:
    setup_logging(json=True)
    structlog.get_logger("test.structlog").info("user_logged_in", user_id=42)
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert payload["event"] == "user_logged_in"
    assert payload["user_id"] == 42
    assert payload["level"] == "info"


def test_contextvars_appear_in_output(capsys: pytest.CaptureFixture[str]) -> None:
    """Bound contextvars must surface in every log line — this is what carries
    request_id from the middleware into application log calls."""
    setup_logging(json=True)
    structlog.contextvars.bind_contextvars(request_id="abc123")
    structlog.get_logger("test").info("event")
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert payload["request_id"] == "abc123"


def test_console_format_does_not_emit_json(capsys: pytest.CaptureFixture[str]) -> None:
    setup_logging(json=False)
    structlog.get_logger("test").info("hello")
    out = capsys.readouterr().out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out.strip())


def test_uvicorn_loggers_configured_to_propagate() -> None:
    """All four loggers we override (uvicorn / uvicorn.access / uvicorn.error /
    fastapi) must have their per-logger handlers stripped and propagate=True
    set — otherwise they'd stay on uvicorn's own console formatter and bypass
    our JSON root handler. The actual stdlib→JSON round-trip is already
    exercised by test_stdlib_logger_emits_json."""
    setup_logging(json=True)
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error", "fastapi"):
        lg = logging.getLogger(name)
        assert lg.propagate is True, f"{name} must propagate to root"
        assert lg.handlers == [], f"{name} must have no own handlers"


# ───────────── no query string in the log (#339 review): credentials, typed text ─────────────
#
# Invented values only: ZZFAKEKEY99 stands for a feed credential of auth type
# `query`, Zzq-typed-339 for text typed in the journey form.

_KEY = "ZZFAKEKEY99"
_TYPED = "Zzq-typed-339"


@pytest.fixture
def _restore_quieted_loggers() -> Iterator[None]:
    """setup_logging changes these process-wide loggers; give them back."""
    levels = {name: logging.getLogger(name).level for name in ("httpx", "httpcore")}
    access = logging.getLogger("uvicorn.access")
    filters = list(access.filters)
    yield
    for name, level in levels.items():
        logging.getLogger(name).setLevel(level)
    access.filters[:] = filters


async def _fetch_with_a_query_credential() -> None:
    def feed(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"zz")

    async with httpx.AsyncClient(transport=httpx.MockTransport(feed)) as client:
        await client.get(f"https://zz-feed.example/gtfs.zip?apikey={_KEY}")


async def test_httpx_request_lines_with_a_query_credential_are_not_logged(
    capsys: pytest.CaptureFixture[str], _restore_quieted_loggers: None
) -> None:
    setup_logging(level="INFO", json=True)
    await _fetch_with_a_query_credential()
    assert _KEY not in capsys.readouterr().out

    # The line exists and would carry the key, at INFO, without the setting.
    logging.getLogger("httpx").setLevel(logging.INFO)
    await _fetch_with_a_query_credential()
    assert f"apikey={_KEY}" in capsys.readouterr().out


async def test_httpx_warnings_still_reach_the_log(
    capsys: pytest.CaptureFixture[str], _restore_quieted_loggers: None
) -> None:
    setup_logging(level="INFO", json=True)
    assert logging.getLogger("httpx").getEffectiveLevel() == logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() == logging.WARNING
    logging.getLogger("httpx").warning("zz httpx warning")
    assert "zz httpx warning" in capsys.readouterr().out


def _access_line(path: str) -> None:
    # uvicorn's own call (protocols/http/h11_impl.py and httptools_impl.py).
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d', "127.0.0.1:9999", "GET", path, "1.1", 200
    )


def test_the_access_log_drops_the_typed_text_of_the_geocoder(
    capsys: pytest.CaptureFixture[str], _restore_quieted_loggers: None
) -> None:
    setup_logging(level="INFO", json=True)
    _access_line(f"/api/geocode?q={_TYPED}&size=20")
    line = json.loads(capsys.readouterr().out.strip())
    assert line["event"] == '127.0.0.1:9999 - "GET /api/geocode HTTP/1.1" 200'
    assert _TYPED not in json.dumps(line)


@pytest.mark.parametrize(
    "path",
    ["/api/journey/searches/zz?x=1", "/api/geocodez?q=zz", "/api/geocode", "/journey"],
    ids=["other-route", "longer-name", "no-query", "page"],
)
def test_the_access_log_keeps_every_other_path_as_it_is(
    capsys: pytest.CaptureFixture[str], _restore_quieted_loggers: None, path: str
) -> None:
    setup_logging(level="INFO", json=True)
    _access_line(path)
    line = json.loads(capsys.readouterr().out.strip())
    assert line["event"] == f'127.0.0.1:9999 - "GET {path} HTTP/1.1" 200'


def test_the_access_filter_is_added_once(_restore_quieted_loggers: None) -> None:
    from app.logging_config import DropTypedQueryString

    setup_logging(json=True)
    setup_logging(json=True)
    access = logging.getLogger("uvicorn.access")
    assert sum(isinstance(f, DropTypedQueryString) for f in access.filters) == 1


def test_a_record_of_another_shape_passes_untouched() -> None:
    from app.logging_config import DropTypedQueryString

    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, "%s %s", ("/api/geocode?q=zz", 1), None
    )
    assert DropTypedQueryString().filter(record) is True
    assert record.args == ("/api/geocode?q=zz", 1)
