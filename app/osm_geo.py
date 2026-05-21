"""Geographic OSM scope — crop a session's street graph to served countries.

The orthogonal companion to `app/osm_filter.py`: where `osm_filter` decides
*what kinds* of OSM ways to keep (tag scope — rail-focused etc.), this module
decides *where* to keep them (geographic scope — the served countries).

It is the single source of truth for:
  - the v1 country list (EU-27 + EFTA + UK) + display names,
  - `validate_countries()` — the config-write gate (mirrors osm_filter),
  - point-in-polygon country lookup for a coordinate (auto-detect), backed by
    a bundled simplified country-boundary GeoJSON (Natural Earth 50m,
    public domain — see NOTICE),
  - `detect_from_stops()` — count a GTFS feed's stops per country (UIC prefix
    primary, point-in-polygon fallback), and the ≥5-stop suggestion,
  - `crop_geojson()` — the merged polygon the build hands to `osmium extract`.

See docs/osm-geographic-scope-design.md.
"""

from __future__ import annotations

import json
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


def country_for_point(lat: float, lon: float) -> str | None:
    """ISO code of the v1 country containing (lat, lon), or None.

    Pure geographic lookup against the bundled boundaries. bbox pre-filter
    keeps it cheap. Coarse 50m borders mean a point within a few km of a
    border may resolve to the neighbour — fine for the suggestion (the UIC
    cross-check in `detect_from_stops` is the precise signal where present).
    """
    for iso, (min_lon, min_lat, max_lon, max_lat), geom in _features():
        if not (min_lon <= lon <= max_lon and min_lat <= lat <= max_lat):
            continue
        for poly in _iter_polygons(geom):
            if _polygon_contains(lon, lat, poly):
                return iso
    return None


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
    if lat is not None and lon is not None and (lat != 0.0 or lon != 0.0):
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
