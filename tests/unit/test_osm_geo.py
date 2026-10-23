"""Unit tests for the geographic OSM scope module (`app/osm_geo.py`).

Covers the config validator, the bundled-boundary point-in-polygon lookup,
the GTFS-stop detection (UIC prefix + coordinate fallback), the ≥5-stop
suggestion threshold, and the osmium crop polygon.
"""

from __future__ import annotations

import hashlib
import math

import pytest

from app import osm_geo as g
from app import station_extract

# The sha256 of app/data/country_borders.geojson (see
# test_the_boundaries_file_changes_with_the_extractor_version).
BOUNDARIES_SHA256 = "b5ca4ef32d12062b03572fd269d5e1400a43fdc20cc8148b6a366b92fa0d0f6d"

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
        # By the Bulgarian and Hungarian borders, beyond BORDER_DOUBT_KM: Serbia.
        ("Negotin", 44.228, 22.531, "RS"),
        ("Zaječar", 43.903, 22.275, "RS"),
        ("Subotica", 46.100, 19.666, "RS"),
        ("Kelebia", 46.198, 19.616, "HU"),
        ("Skopje", 41.997, 21.428, "MK"),
        ("Podgorica", 42.441, 19.263, "ME"),
        ("Sarajevo", 43.856, 18.413, "BA"),
        ("Tirana", 41.327, 19.819, "AL"),
        ("Pristina", 42.663, 21.165, "XK"),
        ("Edirne", 41.677, 26.555, "TR"),
        ("Brest (Belarus)", 52.098, 23.735, "BY"),
        ("Chișinău", 47.011, 28.863, "MD"),
        ("Minsk", 53.902, 27.561, "BY"),
        ("Istanbul Sirkeci", 41.015, 28.977, "TR"),
        ("Kaliningrad", 54.710, 20.452, "RU"),
        ("Andorra la Vella", 42.507, 1.522, "AD"),
    ],
)
def test_country_of_position_knows_every_european_country(name, lat, lon, expected):
    assert g.country_of_position(lat, lon) == expected, name


@pytest.mark.parametrize(
    "name,lat,lon,expected",
    [
        # Stations of v1 countries that fall inside their neighbour's 1:50m
        # polygon: no country, as before the neighbours were bundled — never
        # the neighbour's (review of #389).
        ("Narva, EE", 59.3727, 28.1953, None),
        ("Záhony, HU", 48.4093, 22.1748, None),
        ("Medyka, PL", 49.803, 22.937, None),
        ("Idomeni, GR", 41.1237, 22.5128, None),
        ("Pythio, GR", 41.3718, 26.6209, None),
        ("Kastanies, GR", 41.66, 26.47, None),
        ("Tovarnik, HR", 45.1645, 19.1525, None),
        ("Metković, HR", 43.0537, 17.6488, None),
        ("Cap-d'Ail, FR", 43.720, 7.403, None),
        # The other side of the same lines, within BORDER_DOUBT_KM: none too.
        ("Chop, UA", 48.432, 22.201, None),
        ("Gevgelija, MK", 41.140, 22.503, None),
        ("Monaco-Monte-Carlo", 43.739, 7.420, None),
        # At sea 7 km off Monaco, in no polygon: Monaco's line is the nearest,
        # France's 0.2 km farther (the band counts from the nearest line).
        ("at sea off Monaco", 43.68, 7.43, None),
        # Inside their own v1 country: unchanged.
        ("Terespol, PL", 52.075, 23.62, "PL"),
        ("Valga, EE", 57.776, 26.045, "EE"),
        ("Svilengrad, BG", 41.77, 26.20, "BG"),
    ],
)
def test_a_position_by_a_v1_border_is_never_the_neighbours(name, lat, lon, expected):
    assert g.country_of_position(lat, lon) == expected, name


def test_the_border_doubt_is_measured_from_the_v1_boundary():
    # Due east of Narva, Russia's side: in doubt within BORDER_DOUBT_KM of the
    # 1:50m Estonian line, Russia beyond it (fixed distances, not the constant).
    lat, lon = 59.3727, 28.1953
    k_lon = 111.32 * math.cos(math.radians(lat))
    estonia = g._boundaries_within(lat, lon, 10.0, g.VALID_COUNTRIES)["EE"]
    assert estonia == pytest.approx(2.52, abs=0.05)
    assert g.country_of_position(lat, lon + (2.6 - estonia) / k_lon) is None
    assert g.country_of_position(lat, lon + (3.6 - estonia) / k_lon) == "RU"
    # Just inside the band, beyond 2.7 km: still in doubt (a narrower band fails).
    near_edge = lon + (2.9 - estonia) / k_lon
    assert g._boundaries_within(lat, near_edge, 10.0, g.VALID_COUNTRIES)["EE"] == pytest.approx(
        2.9, abs=0.02
    )
    assert g.country_of_position(lat, near_edge) is None


@pytest.mark.parametrize(
    "name,lat,lon,neighbour",
    [
        # Invented positions in Serbia, by the 1:50m lines of North Macedonia
        # and Bosnia and Herzegovina, more than 10 km from any v1 boundary.
        ("by North Macedonia", 42.3, 21.8, "MK"),
        ("by Bosnia and Herzegovina", 43.7, 19.5, "BA"),
    ],
)
def test_the_border_doubt_counts_only_v1_boundaries(name, lat, lon, neighbour):
    # A border between two countries outside the v1 list puts no stop in
    # doubt: neither side is minted from a wrong v1 country.
    near = g._boundaries_within(lat, lon, 10.0)
    assert near.get(neighbour, math.inf) < g.BORDER_DOUBT_KM
    assert not set(near) & g.VALID_COUNTRIES
    assert g.country_of_position(lat, lon) == "RS", name


@pytest.mark.parametrize(
    "name,lat,lon,expected",
    [
        # Crimea is Ukraine's (the EU position; Natural Earth EU point of
        # view), the rest of Russia Russia's.
        ("Simferopol", 44.952, 34.102, "UA"),
        ("Sevastopol", 44.616, 33.525, "UA"),
        ("Kerch", 45.357, 36.468, "UA"),
        ("Krasnodar", 45.035, 38.975, "RU"),
        ("Rostov-on-Don", 47.222, 39.718, "RU"),
        ("Port Kavkaz", 45.33, 36.67, "RU"),
    ],
)
def test_crimea_is_ukraine(name, lat, lon, expected):
    assert g.country_of_position(lat, lon) == expected, name


def test_the_crimea_polygon_is_ukraines_in_the_bundled_boundaries():
    holders = [
        iso
        for iso, _bbox, poly in g._polygons()
        if iso in ("RU", "UA") and g._polygon_contains(34.102, 44.952, poly)
    ]
    assert holders == ["UA"]
    assert g.country_for_point(44.952, 34.102) is None  # not a v1 country


@pytest.mark.parametrize(
    "name,lat,lon",
    [
        # In a country beyond the bundled ones' land borders: none, never the
        # nearest bundled country (the coast fallback is for a coast).
        ("Gibraltar", 36.14, -5.353),
        ("Qamishli, SY", 37.05, 41.22),
        ("Sarpi, GE", 41.52, 41.55),
        ("Batumi, GE", 41.65, 41.64),
        ("Samur, AZ", 41.83, 48.57),
        ("Manzhouli, CN", 49.6, 117.43),
        ("Sükhbaatar, MN", 50.23, 106.2),
        ("Oiapoque, BR", 3.84, -51.83),
    ],
)
def test_a_position_in_a_neighbour_country_is_in_none(name, lat, lon):
    assert g.country_of_position(lat, lon) is None, name


@pytest.mark.parametrize(
    "name,lat,lon,expected",
    [
        # Outside the 1:50m coast by some 7 km: the nearest boundary's country.
        ("Mallaig", 57.006, -5.830, "GB"),
        ("Sheerness-on-Sea", 51.441, 0.758, "GB"),
        ("Split", 43.504, 16.441, "HR"),
        ("Algeciras, beside Gibraltar", 36.129, -5.447, "ES"),
        ("Kirkenes", 69.727, 30.045, "NO"),
        # Farther than COAST_TOLERANCE_KM from any boundary: none.
        ("St Mary's, Isles of Scilly", 49.916, -6.318, None),
        ("mid-Atlantic", 45.0, -30.0, None),
        ("null island", 0.0, 0.0, None),
        ("not a number", math.nan, 2.349, None),
        ("infinite", math.inf, 2.349, None),
    ],
)
def test_country_of_position_takes_the_nearest_boundary_on_a_coast(name, lat, lon, expected):
    assert g.country_of_position(lat, lon) == expected, name


@pytest.mark.parametrize(
    "name,lat,lon,spain_km,gibraltar_km",
    [
        # In the gap between Gibraltar's 1:10m polygon and Spain's 1:50m one:
        # Spain's line is the nearest, Gibraltar's within BORDER_DOUBT_KM
        # beyond it. 1:50m cannot tell Spain from Gibraltar there, so none,
        # like Cap-d'Ail inside the polygons. Neither has a rail station.
        ("Gibraltar airport", 36.151, -5.347, 0.78, 1.09),
        ("La Línea, beside Gibraltar", 36.165, -5.347, 0.40, 2.64),
    ],
)
def test_the_coast_fallback_gives_none_by_a_neighbours_line(name, lat, lon, spain_km, gibraltar_km):
    near = g._boundaries_within(lat, lon, 10.0)
    assert g._containing(lat, lon, None) is None
    assert near["ES"] == pytest.approx(spain_km, abs=0.02)
    assert near["GI"] == pytest.approx(gibraltar_km, abs=0.02)
    assert g.country_of_position(lat, lon) is None, name


def test_the_neighbour_doubt_is_for_the_coast_fallback_only():
    # In the gap west of Albina (invented position): France's 1:50m line the
    # nearest, Suriname's 2.7 km off — none. Albina itself lies inside
    # France's 1:50m French Guiana, 3.0 km from Suriname's line: a position
    # inside a polygon keeps its country (a known limit of 1:50m).
    near = g._boundaries_within(5.496, -54.057, 10.0)
    assert near["SR"] - near["FR"] < g.BORDER_DOUBT_KM
    assert g.country_of_position(5.496, -54.057) is None
    assert g._containing(5.4975, -54.0533, None) == "FR"
    assert g.country_of_position(5.4975, -54.0533) == "FR"
    # Invented positions by Georgia's 1:50m line: inside Turkey's polygon,
    # 1.9 km from it, Turkish; off Turkey's coast, Turkey's line 0.5 km and
    # Georgia's 3.3 km away, none (the band holds for every country there).
    assert g._boundaries_within(41.46, 41.67, 3.0, g.NEIGHBOUR_COUNTRIES)["GE"] < 2.0
    assert g.country_of_position(41.46, 41.67) == "TR"
    near = g._boundaries_within(41.497, 41.481, 10.0)
    assert g._containing(41.497, 41.481, None) is None
    assert near["GE"] - near["TR"] < g.BORDER_DOUBT_KM
    assert g.country_of_position(41.497, 41.481) is None


def test_a_position_just_inside_the_tolerance_has_a_country_and_just_beyond_none():
    # Due north of Scotland's northernmost 1:50m point, over open sea, at
    # fixed distances (not derived from COAST_TOLERANCE_KM, so a changed
    # tolerance fails here).
    north = max(
        (lat, lon)
        for iso, _bbox, poly in g._polygons()
        if iso == "GB"
        for ring in poly
        for lon, lat in ring
    )
    km_per_degree = 110.57
    assert g.country_of_position(north[0] + 9.5 / km_per_degree, north[1]) == "GB"
    assert g.country_of_position(north[0] + 10.5 / km_per_degree, north[1]) is None
    assert g.country_of_position(north[0] + 12.5 / km_per_degree, north[1]) is None


def test_the_tolerance_runs_east_west_at_high_latitude():
    # Due west of Jan Mayen (Norway, 70.9°N), whose westernmost 1:50m vertex is
    # its nearest point: a degree of longitude is some 36.5 km there, not 111.
    lon, lat = min(
        (lon, lat)
        for iso, _bbox, poly in g._polygons()
        if iso == "NO"
        for ring in poly
        for lon, lat in ring
        if lat > 69.0
    )
    assert lat == pytest.approx(70.85, abs=0.05)
    k_lon = 111.32 * math.cos(math.radians(lat))
    assert g.country_of_position(lat, lon - 9.5 / k_lon) == "NO"
    assert g.country_of_position(lat, lon - 10.5 / k_lon) is None
    assert g.country_of_position(lat, lon - 12.5 / k_lon) is None


def test_an_enclave_is_a_hole_in_its_surrounding_country():
    # San Marino and the Vatican are holes in Italy's 1:50m polygon: in Italy
    # only when the holes are ignored.
    vatican = next(poly for iso, _bbox, poly in g._polygons() if iso == "VA")[0]
    middle = (
        sum(lat for _lon, lat in vatican[:-1]) / (len(vatican) - 1),
        sum(lon for lon, _lat in vatican[:-1]) / (len(vatican) - 1),
    )
    for lat, lon, own in ((43.936, 12.447, "SM"), (*middle, "VA")):
        assert g.country_for_point(lat, lon) is None
        assert g._containing(lat, lon, None) == own
    assert g.country_of_position(43.936, 12.447) == "SM"  # 3.2 km from Italy's line
    assert g.country_of_position(*middle) is None  # the Vatican: within 3 km of Italy's


def test_the_bundled_polygons_do_not_overlap():
    polygons = g._polygons()
    for i in range(int((72.0 - 34.0) / 0.5)):
        lat = 34.0 + 0.25 + 0.5 * i
        for j in range(int((50.0 + 25.0) / 0.5)):
            lon = -25.0 + 0.25 + 0.5 * j
            holders = [
                iso
                for iso, (min_lon, min_lat, max_lon, max_lat), poly in polygons
                if min_lon <= lon <= max_lon
                and min_lat <= lat <= max_lat
                and g._polygon_contains(lon, lat, poly)
            ]
            assert len(holders) <= 1, (lat, lon, holders)


def test_the_bundled_boundaries_are_the_v1_list_the_other_countries_and_neighbours():
    bundled = [iso for iso, _bbox, _geom in g._features()]
    assert set(bundled) == g.VALID_COUNTRIES | g.OTHER_COUNTRIES | g.NEIGHBOUR_COUNTRIES
    assert len(bundled) == len(set(bundled))
    assert not g.VALID_COUNTRIES & g.OTHER_COUNTRIES
    assert not (g.VALID_COUNTRIES | g.OTHER_COUNTRIES) & g.NEIGHBOUR_COUNTRIES
    # The v1 features first, as before: a v1 position stops at its own polygon.
    assert set(bundled[: len(g.VALID_COUNTRIES)]) == g.VALID_COUNTRIES


def test_the_boundaries_file_changes_with_the_extractor_version():
    # The rows of the stop extract follow this file: a change to it changes
    # `iso2_point`, so raise station_extract.EXTRACTOR_VERSION with it and
    # update both values here.
    digest = hashlib.sha256(g._GEOJSON_PATH.read_bytes()).hexdigest()
    assert digest == BOUNDARIES_SHA256
    assert station_extract.EXTRACTOR_VERSION == "viator-stop-extract-3"
    assert g._GEOJSON_PATH.stat().st_size < 1024 * 1024  # the pre-commit large-file cap


def test_the_other_countries_are_never_offered_for_the_crop():
    for code in ("RS", "UA", "GE", "GI"):
        with pytest.raises(ValueError):
            g.validate_countries([code])
    every = g.crop_geojson(sorted(g.VALID_COUNTRIES))
    v1_polygons = sum(1 for iso, _bbox, _poly in g._polygons() if iso in g.VALID_COUNTRIES)
    assert len(every["geometry"]["coordinates"]) == v1_polygons


def test_detection_counts_no_stop_in_another_country():
    # Coordinates only, no UIC-shaped id: Belgrade, Skopje and Tbilisi count nowhere.
    stops = [
        ("ZZ-1", 44.817, 20.457),
        ("ZZ-2", 41.997, 21.428),
        ("ZZ-3", 48.853, 2.349),
        ("ZZ-4", 41.716, 44.783),
    ]
    assert g.detect_from_stops(stops) == {"FR": 1}


def test_the_distance_to_a_country_is_its_nearest_polygons():
    # An invented position off Croatia's islands: two Croatian polygons within
    # 10 km, the first in the file's order the farther. The country's distance
    # is the nearer one, whatever the order.
    lat, lon = 43.3, 16.6
    k = (110.57, 111.32 * math.cos(math.radians(lat)))
    croatian = [
        g._polygon_km(lat, lon, poly, k)
        for iso, _bbox, poly in g._polygons()
        if iso == "HR" and g._polygon_km(lat, lon, poly, k) <= 10.0
    ]
    assert len(croatian) >= 2
    assert croatian[0] > min(croatian) + 1.0
    assert g._boundaries_within(lat, lon, 10.0)["HR"] == pytest.approx(min(croatian))


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
