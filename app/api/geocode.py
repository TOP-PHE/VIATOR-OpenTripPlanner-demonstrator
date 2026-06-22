"""MOTIS geocoder proxy for the journey-form typeahead.

The journey UI's existing /api/master/stations typeahead is bootstrapped
from Trainline-eu/stations (rail mainline stations only — ~50k entries).
For sessions whose GTFS feed covers urban transit too (e.g. the full
Swiss national feed, which includes BVB Basel trams + Saint-Louis cross-
border tram terminus + every VBZ, TPG, etc. stop), MOTIS's own geocoder
already knows every stop — but the UI has no way to surface them today.

This endpoint forwards a free-text query to the first serving MOTIS
session's `/api/v1/geocode`, normalises the response to the same row
shape the typeahead's master_stations consumer expects
(`{name, latitude, longitude, country_iso, uic}`), and is intended to
be merged with the master_stations result list client-side.

Returns an empty list — never a 5xx — if no MOTIS session is serving or
its geocoder is unreachable. The typeahead degrades gracefully to its
master_stations-only behaviour, which is exactly what users get today.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..db import get_db
from ..models import Session as SessionRow
from ..models import SessionState
from ..security import CurrentUser, require_logged_in

router = APIRouter(prefix="/api/geocode", tags=["geocode"])

log = logging.getLogger(__name__)

# Tight timeout — the typeahead is on the keystroke critical path.
# If MOTIS is slow we'd rather return master_stations alone than make
# every keystroke wait. 1.5 s is generous for an in-cluster HTTP hop.
_GEOCODE_TIMEOUT_S = 1.5


def _pick_motis_session(db: DbSession) -> SessionRow | None:
    """Pick the MOTIS session whose geocoder to query.

    Strategy: first serving + fanout-enabled MOTIS session, ordered by id
    for stability. A more sophisticated picker (geographic affinity,
    user-selected default, query all and merge) is a follow-up.
    """
    return db.execute(
        select(SessionRow)
        .where(SessionRow.engine == "motis")
        .where(SessionRow.state == SessionState.SERVING.value)
        .where(SessionRow.include_in_fanout.is_(True))
        .order_by(SessionRow.id)
        .limit(1)
    ).scalar_one_or_none()


def _normalize_hit(item: Any) -> dict[str, Any] | None:
    """Map one MOTIS geocoder hit to the typeahead row shape.

    MOTIS returns mixed-type results (STOP, ADDRESS, PLACE, ...). We
    surface only STOPs — addresses and POIs aren't useful for journey
    planning and the typeahead picker doesn't know what to do with them.
    """
    if not isinstance(item, dict):
        return None
    if item.get("type") != "STOP":
        return None
    lat = item.get("lat")
    lon = item.get("lon")
    if not isinstance(lat, int | float) or not isinstance(lon, int | float):
        return None
    name = item.get("name")
    if not isinstance(name, str) or not name:
        return None
    return {
        "name": name,
        "latitude": float(lat),
        "longitude": float(lon),
        # MOTIS exposes ISO 3166-1 alpha-2 in `country` for most stops;
        # for some cross-border ones (e.g. Saint-Louis) it's absent — the
        # typeahead handles a null country_iso (just renders without the
        # `[CH]` chip).
        "country_iso": item.get("country"),
        # MOTIS stop ids aren't UIC — the journey form falls back to
        # lat/lon-based routing when uic is null, which is exactly what
        # we want here.
        "uic": None,
        # Tag the source so the UI can distinguish curated rail stops
        # from MOTIS-only ones (e.g. render a tram-icon badge later).
        "source": "motis",
    }


@router.get("", response_model=list[dict[str, Any]])
async def geocode(
    db: Annotated[DbSession, Depends(get_db)],
    _: Annotated[CurrentUser, Depends(require_logged_in)],
    q: Annotated[str, Query(min_length=2, max_length=200)],
    size: Annotated[int, Query(ge=1, le=50)] = 20,
) -> list[dict[str, Any]]:
    """Proxy a free-text query to the first serving MOTIS session.

    Returns up to `size` STOP-type rows shaped like master_stations:
        {name, latitude, longitude, country_iso, uic, source}

    The endpoint never raises — a missing or unhealthy MOTIS session
    returns `[]` so the typeahead degrades to master_stations alone.
    """
    motis = _pick_motis_session(db)
    if motis is None:
        return []

    url = f"http://motis-{motis.id}:8080/api/v1/geocode"
    try:
        async with httpx.AsyncClient(timeout=_GEOCODE_TIMEOUT_S) as c:
            r = await c.get(url, params={"text": q})
    except httpx.HTTPError as exc:
        log.warning("MOTIS geocoder unreachable for session %s: %s", motis.id, exc)
        return []
    if r.status_code != 200:
        log.warning("MOTIS geocoder returned %s for session %s", r.status_code, motis.id)
        return []

    try:
        payload = r.json()
    except ValueError:
        log.warning("MOTIS geocoder returned non-JSON for session %s", motis.id)
        return []
    if not isinstance(payload, list):
        return []

    out: list[dict[str, Any]] = []
    for item in payload:
        row = _normalize_hit(item)
        if row is None:
            continue
        out.append(row)
        if len(out) >= size:
            break
    return out
