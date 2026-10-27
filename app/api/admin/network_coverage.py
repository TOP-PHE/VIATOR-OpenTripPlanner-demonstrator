"""Network-coverage admin API (v0.1.27 / hub-set bumped to 26 in v0.1.28
/ DB-backed hubs in v0.1.31).

Endpoints:

  GET    /api/admin/network-coverage/hubs              — list active hubs
                                                         (v0.1.31: from DB
                                                         instead of hubs.py)
  POST   /api/admin/network-coverage/hubs              — v0.1.31: create hub
  PATCH  /api/admin/network-coverage/hubs/{id}         — v0.1.31: edit hub
  DELETE /api/admin/network-coverage/hubs/{id}         — v0.1.31: soft-delete
  POST   /api/admin/network-coverage/hubs/resolve      — station codes proposed by
                                                         the station module from each
                                                         hub's position (by its name
                                                         when nothing stands within
                                                         300 m), at most 10 hubs a
                                                         click; writes nothing
  POST   /api/admin/network-coverage/hubs/confirm      — store at most 10 codes the
                                                         administrator accepted, after
                                                         one lookup in the module
  POST   /api/admin/network-coverage/hubs/check        — re-read the stored codes of at
                                                         most 20 hubs a click; writes nothing
  POST   /api/admin/network-coverage/hubs/pick         — the hub picker (#398): at most 10
                                                         stations of the module for a typed
                                                         name in one country; writes nothing
  GET    /api/admin/network-coverage/hubs/countries    — the picker's countries (the module's,
                                                         else VIATOR's list)

  GET    /api/admin/network-coverage/runs              — list past runs
                                                         (newest first)
  POST   /api/admin/network-coverage/runs              — start new coverage run
  GET    /api/admin/network-coverage/runs/{id}         — fetch run + results

Authorization: platform_admin (the matrix consumes serious OTP capacity
when running, and old runs persist forever — content-manager doesn't
need this surface).

Execution (#392): POST /runs creates the row in pending state and returns;
web never executes a run. The worker's coverage executor
(`app/network_coverage/executor.py`) claims it, executes it and resumes it
after a restart. Stop writes `cancel_requested_at` (or cancels a run still
queued). The UI polls GET /runs/{id} every 5s to render progress from the
database; status flips to "completed" when all pairs have processed.
"""

from __future__ import annotations

import logging
import math
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from datetime import time as dtime
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy import func, not_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from ... import osm_geo, station_module
from ...config_schema import CONFIG_SCHEMA
from ...db import get_db
from ...db_steps import db_step
from ...models import (
    JourneySearchExecution,
    JourneyTrip,
    NetworkCoverageHub,
    NetworkCoverageResult,
    NetworkCoverageRun,
)
from ...models import Session as SessionRow
from ...models.sessions import SessionState
from ...network_coverage import executor as coverage_executor
from ...network_coverage import external_verify, hub_derive, runner, translit
from ...network_coverage.hubs import HUBS as STATIC_HUBS
from ...security import CurrentUser, require_platform_admin
from ...templating import templates
from .. import station_suggest

# PR-3 — "HH:MM" and "24:00" sentinel. The DB stores TIME (which can't
# represent 24:00), so the API accepts the sentinel and the runner
# translates it to end-of-day in `_resolve_run_window`. Pre-compiled
# regex so the validator is cheap on every request.
_HHMM_RE = re.compile(r"^(?:(?:[01]\d|2[0-3]):[0-5]\d|24:00)$")

router = APIRouter(
    prefix="/api/admin/network-coverage",
    tags=["admin", "network-coverage"],
)

# Shared 404 detail for runs lookups — used by the export, the run-detail,
# and the cell-trips endpoints. Constant so a future rename / i18n only
# touches one site (Sonar S1192).
_RUN_NOT_FOUND = "Run not found"

log = logging.getLogger(__name__)

# Shared 404 detail for cell lookups within a run — used by the
# verify-external endpoint. Mirrors the _RUN_NOT_FOUND pattern.
_CELL_NOT_FOUND = "Cell (origin,dest) not found in this run"

# Shared 404 detail for hub lookups by id — both the verify-external
# endpoint and any future per-hub action surfaces use this.
_HUB_NOT_FOUND = "Hub not found"

# ── Station codes from the station module (MSMM step 3) ──
# The batch of each click, constants of VIATOR's code (the module's own
# settings cannot be read from here): a resolve looks at most 10 hubs, a
# confirm makes one lookup of at most 10 codes, a check one lookup of at
# most 20. For one hub a resolve makes one near call at its position; when
# that finds nothing, one search with the hub's full name; when that keeps
# nothing either, up to NAME_SHORTEN_MAX searches with the name shortened
# word by word. So one hub costs 1 to RESOLVE_HUB_MAX_CALLS = 1 + 1 + 3 = 5
# calls. Each near call, each search and each code is one call on the
# administrator's own limits at the module (its defaults: 60 a minute and
# 2,000 a day for one person, MSMM app/settings.py internal_user_per_minute
# and internal_user_per_day).
# Ten hubs at 5 calls would be 50 calls, 60 with the confirm: the whole
# minute. So a click also has a budget of RESOLVE_CALL_BUDGET = 15 calls: it
# looks at the next hub only while the calls made so far plus that hub's own
# worst case (`_hub_worst_calls`: its near call if it has a usable position,
# its full name if searchable, and as many shortened forms as its name has,
# at most 3; 1 for a hub without position) stay within 15; the page then
# says why it stopped. Worst case of one resolve click: 15 calls (3 hubs,
# each at 1 near + 1 full name + 3 shortened names). A click whose near
# calls find stations still looks at 10 hubs for 10 calls; one of hubs
# whose names have no shortened form, found by their full names, at 7 hubs
# for 14 calls. Before the shortening the worst click was 20 calls (10 near
# + 10 searches): 3 such clicks in a row filled the 60 a minute (an
# administrator clicking Propose and Check in a row met the limit in
# production). Now 4 worst resolve clicks fit in a minute (4 x 15 = 60); a
# check click stays 20 (3 a minute). With its confirm (at most one code per
# hub looked at, so at most 10): 15 + 10 = 25 at the very worst. The
# module's per-person minute limit must be 20 or more (a check click).
# A near call also counts on the module's own near quota (decision 62 of
# the module: 30 a minute and 200 a day for one person, 500 a day for
# everybody; a search does not): 10 a click stays under the 30 a minute, so
# three clicks in one minute pass and a fourth is refused with a 429 and its
# Retry-After (clicks whose near calls find stations: 3 a minute, before and
# after the shortening). At 15 calls a click the day of 2,000 allows 133
# clicks.
RESOLVE_BATCH = 10
RESOLVE_CALL_BUDGET = 15
CONFIRM_BATCH = 10
CHECK_BATCH = 20
# The radius of a near call: the module proposes its stations within this
# many metres of the hub's position (its own maximum). A filter for
# proposals, never a matcher: two distinct termini can stand closer than this.
PROPOSAL_RADIUS_M = 300
# The name fallback (owner decision of 10 Oct): when the near call finds no
# station around a hub, the hub's name is searched. At most this many of its
# results are shown, nearest the hub's position first.
NAME_FALLBACK_MAX = 5
# A result of the name search farther than this from the hub's position is
# not shown (it is counted, and the page says so): a station 50 km away is a
# namesake in another town or country, never the hub's station, and a hub
# stored that far from its station needs its position corrected first. A
# result between 300 m and this is shown with a warning. No result of the
# name search is ever "proposed" (owner rule): each one is to pick.
NAME_FALLBACK_MAX_DISTANCE_M = 50_000
# The name shortening (owner decision of 10 Oct): when the full name keeps
# no candidate, the name is searched again without its last word, then its
# last two, and so on down to its first word, at most this many times per
# hub. Words are split where the module's search splits them (white space,
# its hyphens, apostrophes and punctuation: MODULE_SEPARATORS), so the
# module's "Marseille-St-Charles" is found by "Marseille" for the hub
# "Marseille Saint-Charles", and "Basel, Burgfelderhof" is shortened to
# "Basel". When the cap would leave out the form that ends before the
# name's first punctuation boundary (the town, in "St-Louis, Gare de
# Saint-Louis"; a full stop after a word of 3 letters or fewer, as in "St.",
# is not one), that form takes the third place: never more searches.
# Only for a hub with a usable position: without a distance to order and
# filter them, the results of a single word would be a guess.
NAME_SHORTEN_MAX = 3
# The most calls one hub of a resolve click can make: its near call, the
# search with its full name, and the shortened ones (`_hub_worst_calls`
# gives each hub's own, never more).
RESOLVE_HUB_MAX_CALLS = 1 + 1 + NAME_SHORTEN_MAX
_EARTH_RADIUS_M = 6_371_000.0
# The most hub ids a resolve or check request may name as already shown.
_SKIP_MAX = 2_000
_CODE_RULE = "uic must be 3 to 20 characters, with no white space and no control character"
_NOT_AN_ID = "uic must be a station code, not a VIATOR id (ZZ-99999)"
_ID_RULE = "station_id must be a VIATOR id: two capital letters, a hyphen, five digits"
_PAIR_RULE = "each pair needs a uic, a station_id, or both"
_MODULE_DID_NOT_ANSWER = "The station module did not answer; nothing was changed."
_MODULE_OFF = "The station module is not configured; nothing was changed."
# A 404 on the near call: the module is older than the release that added it.
_NEAR_NEEDS_NEWER_MODULE = (
    "The station module does not offer this call: proposing codes by position needs "
    "MSMM v0.3.4 or later. Nothing was changed."
)
# The module's 429 window words that refuse the near call alone (its decision
# 62); the daily ones end at midnight UTC.
_NEAR_WINDOWS = frozenset({"near_user_minute", "near_user_day", "near_all_day"})
_STILL_WORKS = "Saving and checking codes still work."
# The reason of a search the module refused as a text it does not take (its
# 422): the same text always gets the same answer, so it is not a failure
# to stop at but a form that found nothing.
_TEXT_REFUSED = "status_422"
# The same 422 on a lookup that names VIATOR ids: a module from before its
# contract's phase 1 refuses the field `ids` (its body check), so the codes
# alone are asked once more.
_IDS_REFUSED = "status_422"
# The matrix order of the hubs, the order in which they are resolved and checked.
_MATRIX_ORDER = (NetworkCoverageHub.country, NetworkCoverageHub.sort_order, NetworkCoverageHub.id)


# ─────────────────────────── pydantic shapes ────────────────────────────


class HubInfo(BaseModel):
    """One hub in the matrix axis. v0.1.31 added country/tier/sort_order/
    is_active so the manage-hubs UI can group, sort, and soft-delete."""

    id: str
    name: str
    short: str
    region: str | None = None
    country: str = "FR"
    tier: str = "main"
    modes: str | None = None
    lat: float
    lon: float
    is_active: bool = True
    sort_order: int = 100
    # Step 3 of the station module: the station's code, and where it came
    # from ('msmm' confirmed from the module, 'manual' typed); both null when
    # the hub is not resolved. The cell dialog's re-run link carries `uic`.
    uic: str | None = None
    uic_origin: str | None = None
    # The contract's phase 2: the station's VIATOR id in the module, kept
    # beside the code (never in it); null when none is known. Its origin
    # and licence are read live by "Check station codes".
    station_id: str | None = None


def _station_code(value: str | None) -> str | None:
    """A station code typed by an administrator, or ValueError: the station
    module's rule (3 to 20 characters, no white space, no control character,
    no surrogate), so that a typed code can later be looked up; and never a
    VIATOR id, which is no code (the module's design 22.12)."""
    if value is not None and station_module.code_refused(value) is not None:
        raise ValueError(_CODE_RULE)
    if station_module.is_viator_id(value):
        raise ValueError(_NOT_AN_ID)
    return value


TypedCode = Annotated[str | None, AfterValidator(_station_code)]


def _writable(text: str) -> bool:
    """True when the text holds no surrogate (Cs) and no control character
    (Cc, NUL included). JSON can carry a lone surrogate (`\\ud800`) or a NUL,
    which PostgreSQL refuses and no UTF-8 answer can hold: a 500 either way."""
    return not any(unicodedata.category(character) in ("Cs", "Cc") for character in text)


def _writable_text(value: Any) -> Any:
    """`value`, or ValueError when it is a text that fails `_writable`."""
    if isinstance(value, str) and not _writable(value):
        raise ValueError("a text holds a control character or a lone surrogate")
    return value


def _writable_id(value: str) -> str:
    if not _writable(value):
        raise ValueError("a hub id holds a control character or a lone surrogate")
    return value


HubId = Annotated[str, Field(min_length=1, max_length=64), AfterValidator(_writable_id)]

# The 422 of a hub body that breaks a rule: a fixed sentence, then the names
# of the fields at fault, never their values. FastAPI's own 422 copies the
# refused input into its answer; a lone surrogate there cannot be written as
# UTF-8, so that answer would itself fail with a 500 (as in
# app/api/station_suggest.py). Only the routes of the hub form and of the
# station codes answer this way.
_HUB_REFUSED = (
    "The hub is not valid: name 1 to 120 characters, short 1 to 16, country 2, "
    "region up to 40, modes up to 20, tier main or regional, latitude -90 to 90, "
    "longitude -180 to 180, sort order 0 to 10000, station code 3 to 20 characters "
    "with no white space; no text may hold a control character or a lone surrogate."
)
_BATCH_REFUSED = (
    'The body must be {"skip": [<hub id>, ...]}: at most 2000 ids of 1 to 64 characters, '
    "with no control character and no lone surrogate."
)
_CONFIRM_REFUSED = (
    'The body must be {"pairs": [{"hub_id": <hub id>, "uic": <code>}, ...]}: 1 to 10 pairs, '
    "each hub once, each code 3 to 20 characters with no white space, no control character "
    "and no lone surrogate."
)


# The refusals of the hub form's and the station codes' routes, for their
# OpenAPI answers.
_BODY_422: dict[int | str, dict[str, Any]] = {
    422: {"description": "The body breaks a rule: a fixed sentence, never the input."},
}
_HUB_CODE_RESPONSES: dict[int | str, dict[str, Any]] = {
    **_BODY_422,
    403: {"description": "Not a platform administrator, or not a VIATOR user."},
}
_HUB_CREATE_RESPONSES: dict[int | str, dict[str, Any]] = {
    **_BODY_422,
    409: {"description": "A hub with this id already exists."},
}
_HUB_UPDATE_RESPONSES: dict[int | str, dict[str, Any]] = {
    **_BODY_422,
    404: {"description": _HUB_NOT_FOUND},
}


def _body_or_422[M: BaseModel](model: type[M], given: Any, refused: str) -> M:
    """`given` (the parsed JSON body) as `model`, or a 422 whose detail is
    `refused` and the names of the fields at fault, never a value."""
    try:
        return model.model_validate(given)
    except ValidationError as failure:
        fields = sorted(
            {
                str(error["loc"][0])
                for error in failure.errors(include_input=False, include_url=False)
                if error["loc"] and str(error["loc"][0]) in model.model_fields
            }
        )
        detail = refused + f" Fields at fault: {', '.join(fields)}." if fields else refused
        raise HTTPException(status_code=422, detail=detail) from None


class HubCreate(BaseModel):
    """v0.1.31 — POST /hubs body. id is the slug, mandatory and immutable
    once created; any later edits use PATCH on the existing id."""

    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str = Field(min_length=1, max_length=120)
    short: str = Field(min_length=1, max_length=16)
    country: str = Field(min_length=2, max_length=2, description="ISO 3166-1 alpha-2 (uppercase)")
    region: str | None = Field(default=None, max_length=40)
    tier: str = Field(default="main", pattern=r"^(main|regional)$")
    # Left unset at create time — populated later by the classification
    # job (or a manual edit) once GTFS/NeTEx route_type data is known.
    modes: str | None = Field(default=None, max_length=20)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    sort_order: int = Field(default=100, ge=0, le=10_000)
    # A station code typed by hand: stored with uic_origin = 'manual'.
    uic: TypedCode = None

    @field_validator("*")
    @classmethod
    def _writable_texts(cls, value: Any) -> Any:
        return _writable_text(value)


class HubUpdate(BaseModel):
    """v0.1.31 — PATCH /hubs/{id} body. Every field is optional; missing
    fields are not modified. id is immutable (use DELETE + POST to rename)."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    short: str | None = Field(default=None, min_length=1, max_length=16)
    country: str | None = Field(default=None, min_length=2, max_length=2)
    region: str | None = Field(default=None, max_length=40)
    tier: str | None = Field(default=None, pattern=r"^(main|regional)$")
    modes: str | None = Field(default=None, max_length=20)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    sort_order: int | None = Field(default=None, ge=0, le=10_000)
    # is_active separately so a soft-deleted hub can be restored
    # without changing other fields.
    is_active: bool | None = None
    # A station code typed by hand: stored with uic_origin = 'manual' when it
    # differs from the stored one; null clears the code and its origin.
    uic: TypedCode = None
    # True forgets the hub's VIATOR id (the station module's), for a hub
    # whose id no longer names its station; the code is left as it is.
    forget_station_id: bool = False

    @field_validator("*")
    @classmethod
    def _writable_texts(cls, value: Any) -> Any:
        return _writable_text(value)


class RunCreate(BaseModel):
    """Body of POST /api/admin/network-coverage/runs.

    Two valid shapes:
      - mode='single_session' + session_id='<sid>'  (the legacy default)
      - mode='fanout' + session_id=None              (PR #36)

    The combination is enforced at endpoint time — invalid pairings get
    a 400 with a descriptive error rather than silently falling through.
    """

    # Optional in fanout mode; required in single-session mode.
    session_id: str | None = Field(
        default=None, description="Target session — required when mode='single_session'"
    )
    depart_at: datetime = Field(description="Departure datetime (timezone-aware preferred)")
    direction: str = Field(default="both", description="'both' | 'single'")
    # PR #36 — see runner.MODE_SINGLE_SESSION / MODE_FANOUT.
    mode: str = Field(
        default="single_session",
        pattern=r"^(single_session|fanout)$",
        description="'single_session' (legacy) | 'fanout' (cross-session matrix)",
    )
    # Optional ISO 3166-1 alpha-2 country filter. None / [] = no filter
    # (full active hub list, legacy behaviour). When set, both matrix
    # axes are restricted to hubs whose `country` is in the list — turns
    # a 50-hub by 50-hub matrix into a ~100-pair smoke test for fast
    # single-country or cross-border probes.
    countries: list[str] | None = Field(
        default=None,
        description=(
            "Optional ISO 3166-1 alpha-2 country codes (e.g. ['FR','CH']). "
            "When set, both matrix axes are filtered to hubs in those "
            "countries. None / [] = no filter."
        ),
        max_length=20,
    )
    # PR-E — opt the run into running external-planner verification on
    # every no_route / timeout / error cell at run-completion time.
    # Default False keeps the legacy single-button-per-cell behaviour
    # intact for runs that don't tick the new run-form checkbox.
    verify_externally: bool = Field(
        default=False,
        description=(
            "If true, after the run flips to 'completed' the worker calls "
            "ÖBB HAFAS for every cell whose status is in (no_route, "
            "timeout, error) and persists the verdict to "
            "NetworkCoverageResult.external_* columns."
        ),
    )
    # PR-3 — per-run day-window override (origin-local time-of-day slice).
    # All four fields are optional; NULL means "use the platform_config
    # defaults" (resolved at execute time, see runner._resolve_run_window).
    # The form's Advanced section pre-fills each from the defaults.
    window_start_local: str | None = Field(
        default=None,
        description=("Day-window start as 'HH:MM' 24h. None = use COVERAGE_DEFAULT_WINDOW_START."),
    )
    window_end_local: str | None = Field(
        default=None,
        description=(
            "Day-window end as 'HH:MM' 24h, or '24:00' for end-of-day. "
            "None = use COVERAGE_DEFAULT_WINDOW_END."
        ),
    )
    window_timezone: str | None = Field(
        default=None,
        description=(
            "IANA timezone (e.g. 'Europe/Vienna'). Must be in "
            "COVERAGE_DEFAULT_TIMEZONE.choices. None = use "
            "COVERAGE_DEFAULT_TIMEZONE."
        ),
    )
    reference_date: date | None = Field(
        default=None,
        description=(
            "Calendar date in window_timezone the K slots anchor on. "
            "None = the day whose day-window contains depart_at (normally "
            "depart_at's own date; one day earlier for a cross-midnight "
            "window). Supplying a date that disagrees with that anchor is "
            "a 400: the grid is searched on reference_date while every "
            "comparison anchors on depart_at, so the two must agree."
        ),
    )

    @field_validator("window_start_local", "window_end_local")
    @classmethod
    def _validate_hhmm(cls, v: str | None) -> str | None:
        """Accept 'HH:MM' (00:00-23:59) or the '24:00' end-of-day
        sentinel on the end bound. Empty / None = use the platform
        default at execute time."""
        if v is None or v == "":
            return None
        if not _HHMM_RE.match(v):
            raise ValueError(f"must be 'HH:MM' (00:00-23:59) or '24:00'; got {v!r}")
        return v

    @field_validator("window_timezone")
    @classmethod
    def _validate_tz(cls, v: str | None) -> str | None:
        """Restrict to the COVERAGE_DEFAULT_TIMEZONE choices list. The
        DB column is loose TEXT so a future operator-typed zone doesn't
        require a migration — the gate lives here at the API surface so
        the form's `<select>` and the API stay in sync."""
        if v is None or v == "":
            return None
        spec = CONFIG_SCHEMA.get("COVERAGE_DEFAULT_TIMEZONE", {})
        choices = spec.get("choices")
        if choices is not None and v not in choices:
            raise ValueError(f"timezone must be one of {choices}, got {v!r}")
        # Defence in depth — make sure zoneinfo can actually resolve it.
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown IANA timezone {v!r}") from exc
        return v

    @field_validator("countries")
    @classmethod
    def _validate_country_codes(cls, v: list[str] | None) -> list[str] | None:
        """Each entry must be 2 alpha chars; empty list normalises to None
        so the API + DB store 'no filter' the same way."""
        if v is None or len(v) == 0:
            return None
        seen: set[str] = set()
        out: list[str] = []
        for c in v:
            if not isinstance(c, str) or len(c) != 2 or not c.isalpha():
                raise ValueError(
                    f"country codes must be 2-letter alpha (ISO 3166-1 alpha-2); got {c!r}"
                )
            up = c.upper()
            if up not in seen:
                out.append(up)
                seen.add(up)
        return out


class RunSummary(BaseModel):
    """List-view row — minimal info for the sidebar."""

    id: str
    session_id: str | None
    session_label: str
    depart_at: str
    started_at: str
    finished_at: str | None
    status: str
    direction: str
    # PR #36 — 'single_session' (legacy) | 'fanout' (cross-session matrix).
    # Sidebar uses this to render a distinct icon/badge for fanout runs.
    mode: str = "single_session"
    total_pairs: int
    completed_pairs: int
    ok_pairs: int
    no_route_pairs: int
    error_pairs: int
    # Country subset filter (ISO 3166-1 alpha-2) — None when the run used
    # the full active hub list. The sidebar renders a small badge like
    # "FR+CH" when this is set so operators can tell at a glance which
    # runs were full vs subset.
    countries: list[str] | None = None
    # PR-E — surfaces the run-level flag so the sidebar can show which
    # runs auto-verified. Default False handles legacy / pre-migration
    # rows gracefully via the `_run_to_summary` getattr fallback.
    verify_externally: bool = False
    # PR-3 — resolved per-run day-window for UI display. NULL on each
    # field = "uses the platform_config default" — the UI shows a
    # subtler "[default]" pill in that case so the operator knows the
    # behaviour without having to look up the config.
    window_start_local: str | None = None
    window_end_local: str | None = None
    window_timezone: str | None = None
    reference_date: str | None = None
    # True when `depart_at` does NOT fall inside the day-window this run
    # actually searched — i.e. the run's matrix answers a different
    # calendar day than the departure the operator asked for. Only ever
    # True on rows created before `runner._anchor_depart_at_and_reference_date`
    # (whose `reference_date` defaulted to "tomorrow at create time").
    # Such a run's numbers are real but describe the wrong day, so the UI
    # badges it and the trip filter is disabled for it — see
    # `_comparable_depart_at`.
    depart_at_outside_window: bool = False
    # PR-190 — banner stats so operators watching a long run can see
    # "how long has it been running" and "how fast is the runner". All
    # four fields are NULL until at least one cell has finished:
    #   duration_seconds — wall-clock since started_at; uses `now` while
    #     in-flight, finished_at once the run is terminal. Clamped >=0
    #     to avoid negative values on clock skew between writers.
    #   response_ms_(min|avg|max) — aggregated from
    #     NetworkCoverageResult.response_ms across this run's cells
    #     (per-cell wall-clock OTP took to answer). NULL on a 0-cell run.
    duration_seconds: float | None = None
    response_ms_min: int | None = None
    response_ms_avg: float | None = None
    response_ms_max: int | None = None
    # #392 — what the page shows in the status pill, from the executor's
    # bookkeeping on the row: "queued" (pending, no executor holds it yet),
    # "resuming" (running, but its executor stopped: a deploy or a worker
    # restart; the next executor resumes it), "stopping" (Stop clicked, the
    # executor has not confirmed yet), else `status`.
    display_status: str = ""
    resume_count: int = 0
    heartbeat_age_seconds: float | None = None
    # #392 — why a failed run failed (`summary.failure_reason`: the resume
    # limit, an error in the pair loop, ...), shown next to the pill.
    failure_reason: str | None = None
    # The ÖBB check's progress (`verify_progress.SweepWriter`), all None until
    # the check starts: the cells it asks about, those with a saved answer,
    # the start of its current attempt and its last write (the database's
    # clock), the time it has taken so far (start to last write), and an
    # estimate of the time left at the current attempt's pace (None until
    # `_VERIFY_ETA_MIN_CELLS` answers and `_VERIFY_ETA_MIN_SECONDS`).
    verify_total: int | None = None
    verify_done: int | None = None
    verify_started_at: str | None = None
    verify_progress_at: str | None = None
    verify_elapsed_seconds: float | None = None
    verify_seconds_left: float | None = None


# PR-196a — the alignment-tier vocabulary the scorer in
# app/network_coverage/alignment.py emits. Pinning this as a Literal at
# the API boundary catches scorer drift (e.g. a typo or a new tier
# added without UI palette work) at FastAPI's response-model validation
# step rather than silently shipping an unmapped tier to the heatmap
# CSS. Keep in sync with app/network_coverage/alignment.py and the
# CSS palette in app/templates/admin/network_coverage.html.
AlignmentTier = Literal[
    "agree",
    "mostly_agree",
    "partial",
    "disagree",
    "no_overlap",
    "one_sided_viator",
    "one_sided_oebb",
    "no_service",
    "no_data",
]


class ResultEntry(BaseModel):
    """One cell in the matrix."""

    origin_hub_id: str
    dest_hub_id: str
    status: str
    response_ms: int | None
    num_itineraries: int | None
    best_duration_seconds: int | None
    best_num_transfers: int | None
    best_operators: str | None
    error_message: str | None
    journey_search_id: str | None
    # PR #36 — list of sessions that returned trips for this pair in a
    # fanout-mode run. NULL for single-session runs (the run's session_id
    # is the answer). The matrix UI badges each cell with "fr + eu"
    # style markers using this field.
    session_ids: list[str] | None = None
    # PR-E — persisted external-verify verdict for this cell. All NULL on
    # legacy rows or on runs created with verify_externally=False. The
    # matrix UI reads these to render the per-cell coloured dot without
    # making an extra fetch. Semantics:
    #   external_ok=True, external_error=None  → ÖBB found (green dot)
    #   external_ok=False, external_error=None → ÖBB also empty (blue)
    #   external_error non-NULL                → unknown (yellow)
    external_ok: bool | None = None
    external_num_connections: int | None = None
    external_best_duration_seconds: int | None = None
    external_best_transfers: int | None = None
    external_source: str | None = None
    external_error: str | None = None
    external_verified_at: datetime | None = None
    # PR-196a — alignment heatmap signal. tier drives the matrix cell
    # background colour (viridis palette), score is shown in the
    # tooltip. NULL on rows that pre-date the sweep — render as
    # 'no_data' (light grey) in the matrix.
    external_alignment_score: float | None = None
    external_alignment_tier: AlignmentTier | None = None


class RunDetail(RunSummary):
    """Full detail-view shape — used for the matrix render."""

    summary: dict[str, Any] | None = None
    results: list[ResultEntry] = []


class CellTripsDirection(BaseModel):
    """One side of the cell-trips response (outbound A→B or return B→A).

    Mirrors the per-cell summary the matrix already has in memory PLUS
    the materialised trip list — the modal can render the same trip-card
    UI used by the export and by the live journey page without a second
    round-trip.
    """

    origin_hub_id: str
    dest_hub_id: str
    status: str
    response_ms: int | None = None
    num_itineraries: int | None = None
    best_duration_seconds: int | None = None
    best_num_transfers: int | None = None
    best_operators: str | None = None
    error_message: str | None = None
    # journey_trips rows materialised through the
    # search → executions → trips chain. Each entry mirrors the shape
    # produced by `_fetch_trips_by_search` (rank, duration_seconds,
    # num_transfers, departure_at, arrival_at, modes, legs).
    trips: list[dict[str, Any]] = []
    # PR-E — pre-rendered external-verify verdict so the modal can show
    # ÖBB's answer on open without an extra click. The manual "Verify
    # externally" button stays for re-check (transient overlay, doesn't
    # mutate the persisted row).
    external_ok: bool | None = None
    external_num_connections: int | None = None
    external_best_duration_seconds: int | None = None
    external_best_transfers: int | None = None
    external_source: str | None = None
    external_error: str | None = None
    external_verified_at: datetime | None = None
    # PR-196a — graduated alignment heatmap data. The matrix UI reads
    # tier + score per cell to colour the viridis-palette background;
    # the modal renders external_itineraries side-by-side against
    # `trips` (VIATOR) so the operator sees both planners' answers
    # without a second round-trip. NULL on legacy rows (renders as
    # 'no_data' tier — light grey, distinguishable from no_service).
    external_itineraries: list[dict[str, Any]] | None = None
    external_alignment_score: float | None = None
    external_alignment_tier: AlignmentTier | None = None


class CellTripsResponse(BaseModel):
    """Trip breakdown for one matrix cell, split into outbound and return.

    `return_` is None when the run was created with direction='single'
    (B→A wasn't queried) or when no result row exists for the reverse
    pair (data gap). The UI hides the section entirely in the first case
    and shows a "not queried" hint in the second.
    """

    direction: str  # 'both' | 'single' — copied from the run row
    outbound: CellTripsDirection | None = None
    # `return` is a Python keyword — alias maps the wire name to a safe
    # attribute name. Pydantic v2 serialises by alias by default when
    # `populate_by_name=True` isn't set, so the JSON key is "return".
    return_: CellTripsDirection | None = Field(default=None, alias="return")

    model_config = {"populate_by_name": True}


# ─────────────────────────── endpoints ───────────────────────────


@router.get("/hubs", response_model=list[HubInfo])
def list_hubs(
    db: Annotated[DbSession, Depends(get_db)],
    _: Annotated[CurrentUser, Depends(require_platform_admin)],
    include_inactive: bool = False,
) -> list[HubInfo]:
    """List of hubs forming the matrix axis.

    v0.1.31: reads from `network_coverage_hubs` table. By default returns
    only is_active=True (matrix axis); pass `include_inactive=true` from
    the manage-hubs UI to include soft-deleted entries for restoration.

    Falls back to the static HUBS list from `app/network_coverage/hubs.py`
    when the table is empty — handles the brief window between table
    creation and migration seed during deploy, and dev environments
    that haven't run migrations.
    """
    q = select(NetworkCoverageHub)
    if not include_inactive:
        q = q.where(NetworkCoverageHub.is_active.is_(True))
    q = q.order_by(NetworkCoverageHub.country, NetworkCoverageHub.sort_order, NetworkCoverageHub.id)
    rows = db.execute(q).scalars().all()
    if rows:
        return [_hub_to_info(r) for r in rows]
    # Fallback for empty-table case — preserves behaviour for fresh
    # installs and catches the brief migration window.
    return [
        HubInfo(
            id=h.id,
            name=h.name,
            short=h.short,
            region=h.region,
            country="FR",
            tier="regional" if h.id == "batz" else "main",
            lat=h.lat,
            lon=h.lon,
            is_active=True,
            sort_order=100,
        )
        for h in STATIC_HUBS
    ]


@router.post("/hubs", status_code=201, responses=_HUB_CREATE_RESPONSES)
def create_hub(
    body: Annotated[Any, Body()],
    db: Annotated[DbSession, Depends(get_db)],
    _: Annotated[CurrentUser, Depends(require_platform_admin)],
) -> HubInfo:
    """v0.1.31 — create a new hub.

    Slug must be unique (PK conflict → 409). Country normalised to
    uppercase to keep ISO codes consistent regardless of operator
    typing habits."""
    hub_body = _body_or_422(HubCreate, body, _HUB_REFUSED)
    hub = NetworkCoverageHub(
        id=hub_body.id,
        name=hub_body.name,
        short=hub_body.short,
        country=hub_body.country.upper(),
        region=hub_body.region,
        tier=hub_body.tier,
        modes=hub_body.modes,
        lat=hub_body.lat,
        lon=hub_body.lon,
        sort_order=hub_body.sort_order,
        is_active=True,
        uic=hub_body.uic,
        uic_origin="manual" if hub_body.uic is not None else None,
    )
    db.add(hub)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, f"Hub with id={hub_body.id!r} already exists") from None
    db.refresh(hub)
    return _hub_to_info(hub)


@router.patch("/hubs/{hub_id}", responses=_HUB_UPDATE_RESPONSES)
def update_hub(
    hub_id: str,
    body: Annotated[Any, Body()],
    db: Annotated[DbSession, Depends(get_db)],
    _: Annotated[CurrentUser, Depends(require_platform_admin)],
) -> HubInfo:
    """v0.1.31 — edit an existing hub.

    Sparse update: only fields present in the request body are modified.
    The slug (id) is immutable — to rename, soft-delete and create new.
    Country is uppercased on write.
    """
    changes = _body_or_422(HubUpdate, body, _HUB_REFUSED)
    if len(hub_id) > 64 or not _writable(hub_id):
        # No hub has such an id, and PostgreSQL would refuse a NUL in it.
        raise HTTPException(404, _HUB_NOT_FOUND)
    hub = db.get(NetworkCoverageHub, hub_id)
    if hub is None:
        raise HTTPException(404, f"Hub {hub_id!r} not found")
    data = changes.model_dump(exclude_unset=True)
    if "country" in data and data["country"] is not None:
        data["country"] = data["country"].upper()
    if "uic" in data:
        _set_typed_code(hub, data.pop("uic"))
    if data.pop("forget_station_id", False):
        hub.station_id = None
    for key, value in data.items():
        setattr(hub, key, value)
    hub.updated_at = datetime.now(UTC)
    db.commit()
    db.refresh(hub)
    return _hub_to_info(hub)


@router.delete("/hubs/{hub_id}", status_code=204)
def delete_hub(
    hub_id: str,
    db: Annotated[DbSession, Depends(get_db)],
    _: Annotated[CurrentUser, Depends(require_platform_admin)],
) -> None:
    """v0.1.31 — soft-delete a hub (sets is_active=False).

    Hard delete is intentionally not exposed: result rows from
    historical coverage runs reference hub_id as a string and would
    render as "unknown hub" if the row vanished. Soft-delete keeps old
    matrices intact while removing the hub from new runs and from the
    matrix axis on the current view.

    Idempotent: deleting an already-inactive hub returns 204 silently.
    To restore a hub, PATCH it with `{"is_active": true}`.
    """
    hub = db.get(NetworkCoverageHub, hub_id)
    if hub is None:
        raise HTTPException(404, f"Hub {hub_id!r} not found")
    hub.is_active = False
    hub.updated_at = datetime.now(UTC)
    db.commit()


# ─────────── station codes from the station module (MSMM step 3) ───────────
#
# A hub gains a station code in two ways: typed by an administrator (create
# or PATCH, stored as 'manual'), or proposed by the module and confirmed by
# an administrator. The three routes below are the second way:
#
#   resolve  one near call of the module per unresolved hub, at the hub's
#            position, at most 10 a click, one after the other: the module's
#            stations within 300 m (at most 5). When it finds none, or the
#            hub's stored position is not a usable one (no near call then),
#            one name search with the hub's name: at most 5 results, nearest
#            first, each to pick (never "proposed"), those beyond 300 m with
#            a warning, those beyond 50 km not shown. Writes nothing.
#   confirm  the pairs (hub, code) the administrator accepted, at most 10,
#            checked with one lookup of the module, each code sent once;
#            only a code the module serves is stored ('msmm').
#   check    one lookup of the stored codes of at most 20 hubs a click;
#            shows a code the module no longer serves, or a name that
#            differs; writes nothing.
#
# Every module call is made on behalf of the administrator (his VIATOR user
# id), so it counts on his own limits at the module. At the first 429 a
# route stops, never retries, and answers what it found with the module's
# Retry-After. Any other failure: one sentence, nothing written. A coverage
# run never calls the module: it routes by the positions stored on the hubs.


class HubBatchRequest(BaseModel):
    """Body of POST /hubs/resolve and /hubs/check: the hub ids the dialog
    has already shown in this round, which the next click skips."""

    model_config = ConfigDict(extra="forbid")

    skip: list[HubId] = Field(default_factory=list, max_length=_SKIP_MAX)


class HubCandidate(BaseModel):
    """A station of the module offered for a hub. `found_by`: `position`
    (the near call: within 300 m, the module's distance) or `name` (the
    name fallback: the distance VIATOR computed from the hub's position, or
    None when either position is not a usable one). `warning`: a sentence
    the page shows with the candidate when it is not a station within 300 m
    of the hub's position (found by name farther away, or at an unknown
    distance); None otherwise. `shortened_name`: for a candidate found by
    name, the shortened form of the hub's name whose search found it (the
    full name kept nothing); None when the full name found it, or when found
    by position."""

    name: str
    # The station's code; null for a station the module serves by its VIATOR
    # id alone (from the module's phase 3), which then has a `station_id`.
    uic: str | None
    country_iso: str | None = None
    distance_m: int | None
    found_by: Literal["position", "name"] = "position"
    warning: str | None = None
    shortened_name: str | None = None
    station_id: str | None = None


class HubProposal(BaseModel):
    """One hub looked at by a resolve click. `proposed`: exactly one
    candidate of the near call. `to_pick`: none, several (at most five), or
    any found by name, all listed. `no_position`: the
    hub's stored position is not a usable one (not a finite latitude of -90
    to 90 and longitude of -180 to 180), so no near call was made; the
    candidates, if any, were found by name, each with a warning.

    `name_searched`: the near call found nothing (or could not be made), so
    the module's name search was asked with the hub's name. `far_dropped`:
    stations found by name farther than 50 km from the hub's position, not
    listed: each code once, over every search made for the hub (the full
    name and the shortened ones). `shortened_searches`: how many searches
    with a shortened form of the name were made (0 when the full name kept a
    candidate, or for a hub without a usable position)."""

    hub_id: str
    hub_name: str
    state: Literal["proposed", "to_pick", "no_position"]
    candidates: list[HubCandidate]
    name_searched: bool = False
    far_dropped: int = 0
    shortened_searches: int = 0


class HubResolveResponse(BaseModel):
    """`status`: `ok`; `limited` (the module's limit was reached: what was
    found before is kept, `retry_after` seconds to wait, or None when the
    module gave none); `unavailable` (the module is not configured, paused or
    failed: `message` says which). `left`: unresolved hubs not looked at yet.
    `near_limited`: the limit reached is one of the near call alone."""

    status: Literal["ok", "limited", "unavailable"]
    message: str | None = None
    retry_after: int | None = None
    proposals: list[HubProposal]
    left: int
    # True when the 429 came from one of the module's near windows: only the
    # near call is refused, so the panel holds "Propose" and "next" alone
    # and keeps saving and checking codes.
    near_limited: bool = False
    # True when the click stopped before RESOLVE_BATCH hubs because the next
    # hub's worst case would pass RESOLVE_CALL_BUDGET: the page says why.
    call_budget_reached: bool = False


class HubConfirmPair(BaseModel):
    """One station an administrator accepted for a hub: its code, its VIATOR
    id, or both (a candidate's values, as the resolve route gave them)."""

    model_config = ConfigDict(extra="forbid")

    hub_id: HubId
    uic: str | None = None
    station_id: str | None = None

    @field_validator("uic")
    @classmethod
    def _code(cls, value: str | None) -> str | None:
        return _station_code(value)

    @field_validator("station_id")
    @classmethod
    def _id(cls, value: str | None) -> str | None:
        if value is not None and not station_module.is_viator_id(value):
            raise ValueError(_ID_RULE)
        return value

    @model_validator(mode="after")
    def _a_key(self) -> HubConfirmPair:
        if self.uic is None and self.station_id is None:
            raise ValueError(_PAIR_RULE)
        return self


class HubConfirmRequest(BaseModel):
    """Body of POST /hubs/confirm: 1 to 10 pairs, each hub once."""

    model_config = ConfigDict(extra="forbid")

    pairs: list[HubConfirmPair] = Field(min_length=1, max_length=CONFIRM_BATCH)

    @model_validator(mode="after")
    def _hubs_once(self) -> HubConfirmRequest:
        ids = [pair.hub_id for pair in self.pairs]
        if len(set(ids)) != len(ids):
            raise ValueError("each hub may appear once")
        return self


class HubConfirmed(BaseModel):
    """What became of one pair: `stored` (with the module's name, and the
    VIATOR id stored, when the module gave one), `not_served` (the module
    does not serve the code, or the id: the hub is left as it was),
    `unknown_hub`, or `changed_meanwhile` (the hub's code or VIATOR id was
    changed by someone else while the module was asked: nothing written,
    the hub keeps that change; propose again)."""

    hub_id: str
    uic: str | None
    state: Literal["stored", "not_served", "unknown_hub", "changed_meanwhile"]
    module_name: str | None = None
    station_id: str | None = None


class HubConfirmResponse(BaseModel):
    status: Literal["ok", "limited", "unavailable"]
    message: str | None = None
    retry_after: int | None = None
    results: list[HubConfirmed]


class HubChecked(BaseModel):
    """One hub of a check click: `ok`, `not_served` (the module no longer
    serves its VIATOR id, or its code when it has no id, or the code is not
    one the module accepts) or `name_differs` (the module's name is shown);
    `parent_uic` is the code of the station's group in the module, when it
    has one.

    Read live from the module (the contract's phase 2, decision D-R11 A),
    never stored: `module_uic` (the code the module answers for the hub's
    station: for an id, its anchor's, which need not be the hub's own
    `uic`), `code_origin` and `code_origin_text` (the origin label of that
    code, and its words) and `licence` (the module's licence object: tags,
    share-alike fields, attribution texts). `station_id` is the hub's VIATOR
    id after the check; `replaced_id` the id it had when the module said it
    was merged into `station_id`; `no_answer` that the module did not answer
    the second lookup of the hub (its code, after its id went unanswered),
    with `message` and `retry_after`; `id_not_answered` that the module answered
    the hub's code, not its id; `changed_meanwhile` that a change made
    meanwhile kept the check from writing."""

    hub_id: str
    hub_name: str
    uic: str | None
    uic_origin: str | None = None
    state: Literal["ok", "not_served", "name_differs", "no_answer"]
    module_name: str | None = None
    parent_uic: str | None = None
    station_id: str | None = None
    replaced_id: str | None = None
    # `no_answer`: the second lookup (the hub's code, its id unanswered)
    # failed: what the module said, and its Retry-After when it gave one.
    message: str | None = None
    retry_after: int | None = None
    # The hub's VIATOR id got no answer, and its code was asked instead: the
    # row shown is the code's (the id is kept; the administrator decides).
    id_not_answered: bool = False
    # The hub's code or VIATOR id was changed while the module was asked:
    # the id the check would have written was not written; check again.
    changed_meanwhile: bool = False
    module_uic: str | None = None
    code_origin: str | None = None
    code_origin_text: str | None = None
    licence: dict[str, Any] | None = None


class HubCheckResponse(BaseModel):
    status: Literal["ok", "limited", "unavailable"]
    message: str | None = None
    retry_after: int | None = None
    results: list[HubChecked]
    left: int


def _module_refusal() -> str | None:
    """The sentence to answer without any call when the module is not
    configured or its client's pause runs; None when a call may be made."""
    if not station_module.enabled():
        return _MODULE_OFF
    if station_module.paused():
        return _MODULE_DID_NOT_ANSWER
    return None


def _caller(admin: CurrentUser) -> uuid.UUID:
    """The administrator's VIATOR user id, on whose behalf the module is called."""
    if admin.id is None:  # pragma: no cover — require_platform_admin admits JWT users only
        raise HTTPException(403, "Requires a VIATOR user")
    return admin.id


def _wait_text(seconds: int | None) -> str:
    """The module's Retry-After for a sentence: "a minute" when it gave
    none, seconds under two minutes, then minutes under two hours, then
    hours (rounded up); the wait itself is unchanged."""
    if seconds is None:
        return "a minute"
    if seconds < 120:
        return "1 second" if seconds == 1 else f"{seconds} seconds"
    if seconds < 7200:
        return f"about {math.ceil(seconds / 60)} minutes"
    return f"about {math.ceil(seconds / 3600)} hours"


def _failure(
    outcome: station_module.Outcome,
) -> tuple[Literal["limited", "unavailable"], str, int | None]:
    """The status, the sentence and the time to wait of a failed call: a 429
    is `limited` with the module's Retry-After; anything else `unavailable`."""
    if outcome.reason == "status_429":
        wait = _wait_text(outcome.retry_after)
        return (
            "limited",
            f"The station module's limit is reached; try again in {wait}.",
            outcome.retry_after,
        )
    return "unavailable", _MODULE_DID_NOT_ANSWER, None


def _near_limit_message(code: str, retry_after: int | None) -> str:
    """The sentence of a near window's 429: the minute one with its wait, a
    daily one "tomorrow (UTC)" (its window ends at midnight UTC) rather than
    up to a day of seconds; and that saving and checking still work."""
    if code == "near_user_minute":
        wait = _wait_text(retry_after)
        sentence = f"Your limit of proposals a minute is reached; try again in {wait}."
    elif code == "near_user_day":
        sentence = "Your daily limit of proposals is reached; try again tomorrow (UTC)."
    else:
        sentence = (
            "The daily limit of proposals shared by every administrator is reached; "
            "try again tomorrow (UTC)."
        )
    return f"{sentence} {_STILL_WORKS}"


def _resolve_failure(
    outcome: station_module.Outcome,
) -> tuple[Literal["limited", "unavailable"], str, int | None, bool]:
    """`_failure` for a near call, and whether the limit reached is the near
    call's alone (the module refuses only that call: lookup goes on). A 404
    says the module is too old for the call. No pause either way."""
    if outcome.reason == "status_429" and outcome.code in _NEAR_WINDOWS:
        message = _near_limit_message(outcome.code, outcome.retry_after)
        return "limited", message, outcome.retry_after, True
    if outcome.reason == "status_404":
        return "unavailable", _NEAR_NEEDS_NEWER_MODULE, None, False
    status, message, retry_after = _failure(outcome)
    return status, message, retry_after, False


def _has_position(hub: NetworkCoverageHub) -> bool:
    """True when the hub's stored position can be sent to the module (the
    near call's own rule, `station_module.position_refused`). The columns
    are NOT NULL and the hub form checks the ranges, so only a hand edit of
    the table (a NaN, an infinity, a value out of range) fails this."""
    return station_module.position_refused(hub.lat, hub.lon) is None


def _distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """The great-circle (haversine) distance between two positions, in
    metres, unrounded: the 300 m and 50 km rules compare this value, as the
    module's near call compares its own (a station at 300.4 m is beyond)."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def _shown_metres(distance: float) -> int:
    """A distance as shown: whole metres, rounded up, so that a station
    beyond 300 m never reads "300 m"."""
    return math.ceil(distance)


def _distance_text(metres: int) -> str:
    """A distance for a sentence: `640 m` below a kilometre, else `1.2 km`."""
    if metres < 1000:
        return f"{metres} m"
    return f"{metres / 1000:.1f} km"


_UNKNOWN_HUB_POSITION = (
    "Found by name; this hub has no usable position, so the distance is unknown "
    "— check before saving."
)


def _name_warning(
    located: bool, distance: float | None, shortened: str | None = None
) -> str | None:
    """The warning of a candidate found by name: None within 300 m of the
    hub's position (the unrounded distance), else the distance (or why it
    is unknown). Found by a shortened form of the name, it says so and that
    it is a looser match."""
    if not located:
        return _UNKNOWN_HUB_POSITION
    found = (
        "Found by name"
        if shortened is None
        else f"Found by the shortened name «{shortened}», a looser match"
    )
    if distance is None:
        return (
            f"{found}; the station module gives no usable position for it, so its "
            "distance from the hub is unknown — check before saving."
        )
    if distance <= PROPOSAL_RADIUS_M:
        return None
    return (
        f"{found}, {_distance_text(_shown_metres(distance))} from the hub's position "
        "— check before saving."
    )


# The characters the station module's search reads as a space between two
# words, white space aside: its hyphens, apostrophes and punctuation
# (decision 64 of the module), mirrored once in app/api/station_suggest.py
# from MSMM app/master/station_search.py `_HYPHENS`, `_APOSTROPHES` and
# `FOLDED_PUNCTUATION` (a test pins the copy). A word of a hub's name is a
# run of anything else.
MODULE_HYPHENS = station_suggest.MODULE_HYPHENS
MODULE_APOSTROPHES = station_suggest.MODULE_APOSTROPHES
MODULE_PUNCTUATION = station_suggest.MODULE_PUNCTUATION
MODULE_SEPARATORS = station_suggest.MODULE_SEPARATORS
_NAME_WORD = re.compile(r"[^\s" + re.escape(MODULE_SEPARATORS) + "]+")
# The fewest letters or digits a text the module searches must hold: its
# own rule is 3 characters once folded (separators made spaces), so a text
# like "Zz" and a dash, or "St." is refused with a 422. Three letters or
# digits always pass it.
_SEARCH_MIN_LETTERS = 3


def _searchable(text: str) -> bool:
    """True when `text` holds enough letters or digits for the module. A
    module separator never counts, even one Python takes for a letter (the
    modifier apostrophe U+02BC)."""
    letters = sum(character.isalnum() and character not in MODULE_SEPARATORS for character in text)
    return letters >= _SEARCH_MIN_LETTERS


def _name_query(hub: NetworkCoverageHub) -> str | None:
    """The hub's name as the module's search takes it (the typeahead's own
    rule, `station_suggest.normalise_query`: NFC, white space made single,
    3 to 100 characters, still 3 once folded as the module folds it, and
    at least 3 letters or digits), or None when the search would refuse
    it. Sent as stored otherwise: no word is removed."""
    if not isinstance(hub.name, str):
        return None
    try:
        query = station_suggest.normalise_query(hub.name)
    except ValueError:
        return None
    return query if _searchable(query) else None


def _shortened_names(name: str) -> list[str]:
    """The shortened forms of a hub's name (`name`: NFC, white space made
    single), longest first: the name up to the end of each word but the
    last, down to its first word, at most NAME_SHORTEN_MAX of them. Words
    are split at white space and at the module's hyphens, apostrophes and
    punctuation (`MODULE_SEPARATORS`), so a form never ends with a
    separator and never repeats the full name or another form in the
    module's eyes (each holds fewer words than the one before). Skipped: a
    form whose last word is a single letter (an elision such as
    "Zzville-d"), one with fewer than 3 letters or digits, and one the
    search would refuse (over 100 characters, or under 3 characters once
    folded). When the cap leaves out the form that ends at the name's first
    punctuation boundary (`_boundary_form`), that form takes the last
    place: "Zz-Zzlouis, Zzgare de Zzsaint-Zzlouis" gives "Zz-Zzlouis,
    Zzgare de Zzsaint", "Zz-Zzlouis, Zzgare de" and "Zz-Zzlouis" (the
    town, before the comma), still at most NAME_SHORTEN_MAX searches."""
    forms: list[str] = []
    words = list(_NAME_WORD.finditer(name))
    for word in reversed(words[:-1]):
        text = name[: word.end()]
        if len(word.group()) == 1 or not _searchable(text):
            continue
        try:
            forms.append(station_suggest.normalise_query(text))
        except ValueError:
            continue
    shortened = forms[:NAME_SHORTEN_MAX]
    boundary = _boundary_form(name, words, forms)
    if boundary is not None and boundary not in shortened:
        shortened[-1] = boundary
    return shortened


# A word this long or longer (letters and digits) names a place; a shorter
# one followed by a full stop is an abbreviation ("St.", "Zzs."), whose
# full stop is no boundary for the third-place rule.
_BOUNDARY_WORD_MIN = 4


def _boundary_form(name: str, words: list[re.Match[str]], forms: list[str]) -> str | None:
    """The form that ends at the name's first punctuation boundary, or None.

    A boundary is a mark of `MODULE_PUNCTUATION` whose form (the name up to
    the end of its last word before the mark) is one of `forms` (so it
    would be sent) and holds a whole word of `_BOUNDARY_WORD_MIN` letters
    or digits or more. A full stop right after a word of 3 letters or
    fewer is the end of an abbreviation, not a boundary: in "Zzs.
    Zzgallen, Zzbahnhof" the boundary is the comma ("Zzs. Zzgallen"),
    never the full stop ("Zzs", which would match many stations)."""
    for mark, character in enumerate(name):
        if character not in MODULE_PUNCTUATION:
            continue
        before = [word for word in words if word.end() <= mark]
        if not before:
            continue
        last = before[-1]
        if character == "." and last.end() == mark and _letters(last.group()) < _BOUNDARY_WORD_MIN:
            continue
        form = name[: last.end()]
        if form in forms and any(_letters(w.group()) >= _BOUNDARY_WORD_MIN for w in before):
            return form
    return None


def _letters(word: str) -> int:
    """How many letters or digits a word of a hub's name holds."""
    return sum(character.isalnum() for character in word)


def _name_plan(hub: NetworkCoverageHub, *, located: bool) -> tuple[str | None, list[str]]:
    """The searches the name fallback may make for a hub: its full name
    (None when the search would refuse it) and its shortened forms. A hub
    without a usable position is never shortened. A name too long to be
    searched whole (over 100 characters) is still shortened."""
    query = _name_query(hub)
    if not located or not isinstance(hub.name, str):
        return query, []
    if query is not None:
        return query, _shortened_names(query)
    joined = " ".join(unicodedata.normalize("NFC", hub.name).split())
    if len(joined) > station_suggest.QUERY_MAX:
        return None, _shortened_names(joined)
    return None, []


def _hub_worst_calls(hub: NetworkCoverageHub) -> int:
    """The most calls a resolve click can make for this hub: its near call
    (with a usable position), its full name, and its shortened forms."""
    located = _has_position(hub)
    query, forms = _name_plan(hub, located=located)
    return int(located) + int(query is not None) + len(forms)


def _name_candidates(
    hub: NetworkCoverageHub,
    rows: list[dict[str, Any]],
    *,
    located: bool,
    shortened: str | None = None,
) -> tuple[list[HubCandidate], set[str]]:
    """The candidates among one name search's rows, and the codes dropped
    for standing over 50 km away: a code the lookup accepts, each code once,
    farther than 50 km from the hub's position dropped, nearest first (rows
    at an unknown distance last, in the module's order), at most five. One
    beyond 300 m, or at an unknown distance, carries a warning. `shortened`:
    the shortened form of the name that was searched, if any."""
    kept: list[tuple[float | None, int, dict[str, Any]]] = []
    seen: set[str] = set()
    far: set[str] = set()
    for index, row in enumerate(rows):
        key = station_module.station_key(row)
        if key in seen or not _confirmable(row):
            continue
        seen.add(key)
        distance = None
        if located and station_module.position_refused(row["latitude"], row["longitude"]) is None:
            distance = _distance_m(hub.lat, hub.lon, row["latitude"], row["longitude"])
        if distance is not None and distance > NAME_FALLBACK_MAX_DISTANCE_M:
            far.add(key)
            continue
        kept.append((distance, index, row))
    kept.sort(key=lambda item: (item[0] is None, item[0] or 0, item[1]))
    candidates = [
        HubCandidate(
            name=row["name"],
            uic=row["uic"],
            country_iso=row["country_iso"],
            distance_m=None if distance is None else _shown_metres(distance),
            found_by="name",
            warning=_name_warning(located, distance, shortened),
            shortened_name=shortened,
            station_id=row.get("viator_id"),
        )
        for distance, _index, row in kept[:NAME_FALLBACK_MAX]
    ]
    return candidates, far


def _confirmable(row: dict[str, Any]) -> bool:
    """True for a module row a confirm can take: a code the lookup accepts,
    or no code and a VIATOR id (the module serves such a row from its phase
    3; the client keeps a row without a code only with a valid id)."""
    if row["uic"] is None:
        return row.get("viator_id") is not None
    return station_module.code_refused(row["uic"]) is None


def _proposal(hub: NetworkCoverageHub, rows: list[dict[str, Any]]) -> HubProposal:
    """The candidates among a near call's rows (already within 300 m of the
    hub's position, at most five), with a code the module's lookup accepts,
    nearest first, with the module's distance."""
    candidates = [
        HubCandidate(
            name=row["name"],
            uic=row["uic"],
            country_iso=row["country_iso"],
            distance_m=row["distance_m"],
            station_id=row.get("viator_id"),
        )
        for row in rows
        if _confirmable(row)
    ]
    candidates.sort(key=lambda candidate: candidate.distance_m or 0)
    state: Literal["proposed", "to_pick"] = "proposed" if len(candidates) == 1 else "to_pick"
    return HubProposal(hub_id=hub.id, hub_name=hub.name, state=state, candidates=candidates)


def _hubs_to_look_at(db: DbSession, *, resolved: bool, skip: list[str]) -> list[NetworkCoverageHub]:
    """The active hubs with (`resolved`) or without a station (a code or a
    VIATOR id), in the matrix order, less those the dialog has already shown."""
    query = select(NetworkCoverageHub).where(NetworkCoverageHub.is_active.is_(True))
    has_station = or_(
        NetworkCoverageHub.uic.is_not(None), NetworkCoverageHub.station_id.is_not(None)
    )
    query = query.where(has_station if resolved else not_(has_station))
    if skip:
        query = query.where(NetworkCoverageHub.id.not_in(skip))
    return list(db.execute(query.order_by(*_MATRIX_ORDER)).scalars().all())


def _hubs_by_id(db: DbSession, ids: list[str]) -> dict[str, NetworkCoverageHub]:
    """The active hubs of these ids, by id: a soft-deleted hub takes no code."""
    rows = db.execute(
        select(NetworkCoverageHub)
        .where(NetworkCoverageHub.id.in_(ids))
        .where(NetworkCoverageHub.is_active.is_(True))
    )
    return {hub.id: hub for hub in rows.scalars().all()}


@dataclass(frozen=True)
class _ResolveStop:
    """A module call of a resolve click failed: the click stops here and
    answers what it found before, with this status and sentence."""

    status: Literal["limited", "unavailable"]
    message: str
    retry_after: int | None
    near_limited: bool


async def _look_up_by_name(
    hub: NetworkCoverageHub, user_id: uuid.UUID, *, located: bool
) -> tuple[HubProposal | _ResolveStop, int]:
    """The name fallback of one hub, and the number of searches it made:
    one search of the module with the hub's full name
    (`station_module.admin_search_outcome`, which never pauses the
    typeahead); when it keeps no candidate and the hub has a usable
    position, one search with each shortened form of the name
    (`_name_plan`), longest first, until one keeps a candidate. A name the
    search would refuse is not sent. Never `proposed` (owner rule): even a
    single candidate within 300 m is to pick. A text the module refuses
    (a 422) found nothing: the next form is tried, so that a hub never
    blocks every later click. Any other failure stops the click as a near
    failure does (a 429 keeps what was found, with the module's
    Retry-After); the rest of the forms are not searched."""
    state: Literal["to_pick", "no_position"] = "to_pick" if located else "no_position"
    query, forms = _name_plan(hub, located=located)
    searches: list[tuple[str, str | None]] = [(form, form) for form in forms]
    if query is not None:
        searches.insert(0, (query, None))
    candidates: list[HubCandidate] = []
    far: set[str] = set()
    made = 0
    for text, shortened in searches:
        outcome = await station_module.admin_search_outcome(text, user_id)
        made += 1
        if outcome.rows is None:
            if outcome.reason == _TEXT_REFUSED:
                continue
            status, message, retry_after = _failure(outcome)
            return _ResolveStop(status, message, retry_after, near_limited=False), made
        candidates, far_here = _name_candidates(
            hub, outcome.rows, located=located, shortened=shortened
        )
        far |= far_here
        if candidates:
            break
    proposal = HubProposal(
        hub_id=hub.id,
        hub_name=hub.name,
        state=state,
        candidates=candidates,
        name_searched=query is not None,
        far_dropped=len(far),
        shortened_searches=made - int(query is not None),
    )
    return proposal, made


async def _look_at(
    hub: NetworkCoverageHub, user_id: uuid.UUID
) -> tuple[HubProposal | _ResolveStop, int]:
    """One hub of a resolve click, and the number of calls it made: the
    near call at its position; when it gives no usable candidate (no
    station at all, or only codes the lookup would refuse), or the hub has
    no usable position (no near call then: the module would refuse the
    position after counting it), the name fallback. A near failure (a 429
    included) stops the click without any name search."""
    located = _has_position(hub)
    near_calls = 0
    if located:
        outcome = await station_module.near_outcome(
            hub.lat, hub.lon, user_id, radius_m=PROPOSAL_RADIUS_M
        )
        near_calls = 1
        if outcome.rows is None:
            return _ResolveStop(*_resolve_failure(outcome)), near_calls
        proposal = _proposal(hub, outcome.rows)
        if proposal.candidates:
            return proposal, near_calls
    looked, searches = await _look_up_by_name(hub, user_id, located=located)
    return looked, near_calls + searches


@router.post("/hubs/resolve", responses=_HUB_CODE_RESPONSES)
async def resolve_hub_codes(
    body: Annotated[Any, Body()],
    db: Annotated[DbSession, Depends(get_db)],
    admin: Annotated[CurrentUser, Depends(require_platform_admin)],
) -> HubResolveResponse:
    """Propose a station code for at most 10 unresolved hubs: one near call
    of the module per hub, at the hub's position with a radius of 300 m, one
    after the other, on behalf of the administrator. Only when the near call
    finds no station (or a hub has no usable position) is the hub's name
    searched, then its shortened forms (`_look_up_by_name`): the module's
    names often differ from the hubs' own, so the position comes first. The
    click looks at the next hub only while its calls so far plus that hub's
    worst case stay within RESOLVE_CALL_BUDGET (the arithmetic is at the
    constants). Writes nothing: the administrator confirms what he accepts
    with POST /hubs/confirm. Stops at the first failure of either call; at a
    429 (a limit of the person, or of the near quota) it answers what it
    found with the module's Retry-After, never retrying."""
    asked = _body_or_422(HubBatchRequest, body, _BATCH_REFUSED)
    user_id = _caller(admin)
    hubs = await db_step(_hubs_to_look_at, db, resolved=False, skip=asked.skip)
    refusal = _module_refusal()
    if refusal is not None:
        return HubResolveResponse(
            status="unavailable", message=refusal, proposals=[], left=len(hubs)
        )
    proposals: list[HubProposal] = []
    calls = 0
    budget_reached = False
    for hub in hubs[:RESOLVE_BATCH]:
        if calls + _hub_worst_calls(hub) > RESOLVE_CALL_BUDGET:
            budget_reached = True
            break
        looked, made = await _look_at(hub, user_id)
        calls += made
        if isinstance(looked, _ResolveStop):
            return HubResolveResponse(
                status=looked.status,
                message=looked.message,
                retry_after=looked.retry_after,
                proposals=proposals,
                left=len(hubs) - len(proposals),
                near_limited=looked.near_limited,
            )
        proposals.append(looked)
    return HubResolveResponse(
        status="ok",
        proposals=proposals,
        left=len(hubs) - len(proposals),
        call_budget_reached=budget_reached,
    )


async def _lookup(codes: list[str], ids: list[str], user_id: uuid.UUID) -> station_module.Outcome:
    """One lookup of `codes` and `ids`. A module from before the VIATOR ids
    refuses a body with `ids` (a 422 of its body check): the codes alone are
    then asked once more, and the ids are not answered (a hub keyed by its id
    alone is shown as not served), so a hub action still works with a module
    rolled back below its contract's phase 1."""
    outcome = await station_module.lookup(codes, user_id, ids=ids)
    if outcome.rows is None and ids and outcome.reason == _IDS_REFUSED:
        if not codes:
            return station_module.Outcome([], "ok")
        outcome = await station_module.lookup(codes, user_id)
    return outcome


def _lookup_keys(pairs: list[HubConfirmPair]) -> tuple[list[str], list[str]]:
    """The distinct codes and VIATOR ids of the pairs, each once, in order."""
    codes = list(dict.fromkeys(pair.uic for pair in pairs if pair.uic is not None))
    ids = list(dict.fromkeys(pair.station_id for pair in pairs if pair.station_id is not None))
    return codes, ids


def _guarded_write(
    db: DbSession, hub_id: str, before: tuple[str | None, str | None], values: dict[str, Any]
) -> bool:
    """Write `values` to a hub only if its code and its VIATOR id are still
    `before` (as read before the module was asked): one UPDATE whose WHERE
    holds the two, so an edit made meanwhile (a code typed, another station
    confirmed, a check that moved the id) is never overwritten by an answer
    read for the hub as it was. True when the row was written."""
    uic_before, id_before = before
    written = db.execute(
        update(NetworkCoverageHub)
        .where(NetworkCoverageHub.id == hub_id)
        .where(NetworkCoverageHub.is_active.is_(True))
        .where(NetworkCoverageHub.uic.is_not_distinct_from(uic_before))
        .where(NetworkCoverageHub.station_id.is_not_distinct_from(id_before))
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    return bool(getattr(written, "rowcount", 0) == 1)


def _confirmed_row(
    pair: HubConfirmPair,
    by_code: dict[str, dict[str, Any]],
    by_id: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any] | None, str | None]:
    """The module's row that confirms a pair, and the VIATOR id to store:
    a pair with a code needs the module to serve the code (as before the
    ids); its id is then the id's own row's current id when the pair named
    one the module serves, else the code row's. A pair with an id alone
    needs the module to serve the id. Always the current id (the survivor
    of a merge: `station_module.current_id`), never the id as asked."""
    id_row = by_id.get(pair.station_id) if pair.station_id is not None else None
    if pair.uic is None:
        return id_row, None if id_row is None else station_module.current_id(id_row)
    code_row = by_code.get(pair.uic)
    if code_row is None:
        return None, None
    return code_row, station_module.current_id(id_row or code_row)


@router.post("/hubs/confirm", responses=_HUB_CODE_RESPONSES)
async def confirm_hub_codes(
    body: Annotated[Any, Body()],
    db: Annotated[DbSession, Depends(get_db)],
    admin: Annotated[CurrentUser, Depends(require_platform_admin)],
) -> HubConfirmResponse:
    """Store the stations an administrator accepted (at most 10 pairs, each
    a code, a VIATOR id or both), after one lookup of the module with each
    distinct code and id once (at most 20 keys): a pair the module serves
    is stored (its code with uic_origin 'msmm', and the VIATOR id the module
    answered, the current one); a pair it does not serve is refused and its
    hub left as it was. A pair with an id alone stores the id and no code:
    the module serves that station without one. Each write is guarded: a
    hub whose code or id was changed while the module was asked is left
    with that change (`changed_meanwhile`). On any failure nothing is
    written."""
    confirm = _body_or_422(HubConfirmRequest, body, _CONFIRM_REFUSED)
    user_id = _caller(admin)
    refusal = _module_refusal()
    if refusal is not None:
        return HubConfirmResponse(status="unavailable", message=refusal, results=[])
    hubs = await db_step(_hubs_by_id, db, [pair.hub_id for pair in confirm.pairs])
    # The hubs as they were before the module is asked: each write below is
    # guarded by them (`_guarded_write`).
    before = {hub_id: (hub.uic, hub.station_id) for hub_id, hub in hubs.items()}
    codes, ids = _lookup_keys([pair for pair in confirm.pairs if pair.hub_id in hubs])
    by_code: dict[str, dict[str, Any]] = {}
    by_id: dict[str, dict[str, Any]] = {}
    if codes or ids:
        outcome = await _lookup(codes, ids, user_id)
        if outcome.rows is None:
            status, message, retry_after = _failure(outcome)
            return HubConfirmResponse(
                status=status, message=message, retry_after=retry_after, results=[]
            )
        by_code, by_id = station_module.lookup_by_key(outcome.rows, codes, ids)
    results = await db_step(_store_confirmed, db, confirm, hubs, before, by_code, by_id)
    return HubConfirmResponse(status="ok", results=results)


def _store_confirmed(
    db: DbSession,
    confirm: HubConfirmRequest,
    hubs: dict[str, NetworkCoverageHub],
    before: dict[str, tuple[str | None, str | None]],
    by_code: dict[str, dict[str, Any]],
    by_id: dict[str, dict[str, Any]],
) -> list[HubConfirmed]:
    """A confirm's guarded writes and their commit, in the thread pool (#397);
    one result per pair."""
    now = datetime.now(UTC)
    results: list[HubConfirmed] = []
    for pair in confirm.pairs:
        hub = hubs.get(pair.hub_id)
        row, station_id = _confirmed_row(pair, by_code, by_id)
        if hub is None:
            results.append(HubConfirmed(hub_id=pair.hub_id, uic=pair.uic, state="unknown_hub"))
        elif row is None:
            results.append(HubConfirmed(hub_id=pair.hub_id, uic=pair.uic, state="not_served"))
        elif not _guarded_write(
            db,
            pair.hub_id,
            before[pair.hub_id],
            {
                "uic": pair.uic,
                "uic_origin": None if pair.uic is None else "msmm",
                "station_id": station_id,
                "updated_at": now,
            },
        ):
            results.append(
                HubConfirmed(hub_id=pair.hub_id, uic=pair.uic, state="changed_meanwhile")
            )
        else:
            results.append(
                HubConfirmed(
                    hub_id=pair.hub_id,
                    uic=pair.uic,
                    state="stored",
                    module_name=row["name"],
                    station_id=station_id,
                )
            )
    db.commit()
    return results


def _same_name(ours: str, theirs: str) -> bool:
    """Names compared without regard to capitals or runs of white space."""
    return " ".join(ours.split()).casefold() == " ".join(theirs.split()).casefold()


def _check_key(hub: NetworkCoverageHub, *, use_ids: bool = True) -> tuple[str, str] | None:
    """What a check asks the module for a hub: its VIATOR id when it has a
    valid one (VIATOR keys a station by its id: the id's row may carry
    another code than the hub's, the anchor's), else its code when the
    lookup accepts it; None for neither (shown as not served, never sent).
    `use_ids` false: the code alone (a module from before the ids)."""
    if use_ids and station_module.is_viator_id(hub.station_id):
        return ("id", hub.station_id)
    if hub.uic and station_module.code_refused(hub.uic) is None:
        return ("uic", hub.uic)
    return None


def _new_id(hub: NetworkCoverageHub, row: dict[str, Any], via: str) -> str | None:
    """The VIATOR id a check would give the hub, or None to leave it: asked
    by its id, the survivor when the module says the id was merged
    (`replaced_by`); asked by its code while it has no id, the id the module
    serves the code under. Never cleared, and never moved by a row asked by
    code for a hub whose id went unanswered (the administrator decides)."""
    found = station_module.current_id(row)
    if found is None or found == hub.station_id:
        return None
    if via == "id" or (via == "uic" and hub.station_id is None):
        return found
    return None


def _checked(
    hub: NetworkCoverageHub,
    found: tuple[dict[str, Any], str] | None,
    unanswered: station_module.Outcome | None = None,
) -> tuple[HubChecked, str | None]:
    """What a check shows for a hub, and the VIATOR id it would write (None:
    nothing to write). `found`: the module's row and how it was asked
    (`id`, `uic`, or `code_after_id`: the hub's code, its id unanswered);
    `unanswered`: the failure of the hub's second lookup."""
    if unanswered is not None:
        status, message, retry_after = _failure(unanswered)
        if status != "limited":
            message = "The station module did not answer."
        result = HubChecked(
            hub_id=hub.id,
            hub_name=hub.name,
            uic=hub.uic,
            uic_origin=hub.uic_origin,
            state="no_answer",
            station_id=hub.station_id,
            id_not_answered=True,
            message=message,
            retry_after=retry_after,
        )
        return result, None
    if found is None:
        result = HubChecked(
            hub_id=hub.id,
            hub_name=hub.name,
            uic=hub.uic,
            uic_origin=hub.uic_origin,
            state="not_served",
            station_id=hub.station_id,
        )
        return result, None
    row, via = found
    new_id = _new_id(hub, row, via)
    state: Literal["ok", "name_differs"] = (
        "ok" if _same_name(hub.name, row["name"]) else "name_differs"
    )
    result = HubChecked(
        hub_id=hub.id,
        hub_name=hub.name,
        uic=hub.uic,
        uic_origin=hub.uic_origin,
        state=state,
        module_name=row["name"],
        parent_uic=row.get("parent_uic"),
        station_id=new_id or hub.station_id,
        replaced_id=hub.station_id if new_id is not None and hub.station_id is not None else None,
        id_not_answered=via == "code_after_id",
        module_uic=row["uic"],
        code_origin=row["uic_origin"],
        code_origin_text=station_module.uic_origin_text(row["uic_origin"]),
        licence=row["licence"],
    )
    return result, new_id


@router.post("/hubs/check", responses=_HUB_CODE_RESPONSES)
async def check_hub_codes(
    body: Annotated[Any, Body()],
    db: Annotated[DbSession, Depends(get_db)],
    admin: Annotated[CurrentUser, Depends(require_platform_admin)],
) -> HubCheckResponse:
    """Re-read the stations of at most 20 active hubs with one lookup of the
    module (a hub's VIATOR id when it has one, else its code; each distinct
    key once), and show each hub, with the origin and the licence of its
    station read live from the module (D-R11 A), and whether the module no
    longer serves it or names it otherwise. A hub whose id gets no answer
    but which has a code is asked again by its code, in a second lookup
    (`id_not_answered`). Writes only the hubs' VIATOR ids, each guarded by
    the hub as it was read (`_guarded_write`; `changed_meanwhile` when an
    edit came in between), one commit: an id the module says was merged
    moves to the survivor, and a hub without an id takes the one the module
    serves its code under. Clears nothing: the administrator decides."""
    asked = _body_or_422(HubBatchRequest, body, _BATCH_REFUSED)
    user_id = _caller(admin)
    hubs = await db_step(_hubs_to_look_at, db, resolved=True, skip=asked.skip)
    refusal = _module_refusal()
    if refusal is not None:
        return HubCheckResponse(status="unavailable", message=refusal, results=[], left=len(hubs))
    batch = hubs[:CHECK_BATCH]
    before = {hub.id: (hub.uic, hub.station_id) for hub in batch}
    found = await _check_lookup(batch, user_id)
    if isinstance(found, station_module.Outcome):
        status, message, retry_after = _failure(found)
        return HubCheckResponse(
            status=status, message=message, retry_after=retry_after, results=[], left=len(hubs)
        )
    rows, unanswered = found
    checked = [_checked(hub, rows.get(hub.id), unanswered.get(hub.id)) for hub in batch]
    results = await db_step(_write_checked, db, before, checked)
    return HubCheckResponse(status="ok", results=results, left=len(hubs) - len(batch))


def _write_checked(
    db: DbSession,
    before: dict[str, tuple[str | None, str | None]],
    checked: list[tuple[HubChecked, str | None]],
) -> list[HubChecked]:
    """A check's guarded writes and their one commit (when any hub has an id
    to write), in the thread pool (#397); the results as shown."""
    results = [_written(db, before, result, new_id) for result, new_id in checked]
    if any(new_id is not None for _result, new_id in checked):
        db.commit()
    return results


def _written(
    db: DbSession,
    before: dict[str, tuple[str | None, str | None]],
    result: HubChecked,
    new_id: str | None,
) -> HubChecked:
    """Write the id a check found for one hub, guarded; the result as shown:
    unchanged when there is nothing to write or the write went through,
    else marked `changed_meanwhile`, with the id the hub had when read."""
    if new_id is None:
        return result
    values = {"station_id": new_id, "updated_at": datetime.now(UTC)}
    if _guarded_write(db, result.hub_id, before[result.hub_id], values):
        return result
    return result.model_copy(
        update={
            "station_id": before[result.hub_id][1],
            "replaced_id": None,
            "changed_meanwhile": True,
        }
    )


async def _ask(
    keys: dict[str, tuple[str, str] | None], user_id: uuid.UUID
) -> dict[str, dict[str, Any]] | station_module.Outcome:
    """One lookup with each distinct key of `keys` (hub id → key) once: the
    module's row by hub id (a hub it does not answer is absent), or the
    failed `Outcome`."""
    codes = list(dict.fromkeys(key[1] for key in keys.values() if key and key[0] == "uic"))
    ids = list(dict.fromkeys(key[1] for key in keys.values() if key and key[0] == "id"))
    if not codes and not ids:
        return {}
    outcome = await station_module.lookup(codes, user_id, ids=ids)
    if outcome.rows is None:
        return outcome
    by_code, by_id = station_module.lookup_by_key(outcome.rows, codes, ids)
    rows = {hub_id: _row_of(key, by_code, by_id) for hub_id, key in keys.items()}
    return {hub_id: row for hub_id, row in rows.items() if row is not None}


_CheckRows = dict[str, tuple[dict[str, Any], str]]


async def _check_lookup(
    batch: list[NetworkCoverageHub], user_id: uuid.UUID
) -> tuple[_CheckRows, dict[str, station_module.Outcome]] | station_module.Outcome:
    """The module's row of each hub of a check click and how it was asked
    (`id`, `uic`, `code_after_id`), by hub id, and the hubs whose second
    lookup failed, with its failure; the failed `Outcome` when the first
    lookup fails. A stored code the module would refuse (only a hand edit
    of the table can store one) is never sent. A module from before the
    VIATOR ids (its 422 on `ids`) is asked once more with the codes alone.
    A hub asked by its id that gets no row but has a code is asked by its
    code in a second lookup (at most 20 keys more); when that one fails
    (a limit, a timeout, an error), those hubs are shown as not answered,
    never as not served."""
    keys = {hub.id: _check_key(hub) for hub in batch}
    first = await _ask(keys, user_id)
    by_id_asked = {hub_id for hub_id, key in keys.items() if key and key[0] == "id"}
    if isinstance(first, station_module.Outcome):
        if not (by_id_asked and first.reason == _IDS_REFUSED):
            return first
        codes_only = {hub.id: _check_key(hub, use_ids=False) for hub in batch}
        again = await _ask(codes_only, user_id)
        if isinstance(again, station_module.Outcome):
            return again
        return {hub_id: (row, "uic") for hub_id, row in again.items()}, {}
    found = {
        hub_id: (row, key[0]) for hub_id, row in first.items() if (key := keys[hub_id]) is not None
    }
    lost = {
        hub.id: _check_key(hub, use_ids=False)
        for hub in batch
        if hub.id in by_id_asked and hub.id not in first
    }
    lost = {hub_id: key for hub_id, key in lost.items() if key is not None}
    if not lost:
        return found, {}
    again = await _ask(lost, user_id)
    if isinstance(again, station_module.Outcome):
        return found, dict.fromkeys(lost, again)
    found.update({hub_id: (row, "code_after_id") for hub_id, row in again.items()})
    return found, {}


def _row_of(
    key: tuple[str, str] | None,
    by_code: dict[str, dict[str, Any]],
    by_id: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    if key is None:
        return None
    return (by_id if key[0] == "id" else by_code).get(key[1])


# ───────── the hub picker: a station of the module for Add / Edit hub (#398) ─────────
#
# The Add hub and Edit hub dialog picks the hub's station in the module
# instead of having every field typed: a country (the module's countries,
# else VIATOR's list), a city or station name of 3 characters or more, then
# one of at most 10 stations. The dialog fills in the name, a short label, a
# slug (create only), the position and the country; on save it stores the
# hub (POST or PATCH, as typed by hand) and then its station through POST
# /hubs/confirm, exactly as a code confirmed from "Propose station codes"
# (origin 'msmm', the current VIATOR id from the module's lookup). One search
# per keystroke pause on the administrator's own limits at the module (the
# admin search: a failure never pauses the typeahead of every user), two
# with an older module (below), and one lookup when the hub is saved.

# The most stations one search offers (the module's own maximum).
PICK_MAX = station_module.MAX_ROWS
# The short label proposed for a picked station: the matrix header's width.
PICK_SHORT_MAX = 12
# The slug proposed when a station's name gives no Latin letter or digit, even
# in Latin letters (#411: a Cyrillic or Greek name is transliterated first);
# the station says so (`slug_placeholder`) and the dialog asks for a slug.
_SLUG_FALLBACK = "hub"
_HYPHENS = re.compile(r"-{2,}")
_ISO_COUNTRY = re.compile(r"[A-Z]{2}", re.ASCII)
_PICK_REFUSED = (
    'The body must be {"q": <text>, "country": <two capital letters>} and nothing else; '
    "the text 3 to 100 characters with no control character and no lone surrogate."
)
_PICK_MODULE_OFF = "The station module is not configured; fill in the hub by hand."
_PICK_TEXT_REFUSED = (
    "The station module does not take this text; try other words, or fill in the hub by hand."
)
# The text rule of the typeahead in plain words (its own sentences name the
# field `q`): nothing is sent to the module for such a text.
_PICK_TEXT_LENGTH = "Type 3 to 100 characters, at least 3 of them letters or digits."
_PICK_TEXT_CONTROL = (
    "The text holds an invisible character the search does not take; type it again."
)
# A country the module would refuse: never sent (the module would count the
# call and answer 422, which reads as an older module).
_PICK_COUNTRY_NOT_LISTED = (
    "The station module does not list this country; choose a country in the list, "
    "or fill in the hub by hand."
)
_PICK_COUNTRY_NOT_ISO = (
    "This country code is not one the station module takes (an assigned ISO code, or XK); "
    "choose a country in the list, or fill in the hub by hand."
)
# An older module refuses the `country` field (MSMM issue #69): it was asked
# without it, and only the stations of the country are kept from its ten.
_PICK_INCOMPLETE = (
    "The station module is older than this search: it was asked without the country, and "
    "only its stations in this country are listed, so the list may be incomplete."
)


class HubPickRequest(BaseModel):
    """Body of POST /hubs/pick: the typed text and the chosen country."""

    model_config = ConfigDict(extra="forbid", strict=True)

    q: str
    country: str = Field(pattern=r"^[A-Z]{2}$")


class HubPickStation(BaseModel):
    """A station offered by the picker, with the hub fields it fills in:
    `short` (at most 12 characters) and `slug` (lowercase and hyphens,
    unique among the hubs, the soft-deleted ones included, and within the
    answer) are proposals the administrator may edit, both made from the
    name in Latin letters (#411); `latin_name` is that form when the
    module's `name` is in Cyrillic or Greek script (else None), and
    `slug_placeholder` says that the name gave no Latin letter or digit, so
    the slug is the bare `hub` (numbered) and one must be typed;
    `latin_incomplete` that some letters of the name have no Latin form
    here (a Kazakh letter, say), so the slug and the short label lack them;
    an empty `short` (a name of signs only, `ъ` or `ь`) must be typed; `uic` and
    `station_id` (the station's current VIATOR id) are what the dialog
    sends to POST /hubs/confirm once the hub is saved."""

    name: str
    latin_name: str | None = None
    short: str
    slug: str
    slug_placeholder: bool = False
    latin_incomplete: bool = False
    country: str
    lat: float
    lon: float
    uic: str | None
    station_id: str | None


class HubPickResponse(BaseModel):
    """`status`: `ok`; `refused` (the module does not take the text);
    `limited` (the module's limit is reached: `retry_after` seconds, or None
    when it gave none); `unavailable` (not configured, paused or failed).
    `incomplete`: the module did not take the country (an older module), so
    the list holds only the country's stations among its ten."""

    status: Literal["ok", "refused", "limited", "unavailable"]
    message: str | None = None
    retry_after: int | None = None
    stations: list[HubPickStation] = Field(default_factory=list)
    incomplete: bool = False


class HubCountry(BaseModel):
    code: str
    name: str


class HubCountriesResponse(BaseModel):
    """The countries of the picker: `module` (the module's served
    countries) or `viator` (VIATOR's own list, when the module is not
    configured or did not answer)."""

    source: Literal["module", "viator"]
    countries: list[HubCountry]


def _pick_query_or_422(text: str) -> str:
    """The typed text as the module searches for it (the typeahead's rule,
    `station_suggest.normalise_query`), or a 422 with a sentence in plain
    words: never sent when the module would refuse it."""
    try:
        return station_suggest.normalise_query(text)
    except ValueError:
        invisible = any(unicodedata.category(character) in ("Cc", "Cs") for character in text)
        detail = _PICK_TEXT_CONTROL if invisible else _PICK_TEXT_LENGTH
        raise HTTPException(status_code=422, detail=detail) from None


def _pick_country_refusal(country: str, served: frozenset[str] | None) -> str | None:
    """Why `country` is not sent to the module, or None to send it: a country
    the module's own list does not hold, or, when that list could not be
    read, a code outside the assigned ISO codes and XK (the module's rule).
    So a 422 of the module means an older module, not a refused country."""
    if served:
        return None if country in served else _PICK_COUNTRY_NOT_LISTED
    return None if country in station_module.SEARCH_COUNTRIES else _PICK_COUNTRY_NOT_ISO


def _pick_rows(
    rows: list[dict[str, Any]], country: str, *, only_country: bool
) -> list[dict[str, Any]]:
    """The rows a hub can take: a code the lookup accepts or a VIATOR id
    (`_confirmable`), a position the hub form accepts, each station once,
    in the module's order, at most PICK_MAX. `only_country`: the rows of
    other countries are dropped (an older module, asked without it)."""
    kept: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        key = station_module.station_key(row)
        if key in seen or not _confirmable(row):
            continue
        if station_module.position_refused(row["latitude"], row["longitude"]) is not None:
            continue
        if only_country and row["country_iso"] != country:
            continue
        seen.add(key)
        kept.append(row)
    return kept[:PICK_MAX]


def slug_base(name: str, country: str | None = None) -> str:
    """The slug a station's name proposes before uniqueness: in Latin
    letters (Cyrillic by the rules of `country`), lowercase, accents off,
    every other run of characters one hyphen (`hub_derive.slugify`, whose
    hyphens of the name stay, so `ZZ - Zzville` would give three: they are
    made one), or `hub` when nothing is left."""
    return _HYPHENS.sub("-", hub_derive.slugify(name, country)).strip("-") or _SLUG_FALLBACK


def unique_slug(base: str, taken: set[str]) -> str:
    """`base` when no hub holds it, else `base-2`, `base-3`, …: the first free."""
    if base not in taken:
        return base
    number = 2
    while f"{base}-{number}" in taken:
        number += 1
    return f"{base}-{number}"


def _taken_slugs(db: DbSession, bases: set[str]) -> set[str]:
    """The hub ids (soft-deleted hubs included: the id is the primary key)
    equal to one of `bases` or of the form `<base>-<anything>`. A slug holds
    only small letters, digits and hyphens: nothing LIKE reads specially."""
    if not bases:
        return set()
    matches = [
        or_(NetworkCoverageHub.id == base, NetworkCoverageHub.id.like(f"{base}-%"))
        for base in sorted(bases)
    ]
    return set(db.execute(select(NetworkCoverageHub.id).where(or_(*matches))).scalars().all())


def _pick_stations(db: DbSession, rows: list[dict[str, Any]], country: str) -> list[HubPickStation]:
    """The offered stations with their proposed short labels and slugs,
    made from the name in Latin letters by the rules of its country (#411)."""
    countries = [
        row["country_iso"]
        if isinstance(row["country_iso"], str) and _ISO_COUNTRY.fullmatch(row["country_iso"])
        else country
        for row in rows
    ]
    bases = [slug_base(row["name"], code) for row, code in zip(rows, countries, strict=True)]
    taken = _taken_slugs(db, set(bases))
    stations: list[HubPickStation] = []
    for row, base, code in zip(rows, bases, countries, strict=True):
        slug = unique_slug(base, taken)
        taken.add(slug)
        name = row["name"]
        latin = translit.to_latin(name, code)
        stations.append(
            HubPickStation(
                name=name,
                latin_name=None if translit.is_latin(name) else latin,
                short=hub_derive.latin_short(name, code, PICK_SHORT_MAX),
                slug=slug,
                slug_placeholder=not hub_derive.slugify(name, code),
                latin_incomplete=translit.has_foreign_letters(latin),
                country=code,
                lat=row["latitude"],
                lon=row["longitude"],
                uic=row["uic"],
                station_id=station_module.current_id(row),
            )
        )
    return stations


def _pick_failure(outcome: station_module.Outcome) -> HubPickResponse:
    """The answer of a failed picker search: the module's refusal of the
    text, its limit with its Retry-After, or that it did not answer."""
    if outcome.reason == _TEXT_REFUSED:
        return HubPickResponse(status="refused", message=_PICK_TEXT_REFUSED)
    if outcome.reason == "off":
        return HubPickResponse(status="unavailable", message=_PICK_MODULE_OFF)
    status, message, retry_after = _failure(outcome)
    return HubPickResponse(status=status, message=message, retry_after=retry_after)


@router.post("/hubs/pick", responses=_HUB_CODE_RESPONSES)
async def pick_hub_station(
    body: Annotated[Any, Body()],
    db: Annotated[DbSession, Depends(get_db)],
    admin: Annotated[CurrentUser, Depends(require_platform_admin)],
) -> HubPickResponse:
    """At most 10 stations of the module for a typed city or station name in
    one country, for the Add hub / Edit hub dialog: one admin search
    (`station_module.admin_search_outcome`, with `country`) on behalf of the
    administrator. A country the module does not list (or, without its
    list, not an assigned ISO code or XK) is refused with no call. A module
    without the `country` field answers 422: it is asked once more without
    it, and only the rows of the country are kept (`incomplete`). Writes
    nothing; the text is never logged."""
    asked = _body_or_422(HubPickRequest, body, _PICK_REFUSED)
    query = _pick_query_or_422(asked.q)
    if not station_module.enabled():
        return HubPickResponse(status="unavailable", message=_PICK_MODULE_OFF)
    refusal = _pick_country_refusal(asked.country, await station_module.countries())
    if refusal is not None:
        return HubPickResponse(status="refused", message=refusal)
    user_id = _caller(admin)
    outcome = await station_module.admin_search_outcome(query, user_id, country=asked.country)
    incomplete = outcome.reason == _TEXT_REFUSED
    if incomplete:
        outcome = await station_module.admin_search_outcome(query, user_id)
    if outcome.rows is None:
        return _pick_failure(outcome)
    rows = _pick_rows(outcome.rows, asked.country, only_country=incomplete)
    return HubPickResponse(
        status="ok",
        message=_PICK_INCOMPLETE if incomplete else None,
        stations=await db_step(_pick_stations, db, rows, asked.country),
        incomplete=incomplete,
    )


@router.get("/hubs/countries")
async def hub_countries(
    _: Annotated[CurrentUser, Depends(require_platform_admin)],
) -> HubCountriesResponse:
    """The picker's countries: those of the module's served stations
    (`station_module.countries`, cached 60 s, no limit counted), or VIATOR's
    own list (`osm_geo.COUNTRY_NAMES`) when the module is not configured,
    paused, failed or serves none; by code, each with its name when VIATOR
    knows it (`osm_geo.BUNDLED_COUNTRY_NAMES`: every bundled country, #411)."""
    served = await station_module.countries()
    source: Literal["module", "viator"] = "module" if served else "viator"
    codes = sorted(served) if served else sorted(osm_geo.COUNTRY_NAMES)
    return HubCountriesResponse(
        source=source,
        countries=[
            HubCountry(code=code, name=osm_geo.BUNDLED_COUNTRY_NAMES.get(code, code))
            for code in codes
        ],
    )


@router.get("/runs", response_model=list[RunSummary])
def list_runs(
    db: Annotated[DbSession, Depends(get_db)],
    _: Annotated[CurrentUser, Depends(require_platform_admin)],
    limit: int = 20,
) -> list[RunSummary]:
    """Recent coverage runs, newest first. The admin page sidebar.

    PR-190 — surfaces per-cell response_ms min/avg/max in the banner.
    Aggregated in one batched query so a 20-run sidebar still costs
    exactly one extra round-trip.
    """
    rows = runner.list_recent_runs(db, limit=limit)
    stats_by_run = _aggregate_response_ms(db, [r.id for r in rows])
    # One config read for the whole sidebar, not one per run.
    cfg = runner._load_coverage_config(db)
    return [_run_to_summary(r, response_ms_stats=stats_by_run.get(r.id), cfg=cfg) for r in rows]


@router.post(
    "/runs",
    # response_model intentionally omitted — Sonar S7191 flags it as
    # redundant with the `-> RunSummary` return annotation, which
    # FastAPI uses verbatim as the response schema. Behaviour is
    # bit-equivalent (same OpenAPI spec, same serialisation filter).
    status_code=201,
    responses={
        # Sonar S8415 — declare the HTTPException response codes the
        # body can raise so the generated OpenAPI spec is truthful and
        # downstream clients can build matching error-handling.
        400: {
            "description": (
                "Invalid mode/session_id pairing, invalid direction, or "
                "the requested session is not in 'serving' state."
            )
        },
        404: {"description": "session_id refers to a session that does not exist"},
        409: {
            "description": (
                "mode='fanout' requested but no session is both 'serving' and 'include_in_fanout'"
            )
        },
    },
)
def create_run(
    body: RunCreate,
    db: Annotated[DbSession, Depends(get_db)],
    actor: Annotated[CurrentUser, Depends(require_platform_admin)],
) -> RunSummary:
    """Start a new coverage run.

    Two modes (PR #36):
      - 'single_session' (legacy): queries every pair against `session_id`.
                                   `session_id` is required and must be
                                   in 'serving' state.
      - 'fanout':                  queries every pair against every
                                   serving + include_in_fanout session in
                                   parallel and merges results by trip
                                   signature. `session_id` must be omitted.
                                   At least one serving + fanout-enabled
                                   session must exist.

    Validates pre-flight, creates the run row in pending state and returns
    the summary so the UI can start polling immediately. #392: nothing is
    executed here; the worker's coverage executor claims the run (it shows
    "queued" until then).
    """
    if body.direction not in ("both", "single"):
        raise HTTPException(400, "direction must be 'both' or 'single'")

    _validate_run_create_mode(body, db)

    # A naive `depart_at` (what `<input type="datetime-local">` posts —
    # no offset on the wire) is localised by `runner.create_run` against
    # the run's own `window_timezone`. It is deliberately NOT stamped UTC
    # here: doing so read the operator's "06:40" as 06:40Z, i.e. 08:40 on
    # a Europe/Brussels run, silently clipping the early-morning trips off
    # the front of every depart_at-anchored comparison. One zone per run.
    depart_at = body.depart_at

    # PR-3 — translate "HH:MM" / "24:00" strings into the DB's TIME type.
    # "24:00" stores as 00:00 (the runner detects the sentinel by
    # comparing against window_start_local at execute time and bumps
    # the end day by one).
    window_start = _hhmm_to_time(body.window_start_local)
    window_end = _hhmm_to_time(body.window_end_local)

    try:
        run = runner.create_run(
            db,
            actor_user_id=actor.id,
            session_id=body.session_id,
            depart_at=depart_at,
            direction=body.direction,
            mode=body.mode,
            countries=body.countries,
            verify_externally=body.verify_externally,
            window_start_local=window_start,
            window_end_local=window_end,
            window_timezone=body.window_timezone,
            reference_date_value=body.reference_date,
        )
    except ValueError as e:
        # `runner.create_run` raises ValueError when the country filter
        # matches zero hubs (e.g. operator picked AT but never added an
        # AT hub). Surface as 400 with the runner's message so the UI
        # can show it directly under the country picker.
        raise HTTPException(400, str(e)) from e
    db.commit()
    db.refresh(run)
    # #392 — the run stays `pending` until the worker's coverage executor
    # claims it; a restart of web can no longer end it.
    return _run_to_summary(run, cfg=runner._load_coverage_config(db))


def _resolve_hubs(db: DbSession) -> list[HubInfo]:
    """List of active hubs for the matrix axis, DB-backed with the same
    static-list fallback as `list_hubs()` for fresh installs / dev envs."""
    hub_rows = (
        db.execute(
            select(NetworkCoverageHub)
            .where(NetworkCoverageHub.is_active.is_(True))
            .order_by(
                NetworkCoverageHub.country,
                NetworkCoverageHub.sort_order,
                NetworkCoverageHub.id,
            )
        )
        .scalars()
        .all()
    )
    if hub_rows:
        return [_hub_to_info(h) for h in hub_rows]
    return [
        HubInfo(
            id=h.id,
            name=h.name,
            short=h.short,
            region=h.region,
            country="FR",
            tier="regional" if h.id == "batz" else "main",
            lat=h.lat,
            lon=h.lon,
            is_active=True,
            sort_order=0,
        )
        for h in STATIC_HUBS
    ]


# Cap on how many itineraries per search get full leg detail embedded in
# the export/share HTML. K-slot coverage (runner.py's `slot_count` x
# `num_itineraries_per_slot`, 6x10 by default) can legitimately accumulate
# 50+ deduped itineraries for a single pair — harmless in the DB, but
# fatal to embed in full for every cell of a large matrix: a real
# 8742-pair fanout run was found carrying 221k trip rows / ~372MB of raw
# `legs` JSON, which ballooned to 13+GB and pegged the web process's CPU
# once Jinja's `tojson` had to serialize it all (twice, before the
# companion fix that de-duplicated the raw-data panel's own JSON dump).
# `cells[key].num_itineraries` (from the result row, not this list) still
# reports the true total, so nothing about the run's own findings is
# hidden — only how many of them get full leg detail in this view. The
# modal notes when it's showing a truncated set.
_MAX_TRIPS_PER_CELL_EXPORT = 10

# Above this many result rows, the DOWNLOADED export drops leg-by-leg
# detail from the embedded trips (summaries — times, duration, transfers,
# modes — stay). Legs average ~1.7KB/trip and are ~90% of the report's
# bytes: a real 8742-pair run still produced a ~150MB file after the
# per-cell cap above, which no browser can open even when the download
# succeeds. 500 pairs x 10 trips x ~1.7KB keeps the worst full-detail
# file under ~10MB. The ONLINE share page is unaffected — it lazy-loads
# each cell's full detail on click (see app/api/coverage_share.py), so
# it keeps full fidelity at any run size.
_EXPORT_LEG_DETAIL_MAX_PAIRS = 500


def _fetch_trips_by_search(
    db: DbSession,
    search_ids: list[uuid.UUID],
    depart_at: datetime | None = None,
    *,
    include_legs: bool = True,
) -> dict[str, list[dict[str, Any]]]:
    """Bulk-fetch every JourneyTrip linked (via JourneySearchExecution) to
    the given JourneySearch ids.

    Why the JOIN: `coverage_results.journey_search_id` is a FK to
    `JourneySearch.id` (the parent search), not to `JourneySearchExecution.id`
    (per-engine execution rows hung off it). Trips live under executions, so
    the chain is search -> executions -> trips. One execution per session in
    fanout mode; a single execution per search in single-session mode.

    Keys the result by `str(search_id)` so the cell-builder can index
    directly via `coverage_results.journey_search_id` without any further
    translation. When a search has multiple executions (fanout), their
    trips are unioned under the same key — matches what the operator sees
    in the matrix cell ("X itineraries across N sessions").

    Was previously `_fetch_trips_by_exec` keying off `execution_id`; that
    looked correct in tests with synthetic data but failed in production
    because real coverage rows store the *search_id*, not the execution_id.
    Fixed 2026-06-25 after every exported cell came back trip-less.

    Capped at `_MAX_TRIPS_PER_CELL_EXPORT` per search — see that constant's
    comment.

    `depart_at`, when given, restricts to trips departing at/after it and
    orders chronologically instead of by `rank_in_response` (both applied
    at the SQL level, so an out-of-window trip is never even pulled off
    the wire — same memory-safety spirit as `include_legs=False` below).
    ÖBB's verify-sweep call is a single forward-looking search anchored
    at the run's depart_at, while a cell's VIATOR trips span the whole
    K-slot day window (runner.py) — without this filter, the side-by-side
    comparison (and the persisted alignment score — see
    `runner._fetch_viator_trips_for_search`, which applies the same
    filter Python-side since that path isn't a bulk/streaming query)
    could pit ÖBB's few depart_at-anchored itineraries against VIATOR
    trips from hours before or after, an apples-to-oranges scope
    mismatch. `None` (the default) keeps the original best-ranked-first
    behaviour for callers with no natural depart_at to compare against.

    `include_legs=False` (large exports — see `_EXPORT_LEG_DETAIL_MAX_PAIRS`)
    projects explicit columns instead of full ORM rows so the legs JSON is
    never even pulled out of Postgres — on the 8742-pair run that's ~372MB
    of wire traffic skipped, not just dropped after fetching. Trips then
    carry `"legs": []`, which the export modal renders as a summary row.
    """
    if not search_ids:
        return {}
    base = (
        select(JourneySearchExecution.search_id, JourneyTrip)
        if include_legs
        else select(
            JourneySearchExecution.search_id,
            JourneyTrip.rank_in_response,
            JourneyTrip.duration_seconds,
            JourneyTrip.num_transfers,
            JourneyTrip.departure_at,
            JourneyTrip.arrival_at,
            JourneyTrip.modes,
        )
    )
    stmt = base.join(JourneyTrip, JourneyTrip.execution_id == JourneySearchExecution.id).where(
        JourneySearchExecution.search_id.in_(search_ids)
    )
    stmt = (
        stmt.where(JourneyTrip.departure_at >= depart_at).order_by(
            JourneySearchExecution.search_id, JourneyTrip.departure_at
        )
        if depart_at is not None
        else stmt.order_by(JourneySearchExecution.search_id, JourneyTrip.rank_in_response)
    )
    rows = db.execute(stmt).all()
    if include_legs:
        records = (
            (
                search_id,
                t.rank_in_response,
                t.duration_seconds,
                t.num_transfers,
                t.departure_at,
                t.arrival_at,
                t.modes,
                t.legs,
            )
            for search_id, t in rows
        )
    else:
        records = ((*row, []) for row in rows)
    out: dict[str, list[dict[str, Any]]] = {}
    for (
        search_id,
        rank,
        duration_seconds,
        num_transfers,
        departure_at,
        arrival_at,
        modes,
        legs,
    ) in records:
        bucket = out.setdefault(str(search_id), [])
        if len(bucket) >= _MAX_TRIPS_PER_CELL_EXPORT:
            continue
        bucket.append(
            {
                "rank": rank,
                "duration_seconds": duration_seconds,
                "num_transfers": num_transfers,
                "departure_at": departure_at.isoformat() if departure_at else None,
                "arrival_at": arrival_at.isoformat() if arrival_at else None,
                "modes": modes,
                "legs": legs,
            }
        )
    return out


def _depart_at_outside_window(run: NetworkCoverageRun, cfg: runner.CoverageConfig) -> bool:
    """True when the run did NOT search the day its `depart_at` names.

    The K-slot grid a run executes is composed from `run.reference_date` +
    `window_*_local` (`runner._resolve_run_window`); `run.depart_at` is a
    separate instant. On runs created before
    `runner._anchor_depart_at_and_reference_date`, `reference_date`
    defaulted to "tomorrow at create time", so the two routinely named
    different calendar days — and `_fetch_trips_by_search`'s
    `JourneyTrip.departure_at >= depart_at` predicate then matched ZERO of
    the run's own trips, rendering an empty VIATOR column next to a
    non-zero `num_itineraries`.

    Deliberately a calendar-DAY comparison, not window containment: an
    operator who narrows the window to 06:00-22:00 and departs at 05:30
    has a perfectly-anchored run whose depart_at merely precedes the first
    slot — badging that as "wrong day" would slander a valid run and
    silently disable PR #221's filter for it.

    An unresolvable window counts as "outside": both callers degrade to
    the safe, pre-filter behaviour rather than hiding trips.
    """
    try:
        return not runner.reference_date_matches_depart_at(run, cfg)
    except Exception:
        log.warning("could not resolve window for run %s — treating depart_at as unusable", run.id)
        return True


def _safe_depart_at_iso(run: Any) -> str:
    """Raw (UTC) `depart_at` ISO string. Only used when no `cfg` is
    available to localise it — see `_run_to_summary`."""
    depart_at = getattr(run, "depart_at", None)
    return depart_at.isoformat() if depart_at else ""


def _depart_at_local_iso(run: NetworkCoverageRun, cfg: runner.CoverageConfig) -> str:
    """`run.depart_at` rendered as a wall-clock in the run's own timezone.

    `depart_at` is persisted as an instant (`timestamptz`), and psycopg
    hands it back in UTC. Echoing that verbatim shows a `Europe/Brussels`
    operator "04:40" for the 06:40 they typed. Every display surface — run
    header, sidebar, export report, the journey deep-link — reads this
    field, so localise once here rather than at each of them.
    """
    try:
        tz = runner._resolve_timezone(run.window_timezone, cfg)
        return run.depart_at.astimezone(tz).isoformat()
    except Exception:  # never break the banner over a bad zone
        log.warning("could not localise depart_at for run %s", run.id)
        return run.depart_at.isoformat()


def _comparable_depart_at(db: DbSession, run: NetworkCoverageRun) -> datetime | None:
    """`run.depart_at`, but only when the run actually searched that day.

    A `depart_at` whose day the run never searched is exactly the case
    `_fetch_trips_by_search`'s own contract calls "no natural depart_at to
    compare against" — so return None and let it fall back to the
    pre-filter, rank-ordered list rather than showing the operator nothing.
    New runs always agree (create_run rejects a mismatched reference_date),
    so the filter fires normally and PR #221's intent is preserved.
    """
    cfg = runner._load_coverage_config(db)
    return None if _depart_at_outside_window(run, cfg) else run.depart_at


# Country → hue for the matrix's country band. MUST stay in sync with
# COUNTRY_HUES in network_coverage.html's <script> — Jinja (this file,
# server-rendered export) and the live matrix's client-side JS render
# the same header band independently and have no shared runtime to pull
# a single source of truth from. Same tradeoff already accepted for the
# viridis alignment palette (see CLAUDE.md's palette-reconciliation
# note); if you change one side, change the other.
_COUNTRY_HUES: dict[str, int] = {
    "AT": 160,
    "BE": 172,
    "CH": 185,
    "CZ": 197,
    "DE": 209,
    "DK": 222,
    "ES": 234,
    "FR": 246,
    "GB": 259,
    "HU": 271,
    "IT": 283,
    "NL": 296,
    "NO": 308,
    "PL": 320,
    "SE": 333,
}


def _country_color(country: str) -> str:
    hue = _COUNTRY_HUES.get(country)
    return "#5B6B82" if hue is None else f"hsl({hue}, 48%, 40%)"


def _annotate_hubs_with_country_bands(
    hubs: list[HubInfo],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Pre-computes what the export template needs to render the country
    band without doing any previtem/lookahead logic in Jinja: each hub
    dict gets `band_color` always, and `country_rowspan` only on the
    first hub of its country's run (mirrors the live matrix's rowspan
    merge). Also returns the column-axis runs for the header row.

    Hubs arrive already sorted by country (see `_resolve_hubs`), so
    "consecutive same country" is the whole run, not accidental
    adjacency.
    """
    annotated: list[dict[str, Any]] = []
    col_runs: list[dict[str, Any]] = []
    for i, hub in enumerate(hubs):
        # The station code and the VIATOR id stay out of the export and of
        # its public share page: neither uses them.
        row = hub.model_dump(exclude={"uic", "uic_origin", "station_id"})
        row["band_color"] = _country_color(hub.country)
        is_first_of_run = i == 0 or hubs[i - 1].country != hub.country
        if is_first_of_run:
            span = 1
            j = i + 1
            while j < len(hubs) and hubs[j].country == hub.country:
                span += 1
                j += 1
            row["country_rowspan"] = span
            col_runs.append({"country": hub.country, "span": span, "color": row["band_color"]})
        else:
            row["country_rowspan"] = None
        annotated.append(row)
    return annotated, col_runs


def _build_export_context(
    *,
    run: Any,
    results: list[Any],
    hubs: list[HubInfo],
    trips_by_search: dict[str, list[dict[str, Any]]],
    lazy_trips: bool = False,
    legs_omitted: bool = False,
    include_external_itineraries: bool = False,
    cfg: runner.CoverageConfig | None = None,
) -> dict[str, Any]:
    """Pure data-shaping from DB rows → template context dict.

    Extracted so unit tests can exercise the marshalling (run-meta keys,
    cell keying, trip attachment, status passthrough) without touching
    the DB or the FastAPI request pipeline. The endpoint itself becomes
    thin orchestration: query → marshal → render.

    Cells are keyed by `"<origin_id>:<dest_id>"` so the Jinja template can
    index by string concat — cleaner than nested loops with tuple keys.

    `lazy_trips` (share page) tells the template that trips were NOT
    embedded and the modal should fetch each cell's detail on click.
    `legs_omitted` (large downloads) tells it trips were embedded without
    leg detail so the modal can explain the gap. Both default False so
    the small-run download keeps its historical full-detail behaviour.

    `include_external_itineraries` embeds the verify sweep's persisted
    ÖBB itineraries per cell so the downloaded file can render the
    VIATOR-vs-ÖBB side-by-side offline. Only the small-run download
    turns it on (same tier as leg detail — the payloads are compact but
    there's no reason to grow the large export the slimming just
    shrank); the share page leaves it off and fetches per cell instead.
    """
    cells: dict[str, dict[str, Any]] = {}
    for r in results:
        key = f"{r.origin_hub_id}:{r.dest_hub_id}"
        cells[key] = {
            "status": r.status,
            "response_ms": r.response_ms,
            "num_itineraries": r.num_itineraries,
            "best_duration_seconds": r.best_duration_seconds,
            "best_num_transfers": r.best_num_transfers,
            "best_operators": r.best_operators,
            "error_message": r.error_message,
            "session_ids": getattr(r, "session_ids", None),
            "trips": trips_by_search.get(str(r.journey_search_id), [])
            if r.journey_search_id
            else [],
            # PR-E — persisted external-verify verdict so offline HTML
            # exports show the same per-cell dot the live matrix renders.
            "external_ok": getattr(r, "external_ok", None),
            "external_num_connections": getattr(r, "external_num_connections", None),
            "external_best_duration_seconds": getattr(r, "external_best_duration_seconds", None),
            "external_best_transfers": getattr(r, "external_best_transfers", None),
            "external_source": getattr(r, "external_source", None),
            "external_error": getattr(r, "external_error", None),
            "external_verified_at": (
                r.external_verified_at.isoformat()
                if getattr(r, "external_verified_at", None)
                else None
            ),
            # PR-196a — alignment heatmap signal, mirrored into the offline
            # export so operators reviewing a downloaded report see the same
            # tier-colored cells as the live matrix, not just the older
            # PR-E green/blue/yellow verdict dot.
            "external_alignment_tier": getattr(r, "external_alignment_tier", None),
            "external_alignment_score": getattr(r, "external_alignment_score", None),
            # Persisted ÖBB itineraries for the offline side-by-side —
            # None (key always present, for a stable raw-JSON schema)
            # unless the caller opted in; see the docstring.
            "external_itineraries": (
                getattr(r, "external_itineraries", None) if include_external_itineraries else None
            ),
        }
    run_meta = {
        "id": str(run.id),
        "session_id": run.session_id,
        "mode": getattr(run, "mode", "single_session"),
        "direction": run.direction,
        # Localised + wrong-day flagged exactly as the live matrix does,
        # so a downloaded report can't quietly disagree with the UI it
        # was exported from.
        "depart_at": (_depart_at_local_iso(run, cfg) if cfg else _safe_depart_at_iso(run)) or None,
        "depart_at_outside_window": bool(cfg and _depart_at_outside_window(run, cfg)),
        "reference_date": _safe_iso_date(getattr(run, "reference_date", None)),
        "status": run.status,
        "total_pairs": run.total_pairs,
        "completed_pairs": run.completed_pairs,
        "ok_pairs": run.ok_pairs,
        "no_route_pairs": run.no_route_pairs,
        "error_pairs": run.error_pairs,
        "created_at": run.started_at.isoformat() if run.started_at else None,
    }
    annotated_hubs, country_col_runs = _annotate_hubs_with_country_bands(hubs)
    return {
        "run": run_meta,
        "hubs": annotated_hubs,
        "country_col_runs": country_col_runs,
        "cells": cells,
        "lazy_trips": lazy_trips,
        "legs_omitted": legs_omitted,
    }


def _export_filename(run: Any) -> str:
    """`coverage-<sid-or-fanout>-<YYYYMMDD-HHMM>.html`.

    Operators tend to grab several reports at a time when comparing
    sessions or dates, so the timestamp prefix keeps the downloads sorted
    naturally in Finder / Explorer. Slashes in session ids are flattened
    to hyphens so the filename stays portable across OSes.
    """
    label = (run.session_id or "fanout").replace("/", "-")
    timestamp = run.started_at.strftime("%Y%m%d-%H%M") if run.started_at else "unknown"
    return f"coverage-{label}-{timestamp}.html"


class HubDeriveRequest(BaseModel):
    """Body of POST /hubs/derive — fields available when an operator
    clicks `+ Hub` in the journey results. name + coords are mandatory
    (the click is gated on them in the UI); stop_id is reserved for a
    future UIC-prefix country lookup."""

    name: str = Field(min_length=1, max_length=120)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    stop_id: str | None = Field(default=None, max_length=120)


class HubDeriveResponse(BaseModel):
    """Pre-filled form data for the AddHub modal. Country may be empty
    when the point falls outside the v1 country boundaries — the modal
    then prompts the operator to pick manually."""

    name: str
    slug: str
    short: str
    country: str
    lat: float
    lon: float
    tier: str = "main"
    region: str | None = None
    sort_order: int = 100


@router.post(
    "/hubs/derive",
    # `response_model=` dropped per Sonar python:S6781 — the function's
    # `-> HubDeriveResponse` return annotation already conveys the same
    # info, FastAPI infers the response model from it since 0.95+. Same
    # pattern as the v0.1.32.21 PATCH /{sid} endpoint above.
    responses={400: {"description": "name/lat/lon validation failed."}},
)
def derive_hub_fields(
    body: HubDeriveRequest,
    _: Annotated[CurrentUser, Depends(require_platform_admin)],
) -> HubDeriveResponse:
    """Server-side derivation of slug / short / country from a station
    name + coordinates. Powers the "Promote to hub" flow in the journey
    UI: an operator clicks `+ Hub` next to a station that just returned
    itineraries, and the AddHub modal opens pre-filled by this endpoint.

    Single source of truth — the JS modal just renders what we return,
    so the slug regex + the country-detection logic stay testable in
    Python without a JS/Python drift risk.
    """
    out = hub_derive.derive(body.name, body.lat, body.lon, body.stop_id)
    return HubDeriveResponse(**out)


@router.get(
    "/runs/{run_id}/export.html",
    response_class=HTMLResponse,
    responses={
        404: {"description": "Coverage run not found."},
    },
)
def export_run_html(
    run_id: uuid.UUID,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    _: Annotated[CurrentUser, Depends(require_platform_admin)],
) -> HTMLResponse:
    """Render a self-contained HTML report for one coverage run.

    The downloaded file embeds the matrix view AND every cell's full
    itinerary detail (legs, operators, train numbers, departure / arrival
    times). Recipient doesn't need platform access — opens in any browser
    offline, click any cell to drill into the routes for that pair.

    Designed for sharing coverage results with stakeholders outside the
    platform (e.g. via email or upload to a shared drive). All assets
    are inlined: no external CDN, no API round-trips after download.

    Works on runs in any status (pending / running / completed): partial
    data renders as far as it goes; cells without results show their
    in-flight state. Recipients viewing a 'running' export see the matrix
    as of the snapshot moment.

    Large runs (> `_EXPORT_LEG_DETAIL_MAX_PAIRS` result rows) drop the
    leg-by-leg detail from embedded trips — the full-detail file for a
    94-hub run is ~150MB, which downloads fine but no browser will open.
    Trip summaries (times, duration, transfers, modes) stay, and the
    modal points at the online share link for full depth.

    Implementation: data-shaping lives in `_build_export_context` so it's
    unit-testable without DB. This function is thin orchestration:
    query → marshal → render → set download header.
    """
    run, results = runner.get_run_with_results(db, run_id)
    if run is None:
        raise HTTPException(404, _RUN_NOT_FOUND)
    search_ids = [r.journey_search_id for r in results if r.journey_search_id is not None]
    include_legs = len(results) <= _EXPORT_LEG_DETAIL_MAX_PAIRS
    cfg = runner._load_coverage_config(db)
    context = _build_export_context(
        run=run,
        results=results,
        hubs=_resolve_hubs(db),
        trips_by_search=_fetch_trips_by_search(
            db, search_ids, _comparable_depart_at(db, run), include_legs=include_legs
        ),
        legs_omitted=not include_legs,
        # ÖBB side-by-side rides the same small-run tier as leg detail.
        include_external_itineraries=include_legs,
        cfg=cfg,
    )
    response = templates.TemplateResponse(request, "admin/network_coverage_export.html", context)
    response.headers["Content-Disposition"] = f'attachment; filename="{_export_filename(run)}"'
    return response


@router.post(
    "/runs/{run_id}/stop",
    responses={
        404: {"description": _RUN_NOT_FOUND},
        409: {
            "description": (
                "Run is not in 'running' or 'pending' state — stop is only "
                "meaningful for in-flight or queued runs. Terminal-state runs "
                "(completed / failed / cancelled) return 409 unchanged."
            )
        },
    },
)
def stop_run(
    run_id: uuid.UUID,
    db: Annotated[DbSession, Depends(get_db)],
    _: Annotated[CurrentUser, Depends(require_platform_admin)],
) -> RunSummary:
    """PR-1 — operator-driven cancel for an in-flight coverage run.

    #392 — the run executes in the worker, so the signal goes through the
    database: `cancel_requested_at` is stamped, the runner's per-pair DB
    check reads it (within ~3 s) and stops the run exactly as the old
    in-process signal did: the cells already processed stay, the row flips
    to status='cancelled' with a `cancelled_by_operator` marker on
    `summary`. A run still queued (`pending`, no executor holds it) is
    cancelled here at once, with the same marker, and so is a running run
    no live executor holds (paused by a deploy, or its worker is down):
    it ends `cancelled` now instead of showing "stopping" until a worker
    is back (`executor.cancel_unheld_run`).

    Returns the updated run summary (a running run reads 'running' with
    display status 'stopping' until the executor confirms). The UI polls
    /runs/{id} every 5s so the 'cancelled' flip surfaces within a tick.

    Status codes:
      200 — stop recorded (or the queued run cancelled)
      404 — run id unknown
      409 — run is neither running nor queued
    """
    run = db.get(NetworkCoverageRun, run_id)
    if run is None:
        raise HTTPException(404, _RUN_NOT_FOUND)
    if run.status == "pending" and _cancel_queued_run(db, run_id):
        db.refresh(run)
        return _run_to_summary(run, cfg=runner._load_coverage_config(db))
    db.refresh(run)
    if run.status != "running":
        raise HTTPException(
            409,
            f"Run is in state {run.status!r} — stop is only valid for running or queued runs",
        )
    # A run an executor holds is cancelled by that executor, NOT here, so the
    # row's terminal-state guarantee holds (status='running' until the
    # executor confirms it has stopped). Only a run no live executor holds
    # (paused at a deploy, or its holder's heartbeat is stale) is cancelled
    # here, by `cancel_unheld_run` under the row lock (#392).
    db.execute(
        update(NetworkCoverageRun)
        .where(
            NetworkCoverageRun.id == run_id,
            NetworkCoverageRun.status == "running",
            NetworkCoverageRun.cancel_requested_at.is_(None),
        )
        .values(cancel_requested_at=func.now())
    )
    db.commit()
    coverage_executor.cancel_unheld_run(db, run_id)
    db.refresh(run)
    return _run_to_summary(run, cfg=runner._load_coverage_config(db))


def _cancel_queued_run(db: DbSession, run_id: uuid.UUID) -> bool:
    """#392 — cancel a run no executor has claimed yet, in one conditional
    UPDATE (False when an executor claimed it meanwhile: it is running now
    and is stopped through `cancel_requested_at`)."""
    now = datetime.now(UTC)
    cancelled = db.execute(
        update(NetworkCoverageRun)
        .where(NetworkCoverageRun.id == run_id, NetworkCoverageRun.status == "pending")
        .values(
            status="cancelled",
            finished_at=now,
            cancel_requested_at=now,
            summary={
                "elapsed_seconds": 0.0,
                "cancelled_by_operator": True,
                "cancelled_at": now.isoformat(),
                "cancelled_while_queued": True,
            },
        )
        .returning(NetworkCoverageRun.id)
    ).scalar_one_or_none()
    db.commit()
    return cancelled is not None


@router.get(
    "/runs/{run_id}",
    responses={404: {"description": _RUN_NOT_FOUND}},
)
def get_run(
    run_id: uuid.UUID,
    db: Annotated[DbSession, Depends(get_db)],
    _: Annotated[CurrentUser, Depends(require_platform_admin)],
) -> RunDetail:
    """Fetch one run + every result row. Polled by the UI every 5s while
    the run is in 'running' status; static once 'completed'."""
    run, results = runner.get_run_with_results(db, run_id)
    if run is None:
        raise HTTPException(404, _RUN_NOT_FOUND)
    # PR-190 — compute response_ms stats from the in-memory results we
    # already loaded; a second DB round-trip would be wasteful here.
    summary = _run_to_summary(
        run,
        response_ms_stats=_stats_from_results(results),
        cfg=runner._load_coverage_config(db),
    )
    return RunDetail(
        **summary.model_dump(),
        summary=run.summary,
        results=[
            ResultEntry(
                origin_hub_id=r.origin_hub_id,
                dest_hub_id=r.dest_hub_id,
                status=r.status,
                response_ms=r.response_ms,
                num_itineraries=r.num_itineraries,
                best_duration_seconds=r.best_duration_seconds,
                best_num_transfers=r.best_num_transfers,
                best_operators=r.best_operators,
                error_message=r.error_message,
                journey_search_id=str(r.journey_search_id) if r.journey_search_id else None,
                # PR #36 — only fanout-mode rows populate this; getattr
                # so test fixtures without the column survive
                session_ids=getattr(r, "session_ids", None),
                # PR-E — persisted external-verify verdict; NULL on
                # legacy/un-verified rows. getattr fallback for pre-
                # migration fixtures.
                external_ok=getattr(r, "external_ok", None),
                external_num_connections=getattr(r, "external_num_connections", None),
                external_best_duration_seconds=getattr(r, "external_best_duration_seconds", None),
                external_best_transfers=getattr(r, "external_best_transfers", None),
                external_source=getattr(r, "external_source", None),
                external_error=getattr(r, "external_error", None),
                external_verified_at=getattr(r, "external_verified_at", None),
                # PR-196a — alignment tier / score for the matrix
                # heatmap. NULL on rows that pre-date the sweep; the
                # JS render maps NULL → 'no_data' tier.
                external_alignment_score=getattr(r, "external_alignment_score", None),
                external_alignment_tier=getattr(r, "external_alignment_tier", None),
            )
            for r in results
        ],
    )


@router.delete(
    "/runs/{run_id}",
    status_code=204,
    responses={
        404: {"description": _RUN_NOT_FOUND},
        409: {
            "description": (
                "Run is in 'running' state — stop it first. Deleting an "
                "in-flight run would race the background worker's writes."
            )
        },
    },
)
def delete_run(
    run_id: uuid.UUID,
    db: Annotated[DbSession, Depends(get_db)],
    _: Annotated[CurrentUser, Depends(require_platform_admin)],
) -> None:
    """Hard-delete a coverage run and its result rows.

    Unlike `delete_hub` (soft-delete — a hub is referenced by many
    historical runs' `hub_id` strings), a run isn't referenced by
    anything else: `NetworkCoverageResult.run_id` cascades at the DB
    level (`ondelete="CASCADE"`), so this is a single clean DELETE.

    Blocked for 'running' runs (409) — stop it via the Stop button/
    endpoint first so the background worker isn't racing a delete of
    the row it's about to write terminal-state fields onto.
    """
    # #392 — the row is locked while its status is checked, so a queued run
    # the worker's executor claims at this moment (it skips locked rows) is
    # either deleted before the claim or seen running here, never both.
    run = db.get(NetworkCoverageRun, run_id, with_for_update=True)
    if run is None:
        raise HTTPException(404, _RUN_NOT_FOUND)
    if run.status == "running":
        raise HTTPException(409, "Run is 'running' — stop it before deleting")
    db.delete(run)
    db.commit()


@router.get(
    "/runs/{run_id}/cells/{origin_id}/{dest_id}/trips",
    responses={404: {"description": _RUN_NOT_FOUND}},
)
def get_cell_trips(
    run_id: uuid.UUID,
    origin_id: str,
    dest_id: str,
    db: Annotated[DbSession, Depends(get_db)],
    _: Annotated[CurrentUser, Depends(require_platform_admin)],
) -> CellTripsResponse:
    """Return the trip breakdown for one matrix cell, split into outbound
    (A→B) and return (B→A).

    The matrix UI's click-cell modal calls this on open. The same query
    chain powers the HTML export — we just split it per direction here
    and surface BOTH directions in one response so the modal can render
    them as two collapsible sections without a second round-trip.

    For direction='single' runs, B→A wasn't queried; we still set
    response.direction='single' so the JS can hide the return section
    entirely (vs rendering "not found" which would look like a data
    quality issue).
    """
    run = db.get(NetworkCoverageRun, run_id)
    if run is None:
        raise HTTPException(404, _RUN_NOT_FOUND)
    return _build_cell_trips_response(db, run, origin_id, dest_id)


def _build_cell_trips_response(
    db: DbSession, run: NetworkCoverageRun, origin_id: str, dest_id: str
) -> CellTripsResponse:
    """Query + marshal one cell's trip breakdown (both directions).

    Extracted from `get_cell_trips` so the public share router
    (app/api/coverage_share.py) can serve the identical payload for the
    share page's lazy cell modal without inheriting this router's
    platform_admin dependency — same reuse pattern as
    `_build_export_context`.
    """
    # Fetch both result rows in one round-trip. The UNIQUE constraint
    # `(run_id, origin, dest)` means we get at most 2 rows here.
    rows = (
        db.execute(
            select(NetworkCoverageResult)
            .where(NetworkCoverageResult.run_id == run.id)
            .where(
                (
                    (NetworkCoverageResult.origin_hub_id == origin_id)
                    & (NetworkCoverageResult.dest_hub_id == dest_id)
                )
                | (
                    (NetworkCoverageResult.origin_hub_id == dest_id)
                    & (NetworkCoverageResult.dest_hub_id == origin_id)
                )
            )
        )
        .scalars()
        .all()
    )
    by_pair: dict[tuple[str, str], NetworkCoverageResult] = {
        (r.origin_hub_id, r.dest_hub_id): r for r in rows
    }
    outbound_row = by_pair.get((origin_id, dest_id))
    return_row = by_pair.get((dest_id, origin_id))

    # Materialise trips for both rows in one query — cheap when both
    # journey_search_ids are present, no-op when both are NULL.
    search_ids: list[uuid.UUID] = [
        r.journey_search_id
        for r in (outbound_row, return_row)
        if r is not None and r.journey_search_id is not None
    ]
    trips_by_search = _fetch_trips_by_search(db, search_ids, _comparable_depart_at(db, run))

    def _row_to_direction(r: NetworkCoverageResult | None) -> CellTripsDirection | None:
        if r is None:
            return None
        return CellTripsDirection(
            origin_hub_id=r.origin_hub_id,
            dest_hub_id=r.dest_hub_id,
            status=r.status,
            response_ms=r.response_ms,
            num_itineraries=r.num_itineraries,
            best_duration_seconds=r.best_duration_seconds,
            best_num_transfers=r.best_num_transfers,
            best_operators=r.best_operators,
            error_message=r.error_message,
            trips=trips_by_search.get(str(r.journey_search_id), []) if r.journey_search_id else [],
            # PR-E — pre-rendered external-verify verdict so the modal
            # opens with ÖBB's answer already populated.
            external_ok=r.external_ok,
            external_num_connections=r.external_num_connections,
            external_best_duration_seconds=r.external_best_duration_seconds,
            external_best_transfers=r.external_best_transfers,
            external_source=r.external_source,
            external_error=r.external_error,
            external_verified_at=r.external_verified_at,
            # PR-196a — persisted ÖBB itineraries + alignment so the
            # modal can render the two planners' answers side by side.
            # Direct attribute access matches the PR-E reads above;
            # the migration ships in the same release as the writer so
            # the columns are always present on rows the modal queries.
            external_itineraries=r.external_itineraries,
            external_alignment_score=r.external_alignment_score,
            external_alignment_tier=r.external_alignment_tier,
        )

    # direction='single' runs intentionally have no return row — collapse
    # to None so the JS can hide the section cleanly.
    return_direction = _row_to_direction(return_row) if run.direction == "both" else None

    return CellTripsResponse(
        direction=run.direction,
        outbound=_row_to_direction(outbound_row),
        return_=return_direction,
    )


@router.get(
    "/runs/{run_id}/cells/{origin_id}/{dest_id}/verify-external",
    responses={
        404: {"description": f"{_RUN_NOT_FOUND} / {_CELL_NOT_FOUND} / {_HUB_NOT_FOUND}"},
    },
)
async def verify_cell_external(
    run_id: uuid.UUID,
    origin_id: str,
    dest_id: str,
    db: Annotated[DbSession, Depends(get_db)],
    _: Annotated[CurrentUser, Depends(require_platform_admin)],
) -> external_verify.VerifyResult:
    """Ask an external journey planner (ÖBB's HAFAS backend) whether
    this specific pair has a route at the run's depart_at, so the
    operator can disambiguate "VIATOR's data is missing a service" from
    "there is genuinely no service on this date".

    Designed for click-to-verify on `no_route` cells in the matrix
    modal. Operator-driven (rate cap is implicit in click cadence), no
    persistence — purely a live check that runs once per click.

    Implementation: looks up the run for depart_at, the cell row to
    confirm it exists (and bind the pair to a real coverage cell, not
    a typed-in URL), the two hub rows for coordinates, then calls
    `external_verify.verify_via_oebb_hafas` and returns its result
    verbatim.
    """
    origin_hub, dest_hub, run = await db_step(_verify_inputs, db, run_id, origin_id, dest_id)
    return await external_verify.verify_via_oebb_hafas(
        from_lat=origin_hub.lat,
        from_lon=origin_hub.lon,
        to_lat=dest_hub.lat,
        to_lon=dest_hub.lon,
        depart_at=run.depart_at,
    )


def _verify_inputs(
    db: DbSession, run_id: uuid.UUID, origin_id: str, dest_id: str
) -> tuple[NetworkCoverageHub, NetworkCoverageHub, NetworkCoverageRun]:
    """`verify_cell_external`'s reads, in the thread pool (#397): the two hubs
    and the run, or the 404 of the first one missing."""
    run = db.get(NetworkCoverageRun, run_id)
    if run is None:
        raise HTTPException(404, _RUN_NOT_FOUND)
    # Confirm the cell exists in this run — prevents the endpoint from
    # being used as a "verify any pair" oracle bypass of the matrix.
    cell = (
        db.execute(
            select(NetworkCoverageResult)
            .where(NetworkCoverageResult.run_id == run_id)
            .where(NetworkCoverageResult.origin_hub_id == origin_id)
            .where(NetworkCoverageResult.dest_hub_id == dest_id)
        )
        .scalars()
        .first()
    )
    if cell is None:
        raise HTTPException(404, _CELL_NOT_FOUND)
    origin_hub = db.get(NetworkCoverageHub, origin_id)
    dest_hub = db.get(NetworkCoverageHub, dest_id)
    if origin_hub is None or dest_hub is None:
        # Hub may have been soft-deleted since the run; the cell row
        # carries the slug as a denormalised string so we'd lose the
        # coords. Surface as 404 — operator restores the hub if they
        # want this verifiable.
        raise HTTPException(404, _HUB_NOT_FOUND)
    return origin_hub, dest_hub, run


# ─────────────────────────── helpers ───────────────────────────


def _hhmm_to_time(value: str | None) -> dtime | None:
    """PR-3 — translate the API's 'HH:MM' / '24:00' string into the DB's
    `datetime.time` column type. `None` passes through (= use the
    platform_config default at execute time). '24:00' stores as
    `00:00` — the runner translates it back to end-of-day in
    `_resolve_run_window` by detecting `end <= start` after parsing.
    The pydantic validator already gated the format upstream so this
    is a parse-only conversion."""
    if value is None or value == "":
        return None
    if value == "24:00":
        return dtime(hour=0, minute=0)
    hh, mm = value.split(":", 1)
    return dtime(hour=int(hh), minute=int(mm))


def _time_to_hhmm(value: dtime | None) -> str | None:
    """Inverse of `_hhmm_to_time` for the GET surface. Stored 00:00 with
    NULL window_start_local could be either "midnight" or "end-of-day"
    sentinel; we DON'T try to disambiguate here because the persisted
    value is already the disambiguated form (start vs end columns are
    separate). The runner handles cross-midnight semantics.

    Defensive type check (`isinstance(dtime)`) so a MagicMock-populated
    fixture in the test suite (which puts a MagicMock on every
    attribute) doesn't crash _run_to_summary when the test happens to
    not care about the window."""
    if not isinstance(value, dtime):
        return None
    return f"{value.hour:02d}:{value.minute:02d}"


def _set_typed_code(hub: NetworkCoverageHub, code: str | None) -> None:
    """A station code typed in the hub form (PATCH): null clears the code,
    its origin and the hub's VIATOR id; a code that differs from the stored
    one is stored as 'manual' and clears the VIATOR id (it named the station
    of the code before); the stored code sent back unchanged keeps both."""
    if code is None:
        hub.uic = None
        hub.uic_origin = None
        hub.station_id = None
    elif code != hub.uic:
        hub.uic = code
        hub.uic_origin = "manual"
        hub.station_id = None


def _hub_to_info(hub: NetworkCoverageHub) -> HubInfo:
    """Shared shape converter for the v0.1.31 hub endpoints."""
    return HubInfo(
        id=hub.id,
        name=hub.name,
        short=hub.short,
        region=hub.region,
        country=hub.country,
        tier=hub.tier,
        modes=hub.modes,
        lat=hub.lat,
        lon=hub.lon,
        is_active=hub.is_active,
        sort_order=hub.sort_order,
        uic=hub.uic,
        uic_origin=hub.uic_origin,
        station_id=hub.station_id,
    )


def _validate_run_create_mode(body: RunCreate, db: DbSession) -> None:
    """Validate the (mode, session_id) preconditions before a coverage run
    is created. Raises HTTPException with the appropriate status code on
    failure; returns None on success.

    Extracted from `create_run` to keep that endpoint's cognitive
    complexity below SonarCloud's threshold of 15 — the nested mode →
    session_id → state checks were the main contributor.
    """
    if body.mode == runner.MODE_SINGLE_SESSION:
        _validate_single_session_mode(body, db)
    elif body.mode == runner.MODE_FANOUT:
        _validate_fanout_mode(body, db)
    else:  # pragma: no cover — pydantic pattern already gates the values
        raise HTTPException(400, f"unknown mode {body.mode!r}")


def _validate_single_session_mode(body: RunCreate, db: DbSession) -> None:
    """`mode='single_session'` requires an existing serving session.

    400 when session_id is missing or the session isn't in SERVING state;
    404 when the session id doesn't exist."""
    if not body.session_id:
        raise HTTPException(400, "mode='single_session' requires a session_id")
    s = db.get(SessionRow, body.session_id)
    if s is None:
        raise HTTPException(404, f"Session {body.session_id!r} not found")
    if s.state != SessionState.SERVING.value:
        raise HTTPException(
            400,
            f"Session {body.session_id!r} is in state {s.state!r} — must be 'serving' "
            "for coverage runs (the OTP container has to be live to receive queries)",
        )


def _validate_fanout_mode(body: RunCreate, db: DbSession) -> None:
    """`mode='fanout'` rejects an explicit session_id and requires at
    least one eligible (serving + include_in_fanout) session to exist.

    400 when session_id was supplied; 409 when no eligible session
    exists at create time. The runner re-checks at execute time in case
    a session drops between create and run."""
    if body.session_id:
        raise HTTPException(
            400,
            "mode='fanout' must not specify a session_id — every fanout-enabled "
            "session is queried at execute time",
        )
    eligible = (
        db.execute(
            select(SessionRow)
            .where(SessionRow.state == SessionState.SERVING.value)
            .where(SessionRow.include_in_fanout.is_(True))
            .limit(1)
        )
        .scalars()
        .first()
    )
    if eligible is None:
        raise HTTPException(
            409,
            "mode='fanout' requires at least one serving + include_in_fanout session; none found",
        )


def _stats_from_results(
    results: list[Any],
) -> tuple[int | None, float | None, int | None] | None:
    """PR-190 — compute (min, avg, max) of `response_ms` over an in-memory
    list of NetworkCoverageResult rows. Used by the per-run detail
    endpoint which already loaded the full result set; avoids a second
    aggregation round-trip.

    Returns None when no row has a non-NULL `response_ms` so the
    template's `{% if duration_seconds %}` style guards collapse cleanly.
    """
    timings = [r.response_ms for r in results if getattr(r, "response_ms", None) is not None]
    if not timings:
        return None
    return (min(timings), sum(timings) / len(timings), max(timings))


def _compute_duration_seconds(
    started_at: datetime | None,
    finished_at: datetime | None,
) -> float | None:
    """PR-190 — wall-clock duration of a coverage run, in seconds.

    - In-flight runs (`finished_at is None`): "now - started_at"
    - Terminal runs: "finished_at - started_at"
    - Pre-start rows (`started_at is None`): None — nothing to time yet.

    Clamped >=0 so two writers with clock skew can never surface a
    negative number in the banner.

    Uses UTC `now` because every timestamp in the row is stored
    `DateTime(timezone=True)`. We normalise naive timestamps to UTC
    defensively in case a MagicMock-shaped test fixture leaves the
    tzinfo unset — datetime arithmetic on a naive + aware pair raises
    TypeError, which would surface as a 500 from the banner.
    """
    if started_at is None:
        return None
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    end = finished_at if finished_at is not None else datetime.now(UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    delta = (end - started_at).total_seconds()
    return max(delta, 0.0)


def _aggregate_response_ms(
    db: DbSession, run_ids: list[uuid.UUID]
) -> dict[uuid.UUID, tuple[int | None, float | None, int | None]]:
    """PR-190 — single batched MIN / AVG / MAX query over
    NetworkCoverageResult.response_ms grouped by run_id.

    Used by the sidebar list (many runs at once) so a 20-run sidebar
    doesn't trigger 20 round-trips. Returns a dict keyed by run_id with
    `(min_ms, avg_ms, max_ms)` tuples; missing keys mean "no cells with
    response_ms set in that run" — the caller should treat those as the
    NULL/NULL/NULL banner case.

    Empty `run_ids` short-circuits to an empty dict so the caller doesn't
    have to special-case the no-runs sidebar.
    """
    if not run_ids:
        return {}
    rows = db.execute(
        select(
            NetworkCoverageResult.run_id,
            func.min(NetworkCoverageResult.response_ms),
            func.avg(NetworkCoverageResult.response_ms),
            func.max(NetworkCoverageResult.response_ms),
        )
        .where(NetworkCoverageResult.run_id.in_(run_ids))
        .where(NetworkCoverageResult.response_ms.is_not(None))
        .group_by(NetworkCoverageResult.run_id)
    ).all()
    out: dict[uuid.UUID, tuple[int | None, float | None, int | None]] = {}
    for run_id, min_ms, avg_ms, max_ms in rows:
        out[run_id] = (
            int(min_ms) if min_ms is not None else None,
            float(avg_ms) if avg_ms is not None else None,
            int(max_ms) if max_ms is not None else None,
        )
    return out


def _int_or_zero(value: Any) -> int:
    return value if isinstance(value, int) else 0


def _failure_reason(run: Any) -> str | None:
    """#392 — why a failed run failed, from its summary; None otherwise."""
    summary = getattr(run, "summary", None)
    if getattr(run, "status", None) != "failed" or not isinstance(summary, dict):
        return None
    reason = summary.get("failure_reason")
    if isinstance(reason, str) and reason:
        return reason
    if summary.get("orphaned_by_restart"):
        return "web restarted during the run (before runs moved to the worker, #392)"
    return None


def _display_status(run: Any) -> tuple[str, float | None]:
    """#392 — (pill text, heartbeat age in seconds) for a run row. A running
    run that no executor holds (released at a deploy), or whose heartbeat is
    missing or older than the executor's stale limit, is "resuming": its
    executor stopped and the next one takes it over.

    A held running run whose ÖBB check has started is "verifying": every
    pair is done and the check runs to its end. A Stop clicked then does
    not interrupt it (the completion phase completes, #392), so the pill
    does not say "stopping" during the check."""
    status = str(run.status)
    heartbeat_at = getattr(run, "heartbeat_at", None)
    age = (
        max(0.0, (datetime.now(UTC) - heartbeat_at).total_seconds())
        if isinstance(heartbeat_at, datetime)
        else None
    )
    if status == "pending":
        return "queued", age
    if status != "running":
        return status, age
    verifying = isinstance(getattr(run, "verify_started_at", None), datetime)
    if not verifying and isinstance(getattr(run, "cancel_requested_at", None), datetime):
        return "stopping", age
    unheld = getattr(run, "claimed_by", None) is None
    if unheld or age is None or age > coverage_executor.STALE_AFTER_SECONDS:
        return "resuming", age
    if verifying:
        return "verifying", age
    return status, age


# The time-left estimate needs this many answers of the current attempt and
# this long a measure: the first cells of a check are no guide to its pace.
_VERIFY_ETA_MIN_CELLS = 20
_VERIFY_ETA_MIN_SECONDS = 60.0


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _verify_elapsed_seconds(run: Any) -> float | None:
    """The ÖBB check's current attempt, from its start to its last write."""
    started = getattr(run, "verify_started_at", None)
    progress = getattr(run, "verify_progress_at", None)
    if not isinstance(started, datetime) or not isinstance(progress, datetime):
        return None
    return max(0.0, (progress - started).total_seconds())


def _verify_seconds_left(run: Any) -> float | None:
    """Time left in the ÖBB check at the pace of its current attempt: the
    answers saved since the attempt started, over the time between its start
    and its last write (both the database's clock, so no clock skew). 0 once
    every cell has its answer; None while there is too little to measure."""
    total = _int_or_none(getattr(run, "verify_total", None))
    done = _int_or_none(getattr(run, "verify_done", None))
    if total is None or done is None:
        return None
    if done >= total:
        return 0.0
    at_start = _int_or_none(getattr(run, "verify_done_at_start", None)) or 0
    elapsed = _verify_elapsed_seconds(run)
    checked = done - at_start
    if elapsed is None or checked < _VERIFY_ETA_MIN_CELLS or elapsed < _VERIFY_ETA_MIN_SECONDS:
        return None
    return (total - done) * elapsed / checked


def _run_to_summary(
    run: Any,
    *,
    response_ms_stats: tuple[int | None, float | None, int | None] | None = None,
    cfg: runner.CoverageConfig | None = None,
) -> RunSummary:
    """`cfg` is required to answer two questions about the run's timezone:
    whether its `reference_date` agrees with `depart_at`, and what
    wall-clock `depart_at` should display as. Every caller has a `db` and
    so can supply it; the `None` default exists only for the test fixtures
    that build a summary from a bare MagicMock row."""
    depart_at_outside_window = False
    depart_at_iso = _safe_depart_at_iso(run)
    if cfg is not None:
        depart_at_outside_window = _depart_at_outside_window(run, cfg)
        depart_at_iso = _depart_at_local_iso(run, cfg)
    duration_seconds = _compute_duration_seconds(
        getattr(run, "started_at", None),
        getattr(run, "finished_at", None),
    )
    if response_ms_stats is None:
        rmin: int | None = None
        ravg: float | None = None
        rmax: int | None = None
    else:
        rmin, ravg, rmax = response_ms_stats
    display_status, heartbeat_age = _display_status(run)
    return RunSummary(
        display_status=display_status,
        resume_count=_int_or_zero(getattr(run, "resume_count", 0)),
        heartbeat_age_seconds=heartbeat_age,
        failure_reason=_failure_reason(run),
        id=str(run.id),
        session_id=run.session_id,
        session_label=run.session_label,
        # Localised to the run's own timezone — see `_depart_at_local_iso`.
        depart_at=depart_at_iso,
        started_at=run.started_at.isoformat() if run.started_at else "",
        finished_at=run.finished_at.isoformat() if run.finished_at else None,
        status=run.status,
        direction=run.direction,
        # PR #36 — pre-migration rows (which lack the column at SELECT
        # time only in test fixtures that bypass alembic) get the
        # legacy default. In production every row has it populated by
        # the server_default on insert.
        mode=getattr(run, "mode", "single_session") or "single_session",
        total_pairs=run.total_pairs or 0,
        completed_pairs=run.completed_pairs or 0,
        ok_pairs=run.ok_pairs or 0,
        no_route_pairs=run.no_route_pairs or 0,
        error_pairs=run.error_pairs or 0,
        # Country-filter snapshot — None on pre-this-migration rows and
        # on full-matrix runs. The sidebar uses presence/absence to
        # render the "FR+CH" badge.
        countries=getattr(run, "countries", None),
        # PR-E — opt-in flag for auto external-verify on completion.
        # getattr fallback handles fixtures / pre-migration test rows
        # that lack the column. In production every row carries it
        # via the server_default='false'.
        verify_externally=bool(getattr(run, "verify_externally", False)),
        # PR-3 — per-run day-window for UI display. Each field is NULL
        # on legacy rows (and on any new run that didn't override the
        # platform default); the UI renders a "[default]" badge in
        # that case. getattr fallbacks keep the test fixtures working.
        window_start_local=_time_to_hhmm(getattr(run, "window_start_local", None)),
        window_end_local=_time_to_hhmm(getattr(run, "window_end_local", None)),
        window_timezone=_safe_str(getattr(run, "window_timezone", None)),
        reference_date=_safe_iso_date(getattr(run, "reference_date", None)),
        depart_at_outside_window=depart_at_outside_window,
        # PR-190 — banner stats. duration is always computable when
        # started_at is set; per-cell response stats need to be passed
        # in by the caller (a single batched query handles the sidebar
        # list; the per-run endpoint runs its own query).
        duration_seconds=duration_seconds,
        response_ms_min=rmin,
        response_ms_avg=ravg,
        response_ms_max=rmax,
        # The ÖBB check's progress; getattr fallbacks for the MagicMock rows of
        # the unit fixtures, like the fields above.
        verify_total=_int_or_none(getattr(run, "verify_total", None)),
        verify_done=_int_or_none(getattr(run, "verify_done", None)),
        verify_started_at=_safe_iso_datetime(getattr(run, "verify_started_at", None)),
        verify_progress_at=_safe_iso_datetime(getattr(run, "verify_progress_at", None)),
        verify_elapsed_seconds=_verify_elapsed_seconds(run),
        verify_seconds_left=_verify_seconds_left(run),
    )


def _safe_str(value: Any) -> str | None:
    """Type-narrow to `str | None` — drops MagicMocks the test fixtures
    splatter onto every attribute. Same defensive pattern as
    `_time_to_hhmm`."""
    return value if isinstance(value, str) else None


def _safe_iso_date(value: Any) -> str | None:
    """`date.isoformat()` for an actual `date` (or `datetime`); None for
    everything else — protects _run_to_summary from MagicMock-shaped
    test fixtures."""
    from datetime import date as _date_cls

    if isinstance(value, _date_cls):
        return value.isoformat()
    return None


def _safe_iso_datetime(value: Any) -> str | None:
    """`datetime.isoformat()` for an actual `datetime`; None for everything
    else (MagicMock-shaped test fixtures, a NULL column)."""
    return value.isoformat() if isinstance(value, datetime) else None
