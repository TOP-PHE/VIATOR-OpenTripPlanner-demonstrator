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

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from ...db import get_db
from ...models import NetworkCoverageHub
from ...models import Session as SessionRow
from ...models.sessions import SessionState
from ...network_coverage import runner
from ...network_coverage.hubs import HUBS as STATIC_HUBS
from ...security import CurrentUser, require_platform_admin

router = APIRouter(
    prefix="/api/admin/network-coverage",
    tags=["admin", "network-coverage"],
)


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
    response_model=RunSummary,
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

    if body.mode == runner.MODE_SINGLE_SESSION:
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
    elif body.mode == runner.MODE_FANOUT:
        if body.session_id:
            raise HTTPException(
                400,
                "mode='fanout' must not specify a session_id — every fanout-enabled "
                "session is queried at execute time",
            )
        # At least one serving + fanout-enabled session must exist; otherwise
        # the run would have nothing to query against. The runner re-checks
        # at execute time (in case a session drops between create and run)
        # but failing fast at create gives the UI a meaningful 400 instead
        # of a 'failed' status row in the sidebar.
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
                "mode='fanout' requires at least one serving + include_in_fanout session; "
                "none found",
            )
    else:  # pragma: no cover — pydantic pattern already gates the values
        raise HTTPException(400, f"unknown mode {body.mode!r}")

    # Normalise depart_at to UTC if naive — OTP interprets this in
    # transitModelTimeZone; we keep our DB representation tz-aware.
    depart_at = body.depart_at
    if depart_at.tzinfo is None:
        depart_at = depart_at.replace(tzinfo=UTC)

    run = runner.create_run(
        db,
        actor_user_id=actor.id,
        session_id=body.session_id,
        depart_at=depart_at,
        direction=body.direction,
        mode=body.mode,
    )
    db.commit()
    db.refresh(run)
    # Schedule the actual work. FastAPI BackgroundTasks runs after the
    # response is sent — the operator gets the run id immediately and
    # the UI starts polling.
    bg.add_task(runner.execute_run, run.id)
    return _run_to_summary(run)


@router.get("/runs/{run_id}", response_model=RunDetail)
def get_run(
    run_id: uuid.UUID,
    db: Annotated[DbSession, Depends(get_db)],
    _: Annotated[CurrentUser, Depends(require_platform_admin)],
) -> RunDetail:
    """Fetch one run + every result row. Polled by the UI every 5s while
    the run is in 'running' status; static once 'completed'."""
    run, results = runner.get_run_with_results(db, run_id)
    if run is None:
        raise HTTPException(404, "Run not found")
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
    )
