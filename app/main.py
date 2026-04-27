from __future__ import annotations

import hashlib
import secrets
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import detect, ingestion
from .db import SessionLocal, Upload, RebuildJob, init_db
from .settings import settings


app = FastAPI(title="VIATOR — feed ingestion")
templates = Jinja2Templates(directory="app/templates")
basic = HTTPBasic()

# Brand assets (TrackOnPath logo, UIC logo, VIATOR icons) live in ./branding
# at the repo root and are copied into the container by the Dockerfile.
# Mounted at /static/branding so the UI can <img src="/static/branding/uic-logo.svg">.
_branding_dir = Path("branding")
if _branding_dir.is_dir():
    app.mount("/static/branding", StaticFiles(directory=_branding_dir), name="branding")


@app.on_event("startup")
def _startup() -> None:
    settings.inbox_dir.mkdir(parents=True, exist_ok=True)
    init_db()


def authed(creds: Annotated[HTTPBasicCredentials, Depends(basic)]) -> str:
    ok_user = secrets.compare_digest(creds.username, settings.admin_user)
    ok_pwd = secrets.compare_digest(creds.password, settings.admin_password)
    if not (ok_user and ok_pwd):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return creds.username


@app.get("/", response_class=HTMLResponse)
def index(request: Request, user: Annotated[str, Depends(authed)]) -> HTMLResponse:
    with SessionLocal() as db:
        uploads = db.query(Upload).order_by(Upload.created_at.desc()).limit(20).all()
        jobs = db.query(RebuildJob).order_by(RebuildJob.created_at.desc()).limit(10).all()
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "user": user,
            "uploads": uploads,
            "jobs": jobs,
            "kinds": sorted(detect.KNOWN_KINDS),
        },
    )


@app.post("/upload")
async def upload(
    user: Annotated[str, Depends(authed)],
    declared_standard: Annotated[str, Form()],
    version_label: Annotated[str, Form()] = "",
    file: UploadFile = File(...),
) -> RedirectResponse:
    if declared_standard not in detect.KNOWN_KINDS:
        raise HTTPException(400, f"Unknown standard: {declared_standard}")

    # Persist to a per-upload folder so concurrent uploads don't collide.
    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    staging = settings.inbox_dir / "_staging" / f"{stamp}-{secrets.token_hex(4)}"
    staging.mkdir(parents=True, exist_ok=True)
    stored_path = staging / Path(file.filename or "upload.bin").name

    sha = hashlib.sha256()
    size = 0
    max_bytes = settings.max_upload_mb * 1024 * 1024
    with stored_path.open("wb") as out:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > max_bytes:
                out.close()
                stored_path.unlink(missing_ok=True)
                raise HTTPException(413, f"Upload exceeds {settings.max_upload_mb} MB")
            sha.update(chunk)
            out.write(chunk)

    try:
        detected_kind = detect.detect(stored_path)
    except ValueError as exc:
        raise HTTPException(400, f"Detection failed: {exc}")

    if detected_kind != declared_standard:
        raise HTTPException(
            400,
            f"Declared {declared_standard!r} but file looks like {detected_kind!r} — refusing.",
        )

    with SessionLocal() as db:
        triggered = ingestion.dispatch(stored_path, detected_kind, db)
        record = Upload(
            user=user,
            filename=stored_path.name,
            declared_standard=declared_standard,
            detected_kind=detected_kind,
            sha256=sha.hexdigest(),
            size_bytes=size,
            stored_path=str(stored_path),
            version_label=version_label,
            triggered_rebuild=int(triggered),
        )
        db.add(record)
        db.commit()

    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
