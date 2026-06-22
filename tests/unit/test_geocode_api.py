"""Unit tests for app.api.geocode — MOTIS-geocoder proxy.

Three layers are exercised:
  - `_normalize_hit`: pure mapper for one MOTIS hit → typeahead row
  - `_extract_stops`: pure list-level filter/limit
  - `_fetch_motis_geocode`: HTTP layer, driven by httpx MockTransport so
    no network call leaves the test process. Same pattern as
    tests/unit/test_motis_client.py.
"""

from __future__ import annotations

import httpx

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
