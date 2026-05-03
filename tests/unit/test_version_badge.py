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
    t.templates.env.globals["viator_version"] = "V0.0.0-render-test"

    tpl = t.templates.env.get_template("_base.html")
    html = tpl.render(current_user=None)

    assert 'class="ver"' in html, "version badge <span> missing from _base.html"
    assert "V0.0.0-render-test" in html, "viator_version global not rendered"


def test_display_version_capitalises_leading_v(monkeypatch: pytest.MonkeyPatch) -> None:
    """v0.1.11 cosmetic: a git tag like `v0.1.10` must display as `V0.1.10`
    in the UI badge. The /healthz/version endpoint and OCI label keep the
    canonical lowercase form for tooling — only the badge gets capitalised."""
    # `settings` is a module-level singleton; patching the env var alone
    # doesn't help because earlier tests in the suite have already imported
    # it. Patch the live attribute too so the templating reload picks it up.
    from app import settings as settings_module

    monkeypatch.setattr(settings_module.settings, "viator_version", "v0.1.10")

    import app.templating as t

    importlib.reload(t)

    assert t.templates.env.globals["viator_version"] == "V0.1.10"


def test_display_version_handles_already_capitalised() -> None:
    """`main`, `dev`, `Main` etc. should be safe to put through the
    capitaliser — no double-capitalisation, no crash on empty."""
    from app.templating import _display_version

    assert _display_version("dev") == "Dev"
    assert _display_version("main") == "Main"
    assert _display_version("V0.1.10") == "V0.1.10"  # already capital
    assert _display_version("") == ""


def test_base_template_renders_role_badge_when_logged_in() -> None:
    """v0.1.11: when current_user is set, the header should include a role
    badge with a class matching the role string so CSS can colour it."""
    from dataclasses import dataclass

    import app.templating as t

    importlib.reload(t)

    @dataclass
    class _StubUser:
        username: str = "alice@example.org"
        role: str = "platform_admin"

    tpl = t.templates.env.get_template("_base.html")
    html = tpl.render(current_user=_StubUser())

    # Class on the badge matches role (used by .role.platform_admin CSS).
    assert 'class="role platform_admin"' in html
    # Underscore in role display is replaced with space for readability.
    assert "platform admin" in html
