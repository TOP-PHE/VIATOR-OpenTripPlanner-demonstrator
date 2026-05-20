"""GTFS cross-border filter — keep only routes that cross a national border.

Motivation (the "corridors session"):

    VIATOR's cross-NAP federation needs a small GTFS containing *only* the
    international rail services (TGV Lyria, Eurostar, ICE International,
    cross-border TER like Delle↔Delémont, the Centovalli Brig→Domodossola→
    Locarno, etc.). Those services are already inside the big national GTFS
    feeds (SNCF, SBB, DB), bundled with thousands of domestic-only routes.
    Loading a whole national feed into a corridors session would bloat the
    OTP graph with 95% irrelevant data.

    This filter extracts the cross-border subset automatically — no manual
    list of "famous cross-border quirks" to maintain.

Detection rule:

    Every European rail station carries a UIC code whose first two digits
    encode the country (87=FR, 85=CH, 80=DE, 88=BE, 84=NL, 83=IT, 71=ES,
    70=GB, 81=AT, 82=LU, …). A GTFS route is "cross-border" iff its
    `stop_times` reference stops whose UICs carry **2+ distinct country
    prefixes**.

    The rule is agnostic to *where* the crossing happens:
      - endpoint crossing  (Paris→Zürich Lyria: 87…→85…)
      - mid-journey crossing (Brig→Domodossola→Locarno: 85→83→85)
      - brief in-and-out     (Centovalli line dipping into IT)
    all get the same treatment: 2+ country prefixes ⇒ kept.

Stdlib only (`csv` + `zipfile`) — no pandas dependency. `stop_times.txt`
(the largest file, millions of rows on a national feed) is **streamed**
in two passes:

    pass 1 — accumulate route_id → {country prefixes}  (decide cross-border)
    pass 2 — write only the rows of kept trips           (and collect stops)

so memory stays bounded regardless of feed size.

This is the in-app sibling of the manual rail-only filter documented in
docs/nap-ch-rail.md §3.3. Where that one selects by `route_type`, this
one selects by cross-border-ness.

Limitations (v1, strict mode):
  - Stops whose stop_id carries no parseable UIC contribute "unknown
    country" and don't count toward the 2+-country test. For the major
    rail operators (SNCF/SBB/DB/Eurostar/Trenitalia) every stop carries a
    UIC, so this is rarely an issue; non-UIC feeds (IDFM-style) simply
    won't be detected as cross-border. Documented; a permissive mode
    (also keep routes terminating at a known border interchange) is a
    follow-up.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import zipfile
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

log = logging.getLogger(__name__)

# A 7- or 8-digit UIC code embedded anywhere in the stop_id, anchored so
# we don't grab a sub-run of a longer number. France often publishes the
# 8-digit form (UIC + trailing check digit, e.g. "87286005"); Switzerland
# the 7-digit form ("8503000"). Either way the country is the first 2
# digits. `(?<!\d) … (?!\d)` ensures we match a whole code, not a slice.
_UIC_RE = re.compile(r"(?<!\d)(\d{2})\d{5,6}(?!\d)")

# ISO-2 ↔ UIC numeric country prefix, for human-readable stats / logs.
# Not exhaustive — just the European rail countries VIATOR is likely to
# touch. Unknown prefixes still work for the cross-border test (they're
# distinct numbers); this map is only for labelling.
UIC_COUNTRY_NAMES: dict[str, str] = {
    "87": "FR",
    "85": "CH",
    "80": "DE",
    "88": "BE",
    "84": "NL",
    "83": "IT",
    "82": "LU",
    "81": "AT",
    "79": "SI",
    "78": "HR",
    "76": "NO",
    "74": "SE",
    "73": "DK",
    "71": "ES",
    "70": "GB",
    "54": "CZ",
    "51": "PL",
}


def country_prefix(stop_id: str | None) -> str | None:
    """Return the 2-digit UIC country prefix embedded in a stop_id, or None.

    Examples:
        "8503000"                       → "85"  (CH, 7-digit)
        "87286005"                      → "87"  (FR, 8-digit with check)
        "StopPoint:OCETrain-87271007"   → "87"  (FR, embedded)
        "Parent8503000"                 → "85"  (CH, prefixed)
        "8503000:0:5"                   → "85"  (CH, platform suffix)
        "IDFM:monomodalStopPlace:43098" → None  (no UIC pattern)
        None / ""                       → None
    """
    if not stop_id:
        return None
    m = _UIC_RE.search(stop_id)
    return m.group(1) if m else None


@dataclass
class CrossBorderStats:
    """Summary of a filter run — surfaced in logs and the operator UI."""

    routes_total: int = 0
    routes_kept: int = 0
    trips_total: int = 0
    trips_kept: int = 0
    stop_times_total: int = 0
    stop_times_kept: int = 0
    stops_total: int = 0
    stops_kept: int = 0
    # Which country pairs/sets appeared in kept routes, for a human sanity
    # read ("did we actually pick up FR↔CH, FR↔DE, …?").
    country_combos: dict[str, int] = field(default_factory=dict)

    def summary_line(self) -> str:
        combos = ", ".join(f"{k}:{v}" for k, v in sorted(self.country_combos.items()))
        return (
            f"cross-border filter: kept {self.routes_kept}/{self.routes_total} routes, "
            f"{self.trips_kept}/{self.trips_total} trips, "
            f"{self.stops_kept}/{self.stops_total} stops "
            f"[{combos or 'none'}]"
        )


# GTFS member filenames (constants — referenced repeatedly; SonarCloud
# S1192 flags duplicated string literals).
_AGENCY = "agency.txt"
_STOPS = "stops.txt"
_ROUTES = "routes.txt"
_TRIPS = "trips.txt"
_STOP_TIMES = "stop_times.txt"
_CALENDAR = "calendar.txt"
_CALENDAR_DATES = "calendar_dates.txt"
_SHAPES = "shapes.txt"
_TRANSFERS = "transfers.txt"
_FREQUENCIES = "frequencies.txt"

# GTFS files we rewrite by filtering rows. Anything not listed (feed_info,
# fare_attributes, …) is copied through verbatim if present.
_FILTERABLE = {
    _AGENCY,
    _STOPS,
    _ROUTES,
    _TRIPS,
    _STOP_TIMES,
    _CALENDAR,
    _CALENDAR_DATES,
    _SHAPES,
    _TRANSFERS,
    _FREQUENCIES,
}


def _read_csv(zf: zipfile.ZipFile, name: str) -> tuple[list[str], list[dict[str, str]]]:
    """Read a GTFS member into (fieldnames, rows). Empty if absent."""
    if name not in zf.namelist():
        return ([], [])
    with zf.open(name) as f:
        text = io.TextIOWrapper(f, encoding="utf-8-sig", newline="")
        reader = csv.DictReader(text)
        fieldnames: list[str] = list(reader.fieldnames or [])
        # csv.DictReader is typed to yield dict[str, str | Any] (because of
        # restval/restkey); our GTFS cells are always strings, so cast the
        # whole list rather than re-building each row.
        rows = cast("list[dict[str, str]]", list(reader))
        return (fieldnames, rows)


def _stream_csv(zf: zipfile.ZipFile, name: str) -> Iterator[Any]:
    """Yield fieldnames (list[str]) first, then each row (dict[str, str]).

    Mixed-type generator: the caller does `fieldnames = next(gen)` then
    iterates the remaining rows. Typed `Iterator[Any]` because the first
    yield differs from the rest.

    The caller MUST exhaust the iterator before the `with zf.open` context
    closes — we keep it open for the generator's lifetime.
    """
    with zf.open(name) as f:
        text = io.TextIOWrapper(f, encoding="utf-8-sig", newline="")
        reader = csv.DictReader(text)
        fieldnames: list[str] = list(reader.fieldnames or [])
        yield fieldnames
        yield from reader


def _write_csv(
    zf: zipfile.ZipFile, name: str, fieldnames: list[str], rows: list[dict[str, str]]
) -> None:
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    zf.writestr(name, buf.getvalue())


@dataclass
class _KeptSets:
    """The id sets that survive the cross-border cut, used to cascade-
    filter every dependent GTFS file."""

    route_ids: set[str]
    trip_ids: set[str]
    service_ids: set[str]
    shape_ids: set[str]
    agency_ids: set[str]
    stop_ids: set[str] = field(default_factory=set)


def _stop_country_map(
    stop_rows: list[dict[str, str]],
) -> tuple[dict[str, str | None], dict[str, str]]:
    """Build stop_id → country prefix and stop_id → parent_station maps.

    A platform-level stop may carry no UIC of its own but a parent that
    does (or vice versa); inherit a missing country from the parent.
    """
    stop_country: dict[str, str | None] = {}
    stop_parent: dict[str, str] = {}
    for s in stop_rows:
        sid = s.get("stop_id", "")
        stop_country[sid] = country_prefix(sid)
        parent = (s.get("parent_station") or "").strip()
        if parent:
            stop_parent[sid] = parent
    for sid, parent in stop_parent.items():
        if stop_country.get(sid) is None and stop_country.get(parent):
            stop_country[sid] = stop_country[parent]
    return stop_country, stop_parent


def _classify_routes(
    zin: zipfile.ZipFile,
    trip_route: dict[str, str],
    stop_country: dict[str, str | None],
    stats: CrossBorderStats,
) -> tuple[set[str], dict[str, set[str]]]:
    """Pass 1 over stop_times: accumulate route → {country prefixes}, then
    return the set of routes spanning 2+ countries (plus the per-route
    country sets for stats labelling)."""
    route_countries: dict[str, set[str]] = defaultdict(set)
    if _STOP_TIMES in zin.namelist():
        gen = _stream_csv(zin, _STOP_TIMES)
        _ = next(gen)  # fieldnames (unused in pass 1)
        for row in gen:
            stats.stop_times_total += 1
            route_id = trip_route.get(row.get("trip_id", ""))
            if route_id is None:
                continue
            ctry = stop_country.get(row.get("stop_id", ""))
            if ctry:
                route_countries[route_id].add(ctry)
    cross_border = {rid for rid, ctrys in route_countries.items() if len(ctrys) >= 2}
    return cross_border, route_countries


def _collect_stop_times(
    zin: zipfile.ZipFile, kept_trip_ids: set[str], stats: CrossBorderStats
) -> tuple[list[str], list[dict[str, str]], set[str]]:
    """Pass 2 over stop_times: keep rows of kept trips, collect their stops."""
    kept_stop_ids: set[str] = set()
    out_rows: list[dict[str, str]] = []
    fields: list[str] = []
    if _STOP_TIMES in zin.namelist():
        gen = _stream_csv(zin, _STOP_TIMES)
        fields = next(gen)
        for row in gen:
            if row.get("trip_id", "") in kept_trip_ids:
                out_rows.append(row)
                kept_stop_ids.add(row.get("stop_id", ""))
    stats.stop_times_kept = len(out_rows)
    return fields, out_rows, kept_stop_ids


def _write_filtered_feed(
    zin: zipfile.ZipFile,
    zout: zipfile.ZipFile,
    *,
    kept: _KeptSets,
    route_fields: list[str],
    route_rows: list[dict[str, str]],
    trip_fields: list[str],
    trip_rows: list[dict[str, str]],
    stop_fields: list[str],
    stop_rows: list[dict[str, str]],
    st_fields: list[str],
    st_rows: list[dict[str, str]],
) -> None:
    """Write every GTFS member, cascade-filtered against `kept`."""
    if route_fields:
        _write_csv(
            zout,
            _ROUTES,
            route_fields,
            [r for r in route_rows if r.get("route_id", "") in kept.route_ids],
        )
    if trip_fields:
        _write_csv(
            zout,
            _TRIPS,
            trip_fields,
            [t for t in trip_rows if t.get("trip_id", "") in kept.trip_ids],
        )
    if st_fields:
        _write_csv(zout, _STOP_TIMES, st_fields, st_rows)
    if stop_fields:
        _write_csv(
            zout,
            _STOPS,
            stop_fields,
            [s for s in stop_rows if s.get("stop_id", "") in kept.stop_ids],
        )

    # agency.txt — keep referenced agencies; if no agency_id column
    # (single-agency feed), copy through whole.
    agency_fields, agency_rows = _read_csv(zin, _AGENCY)
    if agency_fields:
        if "agency_id" in agency_fields and kept.agency_ids:
            agency_rows = [a for a in agency_rows if a.get("agency_id", "") in kept.agency_ids]
        _write_csv(zout, _AGENCY, agency_fields, agency_rows)

    # calendar / calendar_dates — keep kept services
    for cal_name in (_CALENDAR, _CALENDAR_DATES):
        fields, rows = _read_csv(zin, cal_name)
        if fields:
            rows = [r for r in rows if r.get("service_id", "") in kept.service_ids]
            _write_csv(zout, cal_name, fields, rows)

    # shapes — keep kept shapes
    shape_fields, shape_rows = _read_csv(zin, _SHAPES)
    if shape_fields:
        shape_rows = [r for r in shape_rows if r.get("shape_id", "") in kept.shape_ids]
        _write_csv(zout, _SHAPES, shape_fields, shape_rows)

    # transfers — keep transfers where BOTH stops survive
    tr_fields, tr_rows = _read_csv(zin, _TRANSFERS)
    if tr_fields:
        tr_rows = [
            r
            for r in tr_rows
            if r.get("from_stop_id", "") in kept.stop_ids
            and r.get("to_stop_id", "") in kept.stop_ids
        ]
        _write_csv(zout, _TRANSFERS, tr_fields, tr_rows)

    # frequencies — keep kept trips
    fr_fields, fr_rows = _read_csv(zin, _FREQUENCIES)
    if fr_fields:
        fr_rows = [r for r in fr_rows if r.get("trip_id", "") in kept.trip_ids]
        _write_csv(zout, _FREQUENCIES, fr_fields, fr_rows)

    # Everything else (feed_info.txt, fare_*.txt, …) — copy verbatim.
    for name in zin.namelist():
        if name in _FILTERABLE or name.endswith("/"):
            continue
        zout.writestr(name, zin.read(name))


def filter_to_cross_border(input_zip: Path, output_zip: Path) -> CrossBorderStats:
    """Filter a GTFS feed to only cross-border routes. Returns run stats.

    A route is kept iff its stops span 2+ UIC country prefixes (§ module
    docstring). The dependent files are cascade-filtered so the output is
    a valid, self-consistent GTFS containing only the kept routes' data.
    """
    input_zip = Path(input_zip)
    output_zip = Path(output_zip)
    stats = CrossBorderStats()

    with zipfile.ZipFile(input_zip) as zin:
        stop_fields, stop_rows = _read_csv(zin, _STOPS)
        stats.stops_total = len(stop_rows)
        stop_country, stop_parent = _stop_country_map(stop_rows)

        trip_fields, trip_rows = _read_csv(zin, _TRIPS)
        stats.trips_total = len(trip_rows)
        trip_route = {t.get("trip_id", ""): t.get("route_id", "") for t in trip_rows}

        cross_border_routes, route_countries = _classify_routes(
            zin, trip_route, stop_country, stats
        )

        route_fields, route_rows = _read_csv(zin, _ROUTES)
        stats.routes_total = len(route_rows)
        stats.routes_kept = len(cross_border_routes)
        for rid in cross_border_routes:
            # Sort by ISO name (not numeric prefix) so the label reads
            # alphabetically: "CH+IT", not "IT+CH".
            combo = "+".join(sorted(UIC_COUNTRY_NAMES.get(c, c) for c in route_countries[rid]))
            stats.country_combos[combo] = stats.country_combos.get(combo, 0) + 1

        kept = _KeptSets(
            route_ids=cross_border_routes,
            trip_ids={
                t.get("trip_id", "")
                for t in trip_rows
                if t.get("route_id", "") in cross_border_routes
            },
            service_ids=set(),
            shape_ids=set(),
            agency_ids=set(),
        )
        stats.trips_kept = len(kept.trip_ids)
        kept.service_ids = {
            t.get("service_id", "")
            for t in trip_rows
            if t.get("trip_id", "") in kept.trip_ids and t.get("service_id")
        }
        kept.shape_ids = {
            t.get("shape_id", "")
            for t in trip_rows
            if t.get("trip_id", "") in kept.trip_ids and t.get("shape_id")
        }
        kept.agency_ids = {
            r.get("agency_id", "")
            for r in route_rows
            if r.get("route_id", "") in cross_border_routes and r.get("agency_id")
        }

        st_fields, st_rows, kept_stop_ids = _collect_stop_times(zin, kept.trip_ids, stats)
        # Pull in parent stations of kept stops so OTP's station hierarchy
        # stays intact (a platform with no parent row would orphan). Set
        # union (not a mutate-during-iteration loop) keeps it clean.
        kept_stop_ids |= {stop_parent[sid] for sid in kept_stop_ids if sid in stop_parent}
        kept.stop_ids = kept_stop_ids
        stats.stops_kept = sum(1 for s in stop_rows if s.get("stop_id", "") in kept_stop_ids)

        output_zip.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(output_zip, "w", zipfile.ZIP_DEFLATED) as zout:
            _write_filtered_feed(
                zin,
                zout,
                kept=kept,
                route_fields=route_fields,
                route_rows=route_rows,
                trip_fields=trip_fields,
                trip_rows=trip_rows,
                stop_fields=stop_fields,
                stop_rows=stop_rows,
                st_fields=st_fields,
                st_rows=st_rows,
            )

    log.info("%s", stats.summary_line())
    return stats


def _main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Filter a GTFS feed to only cross-border routes (2+ UIC country prefixes)."
    )
    parser.add_argument("input", type=Path, help="input GTFS .zip")
    parser.add_argument("output", type=Path, help="output (filtered) GTFS .zip")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    stats = filter_to_cross_border(args.input, args.output)
    print(stats.summary_line())
    return 0 if stats.routes_kept > 0 else 1


if __name__ == "__main__":
    raise SystemExit(_main())
