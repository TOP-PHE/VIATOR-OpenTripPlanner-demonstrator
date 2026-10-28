"""Repairs, in MOTIS's copy of a NeTEx file, of three forms MOTIS drops a
whole file for.

Measured 2026-10-28 on ÖBB's NAP file (MENTZ, EPIP 1.10, 431 line files):
once it had a time zone (app/motis_timezones.py), MOTIS 2.11.2 still dropped
247 of its files, so most Austrian trains were missing:

* a course whose `passingTimes` skip points of its `ServiceJourneyPattern`
  (10 836 courses in 232 files, e.g. 7 passing times for 8 points). nigiri
  pairs the two lists by position (`utl::zip`, then `utl::verify` on the ids)
  and throws: `utl::zip container size mismatch 7 vs 8`;
* a pattern or course whose `LineRef` names a `Line` defined in another file of
  the zip (1 634 references in 59 files): lines are looked up in the same file
  only (`lines.at`);
* a `ParentSiteRef` to a site the file does not define (67 in 60 files):
  `stops.at(parent_ref)` throws (`ankerl::unordered_dense::map::at(): key not
  found`).

MOTIS 2.11.4 reads all three the same way (nigiri 18bf63c), so the build
repairs its copy:

* such a course gets a pattern of its own: a copy of its pattern keeping only
  the points it has a passing time for (`<id>-viator-<n>`, one per distinct
  set of points). A course whose passing times are not, in order, points of
  its pattern, or that keeps fewer than two, or whose pattern has fewer than
  two, is dropped, with the journey meetings and interchanges of the file
  that name it, instead of the whole file;
* a missing `Line` is copied from the file of the zip that defines it, with
  the operator, authority and product category it names when the file lacks
  them; one with no container to go to is removed from the copy (MOTIS then
  uses its default);
* a dangling `ParentSiteRef` is removed.

Then, measured on the repaired copy the same day, two more:

* 44 files still dropped: ÖBB defines its AvailabilityConditions (with
  ValidDayBits) in `ServiceFrame/validityConditions`, where its stay-seated
  JourneyMeetings name them, but MOTIS 2.11.2 reads them only from
  `ServiceCalendarFrame/validityConditions`: they are copied there;
* every ÖBB train came out as a tram with no name: its patterns have no
  `RouteView/LineRef`, the only place MOTIS reads a passingTimes course's line
  from, so MOTIS gave them its empty default line (route type 0 = tram). Each
  such pattern gets its line: its route's, else its courses', else the file's.

Byte edits, like app/netex_calendar.py: the rest of the file is passed through
byte for byte. Unprefixed elements only, the only ones MOTIS reads. A zip with
base files (SBB's layout, `has_base_files`) is never repaired: what a file
lacks may be defined there. `netex_calendar.convert_zip` runs these repairs.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

# A member larger than this is passed through unrepaired (it is read whole).
MAX_MEMBER_BYTES = 256 * 1024 * 1024

_ID_RE = re.compile(rb"""\bid\s*=\s*(["'])(.*?)\1""", re.S)
_REF_RE = re.compile(rb"""\bref\s*=\s*(["'])(.*?)\1""", re.S)
_SEQUENCE_RE = re.compile(rb"<pointsInSequence\b[^>]*>(.*?)</pointsInSequence\s*>", re.S)
_TIMES_RE = re.compile(rb"<passingTimes\b[^>]*>(.*?)</passingTimes\s*>", re.S)
_PATTERN_REF_RE = re.compile(rb"<(?:ServiceJourneyPatternRef|JourneyPatternRef)\b([^>]*)>")
_POINT_REF_RE = re.compile(rb"<StopPointInJourneyPatternRef\b([^>]*)>")
_OTHER_POINT_RE = re.compile(
    rb"<(?!StopPointInJourneyPattern\b)[A-Za-z][\w.-]*PointInJourneyPattern\b"
)
_LINE_REF_RE = re.compile(rb"<LineRef\b([^>]*)>")
_LINES_END = b"</lines>"
_ORGANISATIONS_END = b"</organisations>"

# What a Line names, and where its definition goes in a file that lacks it.
_DEPENDENCIES: dict[bytes, tuple[bytes, bytes]] = {
    b"OperatorRef": (b"Operator", _ORGANISATIONS_END),
    b"AuthorityRef": (b"Authority", _ORGANISATIONS_END),
    b"TypeOfProductCategoryRef": (b"TypeOfProductCategory", b"</values>"),
}


def _element(name: bytes) -> re.Pattern[bytes]:
    """`<name …/>` or `<name …>…</name>`, not nested in itself."""
    return re.compile(
        rb"<" + name + rb"\b(?P<attrs>[^>]*?)(?:/>|>(?P<body>.*?)</" + name + rb"\s*>)", re.S
    )


def _start_tags(names: tuple[bytes, ...]) -> re.Pattern[bytes]:
    return re.compile(rb"<(?:" + b"|".join(names) + rb")\b([^>]*)>")


_PATTERN_RE = _element(b"ServiceJourneyPattern")
_JOURNEY_RE = _element(b"ServiceJourney")
_POINT_RE = _element(b"StopPointInJourneyPattern")
_TIME_RE = _element(b"TimetabledPassingTime")
_LINE_RE = _element(b"Line")
_LINK_RES = (_element(b"JourneyMeeting"), _element(b"ServiceJourneyInterchange"))
_PARENT_RE = re.compile(
    rb"""<ParentSiteRef\b[^>]*?\bref\s*=\s*(["'])(.*?)\1[^>]*?(?:/>|>\s*</ParentSiteRef\s*>)""",
    re.S,
)
_SITE_TAGS = _start_tags((b"StopPlace", b"Quay"))
_LINE_TAGS = _start_tags((b"Line",))


def _value(rx: re.Pattern[bytes], attrs: bytes) -> bytes | None:
    m = rx.search(attrs)
    return m.group(2) if m else None


def _ids(data: bytes, tags: re.Pattern[bytes]) -> set[bytes]:
    return {v for m in tags.finditer(data) if (v := _value(_ID_RE, m.group(1))) is not None}


@dataclass
class RepairStats:
    members: int = 0  # members changed
    patterns_derived: int = 0
    journeys_refitted: int = 0
    journeys_dropped: int = 0
    links_dropped: int = 0
    parents_dropped: int = 0
    lines_copied: int = 0
    lines_not_found: int = 0
    dependencies_copied: int = 0
    dependencies_dropped: int = 0
    conditions_copied: int = 0
    lines_given: int = 0
    patterns_without_line: int = 0
    too_large: int = 0

    def summary(self) -> str:
        links = f" ({self.links_dropped} meetings/interchanges)" if self.links_dropped else ""
        parts = [
            (
                self.journeys_refitted,
                f"{self.journeys_refitted} courses given one of {self.patterns_derived} "
                "patterns without the points they skip",
            ),
            (self.journeys_dropped, f"{self.journeys_dropped} unreadable courses dropped{links}"),
            (self.lines_given, f"{self.lines_given} patterns given their line"),
            (self.patterns_without_line, f"{self.patterns_without_line} patterns with no line"),
            (self.lines_copied, f"{self.lines_copied} lines copied from another file"),
            (self.lines_not_found, f"{self.lines_not_found} missing lines not copied"),
            (self.dependencies_dropped, f"{self.dependencies_dropped} line references removed"),
            (
                self.conditions_copied,
                f"{self.conditions_copied} validity conditions copied into the ServiceCalendarFrame",
            ),
            (self.parents_dropped, f"{self.parents_dropped} dangling ParentSiteRef removed"),
            (self.too_large, f"{self.too_large} files too large to repair"),
        ]
        return ", ".join(text for count, text in parts if count)


@dataclass
class LineCopy:
    """A Line's block, and what it names: (ref tag, id, definition in the
    Line's own file, None when that file lacks it too)."""

    block: bytes
    dependencies: list[tuple[bytes, bytes, bytes | None]] = field(default_factory=list)


LineSource = Callable[[bytes], "LineCopy | None"]


def has_base_files(names: list[str]) -> bool:
    """True for a zip MOTIS reads with base files (nigiri `load_timetable`:
    a path holding both "SCHWEIZ" and "_1_1_", SBB's layout): their
    definitions are visible to every file, so nothing here is repaired."""
    return any("SCHWEIZ" in n and "_1_1_" in n for n in names)


def line_ids(data: bytes) -> set[bytes]:
    """The ids of the Lines a member defines."""
    return _ids(data, _LINE_TAGS) if b"<Line" in data else set()


def missing_lines(data: bytes) -> set[bytes]:
    """The Lines a member names but does not define."""
    if b"<LineRef" not in data:
        return set()
    refs = {v for m in _LINE_REF_RE.finditer(data) if (v := _value(_REF_RE, m.group(1)))}
    return refs - line_ids(data)


def line_copy(data: bytes, line_id: bytes) -> LineCopy | None:
    """The Line `line_id` of member `data`, with the definitions it names there."""
    for m in _LINE_RE.finditer(data):
        if _value(_ID_RE, m.group("attrs")) != line_id:
            continue
        copy = LineCopy(block=m.group(0))
        for ref_tag, (kind, _container) in _DEPENDENCIES.items():
            tags = re.compile(rb"<" + ref_tag + rb"\b([^>]*)>")
            for ref_m in tags.finditer(copy.block):
                ref = _value(_REF_RE, ref_m.group(1))
                if ref:
                    copy.dependencies.append((ref_tag, ref, _definition(data, kind, ref)))
        return copy
    return None


def _definition(data: bytes, kind: bytes, ident: bytes) -> bytes | None:
    for m in _element(kind).finditer(data):
        if _value(_ID_RE, m.group("attrs")) == ident:
            return m.group(0)
    return None


def repair(data: bytes, lines: LineSource | None, stats: RepairStats) -> bytes:
    """`data` repaired, or `data` itself when nothing needed it."""
    own_lines = line_ids(data)
    out = _drop_dangling_parents(data, stats)
    if lines is not None:
        out = _copy_missing_lines(out, lines, stats)
    out = _copy_conditions(out, stats)
    out = _give_patterns_their_line(out, own_lines, stats)
    out = _refit_courses(out, stats)
    if out is not data:
        stats.members += 1
    return out


# ─────────────────────────── AvailabilityCondition ───────────────────────────

_CALENDAR_FRAME_RE = _element(b"ServiceCalendarFrame")
_CALENDAR_FRAME_START_RE = re.compile(rb"<ServiceCalendarFrame\b[^>]*>")
_CONDITION_RE = _element(b"AvailabilityCondition")


def _copy_conditions(data: bytes, stats: RepairStats) -> bytes:
    """Copy each AvailabilityCondition with ValidDayBits defined outside a
    ServiceCalendarFrame (ÖBB: `ServiceFrame/validityConditions`, named by its
    JourneyMeetings) into a `validityConditions` opening the file's first
    ServiceCalendarFrame: the only place MOTIS 2.11 reads them
    (`//ServiceCalendarFrame/validityConditions/AvailabilityCondition`)."""
    if b"<AvailabilityCondition" not in data:
        return data
    frames = [(m.start(), m.end()) for m in _CALENDAR_FRAME_RE.finditer(data)]
    if not frames:
        return data
    inside = [m for m in _CONDITION_RE.finditer(data) if any(s <= m.start() < e for s, e in frames)]
    have = {_value(_ID_RE, m.group("attrs")) for m in inside}
    copies = []
    for m in _CONDITION_RE.finditer(data):
        ident = _value(_ID_RE, m.group("attrs"))
        if ident is None or ident in have or b"<ValidDayBits" not in (m.group("body") or b""):
            continue
        copies.append(m.group(0))
        have.add(ident)
    start = _CALENDAR_FRAME_START_RE.search(data, frames[0][0])
    if not copies or start is None:
        return data
    stats.conditions_copied += len(copies)
    block = b"<validityConditions>" + b"".join(copies) + b"</validityConditions>"
    return _apply(data, [(start.end(), start.end(), block)])


# ─────────────────────────── a pattern's Line ───────────────────────────

_ROUTE_VIEW_RE = re.compile(rb"<RouteView\b[^>]*?(?:/>|>(?P<body>.*?)</RouteView\s*>)", re.S)
_ROUTE_RE = _element(b"Route")
_ROUTE_REF_RE = re.compile(rb"<RouteRef\b([^>]*)>")


@dataclass
class _LineClues:
    """Where a pattern's line can be read when its RouteView names none."""

    routes: dict[bytes, bytes]  # Route id -> its LineRef
    courses: dict[bytes, Counter[bytes]]  # pattern id -> LineRefs of its courses
    own_lines: set[bytes]  # the Lines the file defined before any copy


def _line_clues(data: bytes, own_lines: set[bytes]) -> _LineClues:
    routes = {}
    for m in _ROUTE_RE.finditer(data):
        line = _LINE_REF_RE.search(m.group("body") or b"")
        ident = _value(_ID_RE, m.group("attrs"))
        if ident is not None and line is not None and (value := _value(_REF_RE, line.group(1))):
            routes[ident] = value
    courses: dict[bytes, Counter[bytes]] = {}
    for j in _JOURNEY_RE.finditer(data):
        body = j.group("body") or b""
        pattern, line = _PATTERN_REF_RE.search(body), _LINE_REF_RE.search(body)
        pid = _value(_REF_RE, pattern.group(1)) if pattern else None
        lid = _value(_REF_RE, line.group(1)) if line else None
        if pid and lid:
            courses.setdefault(pid, Counter())[lid] += 1
    return _LineClues(routes=routes, courses=courses, own_lines=own_lines)


def _pattern_line(pid: bytes | None, body: bytes, clues: _LineClues) -> bytes | None:
    """The line of a pattern without RouteView/LineRef: its route's, else the
    one most of its courses name, else the file's only Line."""
    route = _ROUTE_REF_RE.search(body)
    route_id = _value(_REF_RE, route.group(1)) if route else None
    if route_id is not None and route_id in clues.routes:
        return clues.routes[route_id]
    named = clues.courses.get(pid) if pid is not None else None
    if named:
        return named.most_common(1)[0][0]
    return next(iter(clues.own_lines)) if len(clues.own_lines) == 1 else None


def _give_patterns_their_line(data: bytes, own_lines: set[bytes], stats: RepairStats) -> bytes:
    """MOTIS reads a pattern's line from `RouteView/LineRef` only; without one
    its courses get an empty default line (no name, route type 0, shown as a
    tram: ÖBB, 2026-10-28). Such a pattern gets its line (`_pattern_line`)."""
    if b"<ServiceJourneyPattern" not in data:
        return data
    clues: _LineClues | None = None
    edits: list[tuple[int, int, bytes]] = []
    for m in _PATTERN_RE.finditer(data):
        body = m.group("body")
        view = _ROUTE_VIEW_RE.search(body) if body is not None else None
        if body is None or (view is not None and _LINE_REF_RE.search(view.group("body") or b"")):
            continue
        clues = clues or _line_clues(data, own_lines)
        line = _pattern_line(_value(_ID_RE, m.group("attrs")), body, clues)
        if line is None or b'"' in line:
            stats.patterns_without_line += 1
            continue
        edits.append(_line_edit(m, view, line))
        stats.lines_given += 1
    return _apply(data, edits) if edits else data


def _line_edit(
    pattern: re.Match[bytes], view: re.Match[bytes] | None, line: bytes
) -> tuple[int, int, bytes]:
    """Name `line` in the pattern's RouteView, adding the RouteView if need be."""
    ref = b'<LineRef ref="' + line + b'"/>'
    body_at = pattern.start("body")
    if view is None:  # right after the pattern's start tag
        return body_at, body_at, b"<RouteView>" + ref + b"</RouteView>"
    if view.group("body") is None:  # <RouteView/>
        return body_at + view.start(), body_at + view.end(), b"<RouteView>" + ref + b"</RouteView>"
    at = body_at + view.start("body")
    return at, at, ref


# ───────────────────────────── ParentSiteRef ─────────────────────────────


def _drop_dangling_parents(data: bytes, stats: RepairStats) -> bytes:
    if b"<ParentSiteRef" not in data:
        return data
    sites = _ids(data, _SITE_TAGS)
    dropped = 0

    def keep_or_drop(m: re.Match[bytes]) -> bytes:
        nonlocal dropped
        if m.group(2) in sites:
            return m.group(0)
        dropped += 1
        return b""

    out = _PARENT_RE.sub(keep_or_drop, data)
    if not dropped:
        return data
    stats.parents_dropped += dropped
    return out


# ─────────────────────────────────── Line ───────────────────────────────────


def _values_end(data: bytes) -> int:
    """Where a product category goes: the end of a ValueSet's `values`."""
    start = data.find(b"<typesOfValue")
    return data.find(b"</values>", start) if start >= 0 else -1


def _drop_ref(block: bytes, ref_tag: bytes, ident: bytes) -> bytes:
    rx = re.compile(rb"<" + ref_tag + rb"\b[^>]*?(?:/>|>\s*</" + ref_tag + rb"\s*>)", re.S)
    return rx.sub(lambda m: b"" if _value(_REF_RE, m.group(0)) == ident else m.group(0), block)


def _copy_missing_lines(data: bytes, lines: LineSource, stats: RepairStats) -> bytes:
    wanted = missing_lines(data)
    if not wanted:
        return data
    lines_end = data.find(_LINES_END)
    copies = []
    for line_id in sorted(wanted):
        copy = lines(line_id)
        if copy is None or lines_end < 0:
            stats.lines_not_found += 1
            continue
        copies.append(copy)
    if not copies:
        return data
    target = _Target(
        ends={_ORGANISATIONS_END: data.find(_ORGANISATIONS_END), b"</values>": _values_end(data)},
        have={kind: _ids(data, _start_tags((kind,))) for kind, _end in _DEPENDENCIES.values()},
    )
    for copy in copies:
        block = _place_dependencies(copy, target, stats)
        target.inserts.setdefault(lines_end, []).append(block)
        stats.lines_copied += 1
    return _apply(data, [(at, at, b"".join(blocks)) for at, blocks in target.inserts.items()])


@dataclass
class _Target:
    """The file a Line is copied into: where each kind of definition goes,
    what it already defines, and what is inserted where."""

    ends: dict[bytes, int]
    have: dict[bytes, set[bytes]]
    inserts: dict[int, list[bytes]] = field(default_factory=dict)


def _place_dependencies(copy: LineCopy, target: _Target, stats: RepairStats) -> bytes:
    """Queue the definitions a copied Line names that `target` lacks; the
    Line's block, without the references that have nowhere to go."""
    block = copy.block
    for ref_tag, ident, definition in copy.dependencies:
        kind, end_tag = _DEPENDENCIES[ref_tag]
        if ident in target.have[kind]:
            continue
        if definition is None or target.ends[end_tag] < 0:
            block = _drop_ref(block, ref_tag, ident)
            stats.dependencies_dropped += 1
            continue
        target.inserts.setdefault(target.ends[end_tag], []).append(definition)
        target.have[kind].add(ident)
        stats.dependencies_copied += 1
    return block


# ───────────────────────────── passing times ─────────────────────────────


@dataclass
class _Pattern:
    start: int
    end: int
    id_start: int
    id_end: int
    points: list[tuple[int, int, bytes | None]]  # absolute span, id
    other_points: bool

    @property
    def ids(self) -> list[bytes | None]:
        return [p[2] for p in self.points]


def _patterns(data: bytes) -> dict[bytes, _Pattern]:
    out: dict[bytes, _Pattern] = {}
    for m in _PATTERN_RE.finditer(data):
        id_m = _ID_RE.search(m.group("attrs"))
        body = m.group("body")
        seq = _SEQUENCE_RE.search(body) if body is not None else None
        if id_m is None or seq is None:
            continue
        base = m.start("body") + seq.start(1)
        inner = seq.group(1)
        points = [
            (base + p.start(), base + p.end(), _value(_ID_RE, p.group("attrs")))
            for p in _POINT_RE.finditer(inner)
        ]
        attrs_at = m.start("attrs")
        out[id_m.group(2)] = _Pattern(
            start=m.start(),
            end=m.end(),
            id_start=attrs_at + id_m.start(2),
            id_end=attrs_at + id_m.end(2),
            points=points,
            other_points=_OTHER_POINT_RE.search(inner) is not None,
        )
    return out


def _is_subsequence(part: list[bytes], whole: list[bytes | None]) -> bool:
    it = iter(whole)
    return all(any(x == y for y in it) for x in part)


def _derive(data: bytes, pattern: _Pattern, keep: set[bytes], new_id: bytes) -> bytes:
    pieces = [data[pattern.start : pattern.id_start], new_id]
    pos = pattern.id_end
    for start, end, ident in pattern.points:
        if ident in keep:
            continue
        pieces.append(data[pos:start])
        pos = end
    pieces.append(data[pos : pattern.end])
    return b"".join(pieces)


@dataclass
class _Refit:
    edits: list[tuple[int, int, bytes]] = field(default_factory=list)
    derived: dict[tuple[bytes, tuple[bytes, ...]], bytes] = field(default_factory=dict)
    blocks: dict[bytes, list[bytes]] = field(default_factory=dict)
    dropped: set[bytes] = field(default_factory=set)


def _passing_points(times: bytes) -> list[bytes | None]:
    """The `StopPointInJourneyPatternRef` of each passing time, in order."""
    return [
        _value(_REF_RE, r.group(1)) if (r := _POINT_REF_RE.search(t.group(0))) else None
        for t in _TIME_RE.finditer(times)
    ]


def _readable_subset(refs: list[bytes | None], pattern: _Pattern) -> bool:
    """True when the passing times name, once each and in order, at least two
    points of the pattern: a pattern keeping only those reads as MOTIS wants."""
    kept = [r for r in refs if r is not None]
    return (
        len(kept) == len(refs)
        and len(kept) >= 2
        and len(set(kept)) == len(kept)
        and _is_subsequence(kept, pattern.ids)
    )


def _course(data: bytes, j: re.Match[bytes], patterns: dict[bytes, _Pattern], state: _Refit) -> int:
    """Refit one course; 1 refitted, -1 dropped, 0 left alone. A course whose
    pattern this file does not define is left alone: a base file may."""
    body = j.group("body")
    if body is None or b"<calls" in body:
        return 0
    times = _TIMES_RE.search(body)
    ref_m = _PATTERN_REF_RE.search(body)
    ref_value = _REF_RE.search(ref_m.group(1)) if ref_m is not None else None
    if times is None or ref_m is None or ref_value is None:
        return 0
    pid = ref_value.group(2)
    pattern = patterns.get(pid)
    if pattern is None or pattern.other_points:
        return 0
    refs = _passing_points(times.group(1))
    if refs == pattern.ids and len(refs) >= 2:
        return 0
    if not _readable_subset(refs, pattern):
        state.edits.append((j.start(), j.end(), b""))
        if (jid := _value(_ID_RE, j.group("attrs"))) is not None:
            state.dropped.add(jid)
        return -1
    kept = [r for r in refs if r is not None]
    key = (pid, tuple(kept))
    new_id = state.derived.get(key)
    if new_id is None:
        siblings = state.blocks.setdefault(pid, [])
        new_id = pid + b"-viator-" + str(len(siblings) + 1).encode()
        siblings.append(_derive(data, pattern, set(kept), new_id))
        state.derived[key] = new_id
    value_at = j.start("body") + ref_m.start(1) + ref_value.start(2)
    state.edits.append((value_at, value_at + len(pid), new_id))
    return 1


def _refit_courses(data: bytes, stats: RepairStats) -> bytes:
    if b"<passingTimes" not in data or b"<ServiceJourneyPattern" not in data:
        return data
    patterns = _patterns(data)
    state = _Refit()
    outcomes = [_course(data, j, patterns, state) for j in _JOURNEY_RE.finditer(data)]
    stats.journeys_refitted += outcomes.count(1)
    stats.journeys_dropped += outcomes.count(-1)
    if not state.edits:
        return data
    for pid, blocks in state.blocks.items():
        end = patterns[pid].end
        state.edits.append((end, end, b"".join(blocks)))
        stats.patterns_derived += len(blocks)
    stats.links_dropped += _drop_links(data, state)
    return _apply(data, state.edits)


def _drop_links(data: bytes, state: _Refit) -> int:
    """Queue the removal of the journey meetings and interchanges that name a
    dropped course (MOTIS aborts the whole import on a link to a course it
    did not load); how many."""
    if not state.dropped:
        return 0
    links = [link for rx in _LINK_RES for link in rx.finditer(data)]
    named = [
        link
        for link in links
        if {r.group(2) for r in _REF_RE.finditer(link.group(0))} & state.dropped
    ]
    state.edits += [(link.start(), link.end(), b"") for link in named]
    return len(named)


def _apply(data: bytes, edits: list[tuple[int, int, bytes]]) -> bytes:
    """`data` with each (start, end) span replaced; overlapping edits skipped."""
    pieces: list[bytes] = []
    pos = 0
    for start, end, replacement in sorted(edits, key=lambda e: (e[0], e[1])):
        if start < pos:
            continue
        pieces += [data[pos:start], replacement]
        pos = end
    pieces.append(data[pos:])
    return b"".join(pieces)
