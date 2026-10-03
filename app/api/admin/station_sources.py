"""Station sources: how every station input is acquired (screen E).

    GET    /api/admin/stations/sources                  every source, with its latest version
    POST   /api/admin/stations/sources                  add a source
    PATCH  /api/admin/stations/sources/{key}            edit, enable or disable
    DELETE /api/admin/stations/sources/{key}            only a source that never acquired a file
    GET    /api/admin/stations/sources/{key}/versions   the files acquired for one source
    POST   /api/admin/stations/sources/{key}/versions   upload one file of a source
    GET    /api/admin/stations/builds                   build history and queued build jobs

Authorization: platform_admin only, declared on every route. There is no
router-level dependency in this codebase, so a route has no auth unless its
own signature says so.

Why a dedicated upload route: both existing ones refuse a station file. They
require a `declared_standard` in `detect.KNOWN_KINDS`, and `detect` accepts a
CSV only if it looks like SNCF stations or MCT. This route never calls
`detect.detect` nor `ingestion.dispatch`: a station file belongs to no session
and stages into no engine inbox. It is streamed to `inbox/_stations/<key>/`
while its sha256 is computed, and recorded as a `station_source_version`.

`kind`, `format`, `acquisition` and `resolver_type` are plain text in the
database and are validated here: a new value needs new code anyway (a parser,
a resolver), so the schema is not the place to pin them.

See docs/station-panel-design.md section 4E and section 11.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from ... import audit, feed_resolvers
from ...db import get_db
from ...master import station_files, station_import, station_store
from ...models import (
    RebuildJob,
    StationBuild,
    StationSource,
    StationSourceVersion,
    UserCredential,
)
from ...security import CurrentUser, client_ip, require_platform_admin
from ...settings import settings

router = APIRouter(prefix="/api/admin/stations", tags=["admin", "stations"])

SOURCE_KINDS: frozenset[str] = frozenset(
    {"spine", "timetable", "registry", "merits_input", "crosscheck", "offline_build"}
)
SOURCE_ACQUISITIONS: frozenset[str] = frozenset({"resolver", "url", "upload"})
# The five offline shapes, the Trainline CSV the daily refresh reads, a column
# of the offline master, and `other`: a file that is kept and never parsed.
SOURCE_FORMATS: frozenset[str] = frozenset(
    {*station_files.FILE_SHAPES, "trainline_csv", "offline_master_column", "other"}
)

# An access grant ending within this many days is shown in amber; past, in red.
ACCESS_WARN_DAYS = 90
STATION_BUILD_KIND = station_import.STATION_BUILD_KIND
_NOT_FOUND = "Station source not found"
_QUEUED = "a station build is queued"
_ALREADY_QUEUED = "a station build was already queued"
# Columns that are NOT NULL: a PATCH may change them, never blank them.
_NOT_NULLABLE = (
    "label",
    "kind",
    "format",
    "acquisition",
    "triggers_rebuild",
    "enabled",
    "source_key_unresolved",
)


# ──────────────────────────── pydantic models ────────────────────────────


class VersionResponse(BaseModel):
    id: str
    source_key: str
    acquired_at: str | None
    as_of: str | None
    filename: str
    bytes: int
    sha256: str
    status: str
    error: str | None
    stats: dict[str, Any] | None


class BuildDecision(BaseModel):
    """Whether a station build was queued, and if not, why not."""

    queued: bool
    note: str


_UNCHANGED = BuildDecision(queued=False, note="the same file was already there: nothing changed")


class UploadResult(BaseModel):
    # False when the same file was already there: nothing was written.
    created: bool
    version: VersionResponse
    build: BuildDecision


class SourceResponse(BaseModel):
    id: str
    key: str
    label: str
    kind: str
    format: str
    acquisition: str
    resolver_type: str | None
    resolver_config: dict[str, Any] | None
    credential_id: str | None
    credential_name: str | None
    country_iso: str | None
    operator: str | None
    licence: str | None
    licence_url: str | None
    access_expires_on: str | None
    # none | ok | soon (within ACCESS_WARN_DAYS) | expired
    access_state: str
    access_days_left: int | None
    refresh_cadence: str | None
    triggers_rebuild: bool
    enabled: bool
    source_key_unresolved: bool
    version_count: int
    latest_version: VersionResponse | None


class SourceCreate(BaseModel):
    key: str = Field(pattern=station_store.SOURCE_KEY_RE.pattern)
    label: str = Field(min_length=1, max_length=200)
    kind: str
    format: str
    acquisition: str
    resolver_type: str | None = None
    resolver_config: dict[str, Any] | None = None
    credential_id: str | None = None
    country_iso: str | None = Field(default=None, max_length=2)
    operator: str | None = Field(default=None, max_length=120)
    licence: str | None = Field(default=None, max_length=500)
    licence_url: HttpUrl | None = None
    access_expires_on: date | None = None
    refresh_cadence: str | None = Field(default=None, max_length=120)
    triggers_rebuild: bool = False
    enabled: bool = True
    source_key_unresolved: bool = False


class SourcePatch(BaseModel):
    """Every field optional. A field that is sent is applied, `null` included:
    that is how an access expiry or a credential is cleared. The key is the
    folder name of the stored files and cannot be changed."""

    label: str | None = Field(default=None, min_length=1, max_length=200)
    kind: str | None = None
    format: str | None = None
    acquisition: str | None = None
    resolver_type: str | None = None
    resolver_config: dict[str, Any] | None = None
    credential_id: str | None = None
    country_iso: str | None = Field(default=None, max_length=2)
    operator: str | None = Field(default=None, max_length=120)
    licence: str | None = Field(default=None, max_length=500)
    licence_url: HttpUrl | None = None
    access_expires_on: date | None = None
    refresh_cadence: str | None = Field(default=None, max_length=120)
    triggers_rebuild: bool | None = None
    enabled: bool | None = None
    source_key_unresolved: bool | None = None


class BuildResponse(BaseModel):
    id: int
    started_at: str | None
    finished_at: str | None
    duration_seconds: float | None
    status: str
    builder_version: str | None
    inputs: dict[str, Any] | None
    counts: dict[str, Any] | None
    diff_summary: dict[str, Any] | None


class QueuedJob(BaseModel):
    id: str
    status: str
    created_at: str | None
    started_at: str | None


class BuildsResponse(BaseModel):
    builds: list[BuildResponse]
    # Station build jobs the worker has not finished yet.
    queued: list[QueuedJob]
    # What a build started now would be refused for. Empty: all five inputs are there.
    missing_inputs: list[str]


# ────────────────────────────── pure helpers ──────────────────────────────


def access_state(expires_on: date | None, today: date) -> tuple[str, int | None]:
    """How close a source's access grant is to its end: (state, days left).

    `expired` once the date is past, `soon` from ACCESS_WARN_DAYS before it up
    to and including the day itself, `ok` before that, `none` without a date.
    """
    if expires_on is None:
        return "none", None
    days_left = (expires_on - today).days
    if days_left < 0:
        return "expired", days_left
    if days_left <= ACCESS_WARN_DAYS:
        return "soon", days_left
    return "ok", days_left


def check_vocabulary(kind: str, fmt: str, acquisition: str, resolver_type: str | None) -> None:
    """Refuse a value no code knows. Raises ValueError with the allowed set."""
    for label, value, allowed in (
        ("kind", kind, SOURCE_KINDS),
        ("format", fmt, SOURCE_FORMATS),
        ("acquisition", acquisition, SOURCE_ACQUISITIONS),
    ):
        if value not in allowed:
            raise ValueError(f"unknown {label} {value!r}; valid: {sorted(allowed)}")
    if acquisition == "resolver":
        if resolver_type not in feed_resolvers.RESOLVER_TYPES:
            raise ValueError(
                f"acquisition 'resolver' needs a resolver_type among "
                f"{sorted(feed_resolvers.RESOLVER_TYPES)}"
            )
    elif resolver_type is not None:
        raise ValueError("resolver_type is only meaningful with acquisition 'resolver'")


def _iso(value: datetime | date | None) -> str | None:
    return value.isoformat() if value else None


def version_response(version: StationSourceVersion, source_key: str) -> VersionResponse:
    return VersionResponse(
        id=str(version.id),
        source_key=source_key,
        acquired_at=_iso(version.acquired_at),
        as_of=_iso(version.as_of),
        filename=version.filename,
        bytes=version.bytes,
        sha256=version.sha256,
        status=version.status,
        error=version.error,
        stats=version.stats,
    )


def source_response(
    source: StationSource,
    *,
    today: date,
    credential_name: str | None = None,
    version_count: int = 0,
    latest: StationSourceVersion | None = None,
) -> SourceResponse:
    state, days_left = access_state(source.access_expires_on, today)
    return SourceResponse(
        id=str(source.id),
        key=source.key,
        label=source.label,
        kind=source.kind,
        format=source.format,
        acquisition=source.acquisition,
        resolver_type=source.resolver_type,
        resolver_config=source.resolver_config,
        credential_id=str(source.credential_id) if source.credential_id else None,
        credential_name=credential_name,
        country_iso=source.country_iso,
        operator=source.operator,
        licence=source.licence,
        licence_url=source.licence_url,
        access_expires_on=_iso(source.access_expires_on),
        access_state=state,
        access_days_left=days_left,
        refresh_cadence=source.refresh_cadence,
        triggers_rebuild=source.triggers_rebuild,
        enabled=source.enabled,
        source_key_unresolved=source.source_key_unresolved,
        version_count=version_count,
        latest_version=version_response(latest, source.key) if latest else None,
    )


def build_response(build: StationBuild) -> BuildResponse:
    duration = None
    if build.started_at and build.finished_at:
        duration = (build.finished_at - build.started_at).total_seconds()
    return BuildResponse(
        id=build.id,
        started_at=_iso(build.started_at),
        finished_at=_iso(build.finished_at),
        duration_seconds=duration,
        status=build.status,
        builder_version=build.builder_version,
        inputs=build.inputs,
        counts=build.counts,
        diff_summary=build.diff_summary,
    )


def _normalise_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Shape request values the way the columns hold them."""
    out = dict(fields)
    if out.get("country_iso"):
        out["country_iso"] = str(out["country_iso"]).upper()
    elif "country_iso" in out:
        out["country_iso"] = None
    if out.get("licence_url") is not None:
        out["licence_url"] = str(out["licence_url"])
    return out


# ────────────────────────────── db helpers ──────────────────────────────


def _source_or_404(db: DbSession, key: str) -> StationSource:
    source = db.execute(select(StationSource).where(StationSource.key == key)).scalar_one_or_none()
    if source is None:
        raise HTTPException(404, _NOT_FOUND)
    return source


def _resolve_credential_id(db: DbSession, raw: str | None) -> uuid.UUID | None:
    """Validate that a credential exists. Ownership is not checked: like a NAP
    catalogue, a station source is shared infrastructure a platform admin sets up."""
    if not raw:
        return None
    try:
        credential_id = uuid.UUID(raw)
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, "credential_id is not a valid UUID") from exc
    if db.get(UserCredential, credential_id) is None:
        raise HTTPException(404, "credential_id not found")
    return credential_id


def _credential_names(db: DbSession, sources: list[StationSource]) -> dict[uuid.UUID, str]:
    ids = {s.credential_id for s in sources if s.credential_id}
    if not ids:
        return {}
    rows = db.execute(
        select(UserCredential.id, UserCredential.name).where(UserCredential.id.in_(ids))
    ).all()
    return {row[0]: row[1] for row in rows}


def _latest_versions(db: DbSession) -> dict[uuid.UUID, StationSourceVersion]:
    """The most recent version of every source, in one query (DISTINCT ON)."""
    rows = (
        db.execute(
            select(StationSourceVersion)
            .distinct(StationSourceVersion.source_id)
            .order_by(StationSourceVersion.source_id, StationSourceVersion.acquired_at.desc())
        )
        .scalars()
        .all()
    )
    return {v.source_id: v for v in rows}


def _version_counts(db: DbSession) -> dict[uuid.UUID, int]:
    rows = db.execute(
        select(StationSourceVersion.source_id, func.count()).group_by(
            StationSourceVersion.source_id
        )
    ).all()
    return {row[0]: int(row[1]) for row in rows}


def _one_source_response(db: DbSession, source: StationSource) -> SourceResponse:
    return source_response(
        source,
        today=datetime.now(UTC).date(),
        credential_name=_credential_names(db, [source]).get(source.credential_id)
        if source.credential_id
        else None,
        version_count=_version_counts(db).get(source.id, 0),
        latest=_latest_versions(db).get(source.id),
    )


def _parse_as_of(raw: str | None, filename: str) -> date | None:
    """The date the file describes: stated by the operator, else read off its name."""
    if raw is None or not raw.strip():
        return station_store.as_of_from_filename(filename)
    try:
        return date.fromisoformat(raw.strip())
    except ValueError as exc:
        raise HTTPException(400, "as_of must be a date, YYYY-MM-DD") from exc


def _existing_version(
    db: DbSession, source: StationSource, sha256: str
) -> StationSourceVersion | None:
    return db.execute(
        select(StationSourceVersion).where(
            StationSourceVersion.source_id == source.id,
            StationSourceVersion.sha256 == sha256,
        )
    ).scalar_one_or_none()


def _check_shape(source: StationSource, received: station_store.Received) -> dict[str, Any]:
    """Refuse a file that does not have the shape its source declares.

    Only the five offline shapes are checked; any other format is stored as is.
    """
    if source.format not in station_files.FILE_SHAPES:
        return {}
    try:
        header = station_files.require_shape(source.format, received.path)
    except station_files.StationFileError as exc:
        station_store.discard(received)
        raise HTTPException(400, str(exc)) from exc
    return {"columns": len(header)}


async def _receive(file: UploadFile, key: str) -> station_store.Received:
    try:
        received = await station_store.receive(
            file, key, max_bytes=settings.max_upload_mb * 1024 * 1024
        )
    except station_store.UploadTooLarge as exc:
        raise HTTPException(413, f"Upload exceeds {settings.max_upload_mb} MB") from exc
    if received.size == 0:
        station_store.discard(received)
        raise HTTPException(400, "The uploaded file is empty")
    return received


# ──────────────────────────────── sources ────────────────────────────────


@router.get("/sources", response_model=list[SourceResponse])
def list_sources(
    db: Annotated[DbSession, Depends(get_db)],
    _: Annotated[CurrentUser, Depends(require_platform_admin)],
) -> list[SourceResponse]:
    """Every source with its latest acquisition, ordered by kind then key."""
    sources = list(
        db.execute(select(StationSource).order_by(StationSource.kind, StationSource.key))
        .scalars()
        .all()
    )
    names = _credential_names(db, sources)
    latest = _latest_versions(db)
    counts = _version_counts(db)
    today = datetime.now(UTC).date()
    return [
        source_response(
            s,
            today=today,
            credential_name=names.get(s.credential_id) if s.credential_id else None,
            version_count=counts.get(s.id, 0),
            latest=latest.get(s.id),
        )
        for s in sources
    ]


@router.post(
    "/sources",
    response_model=SourceResponse,
    status_code=201,
    responses={
        400: {"description": "A kind, format, acquisition or resolver type no code knows"},
        409: {"description": "A source with that key already exists"},
    },
)
def create_source(
    payload: SourceCreate,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    actor: Annotated[CurrentUser, Depends(require_platform_admin)],
) -> SourceResponse:
    try:
        check_vocabulary(payload.kind, payload.format, payload.acquisition, payload.resolver_type)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    clash = db.execute(
        select(StationSource.id).where(StationSource.key == payload.key)
    ).scalar_one_or_none()
    if clash is not None:
        raise HTTPException(409, "A station source with that key already exists")

    fields = _normalise_fields(payload.model_dump(exclude={"credential_id"}))
    source = StationSource(
        **fields, credential_id=_resolve_credential_id(db, payload.credential_id)
    )
    db.add(source)
    db.flush()
    audit.record(
        db,
        action="station_source.created",
        actor_user_id=actor.id,
        actor_ip=client_ip(request),
        target_kind="station_source",
        target_id=source.key,
        metadata={"kind": source.kind, "format": source.format, "acquisition": source.acquisition},
    )
    db.commit()
    db.refresh(source)
    return _one_source_response(db, source)


@router.patch(
    "/sources/{key}",
    response_model=SourceResponse,
    responses={
        400: {"description": "A kind, format, acquisition or resolver type no code knows"},
        404: {"description": _NOT_FOUND},
    },
)
def patch_source(
    key: str,
    payload: SourcePatch,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    actor: Annotated[CurrentUser, Depends(require_platform_admin)],
) -> SourceResponse:
    """Edit a source. Only the fields present in the body are applied."""
    source = _source_or_404(db, key)
    sent = _normalise_fields(payload.model_dump(exclude_unset=True))
    refused = sorted(name for name in _NOT_NULLABLE if name in sent and sent[name] is None)
    if refused:
        raise HTTPException(400, f"these fields cannot be null: {', '.join(refused)}")
    if "credential_id" in sent:
        sent["credential_id"] = _resolve_credential_id(db, sent["credential_id"])
    try:
        check_vocabulary(
            sent.get("kind", source.kind),
            sent.get("format", source.format),
            sent.get("acquisition", source.acquisition),
            sent.get("resolver_type", source.resolver_type),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    changed = sorted(name for name, value in sent.items() if getattr(source, name) != value)
    for name in changed:
        setattr(source, name, sent[name])
    if changed:
        audit.record(
            db,
            action="station_source.updated",
            actor_user_id=actor.id,
            actor_ip=client_ip(request),
            target_kind="station_source",
            target_id=source.key,
            metadata={"fields": changed},
        )
    db.commit()
    db.refresh(source)
    return _one_source_response(db, source)


@router.delete(
    "/sources/{key}",
    status_code=204,
    response_model=None,
    responses={
        404: {"description": _NOT_FOUND},
        409: {"description": "The source has acquired files: disable it instead"},
    },
)
def delete_source(
    key: str,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    actor: Annotated[CurrentUser, Depends(require_platform_admin)],
) -> None:
    """Delete a source that never acquired a file. One that did is part of a
    build's input manifest and can only be disabled (the FK is RESTRICT)."""
    source = _source_or_404(db, key)
    versions = db.execute(
        select(func.count())
        .select_from(StationSourceVersion)
        .where(StationSourceVersion.source_id == source.id)
    ).scalar_one()
    if versions:
        raise HTTPException(
            409, "This source has acquired files and can only be disabled, not deleted"
        )
    audit.record(
        db,
        action="station_source.deleted",
        actor_user_id=actor.id,
        actor_ip=client_ip(request),
        target_kind="station_source",
        target_id=source.key,
        metadata={"kind": source.kind, "format": source.format},
    )
    db.delete(source)
    db.commit()


# ──────────────────────────────── versions ────────────────────────────────


@router.get(
    "/sources/{key}/versions",
    response_model=list[VersionResponse],
    responses={404: {"description": _NOT_FOUND}},
)
def list_versions(
    key: str,
    db: Annotated[DbSession, Depends(get_db)],
    _: Annotated[CurrentUser, Depends(require_platform_admin)],
    limit: int = 50,
) -> list[VersionResponse]:
    """The files acquired for one source, newest first."""
    source = _source_or_404(db, key)
    rows = (
        db.execute(
            select(StationSourceVersion)
            .where(StationSourceVersion.source_id == source.id)
            .order_by(StationSourceVersion.acquired_at.desc())
            .limit(max(1, min(limit, 500)))
        )
        .scalars()
        .all()
    )
    return [version_response(v, source.key) for v in rows]


@router.post(
    "/sources/{key}/versions",
    status_code=201,
    responses={
        200: {"description": "The same file was already uploaded: nothing written"},
        400: {"description": "Empty file, bad as_of, or a header that is not the source's shape"},
        404: {"description": _NOT_FOUND},
        409: {"description": "The source is disabled"},
        413: {"description": "The file exceeds MAX_UPLOAD_MB"},
    },
)
async def upload_version(
    key: str,
    file: Annotated[UploadFile, File()],
    request: Request,
    response: Response,
    db: Annotated[DbSession, Depends(get_db)],
    actor: Annotated[CurrentUser, Depends(require_platform_admin)],
    as_of: Annotated[str | None, Form()] = None,
) -> UploadResult:
    """Upload one file of a station source.

    Re-uploading an identical file is a no-op: `(source_id, sha256)` is unique,
    the existing version is returned with `created: false` and status 200.
    """
    source = _source_or_404(db, key)
    if not source.enabled:
        raise HTTPException(409, "This station source is disabled")
    filename = station_store.safe_filename(file.filename)
    as_of_date = _parse_as_of(as_of, filename)

    received = await _receive(file, source.key)
    existing = _existing_version(db, source, received.sha256)
    if existing is not None:
        station_store.discard(received)
        response.status_code = 200
        return UploadResult(
            created=False, version=version_response(existing, source.key), build=_UNCHANGED
        )

    stats = _check_shape(source, received)
    stored = station_store.keep(received, source.key, filename)
    version = StationSourceVersion(
        source_id=source.id,
        as_of=as_of_date,
        filename=filename,
        bytes=received.size,
        sha256=received.sha256,
        status="uploaded",
        stats=stats or None,
        stored_path=str(stored),
        uploaded_by=actor.id,
    )
    db.add(version)
    try:
        db.flush()
    except IntegrityError:
        # Two uploads of the same file raced; the other one won.
        db.rollback()
        stored.unlink(missing_ok=True)
        winner = _existing_version(db, source, received.sha256)
        if winner is None:  # pragma: no cover  defensive
            raise
        response.status_code = 200
        return UploadResult(
            created=False, version=version_response(winner, source.key), build=_UNCHANGED
        )

    audit.record(
        db,
        action="station_source.version.uploaded",
        actor_user_id=actor.id,
        actor_ip=client_ip(request),
        target_kind="station_source",
        target_id=source.key,
        metadata={
            "version_id": str(version.id),
            "filename": filename,
            "bytes": received.size,
            "sha256": received.sha256,
        },
    )
    db.commit()
    db.refresh(version)
    result = version_response(version, source.key)
    return UploadResult(created=True, version=result, build=_queue_build_for(db, source))


# ──────────────────────────────── builds ────────────────────────────────


def _queue_build_for(db: DbSession, source: StationSource) -> BuildDecision:
    """A new version of a source that triggers a rebuild queues one — once all
    five inputs are there. Until then the upload says what is still missing,
    rather than queueing a build that could only be refused."""
    if not source.triggers_rebuild:
        return BuildDecision(queued=False, note="this source does not trigger a rebuild")
    _, problems = station_import.find_inputs(db)
    if problems:
        return BuildDecision(queued=False, note="no build queued yet: " + "; ".join(problems))
    created = station_import.enqueue_build(db, f"a new version of {source.key}")
    return BuildDecision(queued=True, note=_QUEUED if created else _ALREADY_QUEUED)


@router.post(
    "/builds",
    response_model=BuildDecision,
    status_code=202,
    responses={
        409: {"description": "A build needs all five inputs; the detail lists what is missing"}
    },
)
def queue_build(
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    actor: Annotated[CurrentUser, Depends(require_platform_admin)],
) -> BuildDecision:
    """Queue a station build from the files already uploaded.

    Needed because re-uploading an identical file is a no-op: without this, a
    build that failed for a reason outside the files could not be run again.
    The worker starts it once the rebuild debounce has elapsed.
    """
    _, problems = station_import.find_inputs(db)
    if problems:
        raise HTTPException(409, "A build needs all five inputs: " + "; ".join(problems))
    created = station_import.enqueue_build(db, f"requested by {actor.username}")
    audit.record(
        db,
        action="station_build.requested",
        actor_user_id=actor.id,
        actor_ip=client_ip(request),
        target_kind="station_build",
        metadata={"coalesced": not created},
    )
    db.commit()
    return BuildDecision(queued=True, note=_QUEUED if created else _ALREADY_QUEUED)


@router.get("/builds", response_model=BuildsResponse)
def list_builds(
    db: Annotated[DbSession, Depends(get_db)],
    _: Annotated[CurrentUser, Depends(require_platform_admin)],
    limit: int = 20,
) -> BuildsResponse:
    """Build history, newest first, with each build's inputs and diff summary,
    the station build jobs the worker has not finished yet, and what a build
    started now would still be missing."""
    problems = station_import.find_inputs(db)[1]
    builds = (
        db.execute(
            select(StationBuild).order_by(StationBuild.id.desc()).limit(max(1, min(limit, 200)))
        )
        .scalars()
        .all()
    )
    jobs = (
        db.execute(
            select(RebuildJob)
            .where(
                RebuildJob.kind == STATION_BUILD_KIND,
                RebuildJob.status.in_(("pending", "running")),
            )
            .order_by(RebuildJob.created_at.asc())
        )
        .scalars()
        .all()
    )
    return BuildsResponse(
        builds=[build_response(b) for b in builds],
        missing_inputs=problems,
        queued=[
            QueuedJob(
                id=str(j.id),
                status=j.status,
                created_at=_iso(j.created_at),
                started_at=_iso(j.started_at),
            )
            for j in jobs
        ],
    )
