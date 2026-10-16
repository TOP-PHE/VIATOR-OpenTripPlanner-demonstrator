"""Send the stop extracts of a session's feeds to the station module (MSMM).

The module builds its station referential from the timetables of every
VIATOR session (its design, section 22; D-R1 A, D-R15 A). VIATOR makes one
small rail-only, station-level extract per feed version
(`app/station_extract.py`, pull request 4) and sends it with two internal
calls of the module (its design 22.3 and 18.41, entries 408-424):

- `HEAD /internal/v1/feeds/{feed_key}/{sha256}?extractor_version=<v>`: 200
  when the module holds a version of that feed made from that archive by
  that extractor, 404 when not; no body. `sha256` is the feed version, the
  sha256 of the downloaded archive, known before any extraction.
- `PUT /internal/v1/feeds/{feed_key}/extract`: the gzip extract,
  `Content-Type: application/gzip`, at most 32 MiB, streamed from its file
  with its `Content-Length` and no `Transfer-Encoding`. 200
  `{"status": "recorded" | "unchanged", "sha256", "bytes", "rows"}`.

Both carry `X-Viator-Session-Id: <the VIATOR session id>` (opaque, 1 to 64
of `A-Z a-z 0-9 . _ -`; a session id of another form is not sent). Both are
service calls: the token, no user id, counted in no person's window. **The
intake never queues a build** (D-R20): the `POST /internal/v1/builds` that
follows for a MOTIS session (`app/station_builds.py`) queues it at once.

**When** (`send_session_extracts`):

- at the end of every refresh of a session's sources, whatever its engine
  (the refresh endpoints of `app/api/admin/sessions.py` queue it for the
  process's sender thread, `after_refresh`: `REFRESH_BUDGET_SECONDS`);
- in the worker, at the start of every rebuild job, OTP or MOTIS; for a
  MOTIS session **before** the station build request and its wait, so that
  the build sees the new extracts (`BUILD_BUDGET_SECONDS`). The worker uses
  only extracts already made (`cache_only`): no extraction starts before a
  graph build, so none can run into it; a feed without one is "skipped
  not_cached" and goes with the refresh's pass.

Per feed with a feed key: `HEAD`; on 200 nothing more ("already held"); on
404 `station_extract.ensure_extract` (its cache by archive sha256 and
`EXTRACTOR_VERSION`, else, after a refresh, a resource-limited child
process), then the `PUT`.

**Order** (`_send_order`): the feeds whose outcome needs no extraction go
first (no key, no archive, an extract already in the cache or a failure the
extractor remembers), then the others from the smallest timetable file to
the largest. A national archive whose extraction takes many minutes thus
never keeps the small feeds of the same pass from being sent: they are sent
before it, and when the budget ends while it is being made, it alone is
"skipped budget"; its extraction goes on (the child process is not stopped,
and the extractor's warm thread works through the remaining feeds, smallest
first, with no budget), so its extract is in the cache and sent at the next
pass, a refresh's or the worker's. The outcomes are still recorded and
returned in the session's order.

**Bounded.** Each call runs under `asyncio.timeout` on its own event loop
(the per-call deadline of `app/station_builds.py`): `HEAD_SECONDS` for a
`HEAD`, `PUT_SECONDS` for a `PUT`, never past the end of the pass's budget.
The extraction (`_Pass._extract_within`) is waited for at most until the
budget ends too. The pass stops starting new work once its budget is spent,
and starts no `PUT` with less than `MIN_PUT_SECONDS` left; the feeds left
are "skipped" with the reason `budget`. Once a call got no answer after its
retries (`network`, `timeout`), or a `PUT` was answered 404 (the intake off,
or a module older than it), the module is taken for down: the feeds left are
"module unavailable" with the same reason and are not called. Every pause
lasts at least `PAUSE_FLOOR_SECONDS`, and the end of a call never waits for a name lookup
past its deadline. Retries:

- 503 `busy` (another extract being received) and 429 `extract_window` (60
  extracts in the UTC half hour): wait the `Retry-After`, then send again,
  only when the wait fits in what is left of the budget (and is at most
  `RETRY_AFTER_MAX_SECONDS`); else the feed ends "module unavailable" with
  the code as its reason; at most `WAITS_PER_CALL` waits for one call. A
  429 that cannot be waited ends every `PUT` of the pass (the window is the
  module's, for every feed);
- 408 `timeout`, any other 5xx, a call past its deadline, a lost
  connection: tried again `TRANSIENT_RETRIES` times, `TRANSIENT_PAUSE_SECONDS`
  apart, within the budget;
- every other 4xx is a refusal: never sent again in the pass, logged with
  its reason word only ("refused <code>"; a `HEAD` 403, which has no body,
  is the token: "refused token");
- an answer too large or compressed (`shape`) is never sent again; a 3xx,
  or a 2xx other than 200, is "module unavailable" (an address or a proxy
  in the way).

**Never blocking.** Nothing here raises an `Exception` to its caller: an
unexpected one ends the pass (`error`, one WARNING). A failed upload never
keeps a graph from being built. Without `STATION_MODULE_URL` or
`STATION_MODULE_TOKEN`, no call: one INFO line.

**What the admin sees.** The last outcome of each provider is written in the
session's fetch-state folder (`inbox/<sid>/_fetch_state/`, the store of the
feed-status panel), under its own key `provider[<id>].station_extract`, never
in a fetch's own record, so a refresh writing the fetch state at the same
moment loses nothing. The feed-status panel of the Sessions page shows it as
a short text (`STATUS_TEXTS`), the reason word beside it, and under them a
sentence saying what the reason means (`REASON_TEXTS`: fixed texts written
here, never an exception's text, a path, an address or a value of the
extract). No migration.

**Logs.** Reason words, counts and the provider id only (VIATOR's own
configuration): never the token, the session id, a feed key, a body, a value
of the extract or an exception's text (one about a header can quote the
token). Rules kept from `app/station_module.py`: headers built from nothing,
`trust_env=False`, `Accept-Encoding: identity`, an answer over 4 KiB or
compressed refused unread.

**Metrics.** `viator_station_extract_feeds_total{outcome}` counts each feed
of a pass (`app/metrics.py`); only `web`'s passes are scraped.

**Stopping.** `should_stop` is asked before each feed and after each pause:
an operator's cancel of the rebuild job ends the pass (`cancelled`). Nothing
here catches `BaseException`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import queue
import re
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable, Coroutine, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import httpx

from . import feed_fetch, ingestion, station_extract, station_module
from .metrics import STATION_EXTRACT_FEEDS_TOTAL
from .settings import settings

log = logging.getLogger(__name__)

FEEDS_PATH = "/internal/v1/feeds"
SESSION_HEADER = "X-Viator-Session-Id"
CONTENT_TYPE = "application/gzip"

# The module's own rules (its design 22.3; app/master/feed_intake.py).
SESSION_ID = re.compile(r"[A-Za-z0-9._-]{1,64}", re.ASCII)
SHA256 = re.compile(r"[0-9a-f]{64}", re.ASCII)
EXTRACT_MAX_BYTES = 32 * 1024 * 1024

# Deadlines and budgets (seconds).
HEAD_SECONDS = 10.0
# 32 MiB at 300 KB/s; the module itself cuts a body after 600 s in all.
PUT_SECONDS = 120.0
# The worker, before a graph build: what an upload may add to a MOTIS
# session's station build and graph. Nothing waits on a refresh's pass.
BUILD_BUDGET_SECONDS = 5 * 60.0
REFRESH_BUDGET_SECONDS = 30 * 60.0
RETRY_AFTER_MAX_SECONDS = 120.0
TRANSIENT_RETRIES = 2
# A 503 `busy` or 429 without a usable `Retry-After` (the module always sends one).
DEFAULT_RETRY_SECONDS = 10.0
TRANSIENT_PAUSE_SECONDS = 5.0
# How often the pass looks at the cancel and the budget while an extraction runs.
EXTRACT_POLL_SECONDS = 1.0
# Every pause lasts at least this long: a `Retry-After: 0` never makes a tight loop.
PAUSE_FLOOR_SECONDS = 1.0
# At most this many busy or 429 waits for one call, whatever their length.
WAITS_PER_CALL = 6
# No upload starts with less of the budget left (an extract of 32 MiB needs time).
MIN_PUT_SECONDS = 30.0

# An answer is a short JSON object, or nothing; anything larger is refused unread.
MAX_ANSWER_BYTES = 4 * 1024
CHUNK_BYTES = 64 * 1024

# The outcome of one feed.
SENT = "sent"  # PUT answered `recorded`
HELD = "held"  # HEAD 200, or PUT answered `unchanged`
REFUSED = "refused"  # a 4xx refusal: its code is the reason
SKIPPED = "skipped"  # nothing sent: no key, no archive, no extract, the budget, off
UNAVAILABLE = "unavailable"  # the module did not take it: network, timeout, 5xx, 404, shape
CANCELLED = "cancelled"

STATUS_TEXTS = {
    SENT: "sent",
    HELD: "already held",
    REFUSED: "refused",
    SKIPPED: "skipped",
    UNAVAILABLE: "module unavailable",
    CANCELLED: "cancelled",
}

# Reason words of this side.
OFF = "off"
NO_FEED_KEY = "no_feed_key"
NO_ARCHIVE = "no_archive"
NO_EXTRACT = "no_extract"
BUDGET = "budget"
TIMEOUT = "timeout"
NETWORK = "network"
SHAPE = "shape"
ERROR = "error"
BUSY = "busy"
EXTRACT_WINDOW = "extract_window"
ARCHIVE_CHANGED = "archive_changed"
EXTRACT_GONE = "extract_gone"
NO_SNAPSHOT = "no_snapshot"
NOT_CACHED = "not_cached"
FORBIDDEN_WORD = "token"  # the reason word of a 403: the token refused
# Failures that are never sent again in a pass: the same file, the same answer.
_FINAL_FAILURES = frozenset({EXTRACT_GONE, SHAPE})

# The refusal words of the module's 4xx answers (its design 20.6, 22.3), kept
# when an answer names one; any other code gives `status_<n>`.
REFUSAL_CODES = frozenset(
    {
        "token",
        "browser",
        "size",
        "session",
        "source_taken",
        "disconnected",
        "not_gzip",
        "not_utf8",
        "not_csv",
        "too_large",
        "format_version",
        "header",
        "empty",
        "row",
        "feed_key",
        "same_on_every_row",
        "duplicate_stop",
        "invalid",
    }
)
# The extraction's limits, as the texts below give them.
_GIB_OF_MEMORY = station_extract.CHILD_MEMORY_BYTES // (1024 * 1024 * 1024)
_CPU_MINUTES = station_extract.CHILD_CPU_SECONDS // 60
_WALL_MINUTES = station_extract.CHILD_WALL_SECONDS // 60
# What a reason word means, in a few words, for the feed-status panel. Fixed
# texts: a reason word has none when it explains itself (`status_<n>`, a
# refusal word of the module).
REASON_TEXTS = {
    NO_FEED_KEY: "this feed is not sent to the station module",
    NO_ARCHIVE: "no timetable file yet",
    NOT_CACHED: "waiting for its extract; sent at the next pass",
    BUDGET: "the pass ran out of time before it; sent at the next pass",
    BUSY: "the station module or the extractor was busy; sent at the next pass",
    "in_flight": "its extract is being made; sent at the next pass",
    NO_SNAPSHOT: "the timetable file changed during the pass; sent at the next pass",
    ARCHIVE_CHANGED: "the timetable file changed during the pass; sent at the next pass",
    EXTRACT_GONE: "the extract was replaced during the upload; sent at the next pass",
    NO_EXTRACT: "no usable extract was made",
    EXTRACT_WINDOW: "the station module's half-hour window is full; sent later",
    TIMEOUT: "the station module did not answer in time",
    NETWORK: "the station module could not be reached",
    SHAPE: "the station module's answer was not understood",
    ERROR: "an unexpected error; see the web log",
    "input_too_large": "the archive passes a reading bound: size, record length, nesting or counts",
    station_extract.MEMORY_LIMIT: (
        f"the extraction passed its memory limit ({_GIB_OF_MEMORY} GiB); "
        "retried with a new file or extractor"
    ),
    station_extract.CPU_LIMIT: (
        f"the extraction passed its processor-time limit ({_CPU_MINUTES} minutes); "
        "retried with a new file or extractor"
    ),
    station_extract.WALL_CLOCK_LIMIT: (
        f"the extraction passed its wall-clock limit ({_WALL_MINUTES} minutes) on a busy "
        "server; retried once"
    ),
    station_extract.STOPPED: (
        "the extraction was killed from outside (out-of-memory killer, or by hand)"
    ),
    station_extract.CRASHED: "the extraction crashed (a signal, in the log); retried once",
    # The one word of earlier releases for the four above, still in their
    # recorded outcomes until the next pass.
    "resource_limit": "the extraction passed its memory or time limit, or was stopped",
    "bad_archive": "the archive cannot be read as GTFS or NeTEx",
    "unsupported_format": "not a GTFS or NeTEx timetable",
    "no_stop_place": "the NeTEx archive places no rail stop at a StopPlace",
    "no_rail_stop": "no rail station in this feed",
    "too_large": "the extract would pass the station module's size limit",
    "too_many_rows": "the extract would pass the station module's row limit",
    "line_too_long": "the extract would pass the station module's line limit",
    "cell_too_long": "the extract would pass the station module's cell limit",
    "cache": "the extract folder cannot be written",
    "token": "the station module refused VIATOR's token",
}
PUT_STATUSES = frozenset({"recorded", "unchanged"})
STATE_KEY = "provider[{pid}].station_extract"
# Log lines written from more than one place.
_SKIPPED_OFF = "station_extract.skipped reason=off"
_FAILED = "station_extract.failed reason=%s type=%s"


class Clock(Protocol):
    """The time of a pass: the system's in production, a fake one in tests."""

    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


@dataclass(frozen=True)
class Feed:
    """One provider's timetable as the pass sees it. `feed_key` None: the
    provider gets no key (a `cross_border_filter`, or a configuration the
    extractor cannot name); `archive` None: no file in the slot yet."""

    provider_id: str
    provider_cfg: dict[str, Any]
    feed_key: str | None
    archive: Path | None
    archive_sha256: str | None


@dataclass(frozen=True)
class FeedOutcome:
    provider_id: str
    state: str
    reason: str | None = None

    @property
    def text(self) -> str:
        """The short state the admin sees: "refused row", "skipped", ..."""
        base = STATUS_TEXTS[self.state]
        return f"{base} {self.reason}" if self.state == REFUSED and self.reason else base


@dataclass
class PassResult:
    """The outcome of a pass: one per feed, in the session's order. `off`:
    the module is not configured and nothing was called."""

    outcomes: list[FeedOutcome] = field(default_factory=list)
    off: bool = False
    cancelled: bool = False

    def counts(self) -> dict[str, int]:
        found: dict[str, int] = {}
        for outcome in self.outcomes:
            found[outcome.state] = found.get(outcome.state, 0) + 1
        return found


class PassObserver(Protocol):
    """Who follows a pass as it goes (the refresh progress record of
    `app/refresh_progress.py`, `ExtractFollowUp`). Its methods never raise."""

    def started(self, total: int, /) -> None: ...

    def feed_started(self, provider_id: str, /) -> None: ...

    def outcome(self, outcome: FeedOutcome, /) -> None: ...

    def finished(self, end: str, /) -> None: ...


class _Unobserved:
    """A pass nobody follows: every call is ignored."""

    def started(self, *_: object) -> None:
        """Nothing to tell."""

    def feed_started(self, *_: object) -> None:
        """Nothing to tell."""

    def outcome(self, *_: object) -> None:
        """Nothing to tell."""

    def finished(self, *_: object) -> None:
        """Nothing to tell."""


_UNOBSERVED = _Unobserved()


# ── one call ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Answer:
    status: int
    body: bytes
    retry_after: float | None


class _Failure(Exception):
    """A call that got no answer: past its deadline, or the connection lost."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after", "").strip()
    if not value.isascii() or not value.isdigit() or len(value) > 6:
        return None
    return float(int(value))


async def _read(response: httpx.Response) -> bytes:
    encoding = response.headers.get("content-encoding", "").strip().lower()
    length = response.headers.get("content-length", "")
    if encoding not in ("", "identity") or (length.isdigit() and int(length) > MAX_ANSWER_BYTES):
        raise _Failure(SHAPE)
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body += chunk
        if len(body) > MAX_ANSWER_BYTES:
            raise _Failure(SHAPE)
    return bytes(body)


async def _chunks(path: Path) -> AsyncIterator[bytes]:
    """The file, a piece at a time, each read off the event loop: an extract
    is never held whole in memory."""
    handle = await asyncio.to_thread(path.open, "rb")
    try:
        while piece := await asyncio.to_thread(handle.read, CHUNK_BYTES):
            yield piece
    finally:
        await asyncio.to_thread(handle.close)


def _client(transport: httpx.AsyncBaseTransport | None, limit: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(limit),
        trust_env=False,
        follow_redirects=False,
        transport=transport,
    )


class _Calls:
    """The two calls, each a whole call under a hard deadline on its own loop."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None, session_id: str | None) -> None:
        self._transport = transport
        self._session_id = session_id

    def _headers(self) -> dict[str, str]:
        headers = station_module._headers()
        if self._session_id is not None:
            headers[SESSION_HEADER] = self._session_id
        return headers

    def head(self, feed_key: str, sha256: str, extractor_version: str, limit: float) -> _Answer:
        url = station_module._url(f"{FEEDS_PATH}/{feed_key}/{sha256}")
        params = {"extractor_version": extractor_version}
        return _run(self._send("HEAD", url, limit, params=params))

    def put(self, feed_key: str, path: Path, size: int, limit: float) -> _Answer:
        url = station_module._url(f"{FEEDS_PATH}/{feed_key}/extract")
        headers = {"Content-Type": CONTENT_TYPE, "Content-Length": str(size)}
        return _run(self._send("PUT", url, limit, path=path, extra=headers))

    async def _send(
        self,
        method: str,
        url: str,
        limit: float,
        *,
        params: dict[str, str] | None = None,
        path: Path | None = None,
        extra: dict[str, str] | None = None,
    ) -> _Answer:
        headers = {**self._headers(), **(extra or {})}
        content = _chunks(path) if path is not None else None
        try:
            async with (
                asyncio.timeout(limit),
                _client(self._transport, limit) as client,
                client.stream(
                    method, url, params=params, content=content, headers=headers
                ) as response,
            ):
                body = b"" if method == "HEAD" else await _read(response)
                return _Answer(response.status_code, body, _retry_after(response))
        except (TimeoutError, httpx.TimeoutException):
            raise _Failure(TIMEOUT) from None
        except httpx.TransportError:
            raise _Failure(NETWORK) from None
        except OSError:
            # The extract file went away (the extractor keeps one per provider).
            raise _Failure(EXTRACT_GONE) from None


def _run(call: Coroutine[Any, Any, _Answer]) -> _Answer:
    """`asyncio.run`, without its wait for the loop's default executor: a
    name lookup still running there past the call's deadline (`getaddrinfo`
    cannot be cut short) is left to finish alone on its thread instead of
    holding the pass (`loop.close` shuts the executor down without waiting)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(call)
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            loop.close()


def _waits(answer: _Answer) -> bool:
    """503 `busy` and 429: the module says when to send again."""
    return answer.status == 429 or (answer.status == 503 and _code(answer) == BUSY)


def _retry_wait(answer: _Answer) -> float:
    return answer.retry_after if answer.retry_after is not None else DEFAULT_RETRY_SECONDS


def _transient(answer: _Answer) -> bool:
    """408 and any other 5xx: tried again a bounded number of times."""
    return answer.status == 408 or answer.status >= 500


def _code(answer: _Answer) -> str | None:
    """The `code` word of a JSON refusal, or None."""
    try:
        payload = json.loads(answer.body)
    except ValueError:
        return None
    code = payload.get("code") if isinstance(payload, dict) else None
    return code if isinstance(code, str) else None


@dataclass(frozen=True)
class Uploaded:
    status: str  # "recorded" | "unchanged"


def _put_answer(answer: _Answer, sha256: str, size: int, rows: int) -> Uploaded:
    """The 200 of a `PUT`, checked strictly: its own body's sha256, size and
    rows given back, a known status; anything else is `shape`."""
    try:
        payload = json.loads(answer.body)
    except ValueError:
        raise _Failure(SHAPE) from None
    if not isinstance(payload, dict):
        raise _Failure(SHAPE)
    status = payload.get("status")
    if (
        status not in PUT_STATUSES
        or payload.get("sha256") != sha256
        or not _whole(payload.get("bytes"), size)
        or not _whole(payload.get("rows"), rows)
    ):
        raise _Failure(SHAPE)
    return Uploaded(str(status))


def _whole(value: Any, expected: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value == expected


# ── the pass ─────────────────────────────────────────────────────────────


class _Stop(Exception):
    """The operator cancelled the rebuild job."""


class _Pass:
    def __init__(
        self,
        calls: _Calls,
        clock: Clock,
        budget: float,
        should_stop: Callable[[], bool],
        extract: Callable[..., Any],
        extractor_version: str,
        sid: str = "",
        cache_only: bool = False,
    ) -> None:
        self.sid = sid
        self.cache_only = cache_only
        self.calls = calls
        self.clock = clock
        self.deadline = clock.monotonic() + budget
        self.should_stop = should_stop
        self.extract = extract
        self.extractor_version = extractor_version
        self.window_full = False
        # Set once the module is found down (no answer after the retries, or a
        # PUT answered 404): the feeds left are not called.
        self.down: str | None = None

    def left(self) -> float:
        return self.deadline - self.clock.monotonic()

    def limit(self, call_seconds: float) -> float:
        return max(0.0, min(call_seconds, self.left()))

    def pause(self, seconds: float) -> bool:
        """Sleep `seconds`, at least `PAUSE_FLOOR_SECONDS`, when they fit in the
        budget; False when they do not."""
        seconds = max(PAUSE_FLOOR_SECONDS, seconds)
        if seconds > RETRY_AFTER_MAX_SECONDS or seconds >= self.left():
            return False
        self.clock.sleep(seconds)
        if self.should_stop():
            raise _Stop
        return True

    def _call(self, send: Callable[[float], _Answer], call_seconds: float) -> _Answer:
        """One call and its bounded retries: 408, any 5xx but `busy`, a call
        past its deadline or a lost connection, `TRANSIENT_RETRIES` more
        times; 503 `busy` and 429 after their `Retry-After`, within the
        budget. Returns the last answer; raises `_Failure` when none came."""
        retries = _Retries(self)
        while True:
            if self.left() <= 0:
                raise _Failure(BUDGET)
            try:
                answer = send(self.limit(call_seconds))
            except _Failure as failure:
                if failure.reason not in _FINAL_FAILURES and retries.transient():
                    continue
                raise
            if retries.again(answer):
                continue
            return answer

    def feed(self, feed: Feed) -> FeedOutcome:
        pid = feed.provider_id
        if feed.feed_key is None:
            return FeedOutcome(pid, SKIPPED, NO_FEED_KEY)
        if feed.archive is None or feed.archive_sha256 is None:
            return FeedOutcome(pid, SKIPPED, NO_ARCHIVE)
        if self.down is not None:
            return FeedOutcome(pid, UNAVAILABLE, self.down)
        key, sha = feed.feed_key, feed.archive_sha256
        try:
            answer = self._call(
                lambda limit: self.calls.head(key, sha, self.extractor_version, limit),
                HEAD_SECONDS,
            )
        except _Failure as failure:
            return self._unreached(pid, failure.reason)
        if answer.status == 200:
            return FeedOutcome(pid, HELD)
        if answer.status == 403:
            # A HEAD answer has no body (the server drops it), and the token is
            # the one thing VIATOR's HEAD can be refused for (the module's i4;
            # i5 refuses browser headers this client never sends).
            return FeedOutcome(pid, REFUSED, FORBIDDEN_WORD)
        if answer.status != 404:
            return _refused(pid, answer)
        return self._send(feed, key)

    def _unreached(self, pid: str, reason: str) -> FeedOutcome:
        """A call that got no answer: once no answer came after the retries,
        the module is taken for down and the feeds left are not called."""
        if reason in (NETWORK, TIMEOUT):
            self.down = reason
        return _unreached(pid, reason)

    def _extract_within(self, feed: Feed) -> Any:
        """The extract, or a reason word, within what is left of the budget.

        The extractor (`station_extract.ensure_extract`) works in a limited
        child process that this side cannot cut short; so it runs on a
        thread of its own, and the pass waits for it at most until the
        budget ends (`budget`: the child goes on, one extraction at a time
        on the machine, and its extract is in the cache for the next pass),
        asking for the operator's cancel every `EXTRACT_POLL_SECONDS`."""
        box: dict[str, Any] = {}

        def run() -> None:
            try:
                box["made"] = self.extract(self.sid, feed, max(0.0, self.left()), self.cache_only)
            except Exception as exc:  # one feed, never the pass or the thread
                log.warning(_FAILED, ERROR, type(exc).__name__)
                box["made"] = ERROR

        thread = threading.Thread(target=run, name="station-extract-wait", daemon=True)
        thread.start()
        while True:
            thread.join(EXTRACT_POLL_SECONDS)
            if not thread.is_alive():
                return box.get("made", ERROR)
            if self.should_stop():
                raise _Stop
            if self.left() <= 0:
                return BUDGET

    def _send(self, feed: Feed, key: str) -> FeedOutcome:
        pid = feed.provider_id
        if self.window_full:
            return FeedOutcome(pid, UNAVAILABLE, EXTRACT_WINDOW)
        if self.left() < MIN_PUT_SECONDS:
            return FeedOutcome(pid, SKIPPED, BUDGET)
        made = self._extract_within(feed)
        if isinstance(made, str):
            return FeedOutcome(pid, SKIPPED, made)
        path = Path(made.path_gz)
        size = int(made.bytes)
        if (
            made.feed_key != key
            or made.feed_version != feed.archive_sha256
            or not 0 < size <= EXTRACT_MAX_BYTES
            or _size_on_disk(path) != size
        ):
            return FeedOutcome(pid, SKIPPED, NO_EXTRACT)
        if self.left() < MIN_PUT_SECONDS:
            return FeedOutcome(pid, SKIPPED, BUDGET)
        try:
            answer = self._call(lambda limit: self.calls.put(key, path, size, limit), PUT_SECONDS)
            if answer.status == 200:
                uploaded = _put_answer(answer, made.body_sha256, size, int(made.rows))
                return FeedOutcome(pid, SENT if uploaded.status == "recorded" else HELD)
        except _Failure as failure:
            return self._unreached(pid, failure.reason)
        if answer.status == 429:
            self.window_full = True
        if answer.status == 404:
            # The intake calls are off, or the module is older than them.
            self.down = "status_404"
        return _refused(pid, answer)


def _size_on_disk(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


class _Retries:
    """What is left of one call's retries, and whether to try again."""

    def __init__(self, work: _Pass) -> None:
        self.work = work
        self.left = TRANSIENT_RETRIES
        self.waits = WAITS_PER_CALL

    def transient(self) -> bool:
        """After a transient failure: True once the pause is over, while
        retries are left and the pause fits in the budget."""
        if self.left > 0 and self.work.pause(TRANSIENT_PAUSE_SECONDS):
            self.left -= 1
            return True
        return False

    def again(self, answer: _Answer) -> bool:
        """After an answer: busy and 429 after their `Retry-After`, 408 and
        5xx as a transient failure; every other answer is final."""
        if _waits(answer):
            if self.waits <= 0:
                return False
            self.waits -= 1
            return self.work.pause(_retry_wait(answer))
        return _transient(answer) and self.transient()


def _unreached(pid: str, reason: str) -> FeedOutcome:
    if reason in (BUDGET, EXTRACT_GONE):
        return FeedOutcome(pid, SKIPPED, reason)
    return FeedOutcome(pid, UNAVAILABLE, reason)


def _refused(pid: str, answer: _Answer) -> FeedOutcome:
    """A final answer other than a success: a 4xx refusal with its word, else
    the module unavailable (404 of a module without these calls, 5xx, the
    busy and window answers that could not be waited, a 3xx or a 2xx other
    than 200)."""
    code = _code(answer)
    status = answer.status
    if status == 503 and code == BUSY:
        return FeedOutcome(pid, UNAVAILABLE, BUSY)
    if status == 429:
        return FeedOutcome(pid, UNAVAILABLE, EXTRACT_WINDOW)
    if status < 400 or status == 404 or status == 408 or status >= 500:
        # Also a redirect or another 2xx: an address or a proxy in the way.
        return FeedOutcome(pid, UNAVAILABLE, f"status_{status}")
    if code in REFUSAL_CODES:
        return FeedOutcome(pid, REFUSED, code)
    return FeedOutcome(pid, REFUSED, f"status_{status}")


# ── the feeds of a session ───────────────────────────────────────────────


# A VIATOR session id as `create_session` admits it, and a provider id as
# `ingestion` admits it: checked again wherever one names a path.
SESSION_SLUG = re.compile(r"[a-z][a-z0-9-]{1,62}", re.ASCII)
PROVIDER_ID = re.compile(r"[A-Z][A-Z0-9_-]{1,15}", re.ASCII)


def _inside(base: str, *names: str) -> str:
    """`base` joined with `names`, made real (`os.path.realpath`) and refused
    with `ValueError` unless it starts with the real base and a separator:
    the one guard of every path this module builds from a session or a
    provider id (the form CodeQL's py/path-injection and Sonar's S2083
    recognise). The checked string is what the caller opens."""
    root = os.path.realpath(base)
    candidate = os.path.realpath(Path(root, *names))
    if not candidate.startswith(root + os.sep):
        raise ValueError("path outside its base")
    return candidate


def _session_dir(sid: str) -> Path:
    """`<inbox>/<sid>` for a session id of the slug form only, checked to sit
    inside the inbox: `..`, an absolute path, a NUL or a link out of the
    inbox is a `ValueError`, whatever the callers checked before."""
    if SESSION_SLUG.fullmatch(sid) is None:
        raise ValueError("not a session id")
    return Path(_inside(str(settings.inbox_dir), sid))


def _fetch_state_dir(sid: str) -> Path:
    if SESSION_SLUG.fullmatch(sid) is None:
        raise ValueError("not a session id")
    return Path(_inside(str(settings.inbox_dir), sid, "_fetch_state"))


def _outcome_name(provider_id: str) -> str:
    """The file name of a provider's last outcome: `feed_fetch.state_path`'s
    name for the key `provider[<id>].station_extract`."""
    return f"provider_{provider_id}_.station_extract.json"


def _outcome_path(sid: str, provider_id: str) -> str:
    """The checked path of a provider's last outcome; `ValueError` for a
    session or provider id of another form, or a path out of the inbox."""
    if SESSION_SLUG.fullmatch(sid) is None or PROVIDER_ID.fullmatch(provider_id) is None:
        raise ValueError("not a session or provider id")
    return _inside(str(settings.inbox_dir), sid, "_fetch_state", _outcome_name(provider_id))


def _archive(sid: str, provider: dict[str, Any]) -> Path | None:
    """The timetable file in the provider's slot, or None."""
    fmt = (provider.get("timetable") or {}).get("format", "gtfs")
    details = ingestion.TIMETABLE_FORMAT_DETAILS.get(fmt)
    if details is None:
        return None
    pid = str(provider.get("id"))
    if SESSION_SLUG.fullmatch(sid) is None or PROVIDER_ID.fullmatch(pid) is None:
        return None
    name = ingestion.staged_filename_for_format(pid, fmt)
    path = _inside(str(settings.inbox_dir), sid, details["subdir"], name)
    return Path(path) if Path(path).is_file() else None


def _fetched_sha256(sid: str, label: str) -> str | None:
    """The sha256 of the last download of a refresh task (its fetch state)."""
    raw = feed_fetch.load_state(_fetch_state_dir(sid), label).get("sha256")
    return raw if isinstance(raw, str) else None


def session_feeds(sid: str, config: dict[str, Any], upload_sha256: dict[str, str]) -> list[Feed]:
    """The session's providers as feeds. The archive sha256 is the sha256 of
    the bytes VIATOR downloaded, kept in the fetch state (`sha256`), for a
    `url` or `nap` provider; the sha256 of the uploaded file (its `Upload`
    row, `upload_sha256` by provider id) for an `upload` one, which is also
    its feed key. A provider whose slot is empty, or whose file came in
    another way than its source (no fetch state), has no archive."""
    if SESSION_SLUG.fullmatch(sid) is None:
        return []
    try:
        providers = ingestion.normalize_providers(config)
    except ValueError:
        return []
    feeds: list[Feed] = []
    for provider in providers:
        pid = provider["id"]
        timetable = provider.get("timetable") or {}
        fmt = timetable.get("format", "gtfs")
        source = timetable.get("source") or ("url" if timetable.get("url") else "upload")
        archive = _archive(sid, provider)
        if source == "upload":
            sha = upload_sha256.get(pid)
        else:
            sha = _fetched_sha256(sid, f"provider[{pid}].timetable({fmt})")
        if sha is not None and SHA256.fullmatch(sha) is None:
            sha = None
        key = station_extract.feed_key_for(
            provider, upload_sha256=sha if source == "upload" else None
        )
        feeds.append(Feed(pid, provider, key, archive, sha))
    return feeds


def latest_upload_sha256(db: Any, sid: str) -> dict[str, str]:
    """The sha256 of the latest file uploaded for each provider of a session."""
    from sqlalchemy import desc, select

    from .models import Upload

    found: dict[str, str] = {}
    rows = db.execute(
        select(Upload.provider_feed_id, Upload.sha256)
        .where(Upload.session_id == sid, Upload.provider_feed_id.is_not(None))
        .order_by(desc(Upload.created_at))
    ).all()
    for pid, sha in rows:
        if pid is not None:
            found.setdefault(pid, sha)
    return found


# ── the record the admin sees ────────────────────────────────────────────


def record_outcome(sid: str, outcome: FeedOutcome, *, at: datetime | None = None) -> None:
    """Keep a provider's last outcome beside its fetch state. Best-effort."""
    state = {
        "at": (at or datetime.now(UTC)).isoformat(timespec="seconds"),
        "state": outcome.state,
        "reason": outcome.reason,
    }
    try:
        path = _outcome_path(sid, outcome.provider_id)
    except ValueError:
        log.warning("station_extract.not_recorded reason=bad_id")
        return
    feed_fetch.save_state(Path(path).parent, STATE_KEY.format(pid=outcome.provider_id), state)


def load_outcome(sid: str, provider_id: str) -> dict[str, Any] | None:
    """A provider's last outcome as the Sessions page shows it, or None:
    `{"at", "state", "reason", "text", "detail"}`, the text one of
    `STATUS_TEXTS` (with the refusal's word), the detail the reason's
    sentence of `REASON_TEXTS` or None."""
    try:
        path = _outcome_path(sid, provider_id)
    except ValueError:
        return None
    state = feed_fetch.load_state(Path(path).parent, STATE_KEY.format(pid=provider_id))
    word = state.get("state")
    if word not in STATUS_TEXTS:
        return None
    reason = state.get("reason")
    reason = reason if isinstance(reason, str) and _WORD.fullmatch(reason) else None
    at = state.get("at") if isinstance(state.get("at"), str) else None
    return {
        "at": at,
        "state": word,
        "reason": reason,
        "text": FeedOutcome(provider_id, word, reason).text,
        "detail": REASON_TEXTS.get(reason) if reason is not None else None,
    }


_WORD = re.compile(r"[a-z0-9_]{1,40}", re.ASCII)
_OUTCOME_FILE = re.compile(r"provider_([A-Z][A-Z0-9_-]{1,15})_\.station_extract\.json", re.ASCII)


def forget_outcome(sid: str, provider_id: str) -> None:
    """Remove a provider's last outcome (its slot got another file). Best-effort."""
    try:
        path = _outcome_path(sid, provider_id)
    except ValueError:
        return
    with contextlib.suppress(OSError):
        Path(path).unlink(missing_ok=True)


def forget_outcomes_except(sid: str, keep: set[str] | None) -> None:
    """Remove the last outcome of every provider not in `keep` (removed from
    the session). `keep` None (a config that cannot be read): nothing."""
    if keep is None:
        return
    try:
        names = [entry.name for entry in _fetch_state_dir(sid).iterdir()]
    except (OSError, ValueError):
        return
    for name in names:
        found = _OUTCOME_FILE.fullmatch(name)
        if found is not None and found.group(1) not in keep:
            forget_outcome(sid, found.group(1))


# ── the entry point ──────────────────────────────────────────────────────


def _session_header(sid: str) -> str | None:
    return sid if SESSION_ID.fullmatch(sid) else None


def _log_outcome(outcome: FeedOutcome) -> None:
    if outcome.state in (REFUSED, UNAVAILABLE):
        log.warning(
            "station_extract.%s provider=%s reason=%s",
            outcome.state,
            outcome.provider_id,
            outcome.reason,
        )
    elif outcome.state == SKIPPED and outcome.reason not in (NO_FEED_KEY, NO_ARCHIVE):
        log.warning(
            "station_extract.skipped provider=%s reason=%s", outcome.provider_id, outcome.reason
        )


def _source(provider_cfg: dict[str, Any]) -> str:
    timetable = provider_cfg.get("timetable") or {}
    return str(timetable.get("source") or ("url" if timetable.get("url") else "upload"))


def _extract(sid: str, feed: Feed, wait_seconds: float, cache_only: bool = False) -> Any:
    """The extract of a feed, or a reason word when there is none, through
    the extractor's one safe entry point, `station_extract.ensure_extract`.

    `cache_only` (the worker, before a graph build): only an extract already
    in the cache; nothing is started, and a feed without one is `not_cached`
    (the refresh's pass sends it, after the extractor's warm-up). Otherwise
    (after a refresh): a cached extract at once, else a resource-limited
    child process, one extraction at a time on the machine, waiting at most
    `wait_seconds` for another one (`busy`), on a snapshot of the slot that
    the extractor checks against the fetch state (`provider_archive_snapshot`:
    the slot's size and sha256). A slot it cannot vouch for (replaced by a
    refresh meanwhile, not yet described by its state) gives no snapshot
    (`no_snapshot`); a snapshot of another version than the one the module
    was asked about is `archive_changed`. Both go with the next pass. Every
    reason of the extractor (`memory_limit`, `busy`, `too_large`,
    `no_rail_stop`, ...) is a skip: none says the module is unavailable."""
    sha = str(feed.archive_sha256)
    try:
        if cache_only:
            return station_extract.ensure_extract(
                feed.provider_cfg,
                Path(str(feed.archive)),
                sha,
                extracted_at=datetime.now(UTC),
                session_id=sid,
                wait_seconds=0.0,
                cache_only=True,
            )
        upload_sha = sha if _source(feed.provider_cfg) == "upload" else None
        with station_extract.provider_archive_snapshot(
            sid, feed.provider_cfg, upload_sha256=upload_sha
        ) as found:
            if found is None:
                return NO_SNAPSHOT
            if found[1] != sha:
                return ARCHIVE_CHANGED
            return station_extract.ensure_extract(
                feed.provider_cfg,
                found[0],
                found[1],
                extracted_at=datetime.now(UTC),
                session_id=sid,
                wait_seconds=wait_seconds,
            )
    except station_extract.ExtractError as refused:
        return refused.reason if _WORD.fullmatch(refused.reason) else NO_EXTRACT
    except Exception as exc:  # the snapshot's file system, never the extraction
        log.warning(_FAILED, ERROR, type(exc).__name__)
        return ERROR


def _ready(sid: str, feed: Feed) -> bool:
    """Whether a feed's outcome needs no extraction: no key or no archive
    (skipped at once), or an extract, or a remembered failure, in the
    extractor's cache."""
    if feed.feed_key is None or feed.archive is None or feed.archive_sha256 is None:
        return True
    return station_extract.extract_known(feed.provider_cfg, feed.archive_sha256, session_id=sid)


def _archive_size(feed: Feed) -> int:
    try:
        return feed.archive.stat().st_size if feed.archive is not None else 0
    except OSError:
        return 0


def _send_order(
    sid: str, feeds: list[Feed], ready: Callable[[str, Feed], bool]
) -> list[tuple[int, Feed]]:
    """The feeds with their place in the session, in the order they are
    sent: those that need no extraction first, then by the size of their
    timetable file, smallest first; the session's order between equals."""

    def key(item: tuple[int, Feed]) -> tuple[bool, int, int]:
        place, feed = item
        try:
            known = ready(sid, feed)
        except Exception:  # an ordering hint only
            known = False
        return (not known, 0 if known else _archive_size(feed), place)

    return sorted(enumerate(feeds), key=key)


def send_session_extracts(
    sid: str,
    feeds: Iterable[Feed],
    *,
    budget: float,
    should_stop: Callable[[], bool] = lambda: False,
    clock: Clock | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    extract: Callable[[str, Feed, float, bool], Any] = _extract,
    record: bool = True,
    cache_only: bool = False,
    ready: Callable[[str, Feed], bool] = _ready,
    observer: PassObserver | None = None,
) -> PassResult:
    """Send what the module lacks of a session's feeds, within `budget` seconds,
    in the order of `_send_order` (`ready` says which need no extraction).
    `observer`, when given, is told the pass's start, each feed and the end.

    Never raises an `Exception`: an unexpected one ends the pass (`error`,
    one WARNING). The caller goes on whatever the result (a graph is built,
    a refresh answers)."""
    result = PassResult()
    follow = observer or _UNOBSERVED
    if not station_module.enabled():
        result.off = True
        log.info(_SKIPPED_OFF)
        follow.finished(OFF)
        return result
    clock = clock or SystemClock()
    done: list[tuple[int, FeedOutcome]] = []
    end = ERROR
    try:
        work = _Pass(
            _Calls(transport, _session_header(sid)),
            clock,
            budget,
            should_stop,
            extract,
            station_extract.EXTRACTOR_VERSION,
            sid,
            cache_only,
        )
        order = _send_order(sid, list(feeds), ready)
        follow.started(len(order))
        for place, feed in order:
            if should_stop():
                raise _Stop
            follow.feed_started(feed.provider_id)
            outcome = work.feed(feed)
            _log_outcome(outcome)
            STATION_EXTRACT_FEEDS_TOTAL.labels(outcome=outcome.state).inc()
            done.append((place, outcome))
            if record:
                record_outcome(sid, outcome)
            follow.outcome(outcome)
        end = "finished"
    except _Stop:
        end = CANCELLED
        result.cancelled = True
        log.info("station_extract.cancelled")
        return result
    except Exception as exc:
        log.warning(_FAILED, ERROR, type(exc).__name__)
        return result
    finally:
        result.outcomes = [outcome for _, outcome in sorted(done, key=lambda item: item[0])]
        follow.finished(end)
    counts = result.counts()
    log.info(
        "station_extract.done feeds=%d sent=%d held=%d refused=%d unavailable=%d skipped=%d",
        len(result.outcomes),
        counts.get(SENT, 0),
        counts.get(HELD, 0),
        counts.get(REFUSED, 0),
        counts.get(UNAVAILABLE, 0),
        counts.get(SKIPPED, 0),
    )
    return result


# ── the two callers ──────────────────────────────────────────────────────

# One pass at a time in a process: two refreshes finishing together would
# otherwise send at once, and the module takes one extract at a time.
_one_at_a_time = threading.Lock()


def _feeds_of(db: Any, sid: str, only_provider: str | None) -> list[Feed]:
    from .models import Session as SessionRow

    row = db.get(SessionRow, sid)
    if row is None:
        return []
    feeds = session_feeds(sid, row.config or {}, latest_upload_sha256(db, sid))
    if only_provider is not None:
        feeds = [f for f in feeds if f.provider_id == only_provider]
    return feeds


def send_session(
    sid: str,
    *,
    budget: float,
    only_provider: str | None = None,
    should_stop: Callable[[], bool] = lambda: False,
    session_factory: Callable[[], Any] | None = None,
    cache_only: bool = False,
    observer: PassObserver | None = None,
) -> PassResult:
    """Read a session's feeds from the database, then send them. Never raises
    an `Exception`. Used by the worker before a graph build, and by the
    refresh endpoints in a background task (`send_after_refresh`)."""
    follow = observer or _UNOBSERVED
    if not station_module.enabled():
        log.info(_SKIPPED_OFF)
        follow.finished(OFF)
        return PassResult(off=True)
    try:
        if session_factory is None:
            from .db import SessionLocal

            session_factory = SessionLocal
        with session_factory() as db:
            feeds = _feeds_of(db, sid, only_provider)
    except Exception as exc:
        log.warning(_FAILED, ERROR, type(exc).__name__)
        follow.finished(ERROR)
        return PassResult()
    with _one_at_a_time:
        return send_session_extracts(
            sid,
            feeds,
            budget=budget,
            should_stop=should_stop,
            cache_only=cache_only,
            observer=observer,
        )


def send_after_refresh(
    sid: str, only_provider: str | None = None, run_id: uuid.UUID | None = None
) -> None:
    """The pass of a refresh (`REFRESH_BUDGET_SECONDS`), run by the sender
    thread. The pass of a whole-session refresh is written to that refresh's
    progress record (`run_id`, issue #359), which the Sessions page shows."""
    observer: PassObserver | None = None
    if only_provider is None and run_id is not None:
        from .refresh_progress import ExtractFollowUp

        observer = ExtractFollowUp(sid, run_id)
    send_session(sid, budget=REFRESH_BUDGET_SECONDS, only_provider=only_provider, observer=observer)


# The refresh endpoints hand their pass to one sender thread of the process,
# which runs the passes one after the other, never on the event loop and
# never inside the refresh. Not a FastAPI background task: the request's
# task list holds the extractor's own warm-up (`station_extract.
# warm_session_extracts`, pull request 4), which its tests pin as the only
# task. Both go through `station_extract.ensure_extract`, one extraction at
# a time with its cache, so an archive is never extracted twice. A pass
# already waiting for the same session and provider is not queued twice;
# it then reports to the newer refresh's record (`_queued` keeps the
# latest `run_id` of each waiting pass), since it has not started yet.
_waiting: queue.Queue[tuple[str, str | None]] = queue.Queue()
_queued: dict[tuple[str, str | None], uuid.UUID | None] = {}
_queued_lock = threading.Lock()
_sender: threading.Thread | None = None


def _send_queued() -> None:
    while True:
        item = _waiting.get()
        with _queued_lock:
            run_id = _queued.pop(item, None)
        try:
            if run_id is None:
                send_after_refresh(*item)
            else:
                send_after_refresh(*item, run_id=run_id)
        except Exception as exc:  # the thread must outlive any one pass
            log.warning(_FAILED, ERROR, type(exc).__name__)
        finally:
            _waiting.task_done()


def after_refresh(
    sid: str, only_provider: str | None = None, run_id: uuid.UUID | None = None
) -> None:
    """Queue the pass of a refresh; returns at once. `run_id`: the progress
    record of the whole-session refresh that queued it. Without the module
    configured nothing is queued (one INFO line)."""
    global _sender
    if not station_module.enabled():
        log.info(_SKIPPED_OFF)
        return
    item = (sid, only_provider)
    with _queued_lock:
        waiting = item in _queued
        _queued[item] = run_id
        if waiting:
            return
        if _sender is None or not _sender.is_alive():
            _sender = threading.Thread(target=_send_queued, name="station-extracts", daemon=True)
            _sender.start()
    _waiting.put(item)


LOG_MARKER = "[viator] station-extracts:"


def log_line(result: PassResult) -> str:
    """The rebuild job's line for the pass before a graph build (counts and
    reason words only), or "" when the module is not configured."""
    if result.off:
        return ""
    if result.cancelled:
        return f"{LOG_MARKER} cancelled by an operator\n"
    counts = result.counts()
    parts = [
        f"{word}={counts[word]}"
        for word in (SENT, HELD, REFUSED, UNAVAILABLE, SKIPPED)
        if counts.get(word)
    ]
    return f"{LOG_MARKER} {' '.join(parts) or 'no feed'}\n"
