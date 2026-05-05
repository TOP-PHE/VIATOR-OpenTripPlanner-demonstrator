"""Network-coverage admin API (v0.1.27).

Endpoints:

  GET  /api/admin/network-coverage/hubs        — return the curated 23-hub list
  GET  /api/admin/network-coverage/runs        — list past runs (newest first)
  POST /api/admin/network-coverage/runs        — start a new coverage run
  GET  /api/admin/network-coverage/runs/{id}   — fetch a run + its results

Authorization: platform_admin (the matrix consumes serious OTP capacity
when running, and old runs persist forever — content-manager doesn't
need this surface).

Background execution: POST /runs creates the row in pending state and
schedules `runner.execute_run` via FastAPI's BackgroundTasks. The UI
polls GET /runs/{id} every 5s to render progress; status flips to
"completed" when all 506 (or 253) pairs have processed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session as DbSession

from ...db import get_db
from ...models import Session as SessionRow
from ...models.sessions import SessionState
from ...network_coverage import runner
from ...network_coverage.hubs import HUBS
from ...security import CurrentUser, require_platform_admin

router = APIRouter(
    prefix="/api/admin/network-coverage",
    tags=["admin", "network-coverage"],
)


# ─────────────────────────── pydantic shapes ────────────────────────────


class HubInfo(BaseModel):
    id: str
    name: str
    short: str
    region: str
    lat: float
    lon: float


class RunCreate(BaseModel):
    session_id: str = Field(min_length=1, description="Target session — must be serving")
    depart_at: datetime = Field(description="Departure datetime (timezone-aware preferred)")
    direction: str = Field(default="both", description="'both' | 'single'")


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


class RunDetail(RunSummary):
    """Full detail-view shape — used for the matrix render."""

    summary: dict[str, Any] | None = None
    results: list[ResultEntry] = []


# ─────────────────────────── endpoints ───────────────────────────


@router.get("/hubs", response_model=list[HubInfo])
def list_hubs(
    _: Annotated[CurrentUser, Depends(require_platform_admin)],
) -> list[HubInfo]:
    """The curated 23-hub list. Stable identifier set; UI uses these as
    the matrix row + column headers."""
    return [
        HubInfo(id=h.id, name=h.name, short=h.short, region=h.region, lat=h.lat, lon=h.lon)
        for h in HUBS
    ]


@router.get("/runs", response_model=list[RunSummary])
def list_runs(
    db: Annotated[DbSession, Depends(get_db)],
    _: Annotated[CurrentUser, Depends(require_platform_admin)],
    limit: int = 20,
) -> list[RunSummary]:
    """Recent coverage runs, newest first. The admin page sidebar."""
    rows = runner.list_recent_runs(db, limit=limit)
    return [_run_to_summary(r) for r in rows]


@router.post("/runs", response_model=RunSummary, status_code=201)
def create_run(
    body: RunCreate,
    bg: BackgroundTasks,
    db: Annotated[DbSession, Depends(get_db)],
    actor: Annotated[CurrentUser, Depends(require_platform_admin)],
) -> RunSummary:
    """Start a new coverage run.

    Validates the session is currently serving (no point routing against
    a graph that isn't loaded), creates the run row in pending state,
    schedules `execute_run` as a background task, and returns the
    summary so the UI can start polling immediately.
    """
    if body.direction not in ("both", "single"):
        raise HTTPException(400, "direction must be 'both' or 'single'")
    s = db.get(SessionRow, body.session_id)
    if s is None:
        raise HTTPException(404, f"Session {body.session_id!r} not found")
    if s.state != SessionState.SERVING.value:
        raise HTTPException(
            400,
            f"Session {body.session_id!r} is in state {s.state!r} — must be 'serving' "
            "for coverage runs (the OTP container has to be live to receive queries)",
        )

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
            )
            for r in results
        ],
    )


# ─────────────────────────── helpers ───────────────────────────


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
        total_pairs=run.total_pairs or 0,
        completed_pairs=run.completed_pairs or 0,
        ok_pairs=run.ok_pairs or 0,
        no_route_pairs=run.no_route_pairs or 0,
        error_pairs=run.error_pairs or 0,
    )
