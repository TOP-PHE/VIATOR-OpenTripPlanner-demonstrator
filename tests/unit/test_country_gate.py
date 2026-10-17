"""The country gate of a session's graph build (`app/country_gate.py`, the worker).

A save now accepts a new country when VIATOR uses the station module
(tests/unit/test_session_country_gate.py); the gate that holds is the graph
build's: after the stop extracts were sent and the module's station build
waited for, a declared country neither VIATOR's Trainline list nor the module
knows stops the build, with the coded word and what to do. The module used
but not answering never stops a build. The module answers through
`httpx.MockTransport`; the Trainline rows and the database stand in.
Invented values only: countries ZZ, YY, XX, session `zz`.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app import country_gate, station_module, worker
from app import station_builds as sb
from app.settings import settings

MODULE_URL = "http://msmm.invalid:8000"


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


# ── build_blocked ─────────────────────────────────────────────────────────


def test_declared_countries_reads_the_saved_providers() -> None:
    assert country_gate.declared_countries(_config("zz", "YY", None, "")) == {"ZZ", "YY"}
    assert country_gate.declared_countries({"sources": {"providers": "zz"}}) == set()
    assert country_gate.declared_countries(None) == set()


def test_countries_with_trainline_rows_ask_nothing(module: Module) -> None:
    assert country_gate.build_blocked(None, _config("ZZ")) == set()
    assert module.requests == []


def test_a_country_the_module_serves_builds(module: Module) -> None:
    assert country_gate.build_blocked(None, _config("ZZ", "YY")) == set()
    assert [r.url.path for r in module.requests] == ["/internal/v1/countries"]


def test_a_country_neither_knows_blocks_the_build(module: Module) -> None:
    assert country_gate.build_blocked(None, _config("ZZ", "YY", "XX")) == {"XX"}


def test_the_module_is_asked_afresh_after_its_build(module: Module) -> None:
    """The countries cache (60 s) may hold the answer from before the
    module's build that just ended: the build gate asks again."""
    module.response = httpx.Response(200, json={"countries": []})
    assert country_gate.build_blocked(None, _config("XX")) == {"XX"}
    module.response = httpx.Response(200, json={"countries": ["XX"]})
    assert country_gate.build_blocked(None, _config("XX")) == set()
    assert len(module.requests) == 2


def test_the_module_not_answering_never_blocks_a_build(
    module: Module, caplog: pytest.LogCaptureFixture
) -> None:
    module.response = httpx.Response(500, json={"error_id": "zz"})
    with caplog.at_level(logging.WARNING, logger="app.country_gate"):
        assert country_gate.build_blocked(None, _config("XX")) == set()
    assert "country_gate.not_checked reason=module_unanswered" in caplog.text


def test_without_the_module_the_trainline_list_decides(
    module: Module, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "station_module_url", "")
    assert country_gate.build_blocked(None, _config("ZZ", "YY")) == {"YY"}
    assert module.requests == []


def test_the_refusal_says_what_to_do(module: Module, monkeypatch: pytest.MonkeyPatch) -> None:
    line = country_gate.build_refusal({"YY", "XX"})
    assert line.startswith(
        "[viator] graph not built: missing_master_stations_for_countries ['XX', 'YY']: "
    )
    assert "the station module does not serve them" in line
    assert "Accept the feed of each in the station module" in line
    assert "Refresh from Trainline" in line
    assert line.endswith("then Rebuild graph.\n")
    monkeypatch.setattr(settings, "station_module_url", "")
    assert "VIATOR does not use the station module" in country_gate.build_refusal({"XX"})


def test_the_new_country_warning() -> None:
    warning = country_gate.new_country_warning({"XX", "YY"})
    assert warning.startswith("New country ['XX', 'YY']: ")
    assert "once the feed is accepted there" in warning


# ── the worker ────────────────────────────────────────────────────────────


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
) -> tuple[tuple[str, bool, str], list[str]]:
    events: list[str] = []

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

    monkeypatch.setattr(worker, "_send_station_extracts", sent)
    monkeypatch.setattr(worker.station_builds, "wait_before_graph", wait)
    monkeypatch.setattr(worker, "_note_station_build", lambda _jid, _line: None)
    monkeypatch.setattr(worker, "run_build_motis", built("motis"))
    monkeypatch.setattr(worker, "run_build", built("otp"))
    monkeypatch.setattr(worker, "SessionLocal", lambda: _Db(config))
    out = worker._run_engine_build(
        engine, job_id=uuid.uuid4(), session_id="zz", max_memory=False, container="c"
    )
    return out, events


@pytest.mark.usefixtures("module")
@pytest.mark.parametrize("engine", ["motis", "otp"])
def test_a_country_neither_knows_ends_the_job_before_any_build(
    monkeypatch: pytest.MonkeyPatch, engine: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="app.worker"):
        out, events = _engine_run(monkeypatch, engine, _config("ZZ", "XX"))
    assert events == (["send", "wait"] if engine == "motis" else ["send"])
    assert out == (country_gate.build_refusal({"XX"}), False, "")
    assert worker._final_status(success=False, cancel_requested=False) == "failed"
    assert (
        "rebuild refused session=zz reason=missing_master_stations_for_countries countries=XX"
        in caplog.text
    )


@pytest.mark.usefixtures("module")
@pytest.mark.parametrize("engine", ["motis", "otp"])
def test_known_countries_build_the_graph(monkeypatch: pytest.MonkeyPatch, engine: str) -> None:
    out, events = _engine_run(monkeypatch, engine, _config("ZZ", "YY"))
    assert events[-1] == engine
    assert out == ("built", True, "/data/graphs/zz/current")


@pytest.mark.usefixtures("module")
def test_a_session_gone_or_without_providers_builds(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _engine_run(monkeypatch, "otp", None)[1] == ["send", "otp"]
    assert _engine_run(monkeypatch, "otp", {})[1] == ["send", "otp"]


@pytest.mark.usefixtures("module")
def test_a_failing_check_never_stops_the_build(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(_db: Any, _config: Any) -> set[str]:
        raise RuntimeError("stand-in")

    monkeypatch.setattr(country_gate, "build_blocked", broken)
    with caplog.at_level(logging.WARNING, logger="app.worker"):
        out, events = _engine_run(monkeypatch, "otp", _config("XX"))
    assert events == ["send", "otp"]
    assert out[1] is True
    assert "country gate not checked for zz (RuntimeError)" in caplog.text


def test_a_job_without_a_session_is_not_checked() -> None:
    assert worker._country_refusal(None) == ""
