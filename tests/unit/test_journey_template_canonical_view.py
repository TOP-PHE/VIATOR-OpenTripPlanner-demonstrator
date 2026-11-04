"""#303 — the journey page reads canonical journeys through one view module.

Text checks on the template and `app/static/js/canonical_view.js`, the style of the other
`test_journey_template_*` tests (CI runs no JavaScript).
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = (ROOT / "app" / "templates" / "journey.html").read_text(encoding="utf-8")
VIEW = (ROOT / "app" / "static" / "js" / "canonical_view.js").read_text(encoding="utf-8")


def test_the_view_module_loads_before_the_page_script() -> None:
    view = TEMPLATE.index('<script src="/static/app/js/canonical_view.js"></script>')
    grid = TEMPLATE.index('<script src="/static/app/js/compare_grid.js"></script>')
    assert view < grid < TEMPLATE.index("function render(payload)")


def test_the_fanout_answer_is_converted_once_where_it_arrives() -> None:
    assert TEMPLATE.count("CanonicalView.fromFanout(") == 1
    assert "render(CanonicalView.fromFanout(await r.json()));" in TEMPLATE


def test_the_json_inspectors_show_the_canonical_journey_not_an_engine_payload() -> None:
    assert "_raw_itinerary" not in TEMPLATE
    assert "t.best._canonical || t.best" in TEMPLATE
    assert "t._canonical || t" in TEMPLATE
    assert "Show OTP JSON" not in TEMPLATE  # one label for every engine


def test_the_view_gives_each_display_field_one_meaning() -> None:
    # The line first, then the source's label: the labels shown before #303, so the
    # side-by-side's identifier note compares what it compared (design D5).
    assert "return service.line || service.published_name || null;" in VIEW
    # A ride's mode is its family; a walk stays a walk.
    assert "canonicalLeg.service ? canonicalLeg.service.mode : null" in VIEW
    assert "canonicalLeg.continuous_mode || 'WALK'" in VIEW
    # The first ride's boarding time, not the door-to-door start.
    assert (
        "first_transit_leg_departure_utc: rides.length ? instant(rides[0].departure) : null" in VIEW
    )
    # The OJP comparison tags come by journey id.
    assert "tags[canonical.id]" in VIEW


def test_the_view_module_exports_on_globalthis() -> None:
    assert "})(globalThis);" in VIEW
    assert "root.CanonicalView = { fromFanout: fromFanout, journey: journey, leg: leg };" in VIEW


def test_the_view_gives_the_walk_free_span_from_instants() -> None:
    # #303 PR 4b: the coverage modal's walk-free header (#227's review). Instants, never a
    # wall clock, so a night train past midnight or a change of daylight saving time cannot bend
    # the duration.
    assert "rides_departure: ridesDeparture," in VIEW
    assert "rides_arrival: ridesArrival," in VIEW
    assert (
        "ridesDeparture && ridesArrival ? secondsBetween(ridesDeparture, ridesArrival) : null"
        in VIEW
    )
    assert "Math.round((end - start) / 1000)" in VIEW


def test_the_view_keeps_the_label_and_the_stop_places_a_page_may_need() -> None:
    # The ÖBB column shows the label ÖBB shows; the admin modal asks the station module about
    # ÖBB's stop numbers, read from the stop place's codes.
    assert "published_name: service ? service.published_name || null : null," in VIEW
    assert "from_stop: stopOf(canonicalLeg.origin)," in VIEW
    assert "to_stop: stopOf(canonicalLeg.destination)," in VIEW


def test_the_view_module_has_no_closing_script_tag() -> None:
    # The downloaded report embeds the file's text in a <script>: a closing tag would end it.
    assert "</script" not in VIEW.lower()
