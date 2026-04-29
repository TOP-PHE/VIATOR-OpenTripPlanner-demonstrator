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
from sqlalchemy import desc, select
from sqlalchemy.orm import Session as DbSession

from ... import audit, detect, ingestion, sessions_orchestrator
from ...db import get_db
from ...models import RebuildJob, Upload
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

    # Flatten the sources dict into (key, url, feed_id_or_None, staged_filename)
    # tuples. Multi-feed GTFS expands to one tuple per feed; everything else is
    # a single tuple. Order is determined by dict-insertion order, which Python
    # 3.7+ guarantees is stable, so SNCF before IDFM before TRENITALIA the way
    # the operator typed them.
    work: list[tuple[str, str, str | None, str | None]] = []
    for key, value in sources.items():
        if key == "gtfs":
            try:
                feeds = ingestion.normalize_gtfs_sources(value)
            except ValueError as exc:
                skipped.append({"key": "gtfs", "reason": f"invalid gtfs config: {exc}"})
                continue
            for feed in feeds:
                fid = feed["id"]
                work.append((f"gtfs[{fid}]", feed["url"], fid, ingestion.gtfs_staged_filename(fid)))
            continue
        # Non-GTFS keys: simple scalar URL.
        if not isinstance(value, str):
            skipped.append({"key": key, "reason": f"expected a URL string, got {type(value).__name__}"})
            continue
        work.append((key, value, None, None))

    async with httpx.AsyncClient(follow_redirects=True, timeout=600.0) as client:
        for key, url, feed_id, staged_filename in work:
            # `key` here is the surface label used in the response payload —
            # `gtfs[SNCF]` for multi-feed entries, `gtfs` / `osm_pbf` / etc.
            # for single-source ones. The kind lookup uses the bracket-stripped
            # form so every gtfs[*] entry resolves to GTFS.
            base_key = key.split("[", 1)[0]
            kind = _SOURCE_KEY_TO_KIND.get(base_key)
            if kind is None:
                skipped.append({"key": key, "reason": f"unknown source key {base_key!r}"})
                continue
            if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                skipped.append({"key": key, "url": url, "reason": "not an http(s) URL"})
                continue

            ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
            staged_name = f"{ts}-{base_key}{('-' + feed_id) if feed_id else ''}{_url_suffix(url)}"
            staged_path = staging / staged_name
            try:
                async with client.stream("GET", url) as response:
                    response.raise_for_status()
                    with staged_path.open("wb") as out:
                        async for chunk in response.aiter_bytes(1024 * 1024):
                            out.write(chunk)
            except httpx.HTTPError as exc:
                staged_path.unlink(missing_ok=True)
                skipped.append({"key": key, "url": url, "reason": f"download failed: {exc}"})
                continue

            size_bytes = _stat_size(staged_path)
            # detect.detect can be slow on large OSM PBFs; we trust the
            # configured key here since the operator picked it. `staged_filename`
            # is None for everything except multi-feed GTFS — dispatch falls
            # back to the canonical default per kind.
            ingestion.dispatch(
                staged_path,
                kind,
                db,
                session_id=sid,
                staged_filename=staged_filename,
            )
            staged_path.unlink(missing_ok=True)

            fetched.append(
                {"key": key, "kind": kind, "url": url, "size_bytes": size_bytes}
            )

    if fetched and s.state in (SessionState.CREATED.value, SessionState.CONFIGURED.value):
        s.state = SessionState.POPULATED.value

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
    with any existing pending job for the same session."""
    s = db.get(SessionRow, sid)
    if s is None:
        raise HTTPException(404, "Session not found")

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
