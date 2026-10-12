"""A station build of the module before each MOTIS graph build (MSMM decision 6, option B).

The worker asks the Multimodal Station Mapping module (MSMM) for a station
build, waits until that build has ended, and only then builds the session's
MOTIS graph, so that the graph is never built while the module's station
reference changes, nor just before a new one is committed (the module's
design, 21.18, "The VIATOR side: the contract"; issue MSMM #9).

Two internal calls of the module, a service call of VIATOR's backend:

- `POST /internal/v1/builds` with the body `{}`: the build to wait for. A
  build queued or running is the answer and is never doubled; inputs
  unchanged since the last build that ended `done` answer `up_to_date` at
  once; else the module queues one build, to start at once.
- `GET /internal/v1/builds/{build_id}`: the state of that build.

Both answer `{"build_id", "state", "started_at", "finished_at"}`. The
answer is checked strictly (`_build_state`): anything else is a `shape`
failure.

**The wait** (`wait_before_graph`), from the contract:

- the module not configured (`STATION_MODULE_URL` or `STATION_MODULE_TOKEN`
  empty): no call, no wait, one INFO line. The module is optional;
- the `POST`: 5 s per call; a transient failure (a 503, the 5 s timeout,
  a connection refused or reset) is tried once more, 10 s later; a second
  one ends the wait (`unreachable`). Any other refusal ends it at once:
  409 `no_inputs`, 429 `build_interval`, 403 `token`, and any other status,
  among them the 404 of a module older than the builds calls;
- `done` or `up_to_date`: build the graph now. `queued` or `running`: one
  `GET` every 10 s, 5 s per call, for 30 minutes at most from the `POST`.
  `done` builds the graph; `failed`, 403 and 404 end the wait early; every
  other failure of a `GET` (a 503, the timeout, a connection refused or
  reset, an answer of the wrong shape) is not an answer: the next poll asks
  again, within the 30 minutes;
- never a second `POST` while it polls.

Whatever ends the wait, the caller **builds the graph all the same**: the
module keeps serving its last committed build. An early end logs one
WARNING `station_build.unavailable reason=<word>` (with `build_id=<n>` when
there is one); the wait itself is never an error.

Rules kept from `app/station_module.py`: headers built from nothing (the
token, `Accept-Encoding: identity`, and the JSON type on the `POST`; no user
id: a service call), `trust_env=False`, a compressed answer or a body over
4 KiB refused unread, and a log that never holds the token, a body or an
exception's text: reason words and the build number only.

**A hard deadline for each whole call**: each call runs on its own event
loop under `asyncio.timeout`, at most 5 s and never past the end of the 30
minutes, so a module trickling its status line, its headers or its body a
byte at a time is cut there (`timeout`, a transient failure). With the
pauses on the same clock, the wait ends at most 30 minutes after the
first `POST`, plus the few milliseconds of the worker's own checks.

**Stopping.** The worker passes `should_stop`, asked after every pause: an
operator's cancel of the rebuild job ends the wait after the current pause
and call (about 15 s at most), and no graph is built. The pauses go through `Clock.sleep`; a worker stopped by
its container (SIGTERM) dies in it like in a build, and the rebuild job is
closed as orphaned at the next start (`worker._mark_orphaned_rebuild_jobs`).
Nothing here catches `BaseException`, so `SystemExit` and
`KeyboardInterrupt` go through.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

import httpx

from . import station_module

log = logging.getLogger(__name__)

BUILDS_PATH = "/internal/v1/builds"

# The contract's numbers (the module's design, 21.18, points 1 and 2).
POLL_SECONDS = 10.0
CALL_SECONDS = 5.0
WAIT_LIMIT_SECONDS = 30 * 60.0
POST_RETRY_SECONDS = 10.0

# An answer is four short fields; anything larger is refused unread.
MAX_BYTES = 4 * 1024
# A build number is one to eighteen digits, not starting with 0 (the module's i3).
BUILD_ID_MAX = 10**18 - 1

POST_STATES = frozenset({"queued", "running", "done", "failed", "up_to_date"})
GET_STATES = POST_STATES - {"up_to_date"}
_WAITING_STATES = frozenset({"queued", "running"})

# The outcome of a wait, as the worker records it on the rebuild job.
OFF = "off"  # the module is not configured: no call
DONE = "done"
UP_TO_DATE = "up_to_date"
UNAVAILABLE = "unavailable"  # the wait ended early or timed out: the graph is built anyway
CANCELLED = "cancelled"  # the operator cancelled the rebuild job during the wait

# The reason words of an early end, besides `status_<n>` for any other status.
FAILED = "failed"
NO_INPUTS = "no_inputs"
BUILD_INTERVAL = "build_interval"
FORBIDDEN = "token"  # a 403: the token refused
UNKNOWN_BUILD = "unknown_build"
UNREACHABLE = "unreachable"
TIMEOUT = "timeout"
SHAPE = "shape"
ERROR = "error"


# The wait as the worker shows it: one line in the rebuild job's log, written
# when the wait starts and when it ends (`log_line`), read back by the admin
# Sessions page (`status_from_log`). The page shows a fixed sentence for the
# word, never the rest of the line.
WAITING = "waiting"
LOG_MARKER = "[viator] station-build="
_MARKER_LINE = re.compile(r"^\[viator\] station-build=([a-z_]+)(?: |$)", re.MULTILINE)
STATUS_TEXTS = {
    WAITING: "Waiting for the station module build",
    DONE: "Station build done",
    UP_TO_DATE: "Station build up to date",
    UNAVAILABLE: "Station module unavailable — graph built on the last station build",
    CANCELLED: "Cancelled while waiting for the station module build",
}


def log_line(word: str, *, build_id: int | None = None, reason: str | None = None) -> str:
    """The line of the rebuild job's log for a step of the wait."""
    line = f"{LOG_MARKER}{word}"
    if build_id is not None:
        line += f" build={build_id}"
    if reason is not None:
        line += f" reason={reason}"
    return f"{line} · {STATUS_TEXTS[word]}\n"


def status_from_log(text: str | None) -> tuple[str, str] | None:
    """The last step of the wait a rebuild job's log records, as (word, sentence),
    or None when the job did not wait (an OTP build, the module not configured,
    or a job from before this version)."""
    if not text:
        return None
    word: str | None = None
    for match in _MARKER_LINE.finditer(text):
        found = match.group(1)
        if found in STATUS_TEXTS:
            word = found
    return None if word is None else (word, STATUS_TEXTS[word])


class Clock(Protocol):
    """The time of the wait: the worker's is `SystemClock`, tests use a fake one."""

    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


@dataclass(frozen=True)
class BuildState:
    """One answer of the module: the build's number and state."""

    build_id: int
    state: str


@dataclass(frozen=True)
class WaitResult:
    """How the wait ended. `outcome` is one of OFF, DONE, UP_TO_DATE,
    UNAVAILABLE, CANCELLED; `reason` the word of an early end (else None);
    `build_id` the build waited for, when the module named one."""

    outcome: str
    reason: str | None = None
    build_id: int | None = None

    @property
    def build_graph(self) -> bool:
        """True unless the operator cancelled the rebuild during the wait."""
        return self.outcome != CANCELLED


class _Transient(Exception):
    """A failure that is not an answer: a 503, the timeout, a lost connection."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _Ended(Exception):
    """A refusal that ends the wait: the reason word."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


_REFUSAL_CODES = {409: NO_INPUTS, 429: BUILD_INTERVAL, 403: FORBIDDEN}


def _iso_or_null(value: Any) -> bool:
    """True for None, or an ISO 8601 date and time with its offset."""
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _build_state(body: bytes, states: frozenset[str]) -> BuildState:
    """The answer, checked: the four fields, each of its type; else `shape`."""
    try:
        payload = json.loads(body)
    except ValueError:
        raise _Transient(SHAPE) from None
    if not isinstance(payload, dict):
        raise _Transient(SHAPE)
    build_id = payload.get("build_id")
    state = payload.get("state")
    if (
        not isinstance(build_id, int)
        or isinstance(build_id, bool)
        or not 1 <= build_id <= BUILD_ID_MAX
        or not isinstance(state, str)
        or state not in states
        or "started_at" not in payload
        or "finished_at" not in payload
        or not _iso_or_null(payload["started_at"])
        or not _iso_or_null(payload["finished_at"])
    ):
        raise _Transient(SHAPE)
    return BuildState(build_id, state)


class _Calls:
    """The two calls, each a whole call under a hard deadline.

    httpx's timeouts bound each read, not the call: a module trickling its
    status line or its headers a byte at a time would outlast them. So each
    call runs on its own event loop under `asyncio.timeout(limit)`, as
    `app/station_module.py` does, and `limit` is at most 5 s and never past
    the end of the 30 minutes (`_Wait._limit`)."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None) -> None:
        self._transport = transport

    def _send(self, method: str, path: str, limit: float) -> tuple[int, bytes]:
        return asyncio.run(self._send_within(method, path, limit))

    async def _send_within(self, method: str, path: str, limit: float) -> tuple[int, bytes]:
        # The token and no compression, as every call of the module (station_module).
        headers = station_module._headers()
        content: bytes | None = None
        if method == "POST":
            headers["Content-Type"] = "application/json"
            content = b"{}"
        url = station_module._url(path)
        try:
            async with (
                asyncio.timeout(limit),
                _client(self._transport) as client,
                client.stream(method, url, content=content, headers=headers) as response,
            ):
                return response.status_code, await _read(response)
        except (TimeoutError, httpx.TimeoutException):
            raise _Transient(TIMEOUT) from None
        except httpx.TransportError:
            raise _Transient("network") from None

    def request(self, limit: float) -> BuildState:
        """`POST /internal/v1/builds`: the build to wait for."""
        status, body = self._send("POST", BUILDS_PATH, limit)
        if status == 200:
            try:
                return _build_state(body, POST_STATES)
            except _Transient:
                raise _Ended(SHAPE) from None
        if status == 503:
            raise _Transient("status_503")
        raise _Ended(_REFUSAL_CODES.get(status, f"status_{status}"))

    def state(self, build_id: int, limit: float) -> BuildState:
        """`GET /internal/v1/builds/{build_id}`: the state of that build."""
        status, body = self._send("GET", f"{BUILDS_PATH}/{build_id}", limit)
        if status == 200:
            found = _build_state(body, GET_STATES)
            if found.build_id != build_id:
                raise _Transient(SHAPE)
            return found
        if status == 403:
            raise _Ended(FORBIDDEN)
        if status == 404:
            raise _Ended(UNKNOWN_BUILD)
        raise _Transient(f"status_{status}")


async def _read(response: httpx.Response) -> bytes:
    """The body, read as it arrives up to 4 KiB; a compressed answer, or one
    that announces a larger body, is refused unread (`shape`, transient)."""
    encoding = response.headers.get("content-encoding", "").strip().lower()
    length = response.headers.get("content-length", "")
    if encoding not in ("", "identity") or (length.isdigit() and int(length) > MAX_BYTES):
        raise _Transient(SHAPE)
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body += chunk
        if len(body) > MAX_BYTES:
            raise _Transient(SHAPE)
    return bytes(body)


def _client(transport: httpx.AsyncBaseTransport | None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(CALL_SECONDS),
        trust_env=False,
        follow_redirects=False,
        transport=transport,
    )


def _ready(found: BuildState) -> WaitResult | None:
    """The end of the wait an answer gives, or None while the build is still to come."""
    if found.state == UP_TO_DATE:
        return WaitResult(UP_TO_DATE, None, found.build_id)
    if found.state == DONE:
        return WaitResult(DONE, None, found.build_id)
    if found.state == FAILED:
        return WaitResult(UNAVAILABLE, FAILED, found.build_id)
    return None


class _Wait:
    def __init__(
        self,
        calls: _Calls,
        clock: Clock,
        should_stop: Callable[[], bool],
        on_waiting: Callable[[int], None],
    ) -> None:
        self.calls = calls
        self.clock = clock
        self.should_stop = should_stop
        self.on_waiting = on_waiting
        self.deadline = clock.monotonic() + WAIT_LIMIT_SECONDS

    def _limit(self) -> float:
        """The deadline of the next call: 5 s, or what is left of the 30 minutes."""
        return max(0.0, min(CALL_SECONDS, self.deadline - self.clock.monotonic()))

    def _request(self) -> BuildState | WaitResult:
        """The `POST`, tried once more 10 s after a transient failure."""
        try:
            return self.calls.request(self._limit())
        except _Transient:
            pass
        self.clock.sleep(POST_RETRY_SECONDS)
        if self.should_stop():
            return WaitResult(CANCELLED)
        try:
            return self.calls.request(self._limit())
        except _Transient:
            return WaitResult(UNAVAILABLE, UNREACHABLE)

    def _poll(self, build_id: int) -> WaitResult:
        """One `GET` every 10 s until the build ends, or the 30 minutes pass."""
        self.on_waiting(build_id)
        while True:
            remaining = self.deadline - self.clock.monotonic()
            if remaining <= 0:
                return WaitResult(UNAVAILABLE, TIMEOUT, build_id)
            self.clock.sleep(min(POLL_SECONDS, remaining))
            if self.should_stop():
                return WaitResult(CANCELLED, None, build_id)
            if self.clock.monotonic() >= self.deadline:
                return WaitResult(UNAVAILABLE, TIMEOUT, build_id)
            try:
                found = self.calls.state(build_id, self._limit())
            except _Transient as transient:
                log.debug("station_build.poll_retry reason=%s", transient.reason)
                continue
            except _Ended as ended:
                return WaitResult(UNAVAILABLE, ended.reason, build_id)
            ready = _ready(found)
            if ready is not None:
                return ready

    def run(self) -> WaitResult:
        try:
            answer = self._request()
        except _Ended as ended:
            return WaitResult(UNAVAILABLE, ended.reason)
        if isinstance(answer, WaitResult):
            return answer
        ready = _ready(answer)
        if ready is not None:
            return ready
        return self._poll(answer.build_id)


def _log_result(result: WaitResult) -> None:
    if result.outcome == UNAVAILABLE:
        if result.build_id is None:
            log.warning("station_build.unavailable reason=%s", result.reason)
        else:
            log.warning(
                "station_build.unavailable reason=%s build_id=%d", result.reason, result.build_id
            )
    elif result.outcome == CANCELLED:
        log.info("station_build.cancelled")
    elif result.outcome == OFF:
        log.info("station_build.skipped reason=off")
    else:
        log.info("station_build.ready state=%s build_id=%s", result.outcome, result.build_id)


def wait_before_graph(
    *,
    should_stop: Callable[[], bool],
    on_waiting: Callable[[int], None],
    clock: Clock | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> WaitResult:
    """Ask the module for a station build and wait until it has ended.

    `should_stop` is asked after every pause (the operator's cancel);
    `on_waiting(build_id)` is called once when the build is still queued or
    running, so that the worker shows the wait. Never raises an `Exception`:
    an unexpected one ends the wait (`error`, one WARNING) and the graph is
    built."""
    clock = clock or SystemClock()
    try:
        if not station_module.enabled():
            result = WaitResult(OFF)
        else:
            result = _Wait(_Calls(transport), clock, should_stop, on_waiting).run()
    except Exception as exc:
        # One line, never the exception's text: one about a header can quote the token.
        log.warning("station_build.unavailable reason=%s type=%s", ERROR, type(exc).__name__)
        return WaitResult(UNAVAILABLE, ERROR)
    _log_result(result)
    return result
