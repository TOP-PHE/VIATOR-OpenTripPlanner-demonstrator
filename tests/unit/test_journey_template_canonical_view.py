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
