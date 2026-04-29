"""Per-session ingestion dispatcher.

Routes uploaded files into the right per-session inbox subtree and decides
whether an OTP rebuild is needed for that session.

Layout:
  /data/inbox/<session_id>/{gtfs,osm,netex,archive,runtime,_staging}/

Multi-feed GTFS:
  Sessions can have multiple GTFS feeds (SNCF + IDFM + Trenitalia, etc.).
  Each feed lands at `inbox/<sid>/gtfs/<feed_id_lower>.zip`. The OTP entrypoint
  scans `gtfs/*.zip` at build time and generates a `build-config.json` with
  one transitFeeds entry per file (feedId = filename stem, uppercased). See
  docker/otp/entrypoint.sh.

  Single-feed sessions (legacy and the common case) stage at the default
  `gtfs.zip` filename, which the build-config generator turns into a
  feedId of `GTFS` — same routing behaviour as before.
"""

from __future__ import annotations

import re
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

# Canonical default filename per kind. For GTFS, multi-feed sessions override
# this with a per-feed name (`<feed_id_lower>.zip`) — see `dispatch(...,
# staged_filename=)`. The entrypoint reads zips by glob from `gtfs/*.zip`
# regardless of name, and the build-config generator derives feedId from the
# stem.
STAGE_INTO_OTP_INBOX_FILENAME: dict[str, str] = {
    "GTFS": "gtfs.zip",
    "NeTEx-Nordic": "netex.zip",
    "NeTEx-EPIP": "netex.zip",
    "OSM-PBF": "osm.pbf",
}

# Feed IDs become OTP feedId namespaces on stop_ids (e.g. `SNCF:OCETrain-…`).
# OTP doesn't enforce a strict format but we keep ours alphanumeric-uppercase
# to avoid surprises with stop-id parsing in the journey UI. Filenames are
# the lowercased form (case-insensitive filesystems on macOS/Windows would
# otherwise collide on rename).
_FEED_ID_RE = re.compile(r"^[A-Z][A-Z0-9_-]{1,15}$")

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


def normalize_gtfs_sources(raw: object) -> list[dict[str, str]]:
    """Coerce `config.sources.gtfs` into a canonical multi-feed list.

    Accepts:
      - None / "" → []
      - str (legacy single URL) → [{"id": "GTFS", "url": <url>}]
      - list of {"id": str, "url": str} → returned as-is after validation

    Raises ValueError if the shape is wrong or any feed_id fails the regex.
    The "GTFS" default for the legacy single-string form keeps the OTP
    feedId stable across the migration: a graph that previously had a
    single feed with feedId="DEFAULT" (from the old hardcoded build-config)
    now has a single feed with feedId="GTFS" — small visible change in
    stop_id prefixes, but consistent with the new naming rule.
    """
    if raw is None or raw == "":
        return []
    if isinstance(raw, str):
        return [{"id": "GTFS", "url": raw}]
    if not isinstance(raw, list):
        raise ValueError(
            f"config.sources.gtfs must be a string or list, got {type(raw).__name__}"
        )
    out: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValueError(f"feeds[{i}] must be an object with 'id' and 'url'")
        feed_id = (entry.get("id") or "").strip()
        url = (entry.get("url") or "").strip()
        if not feed_id or not _FEED_ID_RE.match(feed_id):
            raise ValueError(
                f"feeds[{i}].id={feed_id!r} must match /^[A-Z][A-Z0-9_-]{{1,15}}$/ "
                "(e.g. SNCF, IDFM, TRENITALIA, FR-SNCF)"
            )
        if feed_id in seen_ids:
            raise ValueError(f"feed id {feed_id!r} appears twice")
        seen_ids.add(feed_id)
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"feeds[{i}].url={url!r} must be an http(s) URL")
        out.append({"id": feed_id, "url": url})
    return out


def gtfs_staged_filename(feed_id: str) -> str:
    """Canonical inbox filename for one GTFS feed in a multi-feed session.

    Lowercased so case-insensitive filesystems don't collide. The OTP
    entrypoint's build-config generator re-uppercases the stem to recover
    the operator-facing feedId.
    """
    return f"{feed_id.lower()}.zip"


def dispatch(
    stored_path: Path,
    kind: str,
    db: DbSession,
    *,
    session_id: str | None = None,
    staged_filename: str | None = None,
) -> bool:
    """Move the stored file to its destination. Returns True if a rebuild was queued.

    `staged_filename` overrides the per-kind default (used by multi-feed GTFS
    to land each feed at `inbox/<sid>/gtfs/<feed_id_lower>.zip`). Other GTFS-
    family files (manual upload, single-source refresh) keep the canonical
    `gtfs.zip` name. For non-GTFS kinds the parameter is ignored.

    Rotation policy: when `staged_filename` is None (legacy single-feed flow)
    we rotate every existing non-`.old` file in the subdir to `.old` first —
    same as before. When a per-feed filename is given (multi-feed flow) we
    rotate ONLY the matching feed's prior file, so the other feeds in the
    same session stay live. This is the difference between "I'm replacing
    the only GTFS we have" and "I'm refreshing one of N feeds".
    """
    base = session_inbox(session_id)

    if kind in STAGE_INTO_OTP_INBOX:
        subdir = base / STAGE_INTO_OTP_INBOX[kind]
        subdir.mkdir(parents=True, exist_ok=True)
        target_name = staged_filename or STAGE_INTO_OTP_INBOX_FILENAME[kind]
        if staged_filename is None:
            # Legacy: rotate everything (single-feed sessions).
            for existing in subdir.iterdir():
                if existing.is_file() and not existing.name.endswith(".old"):
                    existing.rename(existing.with_suffix(existing.suffix + ".old"))
        else:
            # Multi-feed: rotate only the matching feed's prior file.
            existing_target = subdir / target_name
            if existing_target.exists():
                existing_target.rename(
                    existing_target.with_suffix(existing_target.suffix + ".old")
                )
        target = subdir / target_name
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
