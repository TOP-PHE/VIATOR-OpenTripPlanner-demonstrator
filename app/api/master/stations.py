"""Master stations + drift API. See spec §9.9."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy import func, or_, select
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
    # Dedicated operator codes — frequently queried, displayed as columns
    # in the admin UI's main station table.
    trigramme_sncf: str | None
    db_code: str | None
    trenitalia_code: str | None
    renfe_code: str | None
    atoc_code: str | None
    # Catch-all JSONB for less common operator codes — OBB, SBB, NTV,
    # Trenord, Cercanías, Entur, Westbahn, Flixbus, Benerail, etc. UI
    # renders dynamically via small operator badges.
    other_codes: dict[str, str]
    is_main_station: bool
    source: str
    has_drift: bool
    # True when this row matches the search query (only meaningful in
    # `context` mode — see list_stations). The UI uses this to highlight
    # matching rows while keeping their alphabetical neighbours visible.
    is_match: bool = False

    @classmethod
    def from_orm_with_drift(
        cls,
        s: MasterStation,
        drift_uics: set[str],
        *,
        is_match: bool = False,
    ) -> StationResponse:
        return cls(
            uic=s.uic,
            name=s.name,
            country_iso=s.country_iso,
            latitude=s.latitude,
            longitude=s.longitude,
            trigramme_sncf=s.trigramme_sncf,
            db_code=s.db_code,
            trenitalia_code=s.trenitalia_code,
            renfe_code=s.renfe_code,
            atoc_code=s.atoc_code,
            other_codes=s.other_codes or {},
            is_main_station=s.is_main_station,
            source=s.source,
            has_drift=s.uic in drift_uics,
            is_match=is_match,
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
    response: Response,
    db: Annotated[DbSession, Depends(get_db)],
    _: Annotated[CurrentUser, Depends(require_content_manager)],
    q: str | None = Query(None, description="Substring of name (case-insensitive)"),
    country: str | None = Query(None, max_length=2),
    page: int = Query(0, ge=0),
    size: int = Query(50, ge=1, le=500),
    mode: str = Query(
        "filter",
        pattern="^(filter|context)$",
        description=(
            "filter (default): hides non-matching rows. "
            "context: returns the page containing the first alphabetical match "
            "(or the requested `page` if no match), with `is_match` set on rows "
            "that satisfy the query — lets the UI highlight matches in place "
            "while keeping their alphabetical neighbours visible."
        ),
    ),
) -> list[StationResponse]:
    """List master stations with pagination.

    Headers:
        X-Total-Count: total rows matching the country filter (in `filter`
                       mode this is also constrained by `q`; in `context`
                       mode `q` doesn't shrink the universe — it only
                       drives where the page lands and which rows are
                       flagged is_match).
        X-Match-Count: when `q` is set, how many rows match across the
                       entire (country-filtered) universe. In `context`
                       mode, useful for "N matches across all pages".
        X-Match-Page:  when `q` is set in context mode, the page index
                       containing the first match (the same `page` the
                       endpoint navigated to unless overridden by the
                       caller). Lets the UI present "showing matches on
                       page X of Y" without a second round-trip.

    Sorting is `(country_iso, name)`, stable across requests.
    """
    base_filter = select(MasterStation)
    if country:
        base_filter = base_filter.where(MasterStation.country_iso == country.upper())

    match_clause = None
    if q:
        like = f"%{q}%"
        match_clause = or_(MasterStation.name.ilike(like), MasterStation.uic.ilike(like))

    # Universe = the alphabetical list the UI is paging through. In filter
    # mode, the universe shrinks to matches; in context mode, it doesn't.
    universe = base_filter
    if mode == "filter" and match_clause is not None:
        universe = universe.where(match_clause)

    # Total for X-Total-Count — the full universe size, not just this page.
    total = db.execute(select(func.count()).select_from(universe.subquery())).scalar_one()
    response.headers["X-Total-Count"] = str(total)

    match_count = 0
    match_page = page
    if q and match_clause is not None:
        # How many matches across the (country-filtered) universe.
        matches_universe = base_filter.where(match_clause)
        match_count = db.execute(
            select(func.count()).select_from(matches_universe.subquery())
        ).scalar_one()
        response.headers["X-Match-Count"] = str(match_count)

        if mode == "context" and match_count > 0:
            # Find the alphabetical position of the first match within the
            # full base_filter universe, so we can compute which page to
            # jump to. Postgres-side: count rows that come strictly before
            # the first match in (country_iso, name) order.
            first_match = db.execute(
                base_filter.where(match_clause)
                .order_by(MasterStation.country_iso, MasterStation.name)
                .limit(1)
            ).scalar_one_or_none()
            if first_match is not None:
                # Rows alphabetically before the first match.
                before_filter = base_filter.where(
                    or_(
                        MasterStation.country_iso < first_match.country_iso,
                        (MasterStation.country_iso == first_match.country_iso)
                        & (MasterStation.name < first_match.name),
                    )
                )
                before_count = db.execute(
                    select(func.count()).select_from(before_filter.subquery())
                ).scalar_one()
                match_page = before_count // size
        response.headers["X-Match-Page"] = str(match_page)

    # When the caller didn't pin a page (page=0 default) and we've computed
    # a context-mode jump page, navigate there. If they explicitly requested
    # a page, respect it (lets the UI flip pages while keeping the search).
    effective_page = match_page if (mode == "context" and page == 0 and q) else page

    rows = (
        db.execute(
            universe.order_by(MasterStation.country_iso, MasterStation.name)
            .offset(effective_page * size)
            .limit(size)
        )
        .scalars()
        .all()
    )
    drift_uics = {d.uic for d in db.execute(select(MasterStationPendingDrift)).scalars().all()}

    # In context mode, flag rows that match the query so the UI can render
    # a highlight class. In filter mode, every row is by definition a match,
    # so we set is_match=True on all of them for consistency.
    def _is_match(s: MasterStation) -> bool:
        if not q:
            return False
        if mode == "filter":
            return True
        ql = q.lower()
        return ql in (s.name or "").lower() or ql in (s.uic or "").lower()

    return [StationResponse.from_orm_with_drift(s, drift_uics, is_match=_is_match(s)) for s in rows]


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
