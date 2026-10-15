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
const buttons = {
  'refresh-providers': {dataset: {}, title: 'Download every provider', disabled: false},
  'refresh-osm': {dataset: {}, title: 'Re-download the OSM PBF', disabled: false},
};
const listeners = {};
const document = {
  visibilityState: 'visible',
  createElement: tag => new El(tag),
  querySelector: sel => {
    if (sel.includes('refresh-progress')) return section;
    const m = sel.match(/data-action="([a-z-]+)"/);
    return m ? buttons[m[1]] : null;
  },
  addEventListener: (name, fn) => { listeners[name] = fn; },
};
const window = {addEventListener: (name, fn) => { listeners['window:' + name] = fn; }};
const CSS = {escape: s => s};
const dump = el => ({tag: el.tag, cls: el.className, state: el.dataset.state, text: el._text,
                     children: el.children.map(dump)});
"""

_RENDER = (
    _FAKE_DOM
    + """
%(block)s
renderRefreshProgress('zz', %(body)s);
console.log(JSON.stringify({hidden: section.hidden, tree: dump(section), created, buttons}));
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
  progressPoll('zz').expanded = %(expanded)s;
  for (let i = 0; i < answers.length; i++) {
    await loadRefreshProgress('zz');
    if (delays.length < i + 1) break;
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
    assert f"Skipped (kept previous file): {hostile}" in lines
    # One block, a title and its lines: no element was made from the value.
    assert result["created"] == 2 + len(lines)


def test_a_running_refresh_shows_counts_bytes_and_elapsed() -> None:
    result = _render({"providers": _record(), "osm": None})
    assert result["hidden"] is False
    block = result["tree"]["children"][0]
    assert block["state"] == "running"
    assert _lines(result) == [
        "Provider refresh",
        "Running — 9 / 99 done · 1 skipped (kept previous file) · 3 min 19 s",
        "Now: provider[ZZRAIL].timetable(gtfs) — 49.5 MB of 99.0 MB (50 %)",
        "Skipped (kept previous file): provider[ZZBUS].timetable(gtfs)",
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
    assert lines[1].endswith(
        "— 90 fetched · 8 unchanged · 1 skipped (kept previous file) (took 33 min 0 s)"
    )
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


def _step(step: str | None, **changes: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "key": "provider[ZZRAIL].timetable(netex_epip)",
        "bytes": 2_000_000_000,
        "expected": 2_000_000_000,
        "step": step,
        "scanned": None,
        "scan_total": None,
        "elapsed_seconds": 18 * 60,
    }
    item.update(changes)
    return item


def _now_line(item: dict[str, Any]) -> str:
    lines = _lines(_render({"providers": _record(current=[item], failed_keys=[])}))
    return next(line for line in lines if line.startswith("Now: "))


@pytest.mark.parametrize(
    ("item", "line"),
    [
        (
            _step("downloading", bytes=49_500_000, expected=99_000_000, elapsed_seconds=99),
            "Now: provider[ZZRAIL].timetable(netex_epip) — downloading, 49.5 MB of 99.0 MB"
            " (50 %) · for 1 min 39 s",
        ),
        (
            _step("downloading", bytes=None, expected=None, elapsed_seconds=9),
            "Now: provider[ZZRAIL].timetable(netex_epip) — downloading · for 9 s",
        ),
        (
            _step("verifying"),
            "Now: provider[ZZRAIL].timetable(netex_epip) — verifying (format check)"
            " · for 18 min 0 s",
        ),
        (
            _step("checking_structure", scanned=12_300_000_000, scan_total=40_100_000_000),
            "Now: provider[ZZRAIL].timetable(netex_epip) — checking structure,"
            " 12.3 GB of 40.1 GB (30 %) · for 18 min 0 s",
        ),
        (
            _step("checking_structure", scanned=990_000, scan_total=None),
            "Now: provider[ZZRAIL].timetable(netex_epip) — checking structure,"
            " 990.0 KB scanned · for 18 min 0 s",
        ),
        (
            _step("checking_structure"),
            "Now: provider[ZZRAIL].timetable(netex_epip) — checking structure · for 18 min 0 s",
        ),
        (
            _step("storing", elapsed_seconds=None),
            "Now: provider[ZZRAIL].timetable(netex_epip) — storing",
        ),
        (
            _step(None, key="cross-border filters", bytes=None, expected=None, elapsed_seconds=9),
            "Now: cross-border filters · for 9 s",
        ),
    ],
)
def test_the_current_task_says_which_step_it_is_in(item: dict[str, Any], line: str) -> None:
    assert _now_line(item) == line


def test_a_record_written_before_steps_still_renders_as_before() -> None:
    """Rows written by the previous version: no step, no elapsed per task."""
    legacy = {"key": "provider[ZZRAIL].timetable(gtfs)", "bytes": 1_990_000_000, "expected": None}
    assert _now_line(legacy) == "Now: provider[ZZRAIL].timetable(gtfs) — 2.0 GB received"
    full = {**legacy, "expected": 1_990_000_000}
    assert _now_line(full) == "Now: provider[ZZRAIL].timetable(gtfs) — 2.0 GB of 2.0 GB (100 %)"
    bare = {"key": "osm_pbf"}
    assert _now_line(bare) == "Now: osm_pbf"


def test_a_step_line_never_parses_a_value_as_html() -> None:
    hostile = '<img src=x onerror="alert(99)">'
    item = _step("checking_structure", key=hostile, scanned=9, scan_total=99)
    result = _render({"providers": _record(current=[item], failed_keys=[]), "osm": None})
    lines = _lines(result)
    assert f"Now: {hostile} — checking structure, 9 B of 99 B (9 %) · for 18 min 0 s" in lines
    assert result["created"] == 2 + len(lines)


def test_no_record_hides_the_block() -> None:
    assert _render({"providers": None, "osm": None})["hidden"] is True


def _poll(answers: list[Any], expanded: bool = True) -> dict[str, Any]:
    return _node(
        _POLL
        % {"block": _block(), "answers": json.dumps(answers), "expanded": json.dumps(expanded)}
    )


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
    # Expanded: then a slow idle read, so another tab's refresh shows up.
    result = _poll([running, running, follow, ended])
    assert result["delays"] == [2500, 2500, 10000, 45000]
    assert result["calls"] == 4
    # Collapsed: no read is scheduled at all.
    assert _poll([running], expanded=False) == {"delays": [], "calls": 1}


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
    assert "openRefreshProgress(sid);" in expand
    # Collapsing the row stops its reads.
    assert (
        "if (e.target.getAttribute('aria-expanded') === 'false') closeRefreshProgress(sid);" in text
    )
    for url in ("/sources/refresh`", "/sources/osm/refresh`"):
        handler = text[: text.index(url)]
        handler = handler[handler.rindex("btn.disabled = true") :]
        assert "watchRefreshProgress(sid);" in handler


_EVENTS = (
    _FAKE_DOM
    + """
const reads = [];
const timers = [];
const setTimeout = (fn, ms) => { timers.push(ms); return timers.length; };
const cleared = [];
const clearTimeout = id => { cleared.push(id); };
async function fetch(url) {
  reads.push(url);
  return {ok: true, status: 200, json: async () => ({providers: null, osm: null})};
}
%(block)s
(async () => {
  openRefreshProgress('zz');
  openRefreshProgress('zz-b');
  await new Promise(r => setImmediate(r));
  closeRefreshProgress('zz-b');
  const before = reads.length;
  document.visibilityState = 'hidden';
  listeners.visibilitychange();
  const hidden = reads.length - before;
  document.visibilityState = 'visible';
  listeners.visibilitychange();
  listeners['window:focus']();
  await new Promise(r => setImmediate(r));
  console.log(JSON.stringify({
    hidden, after: reads.slice(before), polls: Object.fromEntries(
      [..._progressPolls].map(([sid, p]) => [sid, {expanded: p.expanded, timer: p.timer}])),
  }));
})();
"""
)


def test_a_tab_back_in_front_rereads_only_the_expanded_rows() -> None:
    result = _node(_EVENTS % {"block": _block()})
    assert result["hidden"] == 0
    assert result["after"] == ["/api/sessions/zz/sources/progress"] * 2
    assert result["polls"]["zz"]["expanded"] is True
    assert result["polls"]["zz-b"] == {"expanded": False, "timer": None}


def test_the_button_of_a_running_kind_is_disabled() -> None:
    result = _render({"providers": _record(), "osm": None})
    providers, osm = result["buttons"]["refresh-providers"], result["buttons"]["refresh-osm"]
    assert providers["disabled"] is True
    assert providers["title"].startswith("A refresh of this kind is running")
    assert osm["disabled"] is False
    assert osm["title"] == "Re-download the OSM PBF"
    ended = _render({"providers": _record(state="finished"), "osm": None})
    assert ended["buttons"]["refresh-providers"]["disabled"] is False
    assert ended["buttons"]["refresh-providers"]["title"] == "Download every provider"


def test_a_silent_follow_up_says_interrupted() -> None:
    record = _record(
        state="finished",
        current=[],
        extract={"state": "interrupted", "updated_at": "2026-10-15T09:40:00+00:00"},
    )
    line = _lines(_render({"providers": record}))[-1]
    assert line.startswith("Station module: interrupted — no news since ")
    assert line.endswith("the next refresh or rebuild sends what is missing")


def _refusal_function() -> str:
    text = TEMPLATE.read_text(encoding="utf-8")
    start = text.index("function refreshRefusalText(body)")
    return text[start : text.index("\n}\n", start) + 3]


def test_the_refusal_toast_shows_the_start_in_the_browsers_format() -> None:
    script = (
        _FAKE_DOM
        + _block()
        + _refusal_function()
        + """
const body = {detail: {error: 'refresh_already_running', scope: 'osm',
                       started_at: '2026-10-15T09:00:00+00:00', message: 'raw'}};
console.log(JSON.stringify({
  text: refreshRefusalText(body),
  local: progressTime('2026-10-15T09:00:00+00:00'),
  other: refreshRefusalText({detail: 'bad request'}),
}));
"""
    )
    result = _node(script)
    assert result["text"] == (
        f"An OSM refresh of this session is already running (started {result['local']})."
        " Its progress is shown under the buttons."
    )
    assert "+00:00" not in result["text"]
    assert result["other"] is None
