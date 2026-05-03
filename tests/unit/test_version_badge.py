"""Version stamp surface — Settings field, env override, Jinja global.

Purpose: lock in the contract that the badge shown next to the VIATOR logo
matches `Settings.viator_version`. Two failure modes we're guarding against:

1. Someone removes `viator_version` from `Settings` → header badge silently
   renders empty (Jinja swallows `{{ undefined }}`).
2. Someone refactors `app/templating.py` and forgets to register the
   global → same silent-empty failure on every page.

We don't TestClient-render here (that needs Postgres) — a focused unit
check is enough; integration tests in `tests/integration/test_pages.py`
already prove the templates render end-to-end.
"""

from __future__ import annotations

import importlib

import pytest


def test_settings_default_version_is_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bare `Settings()` (no env, no .env) must yield 'dev' so the badge
    is never blank in local/test runs."""
    monkeypatch.delenv("VIATOR_VERSION", raising=False)
    from app.settings import Settings

    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.viator_version == "dev"


def test_settings_reads_viator_version_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Docker image bakes `ENV VIATOR_VERSION=v0.1.x`; pydantic-settings
    must pick that up case-insensitively."""
    monkeypatch.setenv("VIATOR_VERSION", "v9.9.9-test")
    from app.settings import Settings

    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.viator_version == "v9.9.9-test"


def test_jinja_env_exposes_viator_version_global() -> None:
    """The shared `templates` singleton must register `viator_version` as a
    Jinja global so `{{ viator_version }}` in `_base.html` resolves on every
    page without each route having to thread it through the context dict."""
    # Force a re-import in case a previous test mutated module state.
    import app.templating as t

    importlib.reload(t)

    assert "viator_version" in t.templates.env.globals
    # Whatever the value, it must be a non-empty string — the badge is meant
    # to be eyeball-visible. Empty/None would render an awkward blank pill.
    val = t.templates.env.globals["viator_version"]
    assert isinstance(val, str)
    assert val  # non-empty


def test_base_template_renders_version_in_lockup() -> None:
    """Render the `_base.html` lockup fragment in isolation and assert the
    version badge is present. This catches anyone deleting the `<span class="ver">`
    from the header without noticing the contract was on it."""
    import app.templating as t

    importlib.reload(t)
    t.templates.env.globals["viator_version"] = "v0.0.0-render-test"

    tpl = t.templates.env.get_template("_base.html")
    html = tpl.render(current_user=None)

    assert 'class="ver"' in html, "version badge <span> missing from _base.html"
    assert "v0.0.0-render-test" in html, "viator_version global not rendered"
