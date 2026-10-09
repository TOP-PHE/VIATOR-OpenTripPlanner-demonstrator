"""Station edits end in VIATOR: archive them, then hand the rows back to the import.

MSMM step 3 (decisions 52, 53, 55): the station module is the reference for
a station, and station edits are made only there. VIATOR's own edit route and
drift queue are removed in the same release, so a row an operator edited
(`source = 'manual'`) would otherwise stay frozen for ever, with no screen to
see or release it.

This revision, in the one transaction of `alembic upgrade`:

1. creates `master_stations_edit_archive`: `uic` (text, primary key),
   `station` (JSONB: every column of the `master_stations` row as it stood),
   `drift_snapshot` / `drift_fields` / `drift_detected_at` (the pending drift
   row, when there was one), `archived_at` (default now);
2. copies into it every `master_stations` row whose `source` is `manual`,
   with its pending drift row when there is one, and every station that has
   a pending drift row whatever its `source`;
3. only then sets `source = 'trainline'` on the archived `manual` rows and
   deletes the archived drift rows: their content is in the archive;
4. prints the number of rows archived (VIATOR's own rows), so that the
   administrator reading the web container's start log knows whether there is
   anything to read.

The next Trainline import (04:00 UTC, or "Refresh from Trainline" on the
Stations page) then gives these rows Trainline's values again. A row whose
`uic` Trainline has since dropped keeps its edited values, now marked
`trainline`: the import never deletes a row. The archive keeps every one;
nothing removes it. The runbook's read-only `psql` line lists them.

The downgrade writes the archived rows back (every column, `source = 'manual'`
included), recreates the drift rows from the archive and drops the table. It
does not undo what an import wrote in between, which the archive's values
replace.

The drift table, its model and the import's protection of `manual` rows stay,
empty and dormant: no code can make a row `manual` any more.

Revision ID: 20261010_1200_edit_archive
Revises: 20261009_1200_hub_uic
Create Date: 2026-10-10 12:00:00.000000
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20261010_1200_edit_archive"
down_revision: str | Sequence[str] | None = "20261009_1200_hub_uic"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ARCHIVE = "master_stations_edit_archive"

# The SQL is written out, never assembled: a revision must keep working if the
# model changes later, and no value of a row ever enters a statement's text.

# 2. Every edited row and every row with a pending drift, as they stand.
_COPY = """
INSERT INTO master_stations_edit_archive
    (uic, station, drift_snapshot, drift_fields, drift_detected_at)
SELECT s.uic, to_jsonb(s), d.trainline_snapshot, d.fields_differing, d.detected_at
FROM master_stations AS s
LEFT JOIN master_stations_pending_drift AS d ON d.uic = s.uic
WHERE s.source = 'manual' OR d.uic IS NOT NULL
"""

# 3. Only rows already in the archive are handed back or deleted.
_FLIP = """
UPDATE master_stations SET source = 'trainline'
WHERE source = 'manual' AND uic IN (SELECT uic FROM master_stations_edit_archive)
"""
_DELETE_DRIFT = """
DELETE FROM master_stations_pending_drift
WHERE uic IN (SELECT uic FROM master_stations_edit_archive)
"""

_COUNT = "SELECT count(*) FROM master_stations_edit_archive"

# Downgrade: every column of the initial schema back from the archive. A
# station deleted since is inserted again (no code deletes one), which also
# keeps the key of its drift row valid.
_RESTORE = """
INSERT INTO master_stations (
    uic, uic8_sncf, name, slug, country_iso, latitude, longitude,
    parent_uic, is_main_station, is_suggestable, trigramme_sncf, db_code, trenitalia_code,
    renfe_code, atoc_code, other_codes, name_translations, source, updated_at
)
SELECT
    a.uic, r.uic8_sncf, r.name, r.slug, r.country_iso, r.latitude, r.longitude,
    r.parent_uic, r.is_main_station, r.is_suggestable, r.trigramme_sncf, r.db_code, r.trenitalia_code,
    r.renfe_code, r.atoc_code, r.other_codes, r.name_translations, r.source, r.updated_at
FROM master_stations_edit_archive AS a
CROSS JOIN LATERAL jsonb_populate_record(NULL::master_stations, a.station) AS r
ON CONFLICT (uic) DO UPDATE SET
    uic8_sncf = EXCLUDED.uic8_sncf,
    name = EXCLUDED.name,
    slug = EXCLUDED.slug,
    country_iso = EXCLUDED.country_iso,
    latitude = EXCLUDED.latitude,
    longitude = EXCLUDED.longitude,
    parent_uic = EXCLUDED.parent_uic,
    is_main_station = EXCLUDED.is_main_station,
    is_suggestable = EXCLUDED.is_suggestable,
    trigramme_sncf = EXCLUDED.trigramme_sncf,
    db_code = EXCLUDED.db_code,
    trenitalia_code = EXCLUDED.trenitalia_code,
    renfe_code = EXCLUDED.renfe_code,
    atoc_code = EXCLUDED.atoc_code,
    other_codes = EXCLUDED.other_codes,
    name_translations = EXCLUDED.name_translations,
    source = EXCLUDED.source,
    updated_at = EXCLUDED.updated_at
"""
_RESTORE_DRIFT = """
INSERT INTO master_stations_pending_drift (uic, trainline_snapshot, fields_differing, detected_at)
SELECT uic, drift_snapshot, drift_fields, drift_detected_at
FROM master_stations_edit_archive
WHERE drift_snapshot IS NOT NULL
ON CONFLICT (uic) DO UPDATE SET
    trainline_snapshot = EXCLUDED.trainline_snapshot,
    fields_differing = EXCLUDED.fields_differing,
    detected_at = EXCLUDED.detected_at
"""


def upgrade() -> None:
    op.create_table(
        ARCHIVE,
        sa.Column("uic", sa.String(), primary_key=True),
        sa.Column("station", postgresql.JSONB(), nullable=False),
        sa.Column("drift_snapshot", postgresql.JSONB(), nullable=True),
        sa.Column("drift_fields", postgresql.ARRAY(sa.String()), nullable=True),
        sa.Column("drift_detected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "archived_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    bind = op.get_bind()
    bind.execute(sa.text(_COPY))
    bind.execute(sa.text(_FLIP))
    bind.execute(sa.text(_DELETE_DRIFT))
    archived = bind.execute(sa.text(_COUNT)).scalar_one()
    # Printed, not logged: alembic.ini's logging setup disables loggers made
    # before it, and this line must reach the web container's start log.
    print(
        f"station edit archive: {archived} station row(s) archived in {ARCHIVE}; "
        "if not 0, read them with the psql line of docs/admin-guide.md section 11.1",
        file=sys.stderr,
        flush=True,
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text(_RESTORE))
    bind.execute(sa.text(_RESTORE_DRIFT))
    op.drop_table(ARCHIVE)
