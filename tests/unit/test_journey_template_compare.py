"""Trip-wire tests for the P2 MOTIS comparison view in journey.html.

The comparison view (two-column OTP vs MOTIS layout) lives as inline JS
in the template. We don't execute it — these checks just pin the helpers
the render path depends on, so a careless refactor that deletes one
gets caught by CI rather than by an operator hitting "Search" and seeing
a blank page or a console ReferenceError.

Mirrors the existing `test_sessions_template_js.py` pattern.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TEMPLATE = Path(__file__).resolve().parents[2] / "app" / "templates" / "journey.html"


@pytest.fixture(scope="module")
def template_text() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


# ────────────────────────────────────────────────────────────────────────
# Helpers the comparison render path calls. If any of these is missing
# from the template, the comparison view crashes silently in the browser
# (uncaught ReferenceError → blank results pane) and no Python test would
# otherwise catch it. The fix is just to define the helper here — this
# list is the contract.
# ────────────────────────────────────────────────────────────────────────
COMPARISON_HELPERS = [
    "_engineForSession",
    "_shouldRenderComparison",
    "_enginesForTrip",
    "_renderComparisonCell",
    "renderComparisonGrid",
    # P2 follow-up: "Compare trains only" toggle helpers. Same trip-wire
    # rule — if any of these is undefined, the toggle silently breaks.
    "_trainOnlySignature",
    "_railOnlyDurationSec",
    # P2 follow-up #2 (2026-06-21): mode normaliser. OTP says `RAIL`,
    # MOTIS says `REGIONAL_RAIL`/`HIGHSPEED_RAIL` — same train, different
    # GTFS route_type. The pairing key MUST collapse these together or
    # the toggle does nothing visible.
    "_normalizeMode",
    # Sonar refactor: cognitive-complexity split of renderComparisonGrid
    # into pure helpers (S3776). Each one is named here so a future
    # refactor that inlines them back into a monolithic function will
    # at least notice the test breaking.
    "_usableTrips",
    "_pairKey",
    "_assignBestToBucket",
    "_bucketsForGrid",
    "_cellForBucket",
    "_renderToggleControls",
]


@pytest.mark.parametrize("name", COMPARISON_HELPERS)
def test_comparison_helper_is_defined(name: str, template_text: str):
    """Every helper called from the render path must be defined as either
    `function name(` or `const name =` in the same template."""
    function_re = rf"function\s+{re.escape(name)}\s*\("
    const_re = rf"const\s+{re.escape(name)}\s*="
    assert re.search(function_re, template_text) or re.search(const_re, template_text), (
        f"JS helper {name!r} is called from the comparison view but never defined "
        f"in app/templates/journey.html — the browser will throw a ReferenceError "
        f"the first time a fanout with both engines lands."
    )


def test_render_branches_on_should_render_comparison(template_text: str):
    """The main `render(payload)` must call `_shouldRenderComparison`
    (the dispatch point that picks comparison-grid vs. flat-list)."""
    assert "_shouldRenderComparison(payload)" in template_text


def test_comparison_grid_appears_in_innerhtml_template(template_text: str):
    """The innerHTML template literal must interpolate the grid into the
    output, otherwise the grid is built but never shown."""
    # Quick check: somewhere after `el.innerHTML = ` the literal must
    # reference `${comparisonGrid}`. Not pinning exact position, just
    # presence — the v0.1.41 federated section did the same.
    assert "${comparisonGrid}" in template_text


def test_comparison_grid_css_present(template_text: str):
    """CSS classes the grid relies on must be defined in the template's
    <style> block. Without these, the grid renders as a vertical pile of
    cards instead of two columns."""
    for css_class in (".compare-grid", ".compare-header", ".compare-cell"):
        assert css_class in template_text, f"Missing CSS rule for {css_class}"


def test_engine_dropdown_is_present_in_form(template_text: str):
    """The dropdown that gates the comparison view must still be in the
    form template. (Caught a class of refactor bugs where the JS still
    looks for `document.getElementById('engine')` but the input was
    renamed/removed.)"""
    assert 'id="engine"' in template_text
    # And the JS payload-builder reads it:
    assert "document.getElementById('engine')" in template_text


def test_compare_trains_only_toggle_is_wired(template_text: str):
    """The 'Compare trains only' toggle and its state variable must
    both exist, and the change handler must call render() so the grid
    rebuilds on flip without a fresh fetch."""
    # State variable that the cell + grid helpers read:
    assert "_COMPARE_TRAINS_ONLY" in template_text
    # Toggle HTML id (rendered inside the comparison grid):
    assert 'id="compare-trains-only"' in template_text
    # The change handler must flip the state AND re-render:
    assert "document.getElementById('compare-trains-only')" in template_text
    assert "render(_LAST_PAYLOAD)" in template_text


def test_compare_controls_css_present(template_text: str):
    """The toggle's container styling must be defined — without it the
    checkbox renders as a bare unstyled input inside the results pane."""
    for css_class in (".compare-controls", ".compare-toggle"):
        assert css_class in template_text, f"Missing CSS rule for {css_class}"


# ──────────────────── feat/hafas-journey-comparison ────────────────────
# Trip-wire tests for the second reference-comparison engine. Same
# template-pin pattern as the OJP block above — if any of these
# checkboxes / panels / JS helpers gets renamed or dropped, the
# HAFAS comparison silently breaks in the browser.


def test_hafas_checkbox_gated_by_jinja_flag(template_text: str):
    """The HAFAS checkbox MUST sit inside a Jinja `if hafas_comparison_enabled`
    block — without that gate the input renders even when the platform
    has the feature disabled, leading to confusing "this did nothing"
    moments for operators."""
    assert 'id="compare-hafas"' in template_text
    assert "if hafas_comparison_enabled" in template_text


def test_hafas_checkbox_included_in_search_body(template_text: str):
    """The submit handler must read the checkbox state into the JSON
    fanout body as `compare_hafas`. Without this the server never
    learns the operator opted in and the panel never renders."""
    assert "compare_hafas:" in template_text
    assert "document.getElementById('compare-hafas')" in template_text


def test_hafas_render_function_and_panel_wired(template_text: str):
    """The render function must exist AND be called from the main
    render() flow, AND the panel must land in the innerHTML output —
    skipping any link in the chain produces a payload-with-trips that
    silently renders blank below VIATOR's results."""
    assert "function renderHafasReference" in template_text
    assert "renderHafasReference(payload.hafas_reference)" in template_text
    assert "${hafasPanel}" in template_text


def test_hafas_panel_css_present(template_text: str):
    """The hafas-ref-panel + hafas-ref-card classes must have CSS
    rules — without them the panel renders as un-bordered chaos
    visually indistinguishable from VIATOR's native cards."""
    assert ".hafas-ref-panel" in template_text
    assert ".hafas-ref-card" in template_text


def test_coverage_tooltip_icons_present_for_both_engines(template_text: str):
    """Both comparison checkboxes carry a "?" tooltip icon so operators
    can see scope at a glance. The icons use the `.compare-ref-info`
    class with a `data-tooltip` attribute (CSS-only hover reveal)."""
    assert ".compare-ref-info" in template_text  # CSS rule
    # Both engines have a tooltip string in their data-tooltip attr:
    assert "Swiss OJP covers:" in template_text
    assert "ÖBB HAFAS covers:" in template_text
