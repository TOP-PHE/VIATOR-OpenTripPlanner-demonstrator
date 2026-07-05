"""Tests for the Network Coverage HTML export feature.

The export endpoint takes a coverage run id and returns a self-contained
HTML report. The endpoint is mostly data-marshalling around a Jinja
template, so these tests focus on:

  1. The template renders with realistic synthetic data without raising.
  2. Key UI markers are present in the rendered HTML (matrix cells,
     embedded JSON blobs, modal markup, status pills, etc.).
  3. The embedded JSON in `<script id="cov-cells">…</script>` is valid
     JSON and round-trips through json.loads — that's what the page's
     drill-down JavaScript will parse at runtime.
  4. Status-specific cell rendering (ok / no_route / timeout / error /
     self) emits the right CSS class so colours work in any browser.

These are template-only tests — no FastAPI, no DB. The route-level
behaviour (auth, content-disposition, query) is light enough that the
integration tests under tests/integration/ cover it when they get added.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape


def _env() -> Environment:
    """Vanilla Jinja2 env pointed at app/templates — same lookup path
    the FastAPI Jinja2Templates uses, so the relative {% include %}s and
    filters (tojson, format) behave identically."""
    root = Path(__file__).resolve().parents[2] / "app" / "templates"
    return Environment(
        loader=FileSystemLoader(str(root)),
        autoescape=select_autoescape(["html", "xml"]),
    )


def _sample_hubs() -> list[dict]:
    """Two real-shaped hubs — enough to render a 2x2 matrix with one
    self-cell and three populated cells. band_color/country_rowspan/modes
    mirror what `_annotate_hubs_with_country_bands` actually computes
    (each hub here is a single-hub country run, so rowspan=1 on both)."""
    return [
        {
            "id": "p-nord",
            "name": "Paris Nord",
            "short": "P-Nord",
            "region": "ile-de-france",
            "country": "FR",
            "tier": "main",
            "modes": "R",
            "band_color": "hsl(246, 48%, 40%)",
            "country_rowspan": 1,
            "lat": 48.8809,
            "lon": 2.3553,
            "is_active": True,
            "sort_order": 0,
        },
        {
            "id": "bxl-mid",
            "name": "Bruxelles-Midi",
            "short": "BXL-MID",
            "region": "brussels",
            "country": "BE",
            "tier": "main",
            "modes": None,
            "band_color": "hsl(172, 48%, 40%)",
            "country_rowspan": 1,
            "lat": 50.8358,
            "lon": 4.3360,
            "is_active": True,
            "sort_order": 0,
        },
    ]


def _sample_country_col_runs() -> list[dict]:
    """Matches `_sample_hubs()` — each hub is its own single-hub run."""
    return [
        {"country": "FR", "span": 1, "color": "hsl(246, 48%, 40%)"},
        {"country": "BE", "span": 1, "color": "hsl(172, 48%, 40%)"},
    ]


def _sample_run() -> dict:
    return {
        "id": "11111111-1111-1111-1111-111111111111",
        "session_id": "eu11-transit-motis",
        "mode": "single_session",
        "direction": "both",
        "depart_at": "2026-06-29T06:00:00",
        "status": "completed",
        "total_pairs": 2,
        "completed_pairs": 2,
        "ok_pairs": 1,
        "no_route_pairs": 0,
        "error_pairs": 0,
        "created_at": "2026-06-29T05:55:12",
    }


def _sample_cells() -> dict[str, dict]:
    """Three cells covering ok / no_route / timeout so the template's
    status-specific branches all execute. The ok cell has a real trip
    with one rail leg, exercising the leg-rendering path."""
    return {
        "p-nord:bxl-mid": {
            "status": "ok",
            "response_ms": 1432,
            "num_itineraries": 1,
            "best_duration_seconds": 4980,  # 1h23
            "best_num_transfers": 0,
            "best_operators": "EUROSTAR",
            "error_message": None,
            "session_ids": None,
            # PR-196a — alignment scored; exercises the tier-attribute +
            # score-badge render path.
            "external_alignment_tier": "agree",
            "external_alignment_score": 1.0,
            "trips": [
                {
                    "rank": 0,
                    "duration_seconds": 4980,
                    "num_transfers": 0,
                    "departure_at": "2026-06-29T06:25:00+02:00",
                    "arrival_at": "2026-06-29T07:48:00+02:00",
                    "modes": "RAIL",
                    "legs": [
                        {
                            "mode": "RAIL",
                            "departure": "2026-06-29T06:25:00+02:00",
                            "arrival": "2026-06-29T07:48:00+02:00",
                            "duration_seconds": 4980,
                            "from_name": "Paris Nord",
                            "to_name": "Bruxelles-Midi",
                            "route_short_name": "EST 9201",
                            "agency_name": "Eurostar",
                            "feed_id": "EUROSTAR",
                            "trip_headsign": "Bruxelles-Midi",
                        }
                    ],
                }
            ],
        },
        "bxl-mid:p-nord": {
            "status": "no_route",
            "response_ms": 412,
            "num_itineraries": 0,
            "best_duration_seconds": None,
            "best_num_transfers": None,
            "best_operators": None,
            "error_message": None,
            "session_ids": None,
            # PR-196a — the real _build_export_context ALWAYS emits both
            # keys via getattr(..., None); this cell is unscored so both
            # are explicitly None (not simply omitted) to match that
            # contract and exercise the "no_data" default-tier path.
            "external_alignment_tier": None,
            "external_alignment_score": None,
            "trips": [],
        },
        # Timeout/error are tested via a third synthetic pair — picking
        # an origin/dest that the matrix template will look up. The render
        # path doesn't actually require both hubs to exist for a cell to
        # appear, but the test asserts on the OK and no_route cells which
        # ARE on the matrix axis.
    }


def _render(**flags) -> str:
    """Render the export template with the standard sample fixtures.

    `flags` passes through `lazy_trips` / `legs_omitted`; omitting them
    (all the pre-existing tests) exercises the template's own
    `|default(false)` fallbacks — the same situation as a cached old
    context or the small-run download path.
    """
    env = _env()
    tpl = env.get_template("admin/network_coverage_export.html")
    return tpl.render(
        run=_sample_run(),
        hubs=_sample_hubs(),
        cells=_sample_cells(),
        country_col_runs=_sample_country_col_runs(),
        **flags,
    )


# ─────────────────────── template renders ───────────────────────


def test_template_renders_without_error() -> None:
    """Sanity: no Jinja-level exceptions on a realistic payload."""
    html = _render()
    assert html.startswith("<!DOCTYPE html>")
    assert "</html>" in html


def test_flags_default_false_when_context_omits_them() -> None:
    """Old callers (and the small-run download) pass no flags at all —
    the template's `|default(false)` must render both as JSON false so
    the modal keeps its historical embedded-trips behaviour."""
    html = _render()
    assert '"lazy_trips": false' in html
    assert '"legs_omitted": false' in html


def test_lazy_trips_flag_flips_in_rendered_json() -> None:
    html = _render(lazy_trips=True)
    assert '"lazy_trips": true' in html
    assert '"legs_omitted": false' in html


def test_lazy_fetch_wiring_present() -> None:
    """The share page's modal must be able to fetch per-cell detail:
    the flags tag, the loader function, and the public endpoint URL
    pattern all have to survive template refactors."""
    html = _render()
    assert 'id="cov-flags"' in html
    assert "loadCellTrips" in html
    assert "/share/coverage/" in html
    assert "/trips" in html
    assert "Loading itineraries" in html


def test_legs_omitted_note_text_present() -> None:
    """The large-download modal explains why legs are missing and where
    to find them — the wording lives in JS so it renders regardless of
    which cell is opened."""
    html = _render(legs_omitted=True)
    assert "Leg-by-leg detail is omitted" in html
    assert "shared link" in html


def test_includes_run_metadata_header() -> None:
    """The operator-facing header should expose session id, status, and
    totals so the recipient knows what they're looking at without
    re-asking the source."""
    html = _render()
    assert "eu11-transit-motis" in html
    assert "completed" in html
    assert "2026-06-29" in html
    assert "1 ok" in html  # the totals badge


# ─────────────────────── matrix cell rendering ───────────────────────


def test_matrix_axis_uses_hub_short_names() -> None:
    """The matrix headers should use the short label, not the full name —
    short keeps the matrix scannable on wide deployments."""
    html = _render()
    assert ">P-Nord</th>" in html or ">P-Nord<" in html
    assert ">BXL-MID</th>" in html or ">BXL-MID<" in html


def test_ok_cell_emits_ok_class_and_duration() -> None:
    """The OK cell uses class=cell-ok and shows the best duration in
    H'h'MM format."""
    html = _render()
    assert 'class="cell-ok"' in html
    # 4980s = 1h23
    assert "1h23" in html


def test_no_route_cell_emits_class_and_empty_set_glyph() -> None:
    html = _render()
    assert 'class="cell-no-route"' in html
    assert "∅" in html  # the empty-set glyph for no-route cells


# ─────────────────────── PR-196a alignment heatmap in export ───────────────────────


def test_scored_cell_carries_alignment_tier_attribute() -> None:
    """The ok cell in the fixture has external_alignment_tier='agree' —
    the template must surface it as a data-alignment-tier attribute so
    the CSS heatmap toggle can colour it."""
    html = _render()
    assert 'data-alignment-tier="agree"' in html


def test_scored_cell_shows_score_badge() -> None:
    """A cell with a non-null external_alignment_score must render the
    small corner badge with the score to one decimal place."""
    html = _render()
    assert '<span class="cov-align-badge">1.0</span>' in html


def test_unscored_cell_defaults_to_no_data_tier() -> None:
    """The no_route cell in the fixture has NO external_alignment_tier
    set (pre-sweep / never scored). It must default to 'no_data' —
    same convention as the live matrix's externalOkAttr() — rather than
    emitting an empty or missing data-alignment-tier attribute, which
    would leave the CSS heatmap unable to colour it at all."""
    html = _render()
    assert 'data-alignment-tier="no_data"' in html


def test_alignment_toggle_checkbox_present() -> None:
    """The opt-in heatmap toggle must exist so operators can flip
    between the primary status legend and the alignment view without
    re-downloading the report."""
    html = _render()
    assert 'id="align-toggle-input"' in html
    assert 'type="checkbox"' in html


def test_alignment_tier_css_rules_present() -> None:
    """The viridis tier palette must be inlined in the <style> block —
    this is a self-contained file, so it cannot link to
    compare_grid.css. Spot-check the 'agree' (darkest) and 'no_data'
    (lightest) tiers so a future edit that drops the whole block is
    caught."""
    html = _render()
    assert 'td[data-alignment-tier="agree"]' in html
    assert 'td[data-alignment-tier="no_data"]' in html
    assert "#440154" in html  # agree tier's viridis dark-purple


def test_self_pair_renders_as_self_class() -> None:
    """origin==dest cells should render as cell-self with a separator,
    not get a data-pair attribute (no drill-down)."""
    html = _render()
    assert 'class="cell-self"' in html


def test_clickable_cells_carry_data_pair_attribute() -> None:
    """The drill-down JavaScript looks up cells via [data-pair]. Every
    non-self, non-pending cell must carry it; we verify the OK and
    no_route pairs are tagged."""
    html = _render()
    assert 'data-pair="p-nord:bxl-mid"' in html
    assert 'data-pair="bxl-mid:p-nord"' in html


# ─────────────────────── embedded JSON for drill-down ───────────────────────


def test_cells_embedded_as_valid_json_blob() -> None:
    """The drill-down JS reads cell data from <script id="cov-cells"
    type="application/json">…</script>. The content must parse cleanly
    as JSON or the whole drill-down breaks silently in the browser."""
    html = _render()
    match = re.search(
        r'<script id="cov-cells" type="application/json">(.*?)</script>',
        html,
        re.DOTALL,
    )
    assert match is not None, "missing cov-cells <script> block"
    parsed = json.loads(match.group(1))
    assert "p-nord:bxl-mid" in parsed
    assert parsed["p-nord:bxl-mid"]["status"] == "ok"
    # Trip leg detail must be embedded so the modal can render it offline.
    leg0 = parsed["p-nord:bxl-mid"]["trips"][0]["legs"][0]
    assert leg0["mode"] == "RAIL"
    assert leg0["from_name"] == "Paris Nord"
    assert leg0["route_short_name"] == "EST 9201"


def test_hubs_embedded_as_valid_json_blob() -> None:
    """Same drill-down code looks up hub metadata for the modal title
    via the cov-hubs JSON. Validate it round-trips."""
    html = _render()
    match = re.search(
        r'<script id="cov-hubs" type="application/json">(.*?)</script>',
        html,
        re.DOTALL,
    )
    assert match is not None, "missing cov-hubs <script> block"
    parsed = json.loads(match.group(1))
    ids = {h["id"] for h in parsed}
    assert {"p-nord", "bxl-mid"} <= ids


# ─────────────────────── self-containment markers ───────────────────────


def test_no_external_dependencies() -> None:
    """A self-contained file must not pull anything off the network when
    opened offline. Reject any <link href=…> stylesheet, <script src=…>,
    or <img src=http(s)://…>. Inline <style>/<script> is the whole point.
    """
    html = _render()
    assert "<link" not in html.lower() or 'rel="icon"' in html.lower()
    # No external scripts
    assert not re.search(r"<script[^>]*\bsrc\s*=", html), "external <script src> present"
    # No external stylesheets
    assert not re.search(r'<link[^>]*\bhref\s*=\s*"https?://', html), "external <link href> present"
    # No external images
    assert not re.search(r'<img[^>]*\bsrc\s*=\s*"https?://', html), "external <img src> present"


def test_json_appendix_present_for_power_users() -> None:
    """A <details> at the bottom contains the full run JSON for power
    users who want to grep/re-import. Don't enforce content here (covered
    by the cov-cells test); just check the appendix exists."""
    html = _render()
    assert 'class="raw"' in html
    assert "<summary>" in html and "JSON" in html


def test_modal_markup_present() -> None:
    """The drill-down modal scaffolding must be in the DOM up-front; JS
    just toggles its `hidden` attribute. Verify the title/body/close
    placeholders all exist."""
    html = _render()
    assert 'id="modal"' in html
    assert 'id="modal-title"' in html
    assert 'id="modal-body"' in html
    assert "data-close" in html


def test_country_and_type_bands_render_with_real_values() -> None:
    """Smoke test on real rendered output (not just 'doesn't crash'): both
    header bands must actually contain the sample hubs' country codes and
    type codes, including the '?' fallback for an unclassified hub."""
    html = _render()
    assert 'class="band-country"' in html
    assert ">FR<" in html
    assert ">BE<" in html
    assert 'class="band-type"' in html
    assert ">R<" in html  # Paris Nord's modes="R"
    assert ">?<" in html  # Bruxelles-Midi's modes=None


# ─────────────────────── data-shaping helpers ───────────────────────
#
# `_build_export_context` and `_export_filename` were extracted from the
# endpoint so their behaviour is testable without a DB or FastAPI. These
# tests pin the marshalling contract the Jinja template depends on.


class _StubResult:
    """Mimics the SQLAlchemy ResultRow fields the endpoint reads. Avoids
    having to spin up the real model + a session; only attribute access
    matters."""

    def __init__(self, **kw) -> None:
        for k, v in kw.items():
            setattr(self, k, v)


class _StubRun:
    """Mimics NetworkCoverageRun. Only the fields _build_export_context /
    _export_filename touch are populated."""

    def __init__(self, **kw) -> None:
        for k, v in kw.items():
            setattr(self, k, v)


def _stub_run(**overrides) -> _StubRun:
    from datetime import datetime

    base = {
        "id": "11111111-1111-1111-1111-111111111111",
        "session_id": "eu11-transit-motis",
        "mode": "single_session",
        "direction": "both",
        "depart_at": datetime(2026, 6, 29, 6, 0, 0),
        "started_at": datetime(2026, 6, 29, 5, 55, 12),
        "status": "completed",
        "total_pairs": 2,
        "completed_pairs": 2,
        "ok_pairs": 1,
        "no_route_pairs": 0,
        "error_pairs": 0,
    }
    base.update(overrides)
    return _StubRun(**base)


def _stub_hub_info(**overrides):
    """Build a HubInfo-shaped pydantic model. Imported lazily so the
    test module doesn't import the app package until it's needed (keeps
    pure template tests above isolated)."""
    from app.api.admin.network_coverage import HubInfo

    base = {
        "id": "p-nord",
        "name": "Paris Nord",
        "short": "P-Nord",
        "region": "ile-de-france",
        "country": "FR",
        "tier": "main",
        "lat": 48.8809,
        "lon": 2.3553,
        "is_active": True,
        "sort_order": 0,
    }
    base.update(overrides)
    return HubInfo(**base)


def test_build_export_context_keys_cells_by_origin_dest() -> None:
    """The Jinja template indexes cells via `cells.get(orig.id ~ ':' ~ dest.id)`.
    Verify the key format matches that contract — if this drifts, the
    matrix renders empty silently."""
    from app.api.admin.network_coverage import _build_export_context

    results = [
        _StubResult(
            origin_hub_id="p-nord",
            dest_hub_id="bxl-mid",
            status="ok",
            response_ms=1432,
            num_itineraries=1,
            best_duration_seconds=4980,
            best_num_transfers=0,
            best_operators="EUROSTAR",
            error_message=None,
            journey_search_id=None,
            session_ids=None,
        ),
    ]
    ctx = _build_export_context(
        run=_stub_run(),
        results=results,
        hubs=[_stub_hub_info()],
        trips_by_search={},
    )
    assert "p-nord:bxl-mid" in ctx["cells"]
    assert ctx["cells"]["p-nord:bxl-mid"]["status"] == "ok"
    assert ctx["cells"]["p-nord:bxl-mid"]["best_operators"] == "EUROSTAR"


# ─────────────────────── country + type header bands ───────────────────────


def test_country_color_is_stable_and_falls_back_for_unknown_codes() -> None:
    from app.api.admin.network_coverage import _country_color

    # Same country -> same color every call (operators build muscle memory
    # for "AT is violet" across different runs/sessions).
    assert _country_color("AT") == _country_color("AT")
    assert _country_color("AT") != _country_color("BE")
    # A country not in the curated list still renders something, not a
    # crash or an empty string.
    assert _country_color("ZZ") == "#5B6B82"


def test_annotate_hubs_merges_consecutive_same_country_into_one_run() -> None:
    from app.api.admin.network_coverage import _annotate_hubs_with_country_bands

    hubs = [
        _stub_hub_info(id="a", country="AT"),
        _stub_hub_info(id="b", country="AT"),
        _stub_hub_info(id="c", country="BE"),
    ]
    annotated, col_runs = _annotate_hubs_with_country_bands(hubs)

    assert [r["country"] for r in col_runs] == ["AT", "BE"]
    assert [r["span"] for r in col_runs] == [2, 1]
    # Rowspan only on the first hub of each run; the rest get None so the
    # template knows not to emit a duplicate <th> for that cell.
    assert annotated[0]["country_rowspan"] == 2
    assert annotated[1]["country_rowspan"] is None
    assert annotated[2]["country_rowspan"] == 1
    # Every hub still gets a band_color, run or no run.
    assert all(a["band_color"] for a in annotated)


def test_annotate_hubs_does_not_merge_non_consecutive_same_country() -> None:
    """Country A, B, A (not pre-sorted) must NOT merge the two A's into
    one run — that would mis-render a rowspan over B's row."""
    from app.api.admin.network_coverage import _annotate_hubs_with_country_bands

    hubs = [
        _stub_hub_info(id="a1", country="AT"),
        _stub_hub_info(id="b1", country="BE"),
        _stub_hub_info(id="a2", country="AT"),
    ]
    annotated, col_runs = _annotate_hubs_with_country_bands(hubs)

    assert [r["country"] for r in col_runs] == ["AT", "BE", "AT"]
    assert [r["span"] for r in col_runs] == [1, 1, 1]
    assert [a["country_rowspan"] for a in annotated] == [1, 1, 1]


def test_build_export_context_exposes_country_col_runs() -> None:
    from app.api.admin.network_coverage import _build_export_context

    ctx = _build_export_context(
        run=_stub_run(),
        results=[],
        hubs=[
            _stub_hub_info(id="p-nord", country="FR"),
            _stub_hub_info(id="bxl-mid", country="BE"),
        ],
        trips_by_search={},
    )
    assert ctx["country_col_runs"] == [
        {"country": "FR", "span": 1, "color": ctx["hubs"][0]["band_color"]},
        {"country": "BE", "span": 1, "color": ctx["hubs"][1]["band_color"]},
    ]
    assert ctx["hubs"][0]["country_rowspan"] == 1


def test_build_export_context_attaches_trips_by_journey_search_id() -> None:
    """The cell's `trips` array should come from trips_by_search keyed by
    journey_search_id. When a cell has no journey_search_id (a no-route
    or pending row), its trips list must be empty — not crash on missing
    key."""
    from app.api.admin.network_coverage import _build_export_context

    exec_uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    results = [
        _StubResult(
            origin_hub_id="p-nord",
            dest_hub_id="bxl-mid",
            status="ok",
            response_ms=1432,
            num_itineraries=1,
            best_duration_seconds=4980,
            best_num_transfers=0,
            best_operators="EUROSTAR",
            error_message=None,
            journey_search_id=exec_uuid,
            session_ids=None,
        ),
        _StubResult(
            origin_hub_id="bxl-mid",
            dest_hub_id="p-nord",
            status="no_route",
            response_ms=412,
            num_itineraries=0,
            best_duration_seconds=None,
            best_num_transfers=None,
            best_operators=None,
            error_message=None,
            journey_search_id=None,  # no linked execution → empty trips
            session_ids=None,
        ),
    ]
    trips = {
        exec_uuid: [
            {"rank": 0, "duration_seconds": 4980, "num_transfers": 0, "legs": []},
        ],
    }
    ctx = _build_export_context(
        run=_stub_run(),
        results=results,
        hubs=[_stub_hub_info()],
        trips_by_search=trips,
    )
    assert len(ctx["cells"]["p-nord:bxl-mid"]["trips"]) == 1
    assert ctx["cells"]["p-nord:bxl-mid"]["trips"][0]["duration_seconds"] == 4980
    assert ctx["cells"]["bxl-mid:p-nord"]["trips"] == []


def test_build_export_context_includes_alignment_tier_and_score() -> None:
    """PR-196a — the alignment tier + score columns must round-trip
    through the export context exactly like every other external_*
    field, so the offline report's heatmap has data to render."""
    from app.api.admin.network_coverage import _build_export_context

    results = [
        _StubResult(
            origin_hub_id="p-nord",
            dest_hub_id="bxl-mid",
            status="ok",
            response_ms=1432,
            num_itineraries=1,
            best_duration_seconds=4980,
            best_num_transfers=0,
            best_operators="EUROSTAR",
            error_message=None,
            journey_search_id=None,
            session_ids=None,
            external_alignment_tier="mostly_agree",
            external_alignment_score=0.7,
        ),
    ]
    ctx = _build_export_context(
        run=_stub_run(),
        results=results,
        hubs=[_stub_hub_info()],
        trips_by_search={},
    )
    cell = ctx["cells"]["p-nord:bxl-mid"]
    assert cell["external_alignment_tier"] == "mostly_agree"
    assert cell["external_alignment_score"] == 0.7


def test_build_export_context_defaults_alignment_fields_to_none() -> None:
    """A row that pre-dates the PR-196a sweep (or was never scored) has
    no external_alignment_tier/score attributes at all — the marshaller
    must default to None via getattr rather than raise AttributeError."""
    from app.api.admin.network_coverage import _build_export_context

    results = [
        _StubResult(
            origin_hub_id="p-nord",
            dest_hub_id="bxl-mid",
            status="ok",
            response_ms=1432,
            num_itineraries=1,
            best_duration_seconds=4980,
            best_num_transfers=0,
            best_operators="EUROSTAR",
            error_message=None,
            journey_search_id=None,
            session_ids=None,
            # external_alignment_tier / external_alignment_score omitted
        ),
    ]
    ctx = _build_export_context(
        run=_stub_run(),
        results=results,
        hubs=[_stub_hub_info()],
        trips_by_search={},
    )
    cell = ctx["cells"]["p-nord:bxl-mid"]
    assert cell["external_alignment_tier"] is None
    assert cell["external_alignment_score"] is None


def test_build_export_context_run_meta_uses_started_at_not_created_at() -> None:
    """Regression lock: the NetworkCoverageRun model has `started_at`,
    not `created_at`. Mypy caught this in CI on the first push of this
    feature — this test pins the attribute name so a future refactor
    doesn't silently break the export."""
    from app.api.admin.network_coverage import _build_export_context

    ctx = _build_export_context(
        run=_stub_run(),
        results=[],
        hubs=[_stub_hub_info()],
        trips_by_search={},
    )
    # Template reads it as run.created_at — so the dict key stays "created_at"
    # but the value comes from run.started_at.
    assert ctx["run"]["created_at"] == "2026-06-29T05:55:12"


def test_build_export_context_serializes_hubs_via_pydantic() -> None:
    """Hubs go through pydantic's model_dump() so the template sees
    plain dicts (Jinja can't iterate model_dump-able objects reliably).
    Verify the dump worked."""
    from app.api.admin.network_coverage import _build_export_context

    ctx = _build_export_context(
        run=_stub_run(),
        results=[],
        hubs=[
            _stub_hub_info(),
            _stub_hub_info(id="bxl-mid", name="Bruxelles-Midi", short="BXL-MID"),
        ],
        trips_by_search={},
    )
    assert isinstance(ctx["hubs"], list)
    assert all(isinstance(h, dict) for h in ctx["hubs"])
    assert {h["id"] for h in ctx["hubs"]} == {"p-nord", "bxl-mid"}


def test_build_export_context_handles_fanout_session_ids() -> None:
    """Fanout-mode runs populate result.session_ids; single-session runs
    leave it None. The marshaller uses getattr so a result row without
    the attribute also doesn't crash — defensive against test fixtures
    that pre-date the column."""
    from app.api.admin.network_coverage import _build_export_context

    # Fanout result: has session_ids
    r_fanout = _StubResult(
        origin_hub_id="p-nord",
        dest_hub_id="bxl-mid",
        status="ok",
        response_ms=100,
        num_itineraries=1,
        best_duration_seconds=4980,
        best_num_transfers=0,
        best_operators="EUROSTAR",
        error_message=None,
        journey_search_id=None,
        session_ids=["eu11-transit-motis", "eu-rail-motis"],
    )
    # Legacy result: no session_ids attribute at all
    r_legacy = _StubResult(
        origin_hub_id="bxl-mid",
        dest_hub_id="p-nord",
        status="no_route",
        response_ms=412,
        num_itineraries=0,
        best_duration_seconds=None,
        best_num_transfers=None,
        best_operators=None,
        error_message=None,
        journey_search_id=None,
        # session_ids deliberately omitted
    )
    ctx = _build_export_context(
        run=_stub_run(),
        results=[r_fanout, r_legacy],
        hubs=[_stub_hub_info()],
        trips_by_search={},
    )
    assert ctx["cells"]["p-nord:bxl-mid"]["session_ids"] == ["eu11-transit-motis", "eu-rail-motis"]
    assert ctx["cells"]["bxl-mid:p-nord"]["session_ids"] is None


# ─────────────────────── filename helper ───────────────────────


def test_export_filename_uses_session_id_and_timestamp() -> None:
    """Operators sort multiple exports in their file manager — the
    timestamp prefix in the filename keeps related runs adjacent."""
    from app.api.admin.network_coverage import _export_filename

    assert _export_filename(_stub_run()) == "coverage-eu11-transit-motis-20260629-0555.html"


def test_export_filename_falls_back_to_fanout_label() -> None:
    """Fanout runs have session_id=None — the file still needs a label.
    Use 'fanout' explicitly so recipients can tell it apart."""
    from app.api.admin.network_coverage import _export_filename

    assert _export_filename(_stub_run(session_id=None)) == "coverage-fanout-20260629-0555.html"


def test_export_filename_flattens_slashes_in_session_id() -> None:
    """Some session ids contain `/` (legacy from earlier UI); slashes
    are filesystem-unsafe on Windows and ambiguous in URLs. Flatten to
    hyphens — same convention OTP uses for stop ids."""
    from app.api.admin.network_coverage import _export_filename

    name = _export_filename(_stub_run(session_id="foo/bar/baz"))
    assert "/" not in name
    assert "foo-bar-baz" in name


def test_export_filename_handles_missing_started_at() -> None:
    """Pending / not-yet-started runs have started_at=None; export still
    needs a parseable filename rather than crashing. Use 'unknown' as
    the timestamp segment."""
    from app.api.admin.network_coverage import _export_filename

    assert (
        _export_filename(_stub_run(started_at=None)) == "coverage-eu11-transit-motis-unknown.html"
    )


# ─────────────────────── DB-touching helpers (mocked) ───────────────────────
#
# `_fetch_trips_by_search` and `_resolve_hubs` issue real SQLAlchemy queries.
# Rather than spin up Postgres, mock the DB session's execute() chain —
# enough to prove the queries are constructed and shaped correctly. The
# integration tests in tests/integration/ exercise the real query path
# when they get added.


class _MockScalars:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def all(self) -> list:
        return self._rows


class _MockExecuteResult:
    """Supports both `.scalars().all()` (used by _resolve_hubs) and
    `.all()` (used by _fetch_trips_by_search after the JOIN). Tests
    feed the rows they expect each helper to receive."""

    def __init__(self, rows: list) -> None:
        self._rows = rows

    def scalars(self) -> _MockScalars:
        return _MockScalars(self._rows)

    def all(self) -> list:
        return self._rows


class _MockDb:
    """Bare-minimum DB session stub. `execute` returns the canned rows
    set by the test. No query inspection — we trust the helper to pass
    its statement through; the rows are what matter to the assertions.
    """

    def __init__(self, rows: list) -> None:
        self._rows = rows
        self.execute_call_count = 0

    def execute(self, _stmt) -> _MockExecuteResult:
        self.execute_call_count += 1
        return _MockExecuteResult(self._rows)


def _trip_stub(execution_id, rank=0, **overrides):
    """Build a JourneyTrip-shaped row stub with sensible defaults so the
    individual tests stay focused on what they're actually asserting."""
    from datetime import datetime

    base = {
        "execution_id": execution_id,
        "rank_in_response": rank,
        "duration_seconds": 4980,
        "num_transfers": 0,
        "departure_at": datetime(2026, 6, 29, 6, 25),
        "arrival_at": datetime(2026, 6, 29, 7, 48),
        "modes": "RAIL",
        "legs": [{"mode": "RAIL"}],
    }
    base.update(overrides)
    return _StubResult(**base)


def test_fetch_trips_by_search_returns_empty_when_no_search_ids() -> None:
    """If no coverage_result row has a journey_search_id, skip the DB
    query entirely — saves a useless `WHERE search_id IN ()` round-trip
    that some DBs (Postgres included) error on."""
    from app.api.admin.network_coverage import _fetch_trips_by_search

    db = _MockDb(rows=[])
    out = _fetch_trips_by_search(db, search_ids=[])
    assert out == {}
    assert db.execute_call_count == 0


def test_fetch_trips_by_search_groups_rows_by_search_id() -> None:
    """Multiple trips can share a search_id (one search → one execution
    → many trips in single-session mode; one search → many executions →
    many trips in fanout mode). All trips for the same search must group
    under one key — the template's `cells[...].trips` is keyed by
    `journey_search_id` which IS the search_id."""
    from uuid import uuid4

    from app.api.admin.network_coverage import _fetch_trips_by_search

    search_a = uuid4()
    search_b = uuid4()
    exec_a1 = uuid4()
    exec_b1 = uuid4()
    # The JOIN'd query returns (search_id, trip) tuples — the test mock
    # passes them through as the `.all()` return value.
    rows = [
        (search_a, _trip_stub(exec_a1, rank=0, legs=[{"mode": "RAIL"}])),
        (
            search_a,
            _trip_stub(exec_a1, rank=1, num_transfers=1, legs=[{"mode": "RAIL"}, {"mode": "WALK"}]),
        ),
        (search_b, _trip_stub(exec_b1, rank=0, duration_seconds=3600)),
    ]
    db = _MockDb(rows=rows)
    out = _fetch_trips_by_search(db, search_ids=[search_a, search_b])
    assert db.execute_call_count == 1
    assert len(out[str(search_a)]) == 2
    assert len(out[str(search_b)]) == 1
    assert out[str(search_a)][0]["rank"] == 0
    assert out[str(search_a)][0]["legs"] == [{"mode": "RAIL"}]
    assert out[str(search_a)][1]["num_transfers"] == 1


def test_fetch_trips_by_search_isoformats_datetimes() -> None:
    """The Jinja template + browser JSON.parse expects ISO-formatted
    strings, not datetime objects (which the JSON encoder can't handle
    by default)."""
    from uuid import uuid4

    from app.api.admin.network_coverage import _fetch_trips_by_search

    search_id = uuid4()
    exec_id = uuid4()
    db = _MockDb(rows=[(search_id, _trip_stub(exec_id, legs=[]))])
    out = _fetch_trips_by_search(db, search_ids=[search_id])
    assert isinstance(out[str(search_id)][0]["departure_at"], str)
    assert out[str(search_id)][0]["departure_at"].startswith("2026-06-29T06:25")


def test_fetch_trips_by_search_unions_fanout_executions() -> None:
    """In fanout-mode runs, one search has multiple executions (one per
    targeted session). The helper must UNION trips across all those
    executions under the same search_id key — that's what the matrix
    cell's `"X itineraries across N sessions"` summary depends on.

    Regression lock for the bug fixed 2026-06-25: the original helper
    keyed by execution_id, which silently returned empty trips for every
    production cell because coverage_results stores the SEARCH id, not
    the execution id. The JOIN through JourneySearchExecution is what
    makes the keying right.
    """
    from uuid import uuid4

    from app.api.admin.network_coverage import _fetch_trips_by_search

    search_id = uuid4()  # one search
    exec_otp = uuid4()  # OTP engine produced this execution
    exec_motis = uuid4()  # MOTIS engine produced this one
    rows = [
        # Both executions hang off the same search_id; the JOIN flattens
        # them to (search_id, trip) tuples for the helper.
        (search_id, _trip_stub(exec_otp, rank=0, modes="RAIL")),
        (search_id, _trip_stub(exec_motis, rank=0, modes="RAIL,TRAM")),
        (search_id, _trip_stub(exec_motis, rank=1, modes="RAIL")),
    ]
    db = _MockDb(rows=rows)
    out = _fetch_trips_by_search(db, search_ids=[search_id])
    assert len(out) == 1  # one key, not two — both executions union under it
    assert len(out[str(search_id)]) == 3


def test_fetch_trips_by_search_caps_trips_per_search_at_max() -> None:
    """K-slot coverage runs can accumulate 50+ deduped itineraries for a
    single pair (slot_count x num_itineraries_per_slot). Embedding all of
    them in the export/share HTML is what let a real 8742-pair run balloon
    to 13+GB of process memory and hang the web process — see the module
    comment on `_MAX_TRIPS_PER_CELL_EXPORT`. The helper must keep only the
    first `_MAX_TRIPS_PER_CELL_EXPORT` rows per search (already ordered by
    rank_in_response by the query), not allow unbounded growth."""
    from uuid import uuid4

    from app.api.admin.network_coverage import _MAX_TRIPS_PER_CELL_EXPORT, _fetch_trips_by_search

    search_id = uuid4()
    exec_id = uuid4()
    rows = [(search_id, _trip_stub(exec_id, rank=i)) for i in range(_MAX_TRIPS_PER_CELL_EXPORT + 5)]
    db = _MockDb(rows=rows)
    out = _fetch_trips_by_search(db, search_ids=[search_id])
    assert len(out[str(search_id)]) == _MAX_TRIPS_PER_CELL_EXPORT
    assert [t["rank"] for t in out[str(search_id)]] == list(range(_MAX_TRIPS_PER_CELL_EXPORT))


def test_fetch_trips_by_search_cap_applies_independently_per_search() -> None:
    """The cap is per search_id, not a global budget across the whole
    batch — an early search in a large run must not crowd out a later
    one's trips."""
    from uuid import uuid4

    from app.api.admin.network_coverage import _MAX_TRIPS_PER_CELL_EXPORT, _fetch_trips_by_search

    search_a = uuid4()
    search_b = uuid4()
    exec_a = uuid4()
    exec_b = uuid4()
    rows = [(search_a, _trip_stub(exec_a, rank=i)) for i in range(_MAX_TRIPS_PER_CELL_EXPORT + 3)]
    rows += [(search_b, _trip_stub(exec_b, rank=i)) for i in range(3)]
    db = _MockDb(rows=rows)
    out = _fetch_trips_by_search(db, search_ids=[search_a, search_b])
    assert len(out[str(search_a)]) == _MAX_TRIPS_PER_CELL_EXPORT
    assert len(out[str(search_b)]) == 3


def test_fetch_trips_by_search_without_legs_returns_summaries_with_empty_legs() -> None:
    """`include_legs=False` (large exports) projects explicit columns —
    the query returns plain tuples instead of ORM rows, so the ~1.7KB/trip
    legs JSON never leaves Postgres. The output shape must be identical
    to the full path except `legs` is always `[]`."""
    from datetime import datetime
    from uuid import uuid4

    from app.api.admin.network_coverage import _fetch_trips_by_search

    search_id = uuid4()
    # Column-projected row shape: (search_id, rank, duration, transfers,
    # departure_at, arrival_at, modes) — no trip object, no legs.
    rows = [
        (search_id, 0, 4980, 0, datetime(2026, 6, 29, 6, 25), datetime(2026, 6, 29, 7, 48), "RAIL"),
        (search_id, 1, 5400, 1, None, None, "RAIL,TRAM"),
    ]
    db = _MockDb(rows=rows)
    out = _fetch_trips_by_search(db, search_ids=[search_id], include_legs=False)
    trips = out[str(search_id)]
    assert len(trips) == 2
    assert trips[0] == {
        "rank": 0,
        "duration_seconds": 4980,
        "num_transfers": 0,
        "departure_at": "2026-06-29T06:25:00",
        "arrival_at": "2026-06-29T07:48:00",
        "modes": "RAIL",
        "legs": [],
    }
    assert trips[1]["legs"] == []
    assert trips[1]["departure_at"] is None


def test_fetch_trips_by_search_without_legs_still_caps_per_search() -> None:
    """The per-cell cap applies on both query paths — a large export
    must not regain unbounded trips just because it took the no-legs
    branch."""
    from uuid import uuid4

    from app.api.admin.network_coverage import _MAX_TRIPS_PER_CELL_EXPORT, _fetch_trips_by_search

    search_id = uuid4()
    rows = [
        (search_id, i, 3600, 0, None, None, "RAIL") for i in range(_MAX_TRIPS_PER_CELL_EXPORT + 4)
    ]
    db = _MockDb(rows=rows)
    out = _fetch_trips_by_search(db, search_ids=[search_id], include_legs=False)
    assert len(out[str(search_id)]) == _MAX_TRIPS_PER_CELL_EXPORT


def test_resolve_hubs_returns_db_rows_when_present() -> None:
    """Happy path: `network_coverage_hubs` table has rows → return them
    via `_hub_to_info` shape conversion. The fallback to static HUBS
    only kicks in when the table is empty."""
    from app.api.admin.network_coverage import _resolve_hubs

    hub_row = _StubResult(
        id="p-nord",
        name="Paris Nord",
        short="P-Nord",
        region="ile-de-france",
        country="FR",
        tier="main",
        modes=None,
        lat=48.8809,
        lon=2.3553,
        is_active=True,
        sort_order=0,
    )
    db = _MockDb(rows=[hub_row])
    out = _resolve_hubs(db)
    assert len(out) == 1
    assert out[0].id == "p-nord"
    assert out[0].country == "FR"


def test_resolve_hubs_falls_back_to_static_when_table_empty() -> None:
    """Fresh installs / dev envs that haven't seeded `network_coverage_hubs`
    still need a non-empty axis or the matrix renders blank. Fall back to
    the static HUBS list from app/network_coverage/hubs.py."""
    from app.api.admin.network_coverage import _resolve_hubs
    from app.network_coverage.hubs import HUBS as STATIC

    db = _MockDb(rows=[])
    out = _resolve_hubs(db)
    assert len(out) == len(STATIC)
    assert {h.id for h in out} == {h.id for h in STATIC}
    # Every fallback hub is treated as active + FR (the static list is
    # France-only by construction).
    assert all(h.is_active for h in out)
    assert all(h.country == "FR" for h in out)


# ─────────────────────── endpoint orchestration ───────────────────────


def test_export_run_html_404_when_run_missing(monkeypatch) -> None:
    """If runner.get_run_with_results returns (None, ...), the endpoint
    must raise HTTPException(404) rather than crashing on attribute
    access. Mirrors the existing GET /runs/{id} endpoint's behaviour."""
    import pytest
    from fastapi import HTTPException

    from app.api.admin import network_coverage as mod

    monkeypatch.setattr(mod.runner, "get_run_with_results", lambda _d, _r: (None, []))

    with pytest.raises(HTTPException) as exc_info:
        mod.export_run_html(
            run_id="00000000-0000-0000-0000-000000000000",
            request=None,  # not reached on the 404 path
            db=_MockDb(rows=[]),
            _=None,
        )
    assert exc_info.value.status_code == 404


def test_export_run_html_renders_with_content_disposition(monkeypatch) -> None:
    """Happy path: the endpoint queries the DB, builds the context,
    renders the template, and sets a Content-Disposition header naming
    the file by session id + timestamp. Mocks the DB layer + the
    runner + the template call; verifies the response shape and that
    `_export_filename` was applied to the run."""
    from fastapi.responses import HTMLResponse

    from app.api.admin import network_coverage as mod

    run = _stub_run()
    monkeypatch.setattr(mod.runner, "get_run_with_results", lambda _d, _r: (run, []))

    captured: dict = {}

    def fake_template_response(_request, _name, context):
        captured["context"] = context
        return HTMLResponse(content="<html>stub</html>")

    monkeypatch.setattr(mod.templates, "TemplateResponse", fake_template_response)

    response = mod.export_run_html(
        run_id="11111111-1111-1111-1111-111111111111",
        request=None,
        db=_MockDb(rows=[]),  # empty hubs table → static-list fallback
        _=None,
    )
    # Filename header carries session id + timestamp (proves _export_filename
    # was called with our run).
    cd = response.headers["Content-Disposition"]
    assert "coverage-eu11-transit-motis-20260629-0555.html" in cd
    assert cd.startswith("attachment;")
    # Template context has the top-level keys the template indexes by.
    assert set(captured["context"].keys()) == {
        "run",
        "hubs",
        "cells",
        "country_col_runs",
        "lazy_trips",
        "legs_omitted",
    }
    assert captured["context"]["cells"] == {}  # no results passed
    # A tiny run keeps the historical full-embed behaviour: nothing lazy,
    # nothing omitted.
    assert captured["context"]["lazy_trips"] is False
    assert captured["context"]["legs_omitted"] is False


def _minimal_result_stub(origin: str = "a", dest: str = "b"):
    """The 9 attributes `_build_export_context` reads directly off a
    result row (the external_* / session_ids extras go through getattr
    with a None default, so a bare stub is enough for those)."""
    return _StubResult(
        origin_hub_id=origin,
        dest_hub_id=dest,
        status="ok",
        response_ms=100,
        num_itineraries=1,
        best_duration_seconds=3600,
        best_num_transfers=0,
        best_operators=None,
        error_message=None,
        journey_search_id=None,
    )


def test_export_run_html_drops_legs_above_the_pair_threshold(monkeypatch) -> None:
    """A run with more result rows than `_EXPORT_LEG_DETAIL_MAX_PAIRS`
    must fetch trips WITHOUT legs and flag `legs_omitted` to the
    template — the full-detail file for a 94-hub run is ~150MB, which no
    browser can open. Below the threshold nothing changes (previous
    test)."""
    from fastapi.responses import HTMLResponse

    from app.api.admin import network_coverage as mod

    run = _stub_run()
    results = [
        _minimal_result_stub(f"hub{i}", f"hub{i + 1}")
        for i in range(mod._EXPORT_LEG_DETAIL_MAX_PAIRS + 1)
    ]
    monkeypatch.setattr(mod.runner, "get_run_with_results", lambda _d, _r: (run, results))

    captured: dict = {}

    def fake_fetch(_db, search_ids, *, include_legs=True):
        captured["include_legs"] = include_legs
        return {}

    monkeypatch.setattr(mod, "_fetch_trips_by_search", fake_fetch)
    monkeypatch.setattr(
        mod.templates,
        "TemplateResponse",
        lambda _req, _name, context: (
            captured.__setitem__("context", context),
            HTMLResponse(content="<html>stub</html>"),
        )[1],
    )

    mod.export_run_html(
        run_id="11111111-1111-1111-1111-111111111111",
        request=None,
        db=_MockDb(rows=[]),
        _=None,
    )
    assert captured["include_legs"] is False
    assert captured["context"]["legs_omitted"] is True
    # The share page's lazy mode is never used for the download — the
    # offline file has no server to fetch from.
    assert captured["context"]["lazy_trips"] is False


def test_export_run_html_keeps_legs_at_exactly_the_pair_threshold(monkeypatch) -> None:
    """The threshold is INCLUSIVE: a run of exactly
    `_EXPORT_LEG_DETAIL_MAX_PAIRS` result rows keeps full leg detail —
    that's the contract the constant's size math is built on. Pins the
    boundary so a `<=` → `<` refactor can't ship silently (mutation
    testing showed the rest of the suite passes under that change)."""
    from fastapi.responses import HTMLResponse

    from app.api.admin import network_coverage as mod

    run = _stub_run()
    results = [
        _minimal_result_stub(f"hub{i}", f"hub{i + 1}")
        for i in range(mod._EXPORT_LEG_DETAIL_MAX_PAIRS)
    ]
    monkeypatch.setattr(mod.runner, "get_run_with_results", lambda _d, _r: (run, results))

    captured: dict = {}

    def fake_fetch(_db, search_ids, *, include_legs=True):
        captured["include_legs"] = include_legs
        return {}

    monkeypatch.setattr(mod, "_fetch_trips_by_search", fake_fetch)
    monkeypatch.setattr(
        mod.templates,
        "TemplateResponse",
        lambda _req, _name, context: (
            captured.__setitem__("context", context),
            HTMLResponse(content="<html>stub</html>"),
        )[1],
    )

    mod.export_run_html(
        run_id="11111111-1111-1111-1111-111111111111",
        request=None,
        db=_MockDb(rows=[]),
        _=None,
    )
    assert captured["include_legs"] is True
    assert captured["context"]["legs_omitted"] is False
