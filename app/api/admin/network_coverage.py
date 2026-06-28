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

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

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
from ...network_coverage import hub_derive, runner
from ...network_coverage.hubs import HUBS as STATIC_HUBS
from ...security import CurrentUser, require_platform_admin
from ...templating import templates

router = APIRouter(
    prefix="/api/admin/network-coverage",
    tags=["admin", "network-coverage"],
)

# Shared 404 detail for runs lookups — used by the export, the run-detail,
# and the cell-trips endpoints. Constant so a future rename / i18n only
# touches one site (Sonar S1192).
_RUN_NOT_FOUND = "Run not found"


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
    """Recent coverage runs, newest first. The admin page sidebar."""
    rows = runner.list_recent_runs(db, limit=limit)
    return [_run_to_summary(r) for r in rows]


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

    try:
        run = runner.create_run(
            db,
            actor_user_id=actor.id,
            session_id=body.session_id,
            depart_at=depart_at,
            direction=body.direction,
            mode=body.mode,
            countries=body.countries,
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
    summary = _run_to_summary(run)
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
        )

    # direction='single' runs intentionally have no return row — collapse
    # to None so the JS can hide the section cleanly.
    return_direction = _row_to_direction(return_row) if run.direction == "both" else None

    return CellTripsResponse(
        direction=run.direction,
        outbound=_row_to_direction(outbound_row),
        return_=return_direction,
    )


# ─────────────────────────── helpers ───────────────────────────


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


def _run_to_summary(run: Any) -> RunSummary:
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
    )
