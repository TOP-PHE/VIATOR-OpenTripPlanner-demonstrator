"""Trainline-eu/stations CSV bootstrap + monthly refresh.

CSV is semicolon-delimited UTF-8. Source:
  https://github.com/trainline-eu/stations/blob/master/stations.csv

Conflict resolution: rows with `source='manual'` are NEVER overwritten by a
refresh — instead, the upstream snapshot is recorded in the
`master_stations_pending_drift` table and surfaced in the admin UI.

Two ID spaces in the upstream CSV (frequent source of bugs):

  - `id`              Trainline's internal sequential integer (1, 2, …, ~5000).
                      Used internally by Trainline; not meaningful outside.
  - `uic`             Official UIC code (7-8 digits, e.g. 8775123). Globally
                      unique. This is master_stations.uic, our PK.
  - `parent_station_id`  Points at the parent row's *Trainline `id`*, NOT
                         its UIC. Translation requires a trainline_id → uic
                         lookup built from the same CSV.

We load parent relationships in two passes (to avoid FK-violation cascades
when a child row is inserted before its parent):

  Pass 1 — INSERT/UPDATE all rows with parent_uic=NULL.
  Pass 2 — UPDATE each row to set parent_uic, where the parent has a UIC
           AND we didn't skip the row in pass 1 (e.g. source='manual').
"""

from __future__ import annotations

import csv
import io
import logging
from typing import Any, cast

import httpx
from sqlalchemy import CursorResult, select, update
from sqlalchemy.orm import Session as DbSession

from ..models import MasterStation, MasterStationPendingDrift

log = logging.getLogger(__name__)

TRAINLINE_CSV_URL = "https://raw.githubusercontent.com/trainline-eu/stations/master/stations.csv"


# Trainline column → MasterStation attribute. Excludes `parent_station_id`
# because that's in Trainline's id-space, not UIC-space — handled separately
# via the trainline_id → uic translation in parse_csv().
_COL_MAP = {
    "uic": "uic",
    "uic8_sncf": "uic8_sncf",
    "name": "name",
    "slug": "slug",
    "country": "country_iso",
    "latitude": "latitude",
    "longitude": "longitude",
    "is_main_station": "is_main_station",
    "is_suggestable": "is_suggestable",
    "sncf_id": "trigramme_sncf",
    "db_id": "db_code",
    "trenitalia_id": "trenitalia_code",
    "renfe_id": "renfe_code",
    "atoc_id": "atoc_code",
}

# Trainline columns that map into `master_stations.other_codes` JSONB
# instead of dedicated columns. Adding a new operator's identifier here
# is the painless path — no DB migration required, just an entry in this
# dict + a Trainline refresh. The UI reads `other_codes` and renders
# whichever keys are populated.
#
# Why JSONB and not new columns? Trainline tracks 14+ operator-specific
# IDs and grows over time (Westbahn was added in 2019, Trenord later). If
# every new operator needed an alembic migration the schema would never
# stabilise. Operators that are queried frequently in the UI (SNCF / DB /
# Trenitalia / Renfe / ATOC) keep dedicated columns; everything else lives
# in this JSONB and is rendered dynamically.
#
# Map keys are the Trainline CSV column names; values are the canonical
# JSONB key we use internally (lowercased, terse).
_OTHER_CODES_COL_MAP = {
    "obb_id": "obb",  # ÖBB / Austrian Federal Railways
    "cff_id": "sbb",  # SBB / CFF / FFS — Switzerland (Trainline calls it cff_id)
    "entur_id": "entur",  # Entur — Norway / Nordic transit hub
    "ntv_id": "ntv",  # NTV / Italo — Italian private high-speed
    "trenord_id": "trenord",  # Trenord — Lombardy regional
    "cercanias_id": "cercanias",  # Renfe Cercanías — Spanish regional
    "benerail_id": "benerail",  # Benerail — Belgium booking
    "westbahn_id": "westbahn",  # Westbahn — Austrian private
    "flixbus_id": "flixbus",  # Flixbus
    "busbud_id": "busbud",  # Busbud
    "distribusion_id": "distribusion",  # Distribusion
    "iata_airport_code": "iata",  # IATA airport code (when station is in/at an airport)
}


def parse_csv(content: str) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Parse the Trainline CSV into:

    - list of master_stations-shaped row dicts (without parent_uic — set in
      pass 2 of the upsert)
    - mapping of `uic → parent_uic`, for every row whose parent itself
      has a UIC. Self-references (uic == parent_uic) are dropped.
    """
    reader = csv.DictReader(io.StringIO(content), delimiter=";")
    raw_rows = list(reader)

    # Build trainline_id → uic for the parent translation. Only rows that
    # have BOTH an id and a uic can act as parents (we can't link to a
    # parent that we won't be inserting).
    id_to_uic: dict[str, str] = {
        (raw["id"] or "").strip(): (raw["uic"] or "").strip()
        for raw in raw_rows
        if (raw.get("id") or "").strip() and (raw.get("uic") or "").strip()
    }

    rows: list[dict[str, Any]] = []
    parent_uic_map: dict[str, str] = {}

    for raw in raw_rows:
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

        # Operator-specific identifiers that don't have dedicated columns
        # (OBB, SBB, NTV, Trenord, Cercanías, Entur, etc.) — stored in the
        # `other_codes` JSONB so the UI can render them dynamically. Only
        # populated when at least one upstream value exists; otherwise we
        # leave the column at its server_default of `'{}'::jsonb`.
        other_codes: dict[str, str] = {}
        for src, key in _OTHER_CODES_COL_MAP.items():
            v = (raw.get(src) or "").strip()
            if v:
                other_codes[key] = v
        if other_codes:
            row["other_codes"] = other_codes

        # Translate parent_station_id (Trainline id) → parent_uic (UIC code).
        # Skip if the parent doesn't have a UIC, or if the row points at itself.
        parent_tid = (raw.get("parent_station_id") or "").strip()
        if parent_tid and parent_tid in id_to_uic:
            parent_uic = id_to_uic[parent_tid]
            if parent_uic and parent_uic != uic:
                parent_uic_map[uic] = parent_uic

        # Multilingual names (best-effort)
        translations = {}
        for code in ("fr", "en", "de", "it", "es", "nl"):
            v = (raw.get(f"info:{code}") or raw.get(f"name:{code}") or "").strip()
            if v:
                translations[code] = v
        if translations:
            row["name_translations"] = translations
        rows.append(row)
    return rows, parent_uic_map


def upsert_with_drift_protection(
    db: DbSession,
    parsed: list[dict[str, Any]],
    parent_uic_map: dict[str, str] | None = None,
) -> dict[str, int]:
    """Apply parsed rows to master_stations.

    - source='manual' rows: NEVER overwrite. Capture upstream diff in
      master_stations_pending_drift instead.
    - Other rows: upsert.
    - parent_uic is populated in a second pass, AFTER all rows exist, to
      avoid FK-violation cascades when a child row is inserted before
      its parent. Manual rows preserve their existing parent_uic — the
      parent-pass `WHERE source != 'manual'` keeps operator edits.

    Returns a counts dict:
      {added, updated, skipped_manual, pending_drift, parent_links_set}
    """
    counts = {
        "added": 0,
        "updated": 0,
        "skipped_manual": 0,
        "pending_drift": 0,
        "parent_links_set": 0,
    }
    parent_uic_map = parent_uic_map or {}

    # ────────────────────────── Pass 1 — rows ──────────────────────────
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

    # ────────────────────── Pass 2 — parent_uic links ──────────────────────
    # Now that every row exists, we can safely set parent_uic without
    # tripping the FK. We only target rows whose source != 'manual', so
    # manual-rebuilt parent relationships survive a Trainline refresh.
    if parent_uic_map:
        live_uics = {row[0] for row in db.execute(select(MasterStation.uic)).all()}
        for child_uic, parent_uic in parent_uic_map.items():
            if child_uic not in live_uics or parent_uic not in live_uics:
                continue  # parent isn't in our table (no UIC, or skipped)
            # See app/retention.py for the rationale on the CursorResult cast
            # (audit-2026-05 #23 — sqlalchemy 2.0.49+ tightened Result typing).
            result = cast(  # type: ignore[redundant-cast,unused-ignore]
                CursorResult[Any],
                db.execute(
                    update(MasterStation)
                    .where(MasterStation.uic == child_uic)
                    .where(MasterStation.source != "manual")
                    .values(parent_uic=parent_uic)
                ),
            )
            if result.rowcount > 0:
                counts["parent_links_set"] += 1
        db.commit()

    return counts


def _diff_fields(existing: MasterStation, incoming: dict[str, Any]) -> list[str]:
    """Field-by-field diff between an existing manual row and the upstream
    incoming row. Used to populate `master_stations_pending_drift.fields_differing`.

    `other_codes` is decomposed into per-key entries so the drift queue tells
    the operator *which* operator code changed (e.g. `other_codes.obb`)
    rather than just listing the JSONB column wholesale.
    """
    diff: list[str] = []
    for key, value in incoming.items():
        if key == "source":
            continue
        if key == "other_codes":
            existing_codes = getattr(existing, "other_codes", None) or {}
            incoming_codes = value or {}
            for code_key in set(existing_codes) | set(incoming_codes):
                if existing_codes.get(code_key) != incoming_codes.get(code_key):
                    diff.append(f"other_codes.{code_key}")
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
    parsed, parent_uic_map = parse_csv(content)
    log.info(
        "Trainline CSV: parsed %d stations, %d parent links to resolve",
        len(parsed),
        len(parent_uic_map),
    )
    return upsert_with_drift_protection(db, parsed, parent_uic_map)
