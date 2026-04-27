"""Trainline-eu/stations CSV bootstrap + monthly refresh.

CSV is semicolon-delimited UTF-8. Source:
  https://github.com/trainline-eu/stations/blob/master/stations.csv

Conflict resolution: rows with `source='manual'` are NEVER overwritten by a
refresh — instead, the upstream snapshot is recorded in the
`master_stations_pending_drift` table and surfaced in the admin UI.
"""

from __future__ import annotations

import csv
import io
import logging
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..models import MasterStation, MasterStationPendingDrift

log = logging.getLogger(__name__)

TRAINLINE_CSV_URL = "https://raw.githubusercontent.com/trainline-eu/stations/master/stations.csv"


# Trainline column → MasterStation attribute.
_COL_MAP = {
    "uic": "uic",
    "uic8_sncf": "uic8_sncf",
    "name": "name",
    "slug": "slug",
    "country": "country_iso",
    "latitude": "latitude",
    "longitude": "longitude",
    "parent_station_id": "parent_uic",
    "is_main_station": "is_main_station",
    "is_suggestable": "is_suggestable",
    "sncf_id": "trigramme_sncf",
    "db_id": "db_code",
    "trenitalia_id": "trenitalia_code",
    "renfe_id": "renfe_code",
    "atoc_id": "atoc_code",
}


def parse_csv(content: str) -> list[dict[str, Any]]:
    """Parse the Trainline CSV into a list of master_stations-shaped dicts."""
    reader = csv.DictReader(io.StringIO(content), delimiter=";")
    rows: list[dict[str, Any]] = []
    for raw in reader:
        uic = (raw.get("uic") or "").strip()
        if not uic:
            continue  # Trainline has rows without UIC — we skip them
        row: dict[str, Any] = {"source": "trainline"}
        for src, dst in _COL_MAP.items():
            v = (raw.get(src) or "").strip()
            if not v:
                continue
            if dst in ("latitude", "longitude"):
                try:
                    row[dst] = float(v)
                except ValueError:
                    continue
            elif dst in ("is_main_station", "is_suggestable"):
                row[dst] = v.lower() in ("t", "true", "1", "yes")
            else:
                row[dst] = v
        # Multilingual names (best-effort)
        translations = {}
        for code in ("fr", "en", "de", "it", "es", "nl"):
            v = (raw.get(f"info:{code}") or raw.get(f"name:{code}") or "").strip()
            if v:
                translations[code] = v
        if translations:
            row["name_translations"] = translations
        rows.append(row)
    return rows


def upsert_with_drift_protection(db: DbSession, parsed: list[dict[str, Any]]) -> dict[str, int]:
    """Apply parsed rows to master_stations.

    - source='manual' rows: NEVER overwrite. Capture upstream diff in
      master_stations_pending_drift instead.
    - Other rows: upsert.

    Returns a counts dict: {added, updated, skipped_manual, pending_drift}.
    """
    counts = {"added": 0, "updated": 0, "skipped_manual": 0, "pending_drift": 0}

    existing_by_uic = {row.uic: row for row in db.execute(select(MasterStation)).scalars().all()}

    for row in parsed:
        uic = row["uic"]
        existing = existing_by_uic.get(uic)
        if existing is None:
            db.add(MasterStation(**row))
            counts["added"] += 1
            continue
        if existing.source == "manual":
            differing = _diff_fields(existing, row)
            if differing:
                drift = db.get(MasterStationPendingDrift, uic)
                if drift is None:
                    db.add(
                        MasterStationPendingDrift(
                            uic=uic,
                            trainline_snapshot=row,
                            fields_differing=differing,
                        )
                    )
                else:
                    drift.trainline_snapshot = row
                    drift.fields_differing = differing
                counts["pending_drift"] += 1
            counts["skipped_manual"] += 1
            continue
        # Plain upstream-managed row: just update.
        for k, v in row.items():
            setattr(existing, k, v)
        counts["updated"] += 1

    db.commit()
    return counts


def _diff_fields(existing: MasterStation, incoming: dict[str, Any]) -> list[str]:
    diff = []
    for key, value in incoming.items():
        if key in ("source",):
            continue
        if getattr(existing, key, None) != value:
            diff.append(key)
    return diff


async def fetch_csv() -> str:
    """Pull the latest stations.csv from GitHub. Raises httpx.HTTPError on failure."""
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.get(TRAINLINE_CSV_URL)
        r.raise_for_status()
        return r.text


async def refresh(db: DbSession) -> dict[str, int]:
    """Fetch + parse + upsert. Used by the admin "refresh Trainline" button + cron."""
    content = await fetch_csv()
    parsed = parse_csv(content)
    log.info("Trainline CSV: parsed %d stations", len(parsed))
    return upsert_with_drift_protection(db, parsed)
