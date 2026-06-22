"""Unit tests for app.api.geocode — MOTIS-geocoder proxy normaliser.

The endpoint itself is exercised at integration level alongside the
journey form (which is the only consumer); these tests pin the response
normalisation in isolation so a future MOTIS API change that adds a new
result type (e.g. ROUTE) doesn't quietly leak into the typeahead.
"""

from __future__ import annotations

from app.api.geocode import _normalize_hit


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
