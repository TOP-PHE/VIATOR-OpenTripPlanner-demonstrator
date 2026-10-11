"""setup_logging() — JSON output, stdlib + structlog parity, idempotency."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path

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

    # The line exists at INFO; lowered to INFO by hand, it is written with
    # the key redacted (redact_urls), never with the key.
    logging.getLogger("httpx").setLevel(logging.INFO)
    await _fetch_with_a_query_credential()
    out = capsys.readouterr().out
    assert "gtfs.zip?apikey=REDACTED" in out
    assert _KEY not in out


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
    assert line["event"] == '127.0.0.1:9999 - "GET /api/geocode?q=REDACTED&size=20 HTTP/1.1" 200'
    assert _TYPED not in json.dumps(line)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/api/auth/check-token?t=ZZ99", "/api/auth/check-token?t=REDACTED"),
        ("/zz-new-route?code=ZZ99&state=ZZ99", "/zz-new-route?code=REDACTED&state=REDACTED"),
        ("/confirm/ZZ99", "/confirm/REDACTED"),
        ("/reset/ZZ99", "/reset/REDACTED"),
        ("/share/coverage/ZZ99/cells/a/b/trips", "/share/coverage/REDACTED/cells/a/b/trips"),
        ("/api/reports/searches?since=7d&page=2", "/api/reports/searches?since=7d&page=2"),
        ("/api/geocode", "/api/geocode"),
        ("/journey", "/journey"),
    ],
    ids=[
        "check-token",
        "new-route",
        "confirm",
        "reset",
        "share",
        "allow-listed",
        "no-query",
        "page",
    ],
)
def test_the_access_log_redacts_every_route(
    capsys: pytest.CaptureFixture[str],
    _restore_quieted_loggers: None,
    path: str,
    expected: str,
) -> None:
    setup_logging(level="INFO", json=True)
    _access_line(path)
    line = json.loads(capsys.readouterr().out.strip())
    assert line["event"] == f'127.0.0.1:9999 - "GET {expected} HTTP/1.1" 200'
    assert "ZZ99" not in json.dumps(line)


def test_the_access_filter_is_added_once(_restore_quieted_loggers: None) -> None:
    from app.logging_config import RedactAccessLogTarget

    setup_logging(json=True)
    setup_logging(json=True)
    access = logging.getLogger("uvicorn.access")
    assert sum(isinstance(f, RedactAccessLogTarget) for f in access.filters) == 1


def test_the_access_filter_itself_redacts_the_target() -> None:
    """The filter works on its own, before any formatter (a handler with
    another formatter would otherwise print the raw target)."""
    from app.logging_config import RedactAccessLogTarget

    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:9999", "GET", "/reset/ZZ99?t=ZZ99&page=2", "1.1", 200),
        None,
    )
    assert RedactAccessLogTarget().filter(record) is True
    assert record.args == ("127.0.0.1:9999", "GET", "/reset/REDACTED?t=REDACTED&page=2", "1.1", 200)


def test_a_record_of_another_shape_passes_untouched() -> None:
    from app.logging_config import RedactAccessLogTarget

    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, "%s %s", ("/api/geocode?q=zz", 1), None
    )
    assert RedactAccessLogTarget().filter(record) is True
    assert record.args == ("/api/geocode?q=zz", 1)


# ───────────── no URL secret in any other line (follow-up of #339) ─────────────

_FEED_KEY = "ZZSECRET99"


async def _failing_feed_download(staging: Path) -> None:
    """The feed refresh's own download path (app/feed_fetch.py), against a
    feed that answers 404 to a URL carrying a query credential."""
    from app import feed_fetch

    def feed(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(feed)) as client:
        await feed_fetch.fetch_validated(
            client,
            fetch_url=f"https://zz-feed.example/gtfs.zip?apikey={_FEED_KEY}",
            state_url="https://zz-feed.example/gtfs.zip",
            extra_headers={},
            kind="gtfs",
            staging=staging,
            base_name="zz",
            suffix=".zip",
            previous={},
            have_current=False,
        )


async def test_a_failing_feed_download_logged_with_its_traceback_carries_no_key(
    capsys: pytest.CaptureFixture[str], _restore_quieted_loggers: None, tmp_path: Path
) -> None:
    """The chained httpx error says `… for url 'https://…?apikey=…'`: a log
    call with the traceback (log.exception, exc_info) must not print it."""
    from app import feed_fetch

    setup_logging(level="INFO", json=True)
    with pytest.raises(feed_fetch.FetchError) as caught:
        await _failing_feed_download(tmp_path)
    assert _FEED_KEY not in str(caught.value)
    assert _FEED_KEY in repr(caught.value.__cause__)  # the leak this guards against

    try:
        raise caught.value
    except feed_fetch.FetchError:
        logging.getLogger("app.zz").exception("feed refresh failed for %s", caught.value.__cause__)
        structlog.get_logger("app.zz").exception(
            "feed.refresh_failed", url=str(caught.value.__cause__)
        )
    out = capsys.readouterr().out
    lines = [json.loads(line) for line in out.strip().splitlines()]
    assert len(lines) == 2
    assert all("exception" in line for line in lines)
    assert "gtfs.zip?apikey=REDACTED" in out
    assert _FEED_KEY not in out


def test_redact_urls_handles_every_value_type(
    capsys: pytest.CaptureFixture[str],
) -> None:
    setup_logging(json=True)
    structlog.get_logger("zz").warning(
        "check https://h.example/?token=ZZ99",
        nested={"u": "/reset/ZZ99", "n": [1, "/x?code=ZZ99", None, True, 2.5]},
        url=httpx.URL("https://h.example/f?apikey=ZZ99"),
        count=3,
    )
    line = json.loads(capsys.readouterr().out.strip())
    assert "ZZ99" not in json.dumps(line)
    assert line["event"] == "check https://h.example/?token=REDACTED"
    assert line["nested"] == {"u": "/reset/REDACTED", "n": [1, "/x?code=REDACTED", None, True, 2.5]}
    assert "apikey=REDACTED" in line["url"]
    assert line["count"] == 3


def test_the_worker_formatter_redacts_message_and_traceback() -> None:
    from app.logging_config import RedactingFormatter, install_redacting_formatter

    fmt = "%(levelname)s %(message)s"
    try:
        raise httpx.ConnectError("cannot reach https://h.example/f.zip?apikey=ZZ99")
    except httpx.ConnectError:
        import sys

        record = logging.LogRecord(
            "worker", logging.ERROR, __file__, 1, "fetch %s failed", ("/x?q=ZZ99",), sys.exc_info()
        )
    out = RedactingFormatter(fmt).format(record)
    assert "ZZ99" not in out
    assert "f.zip?apikey=REDACTED" in out
    assert out.startswith("ERROR fetch /x?q=REDACTED failed")

    root = logging.getLogger()
    root.addHandler(logging.StreamHandler())
    install_redacting_formatter(fmt)
    assert all(isinstance(h.formatter, RedactingFormatter) for h in root.handlers)


def test_the_worker_process_logs_through_the_redacting_formatter() -> None:
    """app/worker.py configures its logging at import; in a fresh process
    (pytest's own handlers would hide basicConfig here) its root handler
    must redact."""
    import os
    import subprocess
    import sys

    code = (
        "import logging, app.worker\n"
        "logging.getLogger('worker').warning('fetch https://h.example/f?apikey=ZZ99 failed')\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "OTEL_EXPORTER_OTLP_ENDPOINT": ""},
        check=True,
    )
    assert "f?apikey=REDACTED failed" in done.stderr
    assert "ZZ99" not in done.stderr + done.stdout
