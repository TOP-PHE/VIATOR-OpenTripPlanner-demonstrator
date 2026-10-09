"""A date outside the loaded timetable is told apart from an error (#338).

MOTIS refuses such a date with HTTP 400 and `{"error": "query time … is outside
of loaded timetable window [from, to["}`; OTP answers 200 with an
OUTSIDE_SERVICE_PERIOD routing error. Every value here is invented (dates in
2031, session ids starting with zz).
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.api import journey as journey_api
from app.journey import motis_client, otp_client, timetable_window
from app.journey.timetable_window import OutsideTimetable, from_motis_refusal, from_otp_answer

_PHRASE = "is outside of loaded timetable window"
_REFUSAL = f"query time 2031-02-03 04:05:00 {_PHRASE} [2031-03-01 00:00, 2031-05-30 00:00["


def _response(status: int, **kwargs: Any) -> httpx.Response:
    return httpx.Response(
        status, request=httpx.Request("GET", "http://zz-motis/api/v6/plan"), **kwargs
    )


# ─────────────────────────── MOTIS: the refusal ───────────────────────────


def test_the_motis_refusal_gives_its_window() -> None:
    refusal = from_motis_refusal(_response(400, json={"error": _REFUSAL}))

    assert refusal == OutsideTimetable(
        datetime(2031, 3, 1, tzinfo=UTC), datetime(2031, 5, 30, tzinfo=UTC)
    )
    assert refusal is not None
    assert refusal.detail() == (
        "date outside the loaded timetable (loaded: 2031-03-01 00:00 to 2031-05-30 00:00 UTC)"
    )
    assert refusal.window() == {
        "from": "2031-03-01T00:00:00+00:00",
        "until": "2031-05-30T00:00:00+00:00",
    }


@pytest.mark.parametrize(
    "error",
    [
        f"query time 2031-02-03 04:05:00 {_PHRASE} [",  # no bounds
        f"query time 2031-02-03 04:05:00 {_PHRASE} [2031-05-30 00:00, 2031-03-01 00:00[",
        f"query time 2031-02-03 04:05:00 {_PHRASE} [2031-02-30 00:00, 2031-05-30 00:00[",
        f"{_PHRASE} [2031-03-01 00:00, 2031-04-01 00:00, 2031-05-30 00:00[",
    ],
    ids=["no-bounds", "reversed", "not-a-date", "three-stamps"],
)
def test_a_refusal_whose_window_does_not_read_says_no_window(error: str) -> None:
    refusal = from_motis_refusal(_response(400, json={"error": error}))

    assert refusal == OutsideTimetable()
    assert refusal is not None
    assert refusal.detail() == "date outside the loaded timetable"
    assert refusal.window() is None


@pytest.mark.parametrize(
    ("status", "kwargs"),
    [
        (400, {"json": {"error": "malformed URI or request"}}),
        (400, {"json": {"error": "query time 2031-02-03 04:05:00 is not valid"}}),
        (400, {"text": _REFUSAL}),  # not JSON
        (400, {"json": [_REFUSAL]}),  # not an object
        (400, {"json": {"error": 400}}),
        (400, {"json": {"message": _REFUSAL}}),
        (400, {"json": {"error": _REFUSAL + " " + "z" * 400}}),  # too long
        (500, {"json": {"error": _REFUSAL}}),
        (422, {"json": {"error": _REFUSAL}}),
    ],
    ids=[
        "other-400",
        "other-time-400",
        "plain-text",
        "json-list",
        "error-not-text",
        "no-error-key",
        "too-long",
        "status-500",
        "status-422",
    ],
)
def test_any_other_answer_is_not_a_refusal(status: int, kwargs: dict[str, Any]) -> None:
    assert from_motis_refusal(_response(status, **kwargs)) is None


def test_nothing_of_the_engines_text_reaches_the_detail() -> None:
    error = (
        "<b>query time</b> 2031-02-03 04:05:00 "
        f"{_PHRASE} [2031-03-01 00:00, 2031-05-30 00:00[ <script>zz()</script>"
    )
    refusal = from_motis_refusal(_response(400, json={"error": error}))

    assert refusal is not None
    detail = refusal.detail()
    assert "<" not in detail
    assert "zz" not in detail
    assert "query time" not in detail


# ──────────────────────── OTP: OUTSIDE_SERVICE_PERIOD ────────────────────────


def _otp(edges: list[Any], *codes: str) -> dict[str, Any]:
    errors = [{"code": code, "description": "zz"} for code in codes]
    return {"data": {"planConnection": {"edges": edges, "routingErrors": errors}}}


def test_otp_outside_service_period_is_a_refusal_without_window() -> None:
    assert from_otp_answer(_otp([], "OUTSIDE_SERVICE_PERIOD")) == OutsideTimetable()


@pytest.mark.parametrize(
    "raw",
    [
        _otp([{"node": {}}], "OUTSIDE_SERVICE_PERIOD"),  # it answered all the same
        _otp([], "NO_TRANSIT_CONNECTION_IN_SEARCH_WINDOW"),
        _otp([]),
        {"data": {"planConnection": None}},
        {"data": []},
        {"errors": [{"message": "zz"}]},
        {"data": {"planConnection": {"edges": [], "routingErrors": ["OUTSIDE_SERVICE_PERIOD"]}}},
    ],
    ids=[
        "with-edges",
        "other-code",
        "no-error",
        "no-plan",
        "data-list",
        "graphql-error",
        "error-not-object",
    ],
)
def test_any_other_otp_answer_is_not_a_refusal(raw: dict[str, Any]) -> None:
    assert from_otp_answer(raw) is None


# ───────────────────── through the journey API's session call ─────────────────────


def _body() -> journey_api.FanoutBody:
    return journey_api.FanoutBody.model_validate(
        {
            "from": {"lat": 46.5, "lon": 6.6},
            "to": {"lat": 47.4, "lon": 8.5},
            "depart_at": "2031-02-03T05:05:00",
        }
    )


def _session(engine: str) -> Any:
    return SimpleNamespace(id=f"zz-{engine}", engine=engine, config={})


def _install(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    transport = httpx.MockTransport(handler)
    real_cls = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_cls(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


async def _query(engine: str) -> tuple[Any, ...]:
    return await journey_api._query_session(
        None,  # type: ignore[arg-type]  # the session call never touches the database
        _session(engine),
        _body(),
        5000,
        num_itineraries=3,
        search_window_seconds=3600,
    )


async def test_a_motis_refusal_is_an_error_with_its_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    assert motis_client.httpx is httpx
    _install(monkeypatch, lambda request: httpx.Response(400, json={"error": _REFUSAL}))

    status, raw, trips, _, refusal = await _query("motis")

    assert (status, raw, trips) == ("error", {}, [])
    assert refusal is not None
    assert refusal.window() is not None
    fields = journey_api._refusal_fields(refusal)
    assert fields == {
        "reason": timetable_window.OUTSIDE_TIMETABLE,
        "detail": refusal.detail(),
        "timetable_window": refusal.window(),
    }


async def test_another_motis_400_stays_a_plain_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, lambda request: httpx.Response(400, json={"error": "zz bad place"}))

    status, _, _, _, refusal = await _query("motis")

    assert status == "error"
    assert refusal is None
    assert journey_api._refusal_fields(refusal) == {}


async def test_an_otp_refusal_is_no_route_with_its_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    assert otp_client.httpx is httpx
    _install(
        monkeypatch, lambda request: httpx.Response(200, json=_otp([], "OUTSIDE_SERVICE_PERIOD"))
    )

    status, _, trips, _, refusal = await _query("otp")

    assert (status, trips) == ("no_route", [])
    assert refusal == OutsideTimetable()
    assert journey_api._refusal_fields(refusal) == {
        "reason": "outside_timetable",
        "detail": "date outside the loaded timetable",
    }


async def test_an_otp_no_route_stays_a_no_route(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(
        monkeypatch,
        lambda request: httpx.Response(
            200, json=_otp([], "NO_TRANSIT_CONNECTION_IN_SEARCH_WINDOW")
        ),
    )

    status, _, _, _, refusal = await _query("otp")

    assert status == "no_route"
    assert refusal is None


# ───────────────────── the parsers never raise (#339 review) ─────────────────────


def test_a_deeply_nested_body_is_no_refusal_and_no_exception() -> None:
    deep = "[" * 100_000 + "]" * 100_000
    response = _response(400, content=deep.encode(), headers={"content-type": "application/json"})
    assert from_motis_refusal(response) is None


@pytest.mark.parametrize(
    "routing_errors",
    [7, 7.5, True, {"code": "OUTSIDE_SERVICE_PERIOD"}],
    ids=["int", "float", "bool", "dict"],
)
def test_otp_routing_errors_of_another_type_are_no_refusal(routing_errors: Any) -> None:
    raw = {"data": {"planConnection": {"edges": [], "routingErrors": routing_errors}}}
    assert from_otp_answer(raw) is None


def test_otp_answer_that_is_not_an_object_is_no_refusal() -> None:
    assert from_otp_answer([]) is None  # type: ignore[arg-type]
