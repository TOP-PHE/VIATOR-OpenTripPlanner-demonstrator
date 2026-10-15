"""The country gate of a session's providers (D-R12 B of the station module's
design, the contract's phase 2).

A provider's country passes when VIATOR's Trainline list has a row for it
(as before) or when the station module serves it (`GET
/internal/v1/countries`, asked only when the Trainline list lacks one); the
same coded 409 otherwise, and the Trainline list alone when the module does
not answer. Driven through the real save (`PATCH /api/sessions/{sid}`) and
the bulk import's gate on a stand-in session; the module through
`httpx.MockTransport`. Invented values only: countries ZZ, YY, XX.
"""

from __future__ import annotations

import asyncio
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


def _save(monkeypatch: pytest.MonkeyPatch, *countries: str) -> Any:
    """The country gate of `patch_session`, as the route runs it (in a
    worker thread, outside the event loop)."""
    from app.models import Session as SessionRow

    row = SessionRow(id="zz-session", name="Zz", config={})
    db = _Db(row)
    body = sessions.SessionPatch(config=_providers(*countries))
    monkeypatch.setattr(sessions.ingestion, "normalize_providers", _normalised)
    return sessions.patch_session("zz-session", body, _request(), _response(), db, _actor())


def _normalised(config: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"id": p["id"], "country_iso": p["country_iso"]} for p in config["sources"]["providers"]
    ]


class _Db:
    def __init__(self, row: Any) -> None:
        self.row = row

    def get(self, *_args: Any) -> Any:
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


def _refusal(monkeypatch: pytest.MonkeyPatch, *countries: str) -> dict[str, Any] | None:
    """The 409 detail of the save, or None when the gate let it through."""
    from fastapi import HTTPException

    try:
        _save(monkeypatch, *countries)
    except HTTPException as refused:
        assert refused.status_code == 409
        return dict(refused.detail)
    except _Reached:
        return None
    raise AssertionError("the save neither passed the gate nor was refused")


def test_a_country_with_trainline_rows_passes_without_asking_the_module(
    monkeypatch: pytest.MonkeyPatch, module: Module, trainline: set[str]
) -> None:
    assert _refusal(monkeypatch, "ZZ") is None
    assert module.requests == []


def test_a_country_the_module_serves_passes_without_trainline_rows(
    monkeypatch: pytest.MonkeyPatch, module: Module, trainline: set[str]
) -> None:
    assert _refusal(monkeypatch, "ZZ", "YY") is None
    (request,) = module.requests
    assert request.url.path == "/internal/v1/countries"


def test_a_country_served_by_neither_is_refused_with_the_same_code(
    monkeypatch: pytest.MonkeyPatch, module: Module, trainline: set[str]
) -> None:
    detail = _refusal(monkeypatch, "ZZ", "YY", "XX")

    assert detail is not None
    assert detail["error"] == "missing_master_stations_for_countries"
    assert detail["missing_countries"] == ["XX"]


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(httpx.Response(500, json={"error_id": "zz"}), id="500"),
        pytest.param(httpx.Response(200, json={"countries": "YY"}), id="wrong-shape"),
    ],
)
def test_without_an_answer_of_the_module_the_trainline_list_decides(
    monkeypatch: pytest.MonkeyPatch, module: Module, trainline: set[str], response: httpx.Response
) -> None:
    module.response = response

    detail = _refusal(monkeypatch, "YY")

    assert detail is not None
    assert detail["missing_countries"] == ["YY"]
    assert not station_module.paused()


def test_without_the_module_configured_the_trainline_list_decides(
    monkeypatch: pytest.MonkeyPatch, module: Module, trainline: set[str]
) -> None:
    monkeypatch.setattr(settings, "station_module_url", "")

    detail = _refusal(monkeypatch, "YY")

    assert detail is not None
    assert detail["missing_countries"] == ["YY"]
    assert module.requests == []


def test_the_bulk_import_gate_awaits_the_same_rule(module: Module) -> None:
    """`import_providers_from_nap` is a coroutine: it awaits
    `station_module.countries()` (never the blocking form) for the gap."""
    import inspect

    source = inspect.getsource(sessions.import_providers_from_nap)
    assert "_unserved(missing, await station_module.countries())" in source
    assert asyncio.run(station_module.countries()) == frozenset({"YY"})
    assert module.requests[0].method == "GET"
