"""The places ÖBB's HAFAS planner answered, kept for the station module's stop list (its design
22.20, the owner's decisions 69 to 73 of 2026-11-13).

Creates two tables, both empty (the worker fills them from the coverage runs already stored, then
from each new run: `app/network_coverage/hafas_places.py`):

- `hafas_places`, one row per HAFAS place of type station at which a train of ÖBB's answers starts
  or ends a leg: `ext_id` (text, primary key, digits only), `name`, `lat`, `lon` (as HAFAS last
  gave them), `uic` (text, null; the UIC code its `gidL` states, seven digits), `times_seen`
  (integer, at least 1), `first_seen_at`, `last_seen_at` (timestamptz), `operators` (text[],
  default empty): the operators of the legs it was seen on;
- `hafas_place_runs`, one row per coverage run added: `run_id` (uuid, primary key, foreign key to
  `network_coverage_runs.id`, ON DELETE CASCADE), `collected_at` (timestamptz, default now()),
  `calls` and `places` (integers): a run is added once.

Nothing else changes and nothing is copied. Re-runnable: each table is created only when it is
missing. The downgrade drops both and loses only the list (the next upgrade rebuilds it from the
runs still stored; runs deleted meanwhile are no longer in it).

Revision ID: 20261116_1200_hafas_places
Revises: 20261105_1200_alignment_detail
Create Date: 2026-11-16 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20261116_1200_hafas_places"
down_revision: str | Sequence[str] | None = "20261105_1200_alignment_detail"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PLACES = "hafas_places"
RUNS = "hafas_place_runs"
# The same checks as app/models/hafas_places.py, written out: a revision must keep working if the
# model changes later.
EXT_ID_CHECK = "ext_id ~ '^[0-9]{1,15}$'"
UIC_CHECK = "uic IS NULL OR uic ~ '^[0-9]{7}$'"
POSITION_CHECK = "lat BETWEEN -90 AND 90 AND lon BETWEEN -180 AND 180"


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(PLACES):
        op.create_table(
            PLACES,
            sa.Column("ext_id", sa.String(15), nullable=False),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("lat", sa.Float(), nullable=False),
            sa.Column("lon", sa.Float(), nullable=False),
            sa.Column("uic", sa.String(7), nullable=True),
            sa.Column("times_seen", sa.Integer(), nullable=False),
            sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column(
                "operators",
                postgresql.ARRAY(sa.String(200)),
                nullable=False,
                server_default=sa.text("'{}'"),
            ),
            sa.CheckConstraint(EXT_ID_CHECK, name="ck_hafas_places_ext_id_digits"),
            sa.CheckConstraint(UIC_CHECK, name="ck_hafas_places_uic_digits"),
            sa.CheckConstraint("times_seen >= 1", name="ck_hafas_places_times_seen_positive"),
            sa.CheckConstraint(POSITION_CHECK, name="ck_hafas_places_position"),
            sa.PrimaryKeyConstraint("ext_id", name="pk_hafas_places"),
        )
    if not inspector.has_table(RUNS):
        op.create_table(
            RUNS,
            sa.Column(
                "run_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey(
                    "network_coverage_runs.id",
                    ondelete="CASCADE",
                    name="fk_hafas_place_runs_run_id_network_coverage_runs",
                ),
                nullable=False,
            ),
            sa.Column(
                "collected_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column("calls", sa.Integer(), nullable=False),
            sa.Column("places", sa.Integer(), nullable=False),
            sa.PrimaryKeyConstraint("run_id", name="pk_hafas_place_runs"),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for table in (RUNS, PLACES):
        if inspector.has_table(table):
            op.drop_table(table)
