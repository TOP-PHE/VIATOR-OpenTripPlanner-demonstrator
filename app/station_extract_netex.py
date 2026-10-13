"""The rail stations of a NeTEx archive (EPIP or Nordic), for the stop extract.

MSMM 22.2: one row per StopPlace (the root one when `ParentSiteRef` names a
StopPlace the archive defines) at which a rail ServiceJourney calls. Rail is
`TransportMode` rail on the ServiceJourney or on its Line (a rail journey
without a submode takes its Line's); a journey that carries no mode at all,
on itself or on its Line, counts at a StopPlace whose own `TransportMode` is
rail, or that gives no mode and whose `StopPlaceType` is `railStation`
(22.2, point 2: the railStation fallback, read as MSMM's chain reads it; the
Belgian export, for one, gives its journeys no mode).

**Streaming, bounded.** Every XML member is read in chunks with expat, never
whole: an element is built only inside the elements a pass reads (a Line, a
journey, a pattern, an assignment, a StopPlace), each dropped once read, so
memory holds one of them at a time plus the small maps below. Past a bound
the archive is refused (`ArchiveError`): elements nested deeper than
`XML_DEPTH_MAX`, a text run or an attribute over `XML_TEXT_MAX`, a token
(a tag, a comment) over `XML_TOKEN_MAX`, an element read with more than
`TARGET_ELEMENTS_MAX` elements inside, more than `GROUPS_MAX` journey groups,
the unpacked bytes of `station_extract_common.Budget`. A document that
declares an entity, internal or external, is refused (`bad_archive`): NeTEx
needs none. An external DTD subset a document names is never loaded.

The references between frames point forwards and across files, so the
members are read in three passes, each keeping only what the next one needs:

1. Lines (mode, submode, operator and authority), Routes (their Line) and
   ServiceJourneys, grouped by pattern, Line, mode and operator, the non-rail
   ones dropped;
2. the journey patterns those journeys use (their scheduled stop points),
   every PassengerStopAssignment (point -> Quay and StopPlace) and the
   StopPlace structure: which StopPlaces exist, their parent, their Quays,
   which call themselves rail;
3. the details (name, position, codes) of the stations and their children.

A member that holds none of a pass's elements is not read again, and inside
the elements a pass reads, the subtrees no pass reads (passing times, links,
equipment) are not built. Measured on a synthetic single-file feed of 538 MB
(200,000 journeys): about 56 s on one core; a national file of a few GB takes
some minutes, within the child process's 25 minutes of processor time.
"""

from __future__ import annotations

import collections
import math
import re
import zipfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import IO, Any

# ElementTree only builds the elements read; expat parses, hardened in _parser().
from xml.etree.ElementTree import Element, SubElement  # nosec B405
from xml.parsers import expat  # nosec B406

from .station_extract_common import (
    MEGABYTE,
    RAIL_MAIN,
    RAIL_SUBURBAN,
    ArchiveError,
    Budget,
    Station,
    coordinate,
    new_station,
    open_member,
)

# A rail submode that MSMM's offline chain reads as suburban rail (its
# `nap_modes.py`, `_SUBURBAN_SUBMODES`), compared in small letters.
_SUBURBAN_SUBMODES = frozenset(
    {"suburbanrailway", "urbanrailway", "citytrain", "regionalrail", "s-bahn"}
)
# The modes MSMM's chain maps to something other than rail: a StopPlace with
# one of them never counts a journey without a mode, whatever its type.
_OTHER_MODES = frozenset(
    {
        "metro",
        "underground",
        "tram",
        "funicular",
        "cableway",
        "telecabin",
        "lift",
        "chairLift",
        "bus",
        "coach",
        "trolleyBus",
        "taxi",
        "shuttle",
        "water",
        "ferry",
    }
)
# A mode not yet known: the journey's StopPlace decides (the railStation
# fallback).
_UNKNOWN = "?"
# How far a `ParentSiteRef` chain is followed.
_PARENT_DEPTH = 16
# The bounds of what is read (review of pull request 4).
XML_DEPTH_MAX = 64
XML_TEXT_MAX = MEGABYTE
XML_TOKEN_MAX = 2 * MEGABYTE
TARGET_ELEMENTS_MAX = 200_000
GROUPS_MAX = 1_000_000
_CHUNK = 64 * 1024
# expat's own guard against amplification (a few KB that unfold to GB), when
# the expat in use has it.
_AMPLIFICATION_MAX = 100.0

# The elements each pass reads.
_JOURNEYS = frozenset({"Line", "Route", "ServiceJourney"})
_PATTERNS = frozenset({"ServiceJourneyPattern", "JourneyPattern"})
_PLACES = frozenset({"PassengerStopAssignment", "StopPlace"})
_DETAILS = frozenset({"StopPlace"})
_ALL = _JOURNEYS | _PATTERNS | _PLACES
# Inside the elements read, the subtrees no pass reads (the bulk of a
# timetable: passing times, links, equipment) are not built.
_UNREAD = frozenset(
    {
        "passingTimes",
        "linksInSequence",
        "dayTypes",
        "noticeAssignments",
        "validityConditions",
        "accessibilityAssessment",
        "equipmentPlaces",
        "placeEquipments",
        "entrances",
        "levels",
        "facilities",
        "alternativeNames",
    }
)


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


def _refuse(*_args: object) -> None:
    raise ArchiveError("bad_archive", "the XML declares an entity or an external subset")


def _refuse_external(
    _context: str, _base: str | None, _system_id: str | None, _public_id: str | None
) -> int:
    raise ArchiveError("bad_archive", "the XML names an external entity or subset")


def _parser() -> Any:
    """An expat parser that loads nothing from outside the document and
    refuses every entity declaration (as defusedxml does)."""
    parser = expat.ParserCreate(namespace_separator="}")  # nosec B314
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    # Called for every entity declaration, internal, external and unparsed.
    parser.EntityDeclHandler = _refuse
    parser.ExternalEntityRefHandler = _refuse_external
    for guard in (
        "SetAllocTrackerMaximumAmplification",
        "SetBillionLaughsAttackProtectionMaximumAmplification",
    ):
        if hasattr(parser, guard):
            getattr(parser, guard)(_AMPLIFICATION_MAX)
    parser.buffer_text = True
    parser.buffer_size = 1 << 16
    return parser


class _Builder:
    """The expat handlers of one member: builds the elements of `targets`
    whole, nothing outside them, and keeps every bound."""

    def __init__(self, parser: Any, targets: frozenset[str], seen: set[str]) -> None:
        self.parser = parser
        self.targets = targets
        self.seen = seen
        self.depth = 0
        self.open: list[Element] = []  # the open elements inside a target
        self.texts: list[list[str]] = []
        self.skipping = 0  # depth inside a subtree that no pass reads
        self.size = 0  # elements in the current target
        self.run = 0  # characters of the current text run
        self.last_event = 0  # byte index of the last event
        self.ready: list[Element] = []
        self.locals: dict[str, str] = {}
        parser.StartElementHandler = self.start
        parser.EndElementHandler = self.end
        parser.CharacterDataHandler = self.characters

    def _local(self, name: str) -> str:
        local = self.locals.get(name)
        if local is None:
            local = self.locals[name] = name.rpartition("}")[2]
        return local

    def start(self, name: str, attrs: dict[str, str]) -> None:
        self.last_event = self.parser.CurrentByteIndex
        self.run = 0
        self.depth += 1
        if self.depth > XML_DEPTH_MAX:
            raise ArchiveError("input_too_large", "XML nested deeper than 64 elements")
        if attrs and max(map(len, attrs.values())) > XML_TEXT_MAX:
            raise ArchiveError("input_too_large", "an XML attribute over 1 MiB")
        if self.skipping:
            self.skipping += 1
        elif self.open:
            if self._local(name) in _UNREAD:
                self.skipping = 1
                return
            self.size += 1
            if self.size > TARGET_ELEMENTS_MAX:
                raise ArchiveError("input_too_large", "an XML element holding too many elements")
            self.open.append(SubElement(self.open[-1], name, attrs))
            self.texts.append([])
        elif self._local(name) in self.targets:
            self.size = 1
            self.open.append(Element(name, attrs))
            self.texts.append([])

    def end(self, name: str) -> None:
        self.last_event = self.parser.CurrentByteIndex
        self.run = 0
        self.depth -= 1
        if self.skipping:
            self.skipping -= 1
            return
        local = self._local(name)
        if local in _ALL:
            self.seen.add(local)
        if not self.open:
            return
        element = self.open.pop()
        element.text = "".join(self.texts.pop()) or None
        if not self.open:
            self.ready.append(element)

    def characters(self, data: str) -> None:
        self.last_event = self.parser.CurrentByteIndex
        self.run += len(data)
        if self.run > XML_TEXT_MAX:
            raise ArchiveError("input_too_large", "an XML text over 1 MiB")
        # Only an element's own text before its first child is kept.
        if self.open and not self.skipping and len(self.open[-1]) == 0:
            self.texts[-1].append(data)


def _walk(source: IO[bytes], targets: frozenset[str], seen: set[str]) -> Iterator[Element]:
    """The outermost elements of `targets` in document order, each whole.
    The local names of `_ALL` met are added to `seen`."""
    parser = _parser()
    builder = _Builder(parser, targets, seen)
    fed = 0
    try:
        while chunk := source.read(_CHUNK):
            parser.Parse(chunk, False)
            fed += len(chunk)
            if fed - builder.last_event > XML_TOKEN_MAX:
                raise ArchiveError("input_too_large", "an XML token over 2 MiB")
            yield from builder.ready
            builder.ready.clear()
        parser.Parse(b"", True)
    except expat.ExpatError as exc:
        raise ArchiveError("bad_archive", f"XML not well-formed at line {exc.lineno}") from None
    yield from builder.ready


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
        """One reading pass: the unpacked bytes are counted from zero."""
        budget = Budget()
        for name in self.names:
            known = self.holds.get(name)
            if known is not None and not known & targets:
                continue
            seen: set[str] = set()
            with open_member(self.zf, name, budget) as source:
                yield from _walk(source, targets, seen)
            self.holds[name] = seen


# ── Pass 1: lines, routes and journeys ────────────────────────────────────


@dataclass(frozen=True)
class _LineInfo:
    mode: str
    submode: str
    operators: tuple[str, ...]


def _operators(element: Element) -> tuple[str, ...]:
    return tuple(
        ref for ref in (_ref(element, "OperatorRef"), _ref(element, "AuthorityRef")) if ref
    )


def _with_line(mode: str, submode: str, info: _LineInfo | None) -> tuple[str, str]:
    """A journey's mode and submode, completed from its Line: the Line's mode
    when the journey has none, the Line's submode when the journey is rail
    without one."""
    if info is None:
        return mode, submode
    if not mode:
        return info.mode, info.submode
    if mode == info.mode and not submode:
        return mode, info.submode
    return mode, submode


# A group of journeys: (line ref, mode, submode, operators) -> count.
_Group = tuple[str, str, str, tuple[str, ...]]


@dataclass
class _Journeys:
    lines: dict[str, _LineInfo] = field(default_factory=dict)
    routes: dict[str, str] = field(default_factory=dict)
    by_pattern: dict[str, collections.Counter[_Group]] = field(default_factory=dict)
    by_points: dict[tuple[str, ...], collections.Counter[_Group]] = field(default_factory=dict)
    groups: int = 0

    def add(self, table: dict[Any, collections.Counter[_Group]], key: Any, group: _Group) -> None:
        counter = table.setdefault(key, collections.Counter())
        if group not in counter:
            self.groups += 1
            if self.groups > GROUPS_MAX:
                raise ArchiveError("input_too_large", "more journey groups than VIATOR keeps")
        counter[group] += 1


def _journey(element: Element, found: _Journeys) -> None:
    """A ServiceJourney, kept unless its mode (its own, or its Line's when
    that Line was read already) is not rail."""
    line = _ref(element, "LineRef")
    mode, submode = _with_line(
        _text(element, "TransportMode"), _submode(element), found.lines.get(line)
    )
    if _mode_of(mode, submode) is None:
        return
    group = (line, mode, submode, _operators(element))
    pattern = _ref(element, "ServiceJourneyPatternRef", "JourneyPatternRef")
    if pattern:
        found.add(found.by_pattern, pattern, group)
        return
    calls = _child(element, "calls")
    points = tuple(
        ref
        for call in (calls if calls is not None else ())
        if (ref := _ref(call, "ScheduledStopPointRef"))
    )
    if points:
        found.add(found.by_points, points, group)


def _keep_rail(table: dict[Any, collections.Counter[_Group]], lines: dict[str, _LineInfo]) -> None:
    """Drop the groups whose Line, read after them, says they are not rail."""
    kept_table = {}
    for key, groups in table.items():
        kept = collections.Counter(
            {
                group: count
                for group, count in groups.items()
                if _mode_of(*_with_line(group[1], group[2], lines.get(group[0]))) is not None
            }
        )
        if kept:
            kept_table[key] = kept
    table.clear()
    table.update(kept_table)


def _read_journeys(archive: _Archive) -> _Journeys:
    """Lines, Routes and the rail journeys (or those whose mode is still
    unknown), grouped by the pattern they follow (or, without one, by the
    points they call at). A journey read before its Line is filtered once
    every Line is known."""
    found = _Journeys()
    for element in archive.walk(_JOURNEYS):
        ident = element.get("id") or ""
        local = _local(element.tag)
        if local == "ServiceJourney":
            _journey(element, found)
        elif ident and local == "Line":
            found.lines[ident] = _LineInfo(
                _text(element, "TransportMode"), _submode(element), _operators(element)
            )
        elif ident and (line := _ref(element, "LineRef")):
            found.routes[ident] = line
    _keep_rail(found.by_pattern, found.lines)
    _keep_rail(found.by_points, found.lines)
    return found


# ── Pass 2: patterns -> points ────────────────────────────────────────────


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
        rail = _mode_of(*_with_line(mode, submode, info))
        if rail is None:
            continue
        resolved.append((rail, count, operators + (info.operators if info else ())))
    if not resolved:
        return
    for point in points:
        target = use.setdefault(point, _Use())
        for rail, count, operators in resolved:
            target.add(rail, count, operators)


def _pattern(element: Element, found: _Journeys, use: dict[str, _Use]) -> None:
    """A journey pattern a rail journey follows: its served points get the
    calls of those journeys."""
    pattern_groups = found.by_pattern.get(element.get("id") or "")
    if not pattern_groups:
        return
    holder = _child(element, "pointsInSequence")
    points = [
        ref
        for point in (holder if holder is not None else ())
        if _local(point.tag) == "StopPointInJourneyPattern"
        and _served(point)
        and (ref := _ref(point, "ScheduledStopPointRef"))
    ]
    _add_groups(use, points, pattern_groups, _pattern_line(element, found.routes), found.lines)


# ── Pass 2: patterns, assignments and the StopPlace structure ─────────────

# The assignments and the Quays kept from that pass, each at most.
ASSIGNMENTS_MAX = 2_000_000
QUAYS_MAX = 2_000_000

# An assignment's place: (Quay ref, StopPlace ref), either may be empty.
_Place = tuple[str, str]


@dataclass
class _Places:
    use: dict[_Place, _Use] = field(default_factory=dict)
    called: bool = False  # whether any scheduled stop point has a rail call
    defined: set[str] = field(default_factory=set)  # every StopPlace id of the archive
    parent: dict[str, str] = field(default_factory=dict)  # StopPlace -> ParentSiteRef
    quay_place: dict[str, str] = field(default_factory=dict)  # Quay -> StopPlace
    rail_places: set[str] = field(default_factory=set)  # StopPlaces that call themselves rail


def _quays(element: Element) -> list[Element]:
    holder = _child(element, "quays")
    return [q for q in (holder if holder is not None else ()) if _local(q.tag) == "Quay"]


def _read_structure(archive: _Archive, found: _Journeys) -> _Places:
    """One pass: the patterns of the rail journeys (scheduled stop point ->
    its rail calls), every PassengerStopAssignment (point -> Quay and
    StopPlace) and the StopPlace structure; then the calls go to the places
    their points are assigned to."""
    use: dict[str, _Use] = {}
    for called, groups in found.by_points.items():
        _add_groups(use, called, groups, "", found.lines)
    assignments: dict[str, list[_Place]] = {}
    places = _Places()
    quays: dict[str, str] = {}
    counts = [0]
    for element in archive.walk(_PATTERNS | _PLACES if found.by_pattern else _PLACES):
        local = _local(element.tag)
        if local == "PassengerStopAssignment":
            _note_assignment(element, assignments, counts)
        elif local == "StopPlace":
            _note_stop_place(element, places, quays)
        else:
            _pattern(element, found, use)
    for point, calls in use.items():
        for place in assignments.get(point, ()):
            places.use.setdefault(place, _Use()).merge(calls)
    places.called = bool(use)
    wanted = {place[0] for place in places.use if place[0]}
    places.quay_place = {q: p for q, p in quays.items() if q in wanted}
    return places


def _note_assignment(
    element: Element, assignments: dict[str, list[_Place]], counts: list[int]
) -> None:
    """A PassengerStopAssignment: its point and its (Quay, StopPlace); which
    of the two places the point is settled once the Quays are known."""
    point = _ref(element, "ScheduledStopPointRef")
    place = (_ref(element, "QuayRef"), _ref(element, "StopPlaceRef"))
    if not point or not any(place):
        return
    counts[0] += 1
    if counts[0] > ASSIGNMENTS_MAX:
        raise ArchiveError("input_too_large", "more assignments than VIATOR keeps")
    assignments.setdefault(point, []).append(place)


def _calls_itself_rail(element: Element) -> bool:
    """A StopPlace's own mode is rail, or it gives none (or one MSMM's chain
    does not know) and its type is `railStation`."""
    mode = _text(element, "TransportMode")
    if mode == "rail":
        return True
    return mode not in _OTHER_MODES and _text(element, "StopPlaceType") == "railStation"


def _note_stop_place(element: Element, places: _Places, quays: dict[str, str]) -> None:
    """A StopPlace's id, parent, whether it calls itself rail, and its Quays."""
    ident = element.get("id") or ""
    if not ident:
        return
    places.defined.add(ident)
    if parent := _ref(element, "ParentSiteRef"):
        places.parent[ident] = parent
    if _calls_itself_rail(element):
        places.rail_places.add(ident)
    for quay in _quays(element):
        if quay.get("id"):
            quays[str(quay.get("id"))] = ident
    if len(quays) > QUAYS_MAX:
        raise ArchiveError("input_too_large", "more Quays than VIATOR keeps")


def _root(place: str, places: _Places) -> str:
    """The root StopPlace of `place`, following `ParentSiteRef` while it names
    a StopPlace the archive defines; a dangling parent is no parent, and a
    StopPlace in a cycle of parents is its own root."""
    current, seen = place, {place}
    for _ in range(_PARENT_DEPTH):
        nxt = places.parent.get(current)
        if not nxt or nxt not in places.defined:
            return current
        if nxt in seen:
            return place
        seen.add(nxt)
        current = nxt
    return current


def _stop_place(place: _Place, places: _Places) -> str:
    """The StopPlace of an assignment: its Quay's when that Quay is known,
    else the StopPlace it names, else '' (nothing to place it at)."""
    quay, stop_place = place
    return places.quay_place.get(quay) or stop_place


def _stations(places: _Places) -> dict[str, Station]:
    """Root StopPlace -> Station, with its calls, modes and operators."""
    stations: dict[str, Station] = {}
    for place, use in places.use.items():
        stop_place = _stop_place(place, places)
        if not stop_place:
            continue
        rail_place = stop_place in places.rail_places
        root = _root(stop_place, places)
        for mode, count in use.calls.items():
            if mode == _UNKNOWN:
                if not rail_place:
                    continue
                mode = RAIL_MAIN
            station = new_station(stations, root)
            station.calls[mode] += count
            station.operators.update(use.operators.get(mode, ()))
            if mode == RAIL_MAIN:
                station.operators.update(use.operators.get(_UNKNOWN, ()))
    return stations


# ── Pass 3: details ───────────────────────────────────────────────────────


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


# The EPSG code at the end of a `srsName`: `EPSG:2154`,
# `urn:ogc:def:crs:EPSG::2154`, `http://www.opengis.net/def/crs/EPSG/0/2154`.
_EPSG = re.compile(r"EPSG(?::+|/(?:[^/]*/)?)(\d+)$", re.ASCII | re.IGNORECASE)
_LATITUDE_FIRST = frozenset({"4326", "4258"})


def _position(element: Element) -> tuple[float | None, float | None]:
    """WGS84 latitude and longitude of `Centroid/Location`: `Latitude` and
    `Longitude`; or a `gml:pos` in EPSG:4326 or EPSG:4258 (latitude first),
    CRS84 (longitude first) or EPSG:2154 (converted); a `gml:pos` with no
    system is the frame's default, WGS84 latitude first, when its first
    value can be a latitude (as MSMM's chain reads it). Anything else is no
    position."""
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
    first, second = (coordinate(p) for p in [*pos.text.split()[:2], "", ""][:2])
    if first is None or second is None:
        return None, None
    srs = (pos.get("srsName") or location.get("srsName") or "").strip()
    return _projected(srs, first, second)


def _projected(srs: str, first: float, second: float) -> tuple[float | None, float | None]:
    if not srs:
        return (first, second) if abs(first) <= 90 else (None, None)
    if srs.upper().endswith("CRS84"):
        return second, first
    match = _EPSG.search(srs)
    code = match.group(1) if match else ""
    if code in _LATITUDE_FIRST:
        return first, second
    if code == "2154":
        return _lambert93_to_wgs84(first, second)
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


def _place_codes(element: Element, station: Station, prefix: str) -> None:
    """The codes of a StopPlace (after `prefix`) and of its Quays (`Quay/`)."""
    _codes(element, station, prefix)
    for quay in _quays(element):
        _codes(quay, station, "Quay/")


def _read_details(archive: _Archive, stations: dict[str, Station], places: _Places) -> None:
    """Names, positions and codes of the stations, and the codes of their
    child StopPlaces (`StopPlace/` before the field) and Quays (`Quay/`); a
    station without a position takes its first child's."""
    fallback: dict[str, tuple[float, float]] = {}
    child_codes: dict[str, Station] = {}
    for element in archive.walk(_DETAILS):
        ident = element.get("id") or ""
        root = _root(ident, places) if ident else ""
        station = stations.get(root)
        if station is None:
            continue
        lat, lon = _position(element)
        if ident == root:
            station.place(_text(element, "Name"), lat, lon)
            _place_codes(element, station, "")
            continue
        _place_codes(element, child_codes.setdefault(root, Station(key=root)), "StopPlace/")
        if lat is not None and lon is not None:
            fallback.setdefault(root, (lat, lon))
    for root, scratch in child_codes.items():
        for name, value in scratch.code_pairs():
            stations[root].add_code(name, value)
        stations[root].codes_over_cap += scratch.codes_over_cap
    for root, (lat, lon) in fallback.items():
        stations[root].place("", lat, lon)


def read_netex(zf: zipfile.ZipFile) -> dict[str, Station]:
    """The rail stations of the NeTEx archive `zf`, by root StopPlace id.
    Raises `ArchiveError` `no_stop_place` when rail journeys call but the
    archive defines no StopPlace or assigns no called point to one."""
    archive = _Archive(zf)
    found = _read_journeys(archive)
    if not found.by_pattern and not found.by_points:
        return {}
    places = _read_structure(archive, found)
    del found
    if not places.called:
        return {}
    if not places.defined or not places.use:
        raise ArchiveError("no_stop_place", "no StopPlace, or no assignment to one")
    stations = _stations(places)
    if stations:
        _read_details(archive, stations, places)
    return stations
