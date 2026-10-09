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
from typing import Any

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
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


# ── 20261009_1200_hub_uic: a station code on the coverage hubs ──

_BEFORE_HUB_UIC = "20261002_2100_rebuild_cancel"
_HUB_UIC = "20261009_1200_hub_uic"


def _hub_columns(engine: Any) -> set[str]:
    return {column["name"] for column in inspect(engine).get_columns("network_coverage_hubs")}


def _set_code(engine: Any, uic: str | None, origin: str | None) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE network_coverage_hubs SET uic = :uic, uic_origin = :origin WHERE id = 'zz-hub'"
            ),
            {"uic": uic, "origin": origin},
        )


def test_hub_uic_revision_up_down_up_and_its_check(alembic_cfg: Config) -> None:
    """The revision adds two null columns to the existing hubs and writes
    nothing else; the CHECK keeps a code and its origin together; the
    downgrade drops both and keeps the hub; a second upgrade works."""
    url = _postgres_or_skip()
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE;"))
        conn.execute(text("CREATE SCHEMA public;"))

    command.upgrade(alembic_cfg, _BEFORE_HUB_UIC)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO network_coverage_hubs (id, name, short, country, lat, lon) "
                "VALUES ('zz-hub', 'ZZ Hub Central', 'ZZ Hub', 'ZZ', 45.5, 6.25)"
            )
        )

    command.upgrade(alembic_cfg, _HUB_UIC)
    assert {"uic", "uic_origin"} <= _hub_columns(engine)
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT name, uic, uic_origin FROM network_coverage_hubs WHERE id = 'zz-hub'")
        ).one()
    assert tuple(row) == ("ZZ Hub Central", None, None)

    for origin in ("msmm", "manual"):
        _set_code(engine, "9900001", origin)
    _set_code(engine, None, None)
    for uic, origin in (("9900001", None), ("9900001", "zz"), (None, "msmm"), (None, "manual")):
        with pytest.raises(IntegrityError):
            _set_code(engine, uic, origin)

    _set_code(engine, "9900001", "manual")
    command.downgrade(alembic_cfg, _BEFORE_HUB_UIC)
    assert not {"uic", "uic_origin"} & _hub_columns(engine)
    with engine.connect() as conn:
        names = (
            conn.execute(text("SELECT name FROM network_coverage_hubs WHERE id = 'zz-hub'"))
            .scalars()
            .all()
        )
    assert names == ["ZZ Hub Central"]

    command.upgrade(alembic_cfg, "head")
    assert {"uic", "uic_origin"} <= _hub_columns(engine)
