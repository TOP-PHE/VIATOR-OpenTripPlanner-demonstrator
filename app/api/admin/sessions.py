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
import logging
import re
import uuid
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
from ...models import AuditEvent, GraphSnapshot, MasterStation, RebuildJob, Upload
from ...models import Session as SessionRow
from ...models.sessions import SessionCategory, SessionState
from ...security import (
    CurrentUser,
    client_ip,
    require_content_manager,
    require_platform_admin,
)
from ...settings import settings

log = logging.getLogger(__name__)

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

        # v0.1.21 — validate otp_timezone if present. Same fail-fast rationale:
        # an invalid IANA tz here would cause OTP to refuse the build with
        # "Cannot resolve zone id <bogus>". Catching at save time means the
        # operator sees the error in a toast next to the dropdown instead of
        # 5 minutes into a rebuild log.
        if "otp_timezone" in body.config:
            from ... import otp_timezone as _otp_tz

            try:
                body.config["otp_timezone"] = _otp_tz.validate_timezone(
                    body.config["otp_timezone"]
                )
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc

        # v0.1.23 — validate otp_build_heap. Same fail-fast pattern. A bad
        # value (e.g. "12 GB" with a space, or "12gb" with the wrong unit)
        # would silently fall back to the env-var default at the worker —
        # confusing because the operator's deliberate UI choice would be
        # invisibly ignored. Reject up front.
        if "otp_build_heap" in body.config:
            from ... import otp_heap as _otp_heap

            try:
                body.config["otp_build_heap"] = _otp_heap.validate_heap(
                    body.config["otp_build_heap"]
                )
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc

        # v0.1.24 — validate otp_api_timeout. Operator picks how long OTP
        # is allowed to spend per journey-search request. Bad values
        # (e.g. "30 s" with a space, "30sec", ISO-8601 "PT30S") would
        # silently fall back to default; reject up front for the same
        # reason as the other knobs.
        if "otp_api_timeout" in body.config:
            from ... import otp_api_timeout as _otp_api_timeout

            try:
                body.config["otp_api_timeout"] = _otp_api_timeout.validate_timeout(
                    body.config["otp_api_timeout"]
                )
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
            declared_countries = {p["country_iso"] for p in providers if p["country_iso"]}
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
    # Explicit dict[str, Any] type so the later `setdefault("filesystem_warnings", [])`
    # below is allowed; without the annotation mypy narrows the value type to
    # the union of the literal initialiser values and rejects appending a list.
    audit_metadata: dict[str, Any] = {
        "name": s.name,
        "category": s.category,
        "state": s.state,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "config_summary": {
            "providers": [
                p.get("id") for p in (s.config or {}).get("sources", {}).get("providers", [])
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


# v0.1.19 — per-provider fetch status, surfaced on the provider cards in the
# admin UI. Read-only view derived from filesystem (inbox file mtime + size)
# plus the latest refresh audit row. No new DB table; the audit log + inbox
# already carry everything we need to disambiguate "never attempted" from
# "fetched OK" from "last attempt failed".
class ProviderStatus(BaseModel):
    feed_id: str
    state: str  # "ok" | "stale" | "pending" | "error"
    fetched_at: datetime | None = None
    size_bytes: int | None = None
    error_hint: str | None = None  # short, UI-friendly explanation when state == "error"


# How recently a provider's inbox file must have been refreshed before we
# stop calling it "ok" and start calling it "stale". 24h is a sensible
# default for daily-published GTFS / GTFS-RT-paired feeds. Operator-tunable
# later if anyone asks; today there's no operator nudge to make it
# session-specific so we keep it module-level and obvious.
_PROVIDER_FRESHNESS_HOURS = 24


@router.post("/{sid}/sources/refresh", response_model=RefreshSourcesResponse)
async def refresh_sources(
    sid: str,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    actor: Annotated[CurrentUser, Depends(require_content_manager)],
) -> RefreshSourcesResponse:
    """Download every PROVIDER URL in `config.sources` into the session's inbox.

    Includes: each provider's timetable + GTFS-RT (handled by OTP at runtime,
    not pre-fetched here) + MCT + stations CSV. Excludes the session-level
    OSM PBF — that has its own POST /sources/osm/refresh endpoint
    (v0.1.14) so a provider tweak doesn't accidentally invalidate the
    streetGraph cache and add 30 min to the next build.

    On success the file is dispatched via `ingestion.dispatch`, which
    queues a rebuild for kinds that warrant one. Existing files of the
    same kind are rotated (`.old` suffix) by dispatch.
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

    # v0.1.14: providers-only by default. OSM PBF lives behind its own
    # endpoint so refreshing a GTFS feed doesn't accidentally bust the
    # streetGraph cache. See `_build_refresh_tasks`'s `include_osm` doc.
    work = _build_refresh_tasks(s.config or {}, include_osm=False)

    async with httpx.AsyncClient(follow_redirects=True, timeout=600.0) as client:
        for task in work:
            outcome = await _refresh_one_task(
                client,
                db,
                sid,
                staging,
                task,
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
        metadata={
            "scope": "providers",  # v0.1.14: distinguishes from osm-only refreshes
            "fetched": [f["key"] for f in fetched],
            "skipped": [s_["key"] for s_ in skipped],
        },
    )
    db.commit()
    return RefreshSourcesResponse(fetched=fetched, skipped=skipped)


# ──── OSM-only refresh (v0.1.14) ────


# Number of historical OSM PBFs to keep on rotation. Each refresh shifts
# osm.pbf → osm.pbf.old.1 (and existing .old.1 → .old.2, etc.); anything
# beyond this count is deleted. Reasonable budget on disk for France-wide:
# ~5 GB x 3 = 15 GB. Operators on a tight VPS can manually delete .old.N
# files between refreshes if disk pressure builds.
_OSM_OLD_GENERATIONS_KEPT = 3


def _rotate_osm_pbf(session_inbox: Path) -> list[str]:
    """Shift osm.pbf → osm.pbf.old.1 → .old.2 → .old.N. Returns a list
    of human-readable rotation events for the audit/UI response.

    Idempotent on missing files — if there's no current osm.pbf yet, no-ops.
    Best-effort on individual rename failures (logs and continues so the
    rotation can still proceed; surfacing as a warning is more useful than
    aborting the whole refresh).
    """
    osm_dir = session_inbox / "osm"
    events: list[str] = []
    if not osm_dir.is_dir():
        return events

    # 1. Drop the oldest generation if it would push us over budget.
    oldest = osm_dir / f"osm.pbf.old.{_OSM_OLD_GENERATIONS_KEPT}"
    if oldest.exists():
        try:
            oldest.unlink()
            events.append(f"deleted oldest .old.{_OSM_OLD_GENERATIONS_KEPT}")
        except OSError as exc:
            log.warning("could not delete %s: %s", oldest, exc)

    # 2. Shift .old.<N-1> → .old.<N>, .old.<N-2> → .old.<N-1>, etc.
    for n in range(_OSM_OLD_GENERATIONS_KEPT - 1, 0, -1):
        src = osm_dir / f"osm.pbf.old.{n}"
        dst = osm_dir / f"osm.pbf.old.{n + 1}"
        if src.exists():
            try:
                src.rename(dst)
                events.append(f".old.{n} → .old.{n + 1}")
            except OSError as exc:
                log.warning("could not rotate %s → %s: %s", src, dst, exc)

    # 3. Move the current osm.pbf → osm.pbf.old.1 (if it exists).
    current = osm_dir / "osm.pbf"
    if current.exists():
        try:
            current.rename(osm_dir / "osm.pbf.old.1")
            events.append("osm.pbf → .old.1")
        except OSError as exc:
            log.warning("could not rotate %s → osm.pbf.old.1: %s", current, exc)

    return events


class RefreshOsmResponse(BaseModel):
    fetched: list[dict[str, Any]]
    skipped: list[dict[str, Any]]
    rotated: list[str]


@router.post("/{sid}/sources/osm/refresh", response_model=RefreshOsmResponse)
async def refresh_osm(
    sid: str,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    actor: Annotated[CurrentUser, Depends(require_content_manager)],
) -> RefreshOsmResponse:
    """Re-download the session's OSM PBF only.

    **Side effect that operators MUST know about**: the streetGraph.obj
    cache key is `sha256(osm.pbf):scope`. Geofabrik rolls the PBF nightly,
    so any non-trivial gap between the cached fetch and this one will
    invalidate the cache → the next rebuild includes a 25-min full OSM
    parse + intersect step.

    The UI's "Refresh OSM" button shows a confirm dialog with this
    warning. CLI callers see it documented in this docstring.

    Rotation: before overwriting, the current `osm.pbf` is shifted to
    `osm.pbf.old.1` (existing `.old.1` → `.old.2`, etc., up to
    _OSM_OLD_GENERATIONS_KEPT). This makes a manual rollback ("revert to
    yesterday's OSM") a one-command `mv` away — useful when a fresh
    Geofabrik PBF turns out to have a regression.

    Returns:
        `fetched`  — the OSM-PBF download outcome (one item, or zero on skip)
        `skipped`  — non-empty only if the OSM URL is unset or download failed
        `rotated`  — list of rotation events for the audit trail / UI display
    """
    s = db.get(SessionRow, sid)
    if s is None:
        raise HTTPException(404, "Session not found")
    if actor.id is None:
        raise HTTPException(400, "Refresh requires a JWT-authenticated actor")

    sources: dict[str, Any] = (s.config or {}).get("sources", {})
    osm_url = sources.get("osm_pbf")
    if not isinstance(osm_url, str) or not osm_url:
        raise HTTPException(400, "config.sources.osm_pbf is unset; nothing to refresh")

    staging = settings.inbox_dir / sid / "_staging"
    staging.mkdir(parents=True, exist_ok=True)

    # Rotate BEFORE downloading so a failed fetch leaves the previous
    # generation in place but recoverable from .old.1 (operator can mv it
    # back to osm.pbf if they need it).
    rotated = _rotate_osm_pbf(settings.inbox_dir / sid)

    fetched: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    # Pass staged_filename="osm.pbf" so dispatch uses the targeted-rotation
    # branch (only rotates the exact file). The legacy "rotate everything
    # not ending in .old" branch would re-rotate our .old.<N> generations,
    # producing garbage like osm.pbf.old.1.old. See app/ingestion.py.
    task: _RefreshTask = ("osm_pbf", "OSM-PBF", osm_url, "osm.pbf", None)
    async with httpx.AsyncClient(follow_redirects=True, timeout=600.0) as client:
        outcome = await _refresh_one_task(client, db, sid, staging, task)
        if outcome.get("status") == "fetched":
            fetched.append({k: v for k, v in outcome.items() if k != "status"})
        else:
            skipped.append({k: v for k, v in outcome.items() if k != "status"})

    if fetched:
        if s.config is None:
            s.config = {}
        staleness.mark_refresh_completed(s.config)
        flag_modified(s, "config")

    audit.record(
        db,
        action="session.osm.refreshed",
        actor_user_id=actor.id,
        actor_ip=client_ip(request),
        target_kind="session",
        target_id=sid,
        metadata={
            "url": osm_url,
            "rotated": rotated,
            "fetched": [f["key"] for f in fetched],
            "skipped": [s_["key"] for s_ in skipped],
            # Flagged so monitoring can alert on osm refreshes (they're rare
            # by intent — every one invalidates the streetGraph cache).
            "invalidates_street_graph_cache": bool(fetched),
        },
    )
    db.commit()
    return RefreshOsmResponse(fetched=fetched, skipped=skipped, rotated=rotated)


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


# Per-task tuple shape: (label, kind, url, staged_filename_or_None, credential_id_or_None).
# `label` is what we surface in the API response — operators see things
# like `gtfs[SNCF]` (multi-feed legacy) or `provider[SNCF].timetable`
# (provider-bundle v0.1.6) or `provider[SNCF].mct` etc., not just bare keys.
# `credential_id` (v0.1.10) is the optional UUID of a user_credentials row
# whose decrypted secret should be applied to the HTTP request. None means
# anonymous fetch (the v0.1.6-v0.1.9 default behaviour).
_RefreshTask = tuple[str, str, str, str | None, str | None]


def _build_refresh_tasks(
    config: dict[str, Any],
    *,
    only_provider: str | None = None,
    include_osm: bool = False,
) -> list[_RefreshTask]:
    """Flatten a session config into a list of download tasks.

    `only_provider`, when set, filters to that provider's tasks only —
    used by the per-provider refresh endpoint. None means "everything".

    `include_osm` (v0.1.14): controls whether the session-level OSM PBF
    is included. **Default False** — refreshing providers must NOT
    re-fetch OSM, because Geofabrik rolls the PBF nightly, which would
    invalidate the streetGraph.obj cache (sha256(osm.pbf):scope) and
    force a 30-min full rebuild on what should have been a quick
    transit-only swap. The dedicated POST /sources/osm/refresh endpoint
    sets this to True.

    Per-provider refresh (only_provider != None) ignores `include_osm`
    entirely — it never refreshes OSM.

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
            tasks.append(
                (
                    f"provider[{pid}].timetable({tt_fmt})",
                    kind,
                    tt_url,
                    ingestion.staged_filename_for_format(pid, tt_fmt),
                    p.get("timetable_credential_id"),
                )
            )
        if p.get("mct_url"):
            tasks.append(
                (
                    f"provider[{pid}].mct",
                    "SNCF-MCT",
                    p["mct_url"],
                    None,
                    p.get("mct_credential_id"),
                )
            )
        if p.get("stations_csv_url"):
            tasks.append(
                (
                    f"provider[{pid}].stations_csv",
                    "SNCF-Stations",
                    p["stations_csv_url"],
                    None,
                    p.get("stations_csv_credential_id"),
                )
            )

    # Session-level OSM PBF — opt-in via include_osm (v0.1.14). Per-provider
    # refresh never includes OSM. Geofabrik-class hosts don't require auth,
    # so no credential field today.
    if (
        only_provider is None
        and include_osm
        and isinstance(sources.get("osm_pbf"), str)
        and sources["osm_pbf"]
    ):
        tasks.append(("osm_pbf", "OSM-PBF", sources["osm_pbf"], None, None))

    return tasks


# v0.1.19 — pure state-derivation helper. Lives alongside `_build_refresh_tasks`
# because both translate the same provider-config shape into something the UI
# cares about: that one tells you what *will* be fetched next, this one tells
# you what *has* been fetched already and how that's going.
#
# Pure: takes only data the caller has already gathered (file metadata + audit
# meta dict) and returns a ProviderStatus. No DB / FS access of its own —
# makes it trivial to unit-test without TestClient or Postgres.
def _derive_provider_status(
    *,
    feed_id: str,
    timetable_format: str,
    inbox_root: Path,
    latest_audit_meta: dict[str, Any] | None,
    now: datetime,
    freshness_hours: int = _PROVIDER_FRESHNESS_HOURS,
) -> ProviderStatus:
    """Decide what to show on a provider card based on inbox + audit state.

    State machine (priority order):
      - file present, mtime within freshness window           → "ok"
      - file present, mtime older than freshness window       → "stale"
      - file missing, last refresh skipped this provider      → "error"
      - file missing, no audit history (or audit didn't touch
        this provider)                                        → "pending"

    The audit metadata we accept is whatever the existing
    `session.sources.refreshed` / `session.provider.refreshed` rows already
    record — a `fetched: [task_key]` and `skipped: [task_key]` list, where
    each task_key looks like `provider[SNCF].timetable(gtfs)` or
    `provider[SNCF].mct`. We match by the `provider[<feed_id>].` prefix so
    timetable / mct / stations_csv tasks all roll up to the same provider.

    `inbox_root` is the per-session inbox dir (`/data/inbox/<sid>`). The file
    we're looking for is at `<inbox_root>/<subdir>/<feed_id_lower>.zip` where
    `<subdir>` is `gtfs/` or `netex/` per the timetable format — same
    convention `dispatch()` uses to stage downloaded files.
    """
    fmt_details = ingestion.TIMETABLE_FORMAT_DETAILS.get(timetable_format)
    if fmt_details is None:
        # Unknown format on a saved provider — degrade to pending rather than
        # crash the endpoint. The country-gate / save-time validation is the
        # right place to refuse the bad value; here we just don't pretend to
        # know where its file would live.
        return ProviderStatus(feed_id=feed_id, state="pending")

    file_path = inbox_root / fmt_details["subdir"] / ingestion.staged_filename_for_format(
        feed_id, timetable_format
    )

    fetched_at: datetime | None = None
    size_bytes: int | None = None
    if file_path.is_file():
        st = file_path.stat()
        fetched_at = datetime.fromtimestamp(st.st_mtime, tz=UTC)
        size_bytes = st.st_size

    in_fetched = False
    in_skipped = False
    if latest_audit_meta:
        marker = f"provider[{feed_id}]."
        in_fetched = any(
            isinstance(k, str) and k.startswith(marker)
            for k in latest_audit_meta.get("fetched", [])
        )
        in_skipped = any(
            isinstance(k, str) and k.startswith(marker)
            for k in latest_audit_meta.get("skipped", [])
        )

    if fetched_at is not None:
        age_h = (now - fetched_at).total_seconds() / 3600.0
        state = "ok" if age_h <= freshness_hours else "stale"
        # If the *latest* audit row has this provider only in skipped (no
        # successful task), the file is from an earlier successful run but
        # the most recent attempt failed. Surface that as a partial-error
        # hint without flipping the whole state to "error" — the operator
        # still has usable data, just stale-after-failed-refresh.
        error_hint = None
        if in_skipped and not in_fetched:
            error_hint = "last refresh failed — using previous file"
        return ProviderStatus(
            feed_id=feed_id,
            state=state,
            fetched_at=fetched_at,
            size_bytes=size_bytes,
            error_hint=error_hint,
        )

    # File is missing — was it ever attempted?
    if in_skipped and not in_fetched:
        return ProviderStatus(
            feed_id=feed_id,
            state="error",
            error_hint="last refresh attempt failed — click Refresh to see why",
        )
    return ProviderStatus(feed_id=feed_id, state="pending")


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
    label, kind, url, staged_filename, credential_id = task
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return {"status": "skipped", "key": label, "url": url, "reason": "not an http(s) URL"}

    # Resolve credential (v0.1.10) — none → fetch anonymously, missing →
    # skip with a clear reason so the operator knows to detach or pick a
    # different one. Decryption failure (e.g. JWT_SECRET rotated) is
    # surfaced the same way: this task fails, but other tasks proceed.
    extra_headers: dict[str, str] = {}
    fetch_url = url
    cred = None
    if credential_id:
        # Triple-dot: this file is at app/api/admin/sessions.py; we need
        # `app.credentials` (the crypto module) and `app.models`. Single-dot
        # would resolve to `app.api.credentials` (the router we just added).
        from ... import credentials as crypto_module
        from ...models import UserCredential

        cred = db.get(UserCredential, uuid.UUID(credential_id))
        if cred is None:
            return {
                "status": "skipped",
                "key": label,
                "url": url,
                "reason": f"credential {credential_id} not found (was it deleted?)",
            }
        try:
            fetch_url, extra_headers = crypto_module.apply_credential(
                cred, url, settings.jwt_secret
            )
        except crypto_module.CredentialDecryptError as exc:
            return {
                "status": "skipped",
                "key": label,
                "url": url,
                "reason": f"credential {cred.name!r} cannot be decrypted: {exc}",
            }

    base_key = label.split("[", 1)[0]
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    staged_name = f"{ts}-{base_key}{_url_suffix(url)}"
    staged_path = staging / staged_name
    try:
        async with client.stream("GET", fetch_url, headers=extra_headers) as response:
            response.raise_for_status()
            with staged_path.open("wb") as out:
                async for chunk in response.aiter_bytes(1024 * 1024):
                    out.write(chunk)
    except httpx.HTTPError as exc:
        staged_path.unlink(missing_ok=True)
        return {"status": "skipped", "key": label, "url": url, "reason": f"download failed: {exc}"}

    # Stamp last_used_at on the credential so users can see "this hasn't
    # been used in months — maybe drop it." Best-effort; failures here
    # don't fail the refresh.
    if cred is not None:
        try:
            cred.last_used_at = datetime.now(UTC)
            db.flush()
        except Exception as exc:
            log.warning("could not stamp last_used_at on credential %s: %s", cred.id, exc)

    size_bytes = _stat_size(staged_path)
    try:
        ingestion.dispatch(
            staged_path,
            kind,
            db,
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

    v0.1.12: identifies the NAP via the catalogue's UUID instead of a free
    URL. The legacy `nap_url` field is still accepted for back-compat (CLI
    callers hitting the API directly) but the UI sends nap_catalogue_id.
    Exactly one of the two must be set.
    """

    nap_catalogue_id: str | None = Field(
        default=None,
        description="UUID of a row in nap_catalogues. Server resolves URL + "
        "credential at fetch time. Preferred over nap_url since v0.1.12.",
    )
    nap_url: str | None = Field(
        default=None,
        description="DIRECT NAP endpoint URL. Legacy escape hatch — operators "
        "should use nap_catalogue_id (managed at /admin/nap-catalogues) so "
        "credentials can be attached. Anonymous fetch only.",
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
    include_dataset_ids: list[str] | None = Field(
        default=None,
        description="Optional positive list — when set, ONLY datasets whose id "
        "is in the list are kept. Used by the picker UI: preview returns the "
        "full filtered list with dataset_ids; on confirm the operator's "
        "checked subset is sent here so only those get persisted. (v0.1.12)",
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

    from ... import credentials as crypto_module
    from ...master import nap_importer
    from ...models import NapCatalogue, UserCredential

    # v0.1.12: resolve the catalogue → (url, credential). Legacy `nap_url`
    # path stays as an anonymous-only escape hatch for CLI callers; UI
    # always sends nap_catalogue_id.
    nap_url: str
    nap_auth: tuple[str, str, str | None] | None = None  # (auth_type, plaintext, param_name)
    catalogue_name: str | None = None

    if body.nap_catalogue_id:
        try:
            cat_uuid = uuid.UUID(body.nap_catalogue_id)
        except (ValueError, TypeError) as exc:
            raise HTTPException(
                400, f"nap_catalogue_id={body.nap_catalogue_id!r} is not a UUID"
            ) from exc
        cat = db.get(NapCatalogue, cat_uuid)
        if cat is None:
            raise HTTPException(404, f"NAP catalogue {body.nap_catalogue_id!r} not found")
        nap_url = cat.url
        catalogue_name = cat.name
        if cat.credential_id is not None:
            cred = db.get(UserCredential, cat.credential_id)
            if cred is None:
                # SET NULL cascade fired but the catalogue row hasn't been
                # re-saved yet — fall back to anonymous + warn in audit.
                log.warning(
                    "catalogue %s references missing credential %s; "
                    "falling back to anonymous NAP fetch",
                    cat.name,
                    cat.credential_id,
                )
            else:
                try:
                    plaintext = crypto_module.decrypt(
                        cred.ciphertext, cred.nonce, settings.jwt_secret
                    )
                except crypto_module.CredentialDecryptError as exc:
                    raise HTTPException(
                        500,
                        f"NAP credential {cred.name!r} cannot be decrypted: {exc}. "
                        "Recreate the credential at /credentials.",
                    ) from exc
                nap_auth = (cred.auth_type, plaintext, cred.param_name)
    elif body.nap_url:
        nap_url = body.nap_url
    else:
        raise HTTPException(
            400,
            "Either nap_catalogue_id (preferred) or nap_url (legacy) must be set.",
        )

    existing_providers = (s.config or {}).get("sources", {}).get("providers") or []

    try:
        result = await nap_importer.import_from_nap(
            existing_providers=existing_providers,
            nap_url=nap_url,
            nap_auth=nap_auth,
            country=body.country.upper() if body.country else None,
            modes=body.modes,
            include_publishers=body.include_publishers,
            exclude_dataset_ids=body.exclude_dataset_ids,
            include_dataset_ids=body.include_dataset_ids,
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
        # v0.1.12: strip the `_nap_dataset_id` bookkeeping field the importer
        # attaches for the picker UI — it shouldn't leak into session.config.
        # Easier to filter here than to thread "is this preview?" into the
        # importer.
        cleaned = [
            {k: v for k, v in p.items() if not k.startswith("_")} for p in result["providers"]
        ]
        new_config = dict(s.config or {})
        sources = dict(new_config.get("sources") or {})
        merged_providers = list(existing_providers) + cleaned
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
                "nap_url": nap_url,
                "nap_catalogue_id": body.nap_catalogue_id,
                "nap_catalogue_name": catalogue_name,
                "nap_authenticated": nap_auth is not None,
                "filters": {
                    "country": body.country,
                    "modes": body.modes,
                    "include_publishers": body.include_publishers,
                    "exclude_dataset_ids": body.exclude_dataset_ids,
                    # v0.1.12 picker: recorded so the audit row shows which
                    # specific datasets the operator hand-picked (vs the
                    # broader filters that were also applied).
                    "include_dataset_ids": body.include_dataset_ids,
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


# ───────────────────────── per-provider status (v0.1.19) ─────────────────────


@router.get("/{sid}/providers/status", response_model=dict[str, ProviderStatus])
def get_providers_status(
    sid: str,
    db: Annotated[DbSession, Depends(get_db)],
    actor: Annotated[CurrentUser, Depends(require_content_manager)],
) -> dict[str, ProviderStatus]:
    """Per-provider fetch status — what's in the inbox, when, and whether the
    last refresh attempt succeeded for each provider configured on this
    session. Used by the admin UI to render status pills on each provider
    card so operators can see at a glance which feeds need a refresh.

    Source-of-truth strategy (v0.1.19, no new tables):
      * **Inbox file** at `<inbox>/<subdir>/<feed_id_lower>.zip` — its
        existence + mtime is the canonical "is this fetched, and when".
      * **Latest refresh audit row** (`session.sources.refreshed` or
        `session.provider.refreshed`) — disambiguates "never attempted"
        from "attempted and failed". Audit metadata only stores task
        keys, not full error reasons; the UI hint says "click Refresh to
        see why" and the per-provider refresh endpoint returns the full
        skip reason in its response on demand.

    A future v0.1.20+ may move this to a dedicated `provider_fetch_status`
    table written by ingestion, with sparkline-grade history. The
    filesystem-derived view here is a deliberate "ship the obvious thing
    first" — operators told us they need *some* visibility right now.
    """
    s = db.get(SessionRow, sid)
    if s is None:
        raise HTTPException(404, "Session not found")

    try:
        providers = ingestion.normalize_providers(s.config or {})
    except ValueError:
        # Same defensive degrade as `_build_refresh_tasks` — if config is
        # somehow malformed, return an empty status map rather than 500.
        # The Configure form will surface the validation error on save.
        return {}

    # Pull the most recent refresh audit row (either scope) for this session.
    # We don't need any older history — only the *latest* attempt informs
    # the "did the last refresh fail for this provider" question.
    latest_audit = db.execute(
        select(AuditEvent)
        .where(
            AuditEvent.target_kind == "session",
            AuditEvent.target_id == sid,
            AuditEvent.action.in_(
                ["session.sources.refreshed", "session.provider.refreshed"]
            ),
        )
        .order_by(desc(AuditEvent.ts))
        .limit(1)
    ).scalar_one_or_none()
    latest_meta = latest_audit.metadata_ if latest_audit is not None else None

    inbox_root = settings.inbox_dir / sid
    now = datetime.now(UTC)

    out: dict[str, ProviderStatus] = {}
    for p in providers:
        fmt = (p.get("timetable") or {}).get("format", "gtfs")
        out[p["id"]] = _derive_provider_status(
            feed_id=p["id"],
            timetable_format=fmt,
            inbox_root=inbox_root,
            latest_audit_meta=latest_meta,
            now=now,
        )
    return out


# ───────────────────────── rebuilds ─────────────────────────


class SnapshotInfo(BaseModel):
    """v0.1.20 — graph_snapshot data joined onto each rebuild job so the admin
    UI can show "what was built, when, what's in it, is it the one currently
    serving" without grovelling through logs.

    Populated by the worker on successful build (see `app/worker.py` —
    `record_snapshot` is called in the success path; older successful
    builds done before v0.1.20 won't have a snapshot row and will surface
    as `snapshot=None` in the response).
    """

    built_at: str
    feed_signature: str  # 64-char sha256 — first 8 chars are shown in the UI
    is_current: bool  # at most one per session has this True
    timetable_main_version: str  # e.g. "2026-W14_2026-W39"
    timetable_update_version: int  # 1, 2, 3... within the same main_version
    service_period_start: str  # ISO date
    service_period_end: str  # ISO date
    source_uploads: list[dict[str, Any]]  # [{filename, sha256, kind, upload_id}]
    main_version_source: str  # "auto" | "manual_override"


class RebuildJobResponse(BaseModel):
    id: str
    session_id: str | None
    status: str
    log: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None
    graph_path: str | None
    # v0.1.20 — derived / joined fields that make the rebuild table useful.
    duration_seconds: int | None = None  # finished_at - started_at, when both exist
    snapshot: SnapshotInfo | None = None  # joined from graph_snapshots by rebuild_job_id
    cache_hit: bool | None = None  # parsed from log; None when not detectable


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
    gtfs_zips = (
        sorted((sess_inbox / "gtfs").glob("*.zip")) if (sess_inbox / "gtfs").exists() else []
    )
    netex_zips = (
        sorted((sess_inbox / "netex").glob("*.zip")) if (sess_inbox / "netex").exists() else []
    )
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
    """Recent rebuild jobs for this session, newest first.

    v0.1.20: each row includes joined `graph_snapshots` data when available
    (`snapshot` field), plus `duration_seconds` and a `cache_hit` flag
    derived from the log. The UI uses these to render the new "Current
    build / History" card layout.
    """
    rows = (
        db.query(RebuildJob)
        .filter(RebuildJob.session_id == sid)
        .order_by(desc(RebuildJob.created_at))
        .limit(limit)
        .all()
    )
    return [_job_to_response(j, db=db) for j in rows]


def _classify_rebuild_log(log: str | None) -> dict[str, Any]:
    """Pure: parse a rebuild log tail for human-actionable signals.

    v0.1.20 detects the streetGraph.obj cache-hit / cache-miss markers
    emitted by the OTP entrypoint (see `docker/otp/entrypoint.sh` lines
    227-235). All three are emitted near the *start* of the build, so
    they survive the 32k log truncation that grabs the tail.

    Marker strings (must match the entrypoint exactly):
      - "streetGraph.obj cache hit (key=..."           → cache_hit=True
      - "streetGraph.obj cache miss (key changed: ..." → cache_hit=False
      - "streetGraph.obj cache empty — building from scratch" → cache_hit=False
      - none of the above                              → cache_hit=None

    cache_hit=False is also surfaced as "first build / cache empty" by
    the entrypoint; we don't distinguish miss-after-key-change from
    first-build because operators see them the same way ("the slow path").

    Tolerant of missing markers — old logs from pre-v0.1.7 builds, builds
    that crashed before reaching the cache phase, and the 32k truncation
    chopping off the relevant line all yield None. UI must handle None
    gracefully (don't claim "cache miss" when we honestly don't know).
    """
    if not log:
        return {"cache_hit": None}
    if "streetGraph.obj cache hit" in log:
        return {"cache_hit": True}
    if "streetGraph.obj cache miss" in log or "streetGraph.obj cache empty" in log:
        return {"cache_hit": False}
    return {"cache_hit": None}


def _snapshot_to_info(snap: GraphSnapshot) -> SnapshotInfo:
    """Convert a GraphSnapshot ORM row into the wire-format SnapshotInfo."""
    return SnapshotInfo(
        built_at=snap.built_at.isoformat() if snap.built_at else "",
        feed_signature=snap.feed_signature or "",
        is_current=bool(snap.is_current),
        timetable_main_version=snap.timetable_main_version or "",
        timetable_update_version=int(snap.timetable_update_version or 0),
        service_period_start=snap.service_period_start.isoformat()
        if snap.service_period_start
        else "",
        service_period_end=snap.service_period_end.isoformat()
        if snap.service_period_end
        else "",
        source_uploads=list(snap.source_uploads or []),
        main_version_source=snap.main_version_source or "auto",
    )


def _job_to_response(j: RebuildJob, db: DbSession | None = None) -> RebuildJobResponse:
    # v0.1.20 — join graph_snapshots when a db handle is provided. Callers
    # that don't have one (the POST /rebuilds endpoint that returns a freshly
    # enqueued job, which obviously has no snapshot yet) pass db=None and
    # get the bare-bones response. The list endpoint always passes db.
    snapshot_info: SnapshotInfo | None = None
    if db is not None:
        snap = db.execute(
            select(GraphSnapshot).where(GraphSnapshot.rebuild_job_id == j.id)
        ).scalar_one_or_none()
        if snap is not None:
            snapshot_info = _snapshot_to_info(snap)

    duration_seconds: int | None = None
    if j.started_at and j.finished_at:
        duration_seconds = int((j.finished_at - j.started_at).total_seconds())

    classification = _classify_rebuild_log(j.log)

    return RebuildJobResponse(
        id=str(j.id),
        session_id=j.session_id,
        status=j.status,
        log=j.log,
        created_at=j.created_at.isoformat() if j.created_at else "",
        started_at=j.started_at.isoformat() if j.started_at else None,
        finished_at=j.finished_at.isoformat() if j.finished_at else None,
        graph_path=j.graph_path,
        duration_seconds=duration_seconds,
        snapshot=snapshot_info,
        cache_hit=classification["cache_hit"],
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
