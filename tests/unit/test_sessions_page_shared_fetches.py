"""The shared lists of the Sessions page (sessions.html): the session list of
the derived-card pickers (`derivedSessions`, GET /api/sessions) and the
credential list of the timetable pickers (`loadCredentialList`).

A provider list renders many cards at once. Each card asking for the session
list before the first answer came back used to send its own GET
/api/sessions (each answer carries every session's full config): on a
session with about a hundred providers the flood exhausted the database pool
and the refresh started meanwhile stopped. Here the functions run in Node
with a fake `fetch`:

- calls made while the request is on its way share the one request;
- a good answer is kept, a failed one (HTTP error, network error) is not, so
  a later call asks again; a saved configuration forgets the session list;
- the provider list fills the session pickers of the cross-border cards only,
  and switching a card's source to the cross-border filter fills its own;
- a list that could not be loaded leaves a card's saved link as drawn.

Skipped where Node is not installed (CI runners have it).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

TEMPLATE = Path(__file__).resolve().parents[2] / "app" / "templates" / "admin" / "sessions.html"

# (function, its promise variable, the URL it asks)
_LISTS = [
    pytest.param("derivedSessions", "derivedSessionsPromise", "/api/sessions", id="sessions"),
    pytest.param(
        "loadCredentialList", "credentialListPromise", "/api/credentials", id="credentials"
    ),
]

_SCRIPT = """
const answers = %(answers)s;
const asked = [];
const console = {warn: () => {}};
function fetch(url) {
  const a = answers[Math.min(asked.length, answers.length - 1)];
  asked.push(url);
  // Answered later, like the network: the callers below ask first.
  return new Promise((resolve, reject) => setTimeout(() => {
    if (a === 'network') reject(new Error('offline'));
    else resolve({ok: a.status === 200, status: a.status, json: async () => a.body});
  }, 5));
}
let %(variable)s = null;
%(function)s
(async () => {
  const rounds = [];
  for (const size of %(rounds)s) {
    const calls = [];
    for (let i = 0; i < size; i++) calls.push(%(name)s());
    rounds.push(await Promise.all(calls));
  }
  process.stdout.write(JSON.stringify({asked, rounds}));
})();
"""

_LIST = [{"id": "zz-one", "name": "ZZ one"}, {"id": "zz-two", "name": "ZZ two"}]


def _function(name: str) -> str:
    """The source of `function <name>(…)` up to its closing brace."""
    text = TEMPLATE.read_text(encoding="utf-8")
    start = text.index(f"function {name}(")
    if text[start - 6 : start] == "async ":
        start -= 6
    return text[start : text.index("\n}\n", start) + 2]


def _node(script: str) -> Any:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def _run(name: str, variable: str, answers: list[Any], rounds: list[int]) -> dict[str, Any]:
    script = _SCRIPT % {
        "answers": json.dumps(answers),
        "variable": variable,
        "function": _function(name),
        "name": name,
        "rounds": json.dumps(rounds),
    }
    result: dict[str, Any] = _node(script)
    return result


@pytest.mark.parametrize(("name", "variable", "url"), _LISTS)
def test_calls_made_while_the_list_loads_share_one_request(
    name: str, variable: str, url: str
) -> None:
    result = _run(name, variable, [{"status": 200, "body": _LIST}], rounds=[9, 3])
    assert result["asked"] == [url]
    assert result["rounds"] == [[_LIST] * 9, [_LIST] * 3]


@pytest.mark.parametrize(("name", "variable", "url"), _LISTS)
@pytest.mark.parametrize("failure", [{"status": 500, "body": None}, "network"], ids=["500", "net"])
def test_a_failed_answer_is_not_kept(name: str, variable: str, url: str, failure: Any) -> None:
    answers = [failure, {"status": 200, "body": _LIST}]
    result = _run(name, variable, answers, rounds=[4, 2, 2])
    # The failed round shares one request and gets an empty list; the next
    # round asks again and keeps the good answer for the third.
    assert result["asked"] == [url, url]
    assert result["rounds"] == [[[]] * 4, [_LIST] * 2, [_LIST] * 2]


_CARDS = """
const filled = [];
function populateDerivedCard(card) { filled.push(card.name); }
const ROWS = ['.prov-tt-url-row', '.prov-tt-upload-row', '.prov-tt-derived-row', '.prov-tt-nap-row',
              '.prov-tt-default-rail-row', '.prov-tt-cred-row', '.prov-refresh-btn'];
function card(name, source) {
  const parts = {'.prov-tt-source': {value: source}, '.prov-derived-session': {}};
  for (const sel of ROWS) parts[sel] = {hidden: false};
  return {name, parts, querySelector: sel => parts[sel] || null};
}
"""


def test_the_provider_list_fills_the_pickers_of_cross_border_cards_only() -> None:
    """Every card carries the hidden derived-source pickers; filling them for
    each card asked for the session list once per card."""
    script = (
        _CARDS
        + """
function providerCardHTML() { return ''; }
function syncProvidersToolbar() {}
function syncSaveFirst() {}
function fillCredentialPickers() {}
const cards = [card('url', 'url'), card('xb', 'cross_border_filter'), card('nap', 'nap'),
               card('upload', 'upload'), card('xb2', 'cross_border_filter')];
const rows = {innerHTML: '', insertAdjacentHTML() {}, querySelectorAll: () => cards};
const form = {querySelector: () => rows};
/*FUNCS*/
populateProvidersList(form, {providers: cards.map(c => ({id: c.name.toUpperCase()}))});
process.stdout.write(JSON.stringify(filled));
"""
    ).replace("/*FUNCS*/", _function("populateProvidersList"))
    assert _node(script) == ["xb", "xb2"]


def _source_toggle() -> str:
    """The `.prov-tt-source` change handler of the config form."""
    text = TEMPLATE.read_text(encoding="utf-8")
    start = text.index("  // v0.1.37 — source toggle")
    return text[start : text.index("\n  });\n", start) + 6]


def test_switching_a_card_to_the_cross_border_filter_fills_its_pickers() -> None:
    script = (
        _CARDS
        + """
const CREDENTIAL_SOURCES = new Set(['url', 'nap']);
let handler = null;
const form = {addEventListener: (name, fn) => { handler = fn; }};
/*FUNCS*/
const seen = {};
for (const source of ['url', 'cross_border_filter', 'nap', 'upload']) {
  const c = card(source, 'url');
  handler({target: {matches: sel => sel === '.prov-tt-source', value: source, closest: () => c}});
  seen[source] = {derived_hidden: c.parts['.prov-tt-derived-row'].hidden,
                  url_hidden: c.parts['.prov-tt-url-row'].hidden};
}
const other = card('other', 'url');
handler({target: {matches: () => false, value: 'cross_border_filter', closest: () => other}});
process.stdout.write(JSON.stringify({filled, seen}));
"""
    ).replace("/*FUNCS*/", _source_toggle())
    result = _node(script)
    assert result["filled"] == ["cross_border_filter"]
    assert result["seen"]["cross_border_filter"] == {"derived_hidden": False, "url_hidden": True}
    assert result["seen"]["url"] == {"derived_hidden": True, "url_hidden": False}


def test_a_saved_configuration_forgets_the_session_list() -> None:
    script = """
const asked = [];
const console = {warn: () => {}};
async function fetch(url) { asked.push(url); return {ok: true, status: 200, json: async () => []}; }
let derivedSessionsPromise = null;
/*FUNCS*/
(async () => {
  await derivedSessions();
  await derivedSessions();
  forgetSessionList();
  await derivedSessions();
  process.stdout.write(JSON.stringify(asked));
})();
""".replace("/*FUNCS*/", _function("derivedSessions") + _function("forgetSessionList"))
    assert _node(script) == ["/api/sessions", "/api/sessions"]


@pytest.mark.parametrize(
    ("anchor", "before"),
    [
        pytest.param(
            "body: JSON.stringify({config: cfg})});", "row.dataset.config", id="config-save"
        ),
        pytest.param("/nap-sources/apply`", "napSyncSessionRow(sid, body.config);", id="nap-apply"),
        pytest.param("/providers/import-from-nap`", "return out;", id="nap-import"),
    ],
)
def test_each_save_of_a_configuration_forgets_the_list_once_it_succeeded(
    anchor: str, before: str
) -> None:
    text = TEMPLATE.read_text(encoding="utf-8")
    start = text.index(anchor)
    body = text[start : text.index(before, start)]
    assert "if (!r.ok)" in body
    assert body.rstrip().endswith("forgetSessionList();")


def test_a_list_that_could_not_be_loaded_leaves_the_saved_link_as_drawn() -> None:
    script = """
const console = {warn: () => {}};
async function fetch() { return {ok: false, status: 500}; }
const escAttr = s => s, escHTML = s => s;
let derivedSessionsPromise = null;
/*FUNCS*/
const sessSel = {dataset: {value: 'zz-national'}, innerHTML: 'SAVED-SESSION', value: 'zz-national'};
const provSel = {dataset: {value: 'ZZNAT'}, innerHTML: 'SAVED-PROVIDER', value: 'ZZNAT'};
const c = {querySelector: sel => (sel === '.prov-derived-session' ? sessSel : provSel)};
(async () => {
  await populateDerivedCard(c);
  process.stdout.write(JSON.stringify([sessSel.innerHTML, provSel.innerHTML]));
})();
""".replace(
        "/*FUNCS*/",
        "".join(
            _function(name)
            for name in (
                "derivedSessions",
                "_selectOptions",
                "fillDerivedProviderSelect",
                "populateDerivedCard",
            )
        ),
    )
    assert _node(script) == ["SAVED-SESSION", "SAVED-PROVIDER"]
