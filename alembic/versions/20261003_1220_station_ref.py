"""Station panel, part 3 of 4: the VIATOR station reference and its children.

`station_ref` is keyed on `(plc, era_uopid)`: a PLC can carry several
operational points, so a key on the PLC alone would silently collapse rows.
`era_uopid` is NOT NULL because a unique index treats NULLs as distinct. A row
is current state; what a build changed goes to `station_ref_history`.

Two things differ from a literal reading of the design, both forced by
Postgres:

  * `is_current` is a plain boolean set by the build. A generated column (and
    a partial-index predicate) must be immutable, and CURRENT_DATE is not.
  * the trigram index for alternative names is on `alt_name_text`, a text
    column the build maintains. pg_trgm cannot index a `text[]`, and
    `array_to_string` is not immutable either.

See docs/station-panel-design.md section 3.3 and 3.4.

Revision ID: 20261003_1220_station_ref
Revises: 20261003_1210_station_reg
Create Date: 2026-10-03 12:20:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20261003_1220_station_ref"
down_revision: str | Sequence[str] | None = "20261003_1210_station_reg"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KEY = sa.Text(collation="C")
_FALSE = sa.text("FALSE")
_TRUE = sa.text("TRUE")
_TEXT_ARRAY = postgresql.ARRAY(sa.Text())


def _pk() -> sa.Column[int]:
    return sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True)


def _station_fk(table: str, *, ondelete: str = "CASCADE") -> sa.Column[int]:
    return sa.Column(
        "station_id",
        sa.BigInteger(),
        sa.ForeignKey(
            "station_ref.id", ondelete=ondelete, name=f"fk_{table}_station_id_station_ref"
        ),
        nullable=False,
    )


def _build_fk(table: str, column: str, *, ondelete: str, nullable: bool) -> sa.Column[int]:
    return sa.Column(
        column,
        sa.BigInteger(),
        sa.ForeignKey(
            "station_build.id", ondelete=ondelete, name=f"fk_{table}_{column}_station_build"
        ),
        nullable=nullable,
    )


def _create_station_ref() -> None:
    op.create_table(
        "station_ref",
        _pk(),
        # Identity
        sa.Column("plc", _KEY, nullable=False),
        sa.Column("era_uopid", _KEY, nullable=False),
        sa.Column("previous_plc", _KEY),
        # Names
        sa.Column("name", sa.Text()),
        sa.Column("name_src", sa.Text()),
        sa.Column("alt_name", _TEXT_ARRAY),
        sa.Column("alt_name_text", sa.Text()),
        # Position
        sa.Column("lat", sa.Float()),
        sa.Column("lon", sa.Float()),
        sa.Column("pos_src", sa.Text()),
        sa.Column("link_pos_src", sa.Text()),
        sa.Column("position_flag", sa.Text()),
        # Classification
        sa.Column("iso2", sa.Text()),
        sa.Column("iso2_all", _TEXT_ARRAY),
        sa.Column("op_type_all", _TEXT_ARRAY),
        sa.Column("op_type_src", sa.Text()),
        sa.Column("is_passenger", sa.Boolean()),
        sa.Column("is_passenger_src", sa.Text()),
        sa.Column("plc_kind", sa.Text()),
        # Multiplicity
        sa.Column("n_op_with_plc", sa.Integer()),
        sa.Column("plc_op_max_sep_m", sa.Integer()),
        # Spine
        sa.Column("spine_source", sa.Text()),
        sa.Column("crd_start", sa.Date()),
        sa.Column("crd_end", sa.Date()),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=_TRUE),
        sa.Column("crd_source_tag", sa.Text()),
        # MERITS (the chosen candidate, mirrored for filtering)
        sa.Column("uic_merits", _KEY),
        sa.Column("uic_merits_origin", sa.Text()),
        sa.Column("uic_merits_rule", sa.Text()),
        sa.Column("uic_merits_confidence", sa.Text()),
        # Other codes
        sa.Column("rl100", sa.Text()),
        sa.Column("nat_code", sa.Text()),
        sa.Column("nat_code_series", sa.Text()),
        sa.Column("nat_code_src", sa.Text()),
        sa.Column("ifopt_dhid", sa.Text()),
        sa.Column("ifopt_dhid_src", sa.Text()),
        sa.Column("eva", sa.Text()),
        sa.Column("eva_src", sa.Text()),
        sa.Column("eva_all", _TEXT_ARRAY),
        # Grouping
        sa.Column(
            "complex_id",
            sa.BigInteger(),
            sa.ForeignKey(
                "station_complex.id",
                ondelete="SET NULL",
                name="fk_station_ref_complex_id_station_complex",
            ),
        ),
        sa.Column("complex_role", sa.Text()),
        # Quality
        sa.Column("warning_level", sa.Text()),
        sa.Column("best_tier", sa.Text()),
        sa.Column("n_nap_feeds", sa.Integer()),
        # Lineage
        _build_fk("station_ref", "first_seen_build_id", ondelete="RESTRICT", nullable=True),
        _build_fk("station_ref", "last_built_build_id", ondelete="RESTRICT", nullable=True),
        _build_fk("station_ref", "last_changed_build_id", ondelete="RESTRICT", nullable=True),
        sa.UniqueConstraint("plc", "era_uopid", name="uq_station_ref_plc_era_uopid"),
        # op.f(): the name is final. Without it Alembic applies the "ck" naming
        # convention a second time (ck_station_ref_ck_station_ref_...).
        sa.CheckConstraint("length(plc) = 7", name=op.f("ck_station_ref_plc_length")),
        sa.CheckConstraint(
            "complex_role IS NULL OR complex_role IN ('principal','member')",
            name=op.f("ck_station_ref_complex_role_valid"),
        ),
    )
    op.create_index("ix_station_ref_iso2_is_passenger", "station_ref", ["iso2", "is_passenger"])
    op.create_index("ix_station_ref_uic_merits", "station_ref", ["uic_merits"])
    op.create_index("ix_station_ref_previous_plc", "station_ref", ["previous_plc"])
    op.create_index("ix_station_ref_complex_id", "station_ref", ["complex_id"])
    op.create_index("ix_station_ref_iso2_all", "station_ref", ["iso2_all"], postgresql_using="gin")
    op.create_index(
        "ix_station_ref_name_trgm",
        "station_ref",
        ["name"],
        postgresql_using="gin",
        postgresql_ops={"name": "gin_trgm_ops"},
    )
    op.create_index(
        "ix_station_ref_alt_name_trgm",
        "station_ref",
        ["alt_name_text"],
        postgresql_using="gin",
        postgresql_ops={"alt_name_text": "gin_trgm_ops"},
    )
    # What step 7 puts behind the journey typeahead: routable rows only.
    op.create_index(
        "ix_station_ref_typeahead",
        "station_ref",
        ["name"],
        postgresql_using="gin",
        postgresql_ops={"name": "gin_trgm_ops"},
        postgresql_where=sa.text("is_passenger AND uic_merits IS NOT NULL"),
    )
    op.create_index(
        "ix_station_ref_current", "station_ref", ["plc"], postgresql_where=sa.text("is_current")
    )


def _create_codes_and_merits() -> None:
    op.create_table(
        "station_ref_code",
        _pk(),
        _station_fk("station_ref_code"),
        sa.Column("source_key", _KEY, nullable=False),
        sa.Column(
            "series",
            _KEY,
            sa.ForeignKey(
                "station_code_series.key", name="fk_station_ref_code_series_station_code_series"
            ),
        ),
        sa.Column("code", _KEY, nullable=False),
        sa.Column("code_raw", sa.Text()),
        sa.Column("normalisation_rule", sa.Text()),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=_FALSE),
        sa.Column("evidence_only", sa.Boolean(), nullable=False, server_default=_FALSE),
        sa.Column("confidence", sa.Text()),
        sa.Column("method", sa.Text()),
        # `series` is NULL where the source states none; NULLS NOT DISTINCT
        # keeps the constraint meaningful for those rows.
        sa.UniqueConstraint(
            "station_id",
            "source_key",
            "series",
            "code",
            name="uq_station_ref_code_station_id_source_key_series_code",
            postgresql_nulls_not_distinct=True,
        ),
    )
    # A lookup index, NOT a unique key: one (series, code) pair can
    # legitimately belong to more than one station.
    op.create_index("ix_station_ref_code_series_code", "station_ref_code", ["series", "code"])
    op.create_index("ix_station_ref_code_code", "station_ref_code", ["code"])

    op.create_table(
        "station_ref_merits",
        _pk(),
        _station_fk("station_ref_merits"),
        sa.Column("code", _KEY, nullable=False),
        sa.Column("origin", sa.Text()),
        sa.Column("rule", sa.Text()),
        sa.Column("confidence", sa.Text()),
        sa.Column("sources", _TEXT_ARRAY),
        sa.Column("check_digit", sa.Text()),
        sa.Column("is_chosen", sa.Boolean(), nullable=False, server_default=_FALSE),
        sa.UniqueConstraint("station_id", "code", name="uq_station_ref_merits_station_id_code"),
    )
    op.create_index(
        "uq_station_ref_merits_chosen",
        "station_ref_merits",
        ["station_id"],
        unique=True,
        postgresql_where=sa.text("is_chosen"),
    )


def _create_alias_flag_override() -> None:
    op.create_table(
        "station_ref_alias",
        _pk(),
        _station_fk("station_ref_alias"),
        sa.Column("alias_plc", _KEY, nullable=False),
        sa.Column("reason", sa.Text()),
        _build_fk("station_ref_alias", "build_id", ondelete="CASCADE", nullable=False),
        sa.UniqueConstraint(
            "alias_plc", "build_id", name="uq_station_ref_alias_alias_plc_build_id"
        ),
    )
    op.create_index("ix_station_ref_alias_station_id", "station_ref_alias", ["station_id"])

    op.create_table(
        "station_ref_flag",
        _pk(),
        _station_fk("station_ref_flag"),
        sa.Column("token", sa.Text(), nullable=False),
        # '' rather than NULL, so the unique constraint constrains bare tokens.
        sa.Column("payload", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("level", sa.Text()),
        sa.Column("warning_code", sa.Text()),
        sa.Column(
            "related_station_id",
            sa.BigInteger(),
            sa.ForeignKey(
                "station_ref.id",
                ondelete="SET NULL",
                name="fk_station_ref_flag_related_station_id_station_ref",
            ),
        ),
        sa.UniqueConstraint(
            "station_id", "token", "payload", name="uq_station_ref_flag_station_id_token_payload"
        ),
    )
    op.create_index("ix_station_ref_flag_token", "station_ref_flag", ["token"])

    op.create_table(
        "station_ref_override",
        _pk(),
        # RESTRICT: a station carrying hand corrections is not deleted by accident.
        _station_fk("station_ref_override", ondelete="RESTRICT"),
        sa.Column("field_name", sa.Text(), nullable=False),
        sa.Column("value", sa.Text()),
        sa.Column("reason", sa.Text()),
        sa.Column(
            "set_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "users.id", ondelete="SET NULL", name="fk_station_ref_override_set_by_users"
            ),
        ),
        sa.Column(
            "set_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("computed_value_at_set", sa.Text()),
        sa.Column("computed_value_latest", sa.Text()),
        sa.Column("released_at", sa.DateTime(timezone=True)),
    )
    # One active correction per (station, field); released ones are history.
    op.create_index(
        "uq_station_ref_override_active",
        "station_ref_override",
        ["station_id", "field_name"],
        unique=True,
        postgresql_where=sa.text("released_at IS NULL"),
    )


def _create_link_and_history() -> None:
    op.create_table(
        "station_ref_link",
        _pk(),
        # NULL = a stop no reference row could be matched to.
        sa.Column(
            "station_id",
            sa.BigInteger(),
            sa.ForeignKey(
                "station_ref.id",
                ondelete="CASCADE",
                name="fk_station_ref_link_station_id_station_ref",
            ),
        ),
        sa.Column(
            "source_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "station_source_version.id",
                ondelete="CASCADE",
                name="fk_station_ref_link_source_version_id",
            ),
            nullable=False,
        ),
        sa.Column("offline_station_id", sa.Text()),
        sa.Column("feed_key", sa.Text()),
        sa.Column("stop_key", sa.Text()),
        sa.Column("stop_name", sa.Text()),
        sa.Column("iso2", sa.Text()),
        sa.Column("lat", sa.Float()),
        sa.Column("lon", sa.Float()),
        sa.Column("label", sa.Text()),
        sa.Column("code_value", sa.Text()),
        sa.Column("code_series", sa.Text()),
        sa.Column("match_method", sa.Text()),
        sa.Column("tier", sa.Text()),
        sa.Column("asserted", sa.Boolean(), nullable=False, server_default=_FALSE),
        sa.Column("distance_m", sa.Float()),
        sa.Column("name_sim", sa.Float()),
        sa.Column("reason", sa.Text()),
        sa.Column("nearest_plc", _KEY),
        sa.Column("nearest_distance_m", sa.Float()),
        sa.Column("note", sa.Text()),
    )
    op.create_index("ix_station_ref_link_station_id", "station_ref_link", ["station_id"])
    op.create_index(
        "ix_station_ref_link_unmatched",
        "station_ref_link",
        ["label", "iso2"],
        postgresql_where=sa.text("station_id IS NULL"),
    )

    op.create_table(
        "station_ref_history",
        _pk(),
        _station_fk("station_ref_history"),
        _build_fk("station_ref_history", "build_id", ondelete="CASCADE", nullable=False),
        sa.Column("field_name", sa.Text(), nullable=False),
        sa.Column("old_value", sa.Text()),
        sa.Column("new_value", sa.Text()),
    )
    op.create_index(
        "ix_station_ref_history_station_id_build_id",
        "station_ref_history",
        ["station_id", "build_id"],
    )
    op.create_index("ix_station_ref_history_build_id", "station_ref_history", ["build_id"])


def upgrade() -> None:
    # Already created by the initial schema; repeated so this revision does
    # not depend on that detail for its trigram indexes.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table(
        "station_complex",
        _pk(),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("rule", sa.Text()),
        sa.Column(
            "requires_physical_separation", sa.Boolean(), nullable=False, server_default=_FALSE
        ),
        sa.Column("separation_reason", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    _create_station_ref()
    _create_codes_and_merits()
    _create_alias_flag_override()
    _create_link_and_history()


def downgrade() -> None:
    # Dropping a table drops its indexes and constraints with it.
    for table in (
        "station_ref_history",
        "station_ref_link",
        "station_ref_override",
        "station_ref_flag",
        "station_ref_alias",
        "station_ref_merits",
        "station_ref_code",
        "station_ref",
        "station_complex",
    ):
        op.drop_table(table)
