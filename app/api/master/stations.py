"""Master stations + drift API. See spec §9.9."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.orm import Session as DbSession

from ... import audit
from ...db import get_db
from ...master import trainline
from ...models import MasterStation, MasterStationPendingDrift
from ...security import CurrentUser, client_ip, require_content_manager

router = APIRouter(prefix="/api/master/stations", tags=["master", "stations"])


class StationResponse(BaseModel):
    uic: str
    name: str
    country_iso: str | None
    latitude: float | None
    longitude: float | None
    trigramme_sncf: str | None
    db_code: str | None
    trenitalia_code: str | None
    is_main_station: bool
    source: str
    has_drift: bool

    @classmethod
    def from_orm_with_drift(cls, s: MasterStation, drift_uics: set[str]) -> StationResponse:
        return cls(
            uic=s.uic,
            name=s.name,
            country_iso=s.country_iso,
            latitude=s.latitude,
            longitude=s.longitude,
            trigramme_sncf=s.trigramme_sncf,
            db_code=s.db_code,
            trenitalia_code=s.trenitalia_code,
            is_main_station=s.is_main_station,
            source=s.source,
            has_drift=s.uic in drift_uics,
        )


class StationPatch(BaseModel):
    name: str | None = None
    country_iso: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    trigramme_sncf: str | None = None
    db_code: str | None = None


class DriftResolveBody(BaseModel):
    action: str  # 'keep_ours' | 'adopt_full' | 'adopt_fields'
    fields: list[str] | None = None


# ────────────────────────── search / list ──────────────────────────


@router.get("", response_model=list[StationResponse])
def list_stations(
    db: Annotated[DbSession, Depends(get_db)],
    _: Annotated[CurrentUser, Depends(require_content_manager)],
    q: str | None = Query(None, description="Substring of name (case-insensitive)"),
    country: str | None = Query(None, max_length=2),
    page: int = Query(0, ge=0),
    size: int = Query(50, ge=1, le=500),
) -> list[StationResponse]:
    stmt = select(MasterStation)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(MasterStation.name.ilike(like), MasterStation.uic.ilike(like)))
    if country:
        stmt = stmt.where(MasterStation.country_iso == country.upper())
    stmt = (
        stmt.order_by(MasterStation.country_iso, MasterStation.name).offset(page * size).limit(size)
    )

    rows = db.execute(stmt).scalars().all()
    drift_uics = {d.uic for d in db.execute(select(MasterStationPendingDrift)).scalars().all()}
    return [StationResponse.from_orm_with_drift(s, drift_uics) for s in rows]


@router.patch("/{uic}", response_model=StationResponse)
def patch_station(
    uic: str,
    body: StationPatch,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    actor: Annotated[CurrentUser, Depends(require_content_manager)],
) -> StationResponse:
    s = db.get(MasterStation, uic)
    if s is None:
        raise HTTPException(404, "Station not found")
    changes = {}
    for f, v in body.model_dump(exclude_none=True).items():
        if getattr(s, f) != v:
            changes[f] = {"from": getattr(s, f), "to": v}
            setattr(s, f, v)
    if changes:
        s.source = "manual"
        s.updated_at = datetime.now(UTC)
        audit.record(
            db,
            action="master_station.updated",
            actor_user_id=actor.id,
            actor_ip=client_ip(request),
            target_kind="master_station",
            target_id=uic,
            metadata={"changes": changes},
        )
    db.commit()
    return StationResponse.from_orm_with_drift(s, set())


# ────────────────────────── refresh + drift ──────────────────────────


@router.post("/refresh-trainline")
async def refresh_trainline(
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    actor: Annotated[CurrentUser, Depends(require_content_manager)],
) -> dict[str, int]:
    counts = await trainline.refresh(db)
    audit.record(
        db,
        action="master_stations.refresh.trainline",
        actor_user_id=actor.id,
        actor_ip=client_ip(request),
        metadata=counts,
    )
    db.commit()
    return counts


@router.get("/drift")
def list_drift(
    db: Annotated[DbSession, Depends(get_db)],
    _: Annotated[CurrentUser, Depends(require_content_manager)],
) -> list[dict[str, Any]]:
    rows = db.execute(select(MasterStationPendingDrift)).scalars().all()
    return [
        {
            "uic": r.uic,
            "fields_differing": list(r.fields_differing),
            "trainline_snapshot": r.trainline_snapshot,
            "detected_at": r.detected_at.isoformat() if r.detected_at else None,
        }
        for r in rows
    ]


@router.post("/{uic}/drift/resolve", response_model=StationResponse)
def resolve_drift(
    uic: str,
    body: DriftResolveBody,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    actor: Annotated[CurrentUser, Depends(require_content_manager)],
) -> StationResponse:
    drift = db.get(MasterStationPendingDrift, uic)
    if drift is None:
        raise HTTPException(404, "No pending drift for that UIC")
    s = db.get(MasterStation, uic)
    if s is None:
        raise HTTPException(404, "Station not found")

    snapshot = dict(drift.trainline_snapshot or {})
    if body.action == "adopt_full":
        for k, v in snapshot.items():
            if hasattr(s, k):
                setattr(s, k, v)
        s.source = "trainline"
    elif body.action == "adopt_fields":
        for k in body.fields or []:
            if k in snapshot and hasattr(s, k):
                setattr(s, k, snapshot[k])
    elif body.action != "keep_ours":
        raise HTTPException(400, "action must be one of: keep_ours, adopt_full, adopt_fields")

    db.delete(drift)
    audit.record(
        db,
        action=f"master_station.drift.{body.action}",
        actor_user_id=actor.id,
        actor_ip=client_ip(request),
        target_kind="master_station",
        target_id=uic,
        metadata={"fields": body.fields or list(snapshot.keys())},
    )
    db.commit()
    return StationResponse.from_orm_with_drift(s, set())
