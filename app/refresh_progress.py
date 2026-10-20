"""The progress of a session's source refreshes, kept in the database (issue #359).

A provider refresh of a large session (about a hundred feeds) runs for many
minutes inside one request, and an OSM refresh downloads gigabytes. Before
this module the page only said "Downloading…", and a reload lost even that
while the refresh went on. Now each refresh writes its progress to one row of
`source_refresh_progress` (`app/models/runtime.py`), per session and kind
(`PROVIDERS`, `OSM`): the page reads it with `GET /api/sessions/{sid}/sources/
progress`, after a reload, from another tab or another browser, whichever web
process serves the request.

**One refresh of a kind at a time per session** (`claim`). A second refresh of
the same kind while one runs is refused (HTTP 409 `refresh_already_running`,
with the time the running one started), never joined: the running refresh
belongs to another request, perhaps of another process, and the record
already gives the second admin everything the first one sees. The claim is
one database transaction (an `INSERT … ON CONFLICT DO UPDATE … WHERE state <>
'running'`), so two processes, or two requests of one process, can never
both win it.

**Lease.** A running refresh writes `heartbeat_at` at least every
`HEARTBEAT_SECONDS` (a task of its own, besides every progress write). A
`running` row whose heartbeat is older than `LEASE_SECONDS` belongs to a
process that died (a restart, a crash): it is marked `interrupted` (when the
page reads it, or when the next refresh claims the row), and a new refresh is
allowed. Every write names the refresh's `run_id` and requires `running`, so
a refresh that lost its lease (an event loop blocked past the lease) changes
nothing any more; it logs one WARNING, finishes the download under way and
starts no other (`RefreshRun.lost`). Inside `async with` the progress writes
go to one writer thread, in order, so the event loop never waits on them.

**What is written** (`detail`): the counts (`total`, `done`, `fetched`,
`unchanged`, `failed`, `not_reached`), the phase, the task being downloaded
with its bytes received and expected (when the server announced a length), and
the labels of the failed tasks. Bytes are written at most every
`BYTES_EVERY_SECONDS` or every `BYTES_STEP_PERCENT` % of a feed, never once
per chunk. Each current task also carries its `step` (`feed_fetch.STEPS`):
downloading, then verifying (format check), checking_structure (the NeTEx
structure check, with the bytes of uncompressed XML scanned and their total
from the zip directory, throttled like the bytes) and storing (the copy into
the slot), and the time it started (`elapsed_seconds` when read): after a
download of a national file the structure check alone runs for many minutes,
and a block frozen on "100 %" looked stuck (production, 15 Oct). A step change
is one write; a record written before steps existed reads `step: null`. `extract`: the station-module follow-up of a provider refresh
(`ExtractFollowUp`, fed by the sender thread of `app/station_extract_upload.py`):
queued, then running with its counts per outcome and reason word, then
finished; `off` when the module is not configured; read as `interrupted` when
it went silent (`EXTRACT_QUEUED_SECONDS`, `EXTRACT_LEASE_SECONDS`). It is
written only into the record of the refresh that queued it.

**One provider alone** ("Refresh this provider" on a provider card): a
record of its own, whose scope is the provider's feed id (`provider_scope`;
a feed id begins with a capital letter, the two kinds above never do, and it
fits the column), one per provider of the session, with the same steps,
bytes, lease and fencing, and its own station-module follow-up. Claims of one
session are taken one at a time (a transaction-level advisory lock), so the
rules between records hold: a second refresh of the same provider is refused
(409 `refresh_already_running`), a provider alone is refused while the whole
session's refresh runs (it downloads that provider too), and the whole
session's refresh is refused while a provider alone runs (409
`provider_refresh_running`): both would download the same feed into the same
slot, fetch state and staging folder, and waiting instead would hold the
request with no record for as long as that download takes. Refreshes of two
different providers may run together; OSM is independent of all of them.

**Never a URL, a reason text or a secret.** Task labels such as
`provider[ZZRAIL].timetable(gtfs)` (a provider id of the session's own
configuration), counts, reason words that match `_WORD`, the type name of an
unexpected exception. A skip reason can quote a URL; the feed-status panel
shows reasons, this record never does.

**Never in the way.** A failed progress write logs one WARNING (the type of
the exception only) and the refresh goes on: the record is best effort, the
refresh is not.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import logging
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Protocol

from sqlalchemy import ColumnElement, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from . import feed_fetch
from .models import SourceRefreshProgress as Row

log = logging.getLogger(__name__)

PROVIDERS = "providers"
OSM = "osm"
SCOPES = (PROVIDERS, OSM)
# The scope of a single provider's refresh is its feed id: the pattern of
# app/ingestion.py `_FEED_ID_RE` (2 to 16 characters, the column's width).
_FEED_ID = re.compile(r"[A-Z][A-Z0-9_-]{1,15}", re.ASCII)
# Single-provider records shown at most (running ones first).
SINGLE_SHOWN_MAX = 4

RUNNING = "running"
FINISHED = "finished"
FAILED = "failed"
INTERRUPTED = "interrupted"

# A running row with no heartbeat for this long belongs to a dead process.
LEASE_SECONDS = 300
# The heartbeat of a running refresh, whatever its downloads do.
HEARTBEAT_SECONDS = 30.0
# Bytes of the feed being downloaded: at most one write per this many
# seconds, or per this step of the expected size.
BYTES_EVERY_SECONDS = 2.0
BYTES_STEP_PERCENT = 5
# Labels of failed tasks kept in the record.
FAILED_KEYS_MAX = 50
# The station-module follow-up: a pass beats `updated_at` this often while it
# runs; a running follow-up silent for `EXTRACT_LEASE_SECONDS`, or one still
# queued after `EXTRACT_QUEUED_SECONDS` (longer than the pass's 30-minute
# budget: the web process restarted and lost its in-memory queue), is shown
# interrupted.
EXTRACT_HEARTBEAT_SECONDS = 30.0
EXTRACT_LEASE_SECONDS = 300
EXTRACT_QUEUED_SECONDS = 35 * 60

# Phases of a provider refresh.
DOWNLOADING = "downloading"
DERIVED = "derived"
FINISHING = "finishing"
DERIVED_LABEL = "cross-border filters"

# Outcome states of the station-module follow-up
# (app/station_extract_upload.py STATUS_TEXTS keys).
EXTRACT_STATES = ("sent", "held", "refused", "unavailable", "skipped", "cancelled")
_EXTRACT_ENDS = ("finished", "cancelled", "error", "off")
_WORD = re.compile(r"[a-z0-9_]{1,40}", re.ASCII)
_LABEL = re.compile(r"[\w\[\]().:\- ]{1,120}", re.ASCII)

_FAILED_WRITE = "refresh_progress.write_failed scope=%s type=%s"

Factory = Callable[[], Any]


def _session_factory(factory: Factory | None) -> Factory:
    if factory is not None:
        return factory
    from .db import SessionLocal

    return SessionLocal


def _lease_cutoff() -> ColumnElement[Any]:
    """The database's now() minus the lease: one clock for every process."""
    return func.now() - func.make_interval(0, 0, 0, 0, 0, 0, LEASE_SECONDS)


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat(timespec="seconds") if value else None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _label(value: object) -> str:
    """A task label as stored: the label itself when it has the expected
    shape, else a neutral word (never a free text)."""
    return value if isinstance(value, str) and _LABEL.fullmatch(value) else "task"


def task_label(value: object) -> str:
    """`_label` for the refresh code: a task label fit for the record and
    for a log line (its expected shape, else the word "task")."""
    return _label(value)


def _word(value: object) -> str:
    return value if isinstance(value, str) and _WORD.fullmatch(value) else "other"


def provider_scope(pid: str) -> str:
    """The scope of the refresh of provider `pid` alone: its feed id. Raises
    ValueError for anything that is not a feed id (never a free text)."""
    if not isinstance(pid, str) or not _FEED_ID.fullmatch(pid):
        raise ValueError("not a feed id")
    return pid


def is_provider_scope(scope: object) -> bool:
    """True for the scope of a single provider's refresh."""
    return isinstance(scope, str) and _FEED_ID.fullmatch(scope) is not None


def refresh_scope(only_provider: str | None) -> str:
    """The scope of the refresh that queued a station-module pass: the whole
    session's provider refresh, or that provider's alone."""
    return PROVIDERS if only_provider is None else provider_scope(only_provider)


def _expire(db: Any, sid: str, scope: str | None = None) -> None:
    """Mark interrupted the session's running records (of `scope`, or every
    kind) whose heartbeat is older than the lease; they end at their last
    heartbeat. The caller commits."""
    stale = update(Row).where(
        Row.session_id == sid,
        Row.state == RUNNING,
        Row.heartbeat_at < _lease_cutoff(),
    )
    if scope is not None:
        stale = stale.where(Row.scope == scope)
    db.execute(stale.values(state=INTERRUPTED, finished_at=Row.heartbeat_at))


# ── the claim ────────────────────────────────────────────────────────────


class RefreshRunning(Exception):
    """A refresh that forbids this one already runs for the session (HTTP
    409). `scope` is the running refresh's, `requested` the refused one's."""

    def __init__(
        self, scope: str, started_at: datetime | None, requested: str | None = None
    ) -> None:
        super().__init__("a refresh is already running")
        self.scope = scope
        self.started_at = started_at
        self.requested = requested if requested is not None else scope

    def detail(self) -> dict[str, Any]:
        """The 409 body: `error` `refresh_already_running` (the same record
        is running, or the whole session's while a provider alone was asked)
        or `provider_refresh_running` (a provider alone, while the whole
        session's was asked); `scope` `providers`, `osm` or `provider` (with
        `provider_id`)."""
        started = _iso(self.started_at)
        since = f" (started at {started})" if started else ""
        single = is_provider_scope(self.scope)
        tail = "Its progress is shown on the page; start a new one when it has ended."
        if single and self.requested == PROVIDERS:
            error = "provider_refresh_running"
            message = (
                f"A refresh of provider {self.scope} alone is running{since}. Refresh "
                f"providers downloads that provider too. {tail}"
            )
        else:
            error = "refresh_already_running"
            what = {OSM: "An OSM refresh", PROVIDERS: "A provider refresh"}.get(self.scope)
            subject = f"{what} of this session" if what else f"A refresh of provider {self.scope}"
            message = f"{subject} is already running{since}. {tail}"
        return {
            "error": error,
            "scope": "provider" if single else self.scope,
            "provider_id": self.scope if single else None,
            "started_at": started,
            "message": message,
        }


def _initial_detail(total: int, previous: datetime | None) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "total": total,
        "done": 0,
        "fetched": 0,
        "unchanged": 0,
        "failed": 0,
        "not_reached": 0,
        "phase": DOWNLOADING,
        "current": [],
        "failed_keys": [],
    }
    if previous is not None:
        # The refresh this one replaces had stopped without a word.
        detail["previous"] = {"state": INTERRUPTED, "started_at": _iso(previous)}
    return detail


def _blocker(db: Any, sid: str, scope: str) -> RefreshRunning | None:
    """The running refresh of another record that forbids `scope`: the whole
    session's for a provider alone, any provider alone for the whole
    session's (the oldest). None for OSM, and when nothing forbids it."""
    if scope == PROVIDERS:
        others = select(Row.scope, Row.started_at).where(
            Row.session_id == sid, Row.state == RUNNING, Row.scope.not_in(SCOPES)
        )
    elif is_provider_scope(scope):
        others = select(Row.scope, Row.started_at).where(
            Row.session_id == sid, Row.state == RUNNING, Row.scope == PROVIDERS
        )
    else:
        return None
    found = db.execute(others.order_by(Row.started_at).limit(1)).one_or_none()
    return RefreshRunning(found.scope, found.started_at, scope) if found else None


def claim(sid: str, scope: str, total: int, *, factory: Factory | None = None) -> RefreshRun:
    """Make this request the session's running refresh of `scope`, or raise
    `RefreshRunning`. One transaction, one claim of the session at a time (an
    advisory lock held until it ends): stale running rows (no heartbeat for
    `LEASE_SECONDS`) are first marked interrupted, a running record that
    forbids this one refuses it (`_blocker`), then the row is taken only when
    it is not running. A replaced interrupted refresh is named in the new
    record (`previous`)."""
    if scope not in SCOPES:
        provider_scope(scope)
    run_id = uuid.uuid4()
    with _session_factory(factory)() as db:
        db.execute(select(func.pg_advisory_xact_lock(func.hashtext(f"refresh_progress:{sid}"))))
        _expire(db, sid)
        blocker = _blocker(db, sid, scope)
        if blocker is not None:
            db.rollback()
            raise blocker
        # The row as it stands, locked until the claim commits or fails.
        replaced = db.execute(
            select(Row.state, Row.started_at)
            .where(Row.session_id == sid, Row.scope == scope)
            .with_for_update()
        ).one_or_none()
        interrupted = replaced.started_at if replaced and replaced.state == INTERRUPTED else None
        detail = _initial_detail(total, interrupted)
        insert = pg_insert(Row).values(
            session_id=sid,
            scope=scope,
            run_id=run_id,
            state=RUNNING,
            started_at=func.now(),
            heartbeat_at=func.now(),
            finished_at=None,
            detail=detail,
            extract={},
        )
        taken = insert.on_conflict_do_update(
            index_elements=[Row.session_id, Row.scope],
            set_={
                name: insert.excluded[name]
                for name in (
                    "run_id",
                    "state",
                    "started_at",
                    "heartbeat_at",
                    "finished_at",
                    "detail",
                    "extract",
                )
            },
            where=Row.state != RUNNING,
        ).returning(Row.run_id)
        if db.execute(taken).scalar_one_or_none() is None:
            db.rollback()
            started = db.execute(
                select(Row.started_at).where(Row.session_id == sid, Row.scope == scope)
            ).scalar_one_or_none()
            raise RefreshRunning(scope, started)
        db.commit()
    return RefreshRun(sid, scope, run_id, detail, factory=factory)


def check_free(sid: str, scope: str, *, factory: Factory | None = None) -> None:
    """Raise `RefreshRunning` when a claim of `scope` would be refused now,
    without claiming (a request that will answer another error first says
    the refresh is running, as before). Stale running rows are marked
    interrupted on the way, like a read does."""
    with _session_factory(factory)() as db:
        _expire(db, sid)
        db.commit()
        started = db.execute(
            select(Row.started_at).where(
                Row.session_id == sid, Row.scope == scope, Row.state == RUNNING
            )
        ).scalar_one_or_none()
        if started is not None:
            raise RefreshRunning(scope, started)
        blocker = _blocker(db, sid, scope)
    if blocker is not None:
        raise blocker


# ── the running refresh ──────────────────────────────────────────────────


class RefreshRun:
    """The progress writer of one claimed refresh. Use it as an async
    context manager around the refresh: it beats the heartbeat while the
    body runs and writes the end state when it leaves (finished; failed on an
    exception, with the exception's type name; interrupted when cancelled)."""

    def __init__(
        self,
        sid: str,
        scope: str,
        run_id: uuid.UUID,
        detail: dict[str, Any],
        *,
        factory: Factory | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.sid = sid
        self.scope = scope
        self.run_id = run_id
        self.detail = detail
        self._factory = factory
        self._clock = clock
        self._lost = False
        self._finishing = False
        # A provider refresh leaves its station-module follow-up in the
        # record when it ends (`_extract_on_finish`); a caller that queues no
        # pass (a derived provider alone) turns it off.
        self.follow_up = scope != OSM
        self._beat_task: asyncio.Task[None] | None = None
        # Inside `async with`: progress writes go to one thread of their own,
        # in order, so the event loop never waits on the database.
        self._writer: ThreadPoolExecutor | None = None
        self._last_bytes_write = 0.0
        self._last_step = 0

    @property
    def lost(self) -> bool:
        """True once a write found the row taken over by another refresh."""
        return self._lost

    # ── writes ──

    def _fenced(self) -> Any:
        return update(Row).where(
            Row.session_id == self.sid,
            Row.scope == self.scope,
            Row.run_id == self.run_id,
            Row.state == RUNNING,
        )

    def _execute(self, statement: Any, *, beat: bool = False) -> None:
        try:
            with _session_factory(self._factory)() as db:
                changed = db.execute(statement).rowcount
                db.commit()
        except Exception as exc:  # the record is best effort, the refresh is not
            log.warning(_FAILED_WRITE, self.scope, type(exc).__name__)
            return
        # A beat that lands after this refresh's own end found the row
        # finished: not a lost lease.
        if changed == 0 and not self._lost and not (beat and self._finishing):
            self._lost = True
            log.warning("refresh_progress.lease_lost scope=%s", self.scope)

    def _write(self) -> None:
        statement = self._fenced().values(
            detail=copy.deepcopy(self.detail), heartbeat_at=func.now()
        )
        if self._writer is not None:
            self._writer.submit(self._execute, statement)
        else:
            self._execute(statement)

    def beat(self) -> None:
        if self._finishing:
            return
        self._execute(self._fenced().values(heartbeat_at=func.now()), beat=True)

    # ── progress ──

    @contextlib.contextmanager
    def task(self, label: str) -> Iterator[None]:
        """One download: the label is shown as the current task, the
        download's bytes reach `bytes_received` (feed_fetch.download_progress)
        and the steps after it reach `step` (feed_fetch.task_step)."""
        self.detail["current"] = [
            {
                "key": _label(label),
                # None until the first chunk: a NAP lookup may come first.
                "bytes": None,
                "expected": None,
                "step": feed_fetch.STEP_DOWNLOADING,
                "scanned": None,
                "scan_total": None,
                # The web process's clock, like the reader's (`_public_current`).
                "started_at": _now_iso(),
            }
        ]
        self._restart_throttle()
        self._write()
        bytes_token = feed_fetch.download_progress.set(self.bytes_received)
        step_token = feed_fetch.task_step.set(self.step)
        try:
            yield
        finally:
            feed_fetch.task_step.reset(step_token)
            feed_fetch.download_progress.reset(bytes_token)

    def _restart_throttle(self) -> None:
        self._last_bytes_write = self._clock()
        self._last_step = 0

    def _throttled_write(self, done: int, total: int | None) -> None:
        """Write at most every `BYTES_EVERY_SECONDS`, or when another
        `BYTES_STEP_PERCENT` % of `total` is done."""
        now = self._clock()
        step = 0
        if total:
            percent = min(100, done * 100 // total)
            step = percent - percent % BYTES_STEP_PERCENT
        if now - self._last_bytes_write >= BYTES_EVERY_SECONDS or step > self._last_step:
            self._last_bytes_write = now
            self._last_step = max(step, self._last_step)
            self._write()

    def bytes_received(self, received: int, expected: int | None) -> None:
        """Called by the download after each chunk: written at most every
        `BYTES_EVERY_SECONDS`, or when another `BYTES_STEP_PERCENT` % of the
        expected size has arrived."""
        current = self.detail.get("current") or []
        if not current:
            return
        current[0]["bytes"] = received
        current[0]["expected"] = expected
        self._throttled_write(received, expected)

    def step(self, name: str, done: int | None = None, total: int | None = None) -> None:
        """The current task moved to step `name` (feed_fetch.STEPS), or
        reports how far that step got (`done` of `total` bytes scanned, the
        structure check). A new step is written at once; its progress like the
        bytes. May run on a worker thread: the event loop leaves the record
        to the task while the step runs, and writes go to the writer thread."""
        current = self.detail.get("current") or []
        if self._finishing or not current or name not in feed_fetch.STEPS:
            # Finishing: a copy job outliving its cancelled request writes nothing.
            return
        item = current[0]
        item["scanned"] = done
        item["scan_total"] = total
        if item.get("step") != name:
            item["step"] = name
            self._restart_throttle()
            self._write()
        elif done is not None:
            self._throttled_write(done, total)

    def done(self, label: str, outcome: dict[str, Any]) -> None:
        """A task ended: `fetched`, `unchanged`, or anything else (the
        refresh's `skipped`: the slot keeps its previous file) is failed."""
        status = outcome.get("status")
        bucket = status if status in ("fetched", "unchanged") else "failed"
        self.detail[bucket] += 1
        if bucket == "failed" and len(self.detail["failed_keys"]) < FAILED_KEYS_MAX:
            self.detail["failed_keys"].append(_label(label))
        self.detail["done"] += 1
        self.detail["current"] = []
        self._write()

    def phase(self, name: str, label: str | None = None) -> None:
        self.detail["phase"] = name
        self.detail["current"] = (
            [{"key": _label(label), "bytes": None, "expected": None, "started_at": _now_iso()}]
            if label
            else []
        )
        self._write()

    def finish(self, state: str, *, error: str | None = None) -> None:
        self.detail["current"] = []
        self.detail["phase"] = None
        self.detail["not_reached"] = max(0, self.detail["total"] - self.detail["done"])
        if error is not None:
            self.detail["error"] = _word(error.lower())
        values: dict[str, Any] = {
            "state": state,
            "finished_at": func.now(),
            "heartbeat_at": func.now(),
            "detail": copy.deepcopy(self.detail),
        }
        if self.follow_up:
            values["extract"] = _extract_on_finish(state)
        self._execute(self._fenced().values(**values))

    # ── lifetime ──

    async def _beat_forever(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            await asyncio.to_thread(self.beat)

    async def __aenter__(self) -> RefreshRun:
        self._writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="refresh-progress")
        self._beat_task = asyncio.create_task(self._beat_forever())
        return self

    def _end(self, state: str, error: str | None = None) -> None:
        """The queued progress writes first, then the end (one thread)."""
        if self._writer is not None:
            self._writer.shutdown(wait=True)
            self._writer = None
        self.finish(state, error=error)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._finishing = True
        if self._beat_task is not None:
            self._beat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._beat_task
        if exc_type is None:
            state, error = FINISHED, None
        elif issubclass(exc_type, Exception):
            state, error = FAILED, exc_type.__name__
        else:  # cancelled (shutdown, a lost client under some servers)
            state, error = INTERRUPTED, None
        if state == INTERRUPTED:
            self._end(state)  # a cancelled task must not await again
        else:
            await asyncio.to_thread(self._end, state, error)


def _extract_on_finish(state: str) -> dict[str, Any]:
    """The follow-up a provider refresh leaves: queued for the sender thread
    when it finished and the module is configured; off without the module;
    nothing after a refresh that did not finish (no pass is queued)."""
    if state != FINISHED:
        return {}
    from . import station_module

    if not station_module.enabled():
        return {"state": "off"}
    return {"state": "queued", "updated_at": _now_iso()}


# ── the station-module follow-up ─────────────────────────────────────────


class _Outcome(Protocol):
    @property
    def state(self) -> str: ...

    @property
    def reason(self) -> str | None: ...


class ExtractFollowUp:
    """The pass of `app/station_extract_upload.py` that follows a provider
    refresh (the whole session's, `scope` PROVIDERS, or one provider's alone,
    `scope` its feed id), written to that refresh's record: fenced by its
    scope, its `run_id` and by `state = 'finished'` (a newer refresh owns the
    row otherwise). One write
    when the pass starts, one when a feed starts, one per outcome, one at the
    end, and a beat of `updated_at` every `EXTRACT_HEARTBEAT_SECONDS` while it
    runs (an extraction can take many minutes). Never raises."""

    def __init__(
        self,
        sid: str,
        run_id: uuid.UUID,
        *,
        scope: str = PROVIDERS,
        factory: Factory | None = None,
    ) -> None:
        self.sid = sid
        self.run_id = run_id
        self.scope = scope
        self._factory = factory
        self.state: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._beater: threading.Thread | None = None

    def _write(self) -> None:
        """Write the state as it stands; the caller holds `_lock`."""
        self.state["updated_at"] = _now_iso()
        try:
            with _session_factory(self._factory)() as db:
                db.execute(
                    update(Row)
                    .where(
                        Row.session_id == self.sid,
                        Row.scope == self.scope,
                        Row.run_id == self.run_id,
                        Row.state == FINISHED,
                    )
                    .values(extract=copy.deepcopy(self.state))
                )
                db.commit()
        except Exception as exc:
            log.warning(_FAILED_WRITE, "extract", type(exc).__name__)

    def _beat_until_stopped(self) -> None:
        while not self._stop.wait(EXTRACT_HEARTBEAT_SECONDS):
            with self._lock:
                self._write()

    def started(self, total: int) -> None:
        with self._lock:
            self.state = {
                "state": RUNNING,
                "total": total,
                "handled": 0,
                **dict.fromkeys(EXTRACT_STATES, 0),
                "reasons": {},
                "current": None,
                "started_at": _now_iso(),
                "finished_at": None,
            }
            self._write()
        if self._beater is None:
            self._beater = threading.Thread(
                target=self._beat_until_stopped, name="refresh-follow-up", daemon=True
            )
            self._beater.start()

    def feed_started(self, provider_id: str) -> None:
        with self._lock:
            if not self.state:
                return
            self.state["current"] = _label(provider_id)
            self._write()

    def outcome(self, outcome: _Outcome) -> None:
        with self._lock:
            if not self.state:
                return
            state = outcome.state if outcome.state in EXTRACT_STATES else "skipped"
            self.state[state] += 1
            self.state["handled"] += 1
            if outcome.reason and state not in ("sent", "held"):
                reasons = self.state["reasons"]
                word = _word(outcome.reason)
                reasons[word] = reasons.get(word, 0) + 1
            self.state["current"] = None
            self._write()

    def finished(self, end: str) -> None:
        """`end`: finished, cancelled, error (the pass stopped early) or off
        (the module is not configured any more)."""
        if not self.state:
            self.started(0)
        self._stop.set()
        with self._lock:
            self.state["state"] = end if end in _EXTRACT_ENDS else "error"
            self.state["current"] = None
            self.state["finished_at"] = _now_iso()
            self._write()


# ── the read ─────────────────────────────────────────────────────────────

_COUNTS = ("total", "done", "fetched", "unchanged", "failed", "not_reached")
_EXTRACT_COUNTS = ("total", "handled", *EXTRACT_STATES)


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _optional_count(value: object) -> int | None:
    return _count(value) if value is not None else None


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _seconds_since(value: object, now: datetime) -> int | None:
    started = _parse_time(value)
    return max(0, int((now - started).total_seconds())) if started else None


def _public_current(items: object, now: datetime) -> list[dict[str, Any]]:
    """The current tasks: label, download bytes, step (None for a record
    written before steps, or an unknown word), the structure check's bytes,
    and how long the task has run. `now` is the web process's clock, the
    clock `started_at` was written with (never PostgreSQL's)."""
    if not isinstance(items, list):
        return []
    return [
        {
            "key": _label(item.get("key")),
            "bytes": _optional_count(item.get("bytes")),
            "expected": _optional_count(item.get("expected")),
            "step": item.get("step") if item.get("step") in feed_fetch.STEPS else None,
            "scanned": _optional_count(item.get("scanned")),
            "scan_total": _optional_count(item.get("scan_total")),
            "elapsed_seconds": _seconds_since(item.get("started_at"), now),
        }
        for item in items[:4]
        if isinstance(item, dict)
    ]


def _extract_state(extract: dict[str, Any], now: datetime) -> str:
    """The follow-up's state, `interrupted` when it went silent: still
    queued after `EXTRACT_QUEUED_SECONDS`, or running with no beat for
    `EXTRACT_LEASE_SECONDS` (a restart lost the queue, or the pass died)."""
    state = _word(extract.get("state"))
    limit = {"queued": EXTRACT_QUEUED_SECONDS, RUNNING: EXTRACT_LEASE_SECONDS}.get(state)
    if limit is None:
        return state
    updated = _parse_time(extract.get("updated_at"))
    if updated is None or (now - updated).total_seconds() > limit:
        return INTERRUPTED
    return state


def _public_extract(extract: object, now: datetime) -> dict[str, Any] | None:
    if not isinstance(extract, dict) or not extract.get("state"):
        return None
    public: dict[str, Any] = {"state": _extract_state(extract, now)}
    updated = _parse_time(extract.get("updated_at"))
    public["updated_at"] = _iso(updated) if updated else None
    if "total" in extract:
        public.update({name: _count(extract.get(name)) for name in _EXTRACT_COUNTS})
        reasons = extract.get("reasons")
        public["reasons"] = (
            {_word(k): _count(v) for k, v in reasons.items()} if isinstance(reasons, dict) else {}
        )
        current = extract.get("current")
        public["current"] = _label(current) if current else None
        for name in ("started_at", "finished_at"):
            value = extract.get(name)
            public[name] = value if isinstance(value, str) and len(value) <= 40 else None
    return public


def _public(row: Row, now: datetime) -> dict[str, Any]:
    detail = row.detail if isinstance(row.detail, dict) else {}
    end = row.finished_at or now
    single = is_provider_scope(row.scope)
    public: dict[str, Any] = {
        "scope": "provider" if single else row.scope,
        "provider_id": row.scope if single else None,
        "state": row.state,
        "started_at": _iso(row.started_at),
        "finished_at": _iso(row.finished_at),
        "heartbeat_at": _iso(row.heartbeat_at),
        "elapsed_seconds": max(0, int((end - row.started_at).total_seconds())),
        **{name: _count(detail.get(name)) for name in _COUNTS},
        "phase": _word(detail["phase"]) if detail.get("phase") else None,
        # Only a running refresh has a current task: an interrupted record
        # keeps the task it died in, which is no longer "now".
        "current": (
            _public_current(detail.get("current"), datetime.now(UTC))
            if row.state == RUNNING
            else []
        ),
        "failed_keys": [_label(k) for k in (detail.get("failed_keys") or [])][:FAILED_KEYS_MAX],
        "error": _word(detail["error"]) if detail.get("error") else None,
        "previous": None,
        "extract": _public_extract(row.extract, now),
    }
    previous = detail.get("previous")
    if isinstance(previous, dict):
        started = previous.get("started_at")
        public["previous"] = {
            "state": INTERRUPTED,
            "started_at": started if isinstance(started, str) and len(started) <= 40 else None,
        }
    return public


def _follow_up_active(row: Row, now: datetime) -> bool:
    """The record's station-module pass is still queued or running (one that
    went silent reads interrupted and no longer counts)."""
    extract = row.extract if isinstance(row.extract, dict) else {}
    return bool(extract.get("state")) and _extract_state(extract, now) in ("queued", RUNNING)


def _superseded(row: Row, whole: Row | None) -> bool:
    """A whole-session provider refresh that started after `row` and
    finished downloaded its provider too. One that failed or was interrupted
    may never have reached it: it supersedes nothing."""
    return whole is not None and whole.state == FINISHED and whole.started_at > row.started_at


def _singles_shown(rows: list[Row], whole: Row | None, now: datetime) -> list[Row]:
    """The single-provider records the page shows, newest first: every
    running one (at most `SINGLE_SHOWN_MAX`), every ended one whose
    station-module pass is still queued or running (at most as many: the
    page polls until it ends), and the newest ended one unless it is
    superseded (`_superseded`)."""
    newest_first = sorted(rows, key=lambda r: r.started_at, reverse=True)
    running = [r for r in newest_first if r.state == RUNNING][:SINGLE_SHOWN_MAX]
    ended = [r for r in newest_first if r.state != RUNNING]
    kept = [r for r in ended if _follow_up_active(r, now)][:SINGLE_SHOWN_MAX]
    if ended and ended[0] not in kept and not _superseded(ended[0], whole):
        kept.insert(0, ended[0])
    return running + kept


def provider_ids(config: object) -> set[str]:
    """The provider ids of a session's config as saved (no validation): a
    record of a provider removed or renamed since is not shown."""
    sources = config.get("sources") if isinstance(config, dict) else None
    providers = sources.get("providers") if isinstance(sources, dict) else None
    if not isinstance(providers, list):
        return set()
    return {p["id"] for p in providers if isinstance(p, dict) and isinstance(p.get("id"), str)}


def read(
    sid: str, *, providers: set[str] | None = None, factory: Factory | None = None
) -> dict[str, Any]:
    """`{"providers": record | None, "osm": record | None, "single":
    [record, ...], "now": iso}` for the page; `single` holds the records of
    one provider's refresh alone (`_singles_shown`), of the session's
    current `providers` only when given. A running record past its lease is
    first marked interrupted (the process that wrote it is gone), so every
    reader sees the same."""
    with _session_factory(factory)() as db:
        _expire(db, sid)
        db.commit()
        now = db.execute(select(func.now())).scalar_one()
        rows = list(db.execute(select(Row).where(Row.session_id == sid)).scalars())
        kinds = {r.scope: r for r in rows if r.scope in SCOPES}
        singles = [
            r
            for r in rows
            if is_provider_scope(r.scope) and (providers is None or r.scope in providers)
        ]
        result: dict[str, Any] = {
            scope: _public(kinds[scope], now) if scope in kinds else None for scope in SCOPES
        }
        shown = _singles_shown(singles, kinds.get(PROVIDERS), now)
        result["single"] = [_public(r, now) for r in shown]
    result["now"] = _iso(now)
    return result
