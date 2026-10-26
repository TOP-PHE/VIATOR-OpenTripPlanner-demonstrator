"""Build app/data/country_borders_v1_patch.geojson: finer v1 borders (#404).

Run by hand, never in CI (it downloads some 425 MB and needs packages the
app does not use):

    pip install shapely==2.2.0 pyproj==3.8.0     # in a scratch environment
    npm install mapshaper@0.6.102                # in a scratch folder
    python scripts/build_v1_border_patch.py --cache /tmp/borders \\
        --mapshaper /path/to/node_modules/.bin/mapshaper

The bundled 1:50m file (`app/data/country_borders.geojson`) puts stations by
a border between two v1 countries on the wrong side (Kehl, Menton, Hendaye,
Konstanz…): its lines lie a median 1.1 km and up to some 4.4 km from the
real ones. This patch holds finer polygons of the v1 countries only within
10 km of their land borders with each other, and `osm_geo.country_of_position`
looks there first. The 1:50m file stays as it is.

Sources, each checked against the sha256 below (the build stops otherwise):

- the land borders: geoBoundaries CGAZ ADM0 (Runfola et al. 2020, CC BY 4.0),
  whose international boundaries are the U.S. Department of State's Large
  Scale International Boundaries (LSIB, public domain), from the publisher's
  own GitHub repository at a fixed commit (Git LFS);
- the coast and the land: Natural Earth 1:10m land v5.1.2 (public domain),
  from Natural Earth's own GitHub repository at its tag. CGAZ's coasts come
  from per-country files of mixed licences, so none of them is kept: the
  patch is Natural Earth's land (and 2 km of its sea), divided between the
  countries by CGAZ.

Steps:

1. CGAZ's polygons within Europe (-25..45 E, 34..72 N). Its United Kingdom
   folds in Gibraltar and the Sovereign Base Areas on Cyprus, which are not
   the United Kingdom: Gibraltar becomes GI (a neighbour, never a stop's
   country) and the United Kingdom keeps the British Isles only, so neither
   gives a "border" between two v1 countries.
2. The band: 10 km (EPSG:3035) around every land border between two v1
   countries (CGAZ boundaries of one that run within 100 m of the other,
   at least 50 m of them), its outline simplified at 1 km.
3. Fix areas: the land of a v1 country outside the band that the 1:50m file
   gives to another v1 country (Borkum, in the 1:50m Netherlands; a shore of
   the Flensburg Fjord in its Denmark): the whole island, or that part of a
   mainland with 3 km around it.
4. The patch of country c: CGAZ(c) ∩ the area, the area being Natural Earth
   land and its sea within 2 km (Natural Earth's coast lies about 1 km off
   at Menton and at Borkum's station) in the band, plus c's fix areas with
   their 2 km of sea. The parts of the area that no CGAZ country covers (the
   sea, an estuary CGAZ leaves out) go to the CGAZ country within 1 km,
   split at equal distance when there are several (a coastal strip across a
   border); a part that goes to a country outside the v1 list, to the
   disputed Piran Bay area or to no country is left out. The countries are
   then made disjoint, in `osm_geo.COUNTRY_NAMES` order.
5. mapshaper: Douglas-Peucker at 200 m, the shared lines simplified once
   (topology kept), coordinates rounded to 5 decimals (about 1 m).
6. One feature per v1 country, in `osm_geo.COUNTRY_NAMES` order, properties
   {"ISO_A2": code}, compact JSON. The file must stay under 1024 KB (the
   repository's large-file cap).

Any change to the output changes the stop extract's rows: raise
`station_extract.EXTRACTOR_VERSION` with it and update the patch's sha256
in tests/unit/test_osm_geo.py.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess  # nosec B404 - runs the mapshaper the operator names
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

from pyproj import Transformer
from shapely import get_coordinates, transform, voronoi_polygons
from shapely.geometry import MultiPoint, MultiPolygon, Polygon, box, mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from app.osm_geo import COUNTRY_NAMES  # noqa: E402

GEOBOUNDARIES_COMMIT = "5c25134028196d43ce97b5071934fd0cfc92f09f"
NATURAL_EARTH_TAG = "v5.1.2"
SOURCES: dict[str, tuple[str, str]] = {
    # name: (url, sha256)
    "cgaz_adm0.geojson": (
        "https://media.githubusercontent.com/media/wmgeolab/geoBoundaries/"
        f"{GEOBOUNDARIES_COMMIT}/releaseData/CGAZ/geoBoundariesCGAZ_ADM0.geojson",
        "5e97632703209f192d87724c3c02c3ed70eccec32ebddb605af9647002b65d67",
    ),
    "ne_10m_land.geojson": (
        "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/"
        f"{NATURAL_EARTH_TAG}/geojson/ne_10m_land.geojson",
        "1ac90796408bc6ad6911d69448485d3c4dbf2190370080368a09976e1c9f7416",
    ),
}

ISO3_TO_V1 = {
    "AUT": "AT", "BEL": "BE", "BGR": "BG", "HRV": "HR", "CYP": "CY", "CZE": "CZ",
    "DNK": "DK", "EST": "EE", "FIN": "FI", "FRA": "FR", "DEU": "DE", "GRC": "GR",
    "HUN": "HU", "ISL": "IS", "IRL": "IE", "ITA": "IT", "LVA": "LV", "LIE": "LI",
    "LTU": "LT", "LUX": "LU", "MLT": "MT", "NLD": "NL", "NOR": "NO", "POL": "PL",
    "PRT": "PT", "ROU": "RO", "SVK": "SK", "SVN": "SI", "ESP": "ES", "SWE": "SE",
    "CHE": "CH", "GBR": "GB",
}  # fmt: skip
EUROPE = box(-25.0, 34.0, 45.0, 72.0)
GIBRALTAR = box(-5.40, 36.10, -5.33, 36.16)
BRITISH_ISLES = box(-11.0, 49.0, 2.5, 61.0)
BAND_M = 10_000.0
BORDER_TOUCH_M = 100.0
BORDER_MIN_M = 50.0
NEAR_DEG = 0.01  # about 1 km: a CGAZ country next to uncovered land
ISLAND_MAX_DEG2 = 0.06  # about 500 km² at 50° N: a fix area takes the whole island
FIX_MARGIN_DEG = 0.03  # about 3 km around a mainland fix area
COAST_M = 2_000.0  # the sea kept along Natural Earth's coast (it lies ~1 km off at Menton)
SIMPLIFY_M = 200
PRECISION = 0.00001
MAX_BYTES = 1024 * 1024

_TO_3035 = Transformer.from_crs(4326, 3035, always_xy=True)
_TO_4326 = Transformer.from_crs(3035, 4326, always_xy=True)


def _project(geom: BaseGeometry, transformer: Transformer) -> BaseGeometry:
    return transform(geom, lambda xy: _xy(transformer, xy))


def _xy(transformer: Transformer, xy: Any) -> Any:
    x, y = transformer.transform(xy[:, 0], xy[:, 1])
    out = xy.copy()
    out[:, 0], out[:, 1] = x, y
    return out


def _polygons(geom: BaseGeometry) -> list[Polygon]:
    if geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    return [p for p in getattr(geom, "geoms", []) for p in _polygons(p)]


def fetch(cache: Path) -> dict[str, Path]:
    cache.mkdir(parents=True, exist_ok=True)
    out = {}
    for name, (url, sha256) in SOURCES.items():
        path = cache / name
        if not path.exists():
            print(f"downloading {url}", flush=True)
            # Fixed https URLs above (S310 / B310).
            with urllib.request.urlopen(url, timeout=600) as r, path.open("wb") as f:  # noqa: S310  # nosec B310
                shutil.copyfileobj(r, f)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != sha256:
            raise SystemExit(f"{name}: sha256 {digest}, expected {sha256}")
        out[name] = path
    return out


def load_cgaz(path: Path) -> dict[str, BaseGeometry]:
    """CGAZ's countries within Europe, by ISO3 (v1 or not), plus GI."""
    data = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, BaseGeometry] = {}
    for feature in data["features"]:
        geom = shape(feature["geometry"])
        if not geom.intersects(EUROPE):
            continue
        geom = geom.buffer(0).intersection(EUROPE)
        if not geom.is_empty:
            out[feature["properties"]["shapeGroup"]] = geom
    del data
    out["GI"] = out["GBR"].intersection(GIBRALTAR)
    out["GBR"] = out["GBR"].intersection(BRITISH_ISLES).difference(GIBRALTAR)
    return out


def band(cgaz: dict[str, BaseGeometry]) -> tuple[BaseGeometry, list[str]]:
    projected = {iso3: _project(cgaz[iso3], _TO_3035) for iso3 in ISO3_TO_V1 if iso3 in cgaz}
    zones, pairs = [], []
    codes = sorted(projected)
    for i, a in enumerate(codes):
        for b in codes[i + 1 :]:
            if not projected[a].envelope.buffer(200).intersects(projected[b].envelope):
                continue
            line = projected[a].boundary.intersection(projected[b].buffer(BORDER_TOUCH_M))
            if line.is_empty or line.length < BORDER_MIN_M:
                continue
            zones.append(line.buffer(BAND_M, quad_segs=4))
            pairs.append(f"{ISO3_TO_V1[a]}-{ISO3_TO_V1[b]}")
    return _project(unary_union(zones).simplify(1000), _TO_4326), pairs


def fix_areas(
    cgaz_v1: dict[str, BaseGeometry],
    land: BaseGeometry,
    region: BaseGeometry,
    coarse: dict[str, BaseGeometry],
) -> dict[str, BaseGeometry]:
    out: dict[str, BaseGeometry] = {}
    for iso, geom in cgaz_v1.items():
        others = unary_union([g for c, g in coarse.items() if c != iso])
        parts = []
        for part in _polygons(geom.intersection(land).difference(region)):
            overlap = part.intersection(others)
            if overlap.is_empty or overlap.area < 1e-6:
                continue
            if part.area <= ISLAND_MAX_DEG2:
                parts.append(part)
            else:
                parts.append(part.intersection(overlap.buffer(FIX_MARGIN_DEG)))
        if parts:
            out[iso] = unary_union(parts)
            print(f"fix area {iso}: {out[iso].area * 7400:.1f} km² at {out[iso].centroid}")
    return out


def _split_by_nearest(piece: Polygon, near: dict[str, BaseGeometry]) -> dict[str, BaseGeometry]:
    """Split `piece` between the `near` countries, each part to the nearest
    (Voronoi cells of their boundaries' points, every 20 m or so)."""
    owner: dict[tuple[float, float], str] = {}
    for code, geom in near.items():
        edge = geom.boundary.intersection(piece.buffer(NEAR_DEG)).segmentize(0.0002)
        for x, y in get_coordinates(edge):
            owner.setdefault((float(x), float(y)), code)
    seeds = list(owner)
    cells = voronoi_polygons(
        MultiPoint(seeds), extend_to=piece.envelope.buffer(NEAR_DEG), ordered=True
    )
    parts: dict[str, list[BaseGeometry]] = {}
    for seed, cell in zip(seeds, cells.geoms, strict=True):
        part = cell.intersection(piece)
        if not part.is_empty:
            parts.setdefault(owner[seed], []).append(part)
    return {code: unary_union(g) for code, g in parts.items()}


def grow(geom: BaseGeometry, metres: float) -> BaseGeometry:
    """`geom` and the sea within `metres` of it (in EPSG:3035)."""
    return _project(_project(geom, _TO_3035).buffer(metres, quad_segs=4), _TO_4326)


def uncovered(cgaz: dict[str, BaseGeometry], area: BaseGeometry) -> dict[str, list[BaseGeometry]]:
    """The parts of `area` that no CGAZ country covers, by the CGAZ country
    next to them (ISO3, or GI), split at equal distance between several."""
    covered = unary_union(list(cgaz.values()))
    out: dict[str, list[BaseGeometry]] = {}
    for piece in _polygons(area.difference(covered)):
        near = {c: g for c, g in cgaz.items() if g.distance(piece) < NEAR_DEG}
        if not near:
            continue
        parts = {next(iter(near)): piece} if len(near) == 1 else _split_by_nearest(piece, near)
        for code, geom in parts.items():
            out.setdefault(code, []).append(geom)
    return out


def build(cache: Path, mapshaper: str, out_path: Path) -> None:
    paths = fetch(cache)
    cgaz = load_cgaz(paths["cgaz_adm0.geojson"])
    region, pairs = band(cgaz)
    print(f"{len(pairs)} land borders between v1 countries: {' '.join(pairs)}")
    land = unary_union(
        [
            shape(f["geometry"])
            for f in json.loads(paths["ne_10m_land.geojson"].read_text())["features"]
        ]
    ).intersection(EUROPE)
    coarse = {
        f["properties"]["ISO_A2"]: shape(f["geometry"]).buffer(0)
        for f in json.loads((REPO / "app/data/country_borders.geojson").read_text())["features"]
        if f["properties"]["ISO_A2"] in COUNTRY_NAMES
    }
    cgaz_v1 = {ISO3_TO_V1[k]: g for k, g in cgaz.items() if k in ISO3_TO_V1}
    # Natural Earth's land and its sea within COAST_M, in the band.
    area = grow(land.intersection(region.buffer(0.05)), COAST_M).intersection(region)
    fixes = fix_areas(cgaz_v1, land, region, coarse)
    extra = uncovered(cgaz, area)
    covered = unary_union(list(cgaz.values()))
    owned: dict[str, BaseGeometry] = {}
    for iso3, iso in ISO3_TO_V1.items():
        if iso3 not in cgaz:
            continue
        parts = [cgaz[iso3].intersection(area), *extra.get(iso3, [])]
        if iso in fixes:
            # The fix area with its sea, but no other country's land.
            others = covered.difference(cgaz[iso3])
            parts.append(grow(fixes[iso], COAST_M).difference(others))
        owned[iso] = unary_union(parts)
    features = []
    taken: list[BaseGeometry] = []
    for iso in COUNTRY_NAMES:
        if iso not in owned:
            continue
        own = owned[iso].difference(unary_union(taken)) if taken else owned[iso]
        taken.append(own)
        polys = [p for p in _polygons(own) if p.area > 1e-9]
        if not polys:
            continue
        features.append(
            {
                "type": "Feature",
                "properties": {"ISO_A2": iso},
                "geometry": mapping(MultiPolygon(polys)),
            }
        )
    with tempfile.TemporaryDirectory() as tmp:
        raw, simplified = Path(tmp) / "raw.geojson", Path(tmp) / "simplified.geojson"
        raw.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
        # The operator's mapshaper with fixed arguments, no shell (S603 / B603).
        subprocess.run(  # noqa: S603  # nosec B603
            [
                mapshaper,
                "-i",
                str(raw),
                "snap",
                "-simplify",
                "dp",
                f"interval={SIMPLIFY_M}",
                "keep-shapes",
                "-o",
                str(simplified),
                "format=geojson",
                f"precision={PRECISION}",
            ],
            check=True,
        )
        result = json.loads(simplified.read_text())
    out = []
    for feature in result["features"]:
        out.append(
            {
                "type": "Feature",
                "properties": {"ISO_A2": feature["properties"]["ISO_A2"]},
                "geometry": feature["geometry"],
            }
        )
    text = json.dumps({"type": "FeatureCollection", "features": out}, separators=(",", ":"))
    text += "\n"
    if len(text.encode()) >= MAX_BYTES:
        raise SystemExit(f"{len(text.encode())} bytes: over the 1024 KB cap")
    out_path.write_text(text, encoding="utf-8")
    print(
        f"{out_path}: {len(text.encode())} bytes, sha256 {hashlib.sha256(text.encode()).hexdigest()}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cache", type=Path, required=True, help="folder for the downloads")
    parser.add_argument("--mapshaper", default="mapshaper", help="the mapshaper 0.6.102 command")
    parser.add_argument(
        "--out", type=Path, default=REPO / "app/data/country_borders_v1_patch.geojson"
    )
    args = parser.parse_args()
    build(args.cache, args.mapshaper, args.out)


if __name__ == "__main__":
    main()
