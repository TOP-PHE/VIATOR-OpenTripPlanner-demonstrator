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


@pytest.mark.parametrize(
    "name,lat,lon",
    [
        ("Belgrade", 44.817, 20.457),
        ("Skopje", 41.997, 21.428),
        ("Podgorica", 42.441, 19.263),
        ("Kyiv", 50.450, 30.523),
    ],
)
def test_country_for_point_keeps_to_the_v1_list(name, lat, lon):
    # Bundled for the stop extract only: the OSM crop and the hubs never see them.
    assert g.country_for_point(lat, lon) is None, name


# ──────────────────────── country_of_position (the stop extract) ────────────────────────


@pytest.mark.parametrize(
    "name,lat,lon,expected",
    [
        ("Paris", 48.853, 2.349, "FR"),
        ("Genève", 46.210, 6.140, "CH"),
        ("Belgrade", 44.817, 20.457, "RS"),
        ("Novi Sad", 45.267, 19.833, "RS"),
        # By the Bulgarian and Romanian borders: Serbia, never its neighbours.
        ("Negotin", 44.228, 22.531, "RS"),
        ("Zaječar", 43.903, 22.275, "RS"),
        ("Subotica", 46.100, 19.666, "RS"),
        ("Kelebia", 46.198, 19.616, "HU"),
        ("Skopje", 41.997, 21.428, "MK"),
        ("Podgorica", 42.441, 19.263, "ME"),
        ("Sarajevo", 43.856, 18.413, "BA"),
        ("Tirana", 41.327, 19.819, "AL"),
        ("Pristina", 42.663, 21.165, "XK"),
        # Across the Hungarian and Slovak borders: Ukraine.
        ("Chop", 48.432, 22.201, "UA"),
        ("Chișinău", 47.011, 28.863, "MD"),
        ("Minsk", 53.902, 27.561, "BY"),
        ("Monaco", 43.739, 7.420, "MC"),
        ("Istanbul Sirkeci", 41.015, 28.977, "TR"),
        ("Kaliningrad", 54.710, 20.452, "RU"),
    ],
)
def test_country_of_position_knows_every_european_country(name, lat, lon, expected):
    assert g.country_of_position(lat, lon) == expected, name


@pytest.mark.parametrize(
    "name,lat,lon,expected",
    [
        # Outside the 1:50m coast by some 7 km: the nearest boundary's country.
        ("Mallaig", 57.006, -5.830, "GB"),
        ("Sheerness-on-Sea", 51.441, 0.758, "GB"),
        ("Split", 43.504, 16.441, "HR"),
        # Farther than COAST_TOLERANCE_KM from any boundary: none.
        ("St Mary's, Isles of Scilly", 49.916, -6.318, None),
        ("mid-Atlantic", 45.0, -30.0, None),
        ("null island", 0.0, 0.0, None),
    ],
)
def test_country_of_position_takes_the_nearest_boundary_on_a_coast(name, lat, lon, expected):
    assert g.country_of_position(lat, lon) == expected, name


def test_a_position_just_inside_the_tolerance_has_a_country_and_just_beyond_none():
    # Due north of Scotland's northernmost 1:50m point, over open sea.
    north = max(
        (lat, lon)
        for iso, _bbox, poly in g._polygons()
        if iso == "GB"
        for ring in poly
        for lon, lat in ring
    )
    km_per_degree = 110.57
    inside = north[0] + (g.COAST_TOLERANCE_KM - 0.5) / km_per_degree
    beyond = north[0] + (g.COAST_TOLERANCE_KM + 0.5) / km_per_degree
    assert g.country_of_position(inside, north[1]) == "GB"
    assert g.country_of_position(beyond, north[1]) is None


def test_the_bundled_boundaries_are_the_v1_list_and_the_other_countries():
    bundled = {iso for iso, _bbox, _geom in g._features()}
    assert bundled == g.VALID_COUNTRIES | g.OTHER_COUNTRIES
    assert not g.VALID_COUNTRIES & g.OTHER_COUNTRIES


def test_the_other_countries_are_never_offered_for_the_crop():
    with pytest.raises(ValueError):
        g.validate_countries(["RS"])
    every = g.crop_geojson(sorted(g.VALID_COUNTRIES))
    v1_polygons = sum(1 for iso, _bbox, _poly in g._polygons() if iso in g.VALID_COUNTRIES)
    assert len(every["geometry"]["coordinates"]) == v1_polygons


def test_detection_counts_no_stop_in_another_country():
    # Coordinates only, no UIC-shaped id: Belgrade and Skopje count nowhere.
    stops = [("ZZ-1", 44.817, 20.457), ("ZZ-2", 41.997, 21.428), ("ZZ-3", 48.853, 2.349)]
    assert g.detect_from_stops(stops) == {"FR": 1}


def test_the_distance_to_a_segment_takes_its_nearest_point():
    assert g._segment_km(3.0, 4.0, 3.0, 4.0) == pytest.approx(5.0)  # a point
    assert g._segment_km(-1.0, 2.0, 1.0, 2.0) == pytest.approx(2.0)  # the foot inside
    assert g._segment_km(1.0, 0.0, 2.0, 0.0) == pytest.approx(1.0)  # beyond an end


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
