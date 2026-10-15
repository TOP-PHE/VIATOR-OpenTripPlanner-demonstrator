"""Coverage hubs — the station module's VIATOR id (MSMM contract, phase 2).

Adds one nullable column to `network_coverage_hubs`:

- `station_id` (`String(8)`): the VIATOR id of the hub's station in the
  station module (its design 22.5: the ISO country at creation, a hyphen,
  five digits, such as `ZZ-99999`). Null = none known: the hub works as
  before. Kept beside the hub's `uic`, never in it: a VIATOR id is not a
  station code.

and one CHECK: null, or exactly that form.

The origin and the licence of the hub's station are not stored: VIATOR reads
them live from the module (decision D-R11 A). No foreign key, nothing
written, nothing called: every existing hub starts without an id, and gets
one when an administrator confirms its code, or when "Check station codes"
finds that the module serves the hub's code under an id. The downgrade drops
the column and its CHECK, and with them the ids stored since; the hubs and
their codes stay.

Revision ID: 20261015_1200_hub_station_id
Revises: 20261010_1200_edit_archive
Create Date: 2026-10-15 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261015_1200_hub_station_id"
down_revision: str | Sequence[str] | None = "20261010_1200_edit_archive"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Written out here, not imported from the model: a revision must keep the
# schema it created even if the model changes later.
_CHECK = "station_id IS NULL OR station_id ~ '^[A-Z]{2}-[0-9]{5}$'"


def upgrade() -> None:
    op.add_column(
        "network_coverage_hubs", sa.Column("station_id", sa.String(length=8), nullable=True)
    )
    op.create_check_constraint(
        op.f("ck_network_coverage_hubs_station_id_valid"), "network_coverage_hubs", _CHECK
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_network_coverage_hubs_station_id_valid"), "network_coverage_hubs", type_="check"
    )
    op.drop_column("network_coverage_hubs", "station_id")
