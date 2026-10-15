"""The rail stations of a GTFS archive, for the stop extract (MSMM 22.2).

Streams: every file is read row by row from the zip, never unpacked to disk
and never held whole. What is kept in memory is bounded by the rail part of
the feed: one entry per rail route, per rail trip, and per stop or station at
which rail calls, never one per row of `stop_times.txt`. What is read is
bounded too: a record of at most `RECORD_MAX` characters and `FIELDS_MAX`
fields, ids of at most `ID_MAX` characters, the unpacked bytes counted
(`station_extract_common.open_member`); past a bound the archive is refused.

`stop_times.txt`, by far the largest file (some 100 million rows in a
national feed), is read by column position (`_Columns`): the same csv
records as every other file, but no dictionary per row and only its four
columns looked at, about four times faster; the rows of other files are
read as dictionaries (`_rows`). Both see the same records and the same
bounds, and give the same values: a column named twice in a header counts
by its last place within the record, as a dictionary built from the record
has it.

Order of reading:

1. `agency.txt`: agency id -> name (the operators, 22.2 point 1);
2. `routes.txt`: rail routes only, by `route_type` (22.2, point 2);
3. `trips.txt`: the trips of those routes;
4. `stop_times.txt`: the calls of those trips, counted per stop;
5. `stops.txt` twice: first which station each called stop belongs to (its
   `parent_station`, or itself when it has none or names one stops.txt does
   not define), then the stations' names, positions and codes, with the codes
   of their children (entrances and generic nodes left out); a station
   without a position takes its first child's (22.2, points 1 and 3).
"""

from __future__ import annotations

import collections
import contextlib
import csv
import io
import re
import zipfile
from collections.abc import Iterator

from .station_extract_common import (
    MEGABYTE,
    RAIL_MAIN,
    RAIL_SUBURBAN,
    ArchiveError,
    Budget,
    Station,
    check_declared,
    coordinate,
    members,
    new_station,
    open_member,
)

_STOPS = "stops.txt"
_REQUIRED = (_STOPS, "routes.txt", "trips.txt", "stop_times.txt")
# The columns whose values are a stop's codes (MSMM 22.2, point 1).
_CODE_COLUMNS = ("stop_id", "stop_code", "platform_code")
# location_type 2 (entrance) and 3 (generic node): no codes of the station.
_NOT_A_PLATFORM = frozenset({"2", "3"})
# The bounds of what is read (review of pull request 4).
RECORD_MAX = MEGABYTE
FIELDS_MAX = 200
ID_MAX = 255
_ID_COLUMNS = ("stop_id", "trip_id", "route_id", "agency_id", "parent_station")
# The distinct stops at which rail calls that are kept, platforms included.
CALLED_STOPS_MAX = 1_000_000


def provider_operator(provider_id: str) -> str:
    """The operator item of a feed that names none (MSMM needs one): a
    marker no agency name or NeTEx reference takes by chance."""
    return f"VIATOR:provider:{provider_id}"


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


# The physical lines as `readline` of a text stream opened with `newline=""`
# returns them: ended by `\r\n`, `\r` or `\n`, the ending kept. Only the
# endings are searched (each match takes constant time), never a line itself:
# a long text without an ending costs one pass, not one per character.
_ENDING = re.compile("\r\n|\r|\n")
# `str.splitlines` splits there too, and also at these, faster: it is used on
# a block that holds none of them.
_OTHER_BREAKS = re.compile("[\x0b\x0c\x1c-\x1e\x85\u2028\u2029]")
_TEXT_BLOCK = 256 * 1024  # characters read at a time
_TOO_LONG = "a GTFS record longer than 1 MiB"


def _lines(text: io.TextIOWrapper, size: list[int]) -> Iterator[str]:
    """The physical lines, bounded; `size[0]` counts the characters of the
    record being built, which the reader sets back to 0 after each record,
    so that no record can grow past `RECORD_MAX` before the csv module has
    built it (a quoted field may span lines).

    The text is read a block at a time and split in C. A block without a
    quote character, whose lines all fit the bound, and that begins a record
    (`size[0]` is 0) is handed over as it is: without a quote every line is
    a whole record, so each line's bound is the record's. Any other block
    goes a line at a time with the count. The lines, and when a record is
    refused, are those of `readline(RECORD_MAX + 1)`."""
    carry = ""
    while True:
        block = text.read(_TEXT_BLOCK)
        if block:
            block = carry + block
            lines, carry = _split(block)
        elif carry:
            block, lines, carry = carry, [carry], ""
        else:
            return
        if size[0] == 0 and '"' not in block and max(map(len, lines), default=0) <= RECORD_MAX:
            yield from lines
        else:
            yield from _counted(lines, size)
        if len(carry) > RECORD_MAX:
            raise ArchiveError("input_too_large", _TOO_LONG)


def _split(block: str) -> tuple[list[str], str]:
    """The whole physical lines of `block`, and what is left after them (a
    line not ended yet, or a last `\r` that may be the first half of a
    `\r\n`)."""
    if _OTHER_BREAKS.search(block) is None:
        lines = block.splitlines(keepends=True)
        carry = lines.pop() if lines and not lines[-1].endswith(("\n", "\r")) else ""
    else:
        lines, start = [], 0
        for ending in _ENDING.finditer(block):
            lines.append(block[start : ending.end()])
            start = ending.end()
        carry = block[start:]
    if not carry and lines and lines[-1].endswith("\r"):
        carry = lines.pop()
    return lines, carry


def _counted(lines: list[str], size: list[int]) -> Iterator[str]:
    """The lines one at a time, each added to the record's size."""
    for line in lines:
        size[0] += len(line)
        if size[0] > RECORD_MAX:
            raise ArchiveError("input_too_large", _TOO_LONG)
        yield line


class _Columns:
    """Where the columns of a header are, for records of any length: the
    value of a column is the one a dictionary built from the record (zip of
    header and record) would hold, its last place within the record."""

    def __init__(self, header: list[str]) -> None:
        self.width = len(header)
        self._places: dict[str, list[int]] = {}
        for place, name in enumerate(header):
            self._places.setdefault(name, []).append(place)
        self._ids = {name: self._places[name] for name in _ID_COLUMNS if name in self._places}
        self.full_ids = self.id_places(self.width)

    def place(self, name: str, length: int) -> int | None:
        """The place of `name` in a record of `length` fields, or None."""
        for place in reversed(self._places.get(name, ())):
            if place < length:
                return place
        return None

    def id_places(self, length: int) -> list[int]:
        found = (self.place(name, length) for name in self._ids)
        return [place for place in found if place is not None]


@contextlib.contextmanager
def _csv_member(
    zf: zipfile.ZipFile, member: str, budget: Budget
) -> Iterator[tuple[list[str], Iterator[list[str]], list[int]]]:
    """The stripped header of one CSV member, the csv reader of its records
    and the record size its caller sets back to 0 after each record. A byte
    order mark and padded headers or cells are tolerated."""
    with open_member(zf, member, budget) as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8-sig", errors="replace", newline="")
        size = [0]
        reader = csv.reader(_lines(text, size))
        header = next(reader, [])
        if len(header) > FIELDS_MAX:
            raise ArchiveError("input_too_large", "a GTFS header with too many fields")
        size[0] = 0
        yield [name.strip() for name in header], reader, size


def _check(record: list[str], columns: _Columns) -> None:
    """The field count and the length of the ids (stripped) of a record."""
    length = len(record)
    if length > FIELDS_MAX:
        raise ArchiveError("input_too_large", "a GTFS record with too many fields")
    places = columns.full_ids if length == columns.width else columns.id_places(length)
    for place in places:
        value = record[place]
        if len(value) > ID_MAX and len(value.strip()) > ID_MAX:
            raise ArchiveError("input_too_large", "a GTFS id longer than 255 characters")


def _rows(zf: zipfile.ZipFile, member: str, budget: Budget) -> Iterator[dict[str, str]]:
    """The non-empty rows of one CSV member as {column: stripped value},
    streamed and checked against the bounds."""
    with _csv_member(zf, member, budget) as (header, reader, size):
        columns = _Columns(header)
        for record in reader:
            size[0] = 0
            if record:
                _check(record, columns)
                yield {name: value.strip() for name, value in zip(header, record, strict=False)}


def _agencies(
    zf: zipfile.ZipFile, files: dict[str, str], budget: Budget
) -> tuple[dict[str, str], str]:
    """Agency id -> name, and the name of the feed's only agency ('' when the
    feed names none or several)."""
    names: dict[str, str] = {}
    member = files.get("agency.txt")
    if member is None:
        return names, ""
    for row in _rows(zf, member, budget):
        name = row.get("agency_name") or row.get("agency_id") or ""
        names[row.get("agency_id", "")] = name
    only = next(iter(names.values())) if len(names) == 1 else ""
    return names, only


def _rail_profiles(
    zf: zipfile.ZipFile, files: dict[str, str], provider_id: str, budget: Budget
) -> tuple[dict[str, int], list[tuple[str, str]]]:
    """Rail route id -> index of its (mode, operator) profile."""
    agencies, only_agency = _agencies(zf, files, budget)
    profiles: list[tuple[str, str]] = []
    index: dict[tuple[str, str], int] = {}
    routes: dict[str, int] = {}
    for row in _rows(zf, files["routes.txt"], budget):
        mode = rail_mode(row.get("route_type", ""))
        if mode is None:
            continue
        agency_id = row.get("agency_id", "")
        # The feed's only agency when the route names none; the provider's
        # marker when the route has no agency at all (MSMM needs one item).
        operator = (
            agencies.get(agency_id) or only_agency or agency_id or provider_operator(provider_id)
        )
        profile = (mode, operator)
        routes[row.get("route_id", "")] = index.setdefault(profile, len(profiles))
        if len(index) > len(profiles):
            profiles.append(profile)
    return routes, profiles


def _rail_trips(
    zf: zipfile.ZipFile, files: dict[str, str], routes: dict[str, int], budget: Budget
) -> dict[str, int]:
    trips: dict[str, int] = {}
    for row in _rows(zf, files["trips.txt"], budget):
        profile = routes.get(row.get("route_id", ""))
        if profile is not None:
            trips[row.get("trip_id", "")] = profile
    return trips


def _calls(
    zf: zipfile.ZipFile, files: dict[str, str], trips: dict[str, int], budget: Budget
) -> dict[str, collections.Counter[int]]:
    """Stop id -> rail calls by profile. A stop where a trip neither picks up
    nor drops off (both types 1) is passed, not called at."""
    calls: dict[str, collections.Counter[int]] = {}
    with _csv_member(zf, files["stop_times.txt"], budget) as (header, reader, size):
        columns = _Columns(header)
        width, ids = columns.width, columns.full_ids
        full = _call_places(columns, width)
        for record in reader:
            size[0] = 0
            if not record:
                continue
            places = full
            if len(record) != width:
                _check(record, columns)
                places = _call_places(columns, len(record))
            elif any(len(record[place]) > ID_MAX for place in ids):
                _check(record, columns)  # inlined for the common case: only a long one
            # Most rows are not rail: their other columns are not looked at.
            profile = trips.get(_value(record, places[0]))
            if profile is not None:
                _add_call(calls, record, places, profile)
    return calls


def _add_call(
    calls: dict[str, collections.Counter[int]],
    record: list[str],
    places: list[int | None],
    profile: int,
) -> None:
    """A rail trip's row: a call at its stop, unless the trip passes it
    without stopping (pick-up and drop-off both type 1) or names none."""
    stop_id = _value(record, places[1])
    if _value(record, places[2]) == "1" and _value(record, places[3]) == "1":
        return
    if not stop_id:
        return
    if stop_id not in calls and len(calls) >= CALLED_STOPS_MAX:
        raise ArchiveError("too_many_rows", "more called stops than VIATOR keeps")
    calls.setdefault(stop_id, collections.Counter())[profile] += 1


_CALL_COLUMNS = ("trip_id", "stop_id", "pickup_type", "drop_off_type")


def _call_places(columns: _Columns, length: int) -> list[int | None]:
    return [columns.place(name, length) for name in _CALL_COLUMNS]


def _value(record: list[str], place: int | None) -> str:
    return record[place].strip() if place is not None else ""


def _stations(
    zf: zipfile.ZipFile,
    stops: str,
    calls: dict[str, collections.Counter[int]],
    profiles: list[tuple[str, str]],
    budget: Budget,
    no_parent: frozenset[str] = frozenset(),
) -> dict[str, Station]:
    """First pass over stops.txt: the station of each called stop (its
    `parent_station`, or itself, also when its parent is in `no_parent`),
    with the calls, modes and operators."""
    station_of: dict[str, str] = {}
    for row in _rows(zf, stops, budget):
        stop_id = row.get("stop_id", "")
        if stop_id in calls:
            parent = row.get("parent_station", "")
            station_of[stop_id] = parent if parent and parent not in no_parent else stop_id
    stations: dict[str, Station] = {}
    for stop_id, key in station_of.items():  # a stop stops.txt does not define is skipped
        station = new_station(stations, key)
        for profile, count in calls[stop_id].items():
            mode, operator = profiles[profile]
            station.calls[mode] += count
            station.operators.add(operator)
    return stations


def _add_details(
    zf: zipfile.ZipFile, stops: str, stations: dict[str, Station], budget: Budget
) -> set[str]:
    """Second pass: the stations' names, positions and codes, then the codes
    of their children, after the station's own; a station without a position
    takes its first child's. Returns the stations stops.txt defines."""
    children: dict[str, Station] = {}  # per station: its children's codes and first position
    defined: set[str] = set()
    for row in _rows(zf, stops, budget):
        stop_id = row.get("stop_id", "")
        lat, lon = coordinate(row.get("stop_lat")), coordinate(row.get("stop_lon"))
        own = stations.get(stop_id)
        if own is not None:
            defined.add(stop_id)
            own.place(row.get("stop_name", ""), lat, lon)
            _add_codes(own, row)
        parent = row.get("parent_station", "")
        if parent in stations and parent != stop_id:
            child = children.setdefault(parent, Station(key=parent))
            child.place("", lat, lon)
            if row.get("location_type", "") not in _NOT_A_PLATFORM:
                _add_codes(child, row)
    for key, child in children.items():
        for name, value in child.code_pairs():
            stations[key].add_code(name, value)
        stations[key].codes_over_cap += child.codes_over_cap
        stations[key].place("", child.lat, child.lon)
    return defined


def _add_codes(station: Station, row: dict[str, str]) -> None:
    for name in _CODE_COLUMNS:
        station.add_code(name, row.get(name))


def read_gtfs(zf: zipfile.ZipFile, provider_id: str) -> dict[str, Station]:
    """The rail stations of the GTFS archive `zf`, by station key."""
    files = members(zf)
    missing = [name for name in _REQUIRED if name not in files]
    if missing:
        raise ArchiveError("bad_archive", f"GTFS without {', '.join(missing)}")
    check_declared(zf, [files[name] for name in ("agency.txt", *_REQUIRED) if name in files])
    budget = Budget()
    routes, profiles = _rail_profiles(zf, files, provider_id, budget)
    trips = _rail_trips(zf, files, routes, budget) if routes else {}
    del routes
    calls = _calls(zf, files, trips, budget) if trips else {}
    del trips
    if not calls:
        return {}
    stations = _stations(zf, files[_STOPS], calls, profiles, budget)
    defined = _add_details(zf, files[_STOPS], stations, budget)
    undefined = frozenset(stations) - defined
    if undefined:
        # A parent_station that stops.txt does not define: each of its
        # children is its own station (22.2, point 1).
        stations = _stations(zf, files[_STOPS], calls, profiles, budget, undefined)
        _add_details(zf, files[_STOPS], stations, budget)
    return stations
