"""The "Stations" admin page's API: search, and the Trainline refresh.

Since MSMM step 3 the station module is the reference for a station, and
station edits are made only there (decisions 52 to 55 of the module's
design). This router therefore has two routes, both for content managers and
platform administrators (`require_content_manager`):

- `POST /api/master/stations/search`: at most ten stations for a text of 3
  characters or more. It is the journey typeahead's search
  (app/api/station_suggest.py) under another gate: the same body check
  (`query_or_422`), the same call of the module on behalf of the person, so
  the same per-person counters at the module, and the same fallback on
  VIATOR's own Trainline list (`find_stations`). The answer says once where
  the stations come from: `{"origin": "msmm" | "trainline", "stations":
  [...]}`. An empty answer of the module is `msmm` with no station: it is
  never topped up with Trainline rows. No paging, no total, no list: the
  table cannot be read off the page. No slowapi limit, as on the typeahead
  (behind nginx it would be one counter for the whole site); the limits are
  the module's.
- `POST /api/master/stations/refresh-trainline`: the Trainline import on
  demand, which keeps the fallback list and VIATOR's internal joins fresh.

The paged list (`GET`), the edit (`PATCH /{uic}`) and the drift queue
(`GET /drift`, `POST /{uic}/drift/resolve`) are gone. The edits made before
were archived by revision 20261010_1200_edit_archive.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Request
from sqlalchemy.orm import Session as DbSession

from ... import audit
from ...db import get_db
from ...master import trainline
from ...security import CurrentUser, client_ip, require_content_manager
from .. import station_suggest

router = APIRouter(prefix="/api/master/stations", tags=["master", "stations"])

# The fields of a station on the page, from either origin: those of the
# typeahead's rows, the only ones the module gives.
STATION_FIELDS = ("name", "latitude", "longitude", "country_iso", "uic")

_FORBIDDEN = {"description": "Content-manager or platform-admin access required."}


def _shown(row: dict[str, Any]) -> dict[str, Any]:
    """A station row in the page's shape: the five fields, nothing else."""
    return {field: row.get(field) for field in STATION_FIELDS}


@router.post(
    "/search",
    responses={
        403: _FORBIDDEN,
        422: {"description": station_suggest.REFUSED_TEXT},
    },
    # The body is declared `Any` below so that the framework never validates
    # it (see app/api/station_suggest.py); the published schema is the model's.
    openapi_extra={
        "requestBody": {
            "content": {
                "application/json": {"schema": station_suggest.SuggestBody.model_json_schema()}
            },
            "required": True,
        }
    },
)
async def search_stations(
    user: Annotated[CurrentUser, Depends(require_content_manager)],
    db: Annotated[DbSession, Depends(get_db)],
    given: Annotated[Any, Body()] = None,
) -> dict[str, Any]:
    """At most ten stations whose name contains `q`, or whose code is `q`,
    and where they come from."""
    q = station_suggest.query_or_422(given)
    origin, rows = await station_suggest.find_stations(db, q, user)
    return {"origin": origin, "stations": [_shown(row) for row in rows]}


@router.post("/refresh-trainline", responses={403: _FORBIDDEN})
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
