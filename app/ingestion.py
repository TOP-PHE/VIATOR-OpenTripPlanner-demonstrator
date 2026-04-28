"""Per-session ingestion dispatcher.

Routes uploaded files into the right per-session inbox subtree and decides
whether an OTP rebuild is needed for that session.

Layout:
  /data/inbox/<session_id>/{gtfs,osm,netex,archive,runtime,_staging}/
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session as DbSession

from .models import RebuildJob
from .settings import settings

# Which detected kinds get staged into OTP's build inbox vs. stored elsewhere.
STAGE_INTO_OTP_INBOX: dict[str, str] = {
    "GTFS": "gtfs",
    "NeTEx-Nordic": "netex",
    "NeTEx-EPIP": "netex",
    "OSM-PBF": "osm",
}

# Canonical filename per kind so `docker/otp/build-config.json` can reference
# stable paths regardless of the original upload/download filename. The
# entrypoint's compgen still picks them up via the `*.zip` / `*.pbf` glob.
# Multi-feed sessions are not in scope (would need numbered filenames).
STAGE_INTO_OTP_INBOX_FILENAME: dict[str, str] = {
    "GTFS": "gtfs.zip",
    "NeTEx-Nordic": "netex.zip",
    "NeTEx-EPIP": "netex.zip",
    "OSM-PBF": "osm.pbf",
}

# Stored but does NOT trigger an OTP rebuild (Phase 6 — see strategy doc).
ARCHIVE_ONLY: set[str] = {
    "NeTEx-FR-Horaires",
    "NeTEx-FR-Arrets",
}

# Loaded into the database / staged for the OJP adapter (Phase 5).
LOAD_TO_DB: set[str] = {
    "SNCF-MCT",
    "SNCF-Stations",
}


def session_inbox(session_id: str | None) -> Path:
    """Return the per-session inbox dir, falling back to a `_phase1` bucket
    while the upload UI still lacks a session selector."""
    sid = session_id or "_phase1"
    return settings.inbox_dir / sid


def dispatch(stored_path: Path, kind: str, db: DbSession, *, session_id: str | None = None) -> bool:
    """Move the stored file to its destination. Returns True if a rebuild was queued."""
    base = session_inbox(session_id)

    if kind in STAGE_INTO_OTP_INBOX:
        subdir = base / STAGE_INTO_OTP_INBOX[kind]
        subdir.mkdir(parents=True, exist_ok=True)
        for existing in subdir.iterdir():
            if existing.is_file() and not existing.name.endswith(".old"):
                existing.rename(existing.with_suffix(existing.suffix + ".old"))
        # Use a canonical per-kind filename so build-config.json can reference
        # `gtfs.zip` / `osm.pbf` instead of the upload's accidental name.
        target = subdir / STAGE_INTO_OTP_INBOX_FILENAME[kind]
        shutil.copy2(stored_path, target)
        _enqueue_rebuild(db, session_id=session_id, reason=f"new {kind} uploaded")
        return True

    if kind in ARCHIVE_ONLY:
        archive = base / "archive" / kind
        archive.mkdir(parents=True, exist_ok=True)
        shutil.copy2(stored_path, archive / stored_path.name)
        return False

    if kind in LOAD_TO_DB:
        runtime = base / "runtime" / kind
        runtime.mkdir(parents=True, exist_ok=True)
        target = runtime / f"latest{stored_path.suffix}"
        tmp = target.with_suffix(target.suffix + ".tmp")
        shutil.copy2(stored_path, tmp)
        tmp.replace(target)
        return False

    raise ValueError(f"No dispatch rule for kind={kind}")


def _enqueue_rebuild(db: DbSession, *, session_id: str | None, reason: str) -> None:
    """Coalesce: skip if a pending job for the same session already exists."""
    pending = (
        db.query(RebuildJob)
        .filter(RebuildJob.status == "pending")
        .filter(RebuildJob.session_id == session_id)
        .first()
    )
    if pending is not None:
        return
    job = RebuildJob(
        session_id=session_id,
        status="pending",
        log=f"queued at {datetime.now(UTC).isoformat()} — {reason}\n",
    )
    db.add(job)
    db.commit()
