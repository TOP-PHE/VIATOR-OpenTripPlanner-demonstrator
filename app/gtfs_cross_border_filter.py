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
docs/nap-ch-rail.md §3.3. It combines TWO selectors — both needed on a
full multimodal national feed like SBB's:

  - rail_only (default True): keep only rail `route_type`s, so lake boats,
    border buses, trams and funiculars can't masquerade as cross-border
    rail. Pass rail_only=False to consider every mode.
  - cross-border-ness: 2+ distinct UIC *country* prefixes among the stops.

Limitations:
  - Stops whose stop_id carries no parseable UIC — or whose 2-digit prefix
    is not a recognised UIC country (UIC_COUNTRY_NAMES) — contribute
    "unknown country" and don't count toward the 2+-country test. The
    whitelist is what stops SBB's internal codes for local/foreign stops
    (e.g. Evian = 1400001 -> "14") from faking a country. The flip side:
    a cross-border service whose foreign leg is encoded ONLY with such an
    internal code (no real foreign UIC anywhere on the route) won't be
    detected. In practice major international stations carry real UICs, and
    origin-country ownership takes those trains from the foreign NAP feed
    anyway. A coordinate fallback (resolve a stop's country from its
    lat/lon via the master_stations registry) is the planned follow-up for
    full recall — deliberately out of scope here to keep this module
    stdlib-only / DB-free.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import zipfile
from collections import defaultdict
from collections.abc import Callable, Iterator
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

# ISO-2 <-> UIC numeric country prefix (leading 2 digits of a UIC station
# code). This map doubles as the **cross-border whitelist**: a stop whose
# 2-digit prefix is not a key here is treated as "unknown country" and
# does NOT contribute to the 2+-country test.
#
# Why a whitelist (audit follow-up, SBB multimodal feed): SBB's national
# feed assigns *internal* 7-digit codes to some local / foreign stops
# (e.g. Evian = 1400001, leading "14"). "14" is not a UIC country, but the
# naive matcher counted it as a distinct one, so domestic Swiss services
# touching such a stop looked "cross-border" -- 322 bogus routes. Counting
# only real country codes kills those false positives.
#
# Europe-focused but generous, so a genuine cross-border service is never
# dropped for want of a country label. (Fixed: 73 is Greece, not Denmark
# -- Denmark is 86; the old map mislabelled 73 as DK.)
UIC_COUNTRY_NAMES: dict[str, str] = {
    "10": "FI",
    "20": "RU",
    "21": "BY",
    "22": "UA",
    "23": "MD",
    "24": "LT",
    "25": "LV",
    "26": "EE",
    "41": "AL",
    "50": "BA",
    "51": "PL",
    "52": "BG",
    "53": "RO",
    "54": "CZ",
    "55": "HU",
    "56": "SK",
    "60": "IE",
    "62": "ME",
    "65": "MK",
    "70": "GB",
    "71": "ES",
    "72": "RS",
    "73": "GR",
    "74": "SE",
    "75": "TR",
    "76": "NO",
    "78": "HR",
    "79": "SI",
    "80": "DE",
    "81": "AT",
    "82": "LU",
    "83": "IT",
    "84": "NL",
    "85": "CH",
    "86": "DK",
    "87": "FR",
    "88": "BE",
    "94": "PT",
}

# Reverse of UIC_COUNTRY_NAMES (ISO-2 -> UIC numeric prefix) for the
# home_country origin-ownership gate.
_UIC_PREFIX_BY_ISO: dict[str, str] = {iso: prefix for prefix, iso in UIC_COUNTRY_NAMES.items()}


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


def _country_of(stop_id: str | None) -> str | None:
    """Country prefix of a stop, but only when it is a *recognised* UIC
    country code (see UIC_COUNTRY_NAMES).

    Returns None for codes whose leading 2 digits aren't a real country --
    notably SBB's internal 7-digit codes for local/foreign stops (e.g.
    Evian = 1400001 -> "14"), which must not be mistaken for a country in
    the cross-border test. `country_prefix` stays a pure extractor; this
    is the validity gate the classifier uses.
    """
    p = country_prefix(stop_id)
    return p if p is not None and p in UIC_COUNTRY_NAMES else None


def _parse_coord(value: str | None) -> float | None:
    """Parse a GTFS lat/lon cell to float, or None when blank/malformed."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _country_of_stop(stop_row: dict[str, str]) -> str | None:
    """Country prefix for a stop: the UIC prefix in its stop_id, else a
    point-in-polygon lookup on its coordinates.

    The UIC path stays primary and exact (SNCF/SBB feeds key stops by UIC). The
    coordinate fallback only fires for stop_ids that carry **no UIC-shaped code
    at all** -- e.g. Renfe's 5-digit codes (`17000`, `37606`), where every stop
    would otherwise resolve to "unknown country" and no route looks cross-border.

    A code that *has* a UIC-shaped prefix which merely isn't a whitelisted country
    (notably SBB-internal 7-digit codes like Evian = `1400001` -> "14") is left
    "unknown" on purpose -- that is the #15 whitelist guard against internal codes
    faking a crossing, and we don't override it with a coordinate guess.
    """
    sid = stop_row.get("stop_id")
    prefix = _country_of(sid)
    if prefix is not None:
        return prefix
    if country_prefix(sid) is not None:
        # Has a UIC-shaped code, just not a whitelisted country → leave unknown.
        return None
    lat = _parse_coord(stop_row.get("stop_lat"))
    lon = _parse_coord(stop_row.get("stop_lon"))
    if lat is None or lon is None:
        return None
    # Lazy import: osm_geo imports this module, so a top-level import would cycle.
    from . import osm_geo

    iso = osm_geo.country_for_point(lat, lon)
    return _UIC_PREFIX_BY_ISO.get(iso) if iso else None


def _is_rail_route_type(route_type: str | None) -> bool:
    """True if a GTFS `route_type` denotes rail.

    Basic GTFS: 2 = Rail. Google extended types: 100-117 are the rail
    family (railway service, high-speed, long-distance, regional, ...).
    Everything else -- bus (3 / 700s), ferry (4 / 1000s), tram (0 / 900s),
    funicular (7 / 1400s), aerial lift (6 / 1300s), metro (1 / 400s) -- is
    excluded. That multimodal tail is exactly what dragged Lake Geneva
    boats and border buses into the SBB feed's "cross-border" set.
    """
    try:
        rt = int((route_type or "").strip())
    except ValueError:
        return False
    return rt == 2 or 100 <= rt <= 117


@dataclass
class CrossBorderStats:
    """Summary of a filter run — surfaced in logs and the operator UI."""

    routes_total: int = 0
    routes_rail: int = 0
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
    # v0.1.38 — origin-ownership home country (ISO-2). When set, only trips
    # departing this country were kept; None = both directions kept.
    home_country: str | None = None

    def summary_line(self) -> str:
        combos = ", ".join(f"{k}:{v}" for k, v in sorted(self.country_combos.items()))
        home = f" home={self.home_country}" if self.home_country else ""
        return (
            f"cross-border filter:{home} kept {self.routes_kept}/{self.routes_rail} rail routes "
            f"(of {self.routes_total} total), "
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
    """Build stop_id -> country prefix and stop_id -> parent_station maps.

    Country is the *recognised* UIC country prefix (`_country_of`), so an
    SBB-internal code like Evian's 1400001 resolves to None rather than a
    bogus "14" country. A platform-level stop may carry no UIC of its own
    but a parent that does (or vice versa); inherit a missing country from
    the parent.
    """
    stop_country: dict[str, str | None] = {}
    stop_parent: dict[str, str] = {}
    for s in stop_rows:
        sid = s.get("stop_id", "")
        stop_country[sid] = _country_of_stop(s)
        parent = (s.get("parent_station") or "").strip()
        if parent:
            stop_parent[sid] = parent
    for sid, parent in stop_parent.items():
        if stop_country.get(sid) is None and stop_country.get(parent):
            stop_country[sid] = stop_country[parent]
    return stop_country, stop_parent


def _stop_sequence(value: str | None) -> int:
    """Parse a GTFS stop_sequence to int; an unparseable value returns a
    large sentinel so that row is never mistaken for a trip's origin (the
    lowest stop_sequence)."""
    try:
        return int((value or "").strip())
    except ValueError:
        return 1 << 30


def _classify_routes(
    zin: zipfile.ZipFile,
    trip_route: dict[str, str],
    stop_country: dict[str, str | None],
    stats: CrossBorderStats,
) -> tuple[set[str], dict[str, set[str]], dict[str, str | None]]:
    """Pass 1 over stop_times. Returns:

    - the set of routes whose stops span 2+ UIC countries (cross-border),
    - the per-route country sets (for stats labelling),
    - per-trip *origin* country: the country of the trip's lowest
      stop_sequence stop, used by the home_country ownership gate.
    """
    route_countries: dict[str, set[str]] = defaultdict(set)
    # trip_id -> (lowest stop_sequence seen so far, country at that stop)
    trip_origin_seq: dict[str, tuple[int, str | None]] = {}
    if _STOP_TIMES in zin.namelist():
        gen = _stream_csv(zin, _STOP_TIMES)
        _ = next(gen)  # fieldnames (unused in pass 1)
        for row in gen:
            stats.stop_times_total += 1
            tid = row.get("trip_id", "")
            route_id = trip_route.get(tid)
            if route_id is None:
                continue
            ctry = stop_country.get(row.get("stop_id", ""))
            if ctry:
                route_countries[route_id].add(ctry)
            seq = _stop_sequence(row.get("stop_sequence"))
            prev = trip_origin_seq.get(tid)
            if prev is None or seq < prev[0]:
                trip_origin_seq[tid] = (seq, ctry)
    cross_border = {rid for rid, ctrys in route_countries.items() if len(ctrys) >= 2}
    trip_origin = {tid: ctry for tid, (_seq, ctry) in trip_origin_seq.items()}
    return cross_border, route_countries, trip_origin


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


def _maybe_write(
    zout: zipfile.ZipFile, name: str, fields: list[str], rows: list[dict[str, str]]
) -> None:
    """Write a member only if it has a header (i.e. existed in the input)."""
    if fields:
        _write_csv(zout, name, fields, rows)


def _read_filter_write(
    zin: zipfile.ZipFile,
    zout: zipfile.ZipFile,
    name: str,
    predicate: Callable[[dict[str, str]], bool],
) -> None:
    """Read a GTFS member, keep rows matching `predicate`, write it back.
    No-op if the member is absent. Collapses the repetitive read→filter→
    write block (SonarCloud S3776 cognitive-complexity)."""
    fields, rows = _read_csv(zin, name)
    if fields:
        _write_csv(zout, name, fields, [r for r in rows if predicate(r)])


def _write_agency(zin: zipfile.ZipFile, zout: zipfile.ZipFile, agency_ids: set[str]) -> None:
    """agency.txt — keep referenced agencies; copy whole when the feed has
    no agency_id column (single-agency feeds omit it)."""
    fields, rows = _read_csv(zin, _AGENCY)
    if not fields:
        return
    if "agency_id" in fields and agency_ids:
        rows = [a for a in rows if a.get("agency_id", "") in agency_ids]
    _write_csv(zout, _AGENCY, fields, rows)


def _copy_passthrough(zin: zipfile.ZipFile, zout: zipfile.ZipFile) -> None:
    """Copy any member we don't filter (feed_info.txt, fare_*.txt, …)."""
    for name in zin.namelist():
        if name in _FILTERABLE or name.endswith("/"):
            continue
        zout.writestr(name, zin.read(name))


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
    # Pre-loaded tables (already parsed in the caller).
    _maybe_write(
        zout,
        _ROUTES,
        route_fields,
        [r for r in route_rows if r.get("route_id", "") in kept.route_ids],
    )
    _maybe_write(
        zout, _TRIPS, trip_fields, [t for t in trip_rows if t.get("trip_id", "") in kept.trip_ids]
    )
    _maybe_write(zout, _STOP_TIMES, st_fields, st_rows)
    _maybe_write(
        zout, _STOPS, stop_fields, [s for s in stop_rows if s.get("stop_id", "") in kept.stop_ids]
    )
    # Members re-read from the input and filtered on the fly.
    _write_agency(zin, zout, kept.agency_ids)
    _read_filter_write(zin, zout, _CALENDAR, lambda r: r.get("service_id", "") in kept.service_ids)
    _read_filter_write(
        zin, zout, _CALENDAR_DATES, lambda r: r.get("service_id", "") in kept.service_ids
    )
    _read_filter_write(zin, zout, _SHAPES, lambda r: r.get("shape_id", "") in kept.shape_ids)
    _read_filter_write(
        zin,
        zout,
        _TRANSFERS,
        lambda r: (
            r.get("from_stop_id", "") in kept.stop_ids
            and r.get("to_stop_id", "") in kept.stop_ids
            # Trip-to-trip transfers (in-seat / constrained — GTFS transfer_type
            # 4/5) carry from_trip_id/to_trip_id. Drop the row if it references a
            # trip we filtered out, even when both stops survived — otherwise
            # OTP's strict GTFS reader aborts the whole build with
            # EntityReferenceNotFoundException on the dangling trip. Absent trip
            # fields ⇒ a plain stop-to-stop transfer, which is kept.
            and (not r.get("from_trip_id") or r.get("from_trip_id", "") in kept.trip_ids)
            and (not r.get("to_trip_id") or r.get("to_trip_id", "") in kept.trip_ids)
        ),
    )
    _read_filter_write(zin, zout, _FREQUENCIES, lambda r: r.get("trip_id", "") in kept.trip_ids)
    _copy_passthrough(zin, zout)


def filter_to_cross_border(
    input_zip: Path,
    output_zip: Path,
    *,
    rail_only: bool = True,
    home_country: str | None = None,
) -> CrossBorderStats:
    """Filter a GTFS feed to only cross-border routes. Returns run stats.

    A route is kept iff (a) it is a rail route -- when ``rail_only`` (the
    default) -- and (b) its stops span 2+ recognised UIC country prefixes
    (see module docstring). The dependent files are cascade-filtered so the
    output is a valid, self-consistent GTFS containing only the kept
    routes' data.

    Set ``rail_only=False`` to classify every mode -- useful only for feeds
    you already know are rail-only.

    ``home_country`` (ISO-2, e.g. ``"FR"``) enables *origin-country
    ownership* for cross-NAP federation: only trips that **depart** that
    country (their lowest-stop_sequence stop is in it) are kept, so the same
    physical train isn't loaded twice when several national feeds are
    federated. Each direction is owned by its departure country -- run the
    SNCF feed with ``home_country="FR"`` and the SBB feed with ``"CH"`` and
    Paris->Geneve comes from one feed, Geneve->Paris from the other, with no
    overlap. ``None`` (default) keeps both directions.
    """
    input_zip = Path(input_zip)
    output_zip = Path(output_zip)
    stats = CrossBorderStats()

    home_prefix: str | None = None
    if home_country:
        iso = home_country.strip().upper()
        home_prefix = _UIC_PREFIX_BY_ISO.get(iso)
        if home_prefix is None:
            raise ValueError(
                f"home_country={iso!r} is not a known UIC country ISO; "
                f"valid: {sorted(_UIC_PREFIX_BY_ISO)}"
            )
        stats.home_country = iso

    with zipfile.ZipFile(input_zip) as zin:
        stop_fields, stop_rows = _read_csv(zin, _STOPS)
        stats.stops_total = len(stop_rows)
        stop_country, stop_parent = _stop_country_map(stop_rows)

        # Read routes up-front so classification can be restricted to rail.
        route_fields, route_rows = _read_csv(zin, _ROUTES)
        stats.routes_total = len(route_rows)
        if rail_only:
            rail_route_ids = {
                r.get("route_id", "")
                for r in route_rows
                if _is_rail_route_type(r.get("route_type"))
            }
        else:
            rail_route_ids = {r.get("route_id", "") for r in route_rows}
        stats.routes_rail = len(rail_route_ids)

        trip_fields, trip_rows = _read_csv(zin, _TRIPS)
        stats.trips_total = len(trip_rows)
        # Only rail routes' trips feed the cross-border classification; a
        # non-rail trip maps to no route here and is skipped in pass 1.
        trip_route = {
            t.get("trip_id", ""): t.get("route_id", "")
            for t in trip_rows
            if t.get("route_id", "") in rail_route_ids
        }

        cross_border_routes, route_countries, trip_origin = _classify_routes(
            zin, trip_route, stop_country, stats
        )

        # Trip-level keep set: on a cross-border route and -- when
        # home_country is set -- *departing* the home country (origin gate).
        kept_trip_ids = {
            t.get("trip_id", "")
            for t in trip_rows
            if t.get("route_id", "") in cross_border_routes
            and (home_prefix is None or trip_origin.get(t.get("trip_id", "")) == home_prefix)
        }
        # A route survives only if at least one of its trips did; the
        # home_country gate can empty a route entirely.
        kept_route_ids = {
            t.get("route_id", "") for t in trip_rows if t.get("trip_id", "") in kept_trip_ids
        }

        stats.routes_kept = len(kept_route_ids)
        for rid in kept_route_ids:
            # Sort by ISO name (not numeric prefix) so the label reads
            # alphabetically: "CH+IT", not "IT+CH".
            combo = "+".join(sorted(UIC_COUNTRY_NAMES.get(c, c) for c in route_countries[rid]))
            stats.country_combos[combo] = stats.country_combos.get(combo, 0) + 1

        kept = _KeptSets(
            route_ids=kept_route_ids,
            trip_ids=kept_trip_ids,
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
            if r.get("route_id", "") in kept.route_ids and r.get("agency_id")
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
    parser.add_argument(
        "--all-modes",
        action="store_true",
        help="consider every route_type, not just rail (default: rail only)",
    )
    parser.add_argument(
        "--home-country",
        metavar="ISO2",
        default=None,
        help="origin-ownership: keep only trips departing this ISO-2 country "
        "(e.g. FR, CH) so federated national feeds don't duplicate trains",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    stats = filter_to_cross_border(
        args.input, args.output, rail_only=not args.all_modes, home_country=args.home_country
    )
    print(stats.summary_line())
    return 0 if stats.routes_kept > 0 else 1


if __name__ == "__main__":
    raise SystemExit(_main())
