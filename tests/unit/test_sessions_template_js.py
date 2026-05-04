"""Trip-wire tests for the inline JavaScript in app/templates/admin/sessions.html.

Static analysis only — we don't execute the JS, just check that helpers
referenced from the template are also defined in the template. The
v0.1.16 bug was a `ReferenceError: escHTML is not defined` because the
helper was called from `napLoadCatalogues` / `napRenderResult` but never
defined; the resulting unhandled promise rejection silently broke the NAP
catalogue dropdown for every operator.

These tests exist purely so that the next templating refactor can't
quietly drop a helper definition without CI noticing.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TEMPLATE = Path(__file__).resolve().parents[2] / "app" / "templates" / "admin" / "sessions.html"


@pytest.fixture(scope="module")
def template_text() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


# ────────────────────────────────────────────────────────────────────────
# Helpers must be defined wherever they're called.
#
# We list every JS helper we want to pin. If the template ever references
# a name that isn't defined right here, the browser throws at first call
# and the symptom is some UI element silently breaking — exactly what we
# want CI to catch instead of an operator on the VPS.
# ────────────────────────────────────────────────────────────────────────
JS_HELPERS = [
    "escAttr",
    "escHTML",  # added in v0.1.16 after the NAP-loading-forever bug
]


@pytest.mark.parametrize("name", JS_HELPERS)
def test_helper_is_defined(template_text: str, name: str) -> None:
    """Every helper in JS_HELPERS must have a `function <name>(` declaration."""
    pattern = rf"function\s+{re.escape(name)}\s*\("
    assert re.search(pattern, template_text), (
        f"sessions.html references `{name}(...)` but no `function {name}(...)` "
        f"declaration exists. The browser will throw ReferenceError at first "
        f"call site — see v0.1.16 changelog for the canonical example."
    )


def test_escHTML_actually_escapes_the_dangerous_chars(template_text: str) -> None:
    """Belt-and-braces: the implementation must cover the five HTML-special
    characters. A regression that drops, say, `<` from the substitution map
    would re-introduce XSS via NAP catalogue names."""
    # Pull just the function body (greedy to the matching `}`).
    match = re.search(
        r"function\s+escHTML\s*\([^)]*\)\s*\{(.*?)^}",
        template_text,
        re.MULTILINE | re.DOTALL,
    )
    assert match, "couldn't locate escHTML body — did the declaration shape change?"
    body = match.group(1)
    for ch, ent in (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"), ('"', "&quot;"), ("'", "&#39;")):
        assert ent in body, (
            f"escHTML's substitution map is missing `{ch} → {ent}` — keeping the "
            f"five HTML-special characters is the whole point of the helper"
        )
