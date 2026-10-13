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
  the build sees the new extracts (`BUILD_BUDGET_SECONDS`).

Per feed with a feed key: `HEAD`; on 200 nothing more ("already held"); on
404 `build_extract` (which reuses its cache by archive sha256 and
`EXTRACTOR_VERSION`), then the `PUT`.

**Bounded.** Each call runs under `asyncio.timeout` on its own event loop
(the per-call deadline of `app/station_builds.py`): `HEAD_SECONDS` for a
`HEAD`, `PUT_SECONDS` for a `PUT`, never past the end of the pass's budget.
The pass stops starting new work once its budget is spent; the feeds left
are "skipped" with the reason `budget`. Retries:

- 503 `busy` (another extract being received) and 429 `extract_window` (60
  extracts in the UTC half hour): wait the `Retry-After`, then send again,
  only when the wait fits in what is left of the budget (and is at most
  `RETRY_AFTER_MAX_SECONDS`); else the feed ends "module unavailable" with
  the code as its reason. A 429 that cannot be waited ends every `PUT` of
  the pass (the window is the module's, for every feed);
- 408 `timeout`, any other 5xx, a call past its deadline, a lost
  connection: tried again `TRANSIENT_RETRIES` times, `TRANSIENT_PAUSE_SECONDS`
  apart, within the budget;
- every other 4xx is a refusal: never sent again in the pass, logged with
  its reason word only ("refused <code>").

**Never blocking.** Nothing here raises an `Exception` to its caller: an
unexpected one ends the pass (`error`, one WARNING). A failed upload never
keeps a graph from being built. Without `STATION_MODULE_URL` or
`STATION_MODULE_TOKEN`, no call: one INFO line.

**What the admin sees.** The last outcome of each provider is written in the
session's fetch-state folder (`inbox/<sid>/_fetch_state/`, the store of the
feed-status panel), under its own key `provider[<id>].station_extract`, never
in a fetch's own record, so a refresh writing the fetch state at the same
moment loses nothing. The feed-status panel of the Sessions page shows it as
a short text (`STATUS_TEXTS`). No migration.

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
import json
import logging
import queue
import re
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterable
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
PUT_STATUSES = frozenset({"recorded", "unchanged"})
STATE_KEY = "provider[{pid}].station_extract"


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
    """The file, a piece at a time: an extract is never held whole in memory."""
    with path.open("rb") as handle:
        while piece := handle.read(CHUNK_BYTES):
            yield piece


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
        return asyncio.run(self._send("HEAD", url, limit, params=params))

    def put(self, feed_key: str, path: Path, size: int, limit: float) -> _Answer:
        url = station_module._url(f"{FEEDS_PATH}/{feed_key}/extract")
        headers = {"Content-Type": CONTENT_TYPE, "Content-Length": str(size)}
        return asyncio.run(self._send("PUT", url, limit, path=path, extra=headers))

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
    ) -> None:
        self.calls = calls
        self.clock = clock
        self.deadline = clock.monotonic() + budget
        self.should_stop = should_stop
        self.extract = extract
        self.extractor_version = extractor_version
        self.window_full = False

    def left(self) -> float:
        return self.deadline - self.clock.monotonic()

    def limit(self, call_seconds: float) -> float:
        return max(0.0, min(call_seconds, self.left()))

    def pause(self, seconds: float) -> bool:
        """Sleep `seconds` when they fit in the budget; False when they do not."""
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
        transient_left = TRANSIENT_RETRIES
        while True:
            if self.left() <= 0:
                raise _Failure(BUDGET)
            try:
                answer = send(self.limit(call_seconds))
            except _Failure:
                if transient_left > 0 and self.pause(TRANSIENT_PAUSE_SECONDS):
                    transient_left -= 1
                    continue
                raise
            if _waits(answer):
                if self.pause(_retry_wait(answer)):
                    continue
            elif _transient(answer) and transient_left > 0 and self.pause(TRANSIENT_PAUSE_SECONDS):
                transient_left -= 1
                continue
            return answer

    def feed(self, feed: Feed) -> FeedOutcome:
        pid = feed.provider_id
        if feed.feed_key is None:
            return FeedOutcome(pid, SKIPPED, NO_FEED_KEY)
        if feed.archive is None or feed.archive_sha256 is None:
            return FeedOutcome(pid, SKIPPED, NO_ARCHIVE)
        key, sha = feed.feed_key, feed.archive_sha256
        try:
            answer = self._call(
                lambda limit: self.calls.head(key, sha, self.extractor_version, limit),
                HEAD_SECONDS,
            )
        except _Failure as failure:
            return _unreached(pid, failure.reason)
        if answer.status == 200:
            return FeedOutcome(pid, HELD)
        if answer.status != 404:
            return _refused(pid, answer)
        return self._send(feed, key)

    def _send(self, feed: Feed, key: str) -> FeedOutcome:
        pid = feed.provider_id
        if self.window_full:
            return FeedOutcome(pid, UNAVAILABLE, EXTRACT_WINDOW)
        if self.left() <= 0:
            return FeedOutcome(pid, SKIPPED, BUDGET)
        assert feed.archive is not None and feed.archive_sha256 is not None
        made = self.extract(feed.provider_cfg, feed.archive, feed.archive_sha256)
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
        try:
            answer = self._call(lambda limit: self.calls.put(key, path, size, limit), PUT_SECONDS)
            if answer.status == 200:
                uploaded = _put_answer(answer, made.body_sha256, size, int(made.rows))
                return FeedOutcome(pid, SENT if uploaded.status == "recorded" else HELD)
        except _Failure as failure:
            return _unreached(pid, failure.reason)
        if answer.status == 429:
            self.window_full = True
        return _refused(pid, answer)


def _size_on_disk(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def _unreached(pid: str, reason: str) -> FeedOutcome:
    if reason == BUDGET:
        return FeedOutcome(pid, SKIPPED, BUDGET)
    return FeedOutcome(pid, UNAVAILABLE, reason)


def _refused(pid: str, answer: _Answer) -> FeedOutcome:
    """A final answer other than a success: a 4xx refusal with its word, else
    the module unavailable (404 of a module without these calls, 5xx, the
    busy and window answers that could not be waited)."""
    code = _code(answer)
    status = answer.status
    if status == 503 and code == BUSY:
        return FeedOutcome(pid, UNAVAILABLE, BUSY)
    if status == 429:
        return FeedOutcome(pid, UNAVAILABLE, EXTRACT_WINDOW)
    if status == 404 or status == 408 or status >= 500:
        return FeedOutcome(pid, UNAVAILABLE, f"status_{status}")
    if 400 <= status < 500 and code in REFUSAL_CODES:
        return FeedOutcome(pid, REFUSED, code)
    return FeedOutcome(pid, REFUSED, f"status_{status}")


# ── the feeds of a session ───────────────────────────────────────────────


def _fetch_state_dir(sid: str) -> Path:
    return settings.inbox_dir / sid / "_fetch_state"


def _archive(sid: str, provider: dict[str, Any]) -> Path | None:
    """The timetable file in the provider's slot, or None."""
    fmt = (provider.get("timetable") or {}).get("format", "gtfs")
    details = ingestion.TIMETABLE_FORMAT_DETAILS.get(fmt)
    if details is None:
        return None
    path = (
        settings.inbox_dir
        / sid
        / details["subdir"]
        / ingestion.staged_filename_for_format(provider["id"], fmt)
    )
    return path if path.is_file() else None


def session_feeds(sid: str, config: dict[str, Any], upload_sha256: dict[str, str]) -> list[Feed]:
    """The session's providers as feeds. The archive sha256 is the sha256 of
    the bytes VIATOR downloaded, kept in the fetch state (`sha256`), for a
    `url` or `nap` provider; the sha256 of the uploaded file (its `Upload`
    row, `upload_sha256` by provider id) for an `upload` one, which is also
    its feed key. A provider whose slot is empty, or whose file came in
    another way than its source (no fetch state), has no archive."""
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
            label = f"provider[{pid}].timetable({fmt})"
            state = feed_fetch.load_state(_fetch_state_dir(sid), label)
            raw = state.get("sha256")
            sha = raw if isinstance(raw, str) else None
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
    feed_fetch.save_state(_fetch_state_dir(sid), STATE_KEY.format(pid=outcome.provider_id), state)


def load_outcome(sid: str, provider_id: str) -> dict[str, Any] | None:
    """A provider's last outcome as the Sessions page shows it, or None:
    `{"at", "state", "reason", "text"}`, the text one of `STATUS_TEXTS`
    (with the refusal's word)."""
    state = feed_fetch.load_state(_fetch_state_dir(sid), STATE_KEY.format(pid=provider_id))
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
    }


_WORD = re.compile(r"[a-z0-9_]{1,40}", re.ASCII)


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


def _extract(provider_cfg: dict[str, Any], archive: Path, sha: str) -> Any:
    """The extractor's result, or a reason word when it made none."""
    try:
        return station_extract.build_extract(
            provider_cfg, archive, sha, extracted_at=datetime.now(UTC)
        )
    except station_extract.ExtractError as refused:
        return refused.reason if _WORD.fullmatch(refused.reason) else NO_EXTRACT


def send_session_extracts(
    sid: str,
    feeds: Iterable[Feed],
    *,
    budget: float,
    should_stop: Callable[[], bool] = lambda: False,
    clock: Clock | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    extract: Callable[[dict[str, Any], Path, str], Any] = _extract,
    record: bool = True,
) -> PassResult:
    """Send what the module lacks of a session's feeds, within `budget` seconds.

    Never raises an `Exception`: an unexpected one ends the pass (`error`,
    one WARNING). The caller goes on whatever the result (a graph is built,
    a refresh answers)."""
    result = PassResult()
    if not station_module.enabled():
        result.off = True
        log.info("station_extract.skipped reason=off")
        return result
    clock = clock or SystemClock()
    try:
        work = _Pass(
            _Calls(transport, _session_header(sid)),
            clock,
            budget,
            should_stop,
            extract,
            station_extract.EXTRACTOR_VERSION,
        )
        for feed in feeds:
            if should_stop():
                raise _Stop
            outcome = work.feed(feed)
            _log_outcome(outcome)
            STATION_EXTRACT_FEEDS_TOTAL.labels(outcome=outcome.state).inc()
            result.outcomes.append(outcome)
            if record:
                record_outcome(sid, outcome)
    except _Stop:
        result.cancelled = True
        log.info("station_extract.cancelled")
        return result
    except Exception as exc:
        log.warning("station_extract.failed reason=%s type=%s", ERROR, type(exc).__name__)
        return result
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
) -> PassResult:
    """Read a session's feeds from the database, then send them. Never raises
    an `Exception`. Used by the worker before a graph build, and by the
    refresh endpoints in a background task (`send_after_refresh`)."""
    if not station_module.enabled():
        log.info("station_extract.skipped reason=off")
        return PassResult(off=True)
    try:
        if session_factory is None:
            from .db import SessionLocal

            session_factory = SessionLocal
        with session_factory() as db:
            feeds = _feeds_of(db, sid, only_provider)
    except Exception as exc:
        log.warning("station_extract.failed reason=%s type=%s", ERROR, type(exc).__name__)
        return PassResult()
    with _one_at_a_time:
        return send_session_extracts(sid, feeds, budget=budget, should_stop=should_stop)


def send_after_refresh(sid: str, only_provider: str | None = None) -> None:
    """The pass of a refresh (`REFRESH_BUDGET_SECONDS`), run by the sender thread."""
    send_session(sid, budget=REFRESH_BUDGET_SECONDS, only_provider=only_provider)


# The refresh endpoints hand their pass to one sender thread of the process,
# which runs the passes one after the other, never on the event loop and
# never inside the refresh. Not a FastAPI background task: the request's
# task list holds the extractor's own warm-up (`station_extract.
# warm_session_extracts`, pull request 4), which its tests pin as the only
# task. The two may then extract one archive at the same time: the
# extractor writes its cache file in one rename, so the cost is time, not a
# broken file. A pass already waiting for the same session and provider is
# not queued twice.
_waiting: queue.Queue[tuple[str, str | None]] = queue.Queue()
_queued: set[tuple[str, str | None]] = set()
_queued_lock = threading.Lock()
_sender: threading.Thread | None = None


def _send_queued() -> None:
    while True:
        item = _waiting.get()
        with _queued_lock:
            _queued.discard(item)
        try:
            send_after_refresh(*item)
        except Exception as exc:  # the thread must outlive any one pass
            log.warning("station_extract.failed reason=%s type=%s", ERROR, type(exc).__name__)
        finally:
            _waiting.task_done()


def after_refresh(sid: str, only_provider: str | None = None) -> None:
    """Queue the pass of a refresh; returns at once. Without the module
    configured nothing is queued (one INFO line)."""
    global _sender
    if not station_module.enabled():
        log.info("station_extract.skipped reason=off")
        return
    item = (sid, only_provider)
    with _queued_lock:
        if item in _queued:
            return
        _queued.add(item)
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
