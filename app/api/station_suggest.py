"""Station suggestions for the journey typeahead: `POST /api/stations/suggest`.

Open to every logged-in user (`require_logged_in`): an end user is served,
which the old `GET /api/master/stations` (content managers and
administrators only, removed in MSMM step 3) never did.

The checks of the body (`query_or_422`) and the search itself
(`find_stations`: the module, then VIATOR's own list) are shared with the
"Stations" admin page's `POST /api/master/stations/search`
(app/api/master/stations.py), which differs only in its gate and in the
shape of its answer.

The text `q` arrives in the JSON body, never in the URL, and is validated
with the rules of the Multimodal Station Mapping module's search
(`normalise_query`): Unicode NFC, any control character or lone surrogate
refused, white space trimmed and runs of it made one space, then 3 to 100
characters; last, the text must still hold 3 characters once folded as the
module folds it (`module_fold`: its hyphens, apostrophes and punctuation
made spaces, decision 64 of the module), so "St.", "---" or "( )" are
refused here, as a text of two characters is, and never sent. The
normalised text is the one VIATOR sends. The module itself refuses a text
of fewer than 3 characters once folded with a 422; should one still happen
(a module whose rules moved), it is logged at INFO with the reason word
only and answered as VIATOR's own refusal of the text, a 422, never by the
fallback (`find_stations`).

**Every refusal of the body is a 422 with a fixed sentence, never the
framework's.** FastAPI's own 422 copies the refused input into its answer;
JSON can carry a lone surrogate (`\\ud800`) anywhere, in a value or a field
name, which no UTF-8 answer can hold, so that answer would itself fail with
a 500. The route therefore takes the parsed JSON as it comes and checks it
against `SuggestBody` itself. The order stays the framework's: a body sent
as JSON that does not parse is refused before the login check (its answer
never holds the input); any other body is checked only once the user is
known, so an anonymous request still gets 401 whatever its body (a body sent
as another type, `text/plain` for one, included).

Where the stations come from:

1. **The module** (app/station_module.py), when it is configured and the
   user has a VIATOR user id: its rows are returned as they come, tagged
   `source: "msmm"` — an empty list included.
2. **VIATOR's own `master_stations` list otherwise** (the module not
   configured, paused, failing, or a user without an id; never for a text
   the module refused with a 422), with the same
   guards: name contains `q` (its `%`, `_` and backslash literal) or UIC
   equals `q`, rows with a position only, ordered by `(country_iso, name)`,
   at most 10 rows, tagged `source: "viator"`.
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
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, ValidationError
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

# The characters the station module's search reads as a space between two
# words: a mirror of MSMM app/master/station_search.py `_HYPHENS`,
# `_APOSTROPHES` and `FOLDED_PUNCTUATION` (decision 64 of the module: the
# comma, semicolon, colon, full stop, slash, backslash, round, square and
# curly brackets, straight double quote, exclamation and question marks,
# asterisk, plus sign and ampersand; the French and German quotation marks,
# the ellipsis, the inverted exclamation and question marks, the fraction
# slash). Its fold makes each of them, and white space, a space. VIATOR
# cannot import the module's code, so they are copied here once, for the
# text rule below and the coverage hubs' name shortening; change them with
# the module's (a test pins the copy and compares it with the module's
# source when it can read it).
MODULE_HYPHENS = "-\u2010\u2011\u2012\u2013\u2014\u2212"
MODULE_APOSTROPHES = "'\u2018\u2019\u02bc`\u00b4"
MODULE_PUNCTUATION = (
    ',;:./\\()[]{}"!?*+&'
    "\u00ab\u00bb\u2039\u203a\u201c\u201d\u201e\u201f\u201a\u201b\u2026\u00a1\u00bf\u2044"
)
MODULE_SEPARATORS = MODULE_HYPHENS + MODULE_APOSTROPHES + MODULE_PUNCTUATION
# The rest of the module's fold that can change a text's length (its
# `_COMBINING_MARKS`, `FOLDED_GONE` and the letters of `FOLDED_LETTERS` it
# spells with two): the combining marks taken off once the text is
# decomposed, the soft hyphen taken out, and four letters spelt with two
# (the others it spells with one, which leaves the length as it is).
_MODULE_COMBINING_MARKS = re.compile(
    "[\u0300-\u036f\u1ab0-\u1aff\u1dc0-\u1dff\u20d0-\u20ff\ufe20-\ufe2f]"
)
MODULE_GONE = "\u00ad"
MODULE_LONG_LETTERS = {"\u00df": "ss", "\u00e6": "ae", "\u0153": "oe", "\u00fe": "th"}
_MODULE_FOLD_TABLE = str.maketrans(
    {
        **MODULE_LONG_LETTERS,
        **dict.fromkeys(MODULE_SEPARATORS, " "),
        **dict.fromkeys(MODULE_GONE),
    }
)

# The description of the route's 422 in its OpenAPI answers.
REFUSED_TEXT = (
    "The text is not one the station search accepts: 3 to 100 characters once "
    "normalised, still 3 once punctuation, hyphens and apostrophes are read as "
    "spaces, no control character, no lone surrogate. Or the body is not "
    '{"q": <text>} and nothing else.'
)

# The 422 of a text that folds, as the module folds it, to fewer than 3
# characters; also the answer when the module itself refuses a text (its 422).
FOLD_REFUSED = (
    f"q must still be {QUERY_MIN} characters long once punctuation, hyphens and "
    "apostrophes are read as spaces"
)

# The 422 of a body that is not `{"q": <text>}` and nothing else.
_BODY_REFUSED = 'The body must be a JSON object {"q": <text>} and nothing else.'

# Control characters (Cc), and lone surrogates (Cs): JSON can carry `\ud800`,
# which no UTF-8 text can hold. Sent on, it makes the module fail with a 500,
# which pauses the module for every user: one user must not be able to.
_REFUSED_CATEGORIES = frozenset({"Cc", "Cs"})


def module_fold(text: str) -> str:
    """`text` folded as the module's search folds it, for its length.

    The module's `fold_name`: decomposed (NFD), its combining marks taken
    off, in small letters, every character of `MODULE_SEPARATORS` and white
    space a space, the soft hyphen taken out, every run of spaces one, none
    at either end. Of its letters spelt plain, only those spelt with two
    (`MODULE_LONG_LETTERS`) are spelt here: the others are one letter for
    one, so the length is the module's. "St." gives "st", "( )" nothing,
    "Zz, Zzhof" "zz zzhof"."""
    decomposed = _MODULE_COMBINING_MARKS.sub("", unicodedata.normalize("NFD", text))
    spaced = _WHITE_SPACE.sub(" ", decomposed.lower().translate(_MODULE_FOLD_TABLE))
    return spaced.strip(" ")


def normalise_query(text: str) -> str:
    """The text as the module searches for it, or ValueError.

    Unicode NFC; any control character (category Cc, tab and line end
    included) or lone surrogate (Cs) refuses it wherever it stands; spaces
    at both ends removed and every run of white space made one space; then 3
    to 100 characters; last, still 3 characters once folded as the module
    folds it (`module_fold`): a text such as "St.", "---" or "( )" folds to
    fewer, and the module would refuse it with a 422.
    """
    composed = unicodedata.normalize("NFC", text)
    if any(unicodedata.category(character) in _REFUSED_CATEGORIES for character in composed):
        raise ValueError("q must not contain a control character or a lone surrogate")
    joined = _WHITE_SPACE.sub(" ", composed).strip(" ")
    if not QUERY_MIN <= len(joined) <= QUERY_MAX:
        raise ValueError(f"q must be {QUERY_MIN} to {QUERY_MAX} characters long")
    if len(module_fold(joined)) < QUERY_MIN:
        raise ValueError(FOLD_REFUSED)
    return joined


def escape_like(text: str) -> str:
    """`text` with the three characters LIKE reads specially escaped."""
    return text.replace(_ESCAPE, _ESCAPE * 2).replace("%", "\\%").replace("_", "\\_")


class SuggestBody(BaseModel):
    """`{"q": <text>}`; nothing else. The text is normalised by the route."""

    model_config = ConfigDict(extra="forbid", strict=True)

    q: str


def _body_or_422(given: Any) -> SuggestBody:
    """`given` (the parsed JSON body) as a `SuggestBody`, or a 422 with a
    fixed sentence. pydantic's error is dropped: it holds the input, which
    may be a lone surrogate that no UTF-8 answer can carry."""
    try:
        return SuggestBody.model_validate(given)
    except ValidationError:
        raise HTTPException(status_code=422, detail=_BODY_REFUSED) from None


def _normalised_or_422(text: str) -> str:
    """`normalise_query`, its refusal a 422 with a fixed sentence.

    Not a pydantic validator: the framework's 422 copies the refused input
    into its answer, and a lone surrogate cannot be written as UTF-8 JSON,
    so that answer itself would fail with a 500."""
    try:
        return normalise_query(text)
    except ValueError as refused:
        raise HTTPException(status_code=422, detail=str(refused)) from None


def query_or_422(given: Any) -> str:
    """The text of the body `given` (the parsed JSON), normalised, or a 422
    with a fixed sentence: `{"q": <text>}` and nothing else, then the
    module's text rules (`normalise_query`)."""
    return _normalised_or_422(_body_or_422(given).q)


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


# Where an answer's stations come from: the module, or VIATOR's own
# Trainline list (the fallback).
Origin = Literal["msmm", "trainline"]


async def find_stations(
    db: DbSession, q: str, user: CurrentUser
) -> tuple[Origin, list[dict[str, Any]]]:
    """At most ten stations for the normalised text `q`, and where they come from.

    The module first, on behalf of `user` (its counters are this person's);
    its answer is final, an empty one included. VIATOR's own list when the
    module is not configured, paused or failing, or when the user has no
    VIATOR id (the basic-auth shadow user): the rows of `fallback_rows`.
    A text the module refuses (its 422: the same text always gets the same
    answer) is not a failure: it is answered as VIATOR refuses a text, a
    422 with `FOLD_REFUSED`, never with VIATOR's list.
    """
    if user.id is not None:
        outcome = await station_module.search_outcome(q, user.id)
        if outcome.rows is not None:
            return "msmm", outcome.rows
        if outcome.reason == station_module.TEXT_REFUSED:
            raise HTTPException(status_code=422, detail=FOLD_REFUSED)
    return "trainline", await run_in_threadpool(fallback_rows, db, q)


@router.post(
    "/suggest",
    responses={422: {"description": REFUSED_TEXT}},
    # The body is declared `Any` below so that the framework never validates
    # it (see the module's docstring); the published schema stays the model's.
    openapi_extra={
        "requestBody": {
            "content": {"application/json": {"schema": SuggestBody.model_json_schema()}},
            "required": True,
        }
    },
)
async def suggest(
    user: Annotated[CurrentUser, Depends(require_logged_in)],
    db: Annotated[DbSession, Depends(get_db)],
    given: Annotated[Any, Body()] = None,
) -> list[dict[str, Any]]:
    """At most ten stations whose name contains `q`, or whose UIC is `q`."""
    origin, rows = await find_stations(db, query_or_422(given), user)
    if origin == "msmm":
        return [{**row, "source": "msmm"} for row in rows]
    return rows
