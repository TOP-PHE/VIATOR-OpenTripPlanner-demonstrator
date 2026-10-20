"""OTP serve memory (issue #384): the GC knob in the compose render, the cap
formula, and the heap / cap / measured use shown on the Sessions page.

- render_compose writes OTP_SERVE_GC_MODE and OTP_SERVE_SOFT_MAX_HEAP into
  every otp-<sid> service (an unknown mode or a malformed size is logged and
  replaced), and the caps of #374 do not depend on the mode;
- app/serve_memory.py asks Prometheus at most once a minute for the whole
  process (concurrent askers share the answer, a failure is cached too), in
  a POST body, never a query string;
- GET /api/sessions/serve-memory answers from that cache;
- the page's script asks once per load, whatever the number of rows, and
  writes text only. Run in Node with a minimal fake DOM (CI has Node).
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from app import serve_memory, sessions_orchestrator
from app.api.pages import _otp_serve_memory
from app.models.sessions import SessionState
from app.otp_heap import serve_mem_limit_for_heap
from app.sessions_orchestrator import render_compose
from app.settings import settings

TEMPLATE = Path(__file__).resolve().parents[2] / "app" / "templates" / "admin" / "sessions.html"


@dataclass
class _StubSession:
    id: str
    engine: str | None = "otp"
    category: str = "MERITS"
    state: str = SessionState.SERVING.value
    config: dict[str, Any] = field(default_factory=dict)


def _otp_env(sessions: list[_StubSession]) -> dict[str, dict[str, str]]:
    services: dict[str, Any] = yaml.safe_load(render_compose(sessions))["services"]  # type: ignore[arg-type]
    return {name: svc["environment"] for name, svc in services.items() if name.startswith("otp-")}


# ── compose render ──────────────────────────────────────────────────────────


def test_render_passes_the_default_gc_mode_to_every_otp_service() -> None:
    env = _otp_env([_StubSession("zz-one"), _StubSession("zz-two", config={"otp_heap": "12g"})])
    for name in ("otp-zz-one", "otp-zz-two"):
        assert env[name]["OTP_SERVE_GC_MODE"] == "g1-uncommit"
        assert env[name]["OTP_SERVE_SOFT_MAX_HEAP"] == ""


@pytest.mark.parametrize("mode", ["g1-uncommit", "zgc", "legacy", " ZGC "])
def test_render_passes_the_configured_gc_mode(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setattr(settings, "otp_serve_gc_mode", mode)
    env = _otp_env([_StubSession("zz-one")])
    assert env["otp-zz-one"]["OTP_SERVE_GC_MODE"] == mode.strip().lower()


def test_render_replaces_an_unknown_gc_mode_and_logs_it_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(settings, "otp_serve_gc_mode", 'zz"bad')
    with caplog.at_level(logging.WARNING, logger="app.sessions_orchestrator"):
        env = _otp_env([_StubSession("zz-one"), _StubSession("zz-two")])
    assert {e["OTP_SERVE_GC_MODE"] for e in env.values()} == {"g1-uncommit"}
    assert caplog.text.count("is unknown") == 1


def test_render_passes_a_well_formed_soft_max_heap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "otp_serve_soft_max_heap", "24g")
    assert _otp_env([_StubSession("zz-one")])["otp-zz-one"]["OTP_SERVE_SOFT_MAX_HEAP"] == "24g"


@pytest.mark.parametrize("value", ["24 gb", "lots", '9g"\n      X: y'])
def test_render_drops_a_malformed_soft_max_heap(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, value: str
) -> None:
    monkeypatch.setattr(settings, "otp_serve_soft_max_heap", value)
    with caplog.at_level(logging.WARNING, logger="app.sessions_orchestrator"):
        env = _otp_env([_StubSession("zz-one")])
    assert env["otp-zz-one"]["OTP_SERVE_SOFT_MAX_HEAP"] == ""
    assert "OTP_SERVE_SOFT_MAX_HEAP" in caplog.text


def test_motis_services_get_no_gc_knob(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "otp_serve_gc_mode", "zgc")
    services = yaml.safe_load(render_compose([_StubSession("zz-motis", engine="motis")]))[
        "services"
    ]
    assert "OTP_SERVE_GC_MODE" not in yaml.safe_dump(services)


# ── the cap formula of #374 still holds, whatever the mode ──────────────────


@pytest.mark.parametrize(
    ("heap", "cap"), [("4g", "10g"), ("12g", "18g"), ("24g", "32g"), ("40g", "53g")]
)
def test_serve_cap_formula_is_unchanged(heap: str, cap: str) -> None:
    assert serve_mem_limit_for_heap(heap) == cap


@pytest.mark.parametrize("mode", ["g1-uncommit", "zgc", "legacy"])
def test_rendered_cap_does_not_depend_on_the_gc_mode(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.setattr(settings, "otp_serve_gc_mode", mode)
    services = yaml.safe_load(render_compose([_StubSession("zz-one", config={"otp_heap": "40g"})]))
    svc = services["services"]["otp-zz-one"]
    assert svc["mem_limit"] == svc["memswap_limit"] == "53g"


# ── heap and cap for the page ───────────────────────────────────────────────


def test_page_lists_heap_and_cap_of_serving_otp_sessions_only() -> None:
    rows = [
        _StubSession("zz-big", config={"otp_heap": "40g"}),
        _StubSession("zz-auto", config={"otp_build_heap": "24g"}),
        _StubSession("zz-motis", engine="motis"),
        _StubSession("zz-built", state=SessionState.GRAPH_BUILT.value),
        _StubSession("zz-legacy-row", engine=None),
    ]
    assert _otp_serve_memory(rows) == {  # type: ignore[arg-type]
        "zz-big": {"heap": "40g", "cap": "53g"},
        "zz-auto": {"heap": "8g", "cap": "14g"},
        "zz-legacy-row": {"heap": "4g", "cap": "10g"},
    }


def test_page_heap_matches_the_rendered_heap() -> None:
    row = _StubSession("zz-auto", config={"otp_build_heap": "36g"})
    env = _otp_env([row])["otp-zz-auto"]
    assert _otp_serve_memory([row])["zz-auto"]["heap"] == env["OTP_HEAP"]  # type: ignore[list-item]


def test_page_shows_no_cap_when_caps_are_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "otp_serve_mem_cap", False)
    assert _otp_serve_memory([_StubSession("zz-one")]) == {"zz-one": {"heap": "4g", "cap": None}}  # type: ignore[list-item]
    assert sessions_orchestrator.otp_serve_mem_limit("4g") is None


# ── the Prometheus fetch ────────────────────────────────────────────────────


def _vector(values: dict[str, float]) -> dict[str, Any]:
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {
                    "metric": {"container_label_com_docker_compose_service": svc},
                    "value": [1.0, str(v)],
                }
                for svc, v in values.items()
            ],
        },
    }


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    serve_memory.reset_cache()
    yield
    serve_memory.reset_cache()


async def test_fetch_posts_both_queries_in_the_body() -> None:
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        query = parse_qs(request.content.decode())["query"][0]
        if query == serve_memory.USED_QUERY:
            return httpx.Response(
                200, json=_vector({"otp-zz-one": 2.5 * 1024**3, "otp-zz-two": 99})
            )
        return httpx.Response(200, json=_vector({"otp-zz-one": 90000.0}))

    measured = await serve_memory._fetch(httpx.MockTransport(answer))

    assert measured == {
        "zz-one": {"used_bytes": 2.5 * 1024**3, "uptime_seconds": 90000.0},
        "zz-two": {"used_bytes": 99.0},
    }
    assert [r.method for r in seen] == ["POST", "POST"]
    assert all(r.url.query == b"" for r in seen)
    assert str(seen[0].url) == f"{settings.prometheus_url}/api/v1/query"


def test_queries_select_the_serve_containers_not_the_build() -> None:
    for query in (serve_memory.USED_QUERY, serve_memory.UPTIME_QUERY):
        assert 'container_label_com_docker_compose_service=~"otp-.+"' in query
        assert 'container_label_com_docker_compose_service!="otp-build"' in query
    assert "container_memory_working_set_bytes" in serve_memory.USED_QUERY
    assert "container_start_time_seconds" in serve_memory.UPTIME_QUERY


@pytest.mark.parametrize(
    "body",
    [{"status": "error"}, [], {"status": "success", "data": {}}],
    ids=["error", "not-an-object", "no-result"],
)
def test_a_malformed_answer_is_refused(body: Any) -> None:
    with pytest.raises((ValueError, KeyError)):
        serve_memory._vector(body)


def _counting_fetch(monkeypatch: pytest.MonkeyPatch, result: Any) -> list[int]:
    calls: list[int] = []

    async def fake_fetch(transport: Any = None) -> Any:
        calls.append(1)
        await asyncio.sleep(0)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(serve_memory, "_fetch", fake_fetch)
    return calls


async def test_answer_is_cached_for_a_minute(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _counting_fetch(monkeypatch, {"zz-one": {"used_bytes": 1.0}})
    clock = [1000.0]
    monkeypatch.setattr(serve_memory.time, "monotonic", lambda: clock[0])

    first = await serve_memory.measured_by_session()
    clock[0] += serve_memory.CACHE_SECONDS - 1
    second = await serve_memory.measured_by_session()
    assert first == second == {"zz-one": {"used_bytes": 1.0}}
    assert len(calls) == 1

    clock[0] += 2
    await serve_memory.measured_by_session()
    assert len(calls) == 2


async def test_concurrent_askers_share_one_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _counting_fetch(monkeypatch, {"zz-one": {"used_bytes": 1.0}})
    answers = await asyncio.gather(*(serve_memory.measured_by_session() for _ in range(8)))
    assert len(calls) == 1
    assert all(a == {"zz-one": {"used_bytes": 1.0}} for a in answers)


async def test_a_failure_is_cached_and_logged_without_the_url(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls = _counting_fetch(monkeypatch, httpx.ConnectError("zz unreachable"))
    with caplog.at_level(logging.WARNING, logger="app.serve_memory"):
        assert await serve_memory.measured_by_session() is None
        assert await serve_memory.measured_by_session() is None
    assert len(calls) == 1
    assert caplog.text.count("Prometheus not available (ConnectError)") == 1
    assert "zz unreachable" not in caplog.text


async def test_no_prometheus_url_means_no_request(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _counting_fetch(monkeypatch, {})
    monkeypatch.setattr(settings, "prometheus_url", "")
    assert await serve_memory.measured_by_session() is None
    assert calls == []


# ── the endpoint ────────────────────────────────────────────────────────────


@pytest.fixture
def admin_client() -> Iterator[TestClient]:
    from app.main import app
    from app.security import CurrentUser, require_platform_admin

    app.dependency_overrides[require_platform_admin] = lambda: CurrentUser(
        id=None, username="zz-admin", role="platform_admin"
    )
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(require_platform_admin, None)


def test_endpoint_returns_the_measure(
    admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def measured() -> dict[str, dict[str, float]]:
        return {
            "zz-one": {"used_bytes": 99.0, "uptime_seconds": 9.0},
            "zz-two": {"used_bytes": 1.0},
        }

    monkeypatch.setattr(serve_memory, "measured_by_session", measured)
    answer = admin_client.get("/api/sessions/serve-memory")
    assert answer.status_code == 200
    assert answer.json() == {
        "available": True,
        "sessions": {
            "zz-one": {"used_bytes": 99.0, "uptime_seconds": 9.0},
            "zz-two": {"used_bytes": 1.0, "uptime_seconds": None},
        },
    }


def test_endpoint_says_when_the_measure_is_not_available(
    admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def measured() -> None:
        return None

    monkeypatch.setattr(serve_memory, "measured_by_session", measured)
    assert admin_client.get("/api/sessions/serve-memory").json() == {
        "available": False,
        "sessions": {},
    }


def test_endpoint_needs_a_platform_admin() -> None:
    from app.main import app

    assert TestClient(app).get("/api/sessions/serve-memory").status_code in (401, 403)


# ── the page's script ───────────────────────────────────────────────────────

_START = "// ── Serve memory (issue #384)"
_END = "// ── end serve memory"

_SCRIPT = """
class El {
  constructor(tag) { this.tag = tag; this.children = []; this.dataset = {}; this._text = '';
                     this.className = ''; this.title = ''; }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
  append(...c) { this.children.push(...c); }
  querySelector(sel) { return sel.includes('serve-memory-use') ? this.use : null; }
}
const rows = %(rows)s.map(([sid, heap]) => {
  const el = new El('div'); el.dataset = {sid, heap}; el.use = new El('span'); return el;
});
const document = {
  createElement: tag => new El(tag),
  querySelectorAll: sel => (sel.includes('"serve-memory"') ? rows : []),
  addEventListener: (name, fn, opts) => { if (name === 'DOMContentLoaded' && opts.once) loaded.push(fn); },
};
const loaded = [];
const console = {warn: () => {}, log: (...a) => process.stdout.write(a.join(' ') + '\\n')};
const calls = [];
async function fetch(url) {
  calls.push(url);
  const a = %(answer)s;
  if (a === 'network') throw new Error('offline');
  return {ok: a.status === 200, json: async () => a.body};
}
%(block)s
loaded.forEach(fn => fn());
setTimeout(() => console.log(JSON.stringify({
  calls,
  rows: rows.map(r => ({text: r.use.textContent,
                        badges: r.use.children.map(c => [c.className, c.title])})),
})), 0);
"""

_GIB = 1024**3


def _page(rows: list[list[str]], answer: Any) -> dict[str, Any]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    text = TEMPLATE.read_text(encoding="utf-8")
    block = text[text.index(_START) : text.index(_END)]
    script = _SCRIPT % {"rows": json.dumps(rows), "answer": json.dumps(answer), "block": block}
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True)
    result: dict[str, Any] = json.loads(out.stdout)
    return result


_ROWS = [["zz-big", "40g"], ["zz-fit", "8g"], ["zz-new", "40g"], ["zz-gone", "8g"]]


def test_page_asks_once_for_all_rows_and_writes_the_use() -> None:
    body = {
        "available": True,
        "sessions": {
            "zz-big": {"used_bytes": 2.5 * _GIB, "uptime_seconds": 90000},
            "zz-fit": {"used_bytes": 5 * _GIB, "uptime_seconds": 90000},
            "zz-new": {"used_bytes": 2.5 * _GIB, "uptime_seconds": 600},
        },
    }
    result = _page(_ROWS, {"status": 200, "body": body})

    assert result["calls"] == ["/api/sessions/serve-memory"]
    big, fit, new, gone = result["rows"]
    assert big["text"] == " · uses 2.5 GiBheap > 4\u00d7 use"
    assert big["badges"][0][0] == "serve-memory-oversized"
    assert "about twice the use" in big["badges"][0][1]
    assert fit == {"text": " · uses 5.0 GiB", "badges": []}  # 8g is not more than 4 x 5 GiB
    assert new == {"text": " · uses 2.5 GiB", "badges": []}  # not a day of serving yet
    assert gone == {"text": " · use not measured", "badges": []}


def test_badge_needs_more_than_four_times_the_use() -> None:
    day = {"uptime_seconds": 86400}
    body = {
        "available": True,
        "sessions": {
            "zz-fit": {"used_bytes": 2 * _GIB, **day},
            "zz-mb": {"used_bytes": 3 * _GIB, **day},  # 8192m is 8 GiB, not 8192
            "zz-mb-big": {"used_bytes": 1.5 * _GIB, **day},
        },
    }
    rows = [["zz-fit", "8g"], ["zz-mb", "8192m"], ["zz-mb-big", "8192m"]]
    result = _page(rows, {"status": 200, "body": body})
    assert [len(r["badges"]) for r in result["rows"]] == [0, 0, 1]


@pytest.mark.parametrize(
    "answer",
    [
        {"status": 200, "body": {"available": False, "sessions": {}}},
        {"status": 500, "body": {}},
        "network",
    ],
    ids=["unavailable", "server-error", "network-error"],
)
def test_page_keeps_heap_and_cap_only_without_a_measure(answer: Any) -> None:
    result = _page(_ROWS, answer)
    assert result["calls"] == ["/api/sessions/serve-memory"]
    assert all(r == {"text": "", "badges": []} for r in result["rows"])


def test_page_without_otp_serve_rows_asks_nothing() -> None:
    assert _page([], {"status": 200, "body": {}})["calls"] == []


def test_the_script_never_uses_inner_html() -> None:
    text = TEMPLATE.read_text(encoding="utf-8")
    block = text[text.index(_START) : text.index(_END)]
    assert "innerHTML" not in block
    assert block.count("fetch(") == 1
