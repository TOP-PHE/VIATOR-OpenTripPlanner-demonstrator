"""Journey endpoints — fanout (default), plan (single session), searches/<id>."""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from .. import concurrency, config_service
from ..db import get_db
from ..journey import otp_client, recorder
from ..models import GraphSnapshot, Session as SessionRow
from ..models.sessions import SessionState
from ..security import CurrentUser, client_ip, require_logged_in


router = APIRouter(prefix="/api/journey", tags=["journey"])


# ────────────────────────── schemas ──────────────────────────


class Coord(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    label: str | None = None


class FanoutBody(BaseModel):
    from_: Coord = Field(alias="from")
    to: Coord
    depart_at: datetime | None = None
    arrive_by: datetime | None = None
    modes: list[str] = Field(default_factory=lambda: ["TRANSIT", "WALK"])

    model_config = {"populate_by_name": True}


class PlanBody(FanoutBody):
    session_id: str


# ────────────────────────── helpers ──────────────────────────


def _resolve_when(body: FanoutBody) -> tuple[str, datetime]:
    if body.arrive_by is not None:
        return "arrive_by", body.arrive_by
    return "depart_at", body.depart_at or datetime.utcnow()


async def _query_session(
    db: DbSession,
    session: SessionRow,
    body: FanoutBody,
    timeout_ms: int,
) -> tuple[str, dict[str, Any], list[dict[str, Any]], int]:
    """Returns (status, raw, trips, response_ms)."""
    start = time.monotonic()
    try:
        when_kind, when = _resolve_when(body)  # noqa: F841 (kind currently informational)
        raw, trips = await otp_client.fetch_plan(
            session_id=session.id,
            from_lat=body.from_.lat, from_lon=body.from_.lon,
            to_lat=body.to.lat,     to_lon=body.to.lon,
            when=when, timeout_ms=timeout_ms,
        )
        elapsed = int((time.monotonic() - start) * 1000)
        return ("ok" if trips else "no_route"), raw, trips, elapsed
    except (httpx.TimeoutException, asyncio.TimeoutError):
        return "timeout", {}, [], int((time.monotonic() - start) * 1000)
    except httpx.HTTPError:
        return "error", {}, [], int((time.monotonic() - start) * 1000)


def _current_snapshot(db: DbSession, sid: str) -> GraphSnapshot | None:
    return (
        db.execute(
            select(GraphSnapshot)
            .where(GraphSnapshot.session_id == sid)
            .where(GraphSnapshot.is_current.is_(True))
            .limit(1)
        )
        .scalar_one_or_none()
    )


def _origin_flag(found_in: list[str], all_fanout: list[str]) -> str:
    """ALL / NAP_ONLY / MERITS_ONLY / <session>_ONLY / SUBSET."""
    s = set(found_in)
    if s == set(all_fanout):
        return "ALL"
    if len(s) == 1:
        return f"{next(iter(s)).upper()}_ONLY"
    return "SUBSET"


# ────────────────────────── routes ──────────────────────────


@router.post("/fanout", summary="Run a search across every fanout-enabled session")
async def fanout(
    body: FanoutBody,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    user: Annotated[CurrentUser, Depends(require_logged_in)],
) -> dict[str, Any]:
    cfg = config_service.get_all(db)
    sessions = (
        db.execute(
            select(SessionRow)
            .where(SessionRow.state == SessionState.SERVING.value)
            .where(SessionRow.include_in_fanout.is_(True))
        )
        .scalars()
        .all()
    )
    if not sessions:
        raise HTTPException(409, "No serving sessions are enabled for fanout")

    when_kind, when = _resolve_when(body)
    overall_start = time.monotonic()

    try:
        async with concurrency.semaphores.journey.acquire_or_fail():
            search = recorder.begin_search(
                db,
                user_id=user.id, ip=client_ip(request), endpoint="fanout",
                origin_lat=body.from_.lat, origin_lon=body.from_.lon, origin_label=body.from_.label,
                dest_lat=body.to.lat, dest_lon=body.to.lon, dest_label=body.to.label,
                requested_time_kind=when_kind, requested_time=when,
                modes=",".join(body.modes),
            )
            timeout_ms = int(cfg["FANOUT_TIMEOUT_MS"])

            results = await asyncio.gather(
                *[_query_session(db, s, body, timeout_ms) for s in sessions]
            )
    except concurrency.ConcurrencyExceeded as exc:
        raise HTTPException(503, str(exc), headers={"Retry-After": "5"}) from exc

    by_signature: dict[str, dict[str, Any]] = {}
    executions_summary: list[dict[str, Any]] = []
    any_error = False
    any_ok = False
    sids_in_fanout = [s.id for s in sessions]

    for session, (status, raw, trips, response_ms) in zip(sessions, results, strict=True):
        snap = _current_snapshot(db, session.id)
        if snap is None:
            status = "error"
        if status == "ok":
            any_ok = True
        else:
            any_error = True

        exe = recorder.record_execution(
            db,
            search_id=search.id,
            session_id=session.id,
            graph_snapshot_id=snap.id if snap else _placeholder_snapshot_id(),
            status=status,
            response_ms=response_ms,
            raw_response=raw if cfg.get("STORE_RAW_RESPONSE", True) else None,
            error_message=None,
            trips=trips,
        )
        executions_summary.append({
            "session_id": session.id,
            "graph_snapshot_id": str(exe.graph_snapshot_id),
            "status": status,
            "num_itineraries": exe.num_itineraries,
            "response_ms": response_ms,
        })

        # Merge trips by signature for the response payload.
        for trip in trips:
            from .. import journey as journey_pkg  # noqa: F401  (keep package import explicit)
            from ..journey.signature import trip_signature
            sig = trip_signature(db, session_id=session.id, legs=trip.get("legs", []))
            slot = by_signature.setdefault(sig, {
                "signature": sig, "found_in_sessions": [], "by_session": {}, "best": trip,
            })
            slot["found_in_sessions"].append(session.id)
            slot["by_session"][session.id] = {
                "duration_seconds": trip["duration_seconds"],
                "departure_at": trip["departure_at"],
                "arrival_at": trip["arrival_at"],
            }
            if trip["duration_seconds"] < slot["best"]["duration_seconds"]:
                slot["best"] = trip

    overall_ms = int((time.monotonic() - overall_start) * 1000)

    if any_ok and any_error:
        status = "partial"
    elif any_ok:
        status = "ok"
    elif by_signature:
        status = "ok"
    else:
        status = "no_route" if not any_error else "error"

    recorder.finish_search(
        db,
        search,
        total_response_ms=overall_ms,
        total_trips_unique=len(by_signature),
        status=status,
    )
    db.commit()

    merged_trips = []
    for slot in by_signature.values():
        merged_trips.append({
            **slot,
            "origin_flag": _origin_flag(slot["found_in_sessions"], sids_in_fanout),
        })

    return {
        "search_id": str(search.id),
        "status": status,
        "trips": merged_trips,
        "executions": executions_summary,
    }


@router.post("/plan", summary="Plan against a single explicit session")
async def plan(
    body: PlanBody,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    user: Annotated[CurrentUser, Depends(require_logged_in)],
) -> dict[str, Any]:
    s = db.get(SessionRow, body.session_id)
    if s is None or s.state != SessionState.SERVING.value:
        raise HTTPException(404, f"No serving session {body.session_id!r}")

    cfg = config_service.get_all(db)
    when_kind, when = _resolve_when(body)

    try:
        async with concurrency.semaphores.journey.acquire_or_fail():
            search = recorder.begin_search(
                db,
                user_id=user.id, ip=client_ip(request), endpoint="plan",
                origin_lat=body.from_.lat, origin_lon=body.from_.lon, origin_label=body.from_.label,
                dest_lat=body.to.lat, dest_lon=body.to.lon, dest_label=body.to.label,
                requested_time_kind=when_kind, requested_time=when,
                modes=",".join(body.modes),
            )
            status, raw, trips, response_ms = await _query_session(
                db, s, body, int(cfg["JOURNEY_TIMEOUT_MS"])
            )
    except concurrency.ConcurrencyExceeded as exc:
        raise HTTPException(503, str(exc), headers={"Retry-After": "5"}) from exc

    snap = _current_snapshot(db, s.id)
    if snap is None:
        status = "error"
    recorder.record_execution(
        db, search_id=search.id, session_id=s.id,
        graph_snapshot_id=snap.id if snap else _placeholder_snapshot_id(),
        status=status, response_ms=response_ms,
        raw_response=raw if cfg.get("STORE_RAW_RESPONSE", True) else None,
        error_message=None, trips=trips,
    )
    recorder.finish_search(
        db, search,
        total_response_ms=response_ms,
        total_trips_unique=len(trips),
        status=status,
    )
    db.commit()

    return {"search_id": str(search.id), "status": status, "trips": trips}


@router.get("/searches/{search_id}")
def get_search(
    search_id: uuid.UUID,
    db: Annotated[DbSession, Depends(get_db)],
    user: Annotated[CurrentUser, Depends(require_logged_in)],
) -> dict[str, Any]:
    from ..models import JourneySearch, JourneySearchExecution, JourneyTrip

    s = db.get(JourneySearch, search_id)
    if s is None:
        raise HTTPException(404, "Search not found")
    if user.role != "platform_admin" and s.user_id != user.id:
        raise HTTPException(403, "Not your search")

    execs = (
        db.execute(select(JourneySearchExecution).where(JourneySearchExecution.search_id == s.id))
        .scalars()
        .all()
    )
    out: dict[str, Any] = {
        "id": str(s.id),
        "endpoint": s.endpoint,
        "status": s.status,
        "executions": [],
    }
    for exe in execs:
        trips = (
            db.execute(select(JourneyTrip).where(JourneyTrip.execution_id == exe.id).order_by(JourneyTrip.rank_in_response))
            .scalars()
            .all()
        )
        out["executions"].append({
            "session_id": exe.session_id,
            "status": exe.status,
            "num_itineraries": exe.num_itineraries,
            "response_ms": exe.response_ms,
            "trips": [
                {
                    "signature": t.trip_signature,
                    "duration_seconds": t.duration_seconds,
                    "num_transfers": t.num_transfers,
                    "departure_at": t.departure_at.isoformat(),
                    "arrival_at": t.arrival_at.isoformat(),
                    "modes": t.modes,
                    "legs": t.legs,
                }
                for t in trips
            ],
        })
    return out


# ────────────────────────── placeholder ──────────────────────────


def _placeholder_snapshot_id() -> uuid.UUID:
    """When a session has no snapshot yet, we still need a valid UUID for the FK.

    NOTE: this is a deliberate violation that step 8 (real per-session OTP) will
    fix by ensuring every serving session has at least one snapshot. Until then,
    callers should expect occasional FK errors on uninitialised sessions.
    """
    return uuid.UUID(int=0)  # all-zero UUID, intentionally invalid as FK
