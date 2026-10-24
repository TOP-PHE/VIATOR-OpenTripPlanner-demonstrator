"""The coverage executor: claims coverage runs and executes them outside web
(issue #392, durable part).

Before #392 a run was a FastAPI background task in the web process, so any
restart of web (a deploy, autoheal, the unexplained restart of 22 Oct) ended
it, and web's startup marked it failed. A full run takes many hours, so a run
rarely survived. Now:

- web only creates the run row (`pending`) and returns; Stop writes
  `cancel_requested_at` (or cancels a run still queued);
- this executor, a thread with its own event loop inside the worker process
  (`python -m app.worker`, started by `worker.main`), polls for work and
  claims a run atomically: `SELECT ... FOR UPDATE SKIP LOCKED` on the oldest
  claimable row, then `claimed_by` / `heartbeat_at` set in the same
  transaction. Claimable means `pending`, or `running` and paused: no
  holder (released at a graceful shutdown, or a run left by web before this
  change) or a heartbeat older than `STALE_AFTER_SECONDS` (its executor
  died). An executor never claims a run it is executing itself;
- while it executes a run (`runner.execute_run`, unchanged pair logic and
  knobs: COVERAGE_PAIR_PARALLELISM, COVERAGE_WITHIN_PAIR_PARALLELISM) it
  refreshes `heartbeat_at` every `HEARTBEAT_SECONDS` (the database's clock).
  If the heartbeat finds the run held by another executor, the run stops in
  this process with no terminal write (`runner.abandon_run`);
- a `running` run claimed again is a **resume**: `resume_count` + 1 and a
  line in `summary.resumes`; the runner skips the pairs that already have a
  result row (one transaction writes a pair's journey search, its result
  row and its counter increments, and the (run, origin, dest) unique
  constraint refuses a second row), so the counters stay exact. A resume
  after a release is a `release` resume and costs nothing; one after a
  stale heartbeat (a crash, a kill, a run left for a resume after a
  database error) is a `crash` resume, and past COVERAGE_MAX_RESUMES of
  those the run is marked failed with `summary.resume_limit_reached`
  instead (a run that keeps killing its worker must not loop forever);
- at SIGTERM (a deploy recreates the worker) the worker releases its claims
  (`release_claims`: no holder, `heartbeat_at` = the release time) so the
  next executor resumes at once instead of waiting for the heartbeat to go
  stale;
- a Stop clicked while no executor holds the run ends it `cancelled` at
  once (`cancel_unheld_run`, called by web's Stop; and at the claim).

Web never fails a run at startup any more: the executor decides. The page
reads everything from the database (`display_status`: queued, resuming,
stopping). Prometheus reads the claim state from web's DB collector
(`viator_coverage_*`, app/metrics.py) and alerts on a stale heartbeat
(docker/prometheus/rules/coverage.yml).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import threading
import uuid
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import case, func, select, update
from sqlalchemy.orm import Session as DbSession

from .. import config_service
from ..db import SessionLocal
from ..journey import motis_client
from ..models import NetworkCoverageResult, NetworkCoverageRun
from . import runner

log = logging.getLogger(__name__)

# The holder refreshes its heartbeat every 30 s; a run whose heartbeat is
# older than 3 minutes (six missed beats) is taken as abandoned and resumed.
# A worker restart without a graceful release (a crash, a kill) therefore
# pauses a run for about 3 minutes plus a poll. The Prometheus alert
# CoverageRunHeartbeatStale fires at 15 minutes (rules/coverage.yml).
HEARTBEAT_SECONDS = 30.0
STALE_AFTER_SECONDS = 180.0
POLL_SECONDS = 10.0

DEFAULT_MAX_CONCURRENT_RUNS = 1
DEFAULT_MAX_RESUMES = 5

# Bound of the release at SIGTERM: Docker force-kills 10 s after it.
RELEASE_TIMEOUT_SECONDS = 3.0


def new_executor_id() -> str:
    """`<host>:<pid>:<random>`: unique per process start, readable in a log
    line and in `claimed_by` (a container's host name is its id)."""
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"[:128]


@dataclass(frozen=True)
class Claim:
    """One run this executor now holds. `crash`: a resume after a stale
    heartbeat (its previous executor did not release it)."""

    run_id: uuid.UUID
    resumed: bool
    resume_count: int
    crash: bool = False


@dataclass(frozen=True)
class Limits:
    """The executor's two knobs, read from platform_config at each poll."""

    max_concurrent_runs: int = DEFAULT_MAX_CONCURRENT_RUNS
    max_resumes: int = DEFAULT_MAX_RESUMES


def read_limits(db: DbSession) -> Limits:
    cfg = config_service.get_all(db)
    return Limits(
        max_concurrent_runs=int(
            cfg.get("COVERAGE_MAX_CONCURRENT_RUNS", DEFAULT_MAX_CONCURRENT_RUNS)
        ),
        max_resumes=int(cfg.get("COVERAGE_MAX_RESUMES", DEFAULT_MAX_RESUMES)),
    )


def _unheld(stale_after_s: float) -> Any:
    """A `running` run no live executor holds: released (no holder), or
    its holder's heartbeat is missing or older than `stale_after_s`."""
    stale_before = func.now() - timedelta(seconds=stale_after_s)
    return (NetworkCoverageRun.status == "running") & (
        NetworkCoverageRun.claimed_by.is_(None)
        | NetworkCoverageRun.heartbeat_at.is_(None)
        | (NetworkCoverageRun.heartbeat_at < stale_before)
    )


def _claimable(stale_after_s: float, exclude: Collection[uuid.UUID] = ()) -> Any:
    claimable = (NetworkCoverageRun.status == "pending") | _unheld(stale_after_s)
    if exclude:
        # The runs this executor is executing itself: its own heartbeat may
        # be late (a DB outage), but the run is not abandoned.
        claimable = claimable & NetworkCoverageRun.id.notin_(list(exclude))
    return claimable


def select_claimable(
    db: DbSession, stale_after_s: float, exclude: Collection[uuid.UUID] = ()
) -> NetworkCoverageRun | None:
    """The next run to take, row-locked (FOR UPDATE SKIP LOCKED): a resume
    first, then the oldest queued run, never one of `exclude`. A row another
    executor is claiming is skipped, never waited for, and Postgres
    re-checks the condition on the locked row, so a heartbeat written
    meanwhile makes it unclaimable."""
    return db.execute(
        select(NetworkCoverageRun)
        .where(_claimable(stale_after_s, exclude))
        .order_by(
            case((NetworkCoverageRun.status == "running", 0), else_=1),
            NetworkCoverageRun.started_at,
        )
        .limit(1)
        .with_for_update(skip_locked=True)
    ).scalar_one_or_none()


def _heartbeat_age_s(run: NetworkCoverageRun, now: datetime) -> float | None:
    if run.heartbeat_at is None:
        return None
    return max(0.0, (now - run.heartbeat_at).total_seconds())


def _crash_resume_count(summary: Any) -> int:
    value = summary.get("crash_resume_count") if isinstance(summary, dict) else None
    return value if isinstance(value, int) else 0


def _fail_resume_limit(run: NetworkCoverageRun, now: datetime, max_resumes: int) -> None:
    summary = run.summary if isinstance(run.summary, dict) else {}
    reason = (
        f"the coverage executor stopped without releasing the run "
        f"{_crash_resume_count(summary) + 1} times (COVERAGE_MAX_RESUMES = {max_resumes}); "
        "marked failed instead of resumed again"
    )
    missing = summary.get("missing_pairs")
    if isinstance(missing, int) and missing > 0:
        reason += f"; {missing} pairs still had no result row"
    last = summary.get("last_interruption")
    if isinstance(last, dict) and last.get("reason"):
        reason += f"; last interruption: {last['reason']}"
    run.status = "failed"
    run.finished_at = now
    run.claimed_by = None
    run.summary = {
        **summary,
        "resume_limit_reached": True,
        "failure_reason": reason[:500],
        "failed_at": now.isoformat(),
        "completed_pairs_at_failure": run.completed_pairs or 0,
    }


def _record_resume(run: NetworkCoverageRun, now: datetime, *, crash: bool) -> None:
    previous = run.summary if isinstance(run.summary, dict) else {}
    resumes = list(previous.get("resumes") or [])
    resumes.append(
        {
            "resumed_at": now.isoformat(),
            "kind": "crash" if crash else "release",
            "previous_executor": run.claimed_by,
            "last_heartbeat_at": run.heartbeat_at.isoformat() if run.heartbeat_at else None,
            "completed_pairs": run.completed_pairs or 0,
        }
    )
    run.resume_count = (run.resume_count or 0) + 1
    crashes = _crash_resume_count(previous) + (1 if crash else 0)
    run.summary = {
        **previous,
        "resume_count": run.resume_count,
        "crash_resume_count": crashes,
        "release_resume_count": run.resume_count - crashes,
        "resumes": resumes,
    }


def cancel_on_row(db: DbSession, run: NetworkCoverageRun, now: datetime) -> None:
    """End a run no executor holds as `cancelled` (its Stop was clicked):
    the counters recounted from its result rows, the summary marked as the
    runner's Stop marks it. The caller holds the row lock and commits."""
    statuses = db.execute(
        select(NetworkCoverageResult.status).where(NetworkCoverageResult.run_id == run.id)
    ).scalars()
    runner._apply_counters(run, list(statuses))
    started = run.started_at
    run.status = "cancelled"
    run.finished_at = now
    run.claimed_by = None
    run.summary = {
        **(run.summary if isinstance(run.summary, dict) else {}),
        "elapsed_seconds": (now - started).total_seconds() if started else 0.0,
        "cancelled_by_operator": True,
        "cancelled_at": now.isoformat(),
        "cancelled_while_paused": True,
    }


def cancel_unheld_run(
    db: DbSession, run_id: uuid.UUID, stale_after_s: float = STALE_AFTER_SECONDS
) -> bool:
    """Web's Stop on a running run that no live executor holds (paused by
    a deploy, or its worker is down): end it `cancelled` now instead of
    showing "stopping" until a worker comes back. The row lock and the
    re-checked condition make it exclusive with a claim: a run an executor
    claimed meanwhile is left to that executor (it reads the Stop click).
    True when it was cancelled here."""
    run = db.execute(
        select(NetworkCoverageRun)
        .where(NetworkCoverageRun.id == run_id, _unheld(stale_after_s))
        .with_for_update()
    ).scalar_one_or_none()
    if run is None:
        db.rollback()
        return False
    cancelled_id = run.id  # the row's id, not the request's value (log injection)
    cancel_on_row(db, run, datetime.now(UTC))
    db.commit()
    log.warning("coverage_executor.cancelled_while_paused run_id=%s", cancelled_id)
    return True


def _settle_instead_of_claim(
    db: DbSession, run: NetworkCoverageRun, now: datetime, max_resumes: int
) -> bool:
    """A paused run that must not be resumed: its Stop was clicked (it ends
    `cancelled`), or it is past its crash-resume limit (it ends `failed`
    with the reason). True when the run was settled and committed."""
    if run.cancel_requested_at is not None:
        cancel_on_row(db, run, now)
        db.commit()
        log.warning("coverage_executor.cancelled_while_paused run_id=%s", run.id)
        return True
    crashes = _crash_resume_count(run.summary)
    if run.claimed_by is None or crashes < max_resumes:
        return False
    run_id = run.id
    _fail_resume_limit(run, now, max_resumes)
    db.commit()
    log.warning(
        "coverage_executor.resume_limit_reached run_id=%s crash_resumes=%d limit=%d",
        run_id,
        crashes,
        max_resumes,
    )
    return True


def _take(run: NetworkCoverageRun, now: datetime, executor_id: str) -> Claim:
    """Make `executor_id` the run's holder (a queued run starts, a paused
    one is resumed). The caller commits."""
    resumed = run.status == "running"
    crash = resumed and run.claimed_by is not None
    if resumed:
        _record_resume(run, now, crash=crash)
    else:
        run.status = "running"
        run.started_at = now
    run.claimed_by = executor_id
    run.heartbeat_at = func.now()
    return Claim(run_id=run.id, resumed=resumed, resume_count=run.resume_count or 0, crash=crash)


def _log_claim(claim: Claim, age: float | None, executor_id: str) -> None:
    if not claim.resumed:
        log.info("coverage_executor.claimed run_id=%s executor=%s", claim.run_id, executor_id)
        return
    log.warning(
        "coverage_executor.resumed run_id=%s kind=%s resume_count=%d heartbeat_age_s=%s "
        "executor=%s",
        claim.run_id,
        "crash" if claim.crash else "release",
        claim.resume_count,
        "none" if age is None else f"{age:.0f}",
        executor_id,
    )


def claim_next_run(
    db: DbSession,
    *,
    executor_id: str,
    max_resumes: int = DEFAULT_MAX_RESUMES,
    stale_after_s: float = STALE_AFTER_SECONDS,
    exclude: Collection[uuid.UUID] = (),
) -> Claim | None:
    """Claim the next run for `executor_id`, or None when there is none.

    One transaction per run looked at: the row is locked by
    `select_claimable`, updated, committed. A paused run whose Stop was
    clicked is cancelled, and one past its crash-resume limit is marked
    failed (with the reason); then the next one is looked at. `exclude`:
    the runs this executor is executing (never claimed again by it)."""
    while True:
        run = select_claimable(db, stale_after_s, exclude)
        if run is None:
            db.rollback()
            return None
        now = datetime.now(UTC)
        if run.status == "running" and _settle_instead_of_claim(db, run, now, max_resumes):
            continue
        age = _heartbeat_age_s(run, now)
        claim = _take(run, now, executor_id)
        db.commit()
        _log_claim(claim, age, executor_id)
        return claim


def heartbeat(db: DbSession, run_id: uuid.UUID, executor_id: str) -> bool:
    """Refresh the run's heartbeat while `executor_id` holds it and it is
    running. False when it no longer does (finished, or taken over)."""
    beat = db.execute(
        update(NetworkCoverageRun)
        .where(
            NetworkCoverageRun.id == run_id,
            NetworkCoverageRun.claimed_by == executor_id,
            NetworkCoverageRun.status == "running",
        )
        .values(heartbeat_at=func.now())
        .returning(NetworkCoverageRun.id)
    ).scalar_one_or_none()
    db.commit()
    return beat is not None


def claim_taken_over(db: DbSession, run_id: uuid.UUID, executor_id: str) -> bool:
    """True when the run is still running but another executor holds it."""
    row = db.execute(
        select(NetworkCoverageRun.status, NetworkCoverageRun.claimed_by).where(
            NetworkCoverageRun.id == run_id
        )
    ).one_or_none()
    return row is not None and row[0] == "running" and row[1] != executor_id


def release_claims(db: DbSession, executor_id: str) -> list[uuid.UUID]:
    """Release every running run `executor_id` holds (graceful shutdown):
    no holder, so the next executor resumes it at its first poll, and
    `heartbeat_at` set to the release time, so the heartbeat-age gauge and
    its alert count from the release, not from the run's start. The run
    stays `running`; it is paused, not failed, and the resume after a
    release does not count toward COVERAGE_MAX_RESUMES."""
    released = list(
        db.execute(
            update(NetworkCoverageRun)
            .where(
                NetworkCoverageRun.claimed_by == executor_id,
                NetworkCoverageRun.status == "running",
            )
            .values(claimed_by=None, heartbeat_at=func.now())
            .returning(NetworkCoverageRun.id)
        ).scalars()
    )
    db.commit()
    return released


def settle_unfinished(db: DbSession, run_id: uuid.UUID, executor_id: str) -> bool:
    """After `execute_run` returned: a run this executor still holds that
    is still `running` reached no terminal state (no pairs to run, or an
    error before the pair loop). Mark it failed with the reason rather than
    resuming it forever. True when it did."""
    now = datetime.now(UTC)
    run = db.execute(
        select(NetworkCoverageRun)
        .where(
            NetworkCoverageRun.id == run_id,
            NetworkCoverageRun.claimed_by == executor_id,
            NetworkCoverageRun.status == "running",
        )
        .with_for_update()
    ).scalar_one_or_none()
    if run is None:
        db.rollback()
        return False
    run.status = "failed"
    run.finished_at = now
    run.summary = {
        **(run.summary or {}),
        "failure_reason": "the run ended without a result state (see the worker log)",
        "failed_at": now.isoformat(),
    }
    db.commit()
    log.warning("coverage_executor.unfinished_marked_failed run_id=%s", run_id)
    return True


def note_interruption(db: DbSession, run_id: uuid.UUID, executor_id: str, reason: str) -> None:
    """Record on the run why this execution left it for a resume
    (`summary.last_interruption`, named in the failure reason if the resume
    limit is reached). Only while `executor_id` holds the run."""
    run = db.execute(
        select(NetworkCoverageRun)
        .where(NetworkCoverageRun.id == run_id, NetworkCoverageRun.claimed_by == executor_id)
        .with_for_update()
    ).scalar_one_or_none()
    if run is None:
        db.rollback()
        return
    run.summary = {
        **(run.summary if isinstance(run.summary, dict) else {}),
        "last_interruption": {"at": datetime.now(UTC).isoformat(), "reason": reason[:300]},
    }
    db.commit()


class CoverageExecutor:
    """Polls, claims and executes coverage runs on its own event loop.

    `serve()` is the loop (run it with `asyncio.run`, or `start()` for a
    daemon thread); `poll_once()` is one step, for tests. The blocking DB
    calls of the executor itself go through `asyncio.to_thread`, so the
    heartbeat stays on time while pairs are in flight."""

    def __init__(
        self,
        executor_id: str | None = None,
        *,
        poll_s: float = POLL_SECONDS,
        heartbeat_s: float = HEARTBEAT_SECONDS,
        stale_after_s: float = STALE_AFTER_SECONDS,
        session_factory: Callable[[], DbSession] = SessionLocal,
    ) -> None:
        self.executor_id = executor_id or new_executor_id()
        self.poll_s = poll_s
        self.heartbeat_s = heartbeat_s
        self.stale_after_s = stale_after_s
        self._sessions = session_factory
        self._active: dict[uuid.UUID, asyncio.Task[None]] = {}
        self._stop: asyncio.Event | None = None

    @property
    def active_run_ids(self) -> list[uuid.UUID]:
        return list(self._active)

    def _db(self, fn: Callable[..., Any], *args: Any) -> Any:
        with self._sessions() as db:
            return fn(db, *args)

    async def _call(self, fn: Callable[..., Any], *args: Any) -> Any:
        return await asyncio.to_thread(self._db, fn, *args)

    async def poll_once(self) -> Claim | None:
        """Claim one run if this executor has room, and start it. A run this
        executor is executing is never claimed again by it, even when its
        own heartbeat is late (with COVERAGE_MAX_CONCURRENT_RUNS >= 2)."""
        limits: Limits = await self._call(read_limits)
        if len(self._active) >= limits.max_concurrent_runs:
            return None
        claim: Claim | None = await asyncio.to_thread(self._claim, limits.max_resumes)
        if claim is not None:
            self._active[claim.run_id] = asyncio.create_task(self._execute(claim))
        return claim

    def _claim(self, max_resumes: int) -> Claim | None:
        with self._sessions() as db:
            return claim_next_run(
                db,
                executor_id=self.executor_id,
                max_resumes=max_resumes,
                stale_after_s=self.stale_after_s,
                exclude=frozenset(self._active),
            )

    async def _execute(self, claim: Claim) -> None:
        beat = asyncio.create_task(self._heartbeat_loop(claim.run_id))
        try:
            await runner.execute_run(claim.run_id, executor_id=self.executor_id)
        except asyncio.CancelledError:
            # The executor itself is stopping: the run stays `running` and is
            # resumed by the next executor. Never settle it as failed.
            beat.cancel()
            self._active.pop(claim.run_id, None)
            raise
        except runner.RunNeedsResume as exc:
            # Not failed: left `running` and claimed, with no heartbeat any
            # more; an executor resumes it once the heartbeat is stale (a
            # crash resume, so a run that never gets through is bounded).
            beat.cancel()
            await self._leave_for_resume(claim.run_id, str(exc))
            return
        except Exception:
            log.exception("coverage_executor.run_crashed run_id=%s", claim.run_id)
        beat.cancel()
        try:
            await self._call(settle_unfinished, claim.run_id, self.executor_id)
        except Exception:
            log.exception("coverage_executor.settle_failed run_id=%s", claim.run_id)
        self._active.pop(claim.run_id, None)

    async def _leave_for_resume(self, run_id: uuid.UUID, reason: str) -> None:
        log.warning(
            "coverage_executor.left_for_resume run_id=%s reason=%s (resumed once its "
            "heartbeat is %.0f s old)",
            run_id,
            reason,
            self.stale_after_s,
        )
        try:
            await self._call(note_interruption, run_id, self.executor_id, reason)
        except Exception:
            log.warning("coverage_executor.note_failed run_id=%s", run_id, exc_info=True)
        self._active.pop(run_id, None)

    async def _heartbeat_loop(self, run_id: uuid.UUID) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_s)
            try:
                held = await self._call(heartbeat, run_id, self.executor_id)
                if not held and await self._call(claim_taken_over, run_id, self.executor_id):
                    log.warning(
                        "coverage_executor.claim_lost run_id=%s executor=%s",
                        run_id,
                        self.executor_id,
                    )
                    runner.abandon_run(run_id)
                    return
            except Exception:
                # A DB blip: the next beat retries; six missed beats make
                # the run stale, which is the intended outcome if it lasts.
                log.warning("coverage_executor.heartbeat_failed run_id=%s", run_id, exc_info=True)

    async def serve(self) -> None:
        """Poll until `stop()`; a failing poll (DB down, a migration not yet
        applied at a deploy) is logged and retried at the next one."""
        self._stop = asyncio.Event()
        log.info(
            "coverage_executor.started executor=%s poll_s=%s heartbeat_s=%s stale_after_s=%s",
            self.executor_id,
            self.poll_s,
            self.heartbeat_s,
            self.stale_after_s,
        )
        while not self._stop.is_set():
            try:
                await self.poll_once()
            except Exception:
                log.warning("coverage_executor.poll_failed", exc_info=True)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_s)
        for task in self._active.values():
            task.cancel()  # a task leaves `_active` only when it next runs

    def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()

    async def _serve_then_close(self) -> None:
        """`serve()`, then the loop's shared MOTIS client is closed (#397)."""
        try:
            await self.serve()
        finally:
            await motis_client.aclose_shared_client()

    def start(self) -> threading.Thread:
        """Run `serve()` on a new event loop in a daemon thread."""
        thread = threading.Thread(
            target=asyncio.run,
            args=(self._serve_then_close(),),
            name="coverage-executor",
            daemon=True,
        )
        thread.start()
        return thread

    def release(self, timeout_s: float = RELEASE_TIMEOUT_SECONDS) -> list[uuid.UUID]:
        """Release this executor's claims (graceful shutdown), waiting at
        most `timeout_s` for the database. Safe from a signal handler: the
        write runs in its own thread and a hung DB only costs the wait."""
        released: list[uuid.UUID] = []

        def _release() -> None:
            try:
                released.extend(self._db(release_claims, self.executor_id))
            except Exception:
                log.warning("coverage_executor.release_failed", exc_info=True)

        worker = threading.Thread(target=_release, name="coverage-release", daemon=True)
        worker.start()
        worker.join(timeout_s)
        if released:
            log.warning(
                "coverage_executor.released run_ids=%s executor=%s (resumed by the next executor)",
                ",".join(str(r) for r in released),
                self.executor_id,
            )
        return released
