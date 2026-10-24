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
  never topped up with Trainline rows. Every caller has a VIATOR user id:
  the gate admits JWT users only, and a JWT's subject must be a UUID. (The
  legacy basic-auth shadow user has no id and cannot reach the route; the
  shared search would give it Trainline without asking the module, and the
  page would then say the module did not answer.) No paging, no total, no list: the
  table cannot be read off the page. No slowapi limit, as on the typeahead
  (behind nginx it would be one counter for the whole site); the limits are
  the module's.

  Each station has the five fields of the typeahead's rows (`name`,
  `latitude`, `longitude`, `country_iso`, `uic`; `uic` null for a station
  the module serves by its VIATOR id alone) and, from the module, the
  fields of its referential (the contract's phase 2): `viator_id`,
  `uic_origin` (the origin label of the code) with `uic_origin_text` (the
  label in clear words) and `licence` (the licence tags of the name, the
  position and the code, the share-alike fields, the attribution texts),
  all null on a Trainline row or from a module that does not give them.
  The page puts each of them in as text.
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
from starlette.concurrency import run_in_threadpool

from ... import audit, station_module
from ...db import get_db
from ...master import trainline
from ...security import CurrentUser, client_ip, require_content_manager
from .. import station_suggest

router = APIRouter(prefix="/api/master/stations", tags=["master", "stations"])

# The fields of a station on the page, from either origin: those of the
# typeahead's rows, then those of the module's referential (None on a
# Trainline row); `uic_origin_text` is added from `uic_origin`.
STATION_FIELDS = (
    "name",
    "latitude",
    "longitude",
    "country_iso",
    "uic",
    "viator_id",
    "uic_origin",
    "licence",
)

_FORBIDDEN = {"description": "Content-manager or platform-admin access required."}


def _shown(row: dict[str, Any]) -> dict[str, Any]:
    """A station row in the page's shape: the fields of `STATION_FIELDS` and
    the origin label in clear words, nothing else."""
    shown = {field: row.get(field) for field in STATION_FIELDS}
    shown["uic_origin_text"] = station_module.uic_origin_text(shown["uic_origin"])
    return shown


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
    await run_in_threadpool(_audit_refresh, db, actor, client_ip(request), counts)
    return counts


def _audit_refresh(
    db: DbSession, actor: CurrentUser, ip: str | None, counts: dict[str, int]
) -> None:
    """The refresh's audit event, committed, in the thread pool (#397)."""
    audit.record(
        db,
        action="master_stations.refresh.trainline",
        actor_user_id=actor.id,
        actor_ip=ip,
        metadata=counts,
    )
    db.commit()
