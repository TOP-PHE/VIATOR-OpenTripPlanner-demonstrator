"""VIATOR role → Grafana role mapping (audit #14 Phase 2.1).

Pure-data module with no imports. Lives separately from
`app.api.auth.routes` so unit tests can import the mapping without
pulling in the FastAPI app + SQLAlchemy + psycopg stack.

Grafana's auth.proxy plugin (`Role:X-Forwarded-Role`) reads the user role
from a request header and applies it to auto-signed-up users. The header
value MUST be one of Grafana's 3-tier vocabulary: "Admin" / "Editor" /
"Viewer". VIATOR's roles use different names — this dict bridges them:

  platform_admin   →  Admin     (full Grafana org admin: invite users,
                                  manage data sources / plugins)
  content_manager  →  Editor    (create / edit dashboards but no user
                                  or data-source admin)
  end_user         →  Viewer    (read-only)

Defaults to Viewer for any unknown role — least-privilege fallback that
keeps an unrecognised role from accidentally getting Editor/Admin access.
"""

from __future__ import annotations

VIATOR_TO_GRAFANA_ROLE: dict[str, str] = {
    "platform_admin": "Admin",
    "content_manager": "Editor",
    "end_user": "Viewer",
}

DEFAULT_GRAFANA_ROLE = "Viewer"


def viator_role_to_grafana(viator_role: str) -> str:
    """Translate a VIATOR role to its Grafana equivalent.

    Returns `DEFAULT_GRAFANA_ROLE` (Viewer) for unknown / null roles —
    the least-privilege fallback so an unrecognised value can't escalate
    privilege accidentally.
    """
    return VIATOR_TO_GRAFANA_ROLE.get(viator_role, DEFAULT_GRAFANA_ROLE)
