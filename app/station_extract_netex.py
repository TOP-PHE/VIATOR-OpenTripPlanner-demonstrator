"""The rail stations of a NeTEx archive (EPIP or Nordic), for the stop extract.

MSMM 22.2: one row per StopPlace (the root one when `ParentSiteRef` is used)
at which a rail ServiceJourney calls. Rail is `TransportMode` rail on the
ServiceJourney or on its Line; a journey that carries no mode at all, on
itself or on its Line, counts at a StopPlace whose own `TransportMode` is rail
or whose `StopPlaceType` is `railStation` (22.2, point 2: the railStation
fallback; the Belgian export, for one, gives its journeys no mode).

The archive is streamed: every XML member is read with `iterparse` and each
element is dropped as soon as it has been read, so memory holds one
StopPlace, Line or journey at a time plus the small maps below, never a file.
The references between frames can point forwards and across files, so the
members are read in passes, each keeping only what the next one needs:

1. Lines (mode, operator and authority) and Routes (their Line);
2. ServiceJourneys, grouped by pattern, Line, mode and operator, the
   non-rail ones dropped as soon as their mode is known;
3. the journey patterns those journeys use: their scheduled stop points;
4. the PassengerStopAssignments of those points: the StopPlace or Quay;
   every StopPlace's `ParentSiteRef`, the Quays' StopPlace and which
   StopPlaces call themselves rail;
5. the details (name, position, codes) of the stations and their children.

A member that holds none of a pass's elements is not read again (the first
pass notes which elements each member holds).
"""

from __future__ import annotations

import collections
import math
import zipfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import IO

# Only the type: every parse goes through defusedxml.
from xml.etree.ElementTree import Element  # nosec B405

from defusedxml.ElementTree import ParseError, iterparse

from .station_extract_common import (
    RAIL_MAIN,
    RAIL_SUBURBAN,
    ArchiveError,
    Station,
    coordinate,
)

# A rail submode that MSMM's offline chain reads as suburban rail (its
# `nap_modes.py`, `_SUBURBAN_SUBMODES`), compared in small letters.
_SUBURBAN_SUBMODES = frozenset(
    {"suburbanrailway", "urbanrailway", "citytrain", "regionalrail", "s-bahn"}
)
# A mode not yet known: the journey's StopPlace decides (the railStation
# fallback).
_UNKNOWN = "?"
# How deep a `ParentSiteRef` chain is followed.
_MAX_DEPTH = 16

# The elements each pass reads.
_LINES = frozenset({"Line", "Route"})
_JOURNEYS = frozenset({"ServiceJourney"})
_PATTERNS = frozenset({"ServiceJourneyPattern", "JourneyPattern"})
_PLACES = frozenset({"PassengerStopAssignment", "StopPlace"})
_DETAILS = frozenset({"StopPlace"})
_ALL = _LINES | _JOURNEYS | _PATTERNS | _PLACES


def _local(tag: str) -> str:
    return tag.rpartition("}")[2]


def _child(element: Element, name: str) -> Element | None:
    for child in element:
        if _local(child.tag) == name:
            return child
    return None


def _text(element: Element, name: str) -> str:
    child = _child(element, name)
    return (child.text or "").strip() if child is not None else ""


def _ref(element: Element, *names: str) -> str:
    for name in names:
        child = _child(element, name)
        if child is not None and child.get("ref"):
            return str(child.get("ref")).strip()
    return ""


def _submode(element: Element) -> str:
    """The value of `TransportSubmode`, whatever its inner element."""
    holder = _child(element, "TransportSubmode")
    if holder is None:
        return ""
    for inner in holder:
        if inner.text and inner.text.strip():
            return inner.text.strip()
    return (holder.text or "").strip()


def _mode_of(mode: str, submode: str) -> str | None:
    """The MSMM mode of a NeTEx mode, None when it is not rail, `_UNKNOWN`
    when no mode is given."""
    if not mode:
        return _UNKNOWN
    if mode != "rail":
        return None
    return RAIL_SUBURBAN if submode.lower() in _SUBURBAN_SUBMODES else RAIL_MAIN


# ── Streaming ────────────────────────────────────────────────────────────


def _walk(source: IO[bytes], targets: frozenset[str], seen: set[str]) -> Iterator[Element]:
    """The outermost elements of `targets` in document order, each whole.

    Every other element is removed from its parent once read, and a target
    once the caller has had it, so memory holds the open path and the
    current target only. The local names of `_ALL` met are added to `seen`.
    """
    stack: list[Element] = []
    inside = 0
    for event, element in iterparse(source, events=("start", "end")):
        local = _local(element.tag)
        is_target = local in targets
        if event == "start":
            stack.append(element)
            inside += is_target
            continue
        stack.pop()
        if local in _ALL:
            seen.add(local)
        inside -= is_target
        if inside == 0:
            if is_target:
                yield element
            _drop(element, stack)


def _drop(element: Element, stack: list[Element]) -> None:
    """Free a read element: its content, and its place in its parent."""
    element.clear()
    if stack:
        stack[-1].remove(element)


class _Archive:
    """The XML members of a NeTEx zip, and which elements each holds."""

    def __init__(self, zf: zipfile.ZipFile) -> None:
        self.zf = zf
        self.names = [
            info.filename
            for info in zf.infolist()
            if not info.is_dir() and info.filename.lower().endswith(".xml")
        ]
        if not self.names:
            raise ArchiveError("bad_archive", "NeTEx archive without an XML file")
        self.holds: dict[str, set[str]] = {}

    def walk(self, targets: frozenset[str]) -> Iterator[Element]:
        for name in self.names:
            known = self.holds.get(name)
            if known is not None and not known & targets:
                continue
            seen: set[str] = set()
            try:
                with self.zf.open(name) as source:
                    yield from _walk(source, targets, seen)
            except ParseError as exc:
                raise ArchiveError("bad_archive", f"XML not well-formed: {exc}") from None
            self.holds[name] = seen


# ── Pass 1 and 2: lines and journeys ──────────────────────────────────────


@dataclass(frozen=True)
class _LineInfo:
    mode: str
    submode: str
    operators: tuple[str, ...]


def _operators(element: Element) -> tuple[str, ...]:
    return tuple(
        ref for ref in (_ref(element, "OperatorRef"), _ref(element, "AuthorityRef")) if ref
    )


def _read_lines(archive: _Archive) -> tuple[dict[str, _LineInfo], dict[str, str]]:
    lines: dict[str, _LineInfo] = {}
    routes: dict[str, str] = {}
    for element in archive.walk(_LINES):
        ident = element.get("id")
        if not ident:
            continue
        if _local(element.tag) == "Line":
            lines[ident] = _LineInfo(
                _text(element, "TransportMode"), _submode(element), _operators(element)
            )
        elif line := _ref(element, "LineRef"):
            routes[ident] = line
    return lines, routes


# A group of journeys: (line ref, mode, submode, operators) -> count.
_Group = tuple[str, str, str, tuple[str, ...]]


def _read_journeys(
    archive: _Archive, lines: dict[str, _LineInfo]
) -> tuple[
    dict[str, collections.Counter[_Group]], dict[tuple[str, ...], collections.Counter[_Group]]
]:
    """Rail journeys, or journeys whose mode is still unknown, grouped by the
    pattern they follow (or, without one, by the points they call at)."""
    by_pattern: dict[str, collections.Counter[_Group]] = {}
    by_points: dict[tuple[str, ...], collections.Counter[_Group]] = {}
    for element in archive.walk(_JOURNEYS):
        line = _ref(element, "LineRef")
        mode, submode = _text(element, "TransportMode"), _submode(element)
        operators = _operators(element)
        known = lines.get(line)
        if not mode and known is not None:
            mode, submode = known.mode, known.submode
        if _mode_of(mode, submode) is None:
            continue
        group = (line, mode, submode, operators)
        pattern = _ref(element, "ServiceJourneyPatternRef", "JourneyPatternRef")
        if pattern:
            by_pattern.setdefault(pattern, collections.Counter())[group] += 1
            continue
        calls = _child(element, "calls")
        points = tuple(
            ref
            for call in (calls if calls is not None else ())
            if (ref := _ref(call, "ScheduledStopPointRef"))
        )
        if points:
            by_points.setdefault(points, collections.Counter())[group] += 1
    return by_pattern, by_points


# ── Pass 3: patterns -> points ────────────────────────────────────────────


@dataclass
class _Use:
    """Rail calls at one point or place, by mode (`_UNKNOWN` included)."""

    calls: collections.Counter[str] = field(default_factory=collections.Counter)
    operators: dict[str, set[str]] = field(default_factory=dict)

    def add(self, mode: str, count: int, operators: Iterable[str]) -> None:
        self.calls[mode] += count
        self.operators.setdefault(mode, set()).update(operators)

    def merge(self, other: _Use) -> None:
        for mode, count in other.calls.items():
            self.add(mode, count, other.operators.get(mode, ()))


def _served(point: Element) -> bool:
    """A point in a pattern where passengers may board or alight."""
    flags = [_text(point, name).lower() for name in ("ForBoarding", "ForAlighting")]
    return not all(flag == "false" for flag in flags)


def _pattern_line(element: Element, routes: dict[str, str]) -> str:
    line = _ref(element, "LineRef")
    if not line:
        view = _child(element, "RouteView")
        line = _ref(view, "LineRef") if view is not None else ""
    if not line:
        line = routes.get(_ref(element, "RouteRef"), "")
    return line


def _add_groups(
    use: dict[str, _Use],
    points: Iterable[str],
    groups: collections.Counter[_Group],
    pattern_line: str,
    lines: dict[str, _LineInfo],
) -> None:
    resolved: list[tuple[str, int, tuple[str, ...]]] = []
    for (line, mode, submode, operators), count in groups.items():
        info = lines.get(line or pattern_line)
        if not mode and info is not None:
            mode, submode = info.mode, info.submode
        rail = _mode_of(mode, submode)
        if rail is None:
            continue
        resolved.append((rail, count, operators + (info.operators if info else ())))
    if not resolved:
        return
    for point in points:
        target = use.setdefault(point, _Use())
        for rail, count, operators in resolved:
            target.add(rail, count, operators)


def _read_patterns(
    archive: _Archive,
    by_pattern: dict[str, collections.Counter[_Group]],
    by_points: dict[tuple[str, ...], collections.Counter[_Group]],
    lines: dict[str, _LineInfo],
    routes: dict[str, str],
) -> dict[str, _Use]:
    """Scheduled stop point -> its rail calls."""
    use: dict[str, _Use] = {}
    for called, groups in by_points.items():
        _add_groups(use, called, groups, "", lines)
    if not by_pattern:
        return use
    for element in archive.walk(_PATTERNS):
        pattern_groups = by_pattern.get(element.get("id") or "")
        if not pattern_groups:
            continue
        holder = _child(element, "pointsInSequence")
        points = [
            ref
            for point in (holder if holder is not None else ())
            if _local(point.tag) == "StopPointInJourneyPattern"
            and _served(point)
            and (ref := _ref(point, "ScheduledStopPointRef"))
        ]
        _add_groups(use, points, pattern_groups, _pattern_line(element, routes), lines)
    return use


# ── Pass 4: points -> places -> stations ──────────────────────────────────


@dataclass
class _Places:
    use: dict[str, _Use] = field(default_factory=dict)  # StopPlace or Quay id -> calls
    parent: dict[str, str] = field(default_factory=dict)  # StopPlace -> ParentSiteRef
    quay_place: dict[str, str] = field(default_factory=dict)  # Quay -> StopPlace
    rail_places: set[str] = field(default_factory=set)  # StopPlaces that call themselves rail


def _quays(element: Element) -> list[Element]:
    holder = _child(element, "quays")
    return [q for q in (holder if holder is not None else ()) if _local(q.tag) == "Quay"]


def _read_places(archive: _Archive, points: dict[str, _Use]) -> _Places:
    """Assignments of the called points, and the StopPlace structure."""
    places = _Places()
    assigned: dict[str, _Use] = {}
    for element in archive.walk(_PLACES):
        if _local(element.tag) == "PassengerStopAssignment":
            _assign(element, points, assigned)
        else:
            _note_stop_place(element, places)
    # Keep only the quays that matter.
    places.quay_place = {q: p for q, p in places.quay_place.items() if q in assigned}
    places.use = assigned
    return places


def _assign(element: Element, points: dict[str, _Use], assigned: dict[str, _Use]) -> None:
    """A PassengerStopAssignment: the calls of its point go to its Quay, or
    to its StopPlace when it names no Quay."""
    calls = points.get(_ref(element, "ScheduledStopPointRef"))
    place = _ref(element, "QuayRef") or _ref(element, "StopPlaceRef")
    if calls is not None and place:
        assigned.setdefault(place, _Use()).merge(calls)


def _note_stop_place(element: Element, places: _Places) -> None:
    """A StopPlace's parent, whether it calls itself rail, and its Quays."""
    ident = element.get("id") or ""
    if not ident:
        return
    if parent := _ref(element, "ParentSiteRef"):
        places.parent[ident] = parent
    if _text(element, "TransportMode") == "rail" or (
        _text(element, "StopPlaceType") == "railStation"
    ):
        places.rail_places.add(ident)
    for quay in _quays(element):
        if quay.get("id"):
            places.quay_place[str(quay.get("id"))] = ident


def _root(place: str, parent: dict[str, str]) -> str:
    current = place
    for _ in range(_MAX_DEPTH):
        nxt = parent.get(current)
        if not nxt or nxt == place:
            break
        current = nxt
    return current


def _stations(places: _Places) -> dict[str, Station]:
    """Root StopPlace -> Station, with its calls, modes and operators."""
    stations: dict[str, Station] = {}
    for ref, use in places.use.items():
        stop_place = places.quay_place.get(ref, ref)
        rail_place = stop_place in places.rail_places
        root = _root(stop_place, places.parent)
        for mode, count in use.calls.items():
            if mode == _UNKNOWN:
                if not rail_place:
                    continue
                mode = RAIL_MAIN
            station = stations.setdefault(root, Station(key=root))
            station.calls[mode] += count
            station.operators.update(use.operators.get(mode, ()))
            if mode == RAIL_MAIN:
                station.operators.update(use.operators.get(_UNKNOWN, ()))
    return stations


# ── Pass 5: details ───────────────────────────────────────────────────────


def _lambert93_to_wgs84(x: float, y: float) -> tuple[float, float]:
    """EPSG:2154 (RGF93 / Lambert-93) to WGS84 latitude and longitude, as
    MSMM's offline chain converts the Belgian export's `gml:pos`."""
    a, f = 6378137.0, 1 / 298.257222101
    e = math.sqrt(2 * f - f * f)
    lon0, lat0 = math.radians(3.0), math.radians(46.5)
    lat1, lat2 = math.radians(44.0), math.radians(49.0)
    x0, y0 = 700000.0, 6600000.0

    def m(p: float) -> float:
        return math.cos(p) / math.sqrt(1 - e * e * math.sin(p) ** 2)

    def t(p: float) -> float:
        ratio = (1 - e * math.sin(p)) / (1 + e * math.sin(p))
        return float(math.tan(math.pi / 4 - p / 2) / ratio ** (e / 2))

    n = (math.log(m(lat1)) - math.log(m(lat2))) / (math.log(t(lat1)) - math.log(t(lat2)))
    big_f = m(lat1) / (n * t(lat1) ** n)
    r0 = a * big_f * t(lat0) ** n
    dx, dy = x - x0, r0 - (y - y0)
    r = math.copysign(math.hypot(dx, dy), n)
    lon = math.atan2(dx, dy) / n + lon0
    tt = (r / (a * big_f)) ** (1 / n)
    phi = math.pi / 2 - 2 * math.atan(tt)
    for _ in range(15):
        phi = math.pi / 2 - 2 * math.atan(
            tt * ((1 - e * math.sin(phi)) / (1 + e * math.sin(phi))) ** (e / 2)
        )
    return math.degrees(phi), math.degrees(lon)


def _position(element: Element) -> tuple[float | None, float | None]:
    """WGS84 latitude and longitude of `Centroid/Location`: `Latitude` and
    `Longitude`, or a `gml:pos` in EPSG:4326 (latitude first), CRS84
    (longitude first) or EPSG:2154 (converted). Anything else is no position."""
    centroid = _child(element, "Centroid")
    location = _child(centroid, "Location") if centroid is not None else None
    if location is None:
        return None, None
    lat, lon = coordinate(_text(location, "Latitude")), coordinate(_text(location, "Longitude"))
    if lat is not None and lon is not None:
        return lat, lon
    pos = _child(location, "pos")
    if pos is None or not pos.text:
        return None, None
    parts = pos.text.split()
    first, second = (coordinate(p) for p in [*parts, "", ""][:2])
    if first is None or second is None:
        return None, None
    srs = (pos.get("srsName") or location.get("srsName") or "").upper()
    if "2154" in srs:
        return _lambert93_to_wgs84(first, second)
    if "CRS84" in srs:
        return second, first
    if "4326" in srs:
        return first, second
    return None, None


def _codes(element: Element, station: Station, prefix: str) -> None:
    """`@id`, the keyList entries, `PrivateCode` (with its type) and
    `PublicCode` of a StopPlace or a Quay, each field after `prefix`."""
    station.add_code(f"{prefix}@id", element.get("id"))
    key_list = _child(element, "keyList")
    for pair in key_list if key_list is not None else ():
        if _local(pair.tag) == "KeyValue":
            station.add_code(prefix + _text(pair, "Key"), _text(pair, "Value"))
    private = _child(element, "PrivateCode")
    if private is not None:
        kind = (private.get("type") or "").strip()
        name = f"{prefix}PrivateCode:{kind}" if kind else f"{prefix}PrivateCode"
        station.add_code(name, private.text)
    station.add_code(f"{prefix}PublicCode", _text(element, "PublicCode"))


def _place_codes(element: Element, station: Station) -> None:
    """The codes of a StopPlace and of its Quays."""
    _codes(element, station, "")
    for quay in _quays(element):
        _codes(quay, station, "Quay/")


def _read_details(archive: _Archive, stations: dict[str, Station], parent: dict[str, str]) -> None:
    """Names, positions and codes of the stations, and the codes of their
    child StopPlaces and Quays; a station without a position takes its first
    child's."""
    fallback: dict[str, tuple[float, float]] = {}
    child_codes: dict[str, list[tuple[str, str]]] = collections.defaultdict(list)
    for element in archive.walk(_DETAILS):
        ident = element.get("id") or ""
        root = _root(ident, parent) if ident else ""
        station = stations.get(root)
        if station is None:
            continue
        lat, lon = _position(element)
        if ident == root:
            station.place(_text(element, "Name"), lat, lon)
            _place_codes(element, station)
            continue
        scratch = Station(key=ident)
        _place_codes(element, scratch)
        child_codes[root].extend(scratch.code_pairs())
        if lat is not None and lon is not None:
            fallback.setdefault(root, (lat, lon))
    for root, codes in child_codes.items():
        for name, value in codes:
            stations[root].add_code(name, value)
    for root, (lat, lon) in fallback.items():
        stations[root].place("", lat, lon)


def read_netex(zf: zipfile.ZipFile) -> dict[str, Station]:
    """The rail stations of the NeTEx archive `zf`, by root StopPlace id."""
    archive = _Archive(zf)
    lines, routes = _read_lines(archive)
    by_pattern, by_points = _read_journeys(archive, lines)
    if not by_pattern and not by_points:
        return {}
    points = _read_patterns(archive, by_pattern, by_points, lines, routes)
    del by_pattern, by_points, routes
    if not points:
        return {}
    places = _read_places(archive, points)
    del points
    stations = _stations(places)
    if stations:
        _read_details(archive, stations, places.parent)
    return stations
