"""Unit tests for app.api.geocode — MOTIS-geocoder proxy.

Three layers are exercised:
  - `_normalize_hit`: pure mapper for one MOTIS hit → typeahead row
  - `_extract_stops`: pure list-level filter/limit
  - `_fetch_motis_geocode`: HTTP layer, driven by httpx MockTransport so
    no network call leaves the test process. Same pattern as
    tests/unit/test_motis_client.py.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
import pytest

from app.api import geocode as geocode_mod
from app.api.geocode import _extract_stops, _fetch_motis_geocode, _normalize_hit


def test_normalize_hit_passes_a_valid_stop():
    hit = {
        "type": "STOP",
        "name": "Basel, Aeschenplatz",
        "id": "sbb_Parent8500073",
        "lat": 47.55129914,
        "lon": 7.59485149,
        "country": "CH",
    }
    out = _normalize_hit(hit)
    assert out == {
        "name": "Basel, Aeschenplatz",
        "latitude": 47.55129914,
        "longitude": 7.59485149,
        "country_iso": "CH",
        "uic": None,
        "source": "motis",
    }


def test_normalize_hit_drops_non_stop_types():
    """ADDRESS, PLACE, POI all get dropped — the typeahead picker
    only knows what to do with stop coordinates."""
    for t in ("ADDRESS", "PLACE", "POI", "ROUTE", "AREA"):
        assert _normalize_hit({"type": t, "name": "x", "lat": 0.0, "lon": 0.0}) is None


def test_normalize_hit_drops_missing_coords():
    assert _normalize_hit({"type": "STOP", "name": "Nowhere"}) is None
    assert _normalize_hit({"type": "STOP", "name": "Nowhere", "lat": 0.0}) is None
    assert _normalize_hit({"type": "STOP", "name": "Nowhere", "lon": 0.0}) is None


def test_normalize_hit_drops_non_numeric_coords():
    assert _normalize_hit({"type": "STOP", "name": "x", "lat": "47.5", "lon": "7.5"}) is None


def test_normalize_hit_drops_empty_name():
    assert _normalize_hit({"type": "STOP", "name": "", "lat": 0.0, "lon": 0.0}) is None
    assert _normalize_hit({"type": "STOP", "lat": 0.0, "lon": 0.0}) is None


def test_normalize_hit_drops_non_dict_input():
    assert _normalize_hit("not a dict") is None
    assert _normalize_hit(None) is None
    assert _normalize_hit([1, 2, 3]) is None


def test_normalize_hit_allows_missing_country():
    """Some cross-border MOTIS stops omit `country` (e.g. on the FR/CH
    line) — typeahead handles a null country_iso gracefully."""
    hit = {
        "type": "STOP",
        "name": "Saint-Louis Gare",
        "lat": 47.58964569,
        "lon": 7.55520883,
    }
    out = _normalize_hit(hit)
    assert out is not None
    assert out["country_iso"] is None
    assert out["name"] == "Saint-Louis Gare"


def test_normalize_hit_accepts_int_coords():
    """MOTIS occasionally serialises a whole-degree coord as an int. The
    isinstance check uses `int | float` so both are accepted."""
    hit = {"type": "STOP", "name": "Equator+Greenwich", "lat": 0, "lon": 0}
    out = _normalize_hit(hit)
    assert out is not None
    assert out["latitude"] == 0.0
    assert out["longitude"] == 0.0
    assert isinstance(out["latitude"], float)


# ───────────────────────── _extract_stops ──────────────────────────────


_STOP_BASEL = {
    "type": "STOP",
    "name": "Basel SBB",
    "lat": 47.5474,
    "lon": 7.5896,
    "country": "CH",
}
_STOP_AESCHEN = {
    "type": "STOP",
    "name": "Basel, Aeschenplatz",
    "lat": 47.5513,
    "lon": 7.5948,
    "country": "CH",
}
_ADDRESS = {"type": "ADDRESS", "name": "Bahnhofstrasse 1", "lat": 47.5, "lon": 7.5}


def test_extract_stops_returns_empty_on_non_list():
    """MOTIS shouldn't, but if it ever returns a dict or null, don't crash."""
    assert _extract_stops(None, 20) == []
    assert _extract_stops({"error": "x"}, 20) == []
    assert _extract_stops("oops", 20) == []


def test_extract_stops_drops_non_stop_entries():
    payload = [_STOP_BASEL, _ADDRESS, _STOP_AESCHEN]
    out = _extract_stops(payload, 20)
    assert len(out) == 2
    assert [r["name"] for r in out] == ["Basel SBB", "Basel, Aeschenplatz"]


def test_extract_stops_respects_size_limit():
    """If MOTIS returns 100 stops and the UI asked for 5, only 5 come back."""
    payload = [{**_STOP_BASEL, "name": f"stop {i}"} for i in range(20)]
    out = _extract_stops(payload, 5)
    assert len(out) == 5
    assert out[0]["name"] == "stop 0"
    assert out[-1]["name"] == "stop 4"


def test_extract_stops_empty_list_in_empty_list_out():
    assert _extract_stops([], 20) == []


def test_extract_stops_all_filtered_returns_empty():
    """A response containing only non-STOP entries → []."""
    assert _extract_stops([_ADDRESS, _ADDRESS], 20) == []


# ──────────────────────── _fetch_motis_geocode ──────────────────────────
#
# Drive httpx via MockTransport so the test never makes a real network
# call. Same wiring pattern as tests/unit/test_motis_client.py.


def _install_mock_transport(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    real_cls = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_cls(*args, **kwargs)

    monkeypatch.setattr(geocode_mod.httpx, "AsyncClient", factory)


async def test_fetch_hits_expected_url_and_returns_json(monkeypatch):
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        return httpx.Response(200, json=[_STOP_BASEL])

    _install_mock_transport(monkeypatch, handler)
    out = await _fetch_motis_geocode("eu-rail-motis", "Basel")
    assert captured["url"].startswith("http://motis-eu-rail-motis:8080/api/v1/geocode")
    assert "text=Basel" in captured["url"]
    assert out == [_STOP_BASEL]


async def test_fetch_returns_empty_on_non_200(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="service down")

    _install_mock_transport(monkeypatch, handler)
    assert await _fetch_motis_geocode("eu-rail-motis", "Basel") == []


async def test_fetch_returns_empty_on_http_error(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=req)

    _install_mock_transport(monkeypatch, handler)
    assert await _fetch_motis_geocode("eu-rail-motis", "Basel") == []


async def test_fetch_returns_empty_on_non_json_body(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>oops</html>")

    _install_mock_transport(monkeypatch, handler)
    assert await _fetch_motis_geocode("eu-rail-motis", "Basel") == []


# ─────────────────────── how a call that got no answer is logged (#338) ───────────────────────
#
# "MOTIS geocoder unreachable for session …: " used to end with an empty
# `str(exc)` (a read timeout has no message). Now: a reason word and the
# exception's type at WARNING for a real failure; INFO for a call the browser
# dropped (superseded by a newer keystroke) or that was cancelled. Never the
# typed text. All values invented.

_TYPED = "Zzq-typed-338"
_SID = "zz-motis-338"


def _geocode_lines(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == geocode_mod.log.name]


@pytest.mark.parametrize(
    ("exc_type", "word"),
    [
        (httpx.ReadTimeout, "timeout"),
        (httpx.ConnectTimeout, "timeout"),
        (httpx.PoolTimeout, "timeout"),
        (httpx.ConnectError, "connect"),
        (httpx.RemoteProtocolError, "network"),
        (httpx.ReadError, "network"),
    ],
)
async def test_a_failure_is_a_warning_with_a_reason_word_and_its_type(
    monkeypatch, caplog, exc_type, word
):
    def handler(req: httpx.Request) -> httpx.Response:
        # The message carries the address, typed text included, as some
        # httpx errors do: it must not reach the log.
        raise exc_type(f"zz failure at {req.url}", request=req)

    _install_mock_transport(monkeypatch, handler)
    with caplog.at_level(logging.DEBUG, logger=geocode_mod.log.name):
        assert await _fetch_motis_geocode(_SID, _TYPED) == []

    (record,) = _geocode_lines(caplog)
    assert record.levelno == logging.WARNING
    assert record.getMessage() == (
        f"MOTIS geocoder unreachable for session {_SID}: reason={word} ({exc_type.__name__})"
    )
    assert _TYPED not in caplog.text


async def test_a_call_the_browser_dropped_is_info_and_motis_is_not_awaited(monkeypatch, caplog):
    motis_answered = asyncio.Event()

    async def slow_motis(req: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        motis_answered.set()
        return httpx.Response(200, json=[_STOP_BASEL])

    _install_mock_transport(monkeypatch, slow_motis)

    async def browser_left() -> None:
        await asyncio.sleep(0)

    with caplog.at_level(logging.DEBUG, logger=geocode_mod.log.name):
        out = await asyncio.wait_for(_fetch_motis_geocode(_SID, _TYPED, browser_left), 2)

    assert out == []
    assert not motis_answered.is_set()  # the call to MOTIS was cancelled
    (record,) = _geocode_lines(caplog)
    assert record.levelno == logging.INFO
    assert record.getMessage() == f"MOTIS geocoder call for session {_SID} ended: reason=superseded"
    assert _TYPED not in caplog.text


async def test_an_answer_before_the_browser_leaves_is_returned(monkeypatch, caplog):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[_STOP_BASEL])

    _install_mock_transport(monkeypatch, handler)
    still_there = asyncio.Event()

    with caplog.at_level(logging.DEBUG, logger=geocode_mod.log.name):
        out = await _fetch_motis_geocode(_SID, _TYPED, still_there.wait)

    assert out == [_STOP_BASEL]
    assert _geocode_lines(caplog) == []


async def test_when_listening_for_the_browser_fails_the_answer_is_still_awaited(
    monkeypatch, caplog
):
    async def motis(req: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.05)
        return httpx.Response(200, json=[_STOP_AESCHEN])

    _install_mock_transport(monkeypatch, motis)

    async def listening_fails() -> None:
        raise RuntimeError("zz receive failed")

    with caplog.at_level(logging.DEBUG, logger=geocode_mod.log.name):
        out = await _fetch_motis_geocode(_SID, _TYPED, listening_fails)

    assert out == [_STOP_AESCHEN]
    assert _geocode_lines(caplog) == []


async def test_a_cancelled_call_is_info_and_the_cancellation_goes_on(monkeypatch, caplog):
    started = asyncio.Event()

    async def slow_motis(req: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.sleep(5)
        return httpx.Response(200, json=[])

    _install_mock_transport(monkeypatch, slow_motis)

    with caplog.at_level(logging.DEBUG, logger=geocode_mod.log.name):
        task = asyncio.ensure_future(_fetch_motis_geocode(_SID, _TYPED))
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    (record,) = _geocode_lines(caplog)
    assert record.levelno == logging.INFO
    assert record.getMessage() == f"MOTIS geocoder call for session {_SID} ended: reason=cancelled"
    assert _TYPED not in caplog.text


async def test_client_gone_returns_on_the_disconnect_message_only():
    messages = iter(
        [
            {"type": "http.request", "body": b"", "more_body": False},
            {"type": "http.disconnect"},
        ]
    )
    seen: list[str] = []

    async def receive() -> dict:
        message = next(messages)
        seen.append(message["type"])
        return message

    request = geocode_mod.Request({"type": "http", "method": "GET", "headers": []}, receive)

    await asyncio.wait_for(geocode_mod._client_gone(request), 1)
    assert seen == ["http.request", "http.disconnect"]
