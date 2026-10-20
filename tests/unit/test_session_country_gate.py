"""The country gate of a session's providers (D-R12 B of the station module's
design, the contract's phase 2).

A provider's country passes when VIATOR's Trainline list has a row for it
(as before) or when the station module serves it (`GET
/internal/v1/countries`, asked only when the Trainline list lacks one). A
country neither knows (17 October, the owner's first Estonian feed): when
VIATOR uses the module, accepted with a warning, since the module serves a
country only after accepting a feed VIATOR sends once the provider is
saved; when it does not, the same coded 409 as before. Driven through the
real save (`PATCH /api/sessions/{sid}`), the creation and the bulk import's
gate on a stand-in session; the module through `httpx.MockTransport`.
Invented values only: countries ZZ, YY, XX.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app import station_module
from app.api.admin import sessions
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


@pytest.fixture
def trainline(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    """The countries VIATOR's Trainline list has rows for (stands in for
    the GROUP BY of `_countries_without_stations`)."""
    present = {"ZZ"}
    monkeypatch.setattr(
        sessions, "_countries_without_stations", lambda _db, declared: set(declared) - present
    )
    return present


def test_unserved_is_the_trainline_gap_less_the_modules_countries() -> None:
    assert sessions._unserved({"YY", "XX"}, frozenset({"YY"})) == {"XX"}
    assert sessions._unserved({"YY"}, frozenset()) == {"YY"}
    # The module did not answer: the Trainline list alone, nothing let through.
    assert sessions._unserved({"YY", "XX"}, None) == {"YY", "XX"}


def _providers(*countries: str) -> dict[str, Any]:
    return {
        "sources": {
            "providers": [
                {"id": f"zz-{c.lower()}", "label": f"Zz {c}", "country_iso": c, "gtfs": []}
                for c in countries
            ]
        }
    }


def _save(
    monkeypatch: pytest.MonkeyPatch,
    *countries: str,
    stored: tuple[str, ...] = (),
    response: Any = None,
) -> Any:
    """The country gate of `patch_session`, as the route runs it (in a
    worker thread, outside the event loop), on a session whose stored
    providers declare `stored`."""
    from app.models import Session as SessionRow

    row = SessionRow(id="zz-session", name="Zz", config=_providers(*stored))
    db = _Db(row)
    body = sessions.SessionPatch(config=_providers(*countries))
    monkeypatch.setattr(sessions.ingestion, "normalize_providers", _normalised)
    answer = response if response is not None else _response()
    return sessions.patch_session("zz-session", body, _request(), answer, db, _actor())


def _normalised(config: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"id": p["id"], "country_iso": p["country_iso"]} for p in config["sources"]["providers"]
    ]


class _Db:
    def __init__(self, row: Any) -> None:
        self.row = row

    def get(self, *_args: Any, **_kwargs: Any) -> Any:
        return self.row

    def __getattr__(self, name: str) -> Any:
        raise _Reached(name)


class _Reached(Exception):
    """The save went past the country gate (it touched the database)."""


def _request() -> Any:
    from starlette.requests import Request

    return Request({"type": "http", "headers": [], "client": ("127.0.0.1", 1)})


def _response() -> Any:
    from fastapi import Response

    return Response()


def _actor() -> Any:
    import uuid

    from app.security import CurrentUser

    return CurrentUser(id=uuid.uuid4(), username="zz@example.invalid", role="platform_admin")


def _refusal(
    monkeypatch: pytest.MonkeyPatch, *countries: str, stored: tuple[str, ...] = ()
) -> dict[str, Any] | None:
    """The 409 detail of the save, or None when the gate let it through."""
    return _gate(monkeypatch, *countries, stored=stored)[0]


def _gate(
    monkeypatch: pytest.MonkeyPatch, *countries: str, stored: tuple[str, ...] = ()
) -> tuple[dict[str, Any] | None, list[str]]:
    """The save's 409 detail (None when the gate let it through) and the
    warnings of its answer (`X-Warnings`)."""
    from fastapi import HTTPException

    response = _response()
    try:
        _save(monkeypatch, *countries, stored=stored, response=response)
    except HTTPException as refused:
        assert refused.status_code == 409
        return dict(refused.detail), []
    except _Reached:
        return None, json.loads(response.headers.get("X-Warnings", "[]"))
    raise AssertionError("the save neither passed the gate nor was refused")


def _new_country(warnings: list[str], countries: list[str]) -> bool:
    return any(text.startswith(f"New country {countries}:") for text in warnings)


def test_a_country_with_trainline_rows_passes_without_asking_the_module(
    monkeypatch: pytest.MonkeyPatch, module: Module, trainline: set[str]
) -> None:
    assert _gate(monkeypatch, "ZZ") == (None, [])
    assert module.requests == []


def test_a_country_the_module_serves_passes_without_trainline_rows(
    monkeypatch: pytest.MonkeyPatch, module: Module, trainline: set[str]
) -> None:
    assert _gate(monkeypatch, "ZZ", "YY") == (None, [])
    (request,) = module.requests
    assert request.url.path == "/internal/v1/countries"


def test_a_new_country_is_accepted_with_a_warning_when_the_module_is_used(
    monkeypatch: pytest.MonkeyPatch, module: Module, trainline: set[str]
) -> None:
    """The deadlock of 17 October: the module serves XX only after it
    accepted a feed of XX, which VIATOR sends once the provider is saved
    and fetched. The save goes through and says what comes next."""
    detail, warnings = _gate(monkeypatch, "ZZ", "YY", "XX")

    assert detail is None
    (warning,) = warnings
    assert _new_country(warnings, ["XX"])
    assert "once the feed is accepted there" in warning
    assert "the graph is built and these countries route by coordinates" in warning


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(httpx.Response(500, json={"error_id": "zz"}), id="500"),
        pytest.param(httpx.Response(200, json={"countries": "YY"}), id="wrong-shape"),
    ],
)
def test_without_an_answer_of_the_module_a_new_country_is_accepted_with_a_warning(
    monkeypatch: pytest.MonkeyPatch, module: Module, trainline: set[str], response: httpx.Response
) -> None:
    """The module used but not answering: what it serves is not known, so
    YY is taken as new (never as served on a guess): accepted, warned."""
    module.response = response

    detail, warnings = _gate(monkeypatch, "YY")

    assert detail is None
    assert _new_country(warnings, ["YY"])
    assert not station_module.paused()


def test_without_the_module_configured_the_trainline_list_decides(
    monkeypatch: pytest.MonkeyPatch, module: Module, trainline: set[str]
) -> None:
    """Without the module nothing but the Trainline list can ever give YY
    its stations: refused, as before, with the advice to import it."""
    monkeypatch.setattr(settings, "station_module_url", "")

    detail = _refusal(monkeypatch, "YY")

    assert detail is not None
    assert detail["error"] == "missing_master_stations_for_countries"
    assert detail["missing_countries"] == ["YY"]
    assert "VIATOR does not use the station module" in detail["message"]
    assert "Import them from Trainline first" in detail["message"]
    assert module.requests == []


def test_a_country_already_saved_is_not_gated_again_when_the_module_is_down(
    monkeypatch: pytest.MonkeyPatch, module: Module, trainline: set[str]
) -> None:
    """YY passed the gate when it was saved (the module served it then):
    saving the session again while the module fails neither refuses nor
    warns about it; a country the save adds is still gated."""
    module.response = httpx.Response(500, json={"error_id": "zz"})

    assert _gate(monkeypatch, "YY", stored=("yy",)) == (None, [])
    assert module.requests == []
    detail, warnings = _gate(monkeypatch, "YY", "XX", stored=("YY",))
    assert detail is None
    assert _new_country(warnings, ["XX"])
    monkeypatch.setattr(settings, "station_module_url", "")
    detail = _refusal(monkeypatch, "YY", "XX", stored=("YY",))
    assert detail is not None
    assert detail["missing_countries"] == ["XX"]


def test_the_new_country_warning_comes_with_the_osm_warning(
    monkeypatch: pytest.MonkeyPatch, module: Module, trainline: set[str]
) -> None:
    """Both warnings of one save reach the page, the new country first."""
    from app.models import Session as SessionRow

    monkeypatch.setattr(sessions, "_COUNTRY_TO_OSM_HINTS", {"XX": ("xxland",)})
    monkeypatch.setattr(sessions.ingestion, "normalize_providers", _normalised)
    row = SessionRow(id="zz-session", name="Zz", config=_providers())
    config = _providers("XX")
    config["sources"]["osm_pbf"] = "https://osm.invalid/yyland-latest.osm.pbf"
    response = _response()
    patch = sessions.SessionPatch(config=config)
    request, db, actor = _request(), _Db(row), _actor()
    with pytest.raises(_Reached):
        sessions.patch_session("zz-session", patch, request, response, db, actor)

    new_country, osm = json.loads(response.headers["X-Warnings"])
    assert new_country.startswith("New country ['XX']:")
    assert "doesn't appear to cover ['XX']" in osm


class _Imported:
    """Stands in for `nap_importer.import_from_nap`: the providers it finds."""

    def __init__(self, *countries: str) -> None:
        self.providers = _providers(*countries)["sources"]["providers"]

    async def __call__(self, **_kwargs: Any) -> dict[str, Any]:
        return {"providers": self.providers, "skipped": [], "warnings": []}


class _SavingDb(_Db):
    """A database that takes the import's writes (the audit row, the commit)."""

    def __init__(self, row: Any) -> None:
        super().__init__(row)
        self.added: list[Any] = []
        self.committed = False

    def add(self, item: Any) -> None:
        self.added.append(item)

    def commit(self) -> None:
        self.committed = True


def _import(monkeypatch: pytest.MonkeyPatch, *countries: str, stored: tuple[str, ...] = ()) -> Any:
    """The bulk import's commit path, driven for real: the NAP fetch and the
    allow-list stand in, the gate and everything after it run. The 409
    detail, or the import's answer."""
    from fastapi import HTTPException

    from app.master import nap_importer
    from app.models import Session as SessionRow

    row = SessionRow(id="zz-session", name="Zz", config=_providers(*stored))
    monkeypatch.setattr(nap_importer, "import_from_nap", _Imported(*countries))
    monkeypatch.setattr(sessions, "_legacy_nap_url", lambda _db, _url: "https://nap.invalid/")
    monkeypatch.setattr(sessions.ingestion, "normalize_providers", _normalised)
    body = sessions.ImportFromNapBody(nap_url="https://nap.invalid/", preview=False)
    db = _SavingDb(row)
    try:
        answer = asyncio.run(
            sessions.import_providers_from_nap("zz-session", body, _request(), db, _actor())
        )
    except HTTPException as refused:
        assert refused.status_code == 409
        assert not db.committed
        return dict(refused.detail)
    assert db.committed
    return answer


def test_the_bulk_import_accepts_a_country_the_module_serves(
    monkeypatch: pytest.MonkeyPatch, module: Module, trainline: set[str]
) -> None:
    answer = _import(monkeypatch, "ZZ", "YY")
    assert answer.warnings == []
    assert [r.url.path for r in module.requests] == ["/internal/v1/countries"]


def test_the_bulk_import_accepts_a_new_country_with_a_warning(
    monkeypatch: pytest.MonkeyPatch, module: Module, trainline: set[str]
) -> None:
    answer = _import(monkeypatch, "XX")

    assert _new_country(answer.warnings, ["XX"])
    assert [p["country_iso"] for p in answer.providers] == ["XX"]


def test_the_bulk_import_refuses_a_new_country_without_the_module(
    monkeypatch: pytest.MonkeyPatch, module: Module, trainline: set[str]
) -> None:
    monkeypatch.setattr(settings, "station_module_url", "")

    detail = _import(monkeypatch, "XX")

    assert detail["error"] == "missing_master_stations_for_countries"
    assert detail["missing_countries"] == ["XX"]
    assert "retry the bulk-import" in detail["message"]


def test_the_bulk_import_does_not_gate_a_country_already_saved(
    monkeypatch: pytest.MonkeyPatch, module: Module, trainline: set[str]
) -> None:
    module.response = httpx.Response(500, json={"error_id": "zz"})

    assert _import(monkeypatch, "XX", stored=("XX",)).warnings == []
    assert module.requests == []


def test_a_stored_country_still_gets_the_osm_warning(
    monkeypatch: pytest.MonkeyPatch, module: Module, trainline: set[str]
) -> None:
    """Review round 2: the gate checks only the countries a save adds, but
    the OSM warning still covers every declared country: a stored ZZ
    provider whose OSM file now names another region is warned about."""
    from app.models import Session as SessionRow

    monkeypatch.setattr(sessions, "_COUNTRY_TO_OSM_HINTS", {"ZZ": ("zzland",)})
    monkeypatch.setattr(sessions.ingestion, "normalize_providers", _normalised)
    row = SessionRow(id="zz-session", name="Zz", config=_providers("ZZ"))
    config = _providers("ZZ")
    config["sources"]["osm_pbf"] = "https://osm.invalid/yyland-latest.osm.pbf"
    response = _response()
    patch = sessions.SessionPatch(config=config)
    request, db, actor = _request(), _Db(row), _actor()
    with pytest.raises(_Reached):
        sessions.patch_session("zz-session", patch, request, response, db, actor)

    (warning,) = json.loads(response.headers["X-Warnings"])
    assert "doesn't appear to cover ['ZZ']" in warning
    assert module.requests == []  # ZZ was stored: not gated


class _NoSession(_Db):
    """A database where the new session does not exist yet."""

    def get(self, *_args: Any, **_kwargs: Any) -> Any:
        return None


def _create(*countries: str) -> tuple[dict[str, Any] | None, list[str]]:
    from fastapi import HTTPException

    body = sessions.SessionCreate(
        id="zz-new", name="Zz", category="MANUAL", config=_providers(*countries)
    )
    response = _response()
    try:
        sessions.create_session(body, _request(), response, _NoSession(None), _actor())
    except HTTPException as refused:
        assert refused.status_code == 409
        return dict(refused.detail), []
    except _Reached:
        return None, json.loads(response.headers.get("X-Warnings", "[]"))
    raise AssertionError("the creation neither passed the gate nor was refused")


def test_creating_a_session_runs_the_country_gate(
    monkeypatch: pytest.MonkeyPatch, module: Module, trainline: set[str]
) -> None:
    """Review round 2: the providers a session is created with become its
    stored providers, which later saves no longer gate: creation gates them
    (a new country warned with the module, refused without)."""
    assert _create("ZZ", "YY") == (None, [])
    detail, warnings = _create("XX")
    assert detail is None
    assert _new_country(warnings, ["XX"])
    monkeypatch.setattr(settings, "station_module_url", "")
    detail, _ = _create("XX")
    assert detail is not None
    assert detail["error"] == "missing_master_stations_for_countries"
    assert detail["missing_countries"] == ["XX"]


def test_creating_a_session_without_providers_asks_nothing(
    module: Module, trainline: set[str]
) -> None:
    assert _create() == (None, [])
    assert module.requests == []
