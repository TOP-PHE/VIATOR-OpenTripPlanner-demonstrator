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
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ... import audit, config_service
from ...db import get_db
from ...journey import otp_client, recorder
from ...models import (
    GraphSnapshot,
    JourneySearch,
    JourneySearchExecution,
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
    dry_run: bool = Field(default=False, description="Plan and report what would run; don't execute")


@router.post("")
async def replay(
    body: ReplayBody,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    actor: Annotated[CurrentUser, Depends(require_platform_admin)],
) -> dict[str, Any]:
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
        .where(JourneySearch.id.in_(
            db.query(JourneySearchExecution.search_id)
            .filter(JourneySearchExecution.session_id == body.filter.session_id)
            .scalar_subquery()
        ))
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
    queued: list[JourneySearch] = []
    skipped_main_version: list[uuid.UUID] = []
    for s in candidates:
        exe = (
            db.execute(
                select(JourneySearchExecution)
                .where(JourneySearchExecution.search_id == s.id)
                .where(JourneySearchExecution.session_id == body.filter.session_id)
                .limit(1)
            )
            .scalar_one_or_none()
        )
        if exe is None:
            skipped_main_version.append(s.id)
            continue
        original_snap = db.get(GraphSnapshot, exe.graph_snapshot_id)
        if original_snap is None or original_snap.timetable_main_version != target.timetable_main_version:
            skipped_main_version.append(s.id)
            continue
        queued.append(s)

    if body.dry_run:
        return {
            "would_replay": len(queued),
            "would_skip_main_version_mismatch": len(skipped_main_version),
            "truncated": truncated,
            "cap": cap,
        }

    # Execute. Throttle to ~rps queries/sec.
    delay = 1.0 / max(rps, 1)
    outcomes = {"now_ok": 0, "still_failing": 0, "different_result": 0, "errors": 0}
    audit.record(
        db, action="replay.started",
        actor_user_id=actor.id, actor_ip=client_ip(request),
        target_kind="graph_snapshot", target_id=str(target.id),
        metadata={"queued": len(queued), "filter": body.filter.model_dump(mode="json")},
    )
    db.commit()

    timeout_ms = int(cfg["JOURNEY_TIMEOUT_MS"])
    for s in queued:
        try:
            replay_search = recorder.begin_search(
                db,
                user_id=s.user_id, ip=None, endpoint="plan",
                origin_lat=s.origin_lat, origin_lon=s.origin_lon, origin_label=s.origin_label,
                dest_lat=s.dest_lat, dest_lon=s.dest_lon, dest_label=s.dest_label,
                requested_time_kind=s.requested_time_kind, requested_time=s.requested_time,
                modes=s.modes, replay_of_search_id=s.id,
            )
            t0 = time.monotonic()
            try:
                _, trips = await otp_client.fetch_plan(
                    session_id=target.session_id,
                    from_lat=s.origin_lat, from_lon=s.origin_lon,
                    to_lat=s.dest_lat,     to_lon=s.dest_lon,
                    when=s.requested_time, timeout_ms=timeout_ms,
                )
                status = "ok" if trips else "no_route"
            except Exception:  # noqa: BLE001
                status = "error"
                trips = []
            elapsed = int((time.monotonic() - t0) * 1000)

            recorder.record_execution(
                db, search_id=replay_search.id, session_id=target.session_id,
                graph_snapshot_id=target.id, status=status,
                response_ms=elapsed, raw_response=None,
                error_message=None, trips=trips,
            )
            recorder.finish_search(
                db, replay_search,
                total_response_ms=elapsed,
                total_trips_unique=len(trips),
                status=status,
            )

            if status == "ok" and s.status != "ok":
                outcomes["now_ok"] += 1
            elif status != "ok" and s.status != "ok":
                outcomes["still_failing"] += 1
            elif status == "ok" and s.status == "ok":
                outcomes["different_result"] += 1  # both ok — coarse-grained, refine later
            else:
                outcomes["errors"] += 1

            db.commit()
        except Exception:  # noqa: BLE001
            outcomes["errors"] += 1
        await asyncio.sleep(delay)

    audit.record(
        db, action="replay.finished",
        actor_user_id=actor.id, actor_ip=client_ip(request),
        target_kind="graph_snapshot", target_id=str(target.id),
        metadata={"outcomes": outcomes},
    )
    db.commit()

    return {
        "queued": len(queued),
        "skipped_main_version_mismatch": len(skipped_main_version),
        "truncated": truncated,
        "outcomes": outcomes,
    }
