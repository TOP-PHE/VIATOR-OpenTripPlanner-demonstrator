"""The refresh progress block of the Sessions page (issue #359, sessions.html).

The JS block between `// ── Refresh progress (issue #359)` and
`// ── end refresh progress` is executed in Node with a minimal fake DOM:

- every value of the record lands in `textContent`, never parsed as HTML
  (a hostile label stays text, and the block never uses `innerHTML`);
- the lines say what each state means (running with bytes, finished with
  counts, failed, interrupted, the station-module follow-up);
- the page polls while a refresh runs, slower while only the follow-up runs,
  stops once everything has ended, and backs off on errors.

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
_START = "// ── Refresh progress (issue #359)"
_END = "// ── end refresh progress"

_FAKE_DOM = """
let created = 0;
class El {
  constructor(tag) { this.tag = tag; this.children = []; this.dataset = {}; this.className = '';
                     this.hidden = false; this._text = ''; created++; }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() { return this._text + this.children.map(c => c.textContent).join('\\n'); }
  append(...c) { this.children.push(...c); }
  replaceChildren(...c) { this.children = c; this._text = ''; }
}
const section = new El('div'); section.hidden = true;
const document = {
  createElement: tag => new El(tag),
  querySelector: sel => (sel.includes('refresh-progress') ? section : null),
};
const CSS = {escape: s => s};
const dump = el => ({tag: el.tag, cls: el.className, state: el.dataset.state, text: el._text,
                     children: el.children.map(dump)});
"""

_RENDER = (
    _FAKE_DOM
    + """
%(block)s
renderRefreshProgress('zz', %(body)s);
console.log(JSON.stringify({hidden: section.hidden, tree: dump(section), created}));
"""
)

_POLL = (
    _FAKE_DOM
    + """
const answers = %(answers)s;
const delays = [];
let calls = 0;
const setTimeout = (fn, ms) => { delays.push(ms); return delays.length; };
const clearTimeout = () => {};
async function fetch() {
  const a = answers[Math.min(calls++, answers.length - 1)];
  if (a === 'network') throw new Error('offline');
  return {ok: a.status === 200, status: a.status, json: async () => a.body};
}
%(block)s
(async () => {
  for (let i = 0; i < answers.length; i++) {
    await loadRefreshProgress('zz');
    if (delays[delays.length - 1] === undefined || delays.length < i + 1) break;
  }
  console.log(JSON.stringify({delays, calls}));
})();
"""
)


def _block() -> str:
    text = TEMPLATE.read_text(encoding="utf-8")
    return text[text.index(_START) : text.index(_END)]


def _node(script: str) -> dict[str, Any]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def _render(body: dict[str, Any] | None) -> dict[str, Any]:
    return _node(_RENDER % {"block": _block(), "body": json.dumps(body)})


def _lines(result: dict[str, Any], index: int = 0) -> list[str]:
    block = result["tree"]["children"][index]
    return [child["text"] for child in block["children"]]


def _record(**changes: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "scope": "providers",
        "state": "running",
        "started_at": "2026-10-15T09:00:00+00:00",
        "finished_at": None,
        "heartbeat_at": "2026-10-15T09:03:19+00:00",
        "elapsed_seconds": 199,
        "total": 99,
        "done": 9,
        "fetched": 5,
        "unchanged": 3,
        "failed": 1,
        "not_reached": 0,
        "phase": "downloading",
        "current": [
            {"key": "provider[ZZRAIL].timetable(gtfs)", "bytes": 49_500_000, "expected": 99_000_000}
        ],
        "failed_keys": ["provider[ZZBUS].timetable(gtfs)"],
        "error": None,
        "previous": None,
        "extract": None,
    }
    record.update(changes)
    return record


def test_the_block_never_parses_a_value_as_html() -> None:
    assert "innerHTML" not in _block()
    assert "insertAdjacentHTML" not in _block()
    hostile = '<img src=x onerror="alert(99)">'
    result = _render(
        {
            "providers": _record(
                current=[{"key": hostile, "bytes": None, "expected": None}],
                failed_keys=[hostile],
            ),
            "osm": None,
        }
    )
    lines = _lines(result)
    assert f"Now: {hostile}" in lines
    assert f"Failed: {hostile}" in lines
    # One block, a title and its lines: no element was made from the value.
    assert result["created"] == 2 + len(lines)


def test_a_running_refresh_shows_counts_bytes_and_elapsed() -> None:
    result = _render({"providers": _record(), "osm": None})
    assert result["hidden"] is False
    block = result["tree"]["children"][0]
    assert block["state"] == "running"
    assert _lines(result) == [
        "Provider refresh",
        "Running — 9 / 99 done · 1 failed · 3 min 19 s",
        "Now: provider[ZZRAIL].timetable(gtfs) — 49.5 MB of 99.0 MB (50 %)",
        "Failed: provider[ZZBUS].timetable(gtfs)",
    ]


def test_bytes_of_an_unknown_size_and_a_task_without_bytes() -> None:
    result = _render(
        {
            "providers": _record(
                current=[{"key": "cross-border filters", "bytes": None, "expected": None}],
                failed_keys=[],
            ),
            "osm": _record(
                scope="osm",
                total=1,
                done=0,
                failed=0,
                current=[{"key": "osm_pbf", "bytes": 1_990_000_000, "expected": None}],
                failed_keys=[],
            ),
        }
    )
    assert "Now: cross-border filters" in _lines(result, 0)
    osm = _lines(result, 1)
    assert osm[0] == "OSM refresh"
    assert "Now: osm_pbf — 2.0 GB received" in osm


def test_the_end_result_and_the_follow_up() -> None:
    finished = _record(
        state="finished",
        finished_at="2026-10-15T09:33:00+00:00",
        elapsed_seconds=1980,
        done=99,
        fetched=90,
        unchanged=8,
        current=[],
        extract={
            "state": "running",
            "total": 99,
            "handled": 12,
            "sent": 5,
            "held": 6,
            "refused": 0,
            "unavailable": 0,
            "skipped": 1,
            "cancelled": 0,
            "reasons": {"budget": 1},
            "current": "ZZRAIL",
            "started_at": "2026-10-15T09:33:01+00:00",
            "finished_at": None,
        },
    )
    lines = _lines(_render({"providers": finished, "osm": None}))
    assert lines[1].startswith("Finished ")
    assert lines[1].endswith("— 90 fetched · 8 unchanged · 1 failed (took 33 min 0 s)")
    assert lines[2].startswith("Started ")
    assert lines[-1] == (
        "Station module: 12 / 99 feeds handled — 5 sent · 6 already held · 1 skipped"
        " (budget 1) · now ZZRAIL"
    )
    for state, text in (
        ("queued", "Station module: queued — the extracts are sent after the refresh"),
        ("off", "Station module: not configured — nothing sent"),
    ):
        lines = _lines(_render({"providers": {**finished, "extract": {"state": state}}}))
        assert lines[-1] == text


def test_failed_and_interrupted_refreshes_say_what_happened() -> None:
    failed = _render(
        {"providers": _record(state="failed", error="runtimeerror", not_reached=90, current=[])}
    )
    assert "unexpected error (runtimeerror)" in _lines(failed)[1]
    assert "9 / 99 done, 90 not reached; see the web log" in _lines(failed)[1]
    assert failed["tree"]["children"][0]["state"] == "failed"
    interrupted = _render(
        {
            "providers": _record(
                state="interrupted",
                finished_at="2026-10-15T09:03:19+00:00",
                current=[],
                previous=None,
            )
        }
    )
    head = _lines(interrupted)[1]
    assert head.startswith("Interrupted — no news from the server since ")
    assert head.endswith("9 / 99 done, 90 not reached; a new refresh may start")
    replaced = _render(
        {"providers": _record(previous={"state": "interrupted", "started_at": "2026-10-15"})}
    )
    assert any(
        line.startswith("The refresh before this one was interrupted") for line in _lines(replaced)
    )


def test_no_record_hides_the_block() -> None:
    assert _render({"providers": None, "osm": None})["hidden"] is True


def _poll(answers: list[Any]) -> dict[str, Any]:
    return _node(_POLL % {"block": _block(), "answers": json.dumps(answers)})


def test_polling_follows_the_refresh_then_stops() -> None:
    running = {"status": 200, "body": {"providers": _record(), "osm": None}}
    follow = {
        "status": 200,
        "body": {
            "providers": _record(state="finished", extract={"state": "queued"}),
            "osm": None,
        },
    }
    ended = {
        "status": 200,
        "body": {"providers": _record(state="finished", extract={"state": "off"}), "osm": None},
    }
    result = _poll([running, running, follow, ended])
    assert result["delays"] == [2500, 2500, 10000]
    assert result["calls"] == 4


def test_errors_back_off_up_to_thirty_seconds() -> None:
    result = _poll(["network", {"status": 502}, "network", "network", "network", "network"])
    assert result["delays"] == [5000, 10000, 20000, 30000, 30000, 30000]


def test_a_refused_read_stops_polling() -> None:
    result = _poll([{"status": 403}])
    assert result == {"delays": [], "calls": 1}


def test_the_page_restores_the_block_and_both_buttons_watch_it() -> None:
    text = TEMPLATE.read_text(encoding="utf-8")
    assert (
        '<div class="refresh-progress" data-role="refresh-progress" data-sid="{{ s.id }}"'
        ' aria-live="polite" hidden></div>'
    ) in text
    # Restored from the server on every expand of a session row.
    expand = text[text.index("loadProviderStatuses(sid, cf);") :][:200]
    assert "loadRefreshProgress(sid);" in expand
    for url in ("/sources/refresh`", "/sources/osm/refresh`"):
        handler = text[: text.index(url)]
        handler = handler[handler.rindex("btn.disabled = true") :]
        assert "watchRefreshProgress(sid);" in handler
