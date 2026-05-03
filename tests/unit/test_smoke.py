"""Import-chain smoke tests.

Purpose: ensure the project's modules can be imported without errors.
This is the cheapest possible regression guard against:
  - syntax errors
  - circular imports
  - missing dependencies in requirements.txt
  - broken module reorganisations

If a real bug ever sneaks past these, that's a signal we need a focused test
in the right module — not that this file should grow.
"""

from __future__ import annotations

import importlib

import pytest

EXPECTED_MODULES = [
    "app",
    "app.settings",
    "app.db",
    "app.detect",
    "app.ingestion",
    "app.main",
    "app.worker",
    # models package — split by concern, see app/models/__init__.py
    "app.models",
    "app.models.base",
    "app.models.identity",
    "app.models.sessions",
    "app.models.ingestion",
    "app.models.graph",
    "app.models.search",
    "app.models.master",
    "app.models.runtime",
    "app.models.audit",
    "app.models.config",
    # step-2 modules
    "app.config_schema",
    "app.config_service",
    "app.concurrency",
    "app.audit",
    "app.security",
    "app.api.admin.config",
    # step-3 modules
    "app.rate_limit",
    "app.auth",
    "app.auth.passwords",
    "app.auth.tokens",
    "app.auth.email",
    "app.api.auth.routes",
    "app.api.admin.users",
    # step-4 — email module rewritten with real aiosmtplib + Jinja templates
    # (no new module names; templates are non-Python assets loaded at runtime).
    # step-5 — page (HTML) router
    "app.api.pages",
    # steps 7-20 - sessions, master data, journey, reports, replay, retention
    "app.api.admin.sessions",
    "app.api.admin.replay",
    "app.api.master.stations",
    "app.api.master.aliases",
    "app.api.journey",
    "app.api.reports",
    "app.master.nap_importer",
    "app.master.trainline",
    "app.osm_filter",
    "app.router_config",
    "app.sessions_orchestrator",
    "app.staleness",
    "app.graph_snapshots",
    "app.journey.signature",
    "app.journey.recorder",
    "app.journey.otp_client",
    "app.retention",
    # version badge — shared Jinja env that registers the viator_version global
    "app.templating",
]


@pytest.mark.parametrize("module_name", EXPECTED_MODULES)
def test_module_imports_cleanly(module_name: str) -> None:
    module = importlib.import_module(module_name)
    assert module is not None


def test_detect_known_kinds_is_non_empty() -> None:
    """Tiny invariant: the detector must declare at least the eight kinds the spec lists."""
    from app.detect import KNOWN_KINDS

    assert len(KNOWN_KINDS) >= 8
    assert "GTFS" in KNOWN_KINDS
    assert "OSM-PBF" in KNOWN_KINDS
    assert "SNCF-MCT" in KNOWN_KINDS


def test_settings_has_safe_defaults() -> None:
    """Settings() must construct without raising even with minimal env."""
    from app.settings import Settings

    s = Settings()
    assert s.max_upload_mb > 0
    assert s.debounce_seconds > 0


def test_models_metadata_has_all_expected_tables() -> None:
    """Base.metadata carries every table the migration creates."""
    from app.models import Base

    table_names = set(Base.metadata.tables.keys())
    expected = {
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
    missing = expected - table_names
    assert not missing, f"models package missing tables: {sorted(missing)}"


def test_user_role_enum_matches_spec() -> None:
    """Three roles, exactly. See spec §3.1."""
    from app.models.identity import UserRole

    assert {r.value for r in UserRole} == {
        "platform_admin",
        "content_manager",
        "end_user",
    }
