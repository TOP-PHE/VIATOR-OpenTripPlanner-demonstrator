"""Unit tests for the geographic OSM scope module (`app/osm_geo.py`).

Covers the config validator, the bundled-boundary point-in-polygon lookup,
the GTFS-stop detection (UIC prefix + coordinate fallback), the ≥5-stop
suggestion threshold, and the osmium crop polygon.
"""

from __future__ import annotations

import pytest

from app import osm_geo as g

# ──────────────────────── validate_countries ────────────────────────


def test_validate_none_and_empty_give_empty_list():
    assert g.validate_countries(None) == []
    assert g.validate_countries("") == []
    assert g.validate_countries([]) == []


def test_validate_normalises_case_dedupes_sorts():
    assert g.validate_countries(["fr", "CH", "FR", "ch"]) == ["CH", "FR"]


def test_validate_skips_blank_entries():
    assert g.validate_countries(["FR", "  ", "CH"]) == ["CH", "FR"]


def test_validate_rejects_unknown_code():
    with pytest.raises(ValueError, match="not a recognised country"):
        g.validate_countries(["FR", "XX"])


def test_validate_rejects_non_list():
    with pytest.raises(ValueError, match="must be a list"):
        g.validate_countries("FR")  # a bare string is not a list of codes


def test_validate_rejects_non_string_entry():
    with pytest.raises(ValueError, match="entries must be strings"):
        g.validate_countries(["FR", 87])


def test_country_list_is_eu27_efta_uk():
    # 27 + EFTA(4) + UK = 32; spot-check the EFTA + UK members are present.
    assert len(g.VALID_COUNTRIES) == 32
    for iso in ("CH", "NO", "IS", "LI", "GB"):
        assert iso in g.VALID_COUNTRIES


# ──────────────────────── country_for_point ────────────────────────


@pytest.mark.parametrize(
    "name,lat,lon,expected",
    [
        ("Paris", 48.853, 2.349, "FR"),
        ("Zürich", 47.378, 8.540, "CH"),
        ("Genève", 46.210, 6.140, "CH"),
        ("London", 51.507, -0.128, "GB"),
        ("Berlin", 52.520, 13.405, "DE"),
        ("Rome", 41.900, 12.500, "IT"),
        ("Madrid", 40.420, -3.700, "ES"),
        ("Luxembourg City", 49.611, 6.131, "LU"),
        ("mid-Atlantic", 45.0, -30.0, None),
    ],
)
def test_country_for_point(name, lat, lon, expected):
    assert g.country_for_point(lat, lon) == expected, name


# ──────────────────────── country_of_stop ────────────────────────


def test_country_of_stop_prefers_uic_prefix():
    # 85 = CH, 87 = FR — exact even without coordinates.
    assert g.country_of_stop("8501120", None, None) == "CH"
    assert g.country_of_stop("StopPoint:OCETrain-87271007", None, None) == "FR"


def test_country_of_stop_falls_back_to_coordinates():
    # No UIC pattern in the id → point-in-polygon on the coords.
    assert g.country_of_stop("IDFM:monomodalStopPlace:43098", 48.85, 2.35) == "FR"


def test_country_of_stop_ignores_zero_coords_without_uic():
    assert g.country_of_stop("IDFM:x", 0.0, 0.0) is None
    assert g.country_of_stop(None, None, None) is None


def test_country_of_stop_uic_outside_v1_set_is_none():
    # 20 = RU — a real UIC prefix, but not in the v1 country list.
    assert g.country_of_stop("2000001", None, None) is None


# ──────────────────────── detect + suggest ────────────────────────


def test_detect_and_suggest_threshold():
    stops = (
        [("87fr", 48.8, 2.3)] * 6  # FR: 6 (UIC prefix)
        + [("85ch", 47.3, 8.5)] * 5  # CH: 5
        + [("70gb", 51.5, -0.1)] * 2  # GB: 2 (below threshold)
    )
    counts = g.detect_from_stops(stops)
    assert counts == {"FR": 6, "CH": 5, "GB": 2}
    # GB has only 2 stops → not pre-ticked, but still surfaced in counts.
    assert g.suggested_countries(counts) == ["CH", "FR"]


def test_suggest_exactly_at_threshold_is_included():
    assert g.suggested_countries({"FR": g.SUGGEST_MIN_STOPS}) == ["FR"]
    assert g.suggested_countries({"FR": g.SUGGEST_MIN_STOPS - 1}) == []


# ──────────────────────── crop_geojson ────────────────────────


def test_crop_geojson_merges_selected_countries():
    cg = g.crop_geojson(["FR", "CH"])
    # A single Feature with a MultiPolygon — the form osmium extract accepts.
    assert cg["type"] == "Feature"
    assert cg["geometry"]["type"] == "MultiPolygon"
    # FR (mainland + Corsica) + CH ⇒ several sub-polygons.
    assert len(cg["geometry"]["coordinates"]) >= 2
    assert cg["properties"]["countries"] == ["CH", "FR"]


def test_crop_geojson_rejects_unknown_country():
    with pytest.raises(ValueError, match="not a recognised country"):
        g.crop_geojson(["FR", "ZZ"])


def test_crop_geojson_empty_selection_is_empty_multipolygon():
    cg = g.crop_geojson([])
    assert cg["geometry"]["coordinates"] == []
