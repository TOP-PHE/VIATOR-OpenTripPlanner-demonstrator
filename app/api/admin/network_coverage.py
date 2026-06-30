"""Network-coverage admin API (v0.1.27 / hub-set bumped to 26 in v0.1.28
/ DB-backed hubs in v0.1.31).

Endpoints:

  GET    /api/admin/network-coverage/hubs              — list active hubs
                                                         (v0.1.31: from DB
                                                         instead of hubs.py)
  POST   /api/admin/network-coverage/hubs              — v0.1.31: create hub
  PATCH  /api/admin/network-coverage/hubs/{id}         — v0.1.31: edit hub
  DELETE /api/admin/network-coverage/hubs/{id}         — v0.1.31: soft-delete

  GET    /api/admin/network-coverage/runs              — list past runs
                                                         (newest first)
  POST   /api/admin/network-coverage/runs              — start new coverage run
  GET    /api/admin/network-coverage/runs/{id}         — fetch run + results

Authorization: platform_admin (the matrix consumes serious OTP capacity
when running, and old runs persist forever — content-manager doesn't
need this surface).

Background execution: POST /runs creates the row in pending state and
schedules `runner.execute_run` via FastAPI's BackgroundTasks. The UI
polls GET /runs/{id} every 5s to render progress; status flips to
"completed" when all 650 (or 325) pairs have processed.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, date, datetime
from datetime import time as dtime
from typing import Annotated, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from ...config_schema import CONFIG_SCHEMA
from ...db import get_db
from ...models import (
    JourneySearchExecution,
    JourneyTrip,
    NetworkCoverageHub,
    NetworkCoverageResult,
    NetworkCoverageRun,
)
from ...models import Session as SessionRow
from ...models.sessions import SessionState
from ...network_coverage import external_verify, hub_derive, runner
from ...network_coverage.hubs import HUBS as STATIC_HUBS
from ...security import CurrentUser, require_platform_admin
from ...templating import templates

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

# Shared 404 detail for cell lookups within a run — used by the
# verify-external endpoint. Mirrors the _RUN_NOT_FOUND pattern.
_CELL_NOT_FOUND = "Cell (origin,dest) not found in this run"

# Shared 404 detail for hub lookups by id — both the verify-external
# endpoint and any future per-hub action surfaces use this.
_HUB_NOT_FOUND = "Hub not found"


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
    lat: float
    lon: float
    is_active: bool = True
    sort_order: int = 100


class HubCreate(BaseModel):
    """v0.1.31 — POST /hubs body. id is the slug, mandatory and immutable
    once created; any later edits use PATCH on the existing id."""

    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str = Field(min_length=1, max_length=120)
    short: str = Field(min_length=1, max_length=16)
    country: str = Field(min_length=2, max_length=2, description="ISO 3166-1 alpha-2 (uppercase)")
    region: str | None = Field(default=None, max_length=40)
    tier: str = Field(default="main", pattern=r"^(main|regional)$")
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    sort_order: int = Field(default=100, ge=0, le=10_000)


class HubUpdate(BaseModel):
    """v0.1.31 — PATCH /hubs/{id} body. Every field is optional; missing
    fields are not modified. id is immutable (use DELETE + POST to rename)."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    short: str | None = Field(default=None, min_length=1, max_length=16)
    country: str | None = Field(default=None, min_length=2, max_length=2)
    region: str | None = Field(default=None, max_length=40)
    tier: str | None = Field(default=None, pattern=r"^(main|regional)$")
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    sort_order: int | None = Field(default=None, ge=0, le=10_000)
    # is_active separately so a soft-deleted hub can be restored
    # without changing other fields.
    is_active: bool | None = None


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
            "None = tomorrow at run-create time in window_timezone."
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
        return [
            HubInfo(
                id=r.id,
                name=r.name,
                short=r.short,
                region=r.region,
                country=r.country,
                tier=r.tier,
                lat=r.lat,
                lon=r.lon,
                is_active=r.is_active,
                sort_order=r.sort_order,
            )
            for r in rows
        ]
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


@router.post("/hubs", response_model=HubInfo, status_code=201)
def create_hub(
    body: HubCreate,
    db: Annotated[DbSession, Depends(get_db)],
    _: Annotated[CurrentUser, Depends(require_platform_admin)],
) -> HubInfo:
    """v0.1.31 — create a new hub.

    Slug must be unique (PK conflict → 409). Country normalised to
    uppercase to keep ISO codes consistent regardless of operator
    typing habits."""
    hub = NetworkCoverageHub(
        id=body.id,
        name=body.name,
        short=body.short,
        country=body.country.upper(),
        region=body.region,
        tier=body.tier,
        lat=body.lat,
        lon=body.lon,
        sort_order=body.sort_order,
        is_active=True,
    )
    db.add(hub)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, f"Hub with id={body.id!r} already exists") from None
    db.refresh(hub)
    return _hub_to_info(hub)


@router.patch("/hubs/{hub_id}", response_model=HubInfo)
def update_hub(
    hub_id: str,
    body: HubUpdate,
    db: Annotated[DbSession, Depends(get_db)],
    _: Annotated[CurrentUser, Depends(require_platform_admin)],
) -> HubInfo:
    """v0.1.31 — edit an existing hub.

    Sparse update: only fields present in the request body are modified.
    The slug (id) is immutable — to rename, soft-delete and create new.
    Country is uppercased on write.
    """
    hub = db.get(NetworkCoverageHub, hub_id)
    if hub is None:
        raise HTTPException(404, f"Hub {hub_id!r} not found")
    data = body.model_dump(exclude_unset=True)
    if "country" in data and data["country"] is not None:
        data["country"] = data["country"].upper()
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
    return [_run_to_summary(r, response_ms_stats=stats_by_run.get(r.id)) for r in rows]


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
    bg: BackgroundTasks,
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

    Validates pre-flight, creates the run row in pending state, schedules
    `execute_run` as a background task, and returns the summary so the
    UI can start polling immediately.
    """
    if body.direction not in ("both", "single"):
        raise HTTPException(400, "direction must be 'both' or 'single'")

    _validate_run_create_mode(body, db)

    # Normalise depart_at to UTC if naive — OTP interprets this in
    # transitModelTimeZone; we keep our DB representation tz-aware.
    depart_at = body.depart_at
    if depart_at.tzinfo is None:
        depart_at = depart_at.replace(tzinfo=UTC)

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
    # Schedule the actual work. FastAPI BackgroundTasks runs after the
    # response is sent — the operator gets the run id immediately and
    # the UI starts polling.
    bg.add_task(runner.execute_run, run.id)
    return _run_to_summary(run)


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


def _fetch_trips_by_search(
    db: DbSession, search_ids: list[uuid.UUID]
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
    """
    if not search_ids:
        return {}
    rows = db.execute(
        select(JourneySearchExecution.search_id, JourneyTrip)
        .join(JourneyTrip, JourneyTrip.execution_id == JourneySearchExecution.id)
        .where(JourneySearchExecution.search_id.in_(search_ids))
        .order_by(JourneySearchExecution.search_id, JourneyTrip.rank_in_response)
    ).all()
    out: dict[str, list[dict[str, Any]]] = {}
    for search_id, t in rows:
        out.setdefault(str(search_id), []).append(
            {
                "rank": t.rank_in_response,
                "duration_seconds": t.duration_seconds,
                "num_transfers": t.num_transfers,
                "departure_at": t.departure_at.isoformat() if t.departure_at else None,
                "arrival_at": t.arrival_at.isoformat() if t.arrival_at else None,
                "modes": t.modes,
                "legs": t.legs,
            }
        )
    return out


def _build_export_context(
    *,
    run: Any,
    results: list[Any],
    hubs: list[HubInfo],
    trips_by_search: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Pure data-shaping from DB rows → template context dict.

    Extracted so unit tests can exercise the marshalling (run-meta keys,
    cell keying, trip attachment, status passthrough) without touching
    the DB or the FastAPI request pipeline. The endpoint itself becomes
    thin orchestration: query → marshal → render.

    Cells are keyed by `"<origin_id>:<dest_id>"` so the Jinja template can
    index by string concat — cleaner than nested loops with tuple keys.
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
        }
    run_meta = {
        "id": str(run.id),
        "session_id": run.session_id,
        "mode": getattr(run, "mode", "single_session"),
        "direction": run.direction,
        "depart_at": run.depart_at.isoformat() if run.depart_at else None,
        "status": run.status,
        "total_pairs": run.total_pairs,
        "completed_pairs": run.completed_pairs,
        "ok_pairs": run.ok_pairs,
        "no_route_pairs": run.no_route_pairs,
        "error_pairs": run.error_pairs,
        "created_at": run.started_at.isoformat() if run.started_at else None,
    }
    return {
        "run": run_meta,
        "hubs": [h.model_dump() for h in hubs],
        "cells": cells,
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

    Implementation: data-shaping lives in `_build_export_context` so it's
    unit-testable without DB. This function is thin orchestration:
    query → marshal → render → set download header.
    """
    run, results = runner.get_run_with_results(db, run_id)
    if run is None:
        raise HTTPException(404, _RUN_NOT_FOUND)
    search_ids = [r.journey_search_id for r in results if r.journey_search_id is not None]
    context = _build_export_context(
        run=run,
        results=results,
        hubs=_resolve_hubs(db),
        trips_by_search=_fetch_trips_by_search(db, search_ids),
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
                "Run is not in 'running' state — stop is only meaningful for "
                "in-flight runs. Terminal-state runs (completed / failed / "
                "cancelled) return 409 unchanged."
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

    Fires a cooperative cancel signal the runner checks between each
    pair; the worker exits the per-pair loop cleanly, persists the cells
    already processed, and flips the row to status='cancelled' with a
    `cancelled_by_operator` marker on `summary`.

    Returns the updated run summary (status reads 'running' on the
    initial response — the runner observes the signal a beat later when
    the next pair-check fires). The UI polls /runs/{id} every 5s so the
    'cancelled' flip surfaces within one polling tick.

    Status codes:
      200 — signal accepted, runner is in-flight and will stop
      404 — run id unknown
      409 — run is not in 'running' state
    """
    run = db.get(NetworkCoverageRun, run_id)
    if run is None:
        raise HTTPException(404, _RUN_NOT_FOUND)
    if run.status != "running":
        raise HTTPException(
            409,
            f"Run is in state {run.status!r} — stop is only valid for 'running' runs",
        )
    # Fire-and-forget — the runner's per-pair cooperative check picks
    # this up between the in-flight pair's persist and the next pair's
    # fetch_plan call. The DB write to status='cancelled' happens in the
    # runner, NOT here, so the row's terminal-state guarantee holds
    # (status='running' until the worker confirms it has stopped).
    runner.request_cancel(run_id)
    return _run_to_summary(run)


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
    summary = _run_to_summary(run, response_ms_stats=_stats_from_results(results))
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
            )
            for r in results
        ],
    )


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

    # Fetch both result rows in one round-trip. The UNIQUE constraint
    # `(run_id, origin, dest)` means we get at most 2 rows here.
    rows = (
        db.execute(
            select(NetworkCoverageResult)
            .where(NetworkCoverageResult.run_id == run_id)
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
    trips_by_search = _fetch_trips_by_search(db, search_ids)

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
    return await external_verify.verify_via_oebb_hafas(
        from_lat=origin_hub.lat,
        from_lon=origin_hub.lon,
        to_lat=dest_hub.lat,
        to_lon=dest_hub.lon,
        depart_at=run.depart_at,
    )


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


def _hub_to_info(hub: NetworkCoverageHub) -> HubInfo:
    """Shared shape converter for the v0.1.31 hub endpoints."""
    return HubInfo(
        id=hub.id,
        name=hub.name,
        short=hub.short,
        region=hub.region,
        country=hub.country,
        tier=hub.tier,
        lat=hub.lat,
        lon=hub.lon,
        is_active=hub.is_active,
        sort_order=hub.sort_order,
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


def _run_to_summary(
    run: Any,
    *,
    response_ms_stats: tuple[int | None, float | None, int | None] | None = None,
) -> RunSummary:
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
    return RunSummary(
        id=str(run.id),
        session_id=run.session_id,
        session_label=run.session_label,
        depart_at=run.depart_at.isoformat() if run.depart_at else "",
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
        # PR-190 — banner stats. duration is always computable when
        # started_at is set; per-cell response stats need to be passed
        # in by the caller (a single batched query handles the sidebar
        # list; the per-run endpoint runs its own query).
        duration_seconds=duration_seconds,
        response_ms_min=rmin,
        response_ms_avg=ravg,
        response_ms_max=rmax,
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
