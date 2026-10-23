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
  transaction. Claimable means `pending`, or `running` with a heartbeat
  older than `STALE_AFTER_SECONDS` (or none: released at a graceful
  shutdown, or a run left by web before this change);
- while it executes a run (`runner.execute_run`, unchanged pair logic and
  knobs: COVERAGE_PAIR_PARALLELISM, COVERAGE_WITHIN_PAIR_PARALLELISM) it
  refreshes `heartbeat_at` every `HEARTBEAT_SECONDS` (the database's clock).
  If the heartbeat finds the run held by another executor, the run stops in
  this process with no terminal write (`runner.abandon_run`);
- a `running` run claimed again is a **resume**: `resume_count` + 1 and a
  line in `summary.resumes`; the runner skips the pairs that already have a
  result row (one transaction writes a result row and its counter
  increments, and the (run, origin, dest) unique constraint refuses a
  second row), so the counters stay exact. Past COVERAGE_MAX_RESUMES the run
  is marked failed with `summary.resume_limit_reached` instead;
- at SIGTERM (a deploy recreates the worker) the worker releases its claims
  (`release_claims`) so the next executor resumes at once instead of
  waiting for the heartbeat to go stale.

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
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import case, func, select, update
from sqlalchemy.orm import Session as DbSession

from .. import config_service
from ..db import SessionLocal
from ..models import NetworkCoverageRun
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
    """One run this executor now holds."""

    run_id: uuid.UUID
    resumed: bool
    resume_count: int


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


def _claimable(stale_after_s: float) -> Any:
    stale_before = func.now() - timedelta(seconds=stale_after_s)
    running_and_stale = (NetworkCoverageRun.status == "running") & (
        NetworkCoverageRun.heartbeat_at.is_(None) | (NetworkCoverageRun.heartbeat_at < stale_before)
    )
    return (NetworkCoverageRun.status == "pending") | running_and_stale


def select_claimable(db: DbSession, stale_after_s: float) -> NetworkCoverageRun | None:
    """The next run to take, row-locked (FOR UPDATE SKIP LOCKED): a resume
    first, then the oldest queued run. A row another executor is claiming
    is skipped, never waited for, and Postgres re-checks the condition on
    the locked row, so a heartbeat written meanwhile makes it unclaimable."""
    return db.execute(
        select(NetworkCoverageRun)
        .where(_claimable(stale_after_s))
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


def _fail_resume_limit(run: NetworkCoverageRun, now: datetime, max_resumes: int) -> None:
    run.status = "failed"
    run.finished_at = now
    run.claimed_by = None
    run.summary = {
        **(run.summary or {}),
        "resume_limit_reached": True,
        "failure_reason": (
            f"the coverage executor stopped {run.resume_count + 1} times during this run "
            f"(COVERAGE_MAX_RESUMES = {max_resumes}); marked failed instead of resumed again"
        ),
        "failed_at": now.isoformat(),
        "completed_pairs_at_failure": run.completed_pairs or 0,
    }


def _record_resume(run: NetworkCoverageRun, now: datetime) -> None:
    previous = run.summary if isinstance(run.summary, dict) else {}
    resumes = list(previous.get("resumes") or [])
    resumes.append(
        {
            "resumed_at": now.isoformat(),
            "previous_executor": run.claimed_by,
            "last_heartbeat_at": run.heartbeat_at.isoformat() if run.heartbeat_at else None,
            "completed_pairs": run.completed_pairs or 0,
        }
    )
    run.resume_count = (run.resume_count or 0) + 1
    run.summary = {**previous, "resume_count": run.resume_count, "resumes": resumes}


def claim_next_run(
    db: DbSession,
    *,
    executor_id: str,
    max_resumes: int = DEFAULT_MAX_RESUMES,
    stale_after_s: float = STALE_AFTER_SECONDS,
) -> Claim | None:
    """Claim the next run for `executor_id`, or None when there is none.

    One transaction per run looked at: the row is locked by
    `select_claimable`, updated, committed. A stale `running` run past its
    resume limit is marked failed (with the reason) and the next one is
    looked at."""
    while True:
        run = select_claimable(db, stale_after_s)
        if run is None:
            db.rollback()
            return None
        now = datetime.now(UTC)
        resumed = run.status == "running"
        if resumed and (run.resume_count or 0) >= max_resumes:
            failed_id, resumes = run.id, run.resume_count or 0
            _fail_resume_limit(run, now, max_resumes)
            db.commit()
            log.warning(
                "coverage_executor.resume_limit_reached run_id=%s resumes=%d limit=%d",
                failed_id,
                resumes,
                max_resumes,
            )
            continue
        age = _heartbeat_age_s(run, now)
        if resumed:
            _record_resume(run, now)
        else:
            run.status = "running"
            run.started_at = now
        run.claimed_by = executor_id
        run.heartbeat_at = func.now()
        claim = Claim(run_id=run.id, resumed=resumed, resume_count=run.resume_count or 0)
        db.commit()
        if resumed:
            log.warning(
                "coverage_executor.resumed run_id=%s resume_count=%d heartbeat_age_s=%s "
                "executor=%s",
                claim.run_id,
                claim.resume_count,
                "none" if age is None else f"{age:.0f}",
                executor_id,
            )
        else:
            log.info("coverage_executor.claimed run_id=%s executor=%s", claim.run_id, executor_id)
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
    no holder, no heartbeat, so the next executor resumes it at its first
    poll. The run stays `running`; it is paused, not failed."""
    released = list(
        db.execute(
            update(NetworkCoverageRun)
            .where(
                NetworkCoverageRun.claimed_by == executor_id,
                NetworkCoverageRun.status == "running",
            )
            .values(claimed_by=None, heartbeat_at=None)
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
        """Claim one run if this executor has room, and start it."""
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
        except Exception:
            log.exception("coverage_executor.run_crashed run_id=%s", claim.run_id)
        beat.cancel()
        try:
            await self._call(settle_unfinished, claim.run_id, self.executor_id)
        except Exception:
            log.exception("coverage_executor.settle_failed run_id=%s", claim.run_id)
        self._active.pop(claim.run_id, None)

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
        for task in list(self._active.values()):
            task.cancel()

    def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()

    def start(self) -> threading.Thread:
        """Run `serve()` on a new event loop in a daemon thread."""
        thread = threading.Thread(
            target=asyncio.run, args=(self.serve(),), name="coverage-executor", daemon=True
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
