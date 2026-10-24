"""Replay endpoint — re-run historical searches against a target snapshot.

Same-main-version is enforced: searches whose original snapshot has a
different `timetable_main_version` than the target are skipped.

For Phase-1 we keep this synchronous (the batch cap defaults to 1000 searches,
~5 rps, so worst case ~3 minutes blocking). A background queue can be added
later without changing the API surface.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession
from starlette.concurrency import run_in_threadpool

from ... import audit, config_service
from ...db import get_db
from ...journey import planner_dispatch, recorder
from ...models import (
    GraphSnapshot,
    JourneySearch,
    JourneySearchExecution,
)
from ...models import (
    Session as SessionRow,
)
from ...security import CurrentUser, client_ip, require_platform_admin

router = APIRouter(prefix="/api/admin/replay", tags=["admin", "replay"])


class ReplayFilter(BaseModel):
    session_id: str
    since: datetime
    until: datetime | None = None
    status: str | None = None


class ReplayBody(BaseModel):
    filter: ReplayFilter
    against_graph_snapshot_id: uuid.UUID
    dry_run: bool = Field(
        default=False, description="Plan and report what would run; don't execute"
    )


@dataclass(frozen=True)
class _Queued:
    """One search to replay, read before the first commit: the values the
    replay needs, so nothing is loaded again later on the event loop."""

    id: uuid.UUID
    user_id: uuid.UUID | None
    origin_lat: float
    origin_lon: float
    origin_label: str | None
    dest_lat: float
    dest_lon: float
    dest_label: str | None
    requested_time_kind: str
    requested_time: datetime
    modes: str
    status: str


@dataclass(frozen=True)
class _ReplayPlan:
    target_id: uuid.UUID
    target_session_id: str
    planner: planner_dispatch._Planner
    cfg: dict[str, Any]
    cap: int
    rps: int
    queued: list[_Queued]
    skipped_main_version: list[uuid.UUID]
    truncated: bool


@router.post("")
async def replay(
    body: ReplayBody,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    actor: Annotated[CurrentUser, Depends(require_platform_admin)],
) -> dict[str, Any]:
    """#397 — every database step runs in the thread pool, one after the
    other on the request's session; the event loop awaits the engine and
    the throttle only."""
    plan = await run_in_threadpool(_replay_plan, db, body)

    if body.dry_run:
        return {
            "would_replay": len(plan.queued),
            "would_skip_main_version_mismatch": len(plan.skipped_main_version),
            "truncated": plan.truncated,
            "cap": plan.cap,
        }

    # Execute. Throttle to ~rps queries/sec.
    delay = 1.0 / max(plan.rps, 1)
    outcomes = {"now_ok": 0, "still_failing": 0, "different_result": 0, "errors": 0}
    ip = client_ip(request)
    await run_in_threadpool(
        _audit,
        db,
        "replay.started",
        actor.id,
        ip,
        plan.target_id,
        {"queued": len(plan.queued), "filter": body.filter.model_dump(mode="json")},
    )

    cfg = plan.cfg
    knobs = _Knobs(
        timeout_ms=int(cfg["JOURNEY_TIMEOUT_MS"]),
        num_itineraries=int(cfg["OTP_NUM_ITINERARIES"]),
        search_window_seconds=int(cfg["OTP_SEARCH_WINDOW_SECONDS"]),
    )
    for s in plan.queued:
        try:
            outcomes[await _replay_one(db, plan, knobs, s)] += 1
            await run_in_threadpool(db.commit)
        except Exception:
            outcomes["errors"] += 1
        await asyncio.sleep(delay)

    await run_in_threadpool(
        _audit, db, "replay.finished", actor.id, ip, plan.target_id, {"outcomes": outcomes}
    )

    return {
        "queued": len(plan.queued),
        "skipped_main_version_mismatch": len(plan.skipped_main_version),
        "truncated": plan.truncated,
        "outcomes": outcomes,
    }


@dataclass(frozen=True)
class _Knobs:
    timeout_ms: int
    num_itineraries: int
    search_window_seconds: int


async def _replay_one(db: DbSession, plan: _ReplayPlan, knobs: _Knobs, s: _Queued) -> str:
    """Replay one search against the target and record it (the caller
    commits); the outcome to count (`now_ok`, `still_failing`,
    `different_result`, `errors`). An exception (a database error) is
    counted as an error by the caller."""
    replay_search = await run_in_threadpool(_begin_replay, db, s)
    t0 = time.monotonic()
    try:
        _, trips = await plan.planner.fetch_plan(
            session_id=plan.target_session_id,
            from_lat=s.origin_lat,
            from_lon=s.origin_lon,
            to_lat=s.dest_lat,
            to_lon=s.dest_lon,
            when=s.requested_time,
            timeout_ms=knobs.timeout_ms,
            num_itineraries=knobs.num_itineraries,
            search_window_seconds=knobs.search_window_seconds,
        )
        status = "ok" if trips else "no_route"
    except Exception:
        status = "error"
        trips = []
    elapsed = int((time.monotonic() - t0) * 1000)
    await run_in_threadpool(_record_replay, db, plan, replay_search, status, elapsed, trips)
    return _replay_outcome(status, s.status)


def _replay_outcome(status: str, original_status: str) -> str:
    """How a replayed search compares with its original."""
    if status == "ok" and original_status != "ok":
        return "now_ok"
    if status != "ok" and original_status != "ok":
        return "still_failing"
    if status == "ok" and original_status == "ok":
        return "different_result"  # both ok — coarse-grained, refine later
    return "errors"


def _replay_plan(db: DbSession, body: ReplayBody) -> _ReplayPlan:
    """The target, the config and the searches to replay (404 for a missing
    target snapshot or session)."""
    target = db.get(GraphSnapshot, body.against_graph_snapshot_id)
    if target is None:
        raise HTTPException(404, "Target snapshot not found")
    target_session = db.get(SessionRow, target.session_id)
    if target_session is None:
        raise HTTPException(404, "Target session not found")

    cfg = config_service.get_all(db)
    cap = int(cfg["REPLAY_MAX_BATCH_SIZE"])
    rps = int(cfg["REPLAY_MAX_RPS"])

    # Find candidate searches.
    stmt = (
        select(JourneySearch)
        .where(
            JourneySearch.id.in_(
                db.query(JourneySearchExecution.search_id)
                .filter(JourneySearchExecution.session_id == body.filter.session_id)
                .scalar_subquery()
            )
        )
        .where(JourneySearch.ts >= body.filter.since)
    )
    if body.filter.until:
        stmt = stmt.where(JourneySearch.ts <= body.filter.until)
    if body.filter.status:
        stmt = stmt.where(JourneySearch.status == body.filter.status)
    stmt = stmt.order_by(JourneySearch.ts).limit(cap + 1)
    candidates = db.execute(stmt).scalars().all()

    truncated = len(candidates) > cap
    if truncated:
        candidates = candidates[:cap]

    # Same-main-version filter — load each candidate's execution to compare.
    queued: list[_Queued] = []
    skipped_main_version: list[uuid.UUID] = []
    for s in candidates:
        exe = db.execute(
            select(JourneySearchExecution)
            .where(JourneySearchExecution.search_id == s.id)
            .where(JourneySearchExecution.session_id == body.filter.session_id)
            .limit(1)
        ).scalar_one_or_none()
        if exe is None:
            skipped_main_version.append(s.id)
            continue
        original_snap = db.get(GraphSnapshot, exe.graph_snapshot_id)
        if (
            original_snap is None
            or original_snap.timetable_main_version != target.timetable_main_version
        ):
            skipped_main_version.append(s.id)
            continue
        queued.append(
            _Queued(
                id=s.id,
                user_id=s.user_id,
                origin_lat=s.origin_lat,
                origin_lon=s.origin_lon,
                origin_label=s.origin_label,
                dest_lat=s.dest_lat,
                dest_lon=s.dest_lon,
                dest_label=s.dest_label,
                requested_time_kind=s.requested_time_kind,
                requested_time=s.requested_time,
                modes=s.modes,
                status=s.status,
            )
        )

    return _ReplayPlan(
        target_id=target.id,
        target_session_id=target.session_id,
        planner=planner_dispatch.get_planner(target_session),
        cfg=cfg,
        cap=cap,
        rps=rps,
        queued=queued,
        skipped_main_version=skipped_main_version,
        truncated=truncated,
    )


def _audit(
    db: DbSession,
    action: str,
    actor_id: uuid.UUID | None,
    ip: str | None,
    target_id: uuid.UUID,
    metadata: dict[str, Any],
) -> None:
    """A replay's audit event, committed."""
    audit.record(
        db,
        action=action,
        actor_user_id=actor_id,
        actor_ip=ip,
        target_kind="graph_snapshot",
        target_id=str(target_id),
        metadata=metadata,
    )
    db.commit()


def _begin_replay(db: DbSession, s: _Queued) -> JourneySearch:
    """The journey_searches row of one replayed search."""
    return recorder.begin_search(
        db,
        user_id=s.user_id,
        ip=None,
        endpoint="plan",
        origin_lat=s.origin_lat,
        origin_lon=s.origin_lon,
        origin_label=s.origin_label,
        dest_lat=s.dest_lat,
        dest_lon=s.dest_lon,
        dest_label=s.dest_label,
        requested_time_kind=s.requested_time_kind,
        requested_time=s.requested_time,
        modes=s.modes,
        replay_of_search_id=s.id,
    )


def _record_replay(
    db: DbSession,
    plan: _ReplayPlan,
    replay_search: JourneySearch,
    status: str,
    elapsed: int,
    trips: list[dict[str, Any]],
) -> None:
    """One replayed search's execution and its finish (committed by the caller)."""
    recorder.record_execution(
        db,
        search_id=replay_search.id,
        session_id=plan.target_session_id,
        graph_snapshot_id=plan.target_id,
        status=status,
        response_ms=elapsed,
        raw_response=None,
        error_message=None,
        trips=trips,
    )
    recorder.finish_search(
        db,
        replay_search,
        total_response_ms=elapsed,
        total_trips_unique=len(trips),
        status=status,
    )
