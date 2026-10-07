"""Trip-wires on journey.html's station typeahead (the station-module change).

The template's JS is not executed here; these checks read its text, like
tests/unit/test_journey_template_compare.py. They pin three things:

- the station half of the typeahead posts the text to `/api/stations/suggest`
  in a JSON body, never in an address, from 3 characters;
- the MOTIS half (`/api/geocode`, `SUGGEST_MIN = 2`) is unchanged;
- `_suggestionHtml` escapes the name and the country before `innerHTML`:
  the module's names come from third-party feeds, stored as they come.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TEMPLATE = Path(__file__).resolve().parents[2] / "app" / "templates" / "journey.html"


@pytest.fixture(scope="module")
def template_text() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def _function(text: str, name: str) -> str:
    """The source of one top-level `function name(...) {...}` of the script."""
    match = re.search(rf"^(?:async )?function {name}\(.*?^}}$", text, re.S | re.M)
    assert match, f"{name} not found in journey.html"
    return match.group(0)


def _refresh(text: str) -> str:
    match = re.search(r"async function refresh\(\) \{.*?\n  \}\n", text, re.S)
    assert match, "the typeahead's refresh() not found in journey.html"
    return match.group(0)


def test_the_station_half_posts_to_the_suggest_route(template_text: str) -> None:
    refresh = _refresh(template_text)

    assert "_postJson('/api/stations/suggest', {q})" in refresh
    assert "q.length >= STATION_SUGGEST_MIN" in refresh
    assert re.search(r"^const STATION_SUGGEST_MIN = 3;$", template_text, re.M)


def test_the_text_never_goes_into_the_address_of_the_station_list(template_text: str) -> None:
    assert "/api/master/stations" not in _refresh(template_text)
    assert not re.search(r"/api/stations/suggest[?`$]", template_text)
    post = _function(template_text, "_postJson")
    assert "method: 'POST'" in post
    assert "'Content-Type': 'application/json'" in post
    assert "body: JSON.stringify(body)" in post


def test_the_post_helper_is_as_tolerant_as_the_get_helper(template_text: str) -> None:
    post = _function(template_text, "_postJson")

    assert "r.ok ? await r.json() : []" in post
    assert re.search(r"catch \(err\) \{\s*console\.warn\([^)]*\);\s*return \[\];", post)
    assert "body" not in post.split("console.warn(", 1)[1].split(")", 1)[0]


def test_the_motis_half_is_unchanged(template_text: str) -> None:
    refresh = _refresh(template_text)

    assert re.search(r"^const SUGGEST_MIN = 2;$", template_text, re.M)
    assert "if (q.length < SUGGEST_MIN)" in refresh
    assert "_fetchJson(`/api/geocode?q=${qs}&size=20`)" in refresh


def test_the_suggestion_escapes_the_name_and_the_country(template_text: str) -> None:
    html = _function(template_text, "_suggestionHtml")

    assert "${escHTML(s.name)}" in html
    assert "[${escHTML(s.country_iso)}]" in html
    # No raw interpolation of either field is left.
    assert "${s.name}" not in html
    assert "${s.country_iso}" not in html


def test_eschtml_is_a_hoisted_function_of_the_same_script(template_text: str) -> None:
    """`_suggestionHtml` runs long after the script has loaded, but escHTML
    must be a function declaration (hoisted) in the same <script> block, not
    a `const` (a TDZ ReferenceError would abort the block)."""
    start = template_text.index("function _suggestionHtml(")
    script_start = template_text.rindex("<script>", 0, start)
    script_end = template_text.index("</script>", start)
    block = template_text[script_start:script_end]

    line = re.search(r"^function escHTML\(s\) \{.*$", block, re.M)
    assert line, "escHTML is not a function declaration of the typeahead's script"
    assert "replace(/[<>&]/g" in line.group(0)
