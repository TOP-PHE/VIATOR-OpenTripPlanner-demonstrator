"""Run `alembic upgrade head` end-to-end against a real Postgres.

CI provides a postgres service container (see .github/workflows/ci.yml).
Locally, run `docker compose up -d postgres` first OR set DATABASE_URL to a
disposable instance.

These tests are gated on a Postgres being reachable. If the connection fails,
they SKIP rather than fail — so this file is safe to run in environments
without Postgres (e.g. a contributor's laptop without the stack up).
"""

from __future__ import annotations

import os

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, func, inspect, select, text
from sqlalchemy import table as sa_table
from sqlalchemy.exc import IntegrityError, OperationalError

from alembic import command

REQUIRED_TABLES = {
    "users",
    "verification_tokens",
    "password_reset_tokens",
    "sessions",
    "uploads",
    "rebuild_jobs",
    "graph_snapshots",
    "master_stations",
    "master_stations_pending_drift",
    "route_aliases",
    "master_carriers",
    "master_carriers_pending_drift",
    "mct_overrides",
    "stations_xref",
    "journey_searches",
    "journey_search_executions",
    "journey_trips",
    "audit_events",
    "platform_config",
    # Station panel, step 1 (docs/station-panel-design.md section 11).
    "station_code_series",
    "station_source",
    "station_source_version",
    "station_build",
    "crd_location",
    "crd_subsidiary",
    "era_operational_point",
    "station_complex",
    "station_ref",
    "station_ref_code",
    "station_ref_merits",
    "station_ref_alias",
    "station_ref_flag",
    "station_ref_override",
    "station_ref_link",
    "station_ref_history",
}


def _postgres_or_skip() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgresql"):
        pytest.skip(f"DATABASE_URL is not a Postgres URL ({url!r}); skipping migration test")
    try:
        engine = create_engine(url)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError as exc:
        pytest.skip(f"Postgres not reachable ({exc}); skipping migration test")
    return url


@pytest.fixture
def alembic_cfg() -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "alembic")
    return cfg


def test_upgrade_head_creates_full_schema(alembic_cfg: Config) -> None:
    """`alembic upgrade head` lays down every table the spec requires."""
    url = _postgres_or_skip()

    # Start fresh — drop everything if a prior test left residue.
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE;"))
        conn.execute(text("CREATE SCHEMA public;"))

    command.upgrade(alembic_cfg, "head")

    actual_tables = set(inspect(engine).get_table_names())
    missing = REQUIRED_TABLES - actual_tables
    assert not missing, f"missing tables after upgrade head: {sorted(missing)}"


def test_upgrade_head_creates_provenance_view(alembic_cfg: Config) -> None:
    """The cross-session provenance VIEW is present and queryable."""
    url = _postgres_or_skip()

    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE;"))
        conn.execute(text("CREATE SCHEMA public;"))

    command.upgrade(alembic_cfg, "head")

    with engine.connect() as conn:
        # Empty result set is fine — we're testing that the view exists and is queryable.
        result = conn.execute(text("SELECT * FROM journey_trip_provenance LIMIT 1;"))
        result.fetchall()


def test_downgrade_base_drops_everything(alembic_cfg: Config) -> None:
    """`alembic downgrade base` from head leaves the schema empty (modulo extensions)."""
    url = _postgres_or_skip()

    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE;"))
        conn.execute(text("CREATE SCHEMA public;"))

    command.upgrade(alembic_cfg, "head")
    command.downgrade(alembic_cfg, "base")

    remaining = set(inspect(engine).get_table_names()) - {"alembic_version"}
    assert not remaining, f"downgrade base left tables behind: {sorted(remaining)}"


# ────────────────────────── station panel schema ──────────────────────────


def _fresh_head(alembic_cfg: Config) -> Engine:
    url = _postgres_or_skip()
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE;"))
        conn.execute(text("CREATE SCHEMA public;"))
    command.upgrade(alembic_cfg, "head")
    return engine


def _count(engine: Engine, table: str) -> int:
    with engine.connect() as conn:
        return int(conn.execute(select(func.count()).select_from(sa_table(table))).scalar_one())


def test_station_seeds_are_present(alembic_cfg: Config) -> None:
    """The series vocabulary and the importer's sources are seeded; no station row is."""
    engine = _fresh_head(alembic_cfg)
    with engine.connect() as conn:
        formats = dict(
            conn.execute(text("SELECT format, count(*) FROM station_source GROUP BY format")).all()
        )
        unresolved = list(
            conn.execute(
                text("SELECT key FROM station_source WHERE source_key_unresolved ORDER BY key")
            ).scalars()
        )
    assert _count(engine, "station_code_series") == 14
    assert unresolved == ["nap_CH_SBB_non_rail_members", "nap_ES_regional", "nap_FR_regional"]
    assert formats == {
        "crd_locations_csv": 1,
        "era_telref_csv": 1,
        "station_master_csv": 1,
        "station_links_csv": 1,
        "station_unmapped_csv": 1,
        "trainline_csv": 1,
        "offline_master_column": 16,
    }
    for table in ("station_ref", "crd_location", "era_operational_point", "station_ref_link"):
        assert _count(engine, table) == 0


def test_station_ref_grain_is_plc_and_operational_point(alembic_cfg: Config) -> None:
    """Two operational points of one PLC coexist; a repeated pair, a NULL
    era_uopid and a PLC that is not 7 characters are all refused."""
    engine = _fresh_head(alembic_cfg)
    insert = text("INSERT INTO station_ref (plc, era_uopid) VALUES (:plc, :uopid)")
    with engine.begin() as conn:
        conn.execute(insert, {"plc": "ZZ00001", "uopid": "ZZ00001"})
        conn.execute(insert, {"plc": "ZZ00001", "uopid": "ZZOP002"})
    for params in (
        {"plc": "ZZ00001", "uopid": "ZZ00001"},  # the pair repeats
        {"plc": "ZZ00002", "uopid": None},  # NULL would escape the unique key
        {"plc": "ZZ3", "uopid": "ZZ3"},  # not 7 characters
    ):
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(insert, params)
    assert _count(engine, "station_ref") == 2


def test_one_code_may_belong_to_several_stations(alembic_cfg: Config) -> None:
    """(series, code) is a lookup index: the same pair on two stations is legal,
    while the same code twice on one station and source is not, series or no series."""
    engine = _fresh_head(alembic_cfg)
    station = text("INSERT INTO station_ref (plc, era_uopid) VALUES (:plc, :plc) RETURNING id")
    code = text(
        "INSERT INTO station_ref_code (station_id, source_key, series, code) "
        "VALUES (:sid, 'nap_CH_SBB', :series, :code)"
    )
    with engine.begin() as conn:
        ids = [conn.execute(station, {"plc": plc}).scalar_one() for plc in ("ZZ00001", "ZZ00002")]
        for sid in ids:
            conn.execute(code, {"sid": sid, "series": "PLC", "code": "9900001"})
        conn.execute(code, {"sid": ids[0], "series": None, "code": "9900009"})
    for refused in (
        {"sid": ids[0], "series": "PLC", "code": "9900001"},
        {"sid": ids[0], "series": None, "code": "9900009"},  # NULLS NOT DISTINCT
        {"sid": ids[0], "series": "not_a_series", "code": "1"},  # FK to the vocabulary
    ):
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(code, refused)
    assert _count(engine, "station_ref_code") == 3


def test_rebuild_jobs_kind_defaults_to_graph(alembic_cfg: Config) -> None:
    engine = _fresh_head(alembic_cfg)
    with engine.begin() as conn:
        kind = conn.execute(
            text("INSERT INTO rebuild_jobs (status) VALUES ('pending') RETURNING kind")
        ).scalar_one()
    assert kind == "graph"


def test_downgrade_past_the_station_panel_leaves_no_station_table(alembic_cfg: Config) -> None:
    engine = _fresh_head(alembic_cfg)
    command.downgrade(alembic_cfg, "20261002_2100_rebuild_cancel")
    inspector = inspect(engine)
    left = {
        t
        for t in inspector.get_table_names()
        if t.startswith(("station_", "crd_", "era_")) and t != "stations_xref"
    }
    assert not left, f"station tables left behind: {sorted(left)}"
    assert "kind" not in {c["name"] for c in inspector.get_columns("rebuild_jobs")}
