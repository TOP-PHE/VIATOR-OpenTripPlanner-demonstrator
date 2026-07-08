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


def test_viator_trip_card_rank_badge_uses_array_position(template_text: str):
    """Regression lock (adversarial review of PR #221): once a cell's
    trips are fetched filtered-and-sorted by departure time
    (_fetch_trips_by_search's depart_at handling) rather than VIATOR's
    rank_in_response order, badging by `trip.rank` produced a
    non-sequential list (e.g. #7, #2, #9 top to bottom). The card must
    take a 2nd `idx` param (trips.map(renderModalViatorTripCard) already
    passes it) and badge by array position instead.

    No "old pattern is gone" negative check here — the dead (never
    called) legacy `renderTripCard` function still has the old
    `trip.rank`-based badge and isn't in this fix's scope, so a blanket
    file-wide absence check would be a false positive against it."""
    assert re.search(
        r"function\s+renderModalViatorTripCard\s*\(\s*trip\s*,\s*idx\s*\)", template_text
    ), "renderModalViatorTripCard must accept an idx param to badge by array position"
    assert 'cov-trip-rank">#${idx + 1}' in template_text


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


# ─────────────────── Re-run link coord-guard regression ───────────────────
# Bug reported on v0.1.43.28: clicking the modal's "Re-run live in the
# journey UI" link opens /journey with From/To/Depart all empty. Root
# cause: HUBS entries with null/undefined lat or lon interpolated raw
# into the URL template produce `from_lat=undefined&from_lon=undefined`.
# journey.html's setPair() bails on empty lat/lon but treats the LITERAL
# STRING "undefined" as truthy, writes it into the hidden field, and
# then parseFloat("undefined") at submit yields NaN — presenting as
# "search does nothing / all fields empty" to the operator. Fix: coerce
# each coord via Number()/Number.isFinite() and gate the whole link on
# all four coords being non-null.


def test_rerun_link_coerces_coords_before_interpolation(template_text: str):
    """The Re-run link's coord interpolation must go through a `coord()`
    helper that returns null for non-finite values, so a stale/partial
    hub row (lat=null / lon=undefined) never produces the literal string
    "undefined" in the /journey URL. Without this the operator sees
    From/To/Depart all empty after Re-run because journey.html's
    downstream setPair() misreads "undefined" as truthy and then chokes
    at submit-time parseFloat."""
    assert "Number.isFinite(Number(v))" in template_text, (
        "Missing the Number.isFinite(Number(v)) coord-coercer in "
        "openDetailModal's Re-run link block. Without it, a hub row "
        "with null/undefined lat or lon produces `from_lat=undefined` "
        "in the URL — reported v0.1.43.28 bug where Re-run opens "
        "/journey with all fields empty."
    )
    # The four coord-nullness checks must gate the link so a bad hub
    # coord makes the link disappear rather than deep-linking to a
    # broken URL.
    for check in ("oLat !== null", "oLon !== null", "dLat !== null", "dLon !== null"):
        assert check in template_text, (
            f"Re-run link must gate on `{check}` so a hub row with a "
            f"null/undefined coord makes the link disappear rather "
            f"than producing `from_lat=undefined` in the /journey URL."
        )


def test_rerun_link_uses_coerced_coord_vars_not_raw_hub_fields(template_text: str):
    """Belt-and-braces: after the coerce, the URL template must use the
    numeric-typed locals (oLat/oLon/dLat/dLon) and NOT the raw
    orig.lat/orig.lon/dest.lat/dest.lon paths. If a refactor puts the
    raw fields back into the href, the coerce is defeated and the
    "undefined" leak returns."""
    assert "from_lat=${oLat}" in template_text, (
        "Re-run link href must interpolate the coerced `oLat` local, "
        "not the raw `orig.lat` — otherwise the coord-coercer is a "
        "dead-store and the null/undefined leak returns at the URL."
    )
    assert "from_lon=${oLon}" in template_text
    assert "to_lat=${dLat}" in template_text
    assert "to_lon=${dLon}" in template_text
    # Negative: the raw-field interpolation MUST NOT appear in the
    # rerunLink template. Scoped to /journey? so unrelated orig.lat
    # usage elsewhere in the file isn't caught.
    assert "/journey?from_lat=${orig.lat}" not in template_text, (
        "Regression: Re-run link is back to interpolating raw "
        "`orig.lat`. Use the coerced `oLat` local instead — see the "
        "v0.1.43.28 empty-prefill bug."
    )


# ─────────────────── Re-run link UIC passthrough (Scope A) ───────────────────
# Coverage hubs carry no UIC field today (no column on NetworkCoverageHub,
# no join to master_stations) — so orig.uic/dest.uic are always undefined
# and oUic/dUic below always resolve to ''. This is intentionally a no-op
# in behaviour: it future-proofs the Re-run link so the moment hub rows
# gain a `uic` value, journey.html's existing UIC-based routing engages
# automatically without a second template change.


def test_rerun_link_includes_uic_params(template_text: str):
    """The Re-run link must send from_uic/to_uic alongside from_lat/
    from_lon so journey.html's setPair() can populate the hidden
    #from-uic/#to-uic fields — those feed UIC-based station routing on
    the backend, not just coordinate routing. Missing these params
    means coverage-hub re-runs always fall back to raw-coordinate
    matching even when a hub eventually carries a UIC."""
    assert "from_uic=${oUic}" in template_text, (
        "Re-run link href is missing &from_uic=${oUic} — without it, "
        "journey.html's prefill never populates #from-uic, and the "
        "backend always falls back to coordinate-only routing for "
        "coverage-hub re-runs even once hubs carry a UIC."
    )
    assert "to_uic=${dUic}" in template_text


def test_rerun_link_uic_locals_use_value_or_empty_string_convention(template_text: str):
    """oUic/dUic must follow the `s.uic || ''` convention already
    established in journey.html's autocomplete-pick handler — never
    interpolate a raw possibly-undefined orig.uic/dest.uic directly,
    which would repeat the exact "undefined"-string bug the coord
    guard above was written to fix."""
    assert "orig && orig.uic ? encodeURIComponent(orig.uic) : ''" in template_text, (
        "Missing (or changed) the oUic derivation — must guard on "
        "`orig && orig.uic` before encoding, else a hub with no uic "
        "field produces the literal string 'undefined' in the URL "
        "again (same failure class as the v0.1.43.28 coord bug)."
    )
    assert "dest && dest.uic ? encodeURIComponent(dest.uic) : ''" in template_text


# ───────── wrong-day run warning banner (reference_date ⟂ depart_at) ─────────
# A run whose K-slot grid searched a different calendar day than its own
# depart_at names produces real-looking numbers for the wrong date. The
# cell modal renders leg times as HH:MM with no date, so the operator has
# no way to notice. Only reachable on rows created before create_run
# started deriving reference_date from depart_at.


def test_run_window_warning_helper_is_defined(template_text: str):
    assert "function buildRunWindowWarning(run)" in template_text


def test_run_window_warning_is_gated_on_the_backend_flag(template_text: str):
    """The banner must key off `depart_at_outside_window` (computed
    server-side against the run's resolved window) rather than
    re-deriving the comparison in JS, where the run's timezone isn't
    available."""
    assert "run.depart_at_outside_window" in template_text


def test_run_window_warning_is_actually_rendered(template_text: str):
    """Defined-but-never-called is the classic way a warning silently
    stops warning."""
    assert "${buildRunWindowWarning(run)}" in template_text


def test_run_window_warning_css_present(template_text: str):
    """Without the rule the banner renders as unstyled text indistinguishable
    from the stats subline it sits under."""
    assert ".cov-window-warning" in template_text


def test_no_operator_facing_copy_still_promises_tomorrow(template_text: str):
    """The form hint said "(default: tomorrow)" — which was true, and was
    the bug. A first pass fixed that string but left a SECOND copy of the
    same promise two lines below ("Defaults: 00:00-24:00 UTC tomorrow"),
    and the guard only pinned the string that had been replaced.

    So assert on the *concept*: no operator-facing sentence in the Advanced
    panel may say the reference date defaults to tomorrow. The word only
    survives inside JS comments explaining the historical bug, which the
    operator never reads — strip comment lines before checking.
    """
    visible = " ".join(ln for ln in template_text.splitlines() if not ln.strip().startswith("//"))
    assert "tomorrow" not in visible.lower(), (
        "operator-facing copy still promises a 'tomorrow' default for "
        "reference_date; create_run now anchors it on depart_at"
    )
    assert "(default: the Departure date)" in template_text


# ───────── "Show walk legs" OFF must recompute the card header ─────────
# The header used to show door-to-door times unconditionally, so hiding the
# walk rows left an OeBB itinerary reading "08:10 -> 10:48 - 2h38" above a
# single rail leg running 08:10 -> 10:06. (That 42 min is Eurostar's check-in
# allowance at Bruxelles-Midi International, which HAFAS models as an egress
# WALK -- real data, deliberately kept.) With walks hidden the header must
# describe the remaining legs: first train's departure -> last train's arrival.


def test_modal_non_transit_modes_matches_the_backend_set(template_text: str):
    """A bare `mode !== 'WALK'` would keep a MOTIS TRANSFER leg (and a HAFAS
    section with mode: null) visible while excluding it from the recomputed
    header — the exact divergence `_NON_TRANSIT_MODES` exists to prevent, and
    the bug PR #222 fixed once already in journey.html."""
    assert "const MODAL_NON_TRANSIT_MODES = new Set(['WALK', 'TRANSFER', ''])" in template_text
    assert "function isModalTransitLeg(leg)" in template_text
    assert ".toUpperCase()" in template_text


def test_both_leg_renderers_hide_via_the_canonical_predicate(template_text: str):
    """Rows hidden must be exactly the rows excluded from the recompute, or
    the header contradicts the legs again — on one column only."""
    for fn in ("renderModalLeg", "renderModalOebbLeg"):
        start = template_text.find(f"function {fn}(")
        assert start != -1, f"{fn} missing"
        body = template_text[start : template_text.find("\n}\n", start)]
        assert "isModalTransitLeg(leg)" in body, (
            f"{fn} must derive its walk-leg class from isModalTransitLeg, not `mode === 'WALK'`"
        )


def test_transit_bounds_helper_and_overnight_guard(template_text: str):
    start = template_text.find("function modalTransitBounds(")
    assert start != -1, "modalTransitBounds missing"
    body = template_text[start : template_text.find("\n}\n", start)]
    assert "filter(isModalTransitLeg)" in body
    # HAFAS anchors every leg to the connection's single `date` and
    # `_hafas_time_to_utc_iso` drops the day-roll prefix, so an arrival after
    # midnight parses EARLIER than its departure. Without the roll-forward the
    # header would show a negative duration on every NightJet.
    assert "seconds += 86400" in body, "overnight roll-forward guard missing"
    assert "Number.isFinite(seconds)" in body


def test_header_emits_both_variants_and_toggle_swaps_them(template_text: str):
    assert 'data-walks="1"' in template_text  # door-to-door
    assert 'data-walks="0"' in template_text  # train only
    start = template_text.find("function applyModalShowWalks(")
    body = template_text[start : template_text.find("\n}\n", start)]
    assert "[data-walks]" in body, "applyModalShowWalks must swap the header variants"
    assert "MODAL_SHOW_WALKS" in body


def test_initial_render_bakes_in_the_persisted_preference(template_text: str):
    """`applyModalShowWalks()` only runs on a toggle flip, never after the
    first modal render — so the header variants must carry their initial
    [hidden] from MODAL_SHOW_WALKS, exactly as the leg rows do. Otherwise an
    operator with walks persisted OFF opens the modal to a door-to-door header
    above already-hidden legs."""
    start = template_text.find("function modalTripHeadTimes(")
    assert start != -1, "modalTripHeadTimes missing"
    body = template_text[start : template_text.find("\n}\n", start)]
    assert "MODAL_SHOW_WALKS ? '' : ' hidden'" in body
    assert "MODAL_SHOW_WALKS ? ' hidden' : ''" in body


def test_both_cards_recompute_with_their_own_leg_field_names(template_text: str):
    """VIATOR legs carry `departure`/`arrival`; OeBB VerifyLegs carry
    `dep_utc`/`arr_utc`. One missing call would silently fix a single column."""
    assert "modalTransitBounds(trip.legs, 'departure', 'arrival')" in template_text
    assert "modalTransitBounds(it.legs, 'dep_utc', 'arr_utc')" in template_text
