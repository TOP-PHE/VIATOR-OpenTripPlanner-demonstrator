"""The rail stations of a GTFS archive, for the stop extract (MSMM 22.2).

Streams: every file is read row by row from the zip, never unpacked to disk
and never held whole. What is kept in memory is bounded by the rail part of
the feed: one entry per rail route, per rail trip, and per stop or station at
which rail calls, never one per row of `stop_times.txt`.

Order of reading:

1. `agency.txt`: agency id -> name (the operators, 22.2 point 1);
2. `routes.txt`: rail routes only, by `route_type` (22.2, point 2);
3. `trips.txt`: the trips of those routes;
4. `stop_times.txt`: the calls of those trips, counted per stop;
5. `stops.txt` twice: first which station each called stop belongs to (its
   `parent_station`, or itself), then the stations' names, positions and
   codes, with the codes of all their children (22.2, points 1 and 3).
"""

from __future__ import annotations

import collections
import csv
import io
import zipfile
from collections.abc import Iterator

from .station_extract_common import (
    RAIL_MAIN,
    RAIL_SUBURBAN,
    ArchiveError,
    Station,
    coordinate,
    members,
)

_STOPS = "stops.txt"
_REQUIRED = (_STOPS, "routes.txt", "trips.txt", "stop_times.txt")
# The columns whose values are a stop's codes (MSMM 22.2, point 1).
_CODE_COLUMNS = ("stop_id", "stop_code", "platform_code")


def rail_mode(route_type: str) -> str | None:
    """The MSMM mode of a GTFS `route_type`, or None when it is not rail
    (22.2, point 2): 2 and 100 to 117 are rail, 109, 300, 400, 403 and 404
    suburban rail. VIATOR's `gtfs_cross_border_filter._is_rail_route_type` is
    deliberately not reused: it drops the S-Bahn types 400, 403 and 404. A rail
    replacement bus (714) is not rail."""
    try:
        value = int(route_type.strip())
    except ValueError:
        return None
    if value in (109, 300, 400, 403, 404):
        return RAIL_SUBURBAN
    if value == 2 or 100 <= value <= 117:
        return RAIL_MAIN
    return None


def _rows(zf: zipfile.ZipFile, member: str) -> Iterator[dict[str, str]]:
    """The rows of one CSV member as {column: stripped value}, streamed. A
    byte order mark and padded headers or cells are tolerated."""
    with zf.open(member) as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8-sig", errors="replace", newline="")
        reader = csv.reader(text)
        header = [name.strip() for name in next(reader, [])]
        for record in reader:
            if record:
                yield {name: value.strip() for name, value in zip(header, record, strict=False)}


def _agencies(zf: zipfile.ZipFile, files: dict[str, str]) -> tuple[dict[str, str], str]:
    """Agency id -> name, and the name of the feed's only agency ('' when the
    feed names none or several)."""
    names: dict[str, str] = {}
    member = files.get("agency.txt")
    if member is None:
        return names, ""
    for row in _rows(zf, member):
        name = row.get("agency_name") or row.get("agency_id") or ""
        names[row.get("agency_id", "")] = name
    only = next(iter(names.values())) if len(names) == 1 else ""
    return names, only


def _rail_profiles(
    zf: zipfile.ZipFile, files: dict[str, str], provider_id: str
) -> tuple[dict[str, int], list[tuple[str, str]]]:
    """Rail route id -> index of its (mode, operator) profile."""
    agencies, only_agency = _agencies(zf, files)
    profiles: list[tuple[str, str]] = []
    index: dict[tuple[str, str], int] = {}
    routes: dict[str, int] = {}
    for row in _rows(zf, files["routes.txt"]):
        mode = rail_mode(row.get("route_type", ""))
        if mode is None:
            continue
        agency_id = row.get("agency_id", "")
        # The feed's only agency when the route names none; the provider
        # itself when the feed names no agency at all (MSMM needs one item).
        operator = agencies.get(agency_id) or only_agency or agency_id or provider_id
        profile = (mode, operator)
        routes[row.get("route_id", "")] = index.setdefault(profile, len(profiles))
        if len(index) > len(profiles):
            profiles.append(profile)
    return routes, profiles


def _rail_trips(
    zf: zipfile.ZipFile, files: dict[str, str], routes: dict[str, int]
) -> dict[str, int]:
    trips: dict[str, int] = {}
    for row in _rows(zf, files["trips.txt"]):
        profile = routes.get(row.get("route_id", ""))
        if profile is not None:
            trips[row.get("trip_id", "")] = profile
    return trips


def _calls(
    zf: zipfile.ZipFile, files: dict[str, str], trips: dict[str, int]
) -> dict[str, collections.Counter[int]]:
    """Stop id -> rail calls by profile. A stop where a trip neither picks up
    nor drops off (both types 1) is passed, not called at."""
    calls: dict[str, collections.Counter[int]] = {}
    for row in _rows(zf, files["stop_times.txt"]):
        profile = trips.get(row.get("trip_id", ""))
        if profile is None:
            continue
        if row.get("pickup_type") == "1" and row.get("drop_off_type") == "1":
            continue
        stop_id = row.get("stop_id", "")
        if stop_id:
            calls.setdefault(stop_id, collections.Counter())[profile] += 1
    return calls


def _stations(
    zf: zipfile.ZipFile,
    stops: str,
    calls: dict[str, collections.Counter[int]],
    profiles: list[tuple[str, str]],
) -> dict[str, Station]:
    """First pass over stops.txt: the station of each called stop (its
    `parent_station`, or itself), with the calls, modes and operators."""
    station_of: dict[str, str] = {}
    for row in _rows(zf, stops):
        stop_id = row.get("stop_id", "")
        if stop_id in calls:
            station_of[stop_id] = row.get("parent_station") or stop_id
    stations: dict[str, Station] = {}
    for stop_id, key in station_of.items():  # a stop stops.txt does not define is skipped
        station = stations.setdefault(key, Station(key=key))
        for profile, count in calls[stop_id].items():
            mode, operator = profiles[profile]
            station.calls[mode] += count
            station.operators.add(operator)
    return stations


def _add_details(zf: zipfile.ZipFile, stops: str, stations: dict[str, Station]) -> None:
    """Second pass: the stations' names, positions and codes, then the codes
    of their children, after the station's own."""
    children: dict[str, list[tuple[str, str]]] = collections.defaultdict(list)
    for row in _rows(zf, stops):
        stop_id = row.get("stop_id", "")
        own = stations.get(stop_id)
        if own is not None:
            own.place(
                row.get("stop_name", ""),
                coordinate(row.get("stop_lat")),
                coordinate(row.get("stop_lon")),
            )
            for name in _CODE_COLUMNS:
                own.add_code(name, row.get(name))
        parent = row.get("parent_station", "")
        if parent in stations and parent != stop_id:
            children[parent].extend((name, row.get(name, "")) for name in _CODE_COLUMNS)
    for key, codes in children.items():
        for name, value in codes:
            stations[key].add_code(name, value)


def read_gtfs(zf: zipfile.ZipFile, provider_id: str) -> dict[str, Station]:
    """The rail stations of the GTFS archive `zf`, by station key."""
    files = members(zf)
    missing = [name for name in _REQUIRED if name not in files]
    if missing:
        raise ArchiveError("bad_archive", f"GTFS without {', '.join(missing)}")
    routes, profiles = _rail_profiles(zf, files, provider_id)
    trips = _rail_trips(zf, files, routes) if routes else {}
    del routes
    calls = _calls(zf, files, trips) if trips else {}
    del trips
    if not calls:
        return {}
    stations = _stations(zf, files[_STOPS], calls, profiles)
    del calls
    _add_details(zf, files[_STOPS], stations)
    return stations
