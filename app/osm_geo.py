"""Geographic OSM scope — crop a session's street graph to served countries.

The orthogonal companion to `app/osm_filter.py`: where `osm_filter` decides
*what kinds* of OSM ways to keep (tag scope — rail-focused etc.), this module
decides *where* to keep them (geographic scope — the served countries).

It is the single source of truth for:
  - the v1 country list (EU-27 + EFTA + UK) + display names,
  - `validate_countries()` — the config-write gate (mirrors osm_filter),
  - point-in-polygon country lookup for a coordinate (auto-detect), backed by
    the bundled country boundaries (Natural Earth 1:50m, coarse; public
    domain — see NOTICE),
  - `country_of_position()` — the country of a stop for the station module's
    stop extract (MSMM 22.2, `iso2_point`): any bundled European country, not
    only the v1 list, and the nearest one for a stop on a coast the boundaries
    miss; none where 1:50m cannot tell (by a v1 border, by a neighbour's on a
    coast, in a neighbour),
  - `detect_from_stops()` — count a GTFS feed's stops per country (UIC prefix
    primary, point-in-polygon fallback), and the ≥5-stop suggestion,
  - `crop_geojson()` — the merged polygon the build hands to `osmium extract`.

See docs/osm-geographic-scope-design.md.
"""

from __future__ import annotations

import itertools
import json
import math
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path
from typing import Any

from .gtfs_cross_border_filter import UIC_COUNTRY_NAMES, country_prefix

# ─────────────────────────── v1 country list ───────────────────────────────
# EU-27 + EFTA (CH, NO, IS, LI) + UK. ISO-3166-1 alpha-2 → display name.
# Order here is the canonical display order (alphabetical by name); the UI
# renders the checklist in this order.
COUNTRY_NAMES: dict[str, str] = {
    "AT": "Austria",
    "BE": "Belgium",
    "BG": "Bulgaria",
    "HR": "Croatia",
    "CY": "Cyprus",
    "CZ": "Czechia",
    "DK": "Denmark",
    "EE": "Estonia",
    "FI": "Finland",
    "FR": "France",
    "DE": "Germany",
    "GR": "Greece",
    "HU": "Hungary",
    "IS": "Iceland",
    "IE": "Ireland",
    "IT": "Italy",
    "LV": "Latvia",
    "LI": "Liechtenstein",
    "LT": "Lithuania",
    "LU": "Luxembourg",
    "MT": "Malta",
    "NL": "Netherlands",
    "NO": "Norway",
    "PL": "Poland",
    "PT": "Portugal",
    "RO": "Romania",
    "SK": "Slovakia",
    "SI": "Slovenia",
    "ES": "Spain",
    "SE": "Sweden",
    "CH": "Switzerland",
    "GB": "United Kingdom",
}

VALID_COUNTRIES: frozenset[str] = frozenset(COUNTRY_NAMES)

# The other countries of the bundled boundaries: Natural Earth's other European
# countries (the Western Balkans, Ukraine with Crimea, Belarus, Moldova, Russia,
# the microstates, Åland, the Faroes and the Crown dependencies) and Turkey.
# Only `country_of_position` returns them — a stop in Serbia is in Serbia, not
# in no country — and only away from a border (`BORDER_DOUBT_KM`). They are
# never offered for the OSM crop (`validate_countries` refuses them), and
# `country_for_point` keeps to the v1 list.
OTHER_COUNTRIES: frozenset[str] = frozenset(
    {
        "AD", "AL", "AX", "BA", "BY", "FO", "GG", "IM", "JE", "MC",
        "MD", "ME", "MK", "RS", "RU", "SM", "TR", "UA", "VA", "XK",
    }
)  # fmt: skip

# The countries beyond the land borders of the bundled ones (and of French
# Guiana, Sakhalin, Gibraltar): bundled so that a stop on their land is known
# to be in none of ours, never returned. Without them the coast fallback gave
# Gibraltar to Spain, Qamishli to Turkey, Manzhouli to Russia.
NEIGHBOUR_COUNTRIES: frozenset[str] = frozenset(
    {"AM", "AZ", "BR", "CN", "GE", "GI", "IQ", "IR", "JP", "KP", "KZ", "MN", "SR", "SY"}
)

# A position in no country's boundary — on a coast, an estuary or an island the
# 1:50m boundaries leave out (up to some 9 km on the sea lochs and islands of
# western Scotland, 7 km on the Thames estuary) — is in the country whose
# boundary is nearest, within this distance; farther, in none.
COAST_TOLERANCE_KM = 10.0

# The 1:50m borders lie up to some 2.6 km from the real ones (measured on rail
# stations that fall inside their neighbour's polygon: Narva 2.5 km inside
# Russia's, Cap-d'Ail 2.4 km inside Monaco's, Metković 2.3 km inside
# Bosnia's; the station module's review queue of 25 October showed it between
# two v1 countries too: Kehl in France, Menton in Italy, Hendaye in Spain,
# Wissembourg in Germany, Vallorcine in Switzerland, Riksgränsen in Norway). A
# position is in none while the boundary of another bundled country passes
# within this distance (beyond the distance to its own country, for the coast
# fallback): 1:50m cannot tell, and an empty country is not a wrong one. The
# station module takes then the country of the station a stop matches (its
# name agreeing, within 300 m), and asks an administrator for a stop it cannot
# match. A small share of the stations lies within it, and the 1:50m polygon
# puts a good part of those in the wrong country. Until 25 October it applied only to a position whose country would
# be one of `OTHER_COUNTRIES` near a v1 boundary, or placed by the coast
# fallback near one of `NEIGHBOUR_COUNTRIES` (Gibraltar's airport).
BORDER_DOUBT_KM = 3.0

# Every bundled country: a boundary of any of them makes the border band.
BUNDLED_COUNTRIES: frozenset[str] = VALID_COUNTRIES | OTHER_COUNTRIES | NEIGHBOUR_COUNTRIES

# A country is *pre-ticked* in the UI only when it has at least this many
# stops — a flat threshold so a stray / (0,0) coordinate can't drag in a
# whole country. It's only the suggestion; the operator overrides, and the
# UI shows every country that has any stops (incl. below-threshold). See
# docs/osm-geographic-scope-design.md §3.4.
SUGGEST_MIN_STOPS = 5

_GEOJSON_PATH = Path(__file__).parent / "data" / "country_borders.geojson"


# ─────────────────────────── config validation ─────────────────────────────
def validate_countries(value: object | None) -> list[str]:
    """Normalise an `osm_countries` config value to a sorted list of ISO codes.

    None / empty list ⇒ `[]` (no geographic crop — today's behaviour).
    Codes are upper-cased, de-duplicated and sorted. An unrecognised code
    raises ValueError with the valid set, so a bad write 400s at save time
    rather than silently building the wrong graph.
    """
    if value is None or value == "":
        return []
    if not isinstance(value, list):
        raise ValueError(f"osm_countries must be a list of ISO codes, got {type(value).__name__}")
    out: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"osm_countries entries must be strings, got {type(item).__name__}")
        code = item.strip().upper()
        if not code:
            continue
        if code not in VALID_COUNTRIES:
            raise ValueError(
                f"osm_countries={item!r} is not a recognised country. "
                f"Valid codes: {sorted(VALID_COUNTRIES)}"
            )
        out.add(code)
    return sorted(out)


# ─────────────────────────── boundary geometry ─────────────────────────────
@lru_cache(maxsize=1)
def _features() -> list[tuple[str, tuple[float, float, float, float], Any]]:
    """Load the bundled GeoJSON once → list of (iso, bbox, geometry).

    bbox = (min_lon, min_lat, max_lon, max_lat), precomputed so point lookup
    can skip the expensive ray-cast for countries the point isn't near.
    """
    data = json.loads(_GEOJSON_PATH.read_text(encoding="utf-8"))
    out: list[tuple[str, tuple[float, float, float, float], Any]] = []
    for feat in data.get("features", []):
        iso = feat.get("properties", {}).get("ISO_A2")
        geom = feat.get("geometry")
        if not iso or not geom:
            continue
        out.append((iso, _geom_bbox(geom), geom))
    return out


def _geom_bbox(geom: dict[str, Any]) -> tuple[float, float, float, float]:
    xs: list[float] = []
    ys: list[float] = []
    for poly in _iter_polygons(geom):
        for ring in poly:
            for lon, lat in ring:
                xs.append(lon)
                ys.append(lat)
    return (min(xs), min(ys), max(xs), max(ys))


def _iter_polygons(geom: dict[str, Any]) -> Iterable[list[list[list[float]]]]:
    """Yield each polygon (list of rings) of a Polygon / MultiPolygon geometry."""
    gtype = geom.get("type")
    coords = geom.get("coordinates", [])
    if gtype == "Polygon":
        yield coords
    elif gtype == "MultiPolygon":
        yield from coords


def _ring_contains(lon: float, lat: float, ring: list[list[float]]) -> bool:
    """Even-odd ray-cast: is (lon, lat) inside this closed ring?"""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def _polygon_contains(lon: float, lat: float, polygon: list[list[list[float]]]) -> bool:
    """A polygon = outer ring + holes. XOR across all rings handles holes
    (enclaves): inside outer and not in a hole ⇒ odd ⇒ contained."""
    inside = False
    for ring in polygon:
        if _ring_contains(lon, lat, ring):
            inside = not inside
    return inside


@lru_cache(maxsize=1)
def _polygons() -> list[tuple[str, tuple[float, float, float, float], list[list[list[float]]]]]:
    """Every polygon of the bundled boundaries with its country and its bbox,
    in the file's order: a lookup tests a polygon's bbox before its rings."""
    out: list[tuple[str, tuple[float, float, float, float], list[list[list[float]]]]] = []
    for iso, _bbox, geom in _features():
        for poly in _iter_polygons(geom):
            out.append((iso, _geom_bbox({"type": "Polygon", "coordinates": poly}), poly))
    return out


def _containing(lat: float, lon: float, allowed: frozenset[str] | None) -> str | None:
    """The country of the first bundled polygon holding (lat, lon), among the
    `allowed` countries (every bundled one when None). The bundled polygons do
    not overlap (Natural Earth's shared borders are the same lines; checked by
    a test), so the first is the only one."""
    for iso, (min_lon, min_lat, max_lon, max_lat), poly in _polygons():
        if allowed is not None and iso not in allowed:
            continue
        inside_bbox = min_lon <= lon <= max_lon and min_lat <= lat <= max_lat
        if inside_bbox and _polygon_contains(lon, lat, poly):
            return iso
    return None


def _segment_km(ax: float, ay: float, bx: float, by: float) -> float:
    """Distance in km from the origin to the segment from A to B (coordinates in km)."""
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    t = max(0.0, min(1.0, -(ax * dx + ay * dy) / length2)) if length2 > 0.0 else 0.0
    return math.hypot(ax + t * dx, ay + t * dy)


def _polygon_km(
    lat: float, lon: float, poly: list[list[list[float]]], k: tuple[float, float]
) -> float:
    """Distance in km from (lat, lon) to the nearest ring of a polygon, with
    `k` the km of a degree of latitude and of longitude at the point."""
    k_lat, k_lon = k
    best = math.inf
    for ring in poly:
        for (x1, y1, *_), (x2, y2, *_) in itertools.pairwise(ring):
            best = min(
                best,
                _segment_km(
                    (x1 - lon) * k_lon, (y1 - lat) * k_lat, (x2 - lon) * k_lon, (y2 - lat) * k_lat
                ),
            )
    return best


def _boundaries_within(
    lat: float, lon: float, km: float, allowed: frozenset[str] | None = None
) -> dict[str, float]:
    """The bundled countries (among `allowed`, every one when None) whose
    boundary passes at most `km` from (lat, lon), each with its distance, in
    the file's order. Distances on a plane tangent at the point (exact enough
    within tens of km)."""
    k = (110.57, 111.32 * max(math.cos(math.radians(lat)), 0.05))
    d_lat, d_lon = km / k[0], km / k[1]
    out: dict[str, float] = {}
    for iso, (min_lon, min_lat, max_lon, max_lat), poly in _polygons():
        if allowed is not None and iso not in allowed:
            continue
        if not (
            min_lon - d_lon <= lon <= max_lon + d_lon and min_lat - d_lat <= lat <= max_lat + d_lat
        ):
            continue
        distance = _polygon_km(lat, lon, poly, k)
        if distance <= km and distance < out.get(iso, math.inf):
            out[iso] = distance
    return out


def country_for_point(lat: float, lon: float) -> str | None:
    """ISO code of the v1 country containing (lat, lon), or None.

    Pure geographic lookup against the bundled boundaries, the v1 countries
    only (a point in another bundled country, or in a neighbour, is in none
    here, as before they were bundled). bbox pre-filter keeps it cheap. Coarse 50m borders mean a
    point within a few km of a border may resolve to the neighbour — fine for
    the suggestion (the UIC cross-check in `detect_from_stops` is the precise
    signal where present).
    """
    return _containing(lat, lon, VALID_COUNTRIES)


def country_of_position(lat: float, lon: float) -> str | None:
    """The country of a stop's position for the station module's stop extract
    (MSMM 22.2, `iso2_point`), or None:

    1. the bundled country whose boundary holds it, v1 or not
       (`OTHER_COUNTRIES`); else, for a stop on a coast, an estuary or an
       island the 1:50m boundaries leave out, the country whose boundary is
       nearest within `COAST_TOLERANCE_KM`;
    2. None when that country is one of `NEIGHBOUR_COUNTRIES` (a stop in
       Georgia or Gibraltar is in none of ours);
    3. None when the boundary of another bundled country lies within
       `BORDER_DOUBT_KM` beyond it (Kehl inside the 1:50m France, Narva inside
       Russia, Gibraltar's airport between Spain and Gibraltar): 1:50m cannot
       tell which side of a border the position is on.

    None too for the GTFS placeholder (0, 0) and a position not finite.
    """
    if not (math.isfinite(lat) and math.isfinite(lon)) or (abs(lat) <= 1e-7 and abs(lon) <= 1e-7):
        return None
    iso = _containing(lat, lon, None)
    distance = 0.0
    if iso is None:
        near = _boundaries_within(lat, lon, COAST_TOLERANCE_KM)
        if not near:
            return None
        iso = min(near, key=near.__getitem__)
        distance = near[iso]
    if iso in NEIGHBOUR_COUNTRIES:
        return None
    others = BUNDLED_COUNTRIES - {iso}
    if _boundaries_within(lat, lon, distance + BORDER_DOUBT_KM, others):
        return None
    return iso


# ─────────────────────────── detection from stops ──────────────────────────
def country_of_stop(stop_id: str | None, lat: float | None, lon: float | None) -> str | None:
    """Best-effort country for one GTFS stop.

    Primary signal: the UIC country prefix embedded in `stop_id` (exact where
    present — rail feeds usually carry it). Fallback: point-in-polygon on the
    coordinates. Returns an ISO code in the v1 set, or None.
    """
    prefix = country_prefix(stop_id)
    if prefix is not None:
        iso = UIC_COUNTRY_NAMES.get(prefix)
        if iso in VALID_COUNTRIES:
            return iso
    # Skip the GTFS "null island" placeholder (0, 0); a tolerance avoids a
    # float equality check (Sonar S1244).
    if lat is not None and lon is not None and (abs(lat) > 1e-7 or abs(lon) > 1e-7):
        return country_for_point(lat, lon)
    return None


def detect_from_stops(
    stops: Iterable[tuple[str | None, float | None, float | None]],
) -> dict[str, int]:
    """Count stops per country for `(stop_id, lat, lon)` records.

    Stops that resolve to no v1 country (no UIC prefix, no/zero coords, or
    outside the supported set) are simply not counted.
    """
    counts: dict[str, int] = {}
    for stop_id, lat, lon in stops:
        iso = country_of_stop(stop_id, lat, lon)
        if iso is not None:
            counts[iso] = counts.get(iso, 0) + 1
    return counts


def suggested_countries(counts: dict[str, int]) -> list[str]:
    """ISO codes to pre-tick: those with ≥ SUGGEST_MIN_STOPS stops, sorted."""
    return sorted(iso for iso, n in counts.items() if n >= SUGGEST_MIN_STOPS)


# ─────────────────────────── crop polygon for osmium ───────────────────────
def crop_geojson(countries: Iterable[str]) -> dict[str, Any]:
    """Return a single-MultiPolygon-Feature GeoJSON for `osmium extract -p`.

    Merges every selected country's polygons into one MultiPolygon so osmium
    reads a single geometry. Raises ValueError on an unknown code (callers
    pass already-validated lists, but this stays defensive).
    """
    wanted = validate_countries(list(countries))
    multi: list[list[list[list[float]]]] = []
    for iso, _bbox, geom in _features():
        if iso not in wanted:
            continue
        multi.extend(_iter_polygons(geom))
    # A single Feature with a MultiPolygon geometry — the form `osmium extract
    # --polygon` accepts most reliably across versions (a FeatureCollection is
    # version-dependent).
    return {
        "type": "Feature",
        "properties": {"name": "viator-osm-crop", "countries": wanted},
        "geometry": {"type": "MultiPolygon", "coordinates": multi},
    }
