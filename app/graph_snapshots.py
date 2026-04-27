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
from datetime import date, datetime, timezone
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
    last = (
        db.execute(
            select(GraphSnapshot)
            .where(GraphSnapshot.session_id == session_id)
            .where(GraphSnapshot.timetable_main_version == main_version)
            .order_by(GraphSnapshot.timetable_update_version.desc())
            .limit(1)
        )
        .scalar_one_or_none()
    )
    return (last.timetable_update_version + 1) if last else 1


def record_snapshot(
    db: DbSession,
    *,
    session_id: str,
    rebuild_job_id: Any | None,
    graph_path: Path,
    source_uploads: list[Upload],
    main_version: str | None = None,
    service_period_start: date | None = None,
    service_period_end: date | None = None,
    main_version_source: str = "auto",
    set_current: bool = True,
) -> GraphSnapshot:
    """Persist a graph_snapshots row for a successful build."""
    sigs = [u.sha256 for u in source_uploads]
    sig = feed_signature(sigs)

    if main_version is None or service_period_start is None or service_period_end is None:
        # Auto-derive from the first GTFS upload, if any.
        gtfs = next((u for u in source_uploads if u.detected_kind == "GTFS"), None)
        if gtfs is None:
            today = date.today()
            main_version = main_version or iso_week_range(today, today)
            service_period_start = service_period_start or today
            service_period_end = service_period_end or today
        else:
            mv, sps, spe = derive_main_version_from_gtfs(Path(gtfs.stored_path))
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
        built_at=datetime.now(timezone.utc),
        graph_path=str(graph_path),
        source_uploads=[
            {
                "upload_id": str(u.id),
                "filename": u.filename,
                "sha256": u.sha256,
                "kind": u.detected_kind,
            }
            for u in source_uploads
        ],
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
