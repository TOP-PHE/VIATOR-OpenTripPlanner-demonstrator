"""Station panel, part 1 of 4: sources, source versions, builds, code series.

`station_source` is one row per (portal, dataset) a station input comes from;
`station_source_version` one row per acquired file, unique on its sha256 so
re-uploading the same file is a no-op; `station_build` one row per run of the
reference build. `station_code_series` is the code-series vocabulary: a lookup
table rather than a CHECK, so a new series is an INSERT, not a migration.

Seeds are configuration, not data: the 14 series keys of the offline links
file, and one source per input the step 1 importer knows (the five offline
files, Trainline, and the 16 provider columns of the offline master, three of
which are aggregates flagged `source_key_unresolved`).

See docs/station-panel-design.md section 3.1 and 3.7, and
docs/station-offline-file-shapes.md.

Revision ID: 20261003_1200_station_sources
Revises: 20261002_2100_rebuild_cancel
Create Date: 2026-10-03 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20261003_1200_station_sources"
down_revision: str | Sequence[str] | None = "20261002_2100_rebuild_cancel"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KEY = sa.Text(collation="C")
_INTERNAL_ONLY = "RNE licence (CRD): use inside VIATOR only, never published"

# The `code_series` vocabulary of the offline links file, verbatim.
_SERIES: tuple[tuple[str, str, bool], ...] = (
    ("DELFI_stop_key", "DELFI stop key", True),
    ("CH_service_point_number", "Swiss service point number", True),
    ("SNCF_8digit", "SNCF 8-digit code", True),
    # A key that means something only inside the feed that carries it.
    ("feed_local", "Feed-local stop key", False),
    ("PLC", "Primary location code", True),
    ("Renfe_5digit", "Renfe 5-digit code", True),
    ("Trenitalia_9digit", "Trenitalia 9-digit code", True),
    ("SNCF_8digit_in_feed_key", "SNCF 8-digit code inside a feed key", True),
    ("OeBB_NAP_stop_key", "OeBB NAP stop key", True),
    ("SNCB_quay_root_7digit", "SNCB quay root, 7 digits", True),
    ("NS_station_abbreviation", "NS station abbreviation", True),
    ("LU_CdT_stop_number", "Luxembourg CdT stop number", True),
    ("Eurostar_stop_code_intl7", "Eurostar stop code, 7-digit international", True),
    ("Ouigo_ES_9digit", "Ouigo Spain 9-digit code", True),
)

# The provider columns of the offline master: (column, country, operator, aggregate).
# An aggregate does not map onto one (portal, dataset); splitting it is a later job.
_PROVIDER_COLUMNS: tuple[tuple[str, str | None, str | None, bool], ...] = (
    ("nap_AT_OEBB", "AT", "OEBB", False),
    ("nap_BE_SNCB", "BE", "SNCB", False),
    ("nap_CH_SBB", "CH", "SBB", False),
    ("nap_CH_SBB_non_rail_members", "CH", None, True),
    ("nap_CZ_CZPTT", "CZ", "CZPTT", False),
    ("nap_DE_DELFI", "DE", "DELFI", False),
    ("nap_ES_OUIGO", "ES", "OUIGO", False),
    ("nap_ES_RENFE", "ES", "RENFE", False),
    ("nap_ES_regional", "ES", None, True),
    ("nap_EUROSTAR", None, "EUROSTAR", False),
    ("nap_FR_SNCF", "FR", "SNCF", False),
    ("nap_FR_TRENITALIA_FR", "FR", "TRENITALIA_FR", False),
    ("nap_FR_regional", "FR", None, True),
    ("nap_IT_TRENITALIA", "IT", "TRENITALIA", False),
    ("nap_LU", "LU", None, False),
    ("nap_NL_IFF", "NL", "IFF", False),
)


def _source(key: str, label: str, kind: str, fmt: str, **extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "key": key,
        "label": label,
        "kind": kind,
        "format": fmt,
        "acquisition": "upload",
        "country_iso": None,
        "operator": None,
        "licence": None,
        "licence_url": None,
        "refresh_cadence": None,
        "triggers_rebuild": False,
        "source_key_unresolved": False,
    }
    row.update(extra)
    return row


def _seed_sources() -> list[dict[str, Any]]:
    rows = [
        _source(
            "CRD",
            "CRD primary locations (offline extract)",
            "spine",
            "crd_locations_csv",
            licence=_INTERNAL_ONLY,
            triggers_rebuild=True,
        ),
        _source(
            "ERA_TELREF",
            "ERA operational points (telref extract)",
            "registry",
            "era_telref_csv",
            triggers_rebuild=True,
        ),
        _source(
            "OFFLINE_MASTER",
            "Offline station master (station_master_crd)",
            "offline_build",
            "station_master_csv",
            licence=_INTERNAL_ONLY,
            triggers_rebuild=True,
        ),
        _source(
            "OFFLINE_LINKS",
            "Offline NAP stop links (station_links_crd)",
            "offline_build",
            "station_links_csv",
            licence=_INTERNAL_ONLY,
            triggers_rebuild=True,
        ),
        _source(
            "OFFLINE_UNMAPPED",
            "Offline unmatched NAP stops (nap_rail_stations_unmapped_crd)",
            "offline_build",
            "station_unmapped_csv",
            licence=_INTERNAL_ONLY,
            triggers_rebuild=True,
        ),
        _source(
            "TRAINLINE",
            "Trainline stations (trainline-eu/stations)",
            "merits_input",
            "trainline_csv",
            acquisition="url",
            licence="ODbL",
            licence_url="https://github.com/trainline-eu/stations",
            refresh_cadence="daily, 04:00 UTC",
        ),
    ]
    for column, country, operator, aggregate in _PROVIDER_COLUMNS:
        rows.append(
            _source(
                column,
                f"Offline master column {column}",
                "timetable",
                "offline_master_column",
                country_iso=country,
                operator=operator,
                source_key_unresolved=aggregate,
            )
        )
    return rows


def upgrade() -> None:
    series = op.create_table(
        "station_code_series",
        sa.Column("key", _KEY, primary_key=True),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("family", sa.Text()),
        sa.Column("is_joinable", sa.Boolean(), nullable=False, server_default=sa.text("TRUE")),
    )

    source = op.create_table(
        "station_source",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("key", _KEY, nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("format", sa.Text(), nullable=False),
        sa.Column("acquisition", sa.Text(), nullable=False),
        sa.Column("resolver_type", sa.Text()),
        sa.Column("resolver_config", postgresql.JSONB()),
        sa.Column(
            "credential_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "user_credentials.id",
                ondelete="SET NULL",
                name="fk_station_source_credential_id_user_credentials",
            ),
        ),
        sa.Column("country_iso", sa.Text()),
        sa.Column("operator", sa.Text()),
        sa.Column("licence", sa.Text()),
        sa.Column("licence_url", sa.Text()),
        sa.Column("access_expires_on", sa.Date()),
        sa.Column("refresh_cadence", sa.Text()),
        sa.Column(
            "triggers_rebuild", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("TRUE")),
        sa.Column(
            "source_key_unresolved", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("key", name="uq_station_source_key"),
    )

    op.create_table(
        "station_source_version",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "source_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "station_source.id",
                ondelete="RESTRICT",
                name="fk_station_source_version_source_id_station_source",
            ),
            nullable=False,
        ),
        sa.Column(
            "acquired_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("as_of", sa.Date()),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'uploaded'")),
        sa.Column("error", sa.Text()),
        sa.Column("stats", postgresql.JSONB()),
        sa.Column("stored_path", sa.Text()),
        sa.Column(
            "uploaded_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "users.id", ondelete="SET NULL", name="fk_station_source_version_uploaded_by_users"
            ),
        ),
        # Re-uploading the same file is a no-op, not a duplicate.
        sa.UniqueConstraint(
            "source_id", "sha256", name="uq_station_source_version_source_id_sha256"
        ),
    )
    op.create_index(
        "ix_station_source_version_source_id_acquired_at",
        "station_source_version",
        ["source_id", sa.text("acquired_at DESC")],
    )

    op.create_table(
        "station_build",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'running'")),
        sa.Column("builder_version", sa.Text()),
        sa.Column("inputs", postgresql.JSONB()),
        sa.Column("counts", postgresql.JSONB()),
        sa.Column("diff_summary", postgresql.JSONB()),
        sa.Column("log_path", sa.Text()),
    )

    op.bulk_insert(
        series,
        [{"key": key, "label": label, "is_joinable": joinable} for key, label, joinable in _SERIES],
    )
    op.bulk_insert(source, _seed_sources())


def downgrade() -> None:
    op.drop_table("station_build")
    op.drop_index(
        "ix_station_source_version_source_id_acquired_at", table_name="station_source_version"
    )
    op.drop_table("station_source_version")
    op.drop_table("station_source")
    op.drop_table("station_code_series")
