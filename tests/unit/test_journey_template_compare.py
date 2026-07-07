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
# PR-196b — the compare-grid CSS + JS primitives were lifted out of
# journey.html into shared static assets so the network-coverage cell
# modal can re-use the same N-column primitive. The template still has
# to <link>/<script> them in; the CSS rules + the renderGrid primitive
# itself live in these files now.
SHARED_CSS = Path(__file__).resolve().parents[2] / "app" / "static" / "css" / "compare_grid.css"
SHARED_JS = Path(__file__).resolve().parents[2] / "app" / "static" / "js" / "compare_grid.js"


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
    # reference the comparison-grid slot. PR-194 introduced the
    # `comparisonGridSlot` indirection (so the side-by-side fork can
    # blank the slot when it owns the layout) — either name is OK as
    # long as the grid is woven into the innerHTML somewhere.
    assert "${comparisonGrid}" in template_text or "${comparisonGridSlot}" in template_text


def test_comparison_grid_css_present(template_text: str, shared_css_text: str):
    """CSS classes the grid relies on must be defined in the shared
    stylesheet AND the template must <link> to it. Without these, the
    grid renders as a vertical pile of cards instead of two columns.

    PR-196b — the rules moved out of journey.html into the shared CSS
    file so the network-coverage modal can re-use them. The template
    pin is now "link to the shared file" + "shared file defines the
    rules"."""
    assert "/static/app/css/compare_grid.css" in template_text, (
        "journey.html must <link> to the shared compare-grid CSS — "
        "without it the grid loses every layout rule."
    )
    for css_class in (".compare-grid", ".compare-header", ".compare-cell"):
        assert css_class in shared_css_text, f"Missing CSS rule for {css_class}"


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


def test_compare_controls_css_present(shared_css_text: str):
    """The toggle's container styling must be defined — without it the
    checkbox renders as a bare unstyled input inside the results pane.

    PR-196b — the rule moved into the shared compare-grid CSS file."""
    for css_class in (".compare-controls", ".compare-toggle"):
        assert css_class in shared_css_text, f"Missing CSS rule for {css_class}"


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


# ───────────────────────────── PR-194 ─────────────────────────────────
# Trip-wire tests for the two journey-search UI changes bundled in PR-194:
# (1) honest labels on the trains-only toggle, and (2) opt-in side-by-side
# comparison columns. Pure template-string assertions — no rendering or
# JS execution, just pin the strings the operator + the JS handlers
# depend on so a careless refactor that deletes one is caught at CI.


def test_pr194_honest_label_replaces_trains_only(template_text: str):
    """The 'Compare trains only' label was misleading — the underlying
    filter only skipped WALK legs and kept bus/tram/coach. PR-194
    renames it to 'Compare excluding walk legs' so the UI string
    matches what the code does."""
    assert "Compare excluding walk legs" in template_text
    # The old misleading label must NOT survive — operators saw
    # bus-included results under a 'trains only' header for months.
    assert "Compare trains only" not in template_text


def test_pr194_rail_duration_label_renamed_to_transit(template_text: str):
    """Inside the comparison cell the per-itinerary duration label
    was 'X rail' (summed all non-walk legs, not just rail). Renamed
    to 'X transit' so the displayed quantity matches the function
    that computes it."""
    # Belt-and-braces: the literal `rail · ${dur(` substring is what
    # used to render in the cell — it should be gone.
    assert "rail · ${dur(bestRow.duration_seconds)} total" not in template_text
    assert "transit · ${dur(bestRow.duration_seconds)} total" in template_text


def test_pr194_side_by_side_checkbox_present(template_text: str):
    """The opt-in toggle for the new N-column layout must be rendered
    in the form. id is the contract between the JS init hook and the
    label hook, so both the user-visible label AND the id are pinned."""
    assert "Side-by-side comparison" in template_text
    assert 'id="compare-side-by-side"' in template_text


def test_pr194_side_by_side_wired_to_render(template_text: str):
    """The checkbox's change handler must persist the new state to
    localStorage AND call render(_LAST_PAYLOAD) so the layout flips
    without re-fetching. The fork point in render() must read the
    `_shouldRenderSideBySide` predicate."""
    assert "_COMPARE_SIDE_BY_SIDE" in template_text
    assert "_shouldRenderSideBySide" in template_text
    assert "renderSideBySideGrid" in template_text
    assert "viator.compareSideBySide" in template_text  # localStorage key


def test_pr194_side_by_side_grid_css_present(shared_css_text: str):
    """CSS for the side-by-side column grid must be defined — without
    it the columns wrap, the empty-column placeholder is unstyled,
    and the per-source pill accent colours fall back to grey.

    PR-196b — the rules moved into the shared compare-grid CSS so
    they're authoritatively defined once. We assert on the shared
    file rather than the template body."""
    assert "compare-grid-refs" in shared_css_text
    # Per-source pill accents (new in PR-194):
    assert ".engine-pill.viator" in shared_css_text
    assert ".engine-pill.ojp" in shared_css_text
    assert ".engine-pill.hafas" in shared_css_text
    # CSS variable that scales the grid to N columns:
    assert "--compare-cols" in shared_css_text


# ───────────────────── v0.1.43.25 regression guard ─────────────────────
# The `wireSideBySideToggle` IIFE runs synchronously at module
# evaluation and reads `_COMPARE_SIDE_BY_SIDE` + `_SIDE_BY_SIDE_STORAGE_KEY`.
# Because those are block-scoped (`let` / `const`), declaring them
# AFTER the IIFE puts them in the temporal dead zone — the read throws
# `ReferenceError: Cannot access '_COMPARE_SIDE_BY_SIDE' before
# initialization`, which aborts the rest of the inline <script> and
# leaves the form-submit handler unregistered. The user-visible
# symptom (v0.1.43.25) was: click Search → page reloads with empty
# inputs, no fetch dispatched. These tests pin the order so a future
# move-things-around refactor can't silently reintroduce the bug.


def test_side_by_side_storage_key_declared_before_iife(template_text: str):
    """`const _SIDE_BY_SIDE_STORAGE_KEY = ...` must appear in the
    template text BEFORE the `wireSideBySideToggle` IIFE that consumes
    it. Otherwise the IIFE hits the TDZ at module-load."""
    decl_idx = template_text.find("const _SIDE_BY_SIDE_STORAGE_KEY")
    iife_idx = template_text.find("(function wireSideBySideToggle()")
    assert decl_idx != -1, "_SIDE_BY_SIDE_STORAGE_KEY declaration missing"
    assert iife_idx != -1, "wireSideBySideToggle IIFE missing"
    assert decl_idx < iife_idx, (
        "TDZ regression: `const _SIDE_BY_SIDE_STORAGE_KEY` must be "
        "declared before the wireSideBySideToggle IIFE that consumes "
        "it — otherwise the IIFE throws ReferenceError on load and "
        "the form-submit handler never registers (symptom: Search "
        "reloads the page with empty inputs). See v0.1.43.25 bug."
    )


def test_side_by_side_state_var_declared_before_iife(template_text: str):
    """`let _COMPARE_SIDE_BY_SIDE = ...` must appear in the template
    text BEFORE the `wireSideBySideToggle` IIFE that consumes it.
    Same TDZ rationale as the storage-key test above."""
    decl_idx = template_text.find("let _COMPARE_SIDE_BY_SIDE")
    iife_idx = template_text.find("(function wireSideBySideToggle()")
    assert decl_idx != -1, "_COMPARE_SIDE_BY_SIDE declaration missing"
    assert iife_idx != -1, "wireSideBySideToggle IIFE missing"
    assert decl_idx < iife_idx, (
        "TDZ regression: `let _COMPARE_SIDE_BY_SIDE` must be declared "
        "before the wireSideBySideToggle IIFE that reads it on the "
        "first line of its body (cb.checked = _COMPARE_SIDE_BY_SIDE). "
        "If you move the declaration below the IIFE the page silently "
        "breaks: the IIFE throws ReferenceError, aborts the rest of "
        "the <script>, leaves the form-submit handler unregistered, "
        "and clicking Search reloads /journey with empty inputs."
    )


def test_side_by_side_state_initialised_only_once(template_text: str):
    """Belt-and-braces: there must be EXACTLY ONE declaration of each
    side-by-side state binding in the template. A second `let`/`const`
    declaration (e.g. left behind after a refactor) would throw a
    SyntaxError at parse time — page fails to load at all — but a
    careless cut-and-paste could also produce two unrelated names
    pointing at the same string. Pinning the count keeps the file
    honest."""
    storage_decls = template_text.count("const _SIDE_BY_SIDE_STORAGE_KEY")
    state_decls = template_text.count("let _COMPARE_SIDE_BY_SIDE")
    assert storage_decls == 1, (
        f"Expected exactly one `const _SIDE_BY_SIDE_STORAGE_KEY` "
        f"declaration, found {storage_decls}. A duplicate `let`/`const` "
        f"in the same scope is a SyntaxError; remove the duplicate."
    )
    assert state_decls == 1, (
        f"Expected exactly one `let _COMPARE_SIDE_BY_SIDE` declaration, "
        f"found {state_decls}. A duplicate `let`/`const` in the same "
        f"scope is a SyntaxError; remove the duplicate."
    )


# ───────────────────── v0.1.43.26 SBS regression guards ─────────────────────
# Two PR-194 regressions surfaced on the v0.1.43.26 release once the TDZ
# hotfix (PR #196) restored the Search button: (1) the SBS VIATOR column
# always read "VIATOR · MOTIS / OTP" regardless of the engine filter the
# operator picked, and (2) the "Compare excluding walk legs" toggle
# disappeared entirely in SBS mode when the operator picked a single
# engine — `_shouldRenderComparison` requires BOTH engines and was the
# toggle's only host. These tests pin the fix so a future refactor of
# `renderSideBySideGrid` or the dispatch site can't silently bring the
# bugs back.


def test_sbs_viator_column_label_resolved_via_helper(template_text: str):
    """The VIATOR column label inside `renderSideBySideGrid` must come
    from the `_viatorColumnLabel(payload)` helper, NOT a hardcoded
    literal — otherwise picking `Engine=MOTIS only` still reads
    `VIATOR · MOTIS / OTP` in the column header even though only MOTIS
    sessions executed. The helper inspects `payload.executions` so it
    stays accurate to what the server actually ran."""
    # The helper must be defined.
    assert "function _viatorColumnLabel(" in template_text, (
        "Missing `_viatorColumnLabel` helper — needed to derive the SBS "
        "VIATOR column label from `payload.executions` so the header "
        "reflects the engine filter the operator actually picked."
    )
    # And invoked inside renderSideBySideGrid, specifically for the
    # 'viator' column descriptor's `label`. We're checking the exact
    # call shape so a future refactor that drops the helper invocation
    # would fail this test.
    assert "label: _viatorColumnLabel(payload)" in template_text, (
        "Regression: the VIATOR column label in renderSideBySideGrid "
        "is no longer using `_viatorColumnLabel(payload)`. The static "
        "literal `'VIATOR · MOTIS / OTP'` misleads the operator when "
        "they picked Engine=MOTIS-only or Engine=OTP-only — the column "
        "header reads MOTIS/OTP even though only one engine ran."
    )
    # Belt-and-braces: the literal `'VIATOR · MOTIS / OTP'` should still
    # exist (it is the helper's fallback label for the both-engines /
    # zero-engines case), but it must NOT appear in the same column-
    # descriptor object as `pillClass: 'viator'`. A simple proxy: the
    # literal should NOT be on the same line as `pillClass: 'viator'`.
    for line in template_text.splitlines():
        if "pillClass: 'viator'" in line:
            assert "'VIATOR · MOTIS / OTP'" not in line, (
                f"Regression: VIATOR column descriptor hardcodes the "
                f"combined label. Use `_viatorColumnLabel(payload)` "
                f"instead. Offending line: {line.strip()!r}"
            )


def test_sbs_render_includes_walk_legs_toggle(template_text: str):
    """The walk-leg toggle (`_renderToggleControls()`) must be invoked
    from `renderSideBySideGrid` so the toggle appears in SBS mode even
    when the operator picked a single engine.

    Before this fix, `_renderToggleControls` was called only from
    `renderComparisonGrid`, which `_shouldRenderComparison` gates on
    BOTH engines being present. Single-engine SBS therefore had no
    toggle anywhere on the page — the operator lost the ability to
    filter walk legs out of the comparison entirely."""
    # Find renderSideBySideGrid + the next function definition; assert
    # `_renderToggleControls()` appears between them. We can't trivially
    # parse JS in Python, but a substring search within the function's
    # source range is a reliable enough pin.
    sbs_start = template_text.find("function renderSideBySideGrid(")
    assert sbs_start != -1, "renderSideBySideGrid function missing"
    # Look for the next top-level `function ` definition after the SBS
    # body's body (skip past the function's own header).
    next_fn = template_text.find("\nfunction ", sbs_start + len("function renderSideBySideGrid("))
    assert next_fn != -1, "Could not bound renderSideBySideGrid body"
    sbs_body = template_text[sbs_start:next_fn]
    assert "_renderToggleControls()" in sbs_body, (
        "Regression: renderSideBySideGrid no longer injects the walk-"
        "leg toggle. Single-engine SBS would lose the 'Compare "
        "excluding walk legs' checkbox entirely (it used to live only "
        "in renderComparisonGrid, which requires both engines). The "
        "toggle must be prepended in the SBS wrapper so it covers "
        "every column regardless of which engine ran."
    )


def test_sbs_dispatch_does_not_double_inject_walk_toggle(template_text: str):
    """Belt-and-braces: the `render()` dispatch site must hand plain
    `cards` (not `comparisonGrid || cards`) into `renderSideBySideGrid`.

    If the dispatch passes `comparisonGrid` the SBS first column
    re-embeds the entire comparison grid — which itself starts with the
    walk-leg toggle — producing TWO `compare-trains-only` checkboxes
    on the page. The change handler at `document.getElementById(
    'compare-trains-only')` only sees the first one, so the second
    silently no-ops. Pinning the dispatch shape keeps the invariant
    'exactly one toggle in SBS mode' visible at the test layer."""
    # The dispatch line we care about appears exactly once in the file.
    bad = "renderSideBySideGrid(payload, comparisonGrid || cards)"
    good = "renderSideBySideGrid(payload, cards)"
    assert bad not in template_text, (
        "Regression: dispatch re-embeds the comparison grid into the "
        "SBS first column, which duplicates the walk-leg toggle. Use "
        "`renderSideBySideGrid(payload, cards)` instead."
    )
    assert good in template_text, (
        "Dispatch must call `renderSideBySideGrid(payload, cards)` so "
        "the SBS wrapper owns the single walk-leg toggle without "
        "competing nested copies from comparisonGrid."
    )


# ───────────────── v0.1.44 VIATOR-vs-ÖBB row alignment ─────────────────
# When ÖBB HAFAS is the sole reference column, VIATOR and ÖBB trips are
# paired onto the same visual row by matching `first_transit_leg_departure_utc`
# instead of rendering two independent, unaligned column bodies. Same
# trip-wire rationale as the rest of this file: these are inline JS
# helpers with no Python-executable path, so a careless refactor that
# drops one silently breaks the browser render with no server-side signal.

PAIRING_HELPERS = [
    "_firstTransitLeg",
    "_firstTransitLegDepUtc",
    "_firstTransitLegLabel",
    "_pairViatorAndOebbTrips",
    "_trainIdMismatchNote",
    "renderViatorOebbPairedGrid",
    "_viatorTripCardHtml",
    "_oebbTripCardHtml",
]


@pytest.mark.parametrize("name", PAIRING_HELPERS)
def test_pairing_helper_is_defined(name: str, template_text: str):
    """Every helper the VIATOR-vs-ÖBB paired grid depends on must be
    defined as either `function name(` or `const name =`."""
    function_re = rf"function\s+{re.escape(name)}\s*\("
    const_re = rf"const\s+{re.escape(name)}\s*="
    assert re.search(function_re, template_text) or re.search(const_re, template_text), (
        f"JS helper {name!r} is called from renderViatorOebbPairedGrid but "
        f"never defined in app/templates/journey.html — the browser will "
        f"throw a ReferenceError the first time a HAFAS-only SBS search runs."
    )


def test_paired_grid_dispatched_when_hafas_is_sole_reference(template_text: str):
    """`renderSideBySideGrid` must fork to `renderViatorOebbPairedGrid`
    specifically when ÖBB HAFAS is the only reference column present —
    OJP-involved configurations (OJP alone, or OJP+HAFAS together) keep
    the original independent-columns `CompareGrid.renderGrid` path,
    since pairing is only defined for the two-source VIATOR/ÖBB case."""
    sbs_start = template_text.find("function renderSideBySideGrid(")
    assert sbs_start != -1, "renderSideBySideGrid function missing"
    next_fn = template_text.find("\nfunction ", sbs_start + len("function renderSideBySideGrid("))
    assert next_fn != -1, "Could not bound renderSideBySideGrid body"
    sbs_body = template_text[sbs_start:next_fn]
    assert "renderViatorOebbPairedGrid(payload)" in sbs_body, (
        "renderSideBySideGrid no longer forks to the paired-row grid — "
        "VIATOR and ÖBB trips would render as two independent, unaligned "
        "column bodies again (the original user-reported bug)."
    )
    assert "_refsForPairing.length === 1" in sbs_body and "'hafas'" in sbs_body, (
        "The paired-grid fork must be scoped to the HAFAS-is-sole-reference "
        "case — without this guard, OJP-involved SBS layouts would either "
        "lose their columns or crash looking for a non-existent HAFAS "
        "reference."
    )


def test_pairing_matches_on_first_transit_leg_departure(template_text: str):
    """`_pairViatorAndOebbTrips` must key the match on
    `first_transit_leg_departure_utc` — the field every engine client
    computes on a consistent UTC clock basis — not on wall-clock local
    time or duration, which can drift a minute apart between sources."""
    assert "first_transit_leg_departure_utc" in template_text
    fn_start = template_text.find("function _pairViatorAndOebbTrips(")
    assert fn_start != -1, "_pairViatorAndOebbTrips function missing"
    next_fn = template_text.find("\nfunction ", fn_start + len("function _pairViatorAndOebbTrips("))
    body = template_text[fn_start : next_fn if next_fn != -1 else fn_start + 2000]
    assert "_firstTransitLegDepUtc" in body, (
        "_pairViatorAndOebbTrips must match rows via _firstTransitLegDepUtc "
        "(first_transit_leg_departure_utc) — the shared cross-engine match "
        "key computed server-side by every engine client."
    )


def test_unmatched_trips_from_both_sides_still_render(template_text: str):
    """Trips with no counterpart on the other side must still appear as
    their own row (with a 'not found by' placeholder on the empty side,
    via the shared `_pairedGridMissingCell` helper) rather than being
    silently dropped — an operator needs to see every itinerary either
    engine found."""
    assert "function _pairedGridMissingCell(" in template_text
    assert "not found by" in template_text
    assert "_pairedGridMissingCell('VIATOR')" in template_text
    assert "_pairedGridMissingCell('ÖBB HAFAS')" in template_text


def test_train_mismatch_warning_wired(template_text: str):
    """A matched row (same departure/arrival) whose two sources disagree
    on the train label must render a spanning warning note — this is
    the exact behaviour the operator asked for after noticing VIATOR and
    ÖBB report the same physical service under different train IDs."""
    assert "function _trainIdMismatchNote(" in template_text
    assert "train-mismatch-row" in template_text
    assert "train-mismatch-note" in template_text
    # Must actually be invoked from the paired-grid row builder.
    fn_start = template_text.find("function renderViatorOebbPairedGrid(")
    assert fn_start != -1, "renderViatorOebbPairedGrid function missing"
    next_fn = template_text.find(
        "\nfunction ", fn_start + len("function renderViatorOebbPairedGrid(")
    )
    body = template_text[fn_start : next_fn if next_fn != -1 else fn_start + 3000]
    assert "_trainIdMismatchNote(" in body, (
        "renderViatorOebbPairedGrid must call _trainIdMismatchNote per "
        "matched row so the warning actually renders, not just be defined."
    )


def test_train_mismatch_css_present(shared_css_text: str):
    """CSS for the mismatch note must be defined in the shared stylesheet
    — without it the warning renders as unstyled text with no visual
    distinction from the trip cards around it."""
    assert ".train-mismatch-row" in shared_css_text
    assert ".train-mismatch-note" in shared_css_text


# ─────────────── v0.1.44 paired-grid review-fix regression guards ───────────────
# A code-review pass on PR #222 (the pairing feature above) found several
# real bugs before merge. These tests pin the fixes so a later refactor
# can't silently reintroduce them.


def test_first_transit_leg_skips_transfer_and_empty_mode(template_text: str):
    """`_firstTransitLeg` must exclude TRANSFER and empty/missing mode,
    not just WALK — mirrors the server-side `_NON_TRANSIT_MODES` set
    (app/journey/trip_normalize.py) that `first_transit_leg_departure_utc`
    itself is built on. HAFAS emits `mode: null` for TRSF-like filler
    sections; a WALK-only filter would pick that filler leg instead of
    the real transit leg, reading an empty train label off it and
    silently suppressing the train-mismatch warning."""
    assert "_NON_TRANSIT_MODES" in template_text
    fn_start = template_text.find("function _firstTransitLeg(")
    assert fn_start != -1, "_firstTransitLeg function missing"
    next_fn = template_text.find("\nfunction ", fn_start + len("function _firstTransitLeg("))
    body = template_text[fn_start : next_fn if next_fn != -1 else fn_start + 500]
    assert "_NON_TRANSIT_MODES.has(" in body, (
        "_firstTransitLeg regressed to a bare `mode !== 'WALK'` check — "
        "it must consult _NON_TRANSIT_MODES (WALK, TRANSFER, '') so a "
        "TRANSFER/null-mode filler leg doesn't get mistaken for the real "
        "first transit leg."
    )


def test_paired_grid_renders_empty_state_when_both_sides_empty(template_text: str):
    """When VIATOR and ÖBB both genuinely found zero itineraries (a valid
    'ok' response, not an error), the grid must say so explicitly rather
    than rendering two bare column headers with no body — which reads as
    a broken page rather than a real empty result."""
    fn_start = template_text.find("function renderViatorOebbPairedGrid(")
    assert fn_start != -1, "renderViatorOebbPairedGrid function missing"
    next_fn = template_text.find(
        "\nfunction ", fn_start + len("function renderViatorOebbPairedGrid(")
    )
    body = template_text[fn_start : next_fn if next_fn != -1 else fn_start + 3000]
    assert "rows.length" in body and "no itineraries found" in body, (
        "renderViatorOebbPairedGrid must fall back to an explicit "
        "'no itineraries found' placeholder when the paired rows list "
        "is empty, instead of rendering a body-less grid."
    )


def test_paired_grid_toggle_not_rendered_for_inert_control(template_text: str):
    """The HAFAS-pairing fork in `renderSideBySideGrid` must NOT render
    `_renderToggleControls()` — that toggle flips `_COMPARE_TRAINS_ONLY`,
    which only `renderComparisonGrid` (OTP-vs-MOTIS) reads.
    `_pairViatorAndOebbTrips` always matches on the first transit leg
    regardless of the flag, so showing the checkbox here is an inert
    control that looks broken to an operator who flips it and sees
    nothing change."""
    sbs_start = template_text.find("function renderSideBySideGrid(")
    assert sbs_start != -1, "renderSideBySideGrid function missing"
    next_fn = template_text.find("\nfunction ", sbs_start + len("function renderSideBySideGrid("))
    sbs_body = template_text[sbs_start:next_fn]
    pair_fork = sbs_body.find("renderViatorOebbPairedGrid(payload)")
    assert pair_fork != -1, "HAFAS-pairing fork missing from renderSideBySideGrid"
    return_line_start = sbs_body.rfind("return", 0, pair_fork)
    return_line = sbs_body[
        return_line_start : pair_fork + len("renderViatorOebbPairedGrid(payload)") + 5
    ]
    assert "_renderToggleControls()" not in return_line, (
        "Regression: the HAFAS-pairing fork renders _renderToggleControls() "
        "again — the 'Compare excluding walk legs' checkbox is inert in "
        "this view (the pairing logic never reads _COMPARE_TRAINS_ONLY) "
        "and misleads the operator into thinking it does something here."
    )


def test_paired_grid_rows_sorted_chronologically(template_text: str):
    """Unmatched ÖBB-only rows must not simply be appended after every
    matched/VIATOR-ordered row — they must be merged in chronologically
    (mirrors the sortKey approach `_bucketsForGrid` uses for the
    OTP-vs-MOTIS grid), so an early ÖBB-only itinerary doesn't render
    below VIATOR's later departures."""
    assert "function _pairedGridRowSortKey(" in template_text
    fn_start = template_text.find("function _pairViatorAndOebbTrips(")
    assert fn_start != -1, "_pairViatorAndOebbTrips function missing"
    next_fn = template_text.find("\nfunction ", fn_start + len("function _pairViatorAndOebbTrips("))
    body = template_text[fn_start : next_fn if next_fn != -1 else fn_start + 3000]
    assert "rows.sort(" in body and "_pairedGridRowSortKey" in body, (
        "_pairViatorAndOebbTrips no longer sorts the merged row list by "
        "departure time — unmatched ÖBB-only rows would render bunched "
        "at the end regardless of their actual departure time."
    )


def test_paired_grid_viator_index_threaded_not_recomputed(template_text: str):
    """The VIATOR cell's `data-trip-index` must come from the index
    `_pairViatorAndOebbTrips` already tracked while iterating (`viatorIndex`
    on each row), not a fresh `viatorTrips.indexOf(row.viator)` — an O(n)
    scan per row (O(n²) for the whole grid) to reconstruct information
    the pairing step already had for free."""
    fn_start = template_text.find("function renderViatorOebbPairedGrid(")
    assert fn_start != -1, "renderViatorOebbPairedGrid function missing"
    next_fn = template_text.find(
        "\nfunction ", fn_start + len("function renderViatorOebbPairedGrid(")
    )
    body = template_text[fn_start : next_fn if next_fn != -1 else fn_start + 3000]
    assert "row.viatorIndex" in body, (
        "renderViatorOebbPairedGrid must use row.viatorIndex (threaded "
        "through by _pairViatorAndOebbTrips) instead of re-deriving the "
        "index via viatorTrips.indexOf(row.viator)."
    )
    assert "viatorTrips.indexOf(" not in body, (
        "Regression: renderViatorOebbPairedGrid re-introduced the O(n) "
        "viatorTrips.indexOf(row.viator) lookup — use row.viatorIndex "
        "instead."
    )


def test_train_mismatch_css_specificity_beats_compare_cell_padding(shared_css_text: str):
    """The mismatch-note div also carries `.compare-cell`, which pulls in
    `.compare-grid.compare-grid-refs > .compare-cell { padding: 0 }`
    (3 classes) and `.compare-grid .compare-cell { min-height: 4.6rem }`
    — both equal-or-higher specificity than a plain `.train-mismatch-row`
    rule, so they'd silently win regardless of source order and leave the
    note flush with no padding plus a large dead min-height gap. The
    override must match the same selector shape (4 classes) to
    unconditionally win."""
    assert (
        ".compare-grid.compare-grid-refs > .compare-cell.train-mismatch-row" in shared_css_text
    ), (
        "The mismatch-row padding/min-height override must use a selector "
        "at least as specific as `.compare-grid.compare-grid-refs > "
        ".compare-cell` (the rule it needs to beat) — a bare "
        "`.train-mismatch-row` rule loses that specificity fight and the "
        "banner renders with dead space below and no breathing room above."
    )


def test_paired_grid_breaks_same_instant_ties_on_arrival(template_text: str):
    """On a high-frequency corridor, two distinct ÖBB itineraries can
    share the exact same first-transit departure instant (same first
    train, different onward transfer). A pure first-come-first-matched
    scan risks pairing a VIATOR trip to the wrong one of the two. When
    more than one candidate matches the departure instant, prefer the
    candidate whose arrival also agrees before falling back to the
    first-found candidate."""
    fn_start = template_text.find("function _pairViatorAndOebbTrips(")
    assert fn_start != -1, "_pairViatorAndOebbTrips function missing"
    next_fn = template_text.find("\nfunction ", fn_start + len("function _pairViatorAndOebbTrips("))
    body = template_text[fn_start : next_fn if next_fn != -1 else fn_start + 3000]
    assert "candidates.length > 1" in body and "arrival_at" in body, (
        "_pairViatorAndOebbTrips no longer breaks same-departure-instant "
        "ties on arrival time — a shared first-leg departure instant "
        "between two distinct ÖBB itineraries could silently pair the "
        "wrong one to a VIATOR trip."
    )


# ───────────────── v0.1.45 "possible duplicate" badge ─────────────────
# MOTIS can report a single physically-coupled/portioned train (e.g.
# SNCF's "601" + "601A", split for part of the journey but one driver)
# as two independent itineraries sharing the exact same departure and
# arrival. No block_id or trip-composition data exists anywhere in the
# pipeline to confirm the two really are one physical service, so this
# is a same-engine, display-only heuristic: flag every trip whose
# (departure_at, arrival_at) pair was already seen earlier in the list.


def test_duplicate_detector_helper_is_defined(template_text: str):
    assert "function _duplicateDepartureArrivalIndices(" in template_text


def test_viator_card_accepts_duplicate_flag_and_renders_badge(template_text: str):
    """`_viatorTripCardHtml` must accept the duplicate flag as its third
    parameter and conditionally render `.dup-badge` from it — otherwise
    the detector's output never reaches the DOM."""
    fn_start = template_text.find("function _viatorTripCardHtml(")
    assert fn_start != -1, "_viatorTripCardHtml function missing"
    next_fn = template_text.find("\nfunction ", fn_start + len("function _viatorTripCardHtml("))
    body = template_text[fn_start : next_fn if next_fn != -1 else fn_start + 2000]
    assert "isDuplicate" in body, (
        "_viatorTripCardHtml no longer accepts an isDuplicate parameter — "
        "the possible-duplicate badge can never be shown."
    )
    assert "dup-badge" in body, (
        "_viatorTripCardHtml no longer renders the .dup-badge element — "
        "even with isDuplicate wired in, nothing would render."
    )


def test_duplicate_flag_wired_into_flat_list_render(template_text: str):
    """The main `render(payload)` cards map must compute the duplicate
    set from `payload.trips` and pass it per-card — otherwise the flat
    (non-SBS) list never shows the badge even though the paired grid
    does."""
    render_start = template_text.find("function render(payload) {")
    assert render_start != -1, "render(payload) function missing"
    next_fn = template_text.find("\nfunction ", render_start + len("function render(payload) {"))
    body = template_text[render_start : next_fn if next_fn != -1 else render_start + 4000]
    assert "_duplicateDepartureArrivalIndices(payload.trips)" in body, (
        "render(payload) no longer computes the duplicate-index set from "
        "payload.trips — the flat card list would lose the possible-"
        "duplicate badge entirely."
    )
    assert "_viatorTripCardHtml(t, i, " in body, (
        "render(payload)'s cards map no longer passes a third argument "
        "to _viatorTripCardHtml — the duplicate flag is computed but "
        "never threaded into the card markup."
    )


def test_duplicate_flag_wired_into_paired_grid(template_text: str):
    """`renderViatorOebbPairedGrid` must also compute and pass the
    duplicate flag for its VIATOR column — the SBS paired grid is a
    SEPARATE render path from the flat list and doesn't inherit the
    flat list's wiring."""
    fn_start = template_text.find("function renderViatorOebbPairedGrid(")
    assert fn_start != -1, "renderViatorOebbPairedGrid function missing"
    next_fn = template_text.find(
        "\nfunction ", fn_start + len("function renderViatorOebbPairedGrid(")
    )
    body = template_text[fn_start : next_fn if next_fn != -1 else fn_start + 3000]
    assert "_duplicateDepartureArrivalIndices(viatorTrips)" in body, (
        "renderViatorOebbPairedGrid no longer computes the duplicate-"
        "index set — the SBS paired grid would lose the possible-"
        "duplicate badge."
    )
    assert "_viatorTripCardHtml(row.viator, row.viatorIndex, " in body, (
        "renderViatorOebbPairedGrid's VIATOR cell no longer passes the "
        "duplicate flag into _viatorTripCardHtml."
    )


def test_duplicate_badge_css_present(template_text: str):
    """`.dup-badge` styling must be defined — without it the badge
    renders as unstyled inline text with no visual warning cue."""
    assert ".dup-badge" in template_text
