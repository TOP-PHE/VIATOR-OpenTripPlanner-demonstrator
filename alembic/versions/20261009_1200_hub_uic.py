"""Coverage hubs — a station code from the station module (MSMM step 3).

Adds two nullable columns to `network_coverage_hubs`:

- `uic` (`String(20)`): the station's code, the module's MERITS code when it
  came from the module. Null = not resolved: the hub works as before, by
  position.
- `uic_origin` (`String(8)`): `msmm` when an administrator confirmed it from
  a module answer, `manual` when an administrator typed it.

and one CHECK: both null, or a code of 3 to 20 characters (the station
module's length rule) with an origin of `msmm` or `manual`.

No foreign key to `master_stations`: a MERITS code need not be a Trainline
`uic`. The migration writes nothing else and calls nothing: every existing
hub starts unresolved, and is resolved afterwards from the manage-hubs panel
(propose, then confirm). The downgrade drops the two columns, and with them
the codes typed or confirmed since.

Revision ID: 20261009_1200_hub_uic
Revises: 20261002_2100_rebuild_cancel
Create Date: 2026-10-09 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261009_1200_hub_uic"
down_revision: str | Sequence[str] | None = "20261002_2100_rebuild_cancel"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Written out here, not imported from the model: a revision must keep the
# schema it created even if the model changes later. `uic_origin IS NOT NULL`
# is spelled out: `NULL IN (...)` is NULL, which a CHECK lets through.
_CHECK = (
    "(uic IS NULL AND uic_origin IS NULL) OR "
    "(uic IS NOT NULL AND char_length(uic) BETWEEN 3 AND 20 "
    "AND uic_origin IS NOT NULL AND uic_origin IN ('msmm','manual'))"
)


def upgrade() -> None:
    op.add_column("network_coverage_hubs", sa.Column("uic", sa.String(length=20), nullable=True))
    op.add_column(
        "network_coverage_hubs", sa.Column("uic_origin", sa.String(length=8), nullable=True)
    )
    op.create_check_constraint(
        op.f("ck_network_coverage_hubs_uic_origin_valid"), "network_coverage_hubs", _CHECK
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_network_coverage_hubs_uic_origin_valid"), "network_coverage_hubs", type_="check"
    )
    op.drop_column("network_coverage_hubs", "uic_origin")
    op.drop_column("network_coverage_hubs", "uic")
