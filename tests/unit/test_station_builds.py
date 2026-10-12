"""The station build of the module before each MOTIS graph build (MSMM decision 6).

The module is never reached: a fake module answers through
`httpx.MockTransport`, and a fake clock replaces every pause, so no test
sleeps. Values are invented: a `.invalid` address, build numbers beginning
with 99, a token drawn at run time.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import secrets
import socket
import threading
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app import station_builds as sb
from app.settings import settings

MODULE_URL = "http://msmm.invalid:8000"
BUILD = 9901


class FakeClock:
    """The time of the wait: `sleep` moves it on at once."""

    def __init__(self) -> None:
        self.now = 5000.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _state(state: str, build_id: int = BUILD, **extra: Any) -> dict[str, Any]:
    ended = state in ("done", "failed", "up_to_date")
    return {
        "build_id": build_id,
        "state": state,
        "started_at": None if state == "queued" else "2026-10-12T09:00:00+00:00",
        "finished_at": "2026-10-12T09:03:00+00:00" if ended else None,
        **extra,
    }


Answer = httpx.Response | Callable[[httpx.Request], httpx.Response]


class Module:
    """A fake module: `post` answers the POST; `gets` answer the GETs in turn
    (the last one again when they run out). Every request is recorded."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.requests: list[httpx.Request] = []
        self.post: list[Answer] = [httpx.Response(200, json=_state("queued"))]
        self.gets: list[Answer] = [httpx.Response(200, json=_state("done"))]

    def _next(self, answers: list[Answer], request: httpx.Request) -> httpx.Response:
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        return answer(request) if callable(answer) else answer

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "POST":
            return self._next(self.post, request)
        return self._next(self.gets, request)

    @property
    def posts(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "POST"]

    @property
    def polls(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "GET"]


def timeout_after_five(clock: FakeClock) -> Callable[[httpx.Request], httpx.Response]:
    def answer(request: httpx.Request) -> httpx.Response:
        clock.now += sb.CALL_SECONDS
        raise httpx.ReadTimeout("stand-in timeout", request=request)

    return answer


def refused_connection(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("stand-in refusal", request=request)


def reset_connection(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadError("stand-in reset", request=request)


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
def module(clock: FakeClock) -> Module:
    return Module(clock)


class Run:
    def __init__(self) -> None:
        self.waiting: list[int] = []
        self.stop = False


def _wait(module: Module, clock: FakeClock, run: Run | None = None) -> sb.WaitResult:
    run = run or Run()
    return sb.wait_before_graph(
        should_stop=lambda: run.stop,
        on_waiting=run.waiting.append,
        clock=clock,
        transport=httpx.MockTransport(module),
    )


def test_the_numbers_are_the_contracts() -> None:
    # The module's design, 21.18, points 1 and 2: written out, not read back.
    assert sb.POLL_SECONDS == 10.0
    assert sb.CALL_SECONDS == 5.0
    assert sb.WAIT_LIMIT_SECONDS == 1800.0
    assert sb.POST_RETRY_SECONDS == 10.0


# ── the outcomes ─────────────────────────────────────────────────────────


@pytest.mark.usefixtures("token")
def test_a_queued_build_is_polled_until_done(module: Module, clock: FakeClock) -> None:
    module.gets = [
        httpx.Response(200, json=_state("queued")),
        httpx.Response(200, json=_state("running")),
        httpx.Response(200, json=_state("done")),
    ]
    run = Run()
    result = _wait(module, clock, run)
    assert result == sb.WaitResult(sb.DONE, None, BUILD)
    assert result.build_graph
    assert len(module.posts) == 1
    assert [r.url.path for r in module.polls] == [f"/internal/v1/builds/{BUILD}"] * 3
    assert clock.slept == [sb.POLL_SECONDS] * 3
    assert run.waiting == [BUILD]


@pytest.mark.usefixtures("token")
@pytest.mark.parametrize("state", ["up_to_date", "done"])
def test_a_build_already_ended_needs_no_poll(module: Module, clock: FakeClock, state: str) -> None:
    module.post = [httpx.Response(200, json=_state(state))]
    run = Run()
    result = _wait(module, clock, run)
    assert result == sb.WaitResult(state, None, BUILD)
    assert module.polls == []
    assert clock.slept == []
    assert run.waiting == []


@pytest.mark.usefixtures("token")
def test_a_failed_build_ends_the_wait_with_a_warning(
    module: Module, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    module.gets = [httpx.Response(200, json=_state("failed"))]
    with caplog.at_level(logging.INFO, logger="app.station_builds"):
        result = _wait(module, clock)
    assert result == sb.WaitResult(sb.UNAVAILABLE, sb.FAILED, BUILD)
    assert result.build_graph
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == [f"station_build.unavailable reason=failed build_id={BUILD}"]


@pytest.mark.usefixtures("token")
@pytest.mark.parametrize(
    ("status", "code", "reason"),
    [
        (409, "no_inputs", sb.NO_INPUTS),
        (429, "build_interval", sb.BUILD_INTERVAL),
        (403, "token", sb.FORBIDDEN),
        # A module older than the builds calls (VIATOR deployed first).
        (404, None, "status_404"),
        (500, None, "status_500"),
    ],
)
def test_a_refused_request_ends_the_wait_at_once(
    module: Module,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
    status: int,
    code: str | None,
    reason: str,
) -> None:
    body = {"detail": "stand-in", "code": code} if code else {"detail": "Not Found"}
    module.post = [httpx.Response(status, json=body, headers={"Retry-After": "99"})]
    with caplog.at_level(logging.INFO, logger="app.station_builds"):
        result = _wait(module, clock)
    assert result == sb.WaitResult(sb.UNAVAILABLE, reason, None)
    assert result.build_graph
    assert len(module.requests) == 1
    assert clock.slept == []
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == [f"station_build.unavailable reason={reason}"]


@pytest.mark.usefixtures("token")
@pytest.mark.parametrize(("status", "reason"), [(403, sb.FORBIDDEN), (404, sb.UNKNOWN_BUILD)])
def test_a_refused_poll_ends_the_wait(
    module: Module, clock: FakeClock, status: int, reason: str
) -> None:
    module.gets = [httpx.Response(status, json={"detail": "stand-in", "code": "x"})]
    result = _wait(module, clock)
    assert result == sb.WaitResult(sb.UNAVAILABLE, reason, BUILD)
    assert len(module.polls) == 1


@pytest.mark.usefixtures("token")
@pytest.mark.parametrize(
    "transient",
    ["status_503", "timeout", "refused", "reset", "status_500", "shape", "other_build"],
)
def test_a_transient_poll_failure_is_asked_again(
    module: Module, clock: FakeClock, transient: str
) -> None:
    first: Answer = {
        "status_503": httpx.Response(503, json={"detail": "stand-in", "code": "busy"}),
        "timeout": timeout_after_five(clock),
        "refused": refused_connection,
        "reset": reset_connection,
        "status_500": httpx.Response(500, text="stand-in"),
        "shape": httpx.Response(200, json={"build_id": BUILD}),
        "other_build": httpx.Response(200, json=_state("done", build_id=BUILD + 1)),
    }[transient]
    module.gets = [first, httpx.Response(200, json=_state("done"))]
    result = _wait(module, clock)
    assert result == sb.WaitResult(sb.DONE, None, BUILD)
    assert len(module.polls) == 2
    assert len(module.posts) == 1


@pytest.mark.usefixtures("token")
def test_a_poll_retry_is_logged_at_debug_only(
    module: Module, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    module.gets = [
        httpx.Response(503, json={"code": "busy"}),
        httpx.Response(200, json=_state("done")),
    ]
    with caplog.at_level(logging.DEBUG, logger="app.station_builds"):
        _wait(module, clock)
    retries = [r for r in caplog.records if r.getMessage().startswith("station_build.poll_retry")]
    assert [(r.levelno, r.getMessage()) for r in retries] == [
        (logging.DEBUG, "station_build.poll_retry reason=status_503")
    ]


@pytest.mark.usefixtures("token")
def test_thirty_minutes_end_the_wait(
    module: Module, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    module.gets = [httpx.Response(200, json=_state("running"))]
    start = clock.now
    with caplog.at_level(logging.INFO, logger="app.station_builds"):
        result = _wait(module, clock)
    assert result == sb.WaitResult(sb.UNAVAILABLE, sb.TIMEOUT, BUILD)
    assert clock.now - start == sb.WAIT_LIMIT_SECONDS
    # One GET every 10 s, never at the limit itself, and never a second POST.
    assert len(module.polls) == int(sb.WAIT_LIMIT_SECONDS / sb.POLL_SECONDS) - 1
    assert len(module.posts) == 1
    assert max(clock.slept) == sb.POLL_SECONDS
    # No empty pause at the limit: the wait ends as soon as nothing is left.
    assert min(clock.slept) > 0
    assert "station_build.unavailable reason=timeout" in caplog.text


@pytest.mark.usefixtures("token")
def test_the_thirty_minutes_count_from_the_first_request(module: Module, clock: FakeClock) -> None:
    # The first POST times out (5 s), the retry 10 s later answers `queued`,
    # then the build runs for ever: polls at 25 s to 1795 s, the last pause
    # cut to the 5 s left, the end at 1800 s from the first POST.
    module.post = [timeout_after_five(clock), httpx.Response(200, json=_state("queued"))]
    module.gets = [httpx.Response(200, json=_state("running"))]
    start = clock.now
    result = _wait(module, clock)
    assert result == sb.WaitResult(sb.UNAVAILABLE, sb.TIMEOUT, BUILD)
    assert clock.now - start == 1800.0
    assert len(module.posts) == 2
    assert len(module.polls) == 178
    assert clock.slept[0] == 10.0
    assert clock.slept[-1] == 5.0


@pytest.mark.usefixtures("token")
def test_slow_polls_still_end_at_thirty_minutes(module: Module, clock: FakeClock) -> None:
    module.gets = [timeout_after_five(clock)]
    start = clock.now
    result = _wait(module, clock)
    assert result == sb.WaitResult(sb.UNAVAILABLE, sb.TIMEOUT, BUILD)
    assert clock.now - start <= sb.WAIT_LIMIT_SECONDS
    # Each round is a pause of 10 s and a call of 5 s.
    assert len(module.polls) == 120
    # The last call ends at the limit itself: no empty pause follows it.
    assert clock.now - start == sb.WAIT_LIMIT_SECONDS
    assert min(clock.slept) > 0


@pytest.mark.usefixtures("token")
@pytest.mark.parametrize("transient", ["status_503", "timeout", "refused", "reset"])
def test_a_transient_request_failure_is_tried_once_more_after_ten_seconds(
    module: Module, clock: FakeClock, transient: str
) -> None:
    first: Answer = {
        "status_503": httpx.Response(503, json={"detail": "stand-in", "code": "database"}),
        "timeout": timeout_after_five(clock),
        "refused": refused_connection,
        "reset": reset_connection,
    }[transient]
    module.post = [first, httpx.Response(200, json=_state("up_to_date"))]
    result = _wait(module, clock)
    assert result == sb.WaitResult(sb.UP_TO_DATE, None, BUILD)
    assert len(module.posts) == 2
    assert clock.slept == [sb.POST_RETRY_SECONDS]


@pytest.mark.usefixtures("token")
def test_a_second_transient_request_failure_ends_the_wait(module: Module, clock: FakeClock) -> None:
    module.post = [httpx.Response(503, json={"detail": "stand-in", "code": "busy"})]
    result = _wait(module, clock)
    assert result == sb.WaitResult(sb.UNAVAILABLE, sb.UNREACHABLE, None)
    assert len(module.posts) == 2
    assert module.polls == []
    assert clock.slept == [sb.POST_RETRY_SECONDS]


def test_without_the_module_nothing_waits(
    monkeypatch: pytest.MonkeyPatch,
    module: Module,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(settings, "station_module_url", MODULE_URL)
    monkeypatch.setattr(settings, "station_module_token", SecretStr(""))
    with caplog.at_level(logging.INFO, logger="app.station_builds"):
        result = _wait(module, clock)
    assert result == sb.WaitResult(sb.OFF)
    assert result.build_graph
    assert module.requests == []
    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (logging.INFO, "station_build.skipped reason=off")
    ]


# ── the calls ────────────────────────────────────────────────────────────


def test_the_calls_carry_the_token_and_no_user_id(
    token: str, module: Module, clock: FakeClock
) -> None:
    module.gets = [httpx.Response(200, json=_state("done"))]
    _wait(module, clock)
    post, get = module.requests
    assert str(post.url) == f"{MODULE_URL}/internal/v1/builds"
    assert post.content == b"{}"
    assert post.headers["content-type"] == "application/json"
    assert str(get.url) == f"{MODULE_URL}/internal/v1/builds/{BUILD}"
    assert get.content == b""
    for request in (post, get):
        assert request.headers["authorization"] == f"Bearer {token}"
        assert request.headers["accept-encoding"] == "identity"
        assert "x-viator-user-id" not in request.headers


def test_the_client_ignores_proxy_variables() -> None:
    client = sb._client(None)
    try:
        assert client.trust_env is False
        assert client.follow_redirects is False
        assert client.timeout == httpx.Timeout(sb.CALL_SECONDS)
    finally:
        asyncio.run(client.aclose())


@pytest.mark.usefixtures("token")
@pytest.mark.parametrize(
    "body",
    [
        [],
        "done",
        {"build_id": BUILD, "state": "done", "started_at": None},  # a field missing
        _state("done", build_id=True),
        _state("done", build_id="9901"),
        _state("done", build_id=0),
        _state("done", build_id=10**18),
        _state("unknown"),
        {**_state("done"), "started_at": "2026-10-12T09:00:00"},  # no offset
        {**_state("done"), "finished_at": "yesterday"},
        {**_state("done"), "finished_at": 1760000000},
    ],
    ids=lambda b: "body",
)
def test_a_request_answered_in_a_wrong_shape_ends_the_wait(
    module: Module, clock: FakeClock, body: Any
) -> None:
    module.post = [httpx.Response(200, json=body)]
    assert _wait(module, clock) == sb.WaitResult(sb.UNAVAILABLE, sb.SHAPE, None)
    assert module.polls == []


@pytest.mark.usefixtures("token")
def test_a_field_the_contract_does_not_name_is_ignored(module: Module, clock: FakeClock) -> None:
    # A later module may add a field: the four are checked, others ignored.
    module.post = [httpx.Response(200, json={**_state("up_to_date"), "zz_later": 1})]
    assert _wait(module, clock) == sb.WaitResult(sb.UP_TO_DATE, None, BUILD)


@pytest.mark.usefixtures("token")
def test_a_poll_never_answers_up_to_date(module: Module, clock: FakeClock) -> None:
    module.gets = [
        httpx.Response(200, json=_state("up_to_date")),
        httpx.Response(200, json=_state("done")),
    ]
    assert _wait(module, clock) == sb.WaitResult(sb.DONE, None, BUILD)
    assert len(module.polls) == 2


@pytest.mark.usefixtures("token")
def test_a_body_that_is_not_json_is_a_wrong_shape(module: Module, clock: FakeClock) -> None:
    module.post = [httpx.Response(200, text="not json")]
    assert _wait(module, clock) == sb.WaitResult(sb.UNAVAILABLE, sb.SHAPE, None)


@pytest.mark.usefixtures("token")
@pytest.mark.parametrize(
    "response",
    [
        lambda _r: httpx.Response(
            200,
            content=gzip.compress(json.dumps(_state("done")).encode()),
            headers={"Content-Encoding": "gzip"},
        ),
        lambda _r: httpx.Response(200, content=b" " * (sb.MAX_BYTES + 1)),
    ],
    ids=["compressed", "too_large"],
)
def test_a_compressed_or_large_answer_is_refused_unread(
    module: Module, clock: FakeClock, response: Answer
) -> None:
    # Refused as a transient failure: the request is tried once more.
    module.post = [response]
    assert _wait(module, clock) == sb.WaitResult(sb.UNAVAILABLE, sb.UNREACHABLE, None)
    assert len(module.posts) == 2


@pytest.mark.usefixtures("token")
def test_a_large_body_without_its_length_is_cut_while_read(
    module: Module, clock: FakeClock
) -> None:
    async def chunks() -> AsyncIterator[bytes]:
        # Bounded: without the cap the body is read to its end and refused as
        # not JSON (`shape`), so the test fails cleanly instead of running away.
        for _ in range(64):
            yield b" " * 1024

    module.post = [lambda _r: httpx.Response(200, content=chunks())]
    assert _wait(module, clock) == sb.WaitResult(sb.UNAVAILABLE, sb.UNREACHABLE, None)
    assert len(module.posts) == 2


@pytest.mark.usefixtures("token")
def test_an_announced_large_body_is_refused_unread(module: Module, clock: FakeClock) -> None:
    read: list[int] = []

    async def chunks() -> AsyncIterator[bytes]:
        read.append(1)
        yield json.dumps(_state("done")).encode()

    module.post = [
        lambda _r: httpx.Response(
            200, content=chunks(), headers={"Content-Length": str(sb.MAX_BYTES + 1)}
        )
    ]
    assert _wait(module, clock) == sb.WaitResult(sb.UNAVAILABLE, sb.UNREACHABLE, None)
    assert read == []


@pytest.mark.usefixtures("token")
def test_a_body_trickled_past_five_seconds_is_cut_at_five_seconds() -> None:
    """A valid `done` answer, one byte every 0.1 s: the call is cut at 5 s
    (real time), whatever httpx's per-read timeouts would allow."""
    body = json.dumps(_state("done")).encode()

    async def trickle() -> AsyncIterator[bytes]:
        for byte in body:
            await asyncio.sleep(0.1)
            yield bytes([byte])

    calls = sb._Calls(httpx.MockTransport(lambda _r: httpx.Response(200, content=trickle())))
    started = time.monotonic()
    with pytest.raises(sb._Transient) as caught:
        calls.request(sb.CALL_SECONDS)
    elapsed = time.monotonic() - started
    assert caught.value.reason == sb.TIMEOUT
    assert sb.CALL_SECONDS <= elapsed < sb.CALL_SECONDS + 1.0


@pytest.mark.usefixtures("token")
def test_a_status_line_and_headers_trickled_are_cut_at_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real socket sending its status line and headers one byte every 0.1 s:
    the call ends at its deadline (1 s here), not when the headers end."""
    body = json.dumps(_state("done")).encode()
    answer = (
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
        + b"Content-Length: %d\r\n\r\n" % len(body)
        + body
    )
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    stop = threading.Event()

    def serve() -> None:
        connection, _ = server.accept()
        with connection:
            connection.recv(65536)
            for byte in answer:
                if stop.wait(0.1):
                    return
                try:
                    connection.send(bytes([byte]))
                except OSError:
                    return

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    monkeypatch.setattr(
        settings, "station_module_url", f"http://127.0.0.1:{server.getsockname()[1]}"
    )
    started = time.monotonic()
    try:
        with pytest.raises(sb._Transient) as caught:
            sb._Calls(None).state(BUILD, 1.0)
    finally:
        stop.set()
        thread.join(timeout=5)
        server.close()
    elapsed = time.monotonic() - started
    assert caught.value.reason == sb.TIMEOUT
    assert 1.0 <= elapsed < 2.0


@pytest.mark.usefixtures("token")
def test_each_call_gets_five_seconds_or_what_is_left(
    monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    limits: list[tuple[str, float]] = []

    def send(_self: sb._Calls, method: str, _path: str, limit: float) -> tuple[int, bytes]:
        limits.append((method, limit))
        state = "queued" if method == "POST" else "done"
        return 200, json.dumps(_state(state)).encode()

    monkeypatch.setattr(sb._Calls, "_send", send)
    assert _wait(Module(clock), clock) == sb.WaitResult(sb.DONE, None, BUILD)
    assert limits == [("POST", sb.CALL_SECONDS), ("GET", sb.CALL_SECONDS)]
    wait = sb._Wait(sb._Calls(None), clock, lambda: False, lambda _b: None)
    clock.now = wait.deadline - 2.0
    assert wait._limit() == 2.0
    clock.now = wait.deadline + 1.0
    assert wait._limit() == 0.0


# ── the log ──────────────────────────────────────────────────────────────


def test_the_token_is_never_logged(
    token: str, module: Module, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    def leaky(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"stand-in {request.headers['authorization']}", request=request)

    def broken(request: httpx.Request) -> httpx.Response:
        raise ValueError(f"stand-in {request.headers['authorization']}")

    with caplog.at_level(logging.DEBUG):
        for answers in (
            [leaky],
            [broken],
            [httpx.Response(403, json={"detail": "stand-in", "code": "token"})],
        ):
            module.post = list(answers)
            _wait(module, clock)
        module.post = [httpx.Response(200, json=_state("queued"))]
        module.gets = [leaky, httpx.Response(200, json=_state("failed"))]
        _wait(module, clock)
    assert caplog.records
    assert all(token not in r.getMessage() for r in caplog.records)
    assert token not in caplog.text
    assert "station_build.unavailable reason=error type=ValueError" in caplog.text


@pytest.mark.usefixtures("token")
def test_an_unexpected_error_ends_the_wait_and_the_graph_is_built(
    module: Module, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(_request: httpx.Request) -> httpx.Response:
        raise RuntimeError("stand-in")

    module.post = [broken]
    with caplog.at_level(logging.INFO, logger="app.station_builds"):
        result = _wait(module, clock)
    assert result == sb.WaitResult(sb.UNAVAILABLE, sb.ERROR, None)
    assert result.build_graph
    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (logging.WARNING, "station_build.unavailable reason=error type=RuntimeError")
    ]


def test_a_failing_configuration_check_ends_the_wait(
    monkeypatch: pytest.MonkeyPatch, module: Module, clock: FakeClock
) -> None:
    def broken() -> bool:
        raise RuntimeError("stand-in")

    monkeypatch.setattr(sb.station_module, "enabled", broken)
    assert _wait(module, clock) == sb.WaitResult(sb.UNAVAILABLE, sb.ERROR, None)
    assert module.requests == []


# ── stopping ─────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("token")
def test_a_cancel_ends_the_wait_within_one_poll(module: Module, clock: FakeClock) -> None:
    run = Run()

    def running_then_cancelled(_request: httpx.Request) -> httpx.Response:
        run.stop = True
        return httpx.Response(200, json=_state("running"))

    module.gets = [running_then_cancelled]
    result = _wait(module, clock, run)
    assert result == sb.WaitResult(sb.CANCELLED, None, BUILD)
    assert not result.build_graph
    assert len(module.polls) == 1


@pytest.mark.usefixtures("token")
def test_a_cancel_during_the_retry_pause_sends_no_second_request(
    module: Module, clock: FakeClock
) -> None:
    run = Run()

    def busy(_request: httpx.Request) -> httpx.Response:
        run.stop = True
        return httpx.Response(503, json={"detail": "stand-in", "code": "busy"})

    module.post = [busy]
    result = _wait(module, clock, run)
    assert result == sb.WaitResult(sb.CANCELLED)
    assert len(module.posts) == 1


@pytest.mark.usefixtures("token")
@pytest.mark.parametrize("stop", [SystemExit, KeyboardInterrupt])
def test_a_worker_shutdown_goes_through_the_wait(
    module: Module, clock: FakeClock, stop: type[BaseException]
) -> None:
    def interrupted(_seconds: float) -> None:
        raise stop

    clock.sleep = interrupted  # type: ignore[method-assign]
    with pytest.raises(stop):
        _wait(module, clock)
    assert len(module.polls) == 0


def test_the_system_clock_sleeps(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr(sb.time, "sleep", slept.append)
    clock = sb.SystemClock()
    clock.sleep(0.25)
    assert slept == [0.25]
    assert clock.monotonic() > 0


# ── the status the admin sees ────────────────────────────────────────────


def test_each_step_has_its_line_and_sentence() -> None:
    assert sb.log_line(sb.WAITING, build_id=BUILD) == (
        f"[viator] station-build=waiting build={BUILD} · Waiting for the station module build\n"
    )
    assert sb.log_line(sb.UNAVAILABLE, reason="no_inputs") == (
        "[viator] station-build=unavailable reason=no_inputs · Station module unavailable"
        " — graph built on the last station build\n"
    )
    assert sb.STATUS_TEXTS[sb.DONE] == "Station build done"
    assert sb.STATUS_TEXTS[sb.UP_TO_DATE] == "Station build up to date"


def test_the_last_step_of_the_log_is_the_status() -> None:
    text = (
        "queued\n"
        + sb.log_line(sb.WAITING, build_id=BUILD)
        + sb.log_line(sb.DONE, build_id=BUILD)
        + "motis import ...\n"
    )
    assert sb.status_from_log(text) == (sb.DONE, "Station build done")
    assert sb.status_from_log(sb.log_line(sb.WAITING, build_id=BUILD)) == (
        sb.WAITING,
        "Waiting for the station module build",
    )
    assert sb.status_from_log("[viator] station-build=done\n") == (sb.DONE, "Station build done")


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "motis import ...\n",
        "[viator] station-build=bogus · x\n",
        "  [viator] station-build=done\n",
    ],
    ids=["none", "empty", "other", "unknown_word", "not_at_line_start"],
)
def test_a_log_without_a_step_has_no_status(text: str | None) -> None:
    assert sb.status_from_log(text) is None
