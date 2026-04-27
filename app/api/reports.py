"""Search-analytics + version-diff report endpoints. See spec §9.6."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session as DbSession

from ..db import get_db
from ..models import (
    GraphSnapshot,
    JourneySearch,
    JourneySearchExecution,
    JourneyTrip,
    User,
)
from ..security import CurrentUser, require_platform_admin


router = APIRouter(prefix="/api/reports", tags=["admin", "reports"])


def _since_dt(since: str | None, default_days: int = 30) -> datetime:
    if since:
        try:
            return datetime.fromisoformat(since)
        except ValueError as exc:
            raise HTTPException(400, f"Invalid `since` ({since!r}); must be ISO-8601") from exc
    return datetime.now(timezone.utc) - timedelta(days=default_days)


# ────────────────────────── searches list ──────────────────────────


@router.get("/searches")
def list_searches(
    db: Annotated[DbSession, Depends(get_db)],
    _: Annotated[CurrentUser, Depends(require_platform_admin)],
    since: str | None = Query(None),
    user: str | None = Query(None, description="UUID of the user"),
    session: str | None = Query(None),
    status: str | None = Query(None),
    page: int = Query(0, ge=0),
    size: int = Query(50, ge=1, le=500),
) -> dict[str, Any]:
    cutoff = _since_dt(since)
    stmt = select(JourneySearch).where(JourneySearch.ts >= cutoff)
    if user:
        stmt = stmt.where(JourneySearch.user_id == user)
    if status:
        stmt = stmt.where(JourneySearch.status == status)
    if session:
        stmt = stmt.where(
            JourneySearch.id.in_(
                db.query(JourneySearchExecution.search_id)
                .filter(JourneySearchExecution.session_id == session)
                .scalar_subquery()
            )
        )
    stmt = stmt.order_by(desc(JourneySearch.ts)).offset(page * size).limit(size)
    rows = db.execute(stmt).scalars().all()
    return {
        "page": page, "size": size,
        "items": [_search_to_dict(s) for s in rows],
    }


def _search_to_dict(s: JourneySearch) -> dict[str, Any]:
    return {
        "id": str(s.id),
        "ts": s.ts.isoformat(),
        "user_id": str(s.user_id) if s.user_id else None,
        "endpoint": s.endpoint,
        "from": {"lat": s.origin_lat, "lon": s.origin_lon, "label": s.origin_label},
        "to":   {"lat": s.dest_lat,   "lon": s.dest_lon,   "label": s.dest_label},
        "requested": {"kind": s.requested_time_kind, "time": s.requested_time.isoformat()},
        "modes": s.modes,
        "status": s.status,
        "total_response_ms": s.total_response_ms,
        "total_trips_unique": s.total_trips_unique,
    }


# ────────────────────────── O&D pairs ──────────────────────────


@router.get("/od-pairs")
def od_pairs(
    db: Annotated[DbSession, Depends(get_db)],
    _: Annotated[CurrentUser, Depends(require_platform_admin)],
    since: str | None = Query(None),
    precision: int = Query(4, ge=2, le=6, description="lat/lon decimals"),
    limit: int = Query(50, ge=1, le=1000),
) -> list[dict[str, Any]]:
    cutoff = _since_dt(since)
    olat = func.round(JourneySearch.origin_lat.cast(_NUMERIC), precision)
    olon = func.round(JourneySearch.origin_lon.cast(_NUMERIC), precision)
    dlat = func.round(JourneySearch.dest_lat.cast(_NUMERIC),   precision)
    dlon = func.round(JourneySearch.dest_lon.cast(_NUMERIC),   precision)

    stmt = (
        select(
            olat.label("olat"),
            olon.label("olon"),
            dlat.label("dlat"),
            dlon.label("dlon"),
            func.count().label("count"),
            func.avg(JourneySearch.total_response_ms).label("avg_response_ms"),
        )
        .where(JourneySearch.ts >= cutoff)
        .group_by(olat, olon, dlat, dlon)
        .order_by(desc(func.count()))
        .limit(limit)
    )
    return [
        {
            "origin": {"lat": float(r.olat), "lon": float(r.olon)},
            "dest":   {"lat": float(r.dlat), "lon": float(r.dlon)},
            "count": r.count,
            "avg_response_ms": float(r.avg_response_ms) if r.avg_response_ms else None,
        }
        for r in db.execute(stmt).all()
    ]


# ────────────────────────── per-user / per-session volume ──────────────────────────


@router.get("/volume-per-user")
def volume_per_user(
    db: Annotated[DbSession, Depends(get_db)],
    _: Annotated[CurrentUser, Depends(require_platform_admin)],
    since: str | None = Query(None),
) -> list[dict[str, Any]]:
    cutoff = _since_dt(since)
    stmt = (
        select(JourneySearch.user_id, User.email, func.count())
        .join(User, User.id == JourneySearch.user_id, isouter=True)
        .where(JourneySearch.ts >= cutoff)
        .group_by(JourneySearch.user_id, User.email)
        .order_by(desc(func.count()))
    )
    return [
        {"user_id": str(uid) if uid else None, "email": email, "count": count}
        for uid, email, count in db.execute(stmt).all()
    ]


@router.get("/volume-per-session")
def volume_per_session(
    db: Annotated[DbSession, Depends(get_db)],
    _: Annotated[CurrentUser, Depends(require_platform_admin)],
    since: str | None = Query(None),
) -> list[dict[str, Any]]:
    cutoff = _since_dt(since)
    stmt = (
        select(
            JourneySearchExecution.session_id,
            func.count().label("count"),
            func.percentile_cont(0.50).within_group(JourneySearchExecution.response_ms).label("p50"),
            func.percentile_cont(0.95).within_group(JourneySearchExecution.response_ms).label("p95"),
            func.percentile_cont(0.99).within_group(JourneySearchExecution.response_ms).label("p99"),
            func.sum((JourneySearchExecution.status != "ok").cast(_INTEGER)).label("error_count"),
        )
        .join(JourneySearch, JourneySearch.id == JourneySearchExecution.search_id)
        .where(JourneySearch.ts >= cutoff)
        .group_by(JourneySearchExecution.session_id)
        .order_by(desc(func.count()))
    )
    out = []
    for r in db.execute(stmt).all():
        total = r.count or 0
        out.append({
            "session_id": r.session_id,
            "count": total,
            "p50_ms": float(r.p50) if r.p50 is not None else None,
            "p95_ms": float(r.p95) if r.p95 is not None else None,
            "p99_ms": float(r.p99) if r.p99 is not None else None,
            "error_rate": (float(r.error_count) / total) if total else 0.0,
        })
    return out


# ────────────────────────── trip-source distribution ──────────────────────────


@router.get("/trip-source-distribution")
def trip_source_distribution(
    db: Annotated[DbSession, Depends(get_db)],
    _: Annotated[CurrentUser, Depends(require_platform_admin)],
    since: str | None = Query(None),
) -> list[dict[str, Any]]:
    """For each search, count trips per session. Aggregate by session-set."""
    cutoff = _since_dt(since)
    # Per (search, signature) → set of sessions.
    stmt = (
        select(
            JourneyTrip.trip_signature,
            JourneySearchExecution.search_id,
            func.array_agg(func.distinct(JourneySearchExecution.session_id)).label("sessions"),
        )
        .join(JourneySearchExecution, JourneySearchExecution.id == JourneyTrip.execution_id)
        .join(JourneySearch, JourneySearch.id == JourneySearchExecution.search_id)
        .where(JourneySearch.ts >= cutoff)
        .group_by(JourneyTrip.trip_signature, JourneySearchExecution.search_id)
    )
    buckets: dict[str, int] = {}
    for _sig, _sid, sessions in db.execute(stmt).all():
        key = "+".join(sorted(sessions or []))
        buckets[key] = buckets.get(key, 0) + 1
    total = sum(buckets.values()) or 1
    return [{"sessions": k, "count": v, "ratio": v / total} for k, v in sorted(buckets.items(), key=lambda x: -x[1])]


# ────────────────────────── version-diff ──────────────────────────


@router.get("/version-diff")
def version_diff(
    db: Annotated[DbSession, Depends(get_db)],
    _: Annotated[CurrentUser, Depends(require_platform_admin)],
    session: str = Query(..., description="session_id"),
    from_snapshot: str = Query(..., description="UUID"),
    to_snapshot: str = Query(..., description="UUID"),
) -> dict[str, Any]:
    sa = db.get(GraphSnapshot, from_snapshot)
    sb = db.get(GraphSnapshot, to_snapshot)
    if sa is None or sb is None:
        raise HTTPException(404, "Snapshot not found")
    if sa.session_id != session or sb.session_id != session:
        raise HTTPException(400, "Both snapshots must belong to the same session")
    if sa.timetable_main_version != sb.timetable_main_version:
        raise HTTPException(
            400,
            f"Cannot diff across main versions: {sa.timetable_main_version!r} vs {sb.timetable_main_version!r}",
        )

    sigs_a = _trip_signatures_for_snapshot(db, from_snapshot)
    sigs_b = _trip_signatures_for_snapshot(db, to_snapshot)
    return {
        "from_snapshot": from_snapshot, "to_snapshot": to_snapshot,
        "main_version": sa.timetable_main_version,
        "new_in_b": sorted(sigs_b - sigs_a),
        "lost_in_b": sorted(sigs_a - sigs_b),
        "common": len(sigs_a & sigs_b),
    }


def _trip_signatures_for_snapshot(db: DbSession, snap_id: str) -> set[str]:
    return {
        s
        for (s,) in db.execute(
            select(JourneyTrip.trip_signature)
            .join(JourneySearchExecution, JourneySearchExecution.id == JourneyTrip.execution_id)
            .where(JourneySearchExecution.graph_snapshot_id == snap_id)
            .distinct()
        ).all()
    }


# ────────────────────────── unmatched-trips diagnostic ──────────────────────────


@router.get("/unmatched-trips")
def unmatched_trips(
    db: Annotated[DbSession, Depends(get_db)],
    _: Annotated[CurrentUser, Depends(require_platform_admin)],
    since: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
) -> list[dict[str, Any]]:
    """Trips whose signature appears in only ONE session's execution for a given search."""
    cutoff = _since_dt(since)
    rows = db.execute(
        select(
            JourneyTrip.trip_signature,
            JourneySearchExecution.search_id,
            func.array_agg(func.distinct(JourneySearchExecution.session_id)).label("sessions"),
        )
        .join(JourneySearchExecution, JourneySearchExecution.id == JourneyTrip.execution_id)
        .join(JourneySearch, JourneySearch.id == JourneySearchExecution.search_id)
        .where(JourneySearch.ts >= cutoff)
        .group_by(JourneyTrip.trip_signature, JourneySearchExecution.search_id)
        .having(func.count(func.distinct(JourneySearchExecution.session_id)) == 1)
        .limit(limit)
    ).all()
    return [
        {"trip_signature": s, "search_id": str(sid), "session": (sessions or [None])[0]}
        for s, sid, sessions in rows
    ]


# ────────────────────────── CSV exports ──────────────────────────


@router.get("/searches.csv")
def searches_csv(
    db: Annotated[DbSession, Depends(get_db)],
    _: Annotated[CurrentUser, Depends(require_platform_admin)],
    since: str | None = Query(None),
) -> StreamingResponse:
    cutoff = _since_dt(since)
    rows = db.execute(
        select(JourneySearch).where(JourneySearch.ts >= cutoff).order_by(desc(JourneySearch.ts))
    ).scalars().all()

    def gen():  # type: ignore[no-untyped-def]
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["id","ts","user_id","endpoint","from_lat","from_lon","to_lat","to_lon",
                    "modes","status","total_response_ms","total_trips_unique"])
        yield buf.getvalue(); buf.seek(0); buf.truncate(0)
        for s in rows:
            w.writerow([str(s.id), s.ts.isoformat(), str(s.user_id) if s.user_id else "",
                        s.endpoint, s.origin_lat, s.origin_lon, s.dest_lat, s.dest_lon,
                        s.modes, s.status, s.total_response_ms or "", s.total_trips_unique or ""])
            yield buf.getvalue(); buf.seek(0); buf.truncate(0)

    return StreamingResponse(
        gen(), media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="viator-searches.csv"'},
    )


# Imported lazily to avoid pulling pg-specific types when models module loads.
from sqlalchemy import Integer as _INTEGER, Numeric as _NUMERIC  # noqa: E402
