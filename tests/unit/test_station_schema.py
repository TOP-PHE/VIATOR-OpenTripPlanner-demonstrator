"""The station panel schema, checked without a database.

Alembic renders the four migrations to SQL in offline mode, so the things the
design insists on can be asserted here: the revision ids fit Alembic's
varchar(32), `(plc, era_uopid)` is unique with `era_uopid` NOT NULL,
`(series, code)` is a plain index, the FK actions are the specified ones, and
the migrations create exactly the columns the ORM models declare. The one
statement that touches rows, the DELETE of the last downgrade, is also run on
an in-memory SQLite stand-in for `rebuild_jobs`.

The live upgrade/downgrade runs in tests/integration/test_migrations.py.
"""

from __future__ import annotations

import io
import re
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from alembic import command
from app import models
from app.master.station_import import STATION_BUILD_KIND

ROOT = Path(__file__).resolve().parents[2]
BASE = "20261002_2100_rebuild_cancel"
REVISIONS = [
    "20261003_1200_station_sources",
    "20261003_1210_station_reg",
    "20261003_1220_station_ref",
    "20261003_1230_rebuild_kind",
]
STATION_TABLES = {
    "station_code_series": models.StationCodeSeries,
    "station_source": models.StationSource,
    "station_source_version": models.StationSourceVersion,
    "station_build": models.StationBuild,
    "crd_location": models.CrdLocation,
    "crd_subsidiary": models.CrdSubsidiary,
    "era_operational_point": models.EraOperationalPoint,
    "station_complex": models.StationComplex,
    "station_ref": models.StationRef,
    "station_ref_code": models.StationRefCode,
    "station_ref_merits": models.StationRefMerits,
    "station_ref_alias": models.StationRefAlias,
    "station_ref_flag": models.StationRefFlag,
    "station_ref_override": models.StationRefOverride,
    "station_ref_link": models.StationRefLink,
    "station_ref_history": models.StationRefHistory,
}
# The series vocabulary of the offline links file, verbatim.
SERIES_KEYS = {
    "DELFI_stop_key",
    "CH_service_point_number",
    "SNCF_8digit",
    "feed_local",
    "PLC",
    "Renfe_5digit",
    "Trenitalia_9digit",
    "SNCF_8digit_in_feed_key",
    "OeBB_NAP_stop_key",
    "SNCB_quay_root_7digit",
    "NS_station_abbreviation",
    "LU_CdT_stop_number",
    "Eurostar_stop_code_intl7",
    "Ouigo_ES_9digit",
}


def _cfg(buffer: io.StringIO) -> Config:
    # No ini file on purpose: with one, alembic/env.py calls logging's
    # fileConfig, which disables every logger already created in this process
    # and would change what later unit tests can capture.
    cfg = Config(output_buffer=buffer)
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    return cfg


@pytest.fixture(scope="module")
def upgrade_sql() -> str:
    buffer = io.StringIO()
    command.upgrade(_cfg(buffer), f"{BASE}:{REVISIONS[-1]}", sql=True)
    return buffer.getvalue()


@pytest.fixture(scope="module")
def downgrade_sql() -> str:
    buffer = io.StringIO()
    command.downgrade(_cfg(buffer), f"{REVISIONS[-1]}:{BASE}", sql=True)
    return buffer.getvalue()


def _create_table(sql: str, table: str) -> str:
    match = re.search(rf"CREATE TABLE {table} \((.*?)\n\);", sql, re.DOTALL)
    assert match, f"no CREATE TABLE for {table}"
    return match.group(1)


def _columns(body: str) -> set[str]:
    """Column names of a rendered CREATE TABLE body (constraints excluded)."""
    names = set()
    for line in body.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("CONSTRAINT"):
            names.add(stripped.split()[0].strip('"'))
    return names


# ── revision ids ───────────────────────────────────────────────────────


@pytest.mark.parametrize("revision", REVISIONS)
def test_revision_id_fits_alembic_version_num(revision: str) -> None:
    # alembic_version.version_num is varchar(32): a longer id imports fine and
    # fails at `upgrade`, in production.
    assert len(revision) <= 32
    assert re.fullmatch(r"\d{8}_\d{4}_[a-z_]+", revision)


def test_the_four_revisions_form_one_chain_to_a_single_head() -> None:
    script = ScriptDirectory.from_config(_cfg(io.StringIO()))
    assert script.get_heads() == [REVISIONS[-1]]
    expected_down = [BASE, *REVISIONS[:-1]]
    for revision, down in zip(REVISIONS, expected_down, strict=True):
        assert script.get_revision(revision).down_revision == down


# ── what the design insists on ─────────────────────────────────────────


def test_station_ref_key_is_plc_and_a_not_null_era_uopid(upgrade_sql: str) -> None:
    body = _create_table(upgrade_sql, "station_ref")
    assert 'era_uopid TEXT COLLATE "C" NOT NULL' in body
    assert 'plc TEXT COLLATE "C" NOT NULL' in body
    assert "CONSTRAINT uq_station_ref_plc_era_uopid UNIQUE (plc, era_uopid)" in body
    assert "CONSTRAINT ck_station_ref_plc_length CHECK (length(plc) = 7)" in body
    # No char(n) anywhere: it pads, and a real PLC is not always AA99999.
    assert "CHAR(" not in upgrade_sql.replace("VARCHAR(", "")


def test_series_code_is_a_lookup_index_not_a_unique_key(upgrade_sql: str) -> None:
    assert (
        "CREATE INDEX ix_station_ref_code_series_code ON station_ref_code (series, code);"
        in upgrade_sql
    )
    assert "CREATE INDEX ix_station_ref_code_code ON station_ref_code (code);" in upgrade_sql
    body = _create_table(upgrade_sql, "station_ref_code")
    assert "UNIQUE NULLS NOT DISTINCT (station_id, source_key, series, code)" in body
    assert "UNIQUE (series, code)" not in upgrade_sql


def test_is_current_is_a_plain_column_not_a_generated_one(upgrade_sql: str) -> None:
    # CURRENT_DATE is not immutable: Postgres refuses it in a generated column
    # and in an index predicate.
    assert "GENERATED" not in upgrade_sql
    assert "CURRENT_DATE" not in upgrade_sql
    assert "is_current BOOLEAN DEFAULT TRUE NOT NULL" in _create_table(upgrade_sql, "station_ref")
    assert "ON station_ref (plc) WHERE is_current;" in upgrade_sql


def test_trigram_and_partial_indexes(upgrade_sql: str) -> None:
    for statement in (
        "CREATE INDEX ix_station_ref_name_trgm ON station_ref USING gin (name gin_trgm_ops);",
        "CREATE INDEX ix_station_ref_alt_name_trgm ON station_ref "
        "USING gin (alt_name_text gin_trgm_ops);",
        "CREATE INDEX ix_station_ref_typeahead ON station_ref USING gin (name gin_trgm_ops) "
        "WHERE is_passenger AND uic_merits IS NOT NULL;",
        "CREATE INDEX ix_station_ref_iso2_all ON station_ref USING gin (iso2_all);",
        "CREATE INDEX ix_station_ref_iso2_is_passenger ON station_ref (iso2, is_passenger);",
        "CREATE UNIQUE INDEX uq_station_ref_merits_chosen ON station_ref_merits (station_id) "
        "WHERE is_chosen;",
        "CREATE UNIQUE INDEX uq_station_ref_override_active ON station_ref_override "
        "(station_id, field_name) WHERE released_at IS NULL;",
        "CREATE INDEX ix_station_source_version_source_id_acquired_at ON "
        "station_source_version (source_id, acquired_at DESC);",
    ):
        assert statement in upgrade_sql


@pytest.mark.parametrize(
    ("table", "fragment"),
    [
        ("station_source", "REFERENCES user_credentials (id) ON DELETE SET NULL"),
        ("station_source_version", "REFERENCES station_source (id) ON DELETE RESTRICT"),
        ("crd_location", "REFERENCES station_source_version (id) ON DELETE CASCADE"),
        ("crd_subsidiary", "REFERENCES station_source_version (id) ON DELETE CASCADE"),
        ("era_operational_point", "REFERENCES station_source_version (id) ON DELETE CASCADE"),
        ("station_ref", "REFERENCES station_complex (id) ON DELETE SET NULL"),
        (
            "station_ref",
            "FOREIGN KEY(first_seen_build_id) REFERENCES station_build (id) ON DELETE RESTRICT",
        ),
        (
            "station_ref",
            "FOREIGN KEY(last_built_build_id) REFERENCES station_build (id) ON DELETE RESTRICT",
        ),
        (
            "station_ref",
            "FOREIGN KEY(last_changed_build_id) REFERENCES station_build (id) ON DELETE RESTRICT",
        ),
        ("station_ref_code", "REFERENCES station_ref (id) ON DELETE CASCADE"),
        ("station_ref_code", "FOREIGN KEY(series) REFERENCES station_code_series (key)"),
        (
            "station_ref_flag",
            "FOREIGN KEY(related_station_id) REFERENCES station_ref (id) ON DELETE SET NULL",
        ),
    ],
)
def test_foreign_key_actions(upgrade_sql: str, table: str, fragment: str) -> None:
    assert fragment in _create_table(upgrade_sql, table)


def test_unique_keys_of_the_other_tables(upgrade_sql: str) -> None:
    for table, fragment in (
        ("station_source_version", "UNIQUE (source_id, sha256)"),
        ("crd_location", "UNIQUE (source_version_id, country, location_code, start_validity)"),
        ("era_operational_point", "UNIQUE (source_version_id, plc, uopid)"),
        ("station_ref_merits", "UNIQUE (station_id, code)"),
        ("station_ref_alias", "UNIQUE (alias_plc, build_id)"),
        ("station_ref_flag", "UNIQUE (station_id, token, payload)"),
    ):
        assert fragment in _create_table(upgrade_sql, table)


# ── seeds ──────────────────────────────────────────────────────────────


def test_code_series_is_seeded_with_the_fourteen_offline_keys(upgrade_sql: str) -> None:
    seeded = set(
        re.findall(r"INSERT INTO station_code_series \(.*?\) VALUES \('([^']+)'", upgrade_sql)
    )
    assert seeded == SERIES_KEYS
    assert len(seeded) == 14


def test_sources_are_seeded_for_every_importer_input(upgrade_sql: str) -> None:
    rows = re.findall(
        r"INSERT INTO station_source \(.*?\) VALUES \('([^']+)', '[^']*', '([^']+)', '([^']+)'",
        upgrade_sql,
    )
    by_key = {key: (kind, fmt) for key, kind, fmt in rows}
    assert by_key["CRD"] == ("spine", "crd_locations_csv")
    assert by_key["ERA_TELREF"] == ("registry", "era_telref_csv")
    assert by_key["OFFLINE_MASTER"] == ("offline_build", "station_master_csv")
    assert by_key["OFFLINE_LINKS"] == ("offline_build", "station_links_csv")
    assert by_key["OFFLINE_UNMAPPED"] == ("offline_build", "station_unmapped_csv")
    assert by_key["TRAINLINE"] == ("merits_input", "trainline_csv")
    provider_columns = {k for k, (_, fmt) in by_key.items() if fmt == "offline_master_column"}
    assert len(provider_columns) == 16
    assert all(k.startswith("nap_") for k in provider_columns)
    assert "nap_station_ids" not in provider_columns


def test_the_three_aggregate_columns_are_flagged_unresolved(upgrade_sql: str) -> None:
    unresolved = set(
        re.findall(
            r"INSERT INTO station_source \(.*?\) VALUES \('([^']+)',[^;]*, true\);", upgrade_sql
        )
    )
    assert unresolved == {"nap_FR_regional", "nap_ES_regional", "nap_CH_SBB_non_rail_members"}


def test_no_real_station_row_is_seeded(upgrade_sql: str) -> None:
    # The repository is public: configuration is seeded, data never.
    seeded_tables = set(re.findall(r"INSERT INTO (\w+)", upgrade_sql))
    assert seeded_tables == {"station_code_series", "station_source"}


# ── rebuild_jobs.kind ──────────────────────────────────────────────────


def test_rebuild_jobs_kind_defaults_to_graph(upgrade_sql: str) -> None:
    assert (
        "ALTER TABLE rebuild_jobs ADD COLUMN kind VARCHAR(32) DEFAULT 'graph' NOT NULL;"
        in upgrade_sql
    )
    kind = models.RebuildJob.__table__.c.kind
    assert not kind.nullable
    assert str(kind.server_default.arg) == "'graph'"


# ── models and migrations agree ────────────────────────────────────────


@pytest.mark.parametrize("table", sorted(STATION_TABLES))
def test_migration_creates_the_columns_the_model_declares(upgrade_sql: str, table: str) -> None:
    model_sql = str(
        CreateTable(STATION_TABLES[table].__table__).compile(dialect=postgresql.dialect())
    )
    model_body = re.search(r"\((.*)\)", model_sql, re.DOTALL)
    assert model_body
    assert _columns(_create_table(upgrade_sql, table)) == _columns(model_body.group(1))


@pytest.mark.parametrize("table", sorted(STATION_TABLES))
def test_model_and_migration_agree_on_types_and_nullability(upgrade_sql: str, table: str) -> None:
    def column_lines(body: str) -> dict[str, str]:
        lines = {}
        for line in body.splitlines():
            stripped = line.strip().rstrip(",").strip()
            if stripped and not stripped.startswith("CONSTRAINT"):
                lines[stripped.split()[0]] = stripped
        return lines

    model_sql = str(
        CreateTable(STATION_TABLES[table].__table__).compile(dialect=postgresql.dialect())
    )
    model_body = re.search(r"\((.*)\)", model_sql, re.DOTALL)
    assert model_body
    assert column_lines(_create_table(upgrade_sql, table)) == column_lines(model_body.group(1))


# ── downgrade ──────────────────────────────────────────────────────────


def test_downgrade_drops_everything_the_upgrade_created(downgrade_sql: str) -> None:
    dropped = set(re.findall(r"DROP TABLE (\w+);", downgrade_sql))
    assert dropped == set(STATION_TABLES)
    assert "ALTER TABLE rebuild_jobs DROP COLUMN kind;" in downgrade_sql


def test_downgrade_deletes_the_station_jobs_before_it_drops_the_column(downgrade_sql: str) -> None:
    # A station job has no session. Without `kind` it reads as the legacy
    # session-less graph job, which the previous release's worker runs as an
    # OTP build: the rows go first, while the column can still tell them apart.
    delete = "DELETE FROM rebuild_jobs WHERE kind = 'station_build';"
    assert delete in downgrade_sql
    assert downgrade_sql.index(delete) < downgrade_sql.index(
        "ALTER TABLE rebuild_jobs DROP COLUMN kind;"
    )
    # The migration spells the kind out; it is the one the app queues.
    assert f"'{STATION_BUILD_KIND}'" in delete


def test_downgrade_leaves_the_graph_jobs_and_no_station_job() -> None:
    """The downgrade of `rebuild_jobs.kind`, run on a stand-in table."""
    script = ScriptDirectory.from_config(_cfg(io.StringIO()))
    migration = script.get_revision(REVISIONS[-1]).module
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE rebuild_jobs (id INTEGER PRIMARY KEY, session_id TEXT, "
                "status TEXT NOT NULL, kind VARCHAR(32) DEFAULT 'graph' NOT NULL)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO rebuild_jobs (id, status, kind) VALUES "
                "(1, 'pending', 'station_build'), (2, 'done', 'station_build'), "
                "(3, 'pending', 'graph'), (4, 'done', 'graph')"
            )
        )
        with Operations.context(MigrationContext.configure(conn)):
            migration.downgrade()
        left = [tuple(row) for row in conn.execute(text("SELECT * FROM rebuild_jobs ORDER BY id"))]
    engine.dispose()
    # Three columns: `kind` is gone, and so is every job only it could tell apart.
    assert left == [(3, None, "pending"), (4, None, "done")]


def test_required_tables_of_the_integration_test_cover_the_station_tables() -> None:
    from tests.integration.test_migrations import REQUIRED_TABLES

    assert set(STATION_TABLES) <= REQUIRED_TABLES
