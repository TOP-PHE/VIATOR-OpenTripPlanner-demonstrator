"""Trip-wires on journey.html's URL prefill, used by the coverage cell
modal's "Re-run live in the journey UI" link.

The template's JS is not executed here; these checks read its text, like
tests/unit/test_journey_template_timetable.py. They pin that:

- the prefill writes the station name into the visible From/To boxes
  (`from-input`, `to-input`), not into an id no element carries — the link
  used to fill only the hidden coordinates, so the boxes stayed empty and
  the browser refused the submit (both boxes are `required`);
- every element the prefill looks up exists in the form.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TEMPLATE = Path(__file__).resolve().parents[2] / "app" / "templates" / "journey.html"


@pytest.fixture(scope="module")
def template_text() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def prefill(template_text: str) -> str:
    match = re.search(
        r"\(function prefillFromQuery\(\) \{.*?^\}\)\(\);$", template_text, re.S | re.M
    )
    assert match, "prefillFromQuery not found in journey.html"
    return match.group(0)


def _element_ids(template_text: str) -> set[str]:
    return set(re.findall(r'<input[^>]*\bid="([^"]+)"', template_text))


def test_the_prefill_fills_the_visible_station_boxes(prefill: str) -> None:
    assert "document.getElementById(prefix + '-input')" in prefill
    assert "if (visible && name) visible.value = name;" in prefill
    assert "document.getElementById(prefix)" not in prefill


@pytest.mark.parametrize("prefix", ["from", "to"])
def test_every_element_the_prefill_writes_exists(template_text: str, prefix: str) -> None:
    ids = _element_ids(template_text)
    for suffix in ("-input", "-lat", "-lon", "-name", "-uic"):
        assert f"{prefix}{suffix}" in ids
    assert "depart" in ids


@pytest.mark.parametrize("prefix", ["from", "to"])
def test_the_visible_boxes_are_required(template_text: str, prefix: str) -> None:
    """Why an empty visible box blocks the search: the browser refuses to
    submit a form whose `required` input is empty."""
    assert re.search(rf'<input id="{prefix}-input" name="{prefix}" required', template_text)


def test_typing_keeps_the_prefilled_coordinates_when_the_name_matches(template_text: str) -> None:
    """The typeahead clears the hidden coordinates only when the visible text
    differs from the hidden name, which the prefill sets to the same value."""
    assert "if (inp.value.trim() !== name.value) {" in template_text
