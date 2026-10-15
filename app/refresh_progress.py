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
nothing any more; it logs one WARNING and finishes its downloads as before.

**What is written** (`detail`): the counts (`total`, `done`, `fetched`,
`unchanged`, `failed`, `not_reached`), the phase, the task being downloaded
with its bytes received and expected (when the server announced a length), and
the labels of the failed tasks. Bytes are written at most every
`BYTES_EVERY_SECONDS` or every `BYTES_STEP_PERCENT` % of a feed, never once
per chunk. `extract`: the station-module follow-up of a provider refresh
(`ExtractFollowUp`, fed by the sender thread of `app/station_extract_upload.py`):
queued, then running with its counts per outcome and reason word, then
finished; `off` when the module is not configured.

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
import logging
import re
import time
import uuid
from collections.abc import Callable, Iterator
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


def _word(value: object) -> str:
    return value if isinstance(value, str) and _WORD.fullmatch(value) else "other"


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
    """A refresh of this kind already runs for the session (HTTP 409)."""

    def __init__(self, scope: str, started_at: datetime | None) -> None:
        super().__init__(f"a {scope} refresh is already running")
        self.scope = scope
        self.started_at = started_at

    def detail(self) -> dict[str, Any]:
        started = _iso(self.started_at)
        what = "An OSM refresh" if self.scope == OSM else "A provider refresh"
        since = f" (started at {started})" if started else ""
        return {
            "error": "refresh_already_running",
            "scope": self.scope,
            "started_at": started,
            "message": (
                f"{what} of this session is already running{since}. Its progress "
                "is shown on the page; start a new one when it has ended."
            ),
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


def claim(sid: str, scope: str, total: int, *, factory: Factory | None = None) -> RefreshRun:
    """Make this request the session's running refresh of `scope`, or raise
    `RefreshRunning`. One transaction: a stale running row (no heartbeat for
    `LEASE_SECONDS`) is first marked interrupted, then the row is taken only
    when it is not running; a concurrent claim waits on the row lock and then
    finds it running. A replaced interrupted refresh is named in the new
    record (`previous`)."""
    run_id = uuid.uuid4()
    with _session_factory(factory)() as db:
        _expire(db, sid, scope)
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
        self._beat_task: asyncio.Task[None] | None = None
        self._last_bytes_write = 0.0
        self._last_step = 0

    # ── writes ──

    def _fenced(self) -> Any:
        return update(Row).where(
            Row.session_id == self.sid,
            Row.scope == self.scope,
            Row.run_id == self.run_id,
            Row.state == RUNNING,
        )

    def _execute(self, statement: Any) -> None:
        try:
            with _session_factory(self._factory)() as db:
                changed = db.execute(statement).rowcount
                db.commit()
        except Exception as exc:  # the record is best effort, the refresh is not
            log.warning(_FAILED_WRITE, self.scope, type(exc).__name__)
            return
        if changed == 0 and not self._lost:
            self._lost = True
            log.warning("refresh_progress.lease_lost scope=%s", self.scope)

    def _write(self) -> None:
        self._execute(self._fenced().values(detail=dict(self.detail), heartbeat_at=func.now()))

    def beat(self) -> None:
        self._execute(self._fenced().values(heartbeat_at=func.now()))

    # ── progress ──

    @contextlib.contextmanager
    def task(self, label: str) -> Iterator[None]:
        """One download: the label is shown as the current task and the
        download's bytes reach `bytes_received` (feed_fetch.download_progress)."""
        self.detail["current"] = [{"key": _label(label), "bytes": 0, "expected": None}]
        self._last_bytes_write = self._clock()
        self._last_step = 0
        self._write()
        token = feed_fetch.download_progress.set(self.bytes_received)
        try:
            yield
        finally:
            feed_fetch.download_progress.reset(token)

    def bytes_received(self, received: int, expected: int | None) -> None:
        """Called by the download after each chunk: written at most every
        `BYTES_EVERY_SECONDS`, or when another `BYTES_STEP_PERCENT` % of the
        expected size has arrived."""
        current = self.detail.get("current") or []
        if not current:
            return
        current[0]["bytes"] = received
        current[0]["expected"] = expected
        now = self._clock()
        step = 0
        if expected:
            percent = min(100, received * 100 // expected)
            step = percent - percent % BYTES_STEP_PERCENT
        if now - self._last_bytes_write >= BYTES_EVERY_SECONDS or step > self._last_step:
            self._last_bytes_write = now
            self._last_step = max(step, self._last_step)
            self._write()

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
            [{"key": _label(label), "bytes": None, "expected": None}] if label else []
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
            "detail": dict(self.detail),
        }
        if self.scope == PROVIDERS:
            values["extract"] = _extract_on_finish(state)
        self._execute(self._fenced().values(**values))

    # ── lifetime ──

    async def _beat_forever(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            await asyncio.to_thread(self.beat)

    async def __aenter__(self) -> RefreshRun:
        self._beat_task = asyncio.create_task(self._beat_forever())
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._beat_task is not None:
            self._beat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._beat_task
        if exc_type is None:
            self.finish(FINISHED)
        elif issubclass(exc_type, Exception):
            self.finish(FAILED, error=exc_type.__name__)
        else:  # cancelled (shutdown, a lost client under some servers)
            self.finish(INTERRUPTED)


def _extract_on_finish(state: str) -> dict[str, Any]:
    """The follow-up a provider refresh leaves: queued for the sender thread
    when it finished and the module is configured; off without the module;
    nothing after a refresh that did not finish (no pass is queued)."""
    if state != FINISHED:
        return {}
    from . import station_module

    return {"state": "queued"} if station_module.enabled() else {"state": "off"}


# ── the station-module follow-up ─────────────────────────────────────────


class _Outcome(Protocol):
    @property
    def state(self) -> str: ...

    @property
    def reason(self) -> str | None: ...


class ExtractFollowUp:
    """The pass of `app/station_extract_upload.py` that follows a provider
    refresh, written to the session's latest provider record (fenced by its
    `run_id`). One write when the pass starts, one when a feed starts, one per
    outcome, one at the end. Never raises."""

    def __init__(self, sid: str, run_id: uuid.UUID, *, factory: Factory | None = None) -> None:
        self.sid = sid
        self.run_id = run_id
        self._factory = factory
        self.state: dict[str, Any] = {}

    @classmethod
    def for_session(cls, sid: str, *, factory: Factory | None = None) -> ExtractFollowUp | None:
        """The follow-up of the session's latest provider refresh, or None
        when there is none (or the record cannot be read)."""
        try:
            with _session_factory(factory)() as db:
                run_id = db.execute(
                    select(Row.run_id).where(Row.session_id == sid, Row.scope == PROVIDERS)
                ).scalar_one_or_none()
        except Exception as exc:
            log.warning(_FAILED_WRITE, "extract", type(exc).__name__)
            return None
        return cls(sid, run_id, factory=factory) if run_id is not None else None

    def _write(self) -> None:
        try:
            with _session_factory(self._factory)() as db:
                db.execute(
                    update(Row)
                    .where(
                        Row.session_id == self.sid,
                        Row.scope == PROVIDERS,
                        Row.run_id == self.run_id,
                    )
                    .values(extract=dict(self.state))
                )
                db.commit()
        except Exception as exc:
            log.warning(_FAILED_WRITE, "extract", type(exc).__name__)

    def started(self, total: int) -> None:
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

    def feed_started(self, provider_id: str) -> None:
        if not self.state:
            return
        self.state["current"] = _label(provider_id)
        self._write()

    def outcome(self, outcome: _Outcome) -> None:
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


def _public_current(items: object) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    return [
        {
            "key": _label(item.get("key")),
            "bytes": _optional_count(item.get("bytes")),
            "expected": _optional_count(item.get("expected")),
        }
        for item in items[:4]
        if isinstance(item, dict)
    ]


def _public_extract(extract: object) -> dict[str, Any] | None:
    if not isinstance(extract, dict) or not extract.get("state"):
        return None
    public: dict[str, Any] = {"state": _word(extract.get("state"))}
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
    public: dict[str, Any] = {
        "scope": row.scope,
        "state": row.state,
        "started_at": _iso(row.started_at),
        "finished_at": _iso(row.finished_at),
        "heartbeat_at": _iso(row.heartbeat_at),
        "elapsed_seconds": max(0, int((end - row.started_at).total_seconds())),
        **{name: _count(detail.get(name)) for name in _COUNTS},
        "phase": _word(detail["phase"]) if detail.get("phase") else None,
        "current": _public_current(detail.get("current")),
        "failed_keys": [_label(k) for k in (detail.get("failed_keys") or [])][:FAILED_KEYS_MAX],
        "error": _word(detail["error"]) if detail.get("error") else None,
        "previous": None,
        "extract": _public_extract(row.extract),
    }
    previous = detail.get("previous")
    if isinstance(previous, dict):
        started = previous.get("started_at")
        public["previous"] = {
            "state": INTERRUPTED,
            "started_at": started if isinstance(started, str) and len(started) <= 40 else None,
        }
    return public


def read(sid: str, *, factory: Factory | None = None) -> dict[str, Any]:
    """`{"providers": record | None, "osm": record | None, "now": iso}` for
    the page. A running record past its lease is first marked interrupted
    (the process that wrote it is gone), so every reader sees the same."""
    with _session_factory(factory)() as db:
        _expire(db, sid)
        db.commit()
        now = db.execute(select(func.now())).scalar_one()
        rows = {r.scope: r for r in db.execute(select(Row).where(Row.session_id == sid)).scalars()}
        result: dict[str, Any] = {
            scope: _public(rows[scope], now) if scope in rows else None for scope in SCOPES
        }
    result["now"] = _iso(now)
    return result
