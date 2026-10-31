"""The country gate after a save (`app/country_gate.py`, the worker, the Sessions page).

A save accepts a new country when VIATOR uses the station module
(tests/unit/test_session_country_gate.py). The graph build is never stopped
by it (review round 1 of #369: build and warn): after the stop extracts were
sent and the module's station build waited for, a declared country neither
VIATOR's Trainline list nor the module knows gets a warning line in the
job's log, and the job ends `done`; the same, "not checked", when the module
does not answer. The Sessions page shows a lasting notice, computed at each
load. The module answers through `httpx.MockTransport`; the Trainline rows
and the database stand in. Invented values only: countries ZZ, YY, XX,
session `zz`.
"""

from __future__ import annotations

import json
import logging
import secrets
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app import country_gate, station_module, worker
from app import station_builds as sb
from app.settings import settings

MODULE_URL = "http://msmm.invalid:8000"
ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "app" / "templates" / "admin" / "sessions.html"
PAGES = ROOT / "app" / "api" / "pages.py"


class Module:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.response = httpx.Response(200, json={"countries": ["YY"]})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.response


@pytest.fixture
def module(monkeypatch: pytest.MonkeyPatch) -> Iterator[Module]:
    stand_in = Module()
    transport = httpx.MockTransport(stand_in)
    real_async = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async(*args, **kwargs)

    monkeypatch.setattr(station_module.httpx, "AsyncClient", factory)
    monkeypatch.setattr(settings, "station_module_url", MODULE_URL)
    monkeypatch.setattr(settings, "station_module_token", SecretStr(secrets.token_hex(32)))
    station_module.reset()
    yield stand_in
    station_module.reset()


@pytest.fixture(autouse=True)
def trainline(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    """The countries with Trainline rows (stands in for the GROUP BY)."""
    present = {"ZZ"}
    monkeypatch.setattr(
        country_gate, "countries_without_stations", lambda _db, declared: set(declared) - present
    )
    return present


def _config(*countries: str | None) -> dict[str, Any]:
    return {
        "sources": {
            "providers": [
                {"id": f"ZZ{n}", "country_iso": country} for n, country in enumerate(countries)
            ]
        }
    }


# ── unserved, build_warning ───────────────────────────────────────────────


def _unserved(*countries: str | None) -> country_gate.Unserved:
    return country_gate.unserved(None, _config(*countries))


def test_declared_countries_reads_the_saved_providers() -> None:
    assert country_gate.declared_countries(_config("zz", "YY", None, "")) == {"ZZ", "YY"}
    assert country_gate.declared_countries({"sources": {"providers": "zz"}}) == set()
    assert country_gate.declared_countries(None) == set()


def test_countries_with_trainline_rows_ask_nothing(module: Module) -> None:
    assert _unserved("ZZ") == country_gate.Unserved()
    assert module.requests == []


def test_a_country_the_module_serves_is_served(module: Module) -> None:
    assert _unserved("ZZ", "YY") == country_gate.Unserved()
    assert [r.url.path for r in module.requests] == ["/internal/v1/countries"]


def test_a_country_neither_knows_is_unserved(module: Module) -> None:
    assert _unserved("ZZ", "YY", "XX", "WW") == country_gate.Unserved(("WW", "XX"))


def test_the_module_is_asked_afresh_after_its_build(module: Module) -> None:
    """The countries cache (60 s) may hold the answer from before the
    module's build that just ended: the build's check asks again."""
    module.response = httpx.Response(200, json={"countries": []})
    assert _unserved("XX").countries == ("XX",)
    module.response = httpx.Response(200, json={"countries": ["XX"]})
    assert _unserved("XX").countries == ()
    assert len(module.requests) == 2


def test_the_module_not_answering_leaves_them_not_checked(module: Module) -> None:
    module.response = httpx.Response(500, json={"error_id": "zz"})
    assert _unserved("ZZ", "XX") == country_gate.Unserved(("XX",), checked=False)


def test_without_the_module_the_trainline_list_decides(
    module: Module, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "station_module_url", "")
    assert _unserved("ZZ", "YY") == country_gate.Unserved(("YY",))
    assert module.requests == []


def test_the_warning_says_what_to_do(module: Module, monkeypatch: pytest.MonkeyPatch) -> None:
    assert country_gate.build_warning(country_gate.Unserved()) == ""
    line = country_gate.build_warning(country_gate.Unserved(("XX", "YY")))
    assert line == (
        "[viator] warning: missing_master_stations_for_countries ['XX', 'YY']: routes by "
        "coordinates until the station module serves it; accept the feed in the module and "
        "let its station build run.\n"
    )
    unchecked = country_gate.build_warning(country_gate.Unserved(("XX",), checked=False))
    assert unchecked.startswith(
        "[viator] warning: missing_master_stations_for_countries ['XX'] not checked: "
    )
    assert "the station module did not answer; the graph is built" in unchecked
    monkeypatch.setattr(settings, "station_module_url", "")
    unused = country_gate.build_warning(country_gate.Unserved(("XX",)))
    assert "VIATOR does not use the station module; the graph is built" in unused
    assert "Refresh from Trainline" in unused


def test_the_new_country_warning() -> None:
    warning = country_gate.new_country_warning({"XX", "YY"})
    assert warning.startswith("New country ['XX', 'YY']: ")
    assert "once the feed is accepted there" in warning
    assert warning.endswith("the graph is built and these countries route by coordinates.")
    assert "not built" not in warning


# ── the Sessions page's notice ────────────────────────────────────────────


def _sessions() -> list[tuple[str, Any]]:
    return [("zz-a", _config("ZZ")), ("zz-b", _config("ZZ", "XX", "YY")), ("zz-c", None)]


def test_each_session_with_an_unserved_country_gets_a_notice(module: Module) -> None:
    notices = country_gate.page_notices(None, _sessions())
    assert notices == {
        "zz-b": "New country XX: not yet served by the station module. It routes by "
        "coordinates until its feed is accepted in the module and the module's station "
        "build has run."
    }
    assert len(module.requests) == 1  # one question for every session


def test_the_notice_without_an_answer_or_without_the_module(
    module: Module, monkeypatch: pytest.MonkeyPatch
) -> None:
    module.response = httpx.Response(500, json={"error_id": "zz"})
    unchecked = country_gate.page_notices(None, _sessions())["zz-b"]
    assert unchecked.startswith("Country XX, YY: no Trainline stations; the station module")
    monkeypatch.setattr(settings, "station_module_url", "")
    unused = country_gate.page_notices(None, _sessions())["zz-b"]
    assert "VIATOR does not use the station module" in unused


def test_no_notice_asks_nothing(module: Module) -> None:
    assert country_gate.page_notices(None, [("zz-a", _config("ZZ"))]) == {}
    assert country_gate.page_notices(None, []) == {}
    assert module.requests == []


def test_a_failing_notice_never_breaks_the_page(
    module: Module, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(_db: Any, _declared: set[str]) -> set[str]:
        raise RuntimeError("stand-in")

    monkeypatch.setattr(country_gate, "countries_without_stations", broken)
    with caplog.at_level(logging.WARNING, logger="app.country_gate"):
        assert country_gate.page_notices(None, _sessions()) == {}
    assert "country_gate.notices_failed reason=RuntimeError" in caplog.text


def test_the_page_shows_the_notice_and_keeps_the_save_warning() -> None:
    text = TEMPLATE.read_text(encoding="utf-8")
    assert 'data-role="country-notice"' in text
    assert "country_notices.get(s.id" in text
    assert "showCountryNotice(sid, warnings);" in text
    pages = PAGES.read_text(encoding="utf-8")
    assert (
        '"country_notices": country_gate.page_notices(db, [(s.id, s.config) for s in sessions])'
        in pages
    )


def test_the_save_warning_stays_in_the_notice() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    text = TEMPLATE.read_text(encoding="utf-8")
    start = text.index("function showCountryNotice(sid, warnings)")
    function = text[start : text.index("\n}\n", start) + 3]
    script = (
        "const box = {hidden: true, textContent: ''};"
        "const document = {querySelector: sel => (sel.includes('zz') ? box : null)};"
        "const CSS = {escape: s => s};" + function + "showCountryNotice('zz', ['OSM URL zz']);"
        "const before = {...box};"
        "showCountryNotice('zz', ['OSM URL zz', 'New country [\\'XX\\']: zz']);"
        "console.log(JSON.stringify([before, box]));"
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True)
    before, after = json.loads(out.stdout)
    assert before == {"hidden": True, "textContent": ""}
    assert after == {"hidden": False, "textContent": "New country ['XX']: zz"}


# ── the worker: build and warn ────────────────────────────────────────────


class _Db:
    def __init__(self, config: Any) -> None:
        self.session = None if config is None else SimpleNamespace(config=config)

    def __enter__(self) -> _Db:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def get(self, *_args: Any) -> Any:
        return self.session


def _engine_run(
    monkeypatch: pytest.MonkeyPatch, engine: str, config: Any
) -> tuple[tuple[str, bool, str], list[str], list[str]]:
    events: list[str] = []
    notes: list[str] = []

    def wait(**_kw: Any) -> sb.WaitResult:
        events.append("wait")
        return sb.WaitResult(sb.DONE, None, 9901)

    def built(name: str) -> Any:
        def build(**_kw: Any) -> tuple[str, bool, str]:
            events.append(name)
            return "built", True, "/data/graphs/zz/current"

        return build

    def sent(_job_id: uuid.UUID, _sid: str) -> Any:
        events.append("send")
        return SimpleNamespace(cancelled=False)

    def note(_job_id: uuid.UUID, line: str) -> None:
        events.append("note")
        notes.append(line)

    monkeypatch.setattr(worker, "_send_station_extracts", sent)
    monkeypatch.setattr(worker.station_builds, "wait_before_graph", wait)
    monkeypatch.setattr(worker, "_note_station_build", note)
    monkeypatch.setattr(worker, "run_build_motis", built("motis"))
    monkeypatch.setattr(worker, "run_build", built("otp"))
    monkeypatch.setattr(worker, "SessionLocal", lambda: _Db(config))
    out = worker._run_engine_build(
        engine, job_id=uuid.uuid4(), session_id="zz", max_memory=False, container="c"
    )
    return out, events, [n for n in notes if "warning" in n]


@pytest.mark.usefixtures("module")
@pytest.mark.parametrize("engine", ["motis", "otp"])
def test_a_country_neither_knows_is_built_with_a_warning(
    monkeypatch: pytest.MonkeyPatch, engine: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="app.worker"):
        out, events, warnings = _engine_run(monkeypatch, engine, _config("ZZ", "XX"))
    expected = (
        ["send", "wait", "note", "note", engine] if engine == "motis" else ["send", "note", engine]
    )
    assert events == expected
    assert out == ("built", True, "/data/graphs/zz/current")
    assert warnings == [country_gate.build_warning(country_gate.Unserved(("XX",)))]
    assert "rebuild.country_unserved session=zz countries=XX checked=true" in caplog.text


def test_a_module_that_does_not_answer_builds_with_a_not_checked_warning(
    monkeypatch: pytest.MonkeyPatch, module: Module, caplog: pytest.LogCaptureFixture
) -> None:
    module.response = httpx.Response(500, json={"error_id": "zz"})
    with caplog.at_level(logging.WARNING, logger="app.worker"):
        out, events, warnings = _engine_run(monkeypatch, "otp", _config("XX"))
    assert out[1] is True
    assert events == ["send", "note", "otp"]
    assert warnings == [country_gate.build_warning(country_gate.Unserved(("XX",), False))]
    assert "checked=false" in caplog.text


@pytest.mark.usefixtures("module")
@pytest.mark.parametrize("engine", ["motis", "otp"])
def test_known_countries_build_without_a_warning(
    monkeypatch: pytest.MonkeyPatch, engine: str
) -> None:
    out, events, warnings = _engine_run(monkeypatch, engine, _config("ZZ", "YY"))
    assert events[-1] == engine
    assert warnings == []
    assert out == ("built", True, "/data/graphs/zz/current")


@pytest.mark.usefixtures("module")
def test_a_session_gone_or_without_providers_builds(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _engine_run(monkeypatch, "otp", None)[1] == ["send", "otp"]
    assert _engine_run(monkeypatch, "otp", {})[1] == ["send", "otp"]


@pytest.mark.usefixtures("module")
def test_a_failing_check_never_stops_the_build(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(_db: Any, _config: Any) -> Any:
        raise RuntimeError("stand-in")

    monkeypatch.setattr(country_gate, "unserved", broken)
    with caplog.at_level(logging.WARNING, logger="app.worker"):
        out, events, _ = _engine_run(monkeypatch, "otp", _config("XX"))
    assert events == ["send", "otp"]
    assert out[1] is True
    assert "country gate not checked for zz (RuntimeError)" in caplog.text


def test_a_job_without_a_session_is_not_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    def never() -> Any:
        raise AssertionError("the database was opened")

    monkeypatch.setattr(worker, "SessionLocal", never)
    worker._note_country_warning(uuid.uuid4(), None)


class _TickDb:
    """One fake database for every session of tick(): the pending job, its
    OTP session declaring XX, and the lines the worker appends to its log."""

    def __init__(self, job: Any) -> None:
        self.job = job
        self.session = SimpleNamespace(engine="otp", state="graph_built", config=_config("XX"))

    def __enter__(self) -> _TickDb:
        return self

    def __exit__(self, *_a: object) -> None:
        return None

    def query(self, _model: object) -> Any:
        job = self.job
        chain = SimpleNamespace()
        chain.filter = lambda *_a: chain
        chain.order_by = lambda *_a: chain
        chain.first = lambda: job
        return chain

    def execute(self, statement: Any) -> Any:
        sql = str(statement)
        # The note of jobs held back by a running refresh applies only while a
        # refresh runs (its WHERE reads source_refresh_progress): none here.
        if sql.startswith("UPDATE rebuild_jobs SET log=") and "source_refresh_progress" not in sql:
            params = statement.compile().params.values()
            for value in params:
                if isinstance(value, str) and value.startswith("[viator]"):
                    self.job.log = (self.job.log or "") + value
        return SimpleNamespace(scalar_one_or_none=lambda: self.job.id)

    def get(self, model: object, _key: object) -> Any:
        return self.job if model is worker.RebuildJob else self.session

    def commit(self) -> None:
        return None


@pytest.mark.usefixtures("module")
def test_the_job_ends_done_with_the_warning_in_its_log(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review round 1 of #369: the whole job, through tick(): `done`, its
    graph built, the warning in its log."""
    job = SimpleNamespace(
        id=uuid.uuid4(),
        session_id="zz",
        status="pending",
        log="queued\n",
        created_at=datetime(2026, 1, 12, 9, 0, tzinfo=UTC),
        started_at=None,
        finished_at=None,
        graph_path=None,
        max_memory=False,
        cancel_requested_at=None,
    )
    db = _TickDb(job)
    monkeypatch.setattr(worker, "SessionLocal", lambda: db)
    monkeypatch.setattr(worker, "_debounce_seconds", lambda: 0)
    monkeypatch.setattr(worker, "_CANCEL_POLL_SECONDS", 0.001)
    monkeypatch.setattr(worker, "_cancel_requested", lambda _jid: False)
    monkeypatch.setattr(
        worker, "_send_station_extracts", lambda *_a: SimpleNamespace(cancelled=False)
    )
    monkeypatch.setattr(
        worker, "run_build", lambda **_kw: ("otp build ok\n", True, "/data/graphs/zz/current")
    )
    monkeypatch.setattr(worker, "_kill_container", lambda _name: None)
    monkeypatch.setattr(worker.graph_snapshots, "enumerate_session_inputs", lambda *_a: [])
    monkeypatch.setattr(worker.graph_snapshots, "record_snapshot", lambda *_a, **_kw: None)
    worker.tick()
    assert job.status == "done"
    assert job.graph_path == "/data/graphs/zz/current"
    warning = country_gate.build_warning(country_gate.Unserved(("XX",)))
    assert job.log == "queued\n" + warning + "otp build ok\n"
