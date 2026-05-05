"""Async coverage runner — iterates pairs, persists results, updates
the run's progress counters (v0.1.27).

Bounded concurrency: pairs run 5-at-a-time by default. Sequential is
clean and predictable but takes ~42 minutes for 506 directional pairs;
5-way parallelism cuts that to ~10 min while staying well below OTP's
internal queue depth and our `MAX_CONCURRENT_REBUILDS` semaphore (which
isn't on the journey path, but we want to leave headroom for live
journey searches submitted by operators in parallel).

The runner reuses `app.journey.otp_client.fetch_plan` directly — same
plumbing the live journey UI uses. Each pair is recorded into the
existing `journey_searches` + `journey_search_executions` +
`journey_trips` infrastructure so the v0.1.26 trip-card UI works as the
click-cell drilldown out of the box.

State machine:
  pending (created)
    → running (background task started)
      → completed (all pairs processed)
      | failed (unexpected exception escaped the loop)
      | cancelled (operator stopped via API — not yet wired in v0.1.27)
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session as DbSession

from ..db import SessionLocal
from ..journey import otp_client, recorder
from ..models import (
    NetworkCoverageResult,
    NetworkCoverageRun,
)
from ..models import Session as SessionRow
from .hubs import Hub, all_pairs, unordered_pairs

log = logging.getLogger(__name__)

# Bounded parallelism — number of pairs to run simultaneously.
# 5 keeps OTP comfortable while still cutting wallclock by ~5x.
# Adjust via NETWORK_COVERAGE_PARALLELISM platform_config in the future
# if operators want it tunable.
_PARALLELISM = 5

# Per-pair HTTP timeout. We use a generous 60s — coverage runs are not
# user-facing, and a slow OTP returning a real itinerary is more
# valuable signal than a fast timeout.
_PER_PAIR_TIMEOUT_MS = 60_000


def create_run(
    db: DbSession,
    *,
    actor_user_id: uuid.UUID | None,
    session_id: str,
    depart_at: datetime,
    direction: str = "both",
) -> NetworkCoverageRun:
    """Create a pending coverage run and return it.

    Caller is responsible for kicking off the background task that
    processes the run (see `start_run_background`).
    """
    if direction not in ("both", "single"):
        raise ValueError(f"direction must be 'both' or 'single', got {direction!r}")
    pairs = all_pairs() if direction == "both" else unordered_pairs()
    run = NetworkCoverageRun(
        actor_user_id=actor_user_id,
        session_id=session_id,
        session_label=session_id,
        depart_at=depart_at,
        # v0.1.28: hub_set reflects how many hubs are in the curated
        # list at run-creation time. v0.1.27 had 23; v0.1.28 added Paris
        # Austerlitz, Paris Saint-Lazare, and Batz-sur-Mer = 26. Stored
        # so the matrix view can refuse to render a v0.1.27 run with
        # the v0.1.28 hub layout (which would mis-align cells).
        hub_set="fr-major-26",
        direction=direction,
        status="pending",
        total_pairs=len(pairs),
    )
    db.add(run)
    db.flush()
    return run


async def execute_run(run_id: uuid.UUID) -> None:
    """Drive a pending coverage run to completion.

    Designed to be called from a FastAPI BackgroundTask. Manages its own
    DB session lifecycle so the request's transaction can commit + return
    immediately. Idempotent in the loose sense: re-running on a completed
    run is a no-op.
    """
    log.info("network-coverage run %s starting", run_id)
    started = time.monotonic()
    pairs: list[tuple[Hub, Hub]] = []
    session_id_for_pairs: str = ""
    depart_at_for_pairs: datetime | None = None

    # Phase 1: snapshot the run inputs and flip to "running" in a short txn.
    with SessionLocal() as db:
        run = db.get(NetworkCoverageRun, run_id)
        if run is None:
            log.warning("network-coverage run %s not found — aborting", run_id)
            return
        if run.status not in ("pending", "running"):
            log.info(
                "network-coverage run %s already in terminal state %s — skipping",
                run_id,
                run.status,
            )
            return
        run.status = "running"
        run.started_at = datetime.now(UTC)
        db.commit()
        if run.session_id is None:
            log.warning("run %s has no session_id — aborting", run_id)
            run.status = "failed"
            run.finished_at = datetime.now(UTC)
            db.commit()
            return
        session_id_for_pairs = run.session_id
        depart_at_for_pairs = run.depart_at
        pairs = all_pairs() if run.direction == "both" else unordered_pairs()

    if not pairs or depart_at_for_pairs is None:
        log.error("run %s has no pairs to execute", run_id)
        return

    # Phase 2: process pairs with bounded concurrency. Each pair gets
    # its own short-lived DB session — no transaction stays open
    # across the network call to OTP.
    semaphore = asyncio.Semaphore(_PARALLELISM)

    async def _one_pair(origin: Hub, dest: Hub) -> None:
        async with semaphore:
            await _execute_pair(
                run_id=run_id,
                session_id=session_id_for_pairs,
                origin=origin,
                dest=dest,
                depart_at=depart_at_for_pairs,
            )

    try:
        await asyncio.gather(
            *(_one_pair(o, d) for o, d in pairs),
            return_exceptions=False,
        )
    except Exception:
        log.exception("network-coverage run %s failed mid-loop", run_id)
        with SessionLocal() as db:
            db.execute(
                update(NetworkCoverageRun)
                .where(NetworkCoverageRun.id == run_id)
                .values(status="failed", finished_at=datetime.now(UTC))
            )
            db.commit()
        return

    elapsed_s = time.monotonic() - started
    log.info(
        "network-coverage run %s completed in %.1fs (%d pairs)",
        run_id,
        elapsed_s,
        len(pairs),
    )

    # Phase 3: aggregate summary + flip to completed.
    with SessionLocal() as db:
        run = db.get(NetworkCoverageRun, run_id)
        if run is None:
            return
        run.status = "completed"
        run.finished_at = datetime.now(UTC)
        # Compute summary counters one last time — the per-pair updates
        # incremented these but a final SUM is bug-resistant.
        rows = (
            db.execute(select(NetworkCoverageResult).where(NetworkCoverageResult.run_id == run_id))
            .scalars()
            .all()
        )
        run.completed_pairs = len(rows)
        run.ok_pairs = sum(1 for r in rows if r.status == "ok")
        run.no_route_pairs = sum(1 for r in rows if r.status == "no_route")
        run.error_pairs = sum(1 for r in rows if r.status not in ("ok", "no_route", "skipped"))
        run.summary = {
            "elapsed_seconds": elapsed_s,
            "median_response_ms": _median(
                [r.response_ms for r in rows if r.response_ms is not None]
            ),
            "p95_response_ms": _percentile(
                [r.response_ms for r in rows if r.response_ms is not None], 0.95
            ),
        }
        db.commit()


async def _execute_pair(
    *,
    run_id: uuid.UUID,
    session_id: str,
    origin: Hub,
    dest: Hub,
    depart_at: datetime,
) -> None:
    """Run a single A→B search and persist the result row.

    Wraps `otp_client.fetch_plan` (the same call the live journey UI
    makes) so coverage results are bit-equivalent to what an operator
    would see via the Search page — modulo the lat/lon coords which
    come from the hub preset rather than master_stations geocoding.
    """
    started = time.monotonic()
    status = "error"
    response_ms = 0
    num_itineraries: int | None = None
    best_duration_seconds: int | None = None
    best_num_transfers: int | None = None
    best_operators: str | None = None
    error_message: str | None = None
    journey_search_id: uuid.UUID | None = None
    raw: dict[str, Any] = {}
    trips: list[dict[str, Any]] = []

    try:
        raw, trips = await otp_client.fetch_plan(
            session_id=session_id,
            from_lat=origin.lat,
            from_lon=origin.lon,
            to_lat=dest.lat,
            to_lon=dest.lon,
            when=depart_at,
            timeout_ms=_PER_PAIR_TIMEOUT_MS,
        )
        response_ms = int((time.monotonic() - started) * 1000)
        num_itineraries = len(trips)
        if trips:
            status = "ok"
            best = min(trips, key=lambda t: t.get("duration_seconds") or 1 << 30)
            best_duration_seconds = int(best.get("duration_seconds") or 0)
            best_num_transfers = int(best.get("num_transfers") or 0)
            seen_ops: list[str] = []
            for lg in best.get("legs", []):
                feed = lg.get("feed_id")
                if feed and feed not in seen_ops:
                    seen_ops.append(feed)
            best_operators = ",".join(seen_ops) if seen_ops else None
        else:
            status = "no_route"
    except Exception as exc:  # broad: persist as "error" + message
        response_ms = int((time.monotonic() - started) * 1000)
        # Distinguish timeout from generic error for the matrix colouring.
        cls_name = type(exc).__name__
        status = "timeout" if "Timeout" in cls_name or "timeout" in cls_name.lower() else "error"
        error_message = f"{cls_name}: {exc}"[:500]
        log.warning(
            "coverage run %s pair %s→%s failed: %s",
            run_id,
            origin.id,
            dest.id,
            error_message,
        )

    # Persist into journey_searches + journey_trips (best-effort, so the
    # click-cell drilldown can reuse the v0.1.26 trip-card UI).
    with SessionLocal() as db:
        try:
            search = recorder.begin_search(
                db,
                user_id=None,  # ran from a background task
                ip=None,
                endpoint="network-coverage",
                origin_lat=origin.lat,
                origin_lon=origin.lon,
                origin_label=origin.name,
                dest_lat=dest.lat,
                dest_lon=dest.lon,
                dest_label=dest.name,
                requested_time_kind="depart",
                requested_time=depart_at,
                modes="TRANSIT,WALK",
            )
            session_row = db.get(SessionRow, session_id)
            if session_row is not None:
                recorder.record_execution(
                    db,
                    search_id=search.id,
                    session_id=session_id,
                    graph_snapshot_id=None,
                    status=status,
                    response_ms=response_ms,
                    raw_response=raw or None,
                    error_message=error_message,
                    trips=trips,
                )
            recorder.finish_search(
                db,
                search,
                total_response_ms=response_ms,
                total_trips_unique=len(trips),
                status=status,
            )
            journey_search_id = search.id
            db.commit()
        except Exception:
            log.exception(
                "coverage run %s pair %s→%s: recorder write failed (continuing)",
                run_id,
                origin.id,
                dest.id,
            )
            db.rollback()

    # Always persist the coverage result, even when the recorder write
    # failed — the matrix is the canonical artifact.
    with SessionLocal() as db:
        try:
            db.add(
                NetworkCoverageResult(
                    run_id=run_id,
                    origin_hub_id=origin.id,
                    dest_hub_id=dest.id,
                    status=status,
                    response_ms=response_ms,
                    num_itineraries=num_itineraries,
                    best_duration_seconds=best_duration_seconds,
                    best_num_transfers=best_num_transfers,
                    best_operators=best_operators,
                    error_message=error_message,
                    journey_search_id=journey_search_id,
                )
            )
            # Increment run-level counters atomically so the UI's progress
            # bar updates even before the run completes.
            db.execute(
                update(NetworkCoverageRun)
                .where(NetworkCoverageRun.id == run_id)
                .values(
                    completed_pairs=NetworkCoverageRun.completed_pairs + 1,
                    ok_pairs=NetworkCoverageRun.ok_pairs + (1 if status == "ok" else 0),
                    no_route_pairs=NetworkCoverageRun.no_route_pairs
                    + (1 if status == "no_route" else 0),
                    error_pairs=NetworkCoverageRun.error_pairs
                    + (1 if status not in ("ok", "no_route", "skipped") else 0),
                )
            )
            db.commit()
        except Exception:
            log.exception(
                "coverage run %s pair %s→%s: result write failed",
                run_id,
                origin.id,
                dest.id,
            )
            db.rollback()


def _median(values: list[int]) -> int | None:
    if not values:
        return None
    sorted_v = sorted(values)
    return sorted_v[len(sorted_v) // 2]


def _percentile(values: list[int], p: float) -> int | None:
    if not values:
        return None
    sorted_v = sorted(values)
    idx = min(int(p * len(sorted_v)), len(sorted_v) - 1)
    return sorted_v[idx]


def get_run_with_results(
    db: DbSession, run_id: uuid.UUID
) -> tuple[NetworkCoverageRun | None, list[NetworkCoverageResult]]:
    """Fetch one run + all its results — used by the GET endpoint."""
    run = db.get(NetworkCoverageRun, run_id)
    if run is None:
        return None, []
    results = (
        db.execute(select(NetworkCoverageResult).where(NetworkCoverageResult.run_id == run_id))
        .scalars()
        .all()
    )
    return run, list(results)


def list_recent_runs(db: DbSession, *, limit: int = 20) -> list[NetworkCoverageRun]:
    """Most-recent runs first — for the sidebar list on the admin page."""
    return list(
        db.execute(
            select(NetworkCoverageRun).order_by(NetworkCoverageRun.started_at.desc()).limit(limit)
        )
        .scalars()
        .all()
    )
