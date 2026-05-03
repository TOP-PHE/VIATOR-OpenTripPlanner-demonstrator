"""Shared Jinja2Templates singleton.

Two reasons this module exists:

1. **DRY** — `app/main.py` and `app/api/pages.py` both render templates from
   `app/templates/`. Duplicating the `Jinja2Templates(directory=...)` call
   meant any global (custom filter, context variable) had to be set in two
   places. Now there's one.

2. **Globals available to every template** — anything we register here on
   `templates.env.globals[...]` is reachable from any template without each
   route having to remember to put it in the context dict. Currently used
   for `viator_version` so the header badge in `_base.html` renders on
   every page (login, journey search, admin, error pages…) without each
   route remembering to pass it.

If you need a per-render variable (something that depends on the request),
keep using the explicit context dict — globals are for things that are
constant for the lifetime of the process.
"""

from __future__ import annotations

from fastapi.templating import Jinja2Templates

from .settings import settings

templates = Jinja2Templates(directory="app/templates")


# ── Globals ───────────────────────────────────────────────────────────────
# Reachable from any template as bare `{{ viator_version }}`.
# Baked into the web image at build time via `ARG VIATOR_VERSION` in
# docker/web/Dockerfile; can be overridden at runtime via env var
# (set in docker-compose.yml from the .env file).
#
# Display tweak (v0.1.11): capitalise the leading character so a git tag
# like `v0.1.10` shows as `V0.1.10` in the UI badge — operators asked for
# the capital V to match the brand wordmark `VIATOR`. Pure cosmetics; the
# `/healthz/version` endpoint and OCI label keep the canonical lowercase
# form so tooling-side parsers / docker tags aren't disturbed.
def _display_version(raw: str) -> str:
    if not raw:
        return raw
    return raw[0].upper() + raw[1:]


templates.env.globals["viator_version"] = _display_version(settings.viator_version)
