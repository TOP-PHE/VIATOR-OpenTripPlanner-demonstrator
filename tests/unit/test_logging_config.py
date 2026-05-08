"""setup_logging() — JSON output, stdlib + structlog parity, idempotency."""

from __future__ import annotations

import json
import logging

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


def test_uvicorn_logger_propagates(capsys: pytest.CaptureFixture[str]) -> None:
    """Uvicorn's loggers must be reset to propagate so their lines hit our root
    JSON handler — otherwise they'd stay on uvicorn's own console formatter."""
    setup_logging(json=True)
    uv = logging.getLogger("uvicorn.access")
    assert uv.propagate is True
    assert uv.handlers == []
    uv.warning("uvicorn-style %s message", "test")
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert payload["logger"] == "uvicorn.access"
