"""The "Stations" admin page (`/admin/master/stations`), a search page since
MSMM step 3 (design 21.3, decisions 53 to 55).

The page is rendered without a database or a running app (the page guard
reads the JWT only), and its script is run in Node against a small stand-in
for the DOM that keeps every value it is given and refuses `innerHTML`.
Invented values only.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

import pytest
from starlette.requests import Request

from app.api import pages
from app.auth import tokens

TEMPLATES = Path(__file__).resolve().parents[2] / "app" / "templates"
PAGE = TEMPLATES / "admin" / "master_stations.html"
PAGE_PATH = "/admin/master/stations"

_SCRIPT_RE = re.compile(
    r"<script(?P<attrs>[^>]*)>(?P<body>.*?)</script\b[^>]*>", re.DOTALL | re.IGNORECASE
)

NOTICE_MSMM = "Stations from the station module, the reference. Corrections are made in the module."
NOTICE_FALLBACK = (
    "The station module did not answer; these results come from VIATOR's Trainline list, "
    "used as a fallback."
)


def page_request(role: str) -> Request:
    jwt = tokens.issue_jwt(uuid.uuid4(), f"zz-{role}@example.invalid", role)
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": PAGE_PATH,
            "query_string": b"",
            "headers": [(b"authorization", f"Bearer {jwt}".encode())],
        }
    )


def render(monkeypatch: pytest.MonkeyPatch, role: str, *, module: bool) -> str:
    monkeypatch.setitem(pages.templates.env.globals, "station_module_enabled", module)
    response = pages.admin_master_stations_page(page_request(role))
    assert response.status_code == 200
    return bytes(response.body).decode()


def page_script(html: str) -> str:
    """The page's own inline script: the last one (the base's come first)."""
    scripts = [
        match.group("body")
        for match in _SCRIPT_RE.finditer(html)
        if "src=" not in match.group("attrs") and match.group("body").strip()
    ]
    (script,) = [s for s in scripts if "/api/master/stations/search" in s]
    return script


def run_node_program(program: str) -> Any:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    result = subprocess.run(
        [node, "-e", program], capture_output=True, text=True, check=False, encoding="utf-8"
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


ROLES = ["content_manager", "platform_admin"]


# ───────────────────────────── what the page is ─────────────────────────────


@pytest.mark.parametrize("module", [False, True], ids=["module-off", "module-on"])
@pytest.mark.parametrize("role", ROLES)
def test_no_edit_no_drift_no_pagination(
    monkeypatch: pytest.MonkeyPatch, role: str, module: bool
) -> None:
    html = render(monkeypatch, role, module=module)
    content = html[html.index("<h1>Stations</h1>") :]
    lowered = content.lower()

    for absent in ("edit", "drift", "pagination", "page-jump", "x-total-count", "patch"):
        assert absent not in lowered, absent
    assert "prompt(" not in content
    for removed_call in ("'/api/master/stations?", "/drift", "/api/master/stations/${"):
        assert removed_call not in content
    assert 'id="country"' not in content


@pytest.mark.parametrize("role", ROLES)
def test_one_search_field_and_the_refresh_button_for_both_roles(
    monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    html = render(monkeypatch, role, module=True)
    content = html[html.index("<h1>Stations</h1>") :]

    assert content.count('type="search"') == 1
    assert 'minlength="3"' in html
    assert 'id="refresh-btn"' in html
    assert "Refresh from Trainline" in html
    script = page_script(html)
    assert "fetch('/api/master/stations/refresh-trainline', {method: 'POST'})" in script
    assert "fetch('/api/master/stations/search', {" in script
    assert "<title>Stations — VIATOR admin</title>" in html


def test_the_page_states_the_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    html = render(monkeypatch, "content_manager", module=True)
    text = re.sub(r"\s+", " ", html)

    assert "The station module (MSMM) is the reference for a station" in text
    assert "Corrections are made in the module, not here." in text
    assert "at most 10 stations" in text


def test_the_labels_and_the_two_notices_are_written_out(monkeypatch: pytest.MonkeyPatch) -> None:
    script = page_script(render(monkeypatch, "content_manager", module=True))

    assert f"msmm: '{NOTICE_MSMM}'" in script
    assert f'fallback: "{NOTICE_FALLBACK}"' in script
    assert "const LABELS = {msmm: 'MSMM', trainline: 'Trainline'};" in script


@pytest.mark.parametrize(
    ("role", "module", "links"),
    [
        pytest.param("platform_admin", True, "true", id="admin-module-on"),
        pytest.param("platform_admin", False, "false", id="admin-module-off"),
        pytest.param("content_manager", True, "false", id="manager-module-on"),
        pytest.param("content_manager", False, "false", id="manager-module-off"),
    ],
)
def test_the_module_link_switch(
    monkeypatch: pytest.MonkeyPatch, role: str, module: bool, links: str
) -> None:
    script = page_script(render(monkeypatch, role, module=module))

    assert f"const MODULE_LINKS = {links};" in script
    assert f"const MODULE_ENABLED = {'true' if module else 'false'};" in script


def test_no_value_ever_goes_in_as_html(monkeypatch: pytest.MonkeyPatch) -> None:
    script = page_script(render(monkeypatch, "platform_admin", module=True))

    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert sink not in script, sink


# ───────────────────────────── the script, run ─────────────────────────────

DOM_PRELUDE = r"""
class El {
  constructor(tag) {
    this.tagName = tag; this.children = []; this._text = ''; this.className = '';
    this.listeners = {}; this.value = ''; this.href = undefined; this.rows = [];
  }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(''); }
  set innerHTML(v) { throw new Error('innerHTML used'); }
  appendChild(c) { this.children.push(c); return c; }
  replaceChildren() { this.children = []; this._text = ''; }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  createTHead() { const h = new El('thead'); this.appendChild(h); return h; }
  createTBody() { const b = new El('tbody'); this.appendChild(b); return b; }
  insertRow() { const r = new El('tr'); this.appendChild(r); return r; }
  all(tag) {
    const out = [];
    for (const c of this.children) { if (c.tagName === tag) out.push(c); out.push(...c.all(tag)); }
    return out;
  }
}
const ids = {};
globalThis.document = {
  getElementById: (id) => (ids[id] ||= new El('#' + id)),
  createElement: (tag) => new El(tag),
};
let answer = null;
const posted = [];
globalThis.fetch = async (url, init) => {
  posted.push({url, body: init && init.body});
  return {ok: true, status: 200, json: async () => answer};
};
globalThis.confirm = () => true;
"""

DOM_SCENARIO = r"""
(async () => {
  const out = {};
  const submit = () => ids['search-form'].listeners.submit({preventDefault() {}});
  const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
  const view = () => {
    const rows = ids.results.all('tbody').flatMap((b) => b.all('tr'));
    return {
      status: ids.status.textContent,
      notice: ids.notice.textContent,
      noticeClass: ids.notice.className,
      cells: rows.map((r) => r.children.map((c) => c.textContent)),
      links: rows.map((r) => r.all('a').map((a) => a.href)),
      labels: rows.map((r) => r.all('span').map((s) => s.className)),
      heads: ids.results.all('th').map((th) => th.textContent),
    };
  };
  document.getElementById('q').value = '  Zz';
  submit(); await settle();
  out.short = {posted: posted.length, status: ids.status.textContent};

  answer = {origin: 'msmm', stations: [
    {name: '<img src=x onerror=alert(1)>Zz Halt', latitude: 45.5, longitude: 6.25,
     country_iso: 'ZZ', uic: '99 01&x=<b>'},
    {name: 'Zz Quay', latitude: 45.1, longitude: 6.1, country_iso: 'ZZ', uic: '9900002'},
  ]};
  document.getElementById('q').value = '  Zzville ';
  submit(); await settle(); await settle();
  out.msmm = view();
  out.body = posted[posted.length - 1].body;

  answer = {origin: 'trainline', stations: [
    {name: 'Zz Trainline', latitude: null, longitude: null, country_iso: null, uic: '9900003'},
  ]};
  submit(); await settle(); await settle();
  out.trainline = view();

  answer = {origin: 'msmm', stations: []};
  submit(); await settle(); await settle();
  out.empty = view();

  process.stdout.write(JSON.stringify(out));
})();
"""


def run_page(monkeypatch: pytest.MonkeyPatch, role: str, *, module: bool) -> Any:
    script = page_script(render(monkeypatch, role, module=module))
    return run_node_program(DOM_PRELUDE + script + DOM_SCENARIO)


def test_an_administrator_with_the_module_sees_labels_notices_and_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = run_page(monkeypatch, "platform_admin", module=True)

    assert out["short"] == {"posted": 0, "status": "Type at least 3 characters."}
    assert json.loads(out["body"]) == {"q": "Zzville"}

    msmm = out["msmm"]
    assert msmm["notice"] == NOTICE_MSMM
    assert msmm["heads"] == ["Name", "Country", "Code", "Position", "Origin", ""]
    # Markup from the module stays text.
    assert msmm["cells"][0][:5] == [
        "<img src=x onerror=alert(1)>Zz Halt",
        "ZZ",
        "99 01&x=<b>",
        "45.50000, 6.25000",
        "MSMM",
    ]
    assert msmm["labels"] == [["origin origin-msmm"], ["origin origin-msmm"]]
    # The code is URL-encoded in the link.
    assert msmm["links"] == [
        ["/msmm/admin/stations/reference?q=99%2001%26x%3D%3Cb%3E"],
        ["/msmm/admin/stations/reference?q=9900002"],
    ]
    assert msmm["status"] == "2 stations"

    trainline = out["trainline"]
    assert trainline["notice"] == NOTICE_FALLBACK
    assert "warning" in trainline["noticeClass"]
    assert trainline["cells"] == [["Zz Trainline", "—", "9900003", "—", "Trainline", ""]]
    assert trainline["links"] == [[]]  # no module link on a Trainline row

    assert out["empty"]["status"] == "No station found."
    assert out["empty"]["notice"] == NOTICE_MSMM
    assert out["empty"]["cells"] == []


@pytest.mark.parametrize("module", [False, True], ids=["module-off", "module-on"])
def test_a_content_manager_never_gets_a_module_link(
    monkeypatch: pytest.MonkeyPatch, module: bool
) -> None:
    out = run_page(monkeypatch, "content_manager", module=module)

    assert out["msmm"]["heads"] == ["Name", "Country", "Code", "Position", "Origin"]
    assert out["msmm"]["links"] == [[], []]
    assert out["msmm"]["cells"][0][-1] == "MSMM"


def test_without_the_module_trainline_results_carry_no_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = run_page(monkeypatch, "platform_admin", module=False)

    assert out["trainline"]["notice"] == ""
    assert out["trainline"]["noticeClass"] == "msg"
    assert out["trainline"]["cells"][0][4] == "Trainline"
    assert out["msmm"]["links"] == [[], []]


# ── `.flag` and `.hint` belong to the base template (moved from the list's tests) ──


def test_flag_and_hint_are_defined_in_the_base_template() -> None:
    base = (TEMPLATES / "_base.html").read_text(encoding="utf-8")
    styles = base[base.index("<style>") : base.index("{% block extra_styles %}")]
    assert re.search(r"^\s*\.flag \{ font-size: 0\.7rem;", styles, re.MULTILINE)
    for variant in ("ALL", "NAP-ONLY", "MERITS-ONLY", "SUBSET"):
        assert re.search(rf"^\s*\.flag\.{variant}\s*\{{", styles, re.MULTILINE), variant
    assert re.search(r"^\s*\.hint \{ color: var\(--rail-steel\); \}", styles, re.MULTILINE)


def test_the_stations_page_gets_the_styles_it_uses(monkeypatch: pytest.MonkeyPatch) -> None:
    html = render(monkeypatch, "content_manager", module=False)
    assert 'class="hint' in html
    assert ".hint { color: var(--rail-steel); }" in html
    assert ".msg.warning" in html
    assert ".origin-msmm { background: #e6f0fa; color: #174a78; }" in html


def test_the_journey_page_keeps_its_own_blue_and_amber() -> None:
    journey = (TEMPLATES / "journey.html").read_text(encoding="utf-8")
    assert not re.search(r"^\.flag \{", journey, re.MULTILINE)
    assert not re.search(r"^\.flag\.(ALL|NAP-ONLY)\b", journey, re.MULTILINE)
    assert ".flag.MERITS-ONLY{ background: #e8f0fb; color: #1C75BC; }" in journey
    assert ".flag.SUBSET     { background: #fff8e1; color: #b3760e; }" in journey


def test_the_moved_variants_render_as_they_did_on_the_journey_page() -> None:
    base = (TEMPLATES / "_base.html").read_text(encoding="utf-8")
    assert "--ok:         #1a7e3c;" in base
    assert "--fail:       #b30021;" in base
    assert ".flag.ALL         { background: #e9f6ec; color: var(--ok); }" in base
    assert ".flag.NAP-ONLY    { background: #fdecef; color: var(--fail); }" in base


def test_the_journey_page_opts_out_of_the_base_hint() -> None:
    journey = (TEMPLATES / "journey.html").read_text(encoding="utf-8")
    assert re.findall(r"^\.hint \{[^}]*\}", journey, re.MULTILINE) == [".hint { color: inherit; }"]
    assert ".hub-form-grid .hint { font-size: 0.72rem; color: var(--rail-steel);" in journey
    start = journey.index("{% block extra_styles %}")
    styles = journey[start : journey.index("{% endblock %}", start)]
    assert ".hint { color: inherit; }" in styles


def test_no_other_template_defines_an_unscoped_hint_or_flag() -> None:
    for path in sorted(TEMPLATES.rglob("*.html")):
        if path.name == "_base.html":
            continue
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"^\s*\.flag\s*\{", text, re.MULTILINE), path.name
        if path.name != "journey.html":
            assert not re.search(r"^\s*\.hint\s*\{", text, re.MULTILINE), path.name
