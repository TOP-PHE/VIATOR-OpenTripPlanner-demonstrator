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

import json
import re
import shutil
import subprocess
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


def test_a_refused_text_gives_no_station_quietly(template_text: str) -> None:
    """A 422 of the suggest route (a text too short once the module reads its
    punctuation, hyphens and apostrophes as spaces) is "no station": no
    warning, no exception, and the geocoder's rows still show."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    program = (
        "const warned = [];\n"
        "console.warn = (...args) => warned.push(args.length);\n"
        "globalThis.fetch = async () => ({ok: false, status: 422, json: async () => {"
        " throw new Error('zz no body'); }});\n"
        + _function(template_text, "_postJson")
        + "\n_postJson('/api/stations/suggest', {q: 'Zz.'}).then("
        "(rows) => process.stdout.write(JSON.stringify({rows, warned: warned.length})),"
        " (error) => process.stdout.write(JSON.stringify({thrown: String(error)})));\n"
    )
    result = subprocess.run(
        [node, "-e", program], capture_output=True, text=True, check=False, encoding="utf-8"
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"rows": [], "warned": 0}


def test_the_motis_half_is_unchanged(template_text: str) -> None:
    refresh = _refresh(template_text)

    assert re.search(r"^const SUGGEST_MIN = 2;$", template_text, re.M)
    assert "if (q.length < SUGGEST_MIN)" in refresh
    assert "_fetchJson(`/api/geocode?q=${qs}&size=20`, geocodeCall.signal)" in refresh


def test_a_superseded_geocoder_call_is_aborted(template_text: str) -> None:
    """#338: a geocoder call that a newer refresh, a pick or a blur supersedes
    is aborted, so MOTIS stops working for nobody and the server logs it as
    `superseded` (INFO) rather than as a warning. A new refresh aborts the
    previous call first thing, even when it then stops on a short text."""
    setup = template_text[template_text.index("function setupAutocomplete(") :]
    refresh = _refresh(template_text)

    abort = re.search(r"  function abortGeocode\(\) \{.*?\n  \}\n", setup, re.S)
    assert abort, "abortGeocode() not found"
    assert "if (geocodeCall) geocodeCall.abort();" in abort.group(0)
    assert "geocodeCall = null;" in abort.group(0)

    body = refresh.split("{", 1)[1].lstrip()
    assert body.startswith("const mine = ++seq;\n    abortGeocode();")
    assert refresh.index("geocodeCall = new AbortController();") < refresh.index("_fetchJson(")
    assert "++seq; clearTimeout(t); abortGeocode();" in setup.split("function pick(i)", 1)[1]
    blur = re.search(r"inp\.addEventListener\('blur', \(\) => \{(.*?)\n  \}\);", setup, re.S)
    assert blur
    assert blur.group(1).lstrip().startswith("++seq; clearTimeout(t); abortGeocode();")


def test_an_aborted_fetch_is_quiet_and_any_other_failure_still_warns(template_text: str) -> None:
    get = _function(template_text, "_fetchJson")

    assert "await fetch(url, {signal})" in get
    quiet = get.index("if (err && err.name === 'AbortError') return [];")
    assert quiet < get.index("console.warn('typeahead fetch failed:', url, err);")


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


def test_an_older_answer_never_overwrites_a_newer_one(template_text: str) -> None:
    """Each refresh takes a sequence number before anything else (also before
    the early return that hides the box), and drops its answer when a newer
    refresh has started meanwhile: a slow answer can neither overwrite the
    newer list nor re-show a box that was cleared."""
    setup = template_text[template_text.index("function setupAutocomplete(") :]
    refresh = _refresh(template_text)

    assert re.search(r"^  let seq = 0;$", setup[: setup.index("async function refresh")], re.M)
    body = refresh.split("{", 1)[1]
    assert body.lstrip().startswith("const mine = ++seq;")
    guard = refresh.index("if (mine !== seq) return;")
    assert refresh.index("await Promise.all(") < guard < refresh.index("items = _mergeSuggestions(")


def test_a_keystroke_a_pick_and_a_blur_also_drop_an_answer_in_flight(template_text: str) -> None:
    """Without these, an answer still in flight re-shows the old list during
    the debounce after a keystroke, or re-opens the box after a pick or a blur."""
    setup = template_text[template_text.index("function setupAutocomplete(") :]

    pick = re.search(r"  function pick\(i\) \{.*?\n  \}\n", setup, re.S)
    assert pick, "pick() not found"
    assert "++seq; clearTimeout(t);" in pick.group(0)
    assert pick.group(0).index("++seq;") < pick.group(0).index("box.classList.remove('show')")

    assert "++seq; clearTimeout(t); t = setTimeout(refresh, 150);" in setup

    blur = re.search(r"inp\.addEventListener\('blur', \(\) => \{(.*?)\n  \}\);", setup, re.S)
    assert blur, "the blur listener is not a block"
    assert blur.group(1).lstrip().startswith("++seq; clearTimeout(t);")


def test_the_debounce_timer_is_declared_before_the_functions_that_cancel_it(
    template_text: str,
) -> None:
    """pick() cancels `t`; `t` is declared with `seq`, above refresh() and pick()."""
    setup = template_text[template_text.index("function setupAutocomplete(") :]
    declared = re.search(r"^  let t;$", setup, re.M)
    assert declared, "`let t;` is not declared on its own line in setupAutocomplete"
    assert declared.start() < setup.index("async function refresh")
    assert "let t; inp.addEventListener" not in setup
