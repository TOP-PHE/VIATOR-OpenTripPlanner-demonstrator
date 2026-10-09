"""Trip-wires on journey.html for a date outside the loaded timetable (#338).

The template's JS is not executed here; these checks read its text, like
tests/unit/test_journey_template_suggest.py. They pin that:

- the timing strip shows an engine's refusal reason (`detail`) instead of a
  bare status, and escapes every field it prints;
- "No itineraries found" names the sessions whose engine refused the date;
- the form warns, before searching, when the chosen date is in the past, and
  the default date is the browser's local time, not UTC.
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
    match = re.search(rf"^(?:async )?function {name}\(.*?^}}$", text, re.S | re.M)
    assert match, f"{name} not found in journey.html"
    return match.group(0)


def test_the_strip_shows_the_refusal_reason_escaped(template_text: str) -> None:
    status = _function(template_text, "_execStatusText")
    assert "e.reason === 'outside_timetable' && e.detail ? e.detail : e.status" in status

    render = _function(template_text, "render")
    strip = re.search(r"const exec = payload\.executions\.map\(e =>\n(.*?)\n  \)", render, re.S)
    assert strip, "the timing strip is not built as before"
    line = strip.group(1)
    assert "${escHTML(_execStatusText(e))}" in line
    for field in ("session_id", "num_itineraries", "response_ms"):
        assert f"${{escHTML(e.{field})}}" in line
        assert f"${{e.{field}}}" not in line
    assert "${e.status}" not in line


def test_no_results_names_the_sessions_that_refused_the_date(template_text: str) -> None:
    html = _function(template_text, "_noResultsHtml")

    assert "e.reason === 'outside_timetable'" in html
    assert ".map((e) => escHTML(e.session_id))" in html
    assert "return '<p>No itineraries found.</p>';" in html
    assert "_noResultsHtml(payload)" in _function(template_text, "render")


def test_the_form_warns_on_a_past_date(template_text: str) -> None:
    assert re.search(
        r'<input type="datetime-local" id="depart" name="depart" aria-describedby="depart-warn">\n'
        r'\s*<p id="depart-warn" class="depart-warn" hidden>',
        template_text,
    )
    warn = _function(template_text, "_updateDepartWarning")
    assert "value.slice(0, 10) < today" in warn
    assert "_localIsoMinutes(new Date())" in warn
    assert "addEventListener('input', _updateDepartWarning)" in template_text
    # Once more after the URL prefill, which can set a past date.
    prefill = template_text[template_text.index("(function prefillFromQuery() {") :]
    assert "_updateDepartWarning();\n})();" in prefill


def test_the_default_date_is_local_time(template_text: str) -> None:
    local = _function(template_text, "_localIsoMinutes")
    for part in ("getFullYear()", "getMonth() + 1", "getDate()", "getHours()", "getMinutes()"):
        assert part in local
    assert ".toISOString().slice(0,16)" not in template_text
    assert "document.getElementById('depart').value = _localIsoMinutes(now);" in template_text


def test_the_warning_colour_is_one_of_the_contrast_safe_ones(template_text: str) -> None:
    # #5b6470 is CLAUDE.md's contrast-safe muted text (>= 4.5:1 on white and on --paper).
    assert (
        ".depart-warn { margin: 0.25rem 0 0; font-size: 0.8rem; color: #5b6470; }" in template_text
    )
