"""Station sources: how every station input is acquired (screen E).

    POST /api/admin/stations/sources/{key}/versions   upload one file of a source

Authorization: platform_admin only, declared on every route. There is no
router-level dependency in this codebase, so a route has no auth unless its
own signature says so.

Why a dedicated upload route: both existing ones refuse a station file. They
require a `declared_standard` in `detect.KNOWN_KINDS`, and `detect` accepts a
CSV only if it looks like SNCF stations or MCT. This route never calls
`detect.detect` nor `ingestion.dispatch`: a station file belongs to no session
and stages into no engine inbox. It is streamed to `inbox/_stations/<key>/`
while its sha256 is computed, and recorded as a `station_source_version`.

See docs/station-panel-design.md section 4E and section 11.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from ... import audit
from ...db import get_db
from ...master import station_files, station_store
from ...models import StationSource, StationSourceVersion
from ...security import CurrentUser, client_ip, require_platform_admin
from ...settings import settings

router = APIRouter(prefix="/api/admin/stations", tags=["admin", "stations"])


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


class UploadResult(BaseModel):
    # False when the same file was already there: nothing was written.
    created: bool
    version: VersionResponse


def version_response(version: StationSourceVersion, source_key: str) -> VersionResponse:
    return VersionResponse(
        id=str(version.id),
        source_key=source_key,
        acquired_at=version.acquired_at.isoformat() if version.acquired_at else None,
        as_of=version.as_of.isoformat() if version.as_of else None,
        filename=version.filename,
        bytes=version.bytes,
        sha256=version.sha256,
        status=version.status,
        error=version.error,
        stats=version.stats,
    )


# ────────────────────────────── helpers ──────────────────────────────


def _source_or_404(db: DbSession, key: str) -> StationSource:
    source = db.execute(select(StationSource).where(StationSource.key == key)).scalar_one_or_none()
    if source is None:
        raise HTTPException(404, "Station source not found")
    return source


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


# ──────────────────────────────── routes ────────────────────────────────


@router.post(
    "/sources/{key}/versions",
    status_code=201,
    responses={
        200: {"description": "The same file was already uploaded: nothing written"},
        400: {"description": "Empty file, bad as_of, or a header that is not the source's shape"},
        404: {"description": "No such station source"},
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
        return UploadResult(created=False, version=version_response(existing, source.key))

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
        return UploadResult(created=False, version=version_response(winner, source.key))

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
    return UploadResult(created=True, version=version_response(version, source.key))
