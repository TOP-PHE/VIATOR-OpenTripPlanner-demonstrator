"""Routes a stored upload to the right inbox subfolder and decides whether
to enqueue an OTP rebuild."""
from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from .db import RebuildJob
from .settings import settings


# Which detected kinds get staged into OTP's build inbox vs. stored elsewhere.
STAGE_INTO_OTP_INBOX: dict[str, str] = {
    "GTFS": "gtfs",
    "NeTEx-Nordic": "netex",
    "NeTEx-EPIP": "netex",
    "OSM-PBF": "osm",
}

# Stored but does NOT trigger an OTP rebuild (Phase 1 — see strategy doc).
ARCHIVE_ONLY: set[str] = {
    "NeTEx-FR-Horaires",
    "NeTEx-FR-Arrets",
}

# Loaded into the database for runtime consumption by the OJP adapter.
LOAD_TO_DB: set[str] = {
    "SNCF-MCT",
    "SNCF-Stations",
}


def dispatch(stored_path: Path, kind: str, db: Session) -> bool:
    """Move the stored file to its destination. Returns True if a rebuild was queued."""

    if kind in STAGE_INTO_OTP_INBOX:
        subdir = settings.inbox_dir / STAGE_INTO_OTP_INBOX[kind]
        subdir.mkdir(parents=True, exist_ok=True)
        # In each subdir we keep only the latest artifact per kind. Old ones get an .old suffix.
        for existing in subdir.iterdir():
            if existing.is_file() and not existing.name.endswith(".old"):
                existing.rename(existing.with_suffix(existing.suffix + ".old"))
        target = subdir / stored_path.name
        shutil.copy2(stored_path, target)
        _enqueue_rebuild(db, reason=f"new {kind} uploaded")
        return True

    if kind in ARCHIVE_ONLY:
        archive = settings.inbox_dir / "archive" / kind
        archive.mkdir(parents=True, exist_ok=True)
        shutil.copy2(stored_path, archive / stored_path.name)
        return False

    if kind in LOAD_TO_DB:
        # The OJP adapter (Phase 2) reads directly from these files; we keep
        # the latest copy in /data/inbox/runtime/<kind>/.
        runtime = settings.inbox_dir / "runtime" / kind
        runtime.mkdir(parents=True, exist_ok=True)
        target = runtime / f"latest{stored_path.suffix}"
        # write atomically
        tmp = target.with_suffix(target.suffix + ".tmp")
        shutil.copy2(stored_path, tmp)
        tmp.replace(target)
        return False

    raise ValueError(f"No dispatch rule for kind={kind}")


def _enqueue_rebuild(db: Session, reason: str) -> None:
    # Coalesce: if a pending job already exists, do nothing.
    pending = db.query(RebuildJob).filter(RebuildJob.status == "pending").first()
    if pending is not None:
        return
    job = RebuildJob(status="pending", log=f"queued at {datetime.utcnow().isoformat()} — {reason}\n")
    db.add(job)
    db.commit()
