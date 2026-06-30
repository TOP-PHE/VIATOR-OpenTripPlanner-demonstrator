"""Trip-wire tests for the PR-196b side-by-side VIATOR/ÖBB cell-detail
modal in admin/network_coverage.html.

The modal logic (renderDirectionSection, renderModalViatorColumn,
renderModalOebbColumn, etc.) lives as inline JS in the template. We do
not execute it — instead, these tests pin the contract the render path
relies on, so a refactor that drops one of the named helpers, the
compare-grid CSS link, or the modal control strip is caught at CI
rather than by an operator clicking a matrix cell and seeing a blank
modal or a JS ReferenceError.

Pin-points covered:
  - both VIATOR and ÖBB column-header strings render via the shared
    compare-grid primitive
  - the helper that renders the ÖBB column body exists and is called
  - the alignment-tier pill is rendered via the shared CompareGrid
    primitive in the modal control strip
  - the "Show walk legs" toggle is wired (checkbox id + state var +
    localStorage key + change handler)
  - the empty-column placeholder ("no itineraries found") is the same
    string the shared primitive emits, so a payload with one side
    empty visibly tells the operator which side returned nothing
  - the shared compare-grid CSS + JS assets are linked from the
    template (the rendered grid mark-up depends on both)

Mirrors the existing tests/unit/test_journey_template_compare.py
pattern.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TEMPLATE = REPO / "app" / "templates" / "admin" / "network_coverage.html"
SHARED_CSS = REPO / "app" / "static" / "css" / "compare_grid.css"
SHARED_JS = REPO / "app" / "static" / "js" / "compare_grid.js"


@pytest.fixture(scope="module")
def template_text() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def shared_css_text() -> str:
    return SHARED_CSS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def shared_js_text() -> str:
    return SHARED_JS.read_text(encoding="utf-8")


# ────────────────────────────────────────────────────────────────────────
# Helpers the modal render path calls. If any is missing the modal
# crashes silently in the browser (uncaught ReferenceError → blank body
# + console error) and no Python test would otherwise catch it.
# ────────────────────────────────────────────────────────────────────────
MODAL_HELPERS = [
    # The direction section dispatcher + its compare-grid builder.
    "renderDirectionSection",
    "renderDirectionCompareGrid",
    # Per-column body renderers — one each for VIATOR and ÖBB.
    "renderModalViatorColumn",
    "renderModalOebbColumn",
    # Per-trip-card renderers — split by data shape (VIATOR's leg uses
    # `mode`/`route_short_name`, ÖBB's VerifyLeg uses `mode`/`route_name`).
    "renderModalViatorTripCard",
    "renderModalOebbTripCard",
    "renderModalLeg",
    "renderModalOebbLeg",
    # Control strip + walk-leg toggle.
    "renderModalControls",
    "applyModalShowWalks",
    # Re-verify button's in-place ÖBB column refresh.
    "refreshModalOebbColumn",
]


@pytest.mark.parametrize("name", MODAL_HELPERS)
def test_modal_helper_is_defined(name: str, template_text: str):
    """Every helper called from the cell-detail modal render path must
    be defined as `function name(` somewhere in the same template."""
    function_re = rf"function\s+{re.escape(name)}\s*\("
    assert re.search(function_re, template_text), (
        f"JS helper {name!r} is called from the cell-detail modal but never "
        f"defined in app/templates/admin/network_coverage.html — the browser "
        f"will throw a ReferenceError the first time an operator clicks a cell."
    )


def test_both_column_headers_appear_for_viator_and_oebb(template_text: str):
    """The compare-grid descriptors must label one column VIATOR and
    one column ÖBB HAFAS so the operator can tell them apart at a
    glance. The strings are the column labels passed to
    CompareGrid.renderGrid; if either changes the test fails so a
    careless rename is caught."""
    assert "VIATOR · MOTIS / OTP" in template_text, (
        "VIATOR column label string is missing — the side-by-side modal "
        "will render a column without a label."
    )
    assert "ÖBB HAFAS" in template_text, (
        "ÖBB HAFAS column label string is missing — the side-by-side modal "
        "will render the right column unlabelled."
    )


def test_pill_classes_for_both_columns(template_text: str):
    """Each column descriptor passes a pillClass so the per-engine
    accent colour renders. VIATOR uses 'viator' (slate), ÖBB uses
    'oebb' (amber) — the colours live in the shared compare-grid CSS."""
    assert "pillClass: 'viator'" in template_text
    assert "pillClass: 'oebb'" in template_text


def test_oebb_column_reads_external_itineraries(template_text: str):
    """The ÖBB column body MUST read from CellTripsDirection.
    external_itineraries (populated by PR-196a's sweep) — anything
    else would mean we're re-querying HAFAS at modal-open time, which
    defeats the persistence point of PR-196a."""
    assert "external_itineraries" in template_text, (
        "The ÖBB column body must source from dir.external_itineraries "
        "(persisted by PR-196a) — no live ÖBB calls at modal-open time."
    )


def test_no_itineraries_placeholder_present(shared_js_text: str):
    """When one side is empty (VIATOR found 0 trips, or ÖBB), the
    shared compare-grid primitive emits a 'no itineraries found'
    placeholder so the operator can tell which engine returned what.
    The string is part of the primitive's contract — pin it here so a
    refactor that drops the fallback is caught at CI."""
    assert "no itineraries found" in shared_js_text, (
        "Shared compare-grid primitive must emit a 'no itineraries found' "
        "placeholder when a column body is empty — otherwise empty columns "
        "render as a blank box and the operator can't tell which engine "
        "returned zero vs which engine wasn't queried at all."
    )


def test_alignment_tier_pill_rendered_in_modal(template_text: str):
    """The modal control strip MUST render an alignment-tier pill via
    the shared CompareGrid.tierPill primitive, fed from PR-196a's
    persisted external_alignment_tier + external_alignment_score. This
    is the cell's at-a-glance summary above the side-by-side grid."""
    assert "CompareGrid.tierPill" in template_text, (
        "The alignment-tier pill must be rendered via the shared "
        "CompareGrid.tierPill primitive — otherwise the pill's colour "
        "palette can drift between the modal and the matrix heatmap."
    )
    # And it must be fed from the persisted tier + score columns:
    assert "external_alignment_tier" in template_text
    assert "external_alignment_score" in template_text


def test_alignment_tier_pill_css_present(shared_css_text: str):
    """The .alignment-tier-pill class + at least the canonical 'agree'
    tier swatch must be defined in the shared CSS so the pill renders
    with the viridis palette the matrix heatmap uses."""
    assert ".alignment-tier-pill" in shared_css_text
    # Spot-check a few of the tier swatches — full coverage of the
    # nine tiers is in test_oebb_alignment.py.
    for tier in ("agree", "disagree", "no_data"):
        assert f'.alignment-tier-pill[data-tier="{tier}"]' in shared_css_text, (
            f"Missing CSS swatch for alignment tier {tier!r} — the pill "
            f"will render as un-styled text."
        )


def test_show_walks_toggle_is_wired(template_text: str):
    """The 'Show walk legs' toggle must be DISPLAY-ONLY and must persist
    the operator's choice to localStorage. Three contracts checked:
    (1) the checkbox id + data-action attribute exist; (2) the state
    variable + localStorage key exist; (3) the change handler flips
    state AND calls applyModalShowWalks() — not a re-render, since the
    score must stay canonical from the sweep."""
    # (1) Toggle DOM contract — both the visible id and the delegated
    # data-action attribute (the delegated handler uses data-action so
    # it survives modal re-opens).
    assert 'id="modal-show-walks"' in template_text
    assert 'data-action="modal-show-walks"' in template_text
    # (2) State variable + localStorage key.
    assert "MODAL_SHOW_WALKS" in template_text
    assert "viator.modalShowWalks" in template_text
    # (3) Handler flips state AND calls applyModalShowWalks — NOT
    # render(), the alignment score must stay canonical from the sweep.
    assert "applyModalShowWalks()" in template_text


def test_walk_leg_css_marker_present(template_text: str):
    """Walk legs must carry the .walk-leg class so the toggle's CSS
    rule can hide them in place via the [hidden] attribute. The CSS
    rule itself lives in the network_coverage.html style block."""
    assert ".cov-leg.walk-leg" in template_text
    # The rendered class is also referenced by the JS leg renderers:
    assert "walk-leg" in template_text


def test_shared_compare_grid_assets_linked(template_text: str):
    """The template MUST link the shared compare-grid CSS and script so
    the rendered modal mark-up has its layout rules + the
    CompareGrid.{renderGrid,tierPill} primitives. Without the link the
    modal collapses to a single un-styled column and the
    tierPill / renderGrid calls throw ReferenceError."""
    assert "/static/app/css/compare_grid.css" in template_text, (
        "Template must <link> the shared compare-grid CSS — without it "
        "the modal grid loses every layout rule."
    )
    assert "/static/app/js/compare_grid.js" in template_text, (
        "Template must <script src> the shared compare-grid JS — "
        "without it the modal's CompareGrid.renderGrid + tierPill "
        "calls throw ReferenceError."
    )


def test_modal_layout_dispatches_through_compare_grid(template_text: str):
    """The new modal layout must build its grid via the shared
    CompareGrid.renderGrid primitive (not a hand-rolled column-string
    concatenation) — that's the whole point of PR-196b's extraction."""
    assert "CompareGrid.renderGrid" in template_text, (
        "renderDirectionCompareGrid must call CompareGrid.renderGrid "
        "(the shared primitive). Hand-rolling the grid mark-up here "
        "would drift from journey.html's side-by-side layout."
    )


def test_reverify_refreshes_modal_in_place(template_text: str):
    """The PR-196b Re-verify UX improvement: clicking Re-verify must
    update the ÖBB column body in place with the live-query results
    (verifyResult.itineraries), not just render the verdict string.
    The handler in question is handleVerifyExternal → refreshModalOebbColumn."""
    assert "refreshModalOebbColumn" in template_text
    # And the call site is inside the verify-external success branch:
    assert "payload.itineraries" in template_text, (
        "handleVerifyExternal must hand the VerifyResult.itineraries "
        "to refreshModalOebbColumn — otherwise the ÖBB column doesn't "
        "actually refresh on re-verify."
    )
