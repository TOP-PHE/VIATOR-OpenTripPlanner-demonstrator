"""The rail stations of a NeTEx archive (EPIP or Nordic), for the stop extract.

MSMM 22.2: one row per StopPlace (the root one when `ParentSiteRef` names a
StopPlace the archive defines) at which a rail ServiceJourney calls. Rail is
`TransportMode` rail on the ServiceJourney or on its Line (a rail journey
without a submode takes its Line's); a journey that carries no mode at all,
on itself or on its Line, counts at a StopPlace whose own `TransportMode` is
rail, or that gives no mode and whose `StopPlaceType` is `railStation`
(22.2, point 2: the railStation fallback, read as MSMM's chain reads it; the
Belgian export, for one, gives its journeys no mode). A `TransportMode` that
names no mode (`unknown`, `other`, ...: `_NO_MODE`) is no mode, the
deprecated `intercityRail` is rail (`_mode`).

**Census.** As it reads, the reader counts what it sees in a `NetexCensus`
(Lines and journeys by their mode, journeys kept, patterns, points,
assignments, StopPlaces by mode and type): counters only, so that a feed
that gives no station says why (`station_extract_common.Census`).

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

A member that holds none of a pass's elements is not read again. **Most of
the bytes never reach expat**: once a document's root element has begun,
`station_extract_skip.SkipFilter` drops, from the raw bytes, every element
of `_SKIPPED` (the passing times, calendars, links, scheduled stop points
and the other subtrees no pass reads) and the elements only the other
passes read (pass 2 never sees a ServiceJourney); what it drops of `_ALL` is
still noted as held by the member. Inside the elements a pass builds, the
subtrees of `_UNREAD` are not built either (an element written with a
namespace prefix other than the root's is not cut, only not built).

**Memory.** What is kept grows with the network, never with the journeys
or their passing times: each distinct journey group (Line, mode, submode,
operators) once, by number (`_Groups`); per pattern, per called point and
per place a tally (`_Tally`: one int until a second group comes); per
assignment the number of its (Quay, StopPlace) pair; a journey with direct
calls adds to its points at once. Some 250 bytes per point with its
assignment (1.2 KB before 15 October: an export that gives every journey its
own scheduled stop points has as many as calls, and spent the child's 2 GiB
near 1.6 million). Every table has its cap (`GROUPS_MAX`, `POINTS_MAX`,
`ASSIGNMENTS_MAX`, `QUAYS_MAX`, `STOP_PLACES_MAX`); a crafted file reaching
all of them at once would still pass 2 GiB, and ends `memory_limit`.

Measured on a synthetic national-shaped EPIP export (`scripts/
bench_station_extract.py`; one shared file of StopPlaces, one file per line
with its frames, the passing times most of the bytes, packed 20 times): see
`docs/architecture.md`, "Stop extract: large archives".
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
    Census,
    Station,
    ValueTally,
    check_declared,
    coordinate,
    new_station,
    open_member,
    tally_forms,
)
from .station_extract_skip import SkipFilter, root_prefix, start_tag_end

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
# The values of NeTEx's `AllVehicleModesOfTransportEnumeration` that name
# no mode: `unknown` and `other` ("use only when no other entry applies"),
# `all`, `anyMode` ("to be avoided"), and the deprecated `urbanRail` ("use
# mode metro with MetroSubmode urbanRail, or in some few cases rail with
# RailSubmode local"). Read as no mode, as MSMM's chain reads every value it
# does not map: the StopPlace decides (the railStation fallback). Before,
# such a journey was dropped as not rail.
_NO_MODE = frozenset({"unknown", "other", "all", "anyMode", "urbanRail"})
# The deprecated `intercityRail` ("use mode rail with RailSubmode
# longDistance") is rail.
_RAIL_ALIASES = frozenset({"intercityRail"})
# A mode not yet known: the journey's StopPlace decides (the railStation
# fallback).
_UNKNOWN = "?"
_TRANSPORT_MODE = "TransportMode"
_STOP_PLACE_TYPE = "StopPlaceType"
# How far a `ParentSiteRef` chain is followed.
_PARENT_DEPTH = 16
# The bounds of what is read (review of pull request 4).
XML_DEPTH_MAX = 64
XML_TEXT_MAX = MEGABYTE
XML_TOKEN_MAX = 2 * MEGABYTE
TARGET_ELEMENTS_MAX = 200_000
GROUPS_MAX = 1_000_000
_CHUNK = 64 * 1024
_PROLOG_PIECE = 1024
# expat's own guard against amplification (a few KB that unfold to GB), when
# the expat in use has it.
_AMPLIFICATION_MAX = 100.0
_EXPAT_NO_MEMORY = expat.errors.codes[expat.errors.XML_ERROR_NO_MEMORY]

# The elements each pass reads.
_JOURNEYS = frozenset({"Line", "Route", "ServiceJourney"})
_PATTERNS = frozenset({"ServiceJourneyPattern", "JourneyPattern"})
_PLACES = frozenset({"PassengerStopAssignment", "StopPlace"})
_DETAILS = frozenset({"StopPlace"})
_ALL = _JOURNEYS | _PATTERNS | _PLACES
# Inside the elements read, the subtrees no pass reads (the bulk of a
# timetable: passing times, links, equipment, a Call's Arrival and Departure)
# are not built.
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
        "Arrival",
        "Departure",
    }
)
# The elements dropped from the bytes before expat (`SkipFilter`): those of
# `_UNREAD`, and the frames and lists no pass reads, which never hold an
# element of `_ALL` (calendars, links, scheduled stop points, notices, ...).
_SKIPPED = _UNREAD | frozenset(
    {
        "ServiceCalendarFrame",
        "dayTypeAssignments",
        "operatingPeriods",
        "operatingDays",
        "serviceLinks",
        "scheduledStopPoints",
        "routePoints",
        "timingLinks",
        "destinationDisplays",
        "notices",
        "journeyInterchanges",
        "DeadRun",
        "DatedServiceJourney",
    }
)
# What only pass 3 reads (names, positions, codes): dropped in passes 1 and 2.
_DETAIL_ONLY = frozenset(
    {"keyList", "Name", "ShortName", "Description", "Centroid", "PrivateCode", "PublicCode"}
)
# Pass 1 drops the patterns, so a `pointsInSequence` it meets is a Route's.
_PASS_1_SKIPPED = _DETAIL_ONLY | {"pointsInSequence"}


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


def _mode(mode: str) -> str:
    """A `TransportMode` as the reader takes it: `rail` for a deprecated
    name of rail, '' for a value that names no mode."""
    if mode in _RAIL_ALIASES:
        return "rail"
    return "" if mode in _NO_MODE else mode


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
        self.root_index: int | None = None  # where the root element's start tag begins
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
        if self.root_index is None:
            self.root_index = self.last_event
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


class _Pass:
    """No filter: every byte to expat (a root start tag too long to see)."""

    def feed(self, data: bytes) -> bytes:
        return data

    def finish(self) -> bytes:
        return b""


class _Feeder:
    """Feeds one member to expat: the prolog as it is, `_PROLOG_PIECE` bytes
    at a time until the root element begins (so that the filter takes over
    close after the root's tag), then everything through `SkipFilter`, which
    first reads the bytes expat parsed after the root's start tag (a token
    boundary) to know its state."""

    def __init__(self, parser: Any, builder: _Builder, skipped: frozenset[str]) -> None:
        self.parser = parser
        self.builder = builder
        self.skipped = skipped
        self.skip: SkipFilter | _Pass | None = None
        self.window = b""  # the last bytes fed while the root element was not seen
        self.fed = 0

    def _note(self, name: str) -> None:
        if name in _ALL:
            self.builder.seen.add(name)

    def _parse(self, data: bytes) -> None:
        self.parser.Parse(data, False)
        self.fed += len(data)

    def _filter(self) -> SkipFilter | _Pass:
        """The filter, primed with what expat parsed after the root's tag."""
        start = self.builder.root_index - (self.fed - len(self.window))  # type: ignore[operator]
        end = start_tag_end(self.window, start) if start >= 0 else None
        if end is None:
            return _Pass()
        skip = SkipFilter(self.skipped, root_prefix(self.window, start), self._note)
        skip.prime(self.window[end:])
        return skip

    def feed(self, chunk: bytes) -> None:
        while self.skip is None and chunk:
            piece, chunk = chunk[:_PROLOG_PIECE], chunk[_PROLOG_PIECE:]
            self.window = self.window[-_CHUNK:] + piece
            self._parse(piece)
            if self.builder.root_index is not None:
                self.skip = self._filter()
        if self.skip is not None and chunk:
            self._parse(self.skip.feed(chunk))

    def finish(self) -> None:
        if self.skip is not None:
            self._parse(self.skip.finish())
        self.parser.Parse(b"", True)


def _walk(
    source: IO[bytes], targets: frozenset[str], seen: set[str], skipped: frozenset[str]
) -> Iterator[Element]:
    """The outermost elements of `targets` in document order, each whole.
    The local names of `_ALL` met, or dropped, are added to `seen`. Once the
    root element has begun, the elements of `skipped` are dropped from the
    bytes before expat sees them; before it (the prolog, a doctype) every
    byte goes to expat as it is."""
    parser = _parser()
    builder = _Builder(parser, targets, seen)
    feeder = _Feeder(parser, builder, skipped)
    try:
        while chunk := source.read(_CHUNK):
            feeder.feed(chunk)
            if feeder.fed - builder.last_event > XML_TOKEN_MAX:
                raise ArchiveError("input_too_large", "an XML token over 2 MiB")
            yield from builder.ready
            builder.ready.clear()
        feeder.finish()
    except expat.ExpatError as exc:
        if exc.code == _EXPAT_NO_MEMORY:
            # expat could not allocate (the child's memory limit): a memory
            # stop, never a malformed file.
            raise MemoryError from None
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
        check_declared(zf, self.names)
        self.holds: dict[str, set[str]] = {}

    def walk(
        self, targets: frozenset[str], also_skipped: frozenset[str] = frozenset()
    ) -> Iterator[Element]:
        """One reading pass: the unpacked bytes are counted from zero. The
        elements only other passes read are dropped with `_SKIPPED`: the
        passes run in order, and a later pass never needs what an earlier
        one dropped (pass 2 drops the journeys pass 1 read, pass 3 the
        patterns and assignments pass 2 read)."""
        budget = Budget()
        skipped = _SKIPPED | (_ALL - targets) | also_skipped
        for name in self.names:
            known = self.holds.get(name)
            if known is not None and not known & targets:
                continue
            seen: set[str] = set()
            with open_member(self.zf, name, budget) as source:
                yield from _walk(source, targets, seen, skipped)
            self.holds[name] = seen


# ── Journey groups and tallies ────────────────────────────────────────────

# A group of journeys: (line ref, mode, submode, operators).
_Group = tuple[str, str, str, tuple[str, ...]]

# A tally: the rail calls kept for a pattern, a point or a place, by group
# number. Most hold one group: then one int, `count << _SHIFT | group`
# (some 30 bytes, where a Counter takes 200); a dict {group: count} once a
# second group comes. This is what keeps memory small when an export gives
# every journey its own pattern, or its own scheduled stop points.
_Tally = int | dict[int, int]
_SHIFT = 32
_MASK = (1 << _SHIFT) - 1


def _tally_add(table: dict[Any, _Tally], key: Any, group: int, count: int) -> bool:
    """Add `count` calls of `group` to `table[key]`; True when `key` is new."""
    old = table.get(key)
    if old is None:
        table[key] = count << _SHIFT | group
        return True
    if isinstance(old, int):
        if old & _MASK == group:
            table[key] = old + (count << _SHIFT)
            return False
        old = table[key] = {old & _MASK: old >> _SHIFT}
    old[group] = old.get(group, 0) + count
    return False


def _tally_items(tally: _Tally) -> Iterable[tuple[int, int]]:
    """The (group, count) pairs of a tally."""
    if isinstance(tally, int):
        return ((tally & _MASK, tally >> _SHIFT),)
    return tally.items()


def _tally_groups(tally: _Tally) -> int:
    return 1 if isinstance(tally, int) else len(tally)


class _Groups:
    """Every distinct journey group, by number: a tally holds the number,
    never the group's strings."""

    def __init__(self) -> None:
        self.ids: dict[_Group, int] = {}
        self.groups: list[_Group] = []
        self.journeys: list[int] = []  # the journeys of each group, for the census

    def id_of(self, group: _Group) -> int:
        number = self.ids.get(group)
        if number is None:
            if len(self.groups) >= GROUPS_MAX:
                raise ArchiveError("input_too_large", _TOO_MANY_GROUPS)
            number = self.ids[group] = len(self.groups)
            self.groups.append(group)
            self.journeys.append(0)
        return number


_TOO_MANY_GROUPS = "more journey groups than VIATOR keeps"


# The words of the modes a kept journey ends with, for the census.
_KEPT_WORDS = {RAIL_MAIN: "rail", RAIL_SUBURBAN: "suburban", _UNKNOWN: "none"}


@dataclass
class NetexCensus(Census):
    """What the NeTEx reader saw (`station_extract_common.Census`). None:
    the pass that counts it did not run (no journey kept for it)."""

    members: int = 0
    lines: ValueTally = field(default_factory=ValueTally)  # by the Line's TransportMode
    journeys: ValueTally = field(default_factory=ValueTally)  # by the journey's own
    # The journeys kept (rail, or no mode yet), by the mode they end with
    # once their Line is known (from their pattern or route too).
    kept: ValueTally = field(default_factory=ValueTally)
    no_line_ref: int = 0  # kept journeys that name no Line
    line_undefined: int = 0  # kept journeys naming a Line the archive does not define
    line_from_pattern: int = 0  # of those naming none, given one by their pattern or route
    patterns: int | None = None  # journey patterns read
    patterns_used: int | None = None  # of them, followed by a kept journey
    points: int | None = None  # scheduled stop points with calls of kept journeys
    assignments: int | None = None
    stop_places: int | None = None
    stop_place_modes: ValueTally = field(default_factory=ValueTally)
    stop_place_types: ValueTally = field(default_factory=ValueTally)
    rail_stop_places: int | None = None  # StopPlaces that call themselves rail
    places_called: int | None = None  # assigned places with calls

    @property
    def without_line(self) -> int:
        return max(0, self.no_line_ref + self.line_undefined - self.line_from_pattern)

    def segments(self) -> list[list[str]]:
        kept = self.kept.total
        segments = [
            tally_forms(self.lines.total, "lines", self.lines),
            [f"{self.journeys.total:,} journeys ({kept:,} kept)"],
            self.stations_forms(),
        ]
        if self.without_line:
            segments.append([f"{self.without_line:,} without a line"])
        if self.stop_places is not None:
            segments.append([f"{self.stop_places:,} stop places ({self.rail_stop_places:,} rail)"])
        return segments

    def fields(self) -> list[tuple[str, object]]:
        return [
            ("members", self.members),
            ("lines", self.lines.total),
            ("line_modes", self.lines),
            ("journeys", self.journeys.total),
            ("journey_modes", self.journeys),
            ("kept", self.kept),
            ("no_line_ref", self.no_line_ref),
            ("line_undefined", self.line_undefined),
            ("line_from_pattern", self.line_from_pattern),
            ("patterns", self.patterns),
            ("patterns_used", self.patterns_used),
            ("points", self.points),
            ("assignments", self.assignments),
            ("stop_places", self.stop_places),
            ("stop_place_modes", self.stop_place_modes if self.stop_places is not None else None),
            ("stop_place_types", self.stop_place_types if self.stop_places is not None else None),
            ("rail_stop_places", self.rail_stop_places),
            ("places_called", self.places_called),
            *super().fields(),
        ]


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


# The scheduled stop points kept with calls, at most: those of the rail
# patterns, and those a journey without a pattern calls at whose mode is not
# yet known to be other than rail (its Line may come later in the archive;
# such points are dropped at the end of pass 1, but counted until then).
# Past it the archive is refused (`input_too_large`) before their tallies
# take more memory.
# 4 million (owner's decision, 16 October): an export that gives every
# journey its own scheduled stop points has one per call; measured 951 MiB
# for 2.85 GB of such XML (some 4 million), under the child's 2 GiB.
POINTS_MAX = 4_000_000


@dataclass
class _Journeys:
    """What pass 1 keeps: the Lines and Routes (their few fields), the groups,
    and the rail calls by pattern (`by_pattern`) or, for the journeys that
    name no pattern, already by scheduled stop point (`points`)."""

    lines: dict[str, _LineInfo] = field(default_factory=dict)
    routes: dict[str, str] = field(default_factory=dict)
    groups: _Groups = field(default_factory=_Groups)
    by_pattern: dict[str, _Tally] = field(default_factory=dict)
    points: dict[str, _Tally] = field(default_factory=dict)
    pairs: int = 0  # (pattern, group) pairs in `by_pattern`
    census: NetexCensus = field(default_factory=NetexCensus)

    def add_pattern(self, pattern: str, group: int) -> None:
        old = self.by_pattern.get(pattern)
        before = 0 if old is None else _tally_groups(old)
        _tally_add(self.by_pattern, pattern, group, 1)
        if _tally_groups(self.by_pattern[pattern]) > before:
            self.pairs += 1
            if self.pairs > GROUPS_MAX:
                raise ArchiveError("input_too_large", _TOO_MANY_GROUPS)

    def add_point(self, point: str, group: int, count: int) -> None:
        """Add calls to a point; past `POINTS_MAX` points, refuse."""
        if _tally_add(self.points, point, group, count) and len(self.points) > POINTS_MAX:
            raise ArchiveError("input_too_large", "more called points than VIATOR keeps")

    def rail(self, group: int) -> str | None:
        """The MSMM mode of a group (its Line's mode when it has none), None
        when it is not rail, `_UNKNOWN` when no mode is given."""
        line, mode, submode, _ = self.groups.groups[group]
        return _mode_of(*_with_line(mode, submode, self.lines.get(line)))

    def operators_of(self, group: int) -> tuple[str, ...]:
        line, _, _, operators = self.groups.groups[group]
        info = self.lines.get(line)
        return operators + (info.operators if info else ())


def _journey(element: Element, found: _Journeys) -> None:
    """A ServiceJourney, kept unless its mode (its own, or its Line's when
    that Line was read already) is not rail. One that names no pattern adds
    its calls to its points at once: nothing is kept per journey."""
    line = _ref(element, "LineRef")
    own = _text(element, _TRANSPORT_MODE)
    found.census.journeys.add(own)
    mode, submode = _with_line(_mode(own), _submode(element), found.lines.get(line))
    if _mode_of(mode, submode) is None:
        return
    group = found.groups.id_of((line, mode, submode, _operators(element)))
    found.groups.journeys[group] += 1
    pattern = _ref(element, "ServiceJourneyPatternRef", "JourneyPatternRef")
    if pattern:
        found.add_pattern(pattern, group)
        return
    calls = _child(element, "calls")
    for call in calls if calls is not None else ():
        if ref := _ref(call, "ScheduledStopPointRef"):
            found.add_point(ref, group, 1)


def _keep_rail(table: dict[str, _Tally], found: _Journeys) -> None:
    """Drop the groups whose Line, read after them, says they are not rail.
    A key left with none is removed after the loop (the table never changes
    size while it is iterated; replacing a value does not)."""
    emptied: list[str] = []
    for key, tally in table.items():
        kept = {g: c for g, c in _tally_items(tally) if found.rail(g) is not None}
        if not kept:
            emptied.append(key)
        elif len(kept) == 1:
            ((group, count),) = kept.items()
            table[key] = count << _SHIFT | group
        else:
            table[key] = kept
    for key in emptied:
        del table[key]


def _read_journeys(archive: _Archive, census: NetexCensus | None = None) -> _Journeys:
    """Lines, Routes and the rail journeys (or those whose mode is still
    unknown), counted by the pattern they follow (or, without one, at the
    points they call at). A journey read before its Line is filtered once
    every Line is known."""
    found = _Journeys(census=census if census is not None else NetexCensus())
    found.census.members = len(archive.names)
    for element in archive.walk(_JOURNEYS, _PASS_1_SKIPPED):
        ident = element.get("id") or ""
        local = _local(element.tag)
        if local == "ServiceJourney":
            _journey(element, found)
        elif local == "Line":
            own = _text(element, _TRANSPORT_MODE)
            found.census.lines.add(own)
            if ident:
                found.lines[ident] = _LineInfo(_mode(own), _submode(element), _operators(element))
        elif ident and (line := _ref(element, "LineRef")):
            found.routes[ident] = line
    _keep_rail(found.by_pattern, found)
    _keep_rail(found.points, found)
    _count_kept(found)
    return found


def _count_kept(found: _Journeys) -> None:
    """The census of the journeys kept: by the mode they end with, and those
    whose Line is not known."""
    census = found.census
    for group, (line, _, _, _) in enumerate(found.groups.groups):
        mode = found.rail(group)
        if mode is None:
            continue
        count = found.groups.journeys[group]
        census.kept.add(_KEPT_WORDS[mode], count)
        if not line:
            census.no_line_ref += count
        elif line not in found.lines:
            census.line_undefined += count


# ── Pass 2: patterns -> points ────────────────────────────────────────────


@dataclass
class _Use:
    """Rail calls at one place, by mode (`_UNKNOWN` included)."""

    calls: collections.Counter[str] = field(default_factory=collections.Counter)
    operators: dict[str, set[str]] = field(default_factory=dict)

    def add(self, mode: str, count: int, operators: Iterable[str]) -> None:
        self.calls[mode] += count
        self.operators.setdefault(mode, set()).update(operators)


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


def _rail_groups(tally: _Tally, pattern_line: str, found: _Journeys) -> list[tuple[int, int]]:
    """The rail (group, count) pairs of a pattern's tally; a group without a
    Line takes the pattern's."""
    resolved: list[tuple[int, int]] = []
    for group, count in _tally_items(tally):
        line, mode, submode, operators = found.groups.groups[group]
        if not line and pattern_line:
            before = found.rail(group)
            group = found.groups.id_of((pattern_line, mode, submode, operators))
            _count_from_pattern(found, before, group, count)
        if found.rail(group) is not None:
            resolved.append((group, count))
    return resolved


def _count_from_pattern(found: _Journeys, before: str | None, group: int, count: int) -> None:
    """The census of journeys given their Line by their pattern: moved to
    the mode they end with (none when that Line is not rail)."""
    census = found.census
    census.line_from_pattern += count
    after = found.rail(group)
    if before is not None and after != before:
        census.kept.add(_KEPT_WORDS[before], -count)
        if after is not None:
            census.kept.add(_KEPT_WORDS[after], count)


def _served_points(element: Element) -> Iterator[str]:
    """The scheduled stop points of a pattern where passengers may board or
    alight."""
    holder = _child(element, "pointsInSequence")
    for point in holder if holder is not None else ():
        if _local(point.tag) != "StopPointInJourneyPattern" or not _served(point):
            continue
        if ref := _ref(point, "ScheduledStopPointRef"):
            yield ref


def _pattern(element: Element, found: _Journeys) -> None:
    """A journey pattern a rail journey follows: its served points get the
    calls of those journeys."""
    census = found.census
    census.patterns = (census.patterns or 0) + 1
    tally = found.by_pattern.get(element.get("id") or "")
    if tally is None:
        return
    census.patterns_used = (census.patterns_used or 0) + 1
    resolved = _rail_groups(tally, _pattern_line(element, found.routes), found)
    if not resolved:
        return
    for ref in _served_points(element):
        for group, count in resolved:
            found.add_point(ref, group, count)


# ── Pass 2: patterns, assignments and the StopPlace structure ─────────────

# The assignments, the Quays and the StopPlaces kept from that pass, each at
# most.
# The assignments with `POINTS_MAX` (one per point in such an export).
ASSIGNMENTS_MAX = 4_000_000
QUAYS_MAX = 2_000_000
STOP_PLACES_MAX = 2_000_000

# An assignment's place: (Quay ref, StopPlace ref), either may be empty.
_Place = tuple[str, str]


@dataclass
class _Places:
    """What pass 2 keeps. A place is known by its number in `pairs`; an
    assignment keeps the number (an int, or a list for a point assigned more
    than once), never its own copy of the refs."""

    pairs: list[_Place] = field(default_factory=list)
    pair_ids: dict[_Place, int] = field(default_factory=dict)
    assignments: dict[str, int | list[int]] = field(default_factory=dict)
    assigned: int = 0
    use: dict[int, _Tally] = field(default_factory=dict)  # place number -> rail calls
    called: bool = False  # whether any scheduled stop point has a rail call
    defined: set[str] = field(default_factory=set)  # every StopPlace id of the archive
    parent: dict[str, str] = field(default_factory=dict)  # StopPlace -> ParentSiteRef
    quay_place: dict[str, str] = field(default_factory=dict)  # Quay -> StopPlace
    rail_places: set[str] = field(default_factory=set)  # StopPlaces that call themselves rail

    def place_id(self, place: _Place) -> int:
        number = self.pair_ids.get(place)
        if number is None:
            number = self.pair_ids[place] = len(self.pairs)
            self.pairs.append(place)
        return number


def _quays(element: Element) -> list[Element]:
    holder = _child(element, "quays")
    return [q for q in (holder if holder is not None else ()) if _local(q.tag) == "Quay"]


def _read_structure(archive: _Archive, found: _Journeys) -> _Places:
    """One pass: the patterns of the rail journeys (scheduled stop point ->
    its rail calls), every PassengerStopAssignment (point -> Quay and
    StopPlace) and the StopPlace structure; then the calls go to the places
    their points are assigned to."""
    places = _Places()
    quays: dict[str, str] = {}
    targets = _PATTERNS | _PLACES if found.by_pattern else _PLACES
    for element in archive.walk(targets, _DETAIL_ONLY):
        local = _local(element.tag)
        if local == "PassengerStopAssignment":
            _note_assignment(element, places)
        elif local == "StopPlace":
            _note_stop_place(element, places, quays, found.census)
        else:
            _pattern(element, found)
    found.by_pattern.clear()
    _count_places(found, places)
    for point, tally in found.points.items():
        numbers = places.assignments.get(point, ())
        for number in (numbers,) if isinstance(numbers, int) else numbers:
            for group, count in _tally_items(tally):
                _tally_add(places.use, number, group, count)
    places.called = bool(found.points)
    found.census.places_called = len(places.use)
    found.points.clear()
    places.assignments.clear()
    places.pair_ids.clear()
    wanted = {places.pairs[number][0] for number in places.use if places.pairs[number][0]}
    places.quay_place = {q: p for q, p in quays.items() if q in wanted}
    return places


def _count_places(found: _Journeys, places: _Places) -> None:
    census = found.census
    census.patterns = census.patterns or 0
    census.patterns_used = census.patterns_used or 0
    census.points = len(found.points)
    census.assignments = places.assigned
    census.stop_places = len(places.defined)
    census.rail_stop_places = len(places.rail_places)


def _note_assignment(element: Element, places: _Places) -> None:
    """A PassengerStopAssignment: its point and its (Quay, StopPlace); which
    of the two places the point is settled once the Quays are known."""
    point = _ref(element, "ScheduledStopPointRef")
    place = (_ref(element, "QuayRef"), _ref(element, "StopPlaceRef"))
    if not point or not any(place):
        return
    places.assigned += 1
    if places.assigned > ASSIGNMENTS_MAX:
        raise ArchiveError("input_too_large", "more assignments than VIATOR keeps")
    number = places.place_id(place)
    old = places.assignments.get(point)
    if old is None:
        places.assignments[point] = number
    elif isinstance(old, int):
        places.assignments[point] = [old, number]
    else:
        old.append(number)


def _calls_itself_rail(element: Element) -> bool:
    """A StopPlace's own mode is rail, or it gives none (or one MSMM's chain
    does not know) and its type is `railStation`."""
    mode = _mode(_text(element, _TRANSPORT_MODE))
    if mode == "rail":
        return True
    return mode not in _OTHER_MODES and _text(element, _STOP_PLACE_TYPE) == "railStation"


def _note_stop_place(
    element: Element, places: _Places, quays: dict[str, str], census: NetexCensus
) -> None:
    """A StopPlace's id, parent, whether it calls itself rail, and its Quays."""
    census.stop_place_modes.add(_text(element, _TRANSPORT_MODE))
    census.stop_place_types.add(_text(element, _STOP_PLACE_TYPE))
    ident = element.get("id") or ""
    if not ident:
        return
    places.defined.add(ident)
    if len(places.defined) > STOP_PLACES_MAX:
        raise ArchiveError("input_too_large", "more StopPlaces than VIATOR keeps")
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


def _use_of(tally: _Tally, found: _Journeys) -> _Use:
    """The rail calls of a place by mode, with the operators of each mode."""
    use = _Use()
    for group, count in _tally_items(tally):
        mode = found.rail(group)
        if mode is not None:
            use.add(mode, count, found.operators_of(group))
    return use


def _stations(places: _Places, found: _Journeys) -> dict[str, Station]:
    """Root StopPlace -> Station, with its calls, modes and operators."""
    stations: dict[str, Station] = {}
    for number, tally in places.use.items():
        stop_place = _stop_place(places.pairs[number], places)
        if not stop_place:
            continue
        rail_place = stop_place in places.rail_places
        root = _root(stop_place, places)
        use = _use_of(tally, found)
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


def read_netex(zf: zipfile.ZipFile, census: NetexCensus | None = None) -> dict[str, Station]:
    """The rail stations of the NeTEx archive `zf`, by root StopPlace id.
    Raises `ArchiveError` `no_stop_place` when rail journeys call but the
    archive defines no StopPlace or assigns no called point to one.
    `census`, when given, is filled with what was seen, as far as the
    reading went."""
    archive = _Archive(zf)
    found = _read_journeys(archive, census)
    if not found.by_pattern and not found.points:
        return {}
    places = _read_structure(archive, found)
    if not places.called:
        return {}
    if not places.defined or not places.use:
        raise ArchiveError("no_stop_place", "no StopPlace, or no assignment to one")
    stations = _stations(places, found)
    del found
    if stations:
        _read_details(archive, stations, places)
    return stations
