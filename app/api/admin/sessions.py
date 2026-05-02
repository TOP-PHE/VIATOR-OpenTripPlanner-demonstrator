"""Admin sessions CRUD + per-session uploads / source-refresh / rebuilds /
promote. See spec §9.3 and §4.

Phase-A.5 wiring (this file):

- `POST /<sid>/uploads`         multipart upload → ingestion.dispatch()
- `POST /<sid>/sources/refresh` httpx-download URLs from config.sources
- `POST /<sid>/rebuilds`        enqueue OTP build job (worker picks up)
- `GET  /<sid>/rebuilds`        list this session's rebuild jobs
- `POST /<sid>/promote`         regenerate compose+nginx fragments, signal
                                worker to compose-up + nginx-reload, set
                                state='serving'
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import httpx
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm.attributes import flag_modified

from ... import audit, detect, ingestion, sessions_orchestrator, staleness
from ...db import get_db
from ...models import MasterStation, RebuildJob, Upload
from ...models import Session as SessionRow
from ...models.sessions import SessionCategory, SessionState
from ...security import (
    CurrentUser,
    client_ip,
    require_content_manager,
    require_platform_admin,
)
from ...settings import settings

router = APIRouter(prefix="/api/sessions", tags=["admin", "sessions"])

# Sentinel file the worker watches for. When present, the worker runs
# `docker compose -p viator up -d` (picks up new otp-<sid> services from the
# regenerated fragment) and `docker exec viator-nginx-1 nginx -s reload`,
# then deletes the file. See app/worker.py.
_RELOAD_TRIGGER = Path("/data/generated/.reload-trigger")

# Mapping of `config.sources` key → detect.KNOWN_KINDS value. Lowercase keys
# are friendlier in JSON; the detect/dispatch layers expect canonical names.
_SOURCE_KEY_TO_KIND: dict[str, str] = {
    "gtfs": "GTFS",
    "osm_pbf": "OSM-PBF",
    "netex_nordic": "NeTEx-Nordic",
    "netex_epip": "NeTEx-EPIP",
    "mct": "SNCF-MCT",
    "stations": "SNCF-Stations",
}


_VALID_CATEGORIES = {c.value for c in SessionCategory}
_VALID_STATES = {s.value for s in SessionState}
_SLUG = re.compile(r"^[a-z][a-z0-9-]{1,62}$")


class SessionCreate(BaseModel):
    id: str = Field(min_length=2, max_length=63, description="slug: ^[a-z][a-z0-9-]+$")
    name: str = Field(min_length=1, max_length=200)
    category: str = Field(description="NAP | MERITS | MANUAL | EXPERIMENTAL")
    config: dict[str, Any] = Field(default_factory=dict)
    include_in_fanout: bool = False


class SessionPatch(BaseModel):
    name: str | None = None
    config: dict[str, Any] | None = None
    include_in_fanout: bool | None = None
    state: str | None = None


class SessionResponse(BaseModel):
    id: str
    name: str
    category: str
    state: str
    config: dict[str, Any]
    include_in_fanout: bool
    created_at: str
    archived_at: str | None
    # Soft staleness signal (v0.1.7.1): non-null when the operator has
    # edited URLs since the last refresh. UI displays as a yellow banner
    # and adds a confirm dialog before Rebuild graph. Never blocks rebuild
    # by itself — that's handled by the harder input-presence check.
    staleness_warning: str | None = None

    @classmethod
    def from_orm_session(cls, s: SessionRow) -> SessionResponse:
        return cls(
            id=s.id,
            name=s.name,
            category=s.category,
            state=s.state,
            config=s.config or {},
            include_in_fanout=s.include_in_fanout,
            created_at=s.created_at.isoformat() if s.created_at else "",
            archived_at=s.archived_at.isoformat() if s.archived_at else None,
            staleness_warning=staleness.staleness_warning(s.config or {}),
        )


# ────────────────────────── routes ──────────────────────────


@router.get("", response_model=list[SessionResponse], summary="List sessions")
def list_sessions(
    db: Annotated[DbSession, Depends(get_db)],
    _: Annotated[CurrentUser, Depends(require_platform_admin)],
) -> list[SessionResponse]:
    rows = db.execute(select(SessionRow).order_by(SessionRow.created_at)).scalars().all()
    return [SessionResponse.from_orm_session(s) for s in rows]


@router.post("", response_model=SessionResponse, status_code=201, summary="Create a session")
def create_session(
    body: SessionCreate,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    actor: Annotated[CurrentUser, Depends(require_platform_admin)],
) -> SessionResponse:
    if not _SLUG.match(body.id):
        raise HTTPException(400, "Session id must be a slug: ^[a-z][a-z0-9-]+$")
    if body.category not in _VALID_CATEGORIES:
        raise HTTPException(400, f"Invalid category. Must be one of {sorted(_VALID_CATEGORIES)}")
    if db.get(SessionRow, body.id) is not None:
        raise HTTPException(409, f"Session {body.id!r} already exists")

    if actor.id is None:
        raise HTTPException(400, "Sessions can only be created by JWT-authenticated admins")

    s = SessionRow(
        id=body.id,
        name=body.name,
        category=body.category,
        state=SessionState.CREATED.value,
        config=body.config,
        include_in_fanout=body.include_in_fanout,
        created_by=actor.id,
    )
    db.add(s)
    audit.record(
        db,
        action="session.created",
        actor_user_id=actor.id,
        actor_ip=client_ip(request),
        target_kind="session",
        target_id=body.id,
        metadata={"category": body.category, "name": body.name},
    )
    db.commit()
    return SessionResponse.from_orm_session(s)


@router.patch("/{sid}", response_model=SessionResponse)
def patch_session(
    sid: str,
    body: SessionPatch,
    request: Request,
    response: Response,
    db: Annotated[DbSession, Depends(get_db)],
    actor: Annotated[CurrentUser, Depends(require_platform_admin)],
) -> SessionResponse:
    s = db.get(SessionRow, sid)
    if s is None:
        raise HTTPException(404, "Session not found")

    changes: dict[str, dict[str, Any]] = {}
    if body.name is not None and body.name != s.name:
        changes["name"] = {"from": s.name, "to": body.name}
        s.name = body.name
    if body.config is not None and body.config != s.config:
        # Validate osm_scope if present — fail-fast at save time means the
        # operator gets a clear UI error instead of an opaque build failure
        # when osmium-tool errors out on an unknown scope name.
        if "osm_scope" in body.config:
            from ... import osm_filter

            try:
                body.config["osm_scope"] = osm_filter.validate_scope(body.config["osm_scope"])
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc

        # Validate provider bundles (v0.1.6) on save. We accept the legacy
        # gtfs[]/gtfs="..." shapes too (normalize_providers handles them),
        # but for save-time validation we only error on the v0.1.6-shaped
        # providers list since that's what the v0.1.6 UI emits. Legacy
        # shapes pass through unmodified (they'll be migrated on next
        # PATCH that uses the new UI).
        if isinstance(body.config.get("sources"), dict) and "providers" in body.config["sources"]:
            try:
                providers = ingestion.normalize_providers(body.config)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            # Country-gate: every provider that declares country_iso=X must
            # have at least one master_stations row with country_iso=X.
            # Operator-driven: fail save with a clear message that includes
            # which countries are missing AND suggests the Trainline-import
            # action. UI surfaces this as a prompt with a one-click button.
            declared_countries = {
                p["country_iso"] for p in providers if p["country_iso"]
            }
            if declared_countries:
                missing = _countries_without_stations(db, declared_countries)
                if missing:
                    raise HTTPException(
                        409,  # Conflict — semantically "preconditions not met"
                        detail={
                            "error": "missing_master_stations_for_countries",
                            "missing_countries": sorted(missing),
                            "message": (
                                "Cannot save: no master_stations rows for "
                                f"{sorted(missing)}. Import them from Trainline "
                                "first (POST /api/master/stations/refresh-trainline), "
                                "then retry this save."
                            ),
                        },
                    )
            # All checks passed — write back the canonicalised provider
            # list (drops empty fields, normalises country to upper-case,
            # etc.). Operator never sees the cleanup; the next GET returns
            # the normalised shape.
            body.config["sources"]["providers"] = providers

            # Soft warning when the OSM PBF URL likely doesn't cover one
            # of the declared provider countries (v0.1.7-D). Surfaced via
            # `X-Warnings` response header — UI parses + toasts. Never
            # blocks the save (per agreed design).
            osm_warning = _osm_coverage_warning(
                body.config["sources"].get("osm_pbf"),
                declared_countries,
            )
            if osm_warning:
                response.headers["X-Warnings"] = json.dumps([osm_warning])

        # Track staleness: bump `sources_changed_at` if (and only if) the
        # `sources` subtree actually changed. Edits that only touch
        # osm_scope or other non-sources keys don't bump this — the
        # downloaded data is still fresh w.r.t. URLs.
        if not staleness.sources_subtree_equal(s.config, body.config):
            staleness.mark_sources_changed(body.config)

        changes["config"] = {"from": s.config, "to": body.config}
        s.config = body.config
    if body.include_in_fanout is not None and body.include_in_fanout != s.include_in_fanout:
        changes["include_in_fanout"] = {"from": s.include_in_fanout, "to": body.include_in_fanout}
        s.include_in_fanout = body.include_in_fanout
    if body.state is not None and body.state != s.state:
        if body.state not in _VALID_STATES:
            raise HTTPException(400, f"Invalid state {body.state!r}")
        changes["state"] = {"from": s.state, "to": body.state}
        s.state = body.state

    if changes:
        audit.record(
            db,
            action="session.updated",
            actor_user_id=actor.id,
            actor_ip=client_ip(request),
            target_kind="session",
            target_id=sid,
            metadata={"changes": changes},
        )
    db.commit()
    return SessionResponse.from_orm_session(s)


@router.delete("/{sid}", status_code=status.HTTP_204_NO_CONTENT)
def delete_session(
    sid: str,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    actor: Annotated[CurrentUser, Depends(require_platform_admin)],
) -> Response:
    """Permanently delete a session and all its data.

    This is the irreversible "start from scratch" path — distinct from
    `POST /{sid}/archive` which keeps the inbox/graphs and DB rows
    around so they can be restored. Delete:

      - Wipes session row + all FK-referenced child rows (rebuild_jobs,
        uploads, graph_snapshots, journey_search_executions and their
        trips, mct_overrides, stations_xref).
      - Removes the session's filesystem trees (inbox/<sid>/,
        graphs/<sid>/).
      - Re-runs the sessions orchestrator so the per-session compose
        and nginx fragments drop the deleted session, then touches the
        reload-trigger so the worker tears down the otp-<sid> service
        + reloads nginx.

    NOT done by this endpoint:

      - Audit events tagged with this session: kept (immutable record
        of what once existed). Their `target_id` references a session
        that no longer exists in `sessions`, but `audit_events.target_id`
        is just a string column — no FK enforces it.
      - Master-station rows: never owned by a session, untouched.

    Cascade order matters because FK columns to sessions.id don't have
    `ondelete=CASCADE` (an alembic migration to add cascade everywhere
    would be cleaner long-term — tracked as Phase-3 cleanup).
    """
    s = db.get(SessionRow, sid)
    if s is None:
        raise HTTPException(404, "Session not found")

    # Snapshot what we're about to delete for the audit row — useful when
    # an operator deletes the wrong session and wants to know what was lost.
    audit_metadata = {
        "name": s.name,
        "category": s.category,
        "state": s.state,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "config_summary": {
            "providers": [
                p.get("id")
                for p in (s.config or {}).get("sources", {}).get("providers", [])
            ],
            "had_osm_pbf": bool((s.config or {}).get("sources", {}).get("osm_pbf")),
        },
    }

    # ── DB cascade — explicit because no FK has ondelete=CASCADE today ──
    from ...models import (  # local imports keep top-of-file lean
        GraphSnapshot,
        JourneySearchExecution,
        McTOverride,
        StationXref,
    )

    # journey_search_executions has trips that cascade automatically because
    # the trips→executions FK already has ondelete="CASCADE".
    db.query(JourneySearchExecution).filter(JourneySearchExecution.session_id == sid).delete(
        synchronize_session=False
    )
    db.query(McTOverride).filter(McTOverride.session_id == sid).delete(synchronize_session=False)
    db.query(StationXref).filter(StationXref.session_id == sid).delete(synchronize_session=False)
    db.query(GraphSnapshot).filter(GraphSnapshot.session_id == sid).delete(
        synchronize_session=False
    )
    db.query(Upload).filter(Upload.session_id == sid).delete(synchronize_session=False)
    db.query(RebuildJob).filter(RebuildJob.session_id == sid).delete(synchronize_session=False)
    db.delete(s)
    # Force the unit-of-work to flush BEFORE the orchestrator queries
    # SELECT * FROM sessions. Without this, the orchestrator sees the
    # to-be-deleted session as still present (autoflush doesn't always
    # fire reliably across `db.delete()` + a sibling SELECT in the
    # same transaction, and the consequence is a generated
    # nginx-sessions.conf that still references the dead `otp-<sid>`
    # upstream — nginx then refuses to reload with "host not found in
    # upstream" until someone wipes the file manually).
    db.flush()

    # Re-run the orchestrator so the deleted session drops out of the
    # compose + nginx fragments. Done before commit so any DB-side
    # constraint failure rolls back the orchestrator change too.
    sessions_orchestrator.regenerate(db)

    # ── Filesystem cleanup ─────────────────────────────────────────
    # Done before commit so a permission failure surfaces as 500 rather
    # than leaving the DB inconsistent with disk. Worker has rw on both
    # inbox + graphs volumes; web has rw on inbox + ro on graphs (per
    # current docker-compose), so this delete needs the worker — but
    # we run it inline in web for simplicity. If web lacks permission,
    # the cleanup is best-effort: log and continue.
    import shutil  # local — only needed here

    # Trees to remove:
    #   inbox/<sid>/                       — staged GTFS / OSM / etc.
    #   graphs/<sid>/                      — built graph + timestamped history
    #   graphs/.cache/<sid>/               — streetGraph.obj cache (v0.1.7).
    #                                        Outlives a session otherwise.
    for tree in (
        settings.inbox_dir / sid,
        settings.graph_dir / sid,
        settings.graph_dir / ".cache" / sid,
    ):
        if tree.exists():
            try:
                shutil.rmtree(tree)
            except OSError as exc:
                # Filesystem is reclaimable later via worker cleanup or
                # operator SSH; don't block the API delete on it.
                audit_metadata.setdefault("filesystem_warnings", []).append(
                    {"path": str(tree), "error": str(exc)}
                )

    # Touch the reload trigger so the worker tears down the otp-<sid>
    # container (if state was 'serving') and reloads nginx with the
    # new fragments. Same mechanism as `promote`.
    _RELOAD_TRIGGER.parent.mkdir(parents=True, exist_ok=True)
    _RELOAD_TRIGGER.write_text(datetime.now(UTC).isoformat())

    audit.record(
        db,
        action="session.deleted",
        actor_user_id=actor.id,
        actor_ip=client_ip(request),
        target_kind="session",
        target_id=sid,
        metadata=audit_metadata,
    )
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{sid}/archive", status_code=status.HTTP_204_NO_CONTENT)
def archive_session(
    sid: str,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    actor: Annotated[CurrentUser, Depends(require_platform_admin)],
) -> Response:
    s = db.get(SessionRow, sid)
    if s is None:
        raise HTTPException(404, "Session not found")
    s.state = SessionState.ARCHIVED.value
    s.include_in_fanout = False
    s.archived_at = datetime.now(UTC)
    audit.record(
        db,
        action="session.archived",
        actor_user_id=actor.id,
        actor_ip=client_ip(request),
        target_kind="session",
        target_id=sid,
    )
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ───────────────────────── per-session uploads ─────────────────────────


class UploadResponse(BaseModel):
    id: str
    filename: str
    declared_kind: str
    detected_kind: str
    size_bytes: int
    triggered_rebuild: bool


@router.post("/{sid}/uploads", response_model=UploadResponse, status_code=201)
async def upload_to_session(
    sid: str,
    declared_standard: Annotated[str, Form()],
    file: Annotated[UploadFile, File()],
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    actor: Annotated[CurrentUser, Depends(require_content_manager)],
) -> UploadResponse:
    """Upload one file into this session's inbox.

    Streams to a staging file, sha256-hashes on the way, runs format
    detection, and dispatches via `app.ingestion.dispatch` which:

    - moves the file into the right per-kind subfolder under
      `inbox/<sid>/<kind>/`,
    - rotates any prior file of the same kind (`.old` suffix),
    - enqueues a `RebuildJob` for the worker if the kind triggers one
      (GTFS, OSM-PBF, NeTEx-Nordic, NeTEx-EPIP).

    A new Upload row is persisted; the session state advances to
    `populated` if it was at `created` or `configured`.
    """
    s = db.get(SessionRow, sid)
    if s is None:
        raise HTTPException(404, "Session not found")
    if declared_standard not in detect.KNOWN_KINDS:
        raise HTTPException(400, f"Unknown standard: {declared_standard}")
    if actor.id is None:
        raise HTTPException(400, "Uploads require a JWT-authenticated actor")

    # Stream to a per-session staging file while computing sha256.
    staging = settings.inbox_dir / sid / "_staging"
    staging.mkdir(parents=True, exist_ok=True)
    safe_name = _safe_filename(file.filename or "upload.bin")
    staged_path = staging / f"{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}-{safe_name}"

    sha = hashlib.sha256()
    size = 0
    with staged_path.open("wb") as out:
        while chunk := await file.read(1024 * 1024):  # 1 MiB at a time
            sha.update(chunk)
            out.write(chunk)
            size += len(chunk)

    # Verify the format matches what the user said. dispatch() can move it
    # only if detect agrees.
    detected = detect.detect(staged_path)
    if detected != declared_standard:
        staged_path.unlink(missing_ok=True)
        raise HTTPException(
            400,
            f"File looks like {detected!r}, but declared as {declared_standard!r}",
        )

    triggered = ingestion.dispatch(staged_path, detected, db, session_id=sid)
    # Where did dispatch end up putting it? Reconstruct from the rules.
    final_path = _reconstruct_dispatch_target(sid, detected, staged_path.name)

    # Best-effort cleanup of the staging file (dispatch copy2's into final).
    staged_path.unlink(missing_ok=True)

    upload = Upload(
        session_id=sid,
        user_id=actor.id,
        filename=safe_name,
        declared_kind=declared_standard,
        detected_kind=detected,
        sha256=sha.hexdigest(),
        size_bytes=size,
        stored_path=str(final_path),
        triggered_rebuild=triggered,
    )
    db.add(upload)

    if s.state in (SessionState.CREATED.value, SessionState.CONFIGURED.value):
        s.state = SessionState.POPULATED.value

    audit.record(
        db,
        action="session.upload",
        actor_user_id=actor.id,
        actor_ip=client_ip(request),
        target_kind="upload",
        target_id=str(s.id),
        metadata={
            "session_id": sid,
            "kind": detected,
            "declared": declared_standard,
            "size_bytes": size,
            "triggered_rebuild": triggered,
        },
    )
    db.commit()
    db.refresh(upload)
    return UploadResponse(
        id=str(upload.id),
        filename=upload.filename,
        declared_kind=upload.declared_kind,
        detected_kind=upload.detected_kind,
        size_bytes=upload.size_bytes,
        triggered_rebuild=upload.triggered_rebuild,
    )


_COUNTRY_TO_OSM_HINTS: dict[str, set[str]] = {
    # Country ISO → substrings the OSM URL might contain to suggest coverage.
    # Geofabrik regional names are the common case; "europe" / "world" act
    # as wildcard catch-alls. Heuristic only — false positives (claims an
    # uncovered country) are tolerable; false negatives (claims a covered
    # country isn't covered, surfacing a confusing warning) are not. So
    # this list errs on the side of including more hint strings.
    "FR": {"france", "europe", "world", "planet"},
    "DE": {"germany", "deutschland", "europe", "world", "planet"},
    "IT": {"italy", "italia", "europe", "world", "planet"},
    "ES": {"spain", "espana", "europe", "world", "planet"},
    "PT": {"portugal", "europe", "world", "planet"},
    "NL": {"netherlands", "nederland", "europe", "world", "planet"},
    "BE": {"belgium", "belgie", "europe", "world", "planet"},
    "LU": {"luxembourg", "europe", "world", "planet"},
    "CH": {"switzerland", "suisse", "schweiz", "europe", "world", "planet"},
    "AT": {"austria", "oesterreich", "europe", "world", "planet"},
    "GB": {"britain", "uk", "england", "scotland", "wales", "europe", "world", "planet"},
    "IE": {"ireland", "europe", "world", "planet"},
    "DK": {"denmark", "europe", "world", "planet", "nordic"},
    "SE": {"sweden", "sverige", "europe", "world", "planet", "nordic"},
    "NO": {"norway", "norge", "europe", "world", "planet", "nordic"},
    "FI": {"finland", "europe", "world", "planet", "nordic"},
    "PL": {"poland", "polska", "europe", "world", "planet"},
    "CZ": {"czech", "europe", "world", "planet"},
    "HU": {"hungary", "europe", "world", "planet"},
    "GR": {"greece", "europe", "world", "planet"},
    # Add more as the demonstrator's reach grows. Catch-all behaviour for
    # countries not in this dict: no warning is emitted (we don't know
    # what hints to look for).
}


def _osm_coverage_warning(osm_url: str | None, declared_countries: set[str]) -> str | None:
    """Return a soft warning string when the OSM URL likely doesn't cover one
    or more declared provider countries. Returns None when:

      - osm_url is empty (operator hasn't set one yet — no false positive)
      - declared_countries is empty (no providers, nothing to check)
      - we don't have heuristic hints for any of the declared countries
        (better to stay silent than emit a guess we can't justify)
      - every declared country has at least one matching hint substring
        in the URL

    The warning is **soft** — it never blocks save (per agreed v0.1.6 design).
    Operators sometimes know things our heuristic doesn't (e.g. they merged
    multiple PBFs offline and uploaded the result manually); a hard block
    would frustrate those legitimate cases.
    """
    if not osm_url or not declared_countries:
        return None
    url_lower = osm_url.lower()
    uncovered: list[str] = []
    for ci in sorted(declared_countries):
        hints = _COUNTRY_TO_OSM_HINTS.get(ci)
        if hints is None:
            continue  # we don't know what to look for; stay silent
        if not any(h in url_lower for h in hints):
            uncovered.append(ci)
    if not uncovered:
        return None
    return (
        f"OSM URL doesn't appear to cover {uncovered}. Coordinate searches "
        "outside the PBF's region will fail with LOCATION_NOT_FOUND. "
        "If the URL is correct (e.g. you merged regions offline), ignore "
        "this; otherwise switch to a wider PBF."
    )


def _countries_without_stations(db: DbSession, declared: set[str]) -> set[str]:
    """Return the subset of `declared` ISO codes that have ZERO rows in
    master_stations. Used by the country-gate at session-config-save time.

    Empty input → empty output (cheap path; spares the round-trip).
    Cheap-ish single SELECT GROUP BY query — covers the common case (all
    countries already imported, returns no missing) at near-zero cost.
    """
    if not declared:
        return set()
    rows = db.execute(
        select(MasterStation.country_iso, func.count())
        .where(MasterStation.country_iso.in_(declared))
        .group_by(MasterStation.country_iso)
    ).all()
    present = {ci for ci, count in rows if count > 0}
    return declared - present


def _safe_filename(name: str) -> str:
    """Strip path components and dodgy chars from an uploaded filename."""
    base = Path(name).name
    return re.sub(r"[^A-Za-z0-9._-]", "_", base)[:200] or "upload.bin"


def _reconstruct_dispatch_target(sid: str, kind: str, filename: str) -> Path:
    """Predict where ingestion.dispatch put the file. Mirrors that module's rules."""
    base = settings.inbox_dir / sid
    if kind in ingestion.STAGE_INTO_OTP_INBOX:
        return base / ingestion.STAGE_INTO_OTP_INBOX[kind] / filename
    if kind in ingestion.ARCHIVE_ONLY:
        return base / "archive" / kind / filename
    if kind in ingestion.LOAD_TO_DB:
        ext = Path(filename).suffix
        return base / "runtime" / kind / f"latest{ext}"
    return base / filename  # fallback (shouldn't hit)


# ──────────────────── refresh sources from configured URLs ────────────────────


class RefreshSourcesResponse(BaseModel):
    fetched: list[dict[str, Any]]
    skipped: list[dict[str, Any]]


@router.post("/{sid}/sources/refresh", response_model=RefreshSourcesResponse)
async def refresh_sources(
    sid: str,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    actor: Annotated[CurrentUser, Depends(require_content_manager)],
) -> RefreshSourcesResponse:
    """Download every URL in `config.sources` into the session's inbox.

    `config.sources` is a flat dict of `{kind_key: url}`. Recognised keys:

      gtfs, osm_pbf, netex_nordic, netex_epip, mct, stations

    Unknown keys are skipped. On success the file is dispatched via
    `ingestion.dispatch`, which queues a rebuild for kinds that warrant one.
    Existing files of the same kind are rotated (`.old` suffix) by dispatch.
    """
    s = db.get(SessionRow, sid)
    if s is None:
        raise HTTPException(404, "Session not found")
    if actor.id is None:
        raise HTTPException(400, "Refresh requires a JWT-authenticated actor")

    sources: dict[str, Any] = (s.config or {}).get("sources", {})
    if not sources:
        raise HTTPException(400, "No sources configured. Set config.sources first.")

    staging = settings.inbox_dir / sid / "_staging"
    staging.mkdir(parents=True, exist_ok=True)

    fetched: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    # Build a flat list of download tasks from the session's sources.
    # Each task: (response-key-label, kind, url, feed_id_or_None, staged_filename_or_None).
    # Provider-bundle shape (v0.1.6) and legacy flat shapes (v0.1.4 / pre)
    # both end up here; downstream handling is identical.
    work = _build_refresh_tasks(s.config or {})

    async with httpx.AsyncClient(follow_redirects=True, timeout=600.0) as client:
        for task in work:
            outcome = await _refresh_one_task(
                client, db, sid, staging, task,
            )
            if outcome.get("status") == "fetched":
                fetched.append({k: v for k, v in outcome.items() if k != "status"})
            else:
                skipped.append({k: v for k, v in outcome.items() if k != "status"})

    if fetched and s.state in (SessionState.CREATED.value, SessionState.CONFIGURED.value):
        s.state = SessionState.POPULATED.value

    # Staleness tracking (v0.1.7.1): mark refresh completed so the next
    # rebuild is no longer flagged stale. Done only when at least one
    # task actually fetched — if every URL failed, the on-disk data is
    # still stale and the operator needs to know.
    if fetched:
        if s.config is None:
            s.config = {}
        staleness.mark_refresh_completed(s.config)
        flag_modified(s, "config")

    audit.record(
        db,
        action="session.sources.refreshed",
        actor_user_id=actor.id,
        actor_ip=client_ip(request),
        target_kind="session",
        target_id=sid,
        metadata={"fetched": [f["key"] for f in fetched], "skipped": [s_["key"] for s_ in skipped]},
    )
    db.commit()
    return RefreshSourcesResponse(fetched=fetched, skipped=skipped)


def _url_suffix(url: str) -> str:
    """Best-guess file extension from a URL, for the staged filename."""
    tail = url.rsplit("/", 1)[-1].lower()
    for ext in (".pbf", ".zip", ".csv", ".xml", ".gz"):
        if ext in tail:
            return ext
    return ""


def _stat_size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


# ──── refresh task model — used by both session-wide and per-provider ────


# Per-task tuple shape: (label, kind, url, staged_filename_or_None).
# `label` is what we surface in the API response — operators see things
# like `gtfs[SNCF]` (multi-feed legacy) or `provider[SNCF].timetable`
# (provider-bundle v0.1.6) or `provider[SNCF].mct` etc., not just bare keys.
_RefreshTask = tuple[str, str, str, str | None]


def _build_refresh_tasks(config: dict[str, Any], *, only_provider: str | None = None) -> list[_RefreshTask]:
    """Flatten a session config into a list of download tasks.

    `only_provider`, when set, filters to that provider's tasks only —
    used by the per-provider refresh endpoint. None means "everything".
    Session-level OSM PBF refresh is *only* included when only_provider
    is None (per-provider refresh leaves OSM alone — different concern).

    Two input shapes:
      A. v0.1.6 native — `sources.providers = [{...}]` plus session-level
         `sources.osm_pbf`. Each provider contributes 1-3 tasks (timetable,
         mct, stations_csv) depending on what's set.
      B. v0.1.4 / pre — flat `sources.gtfs/osm_pbf/mct/stations`. Lifted
         into the same task tuples via normalize_providers.
    """
    sources = config.get("sources") or {}
    if not isinstance(sources, dict):
        return []

    tasks: list[_RefreshTask] = []

    # Provider tasks (multi-format timetable + optional mct/stations).
    try:
        providers = ingestion.normalize_providers(config)
    except ValueError:
        # Operator has saved a malformed shape via raw API — skip provider
        # tasks rather than crash refresh. Country-gate / save-time
        # validation is the right place to surface the error; here we
        # just degrade gracefully.
        providers = []

    for p in providers:
        if only_provider is not None and p["id"] != only_provider:
            continue
        pid = p["id"]
        tt = p.get("timetable") or {}
        tt_url = tt.get("url")
        tt_fmt = tt.get("format", "gtfs")
        if tt_url:
            kind = ingestion.TIMETABLE_FORMAT_DETAILS[tt_fmt]["kind"]
            tasks.append((
                f"provider[{pid}].timetable({tt_fmt})",
                kind,
                tt_url,
                ingestion.staged_filename_for_format(pid, tt_fmt),
            ))
        if p.get("mct_url"):
            tasks.append((f"provider[{pid}].mct", "SNCF-MCT", p["mct_url"], None))
        if p.get("stations_csv_url"):
            tasks.append((
                f"provider[{pid}].stations_csv",
                "SNCF-Stations",
                p["stations_csv_url"],
                None,
            ))

    # Session-level OSM PBF (only on full refresh, not per-provider).
    if only_provider is None and isinstance(sources.get("osm_pbf"), str) and sources["osm_pbf"]:
        tasks.append(("osm_pbf", "OSM-PBF", sources["osm_pbf"], None))

    return tasks


async def _refresh_one_task(
    client: httpx.AsyncClient,
    db: DbSession,
    sid: str,
    staging: Path,
    task: _RefreshTask,
) -> dict[str, Any]:
    """Run one download+dispatch task. Returns a dict for the response —
    `status: fetched` or `status: skipped`. Wrapped error handling so the
    per-task failure doesn't abort the rest of the batch.
    """
    label, kind, url, staged_filename = task
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return {"status": "skipped", "key": label, "url": url, "reason": "not an http(s) URL"}

    base_key = label.split("[", 1)[0]
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    staged_name = f"{ts}-{base_key}{_url_suffix(url)}"
    staged_path = staging / staged_name
    try:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            with staged_path.open("wb") as out:
                async for chunk in response.aiter_bytes(1024 * 1024):
                    out.write(chunk)
    except httpx.HTTPError as exc:
        staged_path.unlink(missing_ok=True)
        return {"status": "skipped", "key": label, "url": url, "reason": f"download failed: {exc}"}

    size_bytes = _stat_size(staged_path)
    try:
        ingestion.dispatch(
            staged_path, kind, db,
            session_id=sid,
            staged_filename=staged_filename,
        )
    except Exception as exc:
        staged_path.unlink(missing_ok=True)
        return {"status": "skipped", "key": label, "url": url, "reason": f"dispatch failed: {exc}"}

    staged_path.unlink(missing_ok=True)
    return {"status": "fetched", "key": label, "kind": kind, "url": url, "size_bytes": size_bytes}


# ──── bulk import providers from a National Access Point (v0.1.8) ────


class ImportFromNapBody(BaseModel):
    """Filters for bulk-importing providers from a NAP catalogue.

    All filters are optional — omitting them imports every dataset whose
    URL isn't already in the session. Practical use is to filter by
    country + modes (e.g. country=FR, modes=["rail"]) for a focused
    demonstrator session.

    `preview=True` returns the proposed providers WITHOUT persisting,
    so the UI can show a confirmation table before the operator commits.
    """

    nap_url: str = Field(
        default="https://transport.data.gouv.fr/api/datasets",
        description="NAP catalogue endpoint. Defaults to the French NAP.",
    )
    country: str | None = Field(default=None, max_length=2, description="ISO-2 country filter")
    modes: list[str] | None = Field(
        default=None,
        description="Subset of {rail, urban, bus, bike}. None = no mode filter.",
    )
    include_publishers: list[str] | None = Field(
        default=None,
        description="Optional whitelist — substring match on publisher name.",
    )
    exclude_dataset_ids: list[str] | None = Field(
        default=None,
        description="Optional skip list of NAP dataset ids.",
    )
    preview: bool = Field(
        default=False,
        description="True = dry-run, return proposed providers without saving. "
        "False = persist them to session.config.sources.providers[].",
    )


class ImportFromNapResponse(BaseModel):
    providers: list[dict[str, Any]]
    skipped: list[dict[str, Any]]
    warnings: list[str]
    preview: bool


@router.post(
    "/{sid}/providers/import-from-nap",
    response_model=ImportFromNapResponse,
    summary="Bulk-import providers from a NAP catalogue (preview or commit)",
)
async def import_providers_from_nap(
    sid: str,
    body: ImportFromNapBody,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    actor: Annotated[CurrentUser, Depends(require_platform_admin)],
) -> ImportFromNapResponse:
    """Fetch a NAP catalogue, filter, and add new providers in one call.

    Workflow:
      1. Operator opens the Configure section, clicks "Import from NAP"
      2. UI calls this endpoint with `preview=True` and the chosen filters
      3. Endpoint returns a table of (proposed providers + skipped reasons
         + warnings) — UI shows it as a confirmation modal
      4. Operator clicks Confirm → UI calls again with `preview=False`
      5. Endpoint persists the providers AND returns the same shape

    Persisting also bumps `_meta.sources_changed_at` (v0.1.7.1 staleness
    tracking) so the operator gets the "click Refresh sources before
    Rebuild" reminder.

    Country-gate (v0.1.6) runs on each new provider: if any declares a
    country with no master_stations rows, the save fails with the same
    409 the manual save uses. Operator imports master_stations first,
    then re-runs the bulk import.
    """
    s = db.get(SessionRow, sid)
    if s is None:
        raise HTTPException(404, "Session not found")
    if actor.id is None:
        raise HTTPException(400, "Bulk-import requires a JWT-authenticated actor")

    from ...master import nap_importer

    existing_providers = (s.config or {}).get("sources", {}).get("providers") or []

    try:
        result = await nap_importer.import_from_nap(
            existing_providers=existing_providers,
            nap_url=body.nap_url,
            country=body.country.upper() if body.country else None,
            modes=body.modes,
            include_publishers=body.include_publishers,
            exclude_dataset_ids=body.exclude_dataset_ids,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"NAP fetch failed: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    if body.preview:
        # Dry-run path: return proposal without touching session config.
        return ImportFromNapResponse(
            providers=result["providers"],
            skipped=result["skipped"],
            warnings=result["warnings"],
            preview=True,
        )

    # Commit path: merge new providers into session config + run all the
    # same validations that the regular PATCH endpoint runs (provider
    # validation, country-gate). Reuses normalize_providers so the shape
    # ends up canonical.
    if result["providers"]:
        new_config = dict(s.config or {})
        sources = dict(new_config.get("sources") or {})
        merged_providers = list(existing_providers) + list(result["providers"])
        sources["providers"] = merged_providers
        new_config["sources"] = sources

        # Validate via the same path the manual save uses.
        try:
            providers_canon = ingestion.normalize_providers(new_config)
        except ValueError as exc:
            raise HTTPException(400, f"Imported providers failed validation: {exc}") from exc

        # Country-gate.
        declared_countries = {p["country_iso"] for p in providers_canon if p["country_iso"]}
        if declared_countries:
            missing = _countries_without_stations(db, declared_countries)
            if missing:
                raise HTTPException(
                    409,
                    detail={
                        "error": "missing_master_stations_for_countries",
                        "missing_countries": sorted(missing),
                        "message": (
                            "Imported providers reference countries with no "
                            f"master_stations rows: {sorted(missing)}. Import them "
                            "from Trainline first, then retry the bulk-import."
                        ),
                    },
                )

        # Persist canonicalised providers + bump staleness.
        sources["providers"] = providers_canon
        new_config["sources"] = sources
        staleness.mark_sources_changed(new_config)
        s.config = new_config
        flag_modified(s, "config")

        audit.record(
            db,
            action="session.providers.bulk_imported",
            actor_user_id=actor.id,
            actor_ip=client_ip(request),
            target_kind="session",
            target_id=sid,
            metadata={
                "nap_url": body.nap_url,
                "filters": {
                    "country": body.country,
                    "modes": body.modes,
                    "include_publishers": body.include_publishers,
                    "exclude_dataset_ids": body.exclude_dataset_ids,
                },
                "added_count": len(result["providers"]),
                "added_ids": [p["id"] for p in result["providers"]],
                "skipped_count": len(result["skipped"]),
            },
        )
        db.commit()

    return ImportFromNapResponse(
        providers=result["providers"],
        skipped=result["skipped"],
        warnings=result["warnings"],
        preview=False,
    )


# ──── per-provider refresh endpoint (v0.1.6) ────


@router.post("/{sid}/providers/{pid}/refresh", response_model=RefreshSourcesResponse)
async def refresh_provider(
    sid: str,
    pid: str,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    actor: Annotated[CurrentUser, Depends(require_content_manager)],
) -> RefreshSourcesResponse:
    """Download just one provider's files (timetable + optional MCT + optional
    stations CSV). Doesn't touch the session-level OSM PBF — different concern,
    much heavier file.

    Use case: operator just added IDFM to a session that already has SNCF
    serving live. They click "Refresh" on IDFM's card and only IDFM's URLs
    are fetched. SNCF stays live without unnecessary re-download.

    Side effect: dispatch() queues a rebuild iff a routable kind landed
    (GTFS or NeTEx). Worker debounces and runs phase-1-cached / phase-2 only
    in v0.1.7 once streetGraph caching ships; today still runs both phases.
    """
    s = db.get(SessionRow, sid)
    if s is None:
        raise HTTPException(404, "Session not found")
    if actor.id is None:
        raise HTTPException(400, "Refresh requires a JWT-authenticated actor")

    # Confirm the provider exists in this session's config — otherwise we'd
    # silently no-op, which is a worse UX than 404.
    try:
        providers = ingestion.normalize_providers(s.config or {})
    except ValueError as exc:
        raise HTTPException(400, f"Session config is invalid: {exc}") from exc
    if not any(p["id"] == pid for p in providers):
        raise HTTPException(
            404,
            f"Provider {pid!r} not found in session {sid!r}. "
            f"Known providers: {[p['id'] for p in providers]}",
        )

    staging = settings.inbox_dir / sid / "_staging"
    staging.mkdir(parents=True, exist_ok=True)

    work = _build_refresh_tasks(s.config or {}, only_provider=pid)
    if not work:
        raise HTTPException(
            400,
            f"Provider {pid!r} has no URLs to refresh "
            "(no timetable, MCT, or stations CSV configured).",
        )

    fetched: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    async with httpx.AsyncClient(follow_redirects=True, timeout=600.0) as client:
        for task in work:
            outcome = await _refresh_one_task(client, db, sid, staging, task)
            (fetched if outcome.get("status") == "fetched" else skipped).append(
                {k: v for k, v in outcome.items() if k != "status"}
            )

    if fetched and s.state in (SessionState.CREATED.value, SessionState.CONFIGURED.value):
        s.state = SessionState.POPULATED.value

    # Same staleness clear as the session-wide refresh — see refresh_sources
    # for the lossy-by-design caveat (per-provider refresh clears the flag
    # globally, even if other providers' URLs were also edited but not yet
    # re-downloaded).
    if fetched:
        if s.config is None:
            s.config = {}
        staleness.mark_refresh_completed(s.config)
        flag_modified(s, "config")

    audit.record(
        db,
        action="session.provider.refreshed",
        actor_user_id=actor.id,
        actor_ip=client_ip(request),
        target_kind="session",
        target_id=sid,
        metadata={
            "provider_id": pid,
            "fetched": [f["key"] for f in fetched],
            "skipped": [s_["key"] for s_ in skipped],
        },
    )
    db.commit()
    return RefreshSourcesResponse(fetched=fetched, skipped=skipped)


# ───────────────────────── rebuilds ─────────────────────────


class RebuildJobResponse(BaseModel):
    id: str
    session_id: str | None
    status: str
    log: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None
    graph_path: str | None


@router.post("/{sid}/rebuilds", response_model=RebuildJobResponse, status_code=201)
def enqueue_rebuild(
    sid: str,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    actor: Annotated[CurrentUser, Depends(require_content_manager)],
) -> RebuildJobResponse:
    """Manually enqueue a rebuild for this session. Idempotent — coalesces
    with any existing pending job for the same session.

    Refuses (400) when inputs aren't staged on disk. The OTP build itself
    fails ~30 seconds in with "no OSM data available" if you skip the
    Refresh-sources step; this guard saves operators that round-trip and
    points them at the action they actually need.
    """
    s = db.get(SessionRow, sid)
    if s is None:
        raise HTTPException(404, "Session not found")

    # Guard: confirm the inputs OTP needs are actually on disk. The session
    # state advances to `populated` when a refresh succeeds OR an upload
    # lands, so checking state alone isn't enough — operators can be at
    # `populated` from a GTFS upload while OSM is still missing. Inspect
    # the filesystem directly.
    sess_inbox = ingestion.session_inbox(sid)
    gtfs_zips = sorted((sess_inbox / "gtfs").glob("*.zip")) if (sess_inbox / "gtfs").exists() else []
    netex_zips = sorted((sess_inbox / "netex").glob("*.zip")) if (sess_inbox / "netex").exists() else []
    osm_pbfs = sorted((sess_inbox / "osm").glob("*.pbf")) if (sess_inbox / "osm").exists() else []
    if not (gtfs_zips or netex_zips):
        raise HTTPException(
            400,
            f"No transit feed staged for session {sid!r}. "
            "Click 'Refresh all sources' (or use the Upload form) before Rebuild graph.",
        )
    if not osm_pbfs:
        raise HTTPException(
            400,
            f"No OSM PBF staged for session {sid!r}. "
            "Click 'Refresh all sources' (or upload one manually) before Rebuild graph.",
        )

    # Reuse the same coalescing logic ingestion uses.
    pending = (
        db.query(RebuildJob)
        .filter(RebuildJob.status == "pending")
        .filter(RebuildJob.session_id == sid)
        .first()
    )
    if pending is None:
        pending = RebuildJob(
            session_id=sid,
            status="pending",
            log=f"queued at {datetime.now(UTC).isoformat()} — manual trigger\n",
        )
        db.add(pending)
        db.flush()

    audit.record(
        db,
        action="session.rebuild.enqueued",
        actor_user_id=actor.id,
        actor_ip=client_ip(request),
        target_kind="session",
        target_id=sid,
        metadata={"job_id": str(pending.id)},
    )
    db.commit()
    db.refresh(pending)
    return _job_to_response(pending)


@router.get("/{sid}/rebuilds", response_model=list[RebuildJobResponse])
def list_rebuilds(
    sid: str,
    db: Annotated[DbSession, Depends(get_db)],
    _: Annotated[CurrentUser, Depends(require_content_manager)],
    limit: int = 20,
) -> list[RebuildJobResponse]:
    """Recent rebuild jobs for this session, newest first."""
    rows = (
        db.query(RebuildJob)
        .filter(RebuildJob.session_id == sid)
        .order_by(desc(RebuildJob.created_at))
        .limit(limit)
        .all()
    )
    return [_job_to_response(j) for j in rows]


def _job_to_response(j: RebuildJob) -> RebuildJobResponse:
    return RebuildJobResponse(
        id=str(j.id),
        session_id=j.session_id,
        status=j.status,
        log=j.log,
        created_at=j.created_at.isoformat() if j.created_at else "",
        started_at=j.started_at.isoformat() if j.started_at else None,
        finished_at=j.finished_at.isoformat() if j.finished_at else None,
        graph_path=j.graph_path,
    )


# ───────────────────────── promote ─────────────────────────


class PromoteResponse(BaseModel):
    state: str
    fragments_written: bool


@router.post("/{sid}/promote", response_model=PromoteResponse)
def promote_session(
    sid: str,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    actor: Annotated[CurrentUser, Depends(require_platform_admin)],
) -> PromoteResponse:
    """Move a `graph_built` session to `serving`.

    Steps:
      1. Validate state == 'graph_built' (or 'serving' for re-trigger).
      2. Set state to 'serving' so the orchestrator includes it.
      3. Regenerate compose + nginx fragments
         (`app.sessions_orchestrator.regenerate`).
      4. Touch the reload-trigger file. The worker, on its next tick,
         runs `docker compose -p viator up -d` (which picks up the new
         `otp-<sid>` service) and `nginx -s reload` (which picks up the
         new `/otp/<sid>/` location), then deletes the trigger file.

    Steps 4's effect is **eventually consistent** with state=='serving' —
    there's a ≤15 s window between the state flip and the otp-<sid>
    container actually being routable. Phase-B will close that window by
    making the worker pick this up via DB state instead of the trigger.
    """
    s = db.get(SessionRow, sid)
    if s is None:
        raise HTTPException(404, "Session not found")
    if s.state not in (SessionState.GRAPH_BUILT.value, SessionState.SERVING.value):
        raise HTTPException(
            400,
            f"Session must be in state 'graph_built' to promote (current: {s.state!r})",
        )

    s.state = SessionState.SERVING.value

    sessions_orchestrator.regenerate(db)
    _RELOAD_TRIGGER.parent.mkdir(parents=True, exist_ok=True)
    _RELOAD_TRIGGER.write_text(datetime.now(UTC).isoformat())

    audit.record(
        db,
        action="session.promoted",
        actor_user_id=actor.id,
        actor_ip=client_ip(request),
        target_kind="session",
        target_id=sid,
    )
    db.commit()
    return PromoteResponse(state=s.state, fragments_written=True)
