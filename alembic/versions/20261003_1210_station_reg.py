"""Station panel, part 2 of 4: the register tables behind screen B.

`crd_location`, `crd_subsidiary` and `era_operational_point` hold one set of
rows per acquired source version, so the delta an upload produces is a set
difference between two `source_version_id`s. Rows go when their version does
(ON DELETE CASCADE).

See docs/station-panel-design.md section 3.2.

Revision ID: 20261003_1210_station_reg
Revises: 20261003_1200_station_sources
Create Date: 2026-10-03 12:10:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20261003_1210_station_reg"
down_revision: str | Sequence[str] | None = "20261003_1200_station_sources"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KEY = sa.Text(collation="C")


def _version_fk(table: str) -> sa.Column[object]:
    # Named without the referred table: the conventional name would exceed
    # Postgres' 63-character identifier limit on era_operational_point.
    return sa.Column(
        "source_version_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey(
            "station_source_version.id",
            ondelete="CASCADE",
            name=f"fk_{table}_source_version_id",
        ),
        nullable=False,
    )


def upgrade() -> None:
    op.create_table(
        "crd_location",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        _version_fk("crd_location"),
        sa.Column("country", _KEY),
        sa.Column("location_code", _KEY),
        sa.Column("plc", _KEY, nullable=False),
        # Kept as published: the offline extractor's date format is not ours to guess.
        sa.Column("start_validity", sa.Text()),
        sa.Column("end_validity", sa.Text()),
        sa.Column("active_flag", sa.Text()),
        sa.Column("name", sa.Text()),
        sa.Column("free_text", sa.Text()),
        sa.Column("lat", sa.Float()),
        sa.Column("lon", sa.Float()),
        sa.Column("passenger_flag", sa.Text()),
        sa.Column("freight_flag", sa.Text()),
        sa.Column("responsible_im", sa.Text()),
        sa.Column("nuts", sa.Text()),
        # CRD's own key is (country, code, validity).
        sa.UniqueConstraint(
            "source_version_id",
            "country",
            "location_code",
            "start_validity",
            name="uq_crd_location_version_key",
        ),
    )
    op.create_index(
        "ix_crd_location_source_version_id_plc", "crd_location", ["source_version_id", "plc"]
    )

    op.create_table(
        "crd_subsidiary",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        _version_fk("crd_subsidiary"),
        sa.Column("plc", _KEY, nullable=False),
        sa.Column("subsidiary_type", sa.Text(), nullable=False),
        sa.Column("allocation_company", sa.Text()),
        sa.Column("code", _KEY, nullable=False),
        sa.Column("name", sa.Text()),
    )
    op.create_index(
        "ix_crd_subsidiary_source_version_id_plc", "crd_subsidiary", ["source_version_id", "plc"]
    )

    op.create_table(
        "era_operational_point",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        _version_fk("era_operational_point"),
        sa.Column("plc", _KEY, nullable=False),
        sa.Column("uopid", _KEY, nullable=False),
        sa.Column("name", sa.Text()),
        sa.Column("op_type", sa.Text()),
        sa.Column("iso2", sa.Text()),
        sa.Column("lat", sa.Float()),
        sa.Column("lon", sa.Float()),
        sa.Column("rl100", sa.Text()),
        sa.UniqueConstraint(
            "source_version_id",
            "plc",
            "uopid",
            name="uq_era_operational_point_source_version_id_plc_uopid",
        ),
    )


def downgrade() -> None:
    op.drop_table("era_operational_point")
    op.drop_index("ix_crd_subsidiary_source_version_id_plc", table_name="crd_subsidiary")
    op.drop_table("crd_subsidiary")
    op.drop_index("ix_crd_location_source_version_id_plc", table_name="crd_location")
    op.drop_table("crd_location")
