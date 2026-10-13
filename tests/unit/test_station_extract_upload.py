"""Sending the stop extracts to the station module (MSMM 22.3; VIATOR PR 5).

The module is never reached: a fake module answers through
`httpx.MockTransport` (or a small streaming transport), a fake clock replaces
every pause, and a fake extractor stands in for `station_extract.build_extract`
with its interface. Values are invented: a `.invalid` address, `ZZ` provider
ids, a token and archive digests drawn at run time.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import logging
import secrets
import tracemalloc
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app import station_extract
from app import station_extract_upload as up
from app.logging_config import QUIET_HTTP_CLIENT_LOGGERS
from app.settings import settings

MODULE_URL = "http://msmm.invalid:8000"
SESSION = "zz-session_9.9"


class FakeClock:
    def __init__(self) -> None:
        self.now = 9000.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _sha() -> str:
    return hashlib.sha256(secrets.token_bytes(16)).hexdigest()


def _url_key() -> str:
    return "url:" + secrets.token_hex(8)


class FakeExtractor:
    """`station_extract.build_extract` with its interface: writes a small gzip
    extract per call, or raises `ExtractError` when told to."""

    def __init__(self, tmp: Path, keys: dict[str, str], fail: str | None = None) -> None:
        self.tmp = tmp
        self.keys = keys
        self.fail = fail
        self.calls: list[tuple[str, str]] = []
        self.payload = b"format_version,feed_key\n1,zz\n" * 50

    def __call__(
        self,
        provider_cfg: Any,
        archive_path: Path,
        archive_sha256: str,
        *,
        extracted_at: datetime,
        cache_dir: Path | None = None,
    ) -> station_extract.ExtractResult:
        assert extracted_at.tzinfo is not None
        self.calls.append((provider_cfg["id"], archive_sha256))
        if self.fail is not None:
            raise station_extract.ExtractError(self.fail, "stand-in")
        path = self.tmp / f"{archive_sha256}.csv.gz"
        body = gzip.compress(self.payload)
        path.write_bytes(body)
        return station_extract.ExtractResult(
            path_gz=path,
            feed_key=self.keys[provider_cfg["id"]],
            feed_version=archive_sha256,
            extractor_version=station_extract.EXTRACTOR_VERSION,
            rows=50,
            bytes=len(body),
            body_sha256=hashlib.sha256(body).hexdigest(),
        )


Answer = httpx.Response | Callable[[httpx.Request], httpx.Response]


class Module:
    """A fake module: `heads` and `puts` answer in turn (the last one again
    when they run out). A PUT answer of None means "the right 200 for that
    body"; `put_status` gives its status word."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.bodies: list[bytes] = []
        self.heads: list[Answer] = [httpx.Response(404)]
        self.puts: list[Answer | None] = [None]
        self.put_status = "recorded"

    @staticmethod
    def _next(answers: list[Any]) -> Any:
        return answers.pop(0) if len(answers) > 1 else answers[0]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "HEAD":
            answer = self._next(self.heads)
            return answer(request) if callable(answer) else answer
        body = request.read()
        self.bodies.append(body)
        answer = self._next(self.puts)
        if answer is None:
            return httpx.Response(
                200,
                json={
                    "status": self.put_status,
                    "sha256": hashlib.sha256(body).hexdigest(),
                    "bytes": len(body),
                    "rows": 50,
                },
            )
        return answer(request) if callable(answer) else answer

    @property
    def head_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "HEAD"]

    @property
    def put_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "PUT"]


def _refusal(status: int, code: str, retry_after: str | None = None) -> httpx.Response:
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    return httpx.Response(status, json={"detail": "stand-in", "code": code}, headers=headers)


@pytest.fixture
def token(monkeypatch: pytest.MonkeyPatch) -> str:
    value = secrets.token_hex(32)
    monkeypatch.setattr(settings, "station_module_url", MODULE_URL)
    monkeypatch.setattr(settings, "station_module_token", SecretStr(value))
    return value


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def module() -> Module:
    return Module()


class Setup:
    def __init__(self, tmp: Path, monkeypatch: pytest.MonkeyPatch, n: int = 1) -> None:
        self.feeds: list[up.Feed] = []
        keys: dict[str, str] = {}
        for i in range(n):
            pid = f"ZZ{i}"
            archive = tmp / f"zz{i}.zip"
            archive.write_bytes(b"PK stand-in archive")
            keys[pid] = _url_key()
            self.feeds.append(
                up.Feed(
                    pid,
                    {"id": pid, "timetable": {"format": "gtfs"}},
                    keys[pid],
                    archive,
                    _sha(),
                )
            )
        self.extractor = FakeExtractor(tmp, keys)
        monkeypatch.setattr(station_extract, "build_extract", self.extractor)


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Setup:
    return Setup(tmp_path, monkeypatch)


def _run(
    setup: Setup,
    module: Module | httpx.AsyncBaseTransport,
    clock: FakeClock,
    *,
    budget: float = up.BUILD_BUDGET_SECONDS,
    should_stop: Callable[[], bool] = lambda: False,
    sid: str = SESSION,
) -> up.PassResult:
    transport = (
        module if isinstance(module, httpx.AsyncBaseTransport) else httpx.MockTransport(module)
    )
    return up.send_session_extracts(
        sid,
        setup.feeds,
        budget=budget,
        should_stop=should_stop,
        clock=clock,
        transport=transport,
        record=False,
    )


def _states(result: up.PassResult) -> list[tuple[str, str | None]]:
    return [(o.state, o.reason) for o in result.outcomes]


# ── the contract's numbers ───────────────────────────────────────────────


def test_the_numbers() -> None:
    assert up.HEAD_SECONDS == 10.0
    assert up.PUT_SECONDS == 120.0
    assert up.BUILD_BUDGET_SECONDS == 300.0
    assert up.REFRESH_BUDGET_SECONDS == 1800.0
    assert up.EXTRACT_MAX_BYTES == 32 * 1024 * 1024
    assert up.TRANSIENT_RETRIES == 2
    assert up.SESSION_ID.pattern == r"[A-Za-z0-9._-]{1,64}"


# ── HEAD, then PUT ───────────────────────────────────────────────────────


@pytest.mark.usefixtures("token")
def test_a_feed_already_held_sends_nothing(setup: Setup, module: Module, clock: FakeClock) -> None:
    module.heads = [httpx.Response(200)]
    result = _run(setup, module, clock)
    assert _states(result) == [(up.HELD, None)]
    assert module.put_requests == []
    assert setup.extractor.calls == []
    feed = setup.feeds[0]
    (head,) = module.head_requests
    assert head.url.path == f"/internal/v1/feeds/{feed.feed_key}/{feed.archive_sha256}"
    assert dict(head.url.params) == {"extractor_version": station_extract.EXTRACTOR_VERSION}


@pytest.mark.usefixtures("token")
@pytest.mark.parametrize(("status", "state"), [("recorded", up.SENT), ("unchanged", up.HELD)])
def test_a_feed_not_held_is_extracted_and_sent(
    setup: Setup, module: Module, clock: FakeClock, status: str, state: str
) -> None:
    module.put_status = status
    result = _run(setup, module, clock)
    assert _states(result) == [(state, None)]
    feed = setup.feeds[0]
    assert setup.extractor.calls == [(feed.provider_id, feed.archive_sha256)]
    (put,) = module.put_requests
    assert put.url.path == f"/internal/v1/feeds/{feed.feed_key}/extract"
    assert put.url.query == b""
    assert put.headers["content-type"] == "application/gzip"
    assert int(put.headers["content-length"]) == len(module.bodies[0])
    assert "transfer-encoding" not in put.headers
    assert gzip.decompress(module.bodies[0]) == setup.extractor.payload


@pytest.mark.usefixtures("token")
def test_the_headers_are_built_from_nothing(
    setup: Setup, module: Module, clock: FakeClock, token: str
) -> None:
    _run(setup, module, clock)
    for request in module.requests:
        assert request.headers["authorization"] == f"Bearer {token}"
        assert request.headers["accept-encoding"] == "identity"
        assert request.headers[up.SESSION_HEADER] == SESSION
        assert "x-viator-user-id" not in request.headers
        assert "cookie" not in request.headers


@pytest.mark.usefixtures("token")
@pytest.mark.parametrize("sid", ["zz session", "z" * 65, "zz/99", "zzé"])
def test_a_session_id_of_another_form_is_not_sent(
    setup: Setup, module: Module, clock: FakeClock, sid: str
) -> None:
    result = _run(setup, module, clock, sid=sid)
    assert _states(result) == [(up.SENT, None)]
    assert all(up.SESSION_HEADER.lower() not in r.headers for r in module.requests)


# ── refusals ─────────────────────────────────────────────────────────────


REFUSALS = [
    (422, "not_gzip"),
    (422, "not_utf8"),
    (422, "not_csv"),
    (422, "too_large"),
    (422, "format_version"),
    (422, "header"),
    (422, "empty"),
    (422, "row"),
    (422, "feed_key"),
    (422, "same_on_every_row"),
    (422, "duplicate_stop"),
    (422, "session"),
    (409, "source_taken"),
    (413, "size"),
    (415, "size"),
    (403, "token"),
    (403, "browser"),
    (400, "disconnected"),
]


@pytest.mark.usefixtures("token")
@pytest.mark.parametrize(("status", "code"), REFUSALS, ids=[f"{s}-{c}" for s, c in REFUSALS])
def test_each_refusal_is_logged_with_its_word_only(
    setup: Setup,
    module: Module,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
    status: int,
    code: str,
) -> None:
    module.puts = [_refusal(status, code)]
    with caplog.at_level(logging.INFO, logger="app.station_extract_upload"):
        result = _run(setup, module, clock)
    assert _states(result) == [(up.REFUSED, code)]
    assert result.outcomes[0].text == f"refused {code}"
    assert len(module.put_requests) == 1
    assert clock.slept == []
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == [f"station_extract.refused provider=ZZ0 reason={code}"]
    assert "stand-in" not in caplog.text


@pytest.mark.usefixtures("token")
def test_a_refusal_without_a_known_word_names_its_status(
    setup: Setup, module: Module, clock: FakeClock
) -> None:
    module.puts = [httpx.Response(418, json={"detail": "x", "code": "zz_unknown"})]
    assert _states(_run(setup, module, clock)) == [(up.REFUSED, "status_418")]


@pytest.mark.usefixtures("token")
@pytest.mark.parametrize("status", [403, 422])
def test_a_refused_head_sends_no_put(
    setup: Setup, module: Module, clock: FakeClock, status: int
) -> None:
    # A HEAD answer has no body: the status is the reason.
    module.heads = [httpx.Response(status)]
    result = _run(setup, module, clock)
    assert _states(result) == [(up.REFUSED, f"status_{status}")]
    assert module.put_requests == []


# ── waiting and trying again ─────────────────────────────────────────────


@pytest.mark.usefixtures("token")
def test_busy_waits_its_retry_after_then_sends(
    setup: Setup, module: Module, clock: FakeClock
) -> None:
    module.puts = [_refusal(503, "busy", "10"), None]
    result = _run(setup, module, clock)
    assert _states(result) == [(up.SENT, None)]
    assert clock.slept == [10.0]
    assert len(module.put_requests) == 2


@pytest.mark.usefixtures("token")
def test_the_extract_window_waits_its_retry_after_then_sends(
    setup: Setup, module: Module, clock: FakeClock
) -> None:
    module.puts = [_refusal(429, "extract_window", "42"), None]
    result = _run(setup, module, clock)
    assert _states(result) == [(up.SENT, None)]
    assert clock.slept == [42.0]


@pytest.mark.usefixtures("token")
def test_a_window_past_the_budget_ends_every_put_of_the_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module: Module, clock: FakeClock
) -> None:
    setup = Setup(tmp_path, monkeypatch, n=3)
    module.heads = [httpx.Response(404), httpx.Response(404), httpx.Response(200)]
    module.puts = [_refusal(429, "extract_window", "1500")]
    result = _run(setup, module, clock)
    assert _states(result) == [
        (up.UNAVAILABLE, "extract_window"),
        (up.UNAVAILABLE, "extract_window"),
        (up.HELD, None),
    ]
    assert len(module.put_requests) == 1
    assert clock.slept == []
    assert len(setup.extractor.calls) == 1


@pytest.mark.usefixtures("token")
def test_busy_that_lasts_past_the_budget_gives_up(
    setup: Setup, module: Module, clock: FakeClock
) -> None:
    module.puts = [_refusal(503, "busy", "10")]
    result = _run(setup, module, clock, budget=35.0)
    assert _states(result) == [(up.UNAVAILABLE, "busy")]
    assert clock.slept == [10.0, 10.0, 10.0]


@pytest.mark.usefixtures("token")
@pytest.mark.parametrize("status", [408, 500, 502])
def test_a_transient_answer_is_tried_again_a_bounded_number_of_times(
    setup: Setup, module: Module, clock: FakeClock, status: int
) -> None:
    module.puts = [_refusal(status, "timeout")]
    result = _run(setup, module, clock)
    assert _states(result) == [(up.UNAVAILABLE, f"status_{status}")]
    assert len(module.put_requests) == 1 + up.TRANSIENT_RETRIES
    assert clock.slept == [up.TRANSIENT_PAUSE_SECONDS] * up.TRANSIENT_RETRIES


@pytest.mark.usefixtures("token")
def test_a_408_then_success_is_sent(setup: Setup, module: Module, clock: FakeClock) -> None:
    module.puts = [_refusal(408, "timeout"), None]
    assert _states(_run(setup, module, clock)) == [(up.SENT, None)]


@pytest.mark.usefixtures("token")
@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (httpx.ReadTimeout, up.TIMEOUT),
        (httpx.ConnectError, up.NETWORK),
        (httpx.ReadError, up.NETWORK),
    ],
)
def test_a_call_without_an_answer_is_tried_again_then_unavailable(
    setup: Setup, module: Module, clock: FakeClock, error: type[Exception], reason: str
) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise error("stand-in", request=request)  # type: ignore[call-arg]

    module.heads = [fail]
    result = _run(setup, module, clock)
    assert _states(result) == [(up.UNAVAILABLE, reason)]
    assert len(module.head_requests) == 1 + up.TRANSIENT_RETRIES


class _Slow(httpx.AsyncBaseTransport):
    """A module that never answers within the deadline."""

    def __init__(self) -> None:
        self.calls = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        await asyncio.sleep(30)
        return httpx.Response(200)  # pragma: no cover


@pytest.mark.usefixtures("token")
def test_each_call_has_a_hard_deadline(
    setup: Setup, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(up, "HEAD_SECONDS", 0.05)
    slow = _Slow()
    result = _run(setup, slow, clock)
    assert _states(result) == [(up.UNAVAILABLE, up.TIMEOUT)]
    assert slow.calls == 1 + up.TRANSIENT_RETRIES


@pytest.mark.usefixtures("token")
def test_the_deadline_never_passes_the_budget(
    setup: Setup, module: Module, clock: FakeClock
) -> None:
    limits: list[float] = []
    calls = up._Calls(None, None)
    real = up._Pass(calls, clock, 3.0, lambda: False, lambda *_a: "x", "v")
    assert real.limit(up.HEAD_SECONDS) == 3.0
    clock.now += 2.5
    limits.append(real.limit(up.PUT_SECONDS))
    assert limits == [0.5]


# ── strict answers ───────────────────────────────────────────────────────


def _answer_with(**changes: Any) -> Callable[[httpx.Request], httpx.Response]:
    """The right 200 for the body sent, with `changes`."""

    def answer(request: httpx.Request) -> httpx.Response:
        body = request.read()
        payload = {
            "status": "recorded",
            "sha256": hashlib.sha256(body).hexdigest(),
            "bytes": len(body),
            "rows": 50,
            **changes,
        }
        return httpx.Response(200, json=payload)

    return answer


@pytest.mark.usefixtures("token")
@pytest.mark.parametrize(
    "changes",
    [{"sha256": "0" * 64}, {"bytes": 1}, {"rows": 49}, {"status": "kept"}, {"rows": True}],
    ids=["sha256", "bytes", "rows", "status", "bool-rows"],
)
def test_a_put_answer_that_does_not_give_back_the_body_is_unavailable(
    setup: Setup, module: Module, clock: FakeClock, changes: dict[str, Any]
) -> None:
    module.puts = [_answer_with(**changes)]
    assert _states(_run(setup, module, clock)) == [(up.UNAVAILABLE, up.SHAPE)]


@pytest.mark.usefixtures("token")
def test_the_right_put_answer_is_taken(setup: Setup, module: Module, clock: FakeClock) -> None:
    module.puts = [_answer_with()]
    assert _states(_run(setup, module, clock)) == [(up.SENT, None)]


def _good_json(request: httpx.Request) -> bytes:
    body = request.read()
    return json.dumps(
        {
            "status": "recorded",
            "sha256": hashlib.sha256(body).hexdigest(),
            "bytes": len(body),
            "rows": 50,
        }
    ).encode()


def _streamed_large(request: httpx.Request) -> httpx.Response:
    # No Content-Length: the cap counts the bytes as they come.
    pieces = [_good_json(request), b" " * (up.MAX_ANSWER_BYTES + 1)]

    async def stream() -> AsyncIterator[bytes]:
        for piece in pieces:
            yield piece

    return httpx.Response(200, content=stream())


def _compressed_good(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200, content=gzip.compress(_good_json(request)), headers={"Content-Encoding": "gzip"}
    )


@pytest.mark.usefixtures("token")
@pytest.mark.parametrize(
    "answer", [_streamed_large, _compressed_good], ids=["streamed-large", "compressed-good"]
)
def test_a_good_answer_past_the_cap_or_compressed_is_refused(
    setup: Setup,
    module: Module,
    clock: FakeClock,
    answer: Callable[[httpx.Request], httpx.Response],
) -> None:
    module.puts = [answer]
    assert _states(_run(setup, module, clock)) == [(up.UNAVAILABLE, up.SHAPE)]


@pytest.mark.usefixtures("token")
def test_no_extract_is_made_once_the_head_has_spent_the_budget(
    setup: Setup, module: Module, clock: FakeClock
) -> None:
    def slow_404(request: httpx.Request) -> httpx.Response:
        clock.now += 20.0
        return httpx.Response(404)

    module.heads = [slow_404]
    result = _run(setup, module, clock, budget=15.0)
    assert _states(result) == [(up.SKIPPED, up.BUDGET)]
    assert setup.extractor.calls == []
    assert module.put_requests == []


@pytest.mark.usefixtures("token")
@pytest.mark.parametrize(
    "body",
    [
        {"status": "recorded", "sha256": "0" * 64, "bytes": None, "rows": 50},
        {"status": "kept"},
        {"status": "recorded", "bytes": True, "rows": 50},
        [],
    ],
    ids=["other-sha256", "unknown-status", "bool-bytes", "not-an-object"],
)
def test_a_put_answer_of_another_shape_is_unavailable(
    setup: Setup, module: Module, clock: FakeClock, body: Any
) -> None:
    module.puts = [httpx.Response(200, json=body)]
    assert _states(_run(setup, module, clock)) == [(up.UNAVAILABLE, up.SHAPE)]


@pytest.mark.usefixtures("token")
@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(200, content=b"x" * (up.MAX_ANSWER_BYTES + 1)),
        httpx.Response(200, content=gzip.compress(b"{}"), headers={"Content-Encoding": "gzip"}),
        httpx.Response(200, content=b"not json"),
    ],
    ids=["too-large", "compressed", "not-json"],
)
def test_a_large_or_compressed_answer_is_refused_unread(
    setup: Setup, module: Module, clock: FakeClock, answer: httpx.Response
) -> None:
    module.puts = [answer]
    assert _states(_run(setup, module, clock)) == [(up.UNAVAILABLE, up.SHAPE)]


# ── an older module, or none ─────────────────────────────────────────────


@pytest.mark.usefixtures("token")
def test_a_module_without_the_intake_calls_gives_404_and_the_pass_goes_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module: Module, clock: FakeClock
) -> None:
    # VIATOR deployed before MSMM #42: HEAD and PUT both answer 404.
    setup = Setup(tmp_path, monkeypatch, n=2)
    module.puts = [httpx.Response(404, json={"detail": "Not Found"})]
    result = _run(setup, module, clock)
    assert _states(result) == [(up.UNAVAILABLE, "status_404")] * 2
    assert result.outcomes[0].text == "module unavailable"
    assert len(module.put_requests) == 2


def test_without_the_module_nothing_is_called(
    setup: Setup,
    module: Module,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "station_module_url", "")
    with caplog.at_level(logging.INFO, logger="app.station_extract_upload"):
        result = _run(setup, module, clock)
    assert result.off
    assert result.outcomes == []
    assert module.requests == []
    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (logging.INFO, "station_extract.skipped reason=off")
    ]
    assert up.log_line(result) == ""


@pytest.mark.usefixtures("token")
def test_send_session_without_the_module_reads_no_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "station_module_token", SecretStr(""))

    def no_db() -> Any:
        raise AssertionError("the database was read")

    assert up.send_session("zz", budget=1.0, session_factory=no_db).off


# ── the logs ─────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("token")
def test_the_token_the_session_and_the_feed_key_are_never_logged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    module: Module,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
    token: str,
) -> None:
    setup = Setup(tmp_path, monkeypatch, n=4)
    module.heads = [
        httpx.Response(200),
        httpx.Response(404),
        httpx.Response(404),
        httpx.Response(404),
    ]
    module.puts = [
        _refusal(422, "row"),
        _refusal(503, "busy", "10"),
        None,
        httpx.Response(500, text="stand-in"),
    ]
    with caplog.at_level(logging.DEBUG):
        _run(setup, module, clock)
    # httpx's own request lines are raised to WARNING in VIATOR's processes
    # (logging_config.QUIET_HTTP_CLIENT_LOGGERS): every other line counts.
    text = "\n".join(
        r.getMessage()
        for r in caplog.records
        if r.name.split(".")[0] not in QUIET_HTTP_CLIENT_LOGGERS
    )
    for feed in setup.feeds:
        assert str(feed.feed_key) not in text
        assert str(feed.archive_sha256) not in text
    assert token not in text
    assert SESSION not in text
    # Every reason went to the log, at WARNING, by provider id.
    assert "station_extract.refused provider=ZZ1 reason=row" in text
    assert "station_extract.unavailable provider=ZZ3 reason=status_500" in text


@pytest.mark.usefixtures("token")
def test_an_unexpected_error_ends_the_pass_with_one_warning(
    setup: Setup, module: Module, clock: FakeClock, caplog: pytest.LogCaptureFixture, token: str
) -> None:
    def broken(*_a: Any, **_kw: Any) -> Any:
        raise RuntimeError(f"stand-in {token}")

    with caplog.at_level(logging.INFO, logger="app.station_extract_upload"):
        result = up.send_session_extracts(
            SESSION,
            setup.feeds,
            budget=10.0,
            clock=clock,
            transport=httpx.MockTransport(module),
            extract=broken,
            record=False,
        )
    assert result.outcomes == []
    assert "station_extract.failed reason=error type=RuntimeError" in caplog.text
    assert token not in caplog.text


# ── what the extractor gives ─────────────────────────────────────────────


@pytest.mark.usefixtures("token")
def test_no_extract_skips_the_feed_with_the_extractors_word(
    setup: Setup, module: Module, clock: FakeClock
) -> None:
    setup.extractor.fail = "too_large"
    result = _run(setup, module, clock)
    assert _states(result) == [(up.SKIPPED, "too_large")]
    assert result.outcomes[0].text == "skipped"
    assert module.put_requests == []


@pytest.mark.usefixtures("token")
@pytest.mark.parametrize("field", ["feed_key", "feed_version", "bytes"])
def test_an_extract_that_does_not_match_its_feed_is_not_sent(
    setup: Setup, module: Module, clock: FakeClock, field: str
) -> None:
    real = setup.extractor

    def wrong(*args: Any, **kwargs: Any) -> station_extract.ExtractResult:
        made = real(*args, **kwargs)
        changes: dict[str, Any] = {
            "feed_key": {"feed_key": _url_key()},
            "feed_version": {"feed_version": _sha()},
            "bytes": {"bytes": made.bytes + 1},
        }[field]
        return station_extract.ExtractResult(**{**made.__dict__, **changes})

    station_extract.build_extract = wrong  # type: ignore[assignment]
    try:
        assert _states(_run(setup, module, clock)) == [(up.SKIPPED, up.NO_EXTRACT)]
    finally:
        station_extract.build_extract = real  # type: ignore[assignment]
    assert module.put_requests == []


@pytest.mark.usefixtures("token")
def test_feeds_without_a_key_or_an_archive_are_skipped_without_a_call(
    tmp_path: Path, module: Module, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    feeds = [
        up.Feed("ZZX", {"id": "ZZX"}, None, tmp_path / "a.zip", _sha()),
        up.Feed("ZZY", {"id": "ZZY"}, _url_key(), None, None),
    ]
    with caplog.at_level(logging.WARNING):
        result = up.send_session_extracts(
            SESSION,
            feeds,
            budget=10.0,
            clock=clock,
            transport=httpx.MockTransport(module),
            record=False,
        )
    assert _states(result) == [(up.SKIPPED, up.NO_FEED_KEY), (up.SKIPPED, up.NO_ARCHIVE)]
    assert module.requests == []
    assert caplog.records == []


@pytest.mark.usefixtures("token")
def test_the_budget_spent_skips_the_feeds_left(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module: Module, clock: FakeClock
) -> None:
    setup = Setup(tmp_path, monkeypatch, n=3)

    def slow_head(request: httpx.Request) -> httpx.Response:
        clock.now += 6.0
        return httpx.Response(200)

    module.heads = [slow_head]
    result = _run(setup, module, clock, budget=10.0)
    assert _states(result) == [(up.HELD, None), (up.HELD, None), (up.SKIPPED, up.BUDGET)]
    assert len(module.head_requests) == 2


# ── stopping ─────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("token")
def test_a_cancel_ends_the_pass_before_the_next_feed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module: Module, clock: FakeClock
) -> None:
    setup = Setup(tmp_path, monkeypatch, n=3)
    asked = iter([False, True])
    result = _run(setup, module, clock, should_stop=lambda: next(asked))
    assert result.cancelled
    assert len(result.outcomes) == 1
    assert up.log_line(result) == "[viator] station-extracts: cancelled by an operator\n"


@pytest.mark.usefixtures("token")
def test_a_cancel_during_a_pause_ends_the_pass(
    setup: Setup, module: Module, clock: FakeClock
) -> None:
    module.puts = [_refusal(503, "busy", "10")]
    asked = iter([False, True])
    result = _run(setup, module, clock, should_stop=lambda: next(asked))
    assert result.cancelled
    assert clock.slept == [10.0]
    assert len(module.put_requests) == 1


@pytest.mark.usefixtures("token")
def test_keyboard_interrupt_is_never_caught(setup: Setup, module: Module, clock: FakeClock) -> None:
    def interrupt(request: httpx.Request) -> httpx.Response:
        raise KeyboardInterrupt

    module.heads = [interrupt]
    with pytest.raises(KeyboardInterrupt):
        _run(setup, module, clock)


# ── streaming ────────────────────────────────────────────────────────────


class _Counting(httpx.AsyncBaseTransport):
    """A module that reads the body piece by piece, never keeping it."""

    def __init__(self) -> None:
        self.largest = 0
        self.total = 0
        self.digest = hashlib.sha256()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(404)
        assert isinstance(request.stream, httpx.AsyncByteStream)
        async for piece in request.stream:
            self.largest = max(self.largest, len(piece))
            self.total += len(piece)
            self.digest.update(piece)
        return httpx.Response(
            200,
            json={
                "status": "recorded",
                "sha256": self.digest.hexdigest(),
                "bytes": self.total,
                "rows": 50,
            },
        )


@pytest.mark.usefixtures("token")
def test_the_extract_is_streamed_from_its_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    size = 24 * 1024 * 1024
    big = tmp_path / "big.csv.gz"
    with big.open("wb") as out:
        for _ in range(size // (1024 * 1024)):
            out.write(secrets.token_bytes(1024 * 1024))
    digest = hashlib.sha256(big.read_bytes()).hexdigest()
    key, sha = _url_key(), _sha()
    made = station_extract.ExtractResult(big, key, sha, "v", 50, size, digest)
    feed = up.Feed("ZZ0", {"id": "ZZ0"}, key, tmp_path / "a.zip", sha)
    monkeypatch.setattr(station_extract, "build_extract", lambda *_a, **_kw: made)
    counting = _Counting()
    tracemalloc.start()
    try:
        result = up.send_session_extracts(
            SESSION, [feed], budget=60.0, clock=clock, transport=counting, record=False
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert _states(result) == [(up.SENT, None)]
    assert counting.total == size
    assert counting.largest <= up.CHUNK_BYTES
    assert peak < 4 * 1024 * 1024


# ── the record and the admin's text ──────────────────────────────────────


@pytest.mark.parametrize(
    ("state", "reason", "text"),
    [
        (up.SENT, None, "sent"),
        (up.HELD, None, "already held"),
        (up.REFUSED, "row", "refused row"),
        (up.SKIPPED, up.BUDGET, "skipped"),
        (up.UNAVAILABLE, "status_404", "module unavailable"),
    ],
)
def test_the_admin_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str, reason: str | None, text: str
) -> None:
    monkeypatch.setattr(settings, "inbox_dir", tmp_path)
    up.record_outcome("zz", up.FeedOutcome("ZZ0", state, reason))
    loaded = up.load_outcome("zz", "ZZ0")
    assert loaded is not None
    assert loaded["text"] == text
    assert loaded["state"] == state
    assert loaded["reason"] == reason
    assert (tmp_path / "zz" / "_fetch_state" / "provider_ZZ0_.station_extract.json").is_file()


def test_a_record_of_another_form_shows_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "inbox_dir", tmp_path)
    assert up.load_outcome("zz", "ZZ0") is None
    folder = tmp_path / "zz" / "_fetch_state"
    folder.mkdir(parents=True)
    path = folder / "provider_ZZ0_.station_extract.json"
    path.write_text(json.dumps({"state": "<b>x</b>"}), encoding="utf-8")
    assert up.load_outcome("zz", "ZZ0") is None
    path.write_text(json.dumps({"state": "refused", "reason": "<b>"}), encoding="utf-8")
    loaded = up.load_outcome("zz", "ZZ0")
    assert loaded is not None
    assert loaded["text"] == "refused"


@pytest.mark.usefixtures("token")
def test_a_pass_records_each_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module: Module, clock: FakeClock
) -> None:
    monkeypatch.setattr(settings, "inbox_dir", tmp_path / "inbox")
    setup = Setup(tmp_path, monkeypatch, n=2)
    module.heads = [httpx.Response(200), httpx.Response(404)]
    up.send_session_extracts(
        "zz", setup.feeds, budget=60.0, clock=clock, transport=httpx.MockTransport(module)
    )
    assert up.load_outcome("zz", "ZZ0")["text"] == "already held"  # type: ignore[index]
    assert up.load_outcome("zz", "ZZ1")["text"] == "sent"  # type: ignore[index]


def test_the_job_line_counts() -> None:
    result = up.PassResult(
        outcomes=[
            up.FeedOutcome("A", up.SENT),
            up.FeedOutcome("B", up.HELD),
            up.FeedOutcome("C", up.HELD),
            up.FeedOutcome("D", up.REFUSED, "row"),
        ]
    )
    assert up.log_line(result) == "[viator] station-extracts: sent=1 held=2 refused=1\n"
    assert up.log_line(up.PassResult()) == "[viator] station-extracts: no feed\n"


# ── the feeds of a session ───────────────────────────────────────────────


def test_session_feeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "inbox_dir", tmp_path)
    sid = "zz"
    (tmp_path / sid / "gtfs").mkdir(parents=True)
    (tmp_path / sid / "netex").mkdir(parents=True)
    (tmp_path / sid / "gtfs" / "zzurl.zip").write_bytes(b"PK")
    (tmp_path / sid / "gtfs" / "zzup.zip").write_bytes(b"PK")
    fetched, uploaded = _sha(), _sha()
    from app import feed_fetch

    feed_fetch.save_state(
        tmp_path / sid / "_fetch_state", "provider[ZZURL].timetable(gtfs)", {"sha256": fetched}
    )
    config = {
        "sources": {
            "providers": [
                {
                    "id": "ZZURL",
                    "label": "ZZ url",
                    "timetable": {"format": "gtfs", "url": "https://zz.invalid/a.zip"},
                },
                {
                    "id": "ZZUP",
                    "label": "ZZ upload",
                    "timetable": {"format": "gtfs", "source": "upload"},
                },
                {
                    "id": "ZZNONE",
                    "label": "ZZ none",
                    "timetable": {"format": "netex_epip", "url": "https://zz.invalid/b.zip"},
                },
            ]
        }
    }
    feeds = {f.provider_id: f for f in up.session_feeds(sid, config, {"ZZUP": uploaded})}
    assert feeds["ZZURL"].archive == tmp_path / sid / "gtfs" / "zzurl.zip"
    assert feeds["ZZURL"].archive_sha256 == fetched
    assert feeds["ZZURL"].feed_key is not None
    assert feeds["ZZURL"].feed_key.startswith("url:")
    assert feeds["ZZUP"].feed_key == f"upload:{uploaded}"
    assert feeds["ZZUP"].archive_sha256 == uploaded
    assert feeds["ZZNONE"].archive is None
    assert feeds["ZZNONE"].archive_sha256 is None


def test_a_malformed_config_gives_no_feed() -> None:
    assert up.session_feeds("zz", {"sources": "zz"}, {}) == []


def test_the_refresh_task_uses_the_refresh_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def send(sid: str, **kw: Any) -> up.PassResult:
        seen.update(kw, sid=sid)
        return up.PassResult()

    monkeypatch.setattr(up, "send_session", send)
    up.send_after_refresh("zz", "ZZ0")
    assert seen == {"sid": "zz", "budget": up.REFRESH_BUDGET_SECONDS, "only_provider": "ZZ0"}


@pytest.mark.usefixtures("token")
def test_each_outcome_is_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module: Module, clock: FakeClock
) -> None:
    from app.metrics import STATION_EXTRACT_FEEDS_TOTAL, STATION_EXTRACT_OUTCOMES

    def value(outcome: str) -> float:
        return STATION_EXTRACT_FEEDS_TOTAL.labels(outcome=outcome)._value.get()

    assert set(STATION_EXTRACT_OUTCOMES) == {
        up.SENT,
        up.HELD,
        up.REFUSED,
        up.SKIPPED,
        up.UNAVAILABLE,
    }
    before = {o: value(o) for o in STATION_EXTRACT_OUTCOMES}
    setup = Setup(tmp_path, monkeypatch, n=3)
    module.heads = [httpx.Response(200), httpx.Response(404), httpx.Response(404)]
    module.puts = [None, _refusal(422, "row")]
    _run(setup, module, clock)
    after = {o: value(o) - before[o] for o in STATION_EXTRACT_OUTCOMES}
    assert after == {"sent": 1, "held": 1, "refused": 1, "skipped": 0, "unavailable": 0}
