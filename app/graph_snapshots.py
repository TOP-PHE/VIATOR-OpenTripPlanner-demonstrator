"""Graph snapshot recorder — records one row per successful OTP build.

Two-level versioning:
  timetable_main_version   ISO-week range derived from the feed's calendar
  timetable_update_version sequential within (session_id, main_version)

Replay/version-diff endpoints enforce same-main-version comparability.
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from .models import GraphSnapshot, Upload

log = logging.getLogger(__name__)


# ──────────────────────────── version derivation ────────────────────────────


def iso_week_range(start: date, end: date) -> str:
    """Encode `(start_date, end_date)` as `YYYY-Www_YYYY-Www`."""
    sy, sw, _ = start.isocalendar()
    ey, ew, _ = end.isocalendar()
    return f"{sy:04d}-W{sw:02d}_{ey:04d}-W{ew:02d}"


def derive_main_version_from_gtfs(gtfs_zip_path: Path) -> tuple[str, date, date]:
    """Read calendar.txt + calendar_dates.txt to find min/max service date.

    Returns (main_version_string, service_period_start, service_period_end).
    Falls back to today's-week if the zip is malformed.
    """
    starts: list[date] = []
    ends: list[date] = []
    try:
        with zipfile.ZipFile(gtfs_zip_path) as z:
            names = {n.lower(): n for n in z.namelist()}
            cal_name = names.get("calendar.txt")
            if cal_name:
                with z.open(cal_name) as f:
                    text = f.read().decode("utf-8", errors="replace")
                for row in csv.DictReader(io.StringIO(text)):
                    try:
                        starts.append(_parse_yyyymmdd(row["start_date"]))
                        ends.append(_parse_yyyymmdd(row["end_date"]))
                    except (KeyError, ValueError):
                        continue
            cd_name = names.get("calendar_dates.txt")
            if cd_name:
                with z.open(cd_name) as f:
                    text = f.read().decode("utf-8", errors="replace")
                for row in csv.DictReader(io.StringIO(text)):
                    try:
                        d = _parse_yyyymmdd(row["date"])
                        starts.append(d)
                        ends.append(d)
                    except (KeyError, ValueError):
                        continue
    except (zipfile.BadZipFile, OSError) as exc:
        log.warning("could not read GTFS calendar from %s: %s", gtfs_zip_path, exc)

    if not starts or not ends:
        today = date.today()
        return iso_week_range(today, today), today, today
    s, e = min(starts), max(ends)
    return iso_week_range(s, e), s, e


def _parse_yyyymmdd(s: str) -> date:
    s = s.strip()
    return date(int(s[0:4]), int(s[4:6]), int(s[6:8]))


# ──────────────────────────── snapshot recording ────────────────────────────


def feed_signature(upload_sha256s: list[str]) -> str:
    """Deterministic identity from input hashes — same inputs → same feed_signature."""
    h = hashlib.sha256()
    for s in sorted(upload_sha256s):
        h.update(s.encode())
    return h.hexdigest()


def next_update_version(db: DbSession, *, session_id: str, main_version: str) -> int:
    last = db.execute(
        select(GraphSnapshot)
        .where(GraphSnapshot.session_id == session_id)
        .where(GraphSnapshot.timetable_main_version == main_version)
        .order_by(GraphSnapshot.timetable_update_version.desc())
        .limit(1)
    ).scalar_one_or_none()
    return (last.timetable_update_version + 1) if last else 1


# ──────────────────────── inbox enumeration (v0.1.23) ────────────────────────


def _sha256_file(path: Path, *, chunk_bytes: int = 1024 * 1024) -> str:
    """Streamed SHA-256 — handles 4 GB OSM PBFs without loading them in RAM."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            data = fh.read(chunk_bytes)
            if not data:
                break
            h.update(data)
    return h.hexdigest()


def enumerate_session_inputs(
    db: DbSession,
    session_id: str,
    inbox_root: Path,
) -> list[dict[str, Any]]:
    """Walk the session inbox and build a complete `source_uploads` list.

    Why this exists: pre-v0.1.23, `record_snapshot()` only saw the `Upload`
    table — which is populated by manual uploads only. Refresh-from-URL
    drops files into the inbox without writing Upload rows, so a session
    populated entirely via the Refresh providers button (the common case
    for NAP-imported sessions) ended up with an empty `source_uploads`
    list and the v0.1.20 rebuild card couldn't show its inputs.

    This helper closes that gap by scanning the inbox subdirs (gtfs/,
    netex/, osm/) directly, computing SHA-256 of each file, and cross-
    referencing with the `Upload` table by sha256 so manual-upload rows
    are still surfaced (with `source: "uploaded"`). Refresh-fetched
    files get `source: "refreshed"` and `upload_id: None`.

    Returns dicts in the JSONB shape `record_snapshot` writes — the
    additional `size_bytes`, `source`, and `stored_path` keys are
    tolerated by Postgres JSONB and surfaced in the UI's expanded inputs
    list. Older snapshot rows from pre-v0.1.23 still load cleanly because
    the renderer treats unknown keys as optional.
    """
    inputs: list[dict[str, Any]] = []
    seen_sha: set[str] = set()  # dedupe — same content uploaded + refreshed

    # The kind label here is the canonical `detected.KNOWN_KINDS` string.
    # We don't try to distinguish NeTEx-Nordic from NeTEx-EPIP via filename
    # (both live in netex/ and the build-config generator picks the right
    # `transitFeeds.type` per file). For the snapshot inventory it's enough
    # to know a feed is NeTEx-shaped.
    subdirs: list[tuple[str, str, tuple[str, ...]]] = [
        ("gtfs", "GTFS", (".zip",)),
        ("netex", "NeTEx", (".zip",)),
        ("osm", "OSM-PBF", (".pbf",)),
    ]

    for subdir_name, kind_label, extensions in subdirs:
        subdir = inbox_root / subdir_name
        if not subdir.is_dir():
            continue
        for f in sorted(subdir.iterdir()):
            if not f.is_file():
                continue
            # Skip rotated backups (osm.pbf.old.1 etc) — they're history,
            # not current inputs to this build. The rotation lives at
            # app/api/admin/sessions.py::_rotate_osm_pbf.
            if ".old." in f.name:
                continue
            if not any(f.name.endswith(ext) for ext in extensions):
                continue

            try:
                sha = _sha256_file(f)
            except OSError as exc:
                log.warning(
                    "inbox enumeration: could not sha %s: %s — skipping", f, exc
                )
                continue
            if sha in seen_sha:
                # Same content under a different filename. Don't double-count;
                # the build read the same bytes either way.
                continue
            seen_sha.add(sha)

            upload = db.execute(
                select(Upload)
                .where(Upload.sha256 == sha)
                .where(Upload.session_id == session_id)
                .limit(1)
            ).scalar_one_or_none()

            inputs.append(
                {
                    "upload_id": str(upload.id) if upload is not None else None,
                    "filename": f.name,
                    "sha256": sha,
                    "kind": kind_label,
                    "size_bytes": f.stat().st_size,
                    "source": "uploaded" if upload is not None else "refreshed",
                    "stored_path": str(f),
                }
            )

    return inputs


def record_snapshot(
    db: DbSession,
    *,
    session_id: str,
    rebuild_job_id: Any | None,
    graph_path: Path,
    source_uploads: list[Upload] | None = None,
    source_inputs: list[dict[str, Any]] | None = None,
    main_version: str | None = None,
    service_period_start: date | None = None,
    service_period_end: date | None = None,
    main_version_source: str = "auto",
    set_current: bool = True,
) -> GraphSnapshot:
    """Persist a graph_snapshots row for a successful build.

    Two ways to provide the input descriptors:
      * `source_uploads`: a list of `Upload` ORM rows (legacy v0.1.20 path —
        complete only for sessions where every input was manually uploaded).
      * `source_inputs`: a list of pre-built dicts in the JSONB shape — the
        v0.1.23 path, populated by `enumerate_session_inputs()` from the
        inbox so refresh-fetched files are also captured.

    Pass exactly one. Passing neither yields an empty inputs list (defensive;
    we'd rather record the build with no inputs than crash here).
    """
    if source_inputs is not None and source_uploads is not None:
        raise ValueError(
            "record_snapshot: pass either source_uploads OR source_inputs, not both"
        )

    # Normalise to the JSONB-shaped dict list regardless of input style.
    if source_inputs is not None:
        inputs_json: list[dict[str, Any]] = list(source_inputs)
    elif source_uploads is not None:
        inputs_json = [
            {
                "upload_id": str(u.id),
                "filename": u.filename,
                "sha256": u.sha256,
                "kind": u.detected_kind,
            }
            for u in source_uploads
        ]
    else:
        inputs_json = []

    sigs = [d["sha256"] for d in inputs_json if d.get("sha256")]
    sig = feed_signature(sigs)

    if main_version is None or service_period_start is None or service_period_end is None:
        # Auto-derive from the first GTFS file. We need the on-disk path
        # to read the calendar; both source_uploads (Upload.stored_path)
        # and source_inputs (dict["stored_path"] from inbox enumeration)
        # carry one.
        gtfs_path: Path | None = None
        for d in inputs_json:
            if d.get("kind") == "GTFS" and d.get("stored_path"):
                gtfs_path = Path(d["stored_path"])
                break
        # Legacy fallback for callers passing source_uploads (Upload ORM
        # has stored_path but the dict above doesn't surface it for ORM
        # path because Upload.stored_path was already in the projection).
        if gtfs_path is None and source_uploads:
            gtfs_upload = next(
                (u for u in source_uploads if u.detected_kind == "GTFS"), None
            )
            if gtfs_upload is not None:
                gtfs_path = Path(gtfs_upload.stored_path)

        if gtfs_path is None or not gtfs_path.is_file():
            today = date.today()
            main_version = main_version or iso_week_range(today, today)
            service_period_start = service_period_start or today
            service_period_end = service_period_end or today
        else:
            mv, sps, spe = derive_main_version_from_gtfs(gtfs_path)
            main_version = main_version or mv
            service_period_start = service_period_start or sps
            service_period_end = service_period_end or spe

    if set_current:
        # Demote any existing 'current' snapshot for this session.
        for prior in (
            db.execute(
                select(GraphSnapshot)
                .where(GraphSnapshot.session_id == session_id)
                .where(GraphSnapshot.is_current.is_(True))
            )
            .scalars()
            .all()
        ):
            prior.is_current = False

    snap = GraphSnapshot(
        session_id=session_id,
        rebuild_job_id=rebuild_job_id,
        built_at=datetime.now(UTC),
        graph_path=str(graph_path),
        source_uploads=inputs_json,
        feed_signature=sig,
        timetable_main_version=main_version,
        timetable_update_version=next_update_version(
            db, session_id=session_id, main_version=main_version
        ),
        service_period_start=service_period_start,
        service_period_end=service_period_end,
        main_version_source=main_version_source,
        is_current=set_current,
    )
    db.add(snap)
    db.flush()
    return snap
