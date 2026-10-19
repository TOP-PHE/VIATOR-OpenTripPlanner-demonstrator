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
  a later call asks again;
- the provider list fills the session pickers of the cross-border cards only.

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
    """The source of `function <name>()` up to its closing brace."""
    text = TEMPLATE.read_text(encoding="utf-8")
    start = text.index(f"function {name}()")
    return text[start : text.index("\n}\n", start) + 2]


def _run(name: str, variable: str, answers: list[Any], rounds: list[int]) -> dict[str, Any]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = _SCRIPT % {
        "answers": json.dumps(answers),
        "variable": variable,
        "function": _function(name),
        "name": name,
        "rounds": json.dumps(rounds),
    }
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


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


def test_the_provider_list_fills_the_pickers_of_cross_border_cards_only() -> None:
    """Every card carries the hidden derived-source pickers; filling them for
    each card asked for the session list once per card."""
    text = TEMPLATE.read_text(encoding="utf-8")
    start = text.index("function populateProvidersList(")
    body = text[start : text.index("\n}\n", start)]
    assert "populateDerivedCard(c)" in body
    assert (
        "if (c.querySelector('.prov-tt-source')?.value === 'cross_border_filter') "
        "populateDerivedCard(c);"
    ) in body
    assert "if (c.querySelector('.prov-derived-session')) populateDerivedCard" not in text
