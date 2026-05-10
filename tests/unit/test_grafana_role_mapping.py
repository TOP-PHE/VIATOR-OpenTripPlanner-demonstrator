"""Unit tests for the VIATOR-role → Grafana-role mapping (audit #14 Phase 2.1).

Imports from `app.auth.grafana_role_map` (a pure-data module with no
dependencies) so these tests run on any environment — including Windows
dev boxes where psycopg's binary wheel won't load and importing
`app.api.auth.routes` (which transitively pulls in the DB stack) fails.

The mapping is small and stable, but the security implications of getting
it wrong are real:
  - mapping platform_admin → Editor would silently downgrade admin power
  - mapping end_user → Admin would let anyone with a JWT manage Grafana
    plugins, data sources, etc.

These tests pin the mapping so a regression has to be deliberate.
"""

from __future__ import annotations

from app.auth.grafana_role_map import (
    DEFAULT_GRAFANA_ROLE,
    VIATOR_TO_GRAFANA_ROLE,
    viator_role_to_grafana,
)


def test_platform_admin_maps_to_admin() -> None:
    """Full Grafana org admin: invite users, manage data sources, plugins."""
    assert viator_role_to_grafana("platform_admin") == "Admin"


def test_content_manager_maps_to_editor() -> None:
    """Can create/edit dashboards but cannot manage users or data sources."""
    assert viator_role_to_grafana("content_manager") == "Editor"


def test_end_user_maps_to_viewer() -> None:
    """Read-only access to dashboards."""
    assert viator_role_to_grafana("end_user") == "Viewer"


def test_unknown_role_falls_back_to_viewer() -> None:
    """Least-privilege fallback — never give a typo'd role Editor / Admin."""
    assert viator_role_to_grafana("not_a_real_role") == "Viewer"
    assert viator_role_to_grafana("") == "Viewer"
    assert DEFAULT_GRAFANA_ROLE == "Viewer"


def test_mapping_covers_all_three_viator_roles_and_no_extras() -> None:
    """The mapping should have exactly 3 entries — one per VIATOR role.

    A 4th entry would be either a typo or an undocumented role we'd want
    a code reviewer to push back on.
    """
    assert set(VIATOR_TO_GRAFANA_ROLE.keys()) == {
        "platform_admin",
        "content_manager",
        "end_user",
    }


def test_all_target_values_are_valid_grafana_roles() -> None:
    """Grafana's auth.proxy plugin only accepts Admin/Editor/Viewer.

    A typo like 'admin' (lowercase) or 'editor' would silently fall back
    to whatever Grafana's default role is, NOT raise an error — which is
    exactly the kind of silent-misconfiguration this test catches.
    """
    grafana_valid_roles = {"Admin", "Editor", "Viewer"}
    for viator_role, grafana_role in VIATOR_TO_GRAFANA_ROLE.items():
        assert grafana_role in grafana_valid_roles, (
            f"VIATOR role {viator_role!r} maps to {grafana_role!r}, which is "
            f"not a valid Grafana role. Valid: {sorted(grafana_valid_roles)}."
        )
