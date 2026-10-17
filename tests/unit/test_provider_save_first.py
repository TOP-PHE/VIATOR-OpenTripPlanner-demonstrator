"""A provider card's Refresh and Upload wait for Save config (17 October).

The owner clicked Refresh on a card not saved yet and got a raw 404 that
listed every provider id of the session. Now the page disables the card's
Refresh and Upload, "Save config first" as the tooltip, while the card is
not in the saved configuration or has unsaved changes; and the API's 404 is
one short sentence. The page's JS runs in Node with a minimal fake DOM.
Invented values only: provider ids ZZ..., session `zz`.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi import BackgroundTasks, HTTPException

from app.api.admin import sessions

TEMPLATE = Path(__file__).resolve().parents[2] / "app" / "templates" / "admin" / "sessions.html"
_START = "// ── Refresh progress (issue #359)"
_END = "// ── end save first"
SAVE_FIRST = "Save config first: this provider card has changes that are not saved yet."

_DOM = """
function makeButton(action, title) {
  return {action, title, dataset: {}, disabled: false};
}
function makeCard(pid) {
  const idInput = {value: ' ' + pid + ' '};
  const buttons = [makeButton('refresh-provider', 'Refresh this provider'),
                   makeButton('upload-provider', 'Upload')];
  const card = {
    dataset: {}, idInput, buttons,
    querySelector: sel => (sel === '.prov-id' ? idInput : null),
    querySelectorAll: sel => (sel.includes('refresh-provider') ? buttons : []),
  };
  for (const b of buttons) b.closest = () => card;
  return card;
}
const row = {dataset: {config: JSON.stringify(%(saved)s)}};
const cards = %(pids)s.map(makeCard);
const form = {
  dataset: {sid: 'zz'},
  querySelectorAll: sel => (sel === '.provider-card' ? cards
    : sel.includes('refresh-provider') ? cards.map(c => c.buttons[0]) : []),
};
const document = {
  querySelector: sel => (sel.startsWith('tr[') ? row
    : sel.startsWith('form[') ? form : null),
  addEventListener: () => {},
};
const window = {addEventListener: () => {}};
const CSS = {escape: s => s};
const state = () => cards.map(c => c.buttons.map(b => [b.disabled, b.title]));
const target = (card, cls) => ({closest: () => card, matches: sel => sel === cls});
"""


def _block() -> str:
    text = TEMPLATE.read_text(encoding="utf-8")
    return text[text.index(_START) : text.index(_END)]


def _node(steps: str, saved: dict[str, Any], pids: list[str]) -> Any:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = (
        _DOM % {"saved": json.dumps(saved), "pids": json.dumps(pids)}
        + _block()
        + "\nconst out = [];\n"
        + steps
        + "\nconsole.log(JSON.stringify(out));\n"
    )
    run = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True)
    return json.loads(run.stdout)


def _saved(*pids: str) -> dict[str, Any]:
    return {"sources": {"providers": [{"id": pid} for pid in pids]}}


IDLE = [[False, "Refresh this provider"], [False, "Upload"]]
WAITING = [[True, SAVE_FIRST], [True, SAVE_FIRST]]


def test_a_card_not_saved_waits_for_save_config() -> None:
    (before,) = _node(
        "syncSaveFirst(form); out.push(state());", _saved("ZZRAIL"), ["ZZRAIL", "ZZNEW", ""]
    )
    assert before == [IDLE, WAITING, WAITING]


def test_an_edited_card_waits_and_a_save_enables_it() -> None:
    steps = """
syncSaveFirst(form);
markCardEdited(form, target(cards[0], '.prov-label'));
out.push(state());
markCardEdited(form, target(cards[1], '.prov-tt-file'));
out.push(state());
row.dataset.config = JSON.stringify({sources: {providers: [{id: 'ZZRAIL'}, {id: 'ZZBUS'}]}});
markCardsSaved(form);
out.push(state());
"""
    edited, file_picked, saved = _node(steps, _saved("ZZRAIL", "ZZBUS"), ["ZZRAIL", "ZZBUS"])
    assert edited == [WAITING, IDLE]
    assert file_picked == [WAITING, IDLE]  # choosing the file to upload is not a change
    assert saved == [IDLE, IDLE]


def test_a_renamed_card_waits() -> None:
    steps = """
syncSaveFirst(form);
cards[0].idInput.value = 'ZZRENAMED';
markCardEdited(form, target(cards[0], '.prov-id'));
out.push(state());
"""
    (renamed,) = _node(steps, _saved("ZZRAIL"), ["ZZRAIL"])
    assert renamed == [WAITING]


def test_save_first_and_a_running_refresh_both_hold_the_button() -> None:
    """The unsaved card's reason comes first; once saved, the running
    refresh still holds Refresh (not Upload), and a click waiting for its
    answer keeps its button disabled."""
    running = {
        "providers": {"state": "running", "scope": "providers"},
        "osm": None,
        "single": [],
    }
    steps = f"""
syncSaveFirst(form);
markCardEdited(form, target(cards[0], '.prov-label'));
renderCardButtons('zz', {json.dumps(running)});
out.push(state());
markCardsSaved(form);
out.push(state());
renderCardButtons('zz', {{providers: null, osm: null, single: []}});
cards[0].buttons[1].dataset.busy = '1';
cardButtonState(cards[0].buttons[1]);
out.push(state());
"""
    both, refresh_only, clicked = _node(steps, _saved("ZZRAIL"), ["ZZRAIL"])
    assert both == [WAITING]
    assert refresh_only[0][0][0] is True
    assert refresh_only[0][0][1].startswith("A provider refresh of the whole session is running")
    assert refresh_only[0][1] == [False, "Upload"]
    assert clicked == [[[False, "Refresh this provider"], [True, "Upload"]]]


def test_an_unreadable_saved_config_holds_every_card() -> None:
    steps = "row.dataset.config = '{'; syncSaveFirst(form); out.push(state());"
    (held,) = _node(steps, _saved("ZZRAIL"), ["ZZRAIL"])
    assert held == [WAITING]


def test_the_page_wires_save_first() -> None:
    text = TEMPLATE.read_text(encoding="utf-8")
    populate = text[text.index("function populateProvidersList(form, sources)") :]
    populate = populate[: populate.index("\n}\n")]
    assert populate.index("syncProvidersToolbar(form);") < populate.index("syncSaveFirst(form);")
    add = text[text.index('button[data-action="add-provider"]\')?.addEventListener') :]
    assert "syncSaveFirst(form);" in add[: add.index("});")]
    assert "form.addEventListener('input', (e) => markCardEdited(form, e.target));" in text
    assert "form.addEventListener('change', (e) => markCardEdited(form, e.target));" in text
    saved = text.index("row.dataset.config = JSON.stringify(updated.config);")
    assert text.index("markCardsSaved(form);", saved) - saved < 80
    for handler in ("async function refreshOneProvider(", "async function uploadToProvider("):
        body = text[text.index(handler) :]
        body = body[: body.index("\n}\n")]
        finally_part = body[body.index("finally") :]
        assert "cardButtonState(" in finally_part
        assert ".disabled = false" not in finally_part


# ── The API's answer ──────────────────────────────────────────────────────


class _Db:
    def __init__(self, row: Any) -> None:
        self.row = row

    def get(self, *_args: Any) -> Any:
        return self.row


def test_refreshing_a_provider_not_saved_answers_one_short_sentence() -> None:
    from starlette.requests import Request

    from app.models import Session as SessionRow
    from app.security import CurrentUser

    providers = [
        {"id": f"ZZ{n:02d}", "timetable": {"format": "gtfs", "url": f"https://zz.invalid/{n}"}}
        for n in range(99)
    ]
    row = SessionRow(id="zz", name="Zz", config={"sources": {"providers": providers}})
    actor = CurrentUser(id=uuid.uuid4(), username="zz@example.invalid", role="content_manager")
    request = Request({"type": "http", "headers": [], "client": ("127.0.0.1", 1)})
    refresh = sessions.refresh_provider("zz", "ZZNEW", request, _Db(row), actor, BackgroundTasks())
    with pytest.raises(HTTPException) as refused:
        asyncio.run(refresh)
    assert refused.value.status_code == 404
    assert refused.value.detail == (
        "This provider is not in the saved configuration of the session: "
        "save the configuration first."
    )
    assert "ZZ0" not in str(refused.value.detail)
