"""Station suggestions for the journey typeahead: `POST /api/stations/suggest`.

Open to every logged-in user (`require_logged_in`): an end user is served,
which `GET /api/master/stations` (content managers and administrators only)
never did.

The text `q` arrives in the JSON body, never in the URL, and is validated
with exactly the rules of the Multimodal Station Mapping module's search
(`normalise_query`): Unicode NFC, any control character refused, white
space trimmed and runs of it made one space, then 3 to 100 characters. So
no text VIATOR accepts can earn a 422 from the module, and the normalised
text is the one VIATOR sends. A lone surrogate is refused as well, which the
module's rules do not yet say: it would make the module fail with a 500.

Where the stations come from:

1. **The module** (app/station_module.py), when it is configured and the
   user has a VIATOR user id: its rows are returned as they come, tagged
   `source: "msmm"` — an empty list included.
2. **VIATOR's own `master_stations` list otherwise** (the module not
   configured, paused, failing, or a user without an id), with the same
   guards: name contains `q` (its `%`, `_` and backslash literal) or UIC
   equals `q`, rows with a position only, ordered by `(country_iso, name)`
   like the admin station list, at most 10 rows, tagged `source: "viator"`.
   Every value of `q` is a bound parameter, never in the statement's text,
   which VIATOR's SQLAlchemy tracing records.

**No slowapi limit on this route, on purpose.** VIATOR's limiter keys on
`request.client.host`, which behind nginx is nginx's own address for every
user (docs/architecture.md ch. 9, "Invariants & traps"): it would be one
counter for the whole site, which normal typing by a few people would use
up. The per-user and overall limits are the module's own; the fallback
serves VIATOR's Trainline list (ODbL), already public.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Executable, or_, select
from sqlalchemy.orm import Session as DbSession
from starlette.concurrency import run_in_threadpool

from .. import station_module
from ..db import get_db
from ..models import MasterStation
from ..security import CurrentUser, require_logged_in

router = APIRouter(prefix="/api/stations", tags=["stations"])

# The search text once normalised: 3 to 100 characters (the module's rule).
QUERY_MIN = 3
QUERY_MAX = 100

# At most ten stations per answer, from either source.
MAX_ROWS = 10

# The escape character of the fallback's LIKE pattern.
_ESCAPE = "\\"

_WHITE_SPACE = re.compile(r"\s+")

# The description of the route's 422 in its OpenAPI answers.
_REFUSED_TEXT = (
    "The text is not one the station search accepts: 3 to 100 characters once "
    "normalised, no control character, no lone surrogate."
)

# Control characters (Cc), and lone surrogates (Cs): JSON can carry `\ud800`,
# which no UTF-8 text can hold. Sent on, it makes the module fail with a 500,
# which pauses the module for every user: one user must not be able to.
_REFUSED_CATEGORIES = frozenset({"Cc", "Cs"})


def normalise_query(text: str) -> str:
    """The text as the module searches for it, or ValueError.

    Unicode NFC; any control character (category Cc, tab and line end
    included) or lone surrogate (Cs) refuses it wherever it stands; spaces
    at both ends removed and every run of white space made one space; then 3
    to 100 characters.
    """
    composed = unicodedata.normalize("NFC", text)
    if any(unicodedata.category(character) in _REFUSED_CATEGORIES for character in composed):
        raise ValueError("q must not contain a control character or a lone surrogate")
    joined = _WHITE_SPACE.sub(" ", composed).strip(" ")
    if not QUERY_MIN <= len(joined) <= QUERY_MAX:
        raise ValueError(f"q must be {QUERY_MIN} to {QUERY_MAX} characters long")
    return joined


def escape_like(text: str) -> str:
    """`text` with the three characters LIKE reads specially escaped."""
    return text.replace(_ESCAPE, _ESCAPE * 2).replace("%", "\\%").replace("_", "\\_")


class SuggestBody(BaseModel):
    """`{"q": <text>}`; nothing else. The text is normalised by the route."""

    model_config = ConfigDict(extra="forbid", strict=True)

    q: str


def _normalised_or_422(text: str) -> str:
    """`normalise_query`, its refusal a 422 with a fixed sentence.

    Not a pydantic validator: the framework's 422 copies the refused input
    into its answer, and a lone surrogate cannot be written as UTF-8 JSON,
    so that answer itself would fail with a 500."""
    try:
        return normalise_query(text)
    except ValueError as refused:
        raise HTTPException(status_code=422, detail=str(refused)) from None


def fallback_statement(q: str) -> Executable:
    """The query of VIATOR's own stations for `q`; `q` only as bound parameters."""
    return (
        select(
            MasterStation.name,
            MasterStation.latitude,
            MasterStation.longitude,
            MasterStation.country_iso,
            MasterStation.uic,
        )
        .where(
            or_(
                MasterStation.name.ilike(f"%{escape_like(q)}%", escape=_ESCAPE),
                MasterStation.uic == q,
            )
        )
        # The typeahead drops a row without a position; it would only take a place.
        .where(MasterStation.latitude.is_not(None), MasterStation.longitude.is_not(None))
        .order_by(MasterStation.country_iso, MasterStation.name)
        .limit(MAX_ROWS)
    )


def fallback_rows(db: DbSession, q: str) -> list[dict[str, Any]]:
    """VIATOR's own stations for `q`: at most ten, ordered by country then name."""
    return [
        {
            "name": name,
            "latitude": latitude,
            "longitude": longitude,
            "country_iso": country_iso,
            "uic": uic,
            "source": "viator",
        }
        for name, latitude, longitude, country_iso, uic in db.execute(fallback_statement(q)).all()
    ]


@router.post(
    "/suggest",
    responses={422: {"description": _REFUSED_TEXT}},
)
async def suggest(
    body: SuggestBody,
    user: Annotated[CurrentUser, Depends(require_logged_in)],
    db: Annotated[DbSession, Depends(get_db)],
) -> list[dict[str, Any]]:
    """At most ten stations whose name contains `q`, or whose UIC is `q`."""
    q = _normalised_or_422(body.q)
    if user.id is not None:
        rows = await station_module.search(q, user.id)
        if rows is not None:
            return [{**row, "source": "msmm"} for row in rows]
    return await run_in_threadpool(fallback_rows, db, q)
