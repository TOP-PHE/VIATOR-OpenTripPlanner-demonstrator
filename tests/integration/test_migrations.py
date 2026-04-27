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
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import OperationalError

from alembic import command
from alembic.config import Config

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
