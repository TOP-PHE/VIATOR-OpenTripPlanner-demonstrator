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
const dump = el => ({tag: el.tag, cls: el.className, state: el.dataset.state,
                     verdict: el.dataset.verdict, text: el._text,
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
    assert block["verdict"] == "running"
    assert _lines(result) == [
        "Provider refresh",
        "In progress — downloading the timetables",
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
    assert lines[1] == "In progress — sending the stop extracts to the station module"
    assert lines[2].startswith("Finished ")
    assert lines[2].endswith(
        "— 90 fetched · 8 unchanged · 1 skipped (kept previous file) (took 33 min 0 s)"
    )
    assert lines[3].startswith("Started ")
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
    assert _lines(failed)[1] == "Not complete — the refresh stopped before its end (see below)"
    assert "unexpected error (runtimeerror)" in _lines(failed)[2]
    assert "9 / 99 done, 90 not reached; see the web log" in _lines(failed)[2]
    assert failed["tree"]["children"][0]["verdict"] == "problems"
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
    head = _lines(interrupted)[2]
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
    assert line.endswith(
        "it is queued again when the web process starts, or a refresh sends what is missing"
    )
    verdict = _lines(_render({"providers": record}))[1]
    assert verdict == "Not complete — the station module did not get every extract (see below)"


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


# ── one provider alone (the provider card's Refresh) ─────────────────────


def _single(**changes: Any) -> dict[str, Any]:
    record = _record(
        scope="provider",
        provider_id="ZZRAIL",
        total=1,
        done=0,
        fetched=0,
        unchanged=0,
        failed=0,
        failed_keys=[],
        elapsed_seconds=99,
        current=[_step("checking_structure", scanned=9_900_000, scan_total=99_000_000)],
    )
    record.update(changes)
    return record


def _titles(result: dict[str, Any]) -> list[tuple[str, str]]:
    return [(block["children"][0]["text"], block["state"]) for block in result["tree"]["children"]]


def test_a_provider_alone_shows_its_steps_above_the_session_result() -> None:
    whole = _record(state="finished", finished_at="2026-10-15T08:00:00+00:00", current=[])
    result = _render({"providers": whole, "osm": None, "single": [_single()]})
    assert _titles(result) == [
        ("Provider refresh — ZZRAIL only", "running"),
        ("Provider refresh", "finished"),
    ]
    assert _lines(result, 0) == [
        "Provider refresh — ZZRAIL only",
        "In progress — downloading the timetables",
        "Running — 0 / 1 done · 0 skipped (kept previous file) · 1 min 39 s",
        "Now: provider[ZZRAIL].timetable(netex_epip) — checking structure,"
        " 9.9 MB of 99.0 MB (10 %) · for 18 min 0 s",
    ]
    assert _lines(result, 1)[2].startswith("Finished ")


def _ended_single(extract: dict[str, Any]) -> dict[str, Any]:
    return _single(
        state="finished",
        finished_at="2026-10-15T09:01:39+00:00",
        done=1,
        fetched=1,
        current=[],
        extract=extract,
    )


def _single_extract_line(extract: dict[str, Any]) -> str:
    return _lines(_render({"single": [_ended_single(extract)]}))[-1]


def _pass(state: str, **counts: Any) -> dict[str, Any]:
    extract: dict[str, Any] = {
        "state": state,
        "total": 1,
        "handled": 1 if state != "running" else 0,
        "sent": 0,
        "held": 0,
        "refused": 0,
        "unavailable": 0,
        "skipped": 0,
        "cancelled": 0,
        "reasons": {},
        "current": None,
        "started_at": "2026-10-15T09:01:40+00:00",
        "finished_at": "2026-10-15T09:02:00+00:00",
    }
    extract.update(counts)
    return extract


def test_a_provider_alone_follows_its_station_module_line() -> None:
    lines = _lines(_render({"single": [_ended_single({"state": "queued"})]}))
    assert lines[1] == "In progress — sending the stop extracts to the station module"
    assert lines[2].startswith("Finished ")
    assert lines[2].endswith(
        "— 1 fetched · 0 unchanged · 0 skipped (kept previous file) (took 1 min 39 s)"
    )
    assert lines[-1] == "Station module: queued — the extracts are sent after the refresh"
    assert _single_extract_line(_pass("running", current="ZZRAIL")) == (
        "Station module: running · now ZZRAIL"
    )
    assert _single_extract_line(_pass("running")) == "Station module: running"
    for counts, outcome in (
        ({"sent": 1}, "sent"),
        ({"held": 1}, "already held"),
        ({"skipped": 1, "reasons": {"budget": 1}}, "skipped (budget)"),
        ({"refused": 1, "reasons": {"duplicate_stop": 1}}, "refused (duplicate_stop)"),
        ({"handled": 0, "total": 0}, "nothing to send"),
    ):
        line = _single_extract_line(_pass("finished", **counts))
        assert line.startswith("Station module: finished ")
        assert line.endswith(f" — {outcome}")
    assert _single_extract_line(_pass("error")).startswith("Station module: stopped by an error ")
    assert _single_extract_line({"state": "off"}) == "Station module: not configured — nothing sent"
    silent = _single_extract_line(
        {"state": "interrupted", "updated_at": "2026-10-15T09:40:00+00:00"}
    )
    assert silent.startswith("Station module: interrupted — no news since ")


def test_a_provider_alone_never_parses_a_value_as_html() -> None:
    hostile = '<img src=x onerror="alert(99)">'
    record = _ended_single(_pass("finished", skipped=1, reasons={hostile: 1}, current=hostile))
    record["provider_id"] = hostile
    result = _render({"single": [record]})
    lines = _lines(result)
    assert lines[0] == f"Provider refresh — {hostile} only"
    assert lines[-1].endswith(f"— skipped ({hostile})")
    assert result["created"] == 2 + len(lines)
    nameless = _render({"single": [{**record, "provider_id": None}]})
    assert _lines(nameless)[0] == "Provider refresh — one provider only"


def test_a_record_without_single_providers_renders_as_before() -> None:
    """A server of the previous version sends no `single`."""
    result = _render({"providers": _record(), "osm": None})
    assert _titles(result) == [("Provider refresh", "running")]
    assert _render({"providers": None, "osm": None, "single": []})["hidden"] is True


def test_refresh_providers_waits_for_a_provider_alone() -> None:
    result = _render({"providers": None, "osm": None, "single": [_single()]})
    providers, osm = result["buttons"]["refresh-providers"], result["buttons"]["refresh-osm"]
    assert providers["disabled"] is True
    assert providers["title"].startswith("A refresh of one provider is running")
    assert osm["disabled"] is False
    ended = _render({"single": [_ended_single({"state": "queued"})]})
    assert ended["buttons"]["refresh-providers"]["disabled"] is False


def test_polling_follows_a_provider_alone_then_stops() -> None:
    def answer(single: dict[str, Any]) -> dict[str, Any]:
        return {"status": 200, "body": {"providers": None, "osm": None, "single": [single]}}

    result = _poll(
        [
            answer(_single()),
            answer(_ended_single({"state": "queued"})),
            answer(_ended_single(_pass("running"))),
            answer(_ended_single(_pass("finished", sent=1))),
        ]
    )
    assert result["delays"] == [2500, 10000, 10000, 45000]


_FINISH = (
    _FAKE_DOM
    + """
const answers = %(answers)s;
let calls = 0;
const statusReads = [];
const form = {tag: 'form', querySelectorAll: () => []};
const pageQuery = document.querySelector;
document.querySelector = sel => (sel === 'form[data-form="config"][data-sid="zz"]' ? form : pageQuery(sel));
function loadProviderStatuses(sid, f) { statusReads.push([sid, f === form, calls]); }
const setTimeout = () => 1;
const clearTimeout = () => {};
async function fetch() {
  const a = answers[Math.min(calls++, answers.length - 1)];
  return {ok: true, status: 200, json: async () => a};
}
%(block)s
(async () => {
  progressPoll('zz').expanded = true;
  for (let i = 0; i < answers.length; i++) await loadRefreshProgress('zz');
  console.log(JSON.stringify({statusReads}));
})();
"""
)


def test_the_feed_status_table_is_read_once_when_everything_has_ended() -> None:
    running = {"single": [_single()]}
    follow = {"single": [_ended_single({"state": "queued"})]}
    ended = {"single": [_ended_single(_pass("finished", sent=1))]}
    answers = [ended, running, follow, ended, ended, running, ended]
    result = _node(_FINISH % {"block": _block(), "answers": json.dumps(answers)})
    # Not on the first read (nothing ran on this page), once after each end.
    assert result["statusReads"] == [["zz", True, 4], ["zz", True, 7]]


def test_a_provider_card_refresh_watches_the_block() -> None:
    text = TEMPLATE.read_text(encoding="utf-8")
    start = text.index("async function refreshOneProvider(form, refreshBtn)")
    handler = text[start : text.index("\n}\n", start)]
    assert handler.index("watchRefreshProgress(sid);") < handler.index("await fetch(")
    assert "loadRefreshProgress(sid);" in handler[handler.index("finally") :]


def test_the_refusal_toast_names_the_provider_alone() -> None:
    script = (
        _FAKE_DOM
        + _block()
        + _refusal_function()
        + """
const at = '2026-10-15T09:00:00+00:00';
console.log(JSON.stringify({
  local: progressTime(at),
  same: refreshRefusalText({detail: {error: 'refresh_already_running', scope: 'provider',
                                     provider_id: 'ZZRAIL', started_at: at}}),
  whole: refreshRefusalText({detail: {error: 'provider_refresh_running', scope: 'provider',
                                      provider_id: 'ZZRAIL', started_at: at}}),
  session: refreshRefusalText({detail: {error: 'refresh_already_running', scope: 'providers',
                                        provider_id: null, started_at: at}}),
}));
"""
    )
    result = _node(script)
    local = result["local"]
    assert result["same"] == (
        f"A refresh of provider ZZRAIL is already running (started {local})."
        " Its progress is shown under the buttons."
    )
    assert result["whole"] == (
        f"A refresh of provider ZZRAIL alone is running (started {local}); Refresh providers"
        " downloads it too. Its progress is shown under the buttons."
    )
    assert result["session"].startswith("A provider refresh of this session is already running")


def test_a_card_whose_pass_is_queued_stays_while_the_session_refresh_runs() -> None:
    """Review round 1: the server keeps the card's record while its pass is
    queued or running; the page shows it beside the session's refresh and
    keeps polling until the pass ends."""
    card = _ended_single({"state": "queued"})
    result = _render({"providers": _record(), "osm": None, "single": [card]})
    assert _titles(result) == [
        ("Provider refresh — ZZRAIL only", "finished"),
        ("Provider refresh", "running"),
    ]
    assert _lines(result, 0)[-1] == (
        "Station module: queued — the extracts are sent after the refresh"
    )
    whole_done = _record(state="finished", current=[])
    answers = [
        {"status": 200, "body": {"providers": _record(), "osm": None, "single": [card]}},
        {
            "status": 200,
            "body": {
                "providers": whole_done,
                "osm": None,
                "single": [_ended_single(_pass("running"))],
            },
        },
        {"status": 200, "body": {"providers": whole_done, "osm": None, "single": []}},
    ]
    assert _poll(answers)["delays"] == [2500, 10000, 45000]


_CARDS = (
    _FAKE_DOM
    + """
const cards = %(pids)s.map(pid => {
  const btn = {dataset: {}, title: 'Refresh this provider', disabled: false};
  const card = {querySelector: () => ({value: ' ' + pid + ' '})};
  btn.closest = () => card;
  return btn;
});
cards[2].dataset.busy = '1';
const form = {querySelectorAll: () => cards};
const pageQuery = document.querySelector;
document.querySelector = sel => (sel === 'form[data-form="config"][data-sid="zz"]' ? form : pageQuery(sel));
%(block)s
const states = [];
for (const body of %(bodies)s) {
  renderRefreshProgress('zz', body);
  states.push(cards.map(b => [b.disabled, b.title]));
}
console.log(JSON.stringify(states));
"""
)


def test_a_card_button_is_disabled_while_its_refresh_or_the_session_s_runs() -> None:
    bodies = [
        {"providers": None, "osm": None, "single": [_single()]},
        {"providers": _record(), "osm": None, "single": []},
        {"providers": None, "osm": None, "single": [_ended_single({"state": "off"})]},
    ]
    script = _CARDS % {
        "block": _block(),
        "pids": json.dumps(["ZZRAIL", "ZZBUS", "ZZTRAM"]),
        "bodies": json.dumps(bodies),
    }
    mine, whole, ended = _node(script)
    idle = "Refresh this provider"
    assert mine[0] == [
        True,
        "This provider's refresh is running: its progress is shown under the buttons.",
    ]
    assert mine[1] == [False, idle]
    # A card already clicked on this page stays disabled until its answer.
    assert mine[2] == [True, idle]
    assert [state[0] for state in whole] == [True, True, True]
    assert whole[0][1].startswith("A provider refresh of the whole session is running")
    assert ended[:2] == [[False, idle], [False, idle]]


def test_a_card_click_marks_its_button_busy_until_the_answer() -> None:
    text = TEMPLATE.read_text(encoding="utf-8")
    start = text.index("async function refreshOneProvider(form, refreshBtn)")
    handler = text[start : text.index("\n}\n", start)]
    assert handler.index("refreshBtn.dataset.busy = '1';") < handler.index("await fetch(")
    finally_part = handler[handler.index("finally") :]
    assert finally_part.index("delete refreshBtn.dataset.busy;") < finally_part.index(
        "loadRefreshProgress(sid);"
    )


# ── the verdict: one Refresh, one clear end (2026-10-27) ──────────────────


def _ended_pass(**counts: Any) -> dict[str, Any]:
    return {**_pass("finished", total=4, handled=4), **counts}


def _ended_whole(extract: dict[str, Any] | None, **changes: Any) -> dict[str, Any]:
    record = _record(
        state="finished",
        finished_at="2026-10-27T09:33:00+00:00",
        done=99,
        fetched=90,
        unchanged=9,
        failed=0,
        failed_keys=[],
        current=[],
        extract=extract,
    )
    record.update(changes)
    return record


def _render_whole(record: dict[str, Any]) -> tuple[list[str], str]:
    result = _render({"providers": record})
    return _lines(result), result["tree"]["children"][0]["verdict"]


def test_everything_with_the_module_is_complete() -> None:
    extract = _ended_pass(sent=2, held=1, skipped=1, reasons={"no_rail_stop": 1}, problems=[])
    lines, verdict = _render_whole(_ended_whole(extract))
    assert verdict == "complete"
    assert lines[1].startswith("Complete ")
    assert lines[1].endswith(" — every provider is downloaded and with the station module")
    assert not any(line.startswith("Not with the station module") for line in lines)


def test_feeds_the_module_lacks_are_named_with_their_reason() -> None:
    problems = [
        {"provider": "ZZRAIL", "state": "skipped", "reason": "memory_limit"},
        {"provider": "ZZBUS", "state": "unavailable", "reason": "timeout"},
    ]
    extract = _ended_pass(
        sent=2,
        skipped=1,
        unavailable=1,
        reasons={"memory_limit": 1, "timeout": 1},
        problems=problems,
    )
    lines, verdict = _render_whole(_ended_whole(extract, failed=1))
    assert verdict == "problems"
    assert lines[1].startswith("Complete with problems ")
    assert lines[1].endswith(
        " — 1 provider not downloaded (kept the previous file) · 2 feeds not with the station"
        " module (see below)"
    )
    assert lines[-2] == (
        "Not with the station module: ZZRAIL (skipped: memory_limit),"
        " ZZBUS (module unavailable: timeout)"
    )
    assert lines[-1].startswith("The Station module column of the feed table gives each reason.")


def test_more_than_ten_missing_feeds_are_counted() -> None:
    problems = [{"provider": f"ZZ{i}", "state": "skipped", "reason": "busy"} for i in range(12)]
    extract = _ended_pass(total=12, handled=12, skipped=12, reasons={"busy": 12}, problems=problems)
    lines, _ = _render_whole(_ended_whole(extract))
    missing = next(line for line in lines if line.startswith("Not with the station module"))
    assert missing.endswith("ZZ9 (skipped: busy) and 2 more")


def test_a_record_before_the_list_of_problems_still_gives_its_verdict() -> None:
    """Written by the previous version: counts and reason words only."""
    extract = _ended_pass(sent=2, skipped=2, reasons={"no_feed_key": 1, "budget": 1})
    lines, verdict = _render_whole(_ended_whole(extract))
    assert verdict == "problems"
    assert lines[1].endswith(" — 1 feed not with the station module (see below)")
    assert not any(line.startswith("Not with the station module") for line in lines)


@pytest.mark.parametrize(
    ("extract", "first", "verdict"),
    [
        (None, "every provider is downloaded", "complete"),
        (
            {"state": "off"},
            "every provider is downloaded (the station module is not configured)",
            "complete",
        ),
        (
            _pass("cancelled"),
            "Not complete — the station module did not get every extract",
            "problems",
        ),
        (_pass("error"), "Not complete — the station module did not get every extract", "problems"),
    ],
    ids=["no_follow_up", "off", "cancelled", "error"],
)
def test_every_end_of_the_follow_up_has_its_verdict(
    extract: dict[str, Any] | None, first: str, verdict: str
) -> None:
    lines, shown = _render_whole(_ended_whole(extract))
    assert first in lines[1]
    assert shown == verdict


def test_a_provider_alone_says_the_provider_is_complete() -> None:
    lines = _lines(_render({"single": [_ended_single(_pass("finished", sent=1))]}))
    assert lines[1].endswith(" — the provider is downloaded and with the station module")


def test_the_feed_being_sent_says_for_how_long() -> None:
    running = {
        **_pass("running", total=9, handled=3, sent=3),
        "current": "ZZRAIL",
        "current_elapsed_seconds": 25 * 60 + 3,
    }
    lines, _ = _render_whole(_ended_whole(running))
    assert lines[-1].endswith("— 3 sent · now ZZRAIL for 25 min 3 s")
    assert _single_extract_line(running) == "Station module: running · now ZZRAIL for 25 min 3 s"


def test_a_pass_queued_again_after_a_restart_says_so() -> None:
    lines, verdict = _render_whole(_ended_whole({"state": "queued", "resumed": True}))
    assert lines[-1] == (
        "Station module: queued again after a restart of the web process — the extracts are"
        " sent now"
    )
    assert verdict == "running"


def test_a_missing_feed_never_parses_a_value_as_html() -> None:
    hostile = '<img src=x onerror="alert(99)">'
    extract = _ended_pass(
        sent=3,
        skipped=1,
        reasons={"busy": 1},
        problems=[{"provider": hostile, "state": "skipped", "reason": hostile}],
    )
    result = _render({"providers": _ended_whole(extract)})
    lines = _lines(result)
    assert f"Not with the station module: {hostile} (skipped: {hostile})" in lines
    assert result["created"] == 2 + len(lines)
