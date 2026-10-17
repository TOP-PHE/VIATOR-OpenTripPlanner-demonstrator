#!/usr/bin/env python3
"""How a NeTEx archive says its modes and its Lines: counts, for the owner
to run on his own machine when the stop extract of a NeTEx feed ends
`no_rail_stop` or `no_stop_place` and the census on the Sessions page is not
enough to tell why.

    python3 netex_mode_census.py <the feed's zip> > census.txt

Send back the whole of `census.txt`.

Read-only and standard library only (Python 3.9 or later): every XML member
of the zip is streamed through expat, never unpacked to disk and never held
whole (a few megabytes of memory whatever the size). It prints counts and
the names of NeTEx elements only, and the values of the mode elements
(`TransportMode`, `VehicleMode`, `StopPlaceType`, the `...Submode`s) when a
value is one word of letters, as the words of an enumeration are; any other
value is counted as `unreadable`. An element name is printed only when it is
one of letters, digits and `_` (at most 64, not starting with a digit), as
NeTEx's are; any other is printed `(odd name)`. Never an id, a name, a URL
or a file name: the members are numbered, not named. A document that
declares an entity is refused, and nothing outside the zip is ever loaded.
The output is UTF-8 whatever the console's encoding (a character it cannot
show is replaced, never a traceback).

What it prints:

1. per member: its size, its root element, how many elements it holds and
   how many of the elements the extractor reads (`Line`, `ServiceJourney`,
   `StopPlace`, ...);
2. the same counts over the whole archive, and the frames;
3. where each mode element appears (the element that holds it, with the
   elements between them) and its values with their counts;
4. how the journeys, patterns and routes reach their Line: for each kind of
   element, how many hold each reference (`LineRef`, `RouteRef`,
   `JourneyPatternRef`, `RouteView/LineRef`, ...) and each mode element.
"""

from __future__ import annotations

import argparse
import contextlib
import re
import sys
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from typing import IO, Any
from xml.parsers import expat  # nosec B406 - entities refused below, nothing external loaded

# The elements the census is about: a path is written from the nearest one.
OWNERS = (
    "Line",
    "FlexibleLine",
    "GroupOfLines",
    "Network",
    "Route",
    "JourneyPattern",
    "ServiceJourneyPattern",
    "ServiceJourney",
    "DatedServiceJourney",
    "TemplateServiceJourney",
    "VehicleJourney",
    "DeadRun",
    "ScheduledStopPoint",
    "PassengerStopAssignment",
    "StopPlace",
    "Quay",
    "VehicleType",
    "TypeOfProductCategory",
    "Operator",
    "Authority",
)
_OWNERS = frozenset(OWNERS)
# The elements whose value is a mode word.
_MODE_NAMES = frozenset({"TransportMode", "VehicleMode", "StopPlaceType", "MainTransportMode"})
# The elements of a reference: their name ends so, or is one of these.
_REF_NAMES = frozenset({"RouteView", "LinesInFrame"})
# Elements between an owner and what is counted, at most.
PATH_DEPTH = 3
# Distinct paths, distinct Frame names, and distinct values of one path, kept
# at most; the others are counted together.
PATHS_MAX = 2000
VALUES_MAX = 30
WORD_MAX = 32
_CHUNK = 1 << 20
OTHER = "(other)"
OTHER_FRAME = "(other)Frame"
UNREADABLE = "unreadable"
EMPTY = "empty"
_LETTERS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_")
# An element name as printed: else `ODD_NAME` (a crafted name could carry
# text that is not an element's name).
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}", re.ASCII)
ODD_NAME = "(odd name)"


class Refused(Exception):
    """A document this census does not read (an entity declared)."""


OTHER_NAMESPACE = "(other ns)"


def _local(name: str) -> str:
    """The local name of a NeTEx element (or of one without a namespace);
    `(other ns)` for an element of any other namespace, `(odd name)` for a
    name that is not of `_NAME`'s form."""
    namespace, _, local = name.rpartition("}")
    if namespace and "netex" not in namespace:
        return OTHER_NAMESPACE
    return local if _NAME.fullmatch(local) else ODD_NAME


def _namespace(name: str) -> str:
    """`netex`, `other` or `none`: never the namespace itself."""
    namespace = name.rpartition("}")[0]
    if not namespace:
        return "none"
    return "netex" if "netex" in namespace else "other"


def word(text: str) -> str:
    """A mode value as printed: one word of letters, else `unreadable`."""
    value = text.strip()
    if not value:
        return EMPTY
    if len(value) <= WORD_MAX and set(value) <= _LETTERS:
        return value
    return UNREADABLE


def _is_counted(local: str) -> bool:
    return local in _MODE_NAMES or local.endswith(("Submode", "Ref")) or local in _REF_NAMES


@dataclass
class _Owner:
    name: str
    depth: int
    holds: set[str] = field(default_factory=set)


@dataclass
class Census:
    """What the whole archive holds."""

    elements: Counter[str] = field(default_factory=Counter)  # owners and frames
    values: dict[str, Counter[str]] = field(default_factory=dict)  # mode path -> values
    refs: Counter[str] = field(default_factory=Counter)  # reference path -> count
    holders: dict[str, Counter[str]] = field(default_factory=dict)  # owner -> held path -> count
    namespaces: Counter[str] = field(default_factory=Counter)
    frames: int = 0  # distinct Frame names in `elements`

    def element(self, local: str) -> str:
        """Count an owner or a frame; past `PATHS_MAX` distinct Frame names,
        a new one counts as `(other)Frame`. The name counted."""
        if local not in self.elements and local not in _OWNERS:
            if self.frames >= PATHS_MAX:
                local = OTHER_FRAME
            else:
                self.frames += 1
        self.elements[local] += 1
        return local

    def value(self, path: str, value: str) -> None:
        if path not in self.values and len(self.values) >= PATHS_MAX:
            path = OTHER
        found = self.values.setdefault(path, Counter())
        if value not in found and len(found) >= VALUES_MAX:
            value = OTHER
        found[value] += 1

    def ref(self, path: str) -> None:
        if path not in self.refs and len(self.refs) >= PATHS_MAX:
            path = OTHER
        self.refs[path] += 1

    def held(self, owner: str, paths: set[str]) -> None:
        found = self.holders.setdefault(owner, Counter())
        for path in paths:
            if path not in found and len(found) >= PATHS_MAX:
                path = OTHER
            found[path] += 1


@dataclass
class Member:
    number: int
    unpacked: int
    packed: int
    root: str = ""
    total: int = 0
    owners: Counter[str] = field(default_factory=Counter)


class _Reader:
    """The expat handlers of one member."""

    def __init__(self, census: Census, member: Member) -> None:
        self.census = census
        self.member = member
        self.stack: list[str] = []  # local names of the open elements
        self.owners: list[_Owner] = []
        self.text: list[str] | None = None  # the text of an open mode element
        self.text_size = 0

    def _path(self, local: str) -> tuple[str, _Owner | None]:
        """The path of `local` from its nearest owner, at most `PATH_DEPTH`
        elements between them."""
        owner = self.owners[-1] if self.owners else None
        start = owner.depth if owner is not None else 0
        between = self.stack[start:-1]
        if len(between) > PATH_DEPTH:
            between = ["...", *between[-PATH_DEPTH:]]
        head = owner.name if owner is not None else "(no owner)"
        return "/".join([head, *between, local]), owner

    def start(self, name: str, _attrs: dict[str, str]) -> None:
        local = _local(name)
        self.census.namespaces[_namespace(name)] += 1
        self.member.total += 1
        if not self.stack:
            self.member.root = local
        self.stack.append(local)
        if local in _OWNERS or local.endswith("Frame"):
            self.member.owners[self.census.element(local)] += 1
        if local in _MODE_NAMES or local.endswith("Submode"):
            self.text, self.text_size = [], 0
        elif _is_counted(local):
            path, owner = self._path(local)
            self.census.ref(path)
            if owner is not None:
                owner.holds.add(path.partition("/")[2])
        if local in _OWNERS:
            self.owners.append(_Owner(local, len(self.stack)))

    def characters(self, data: str) -> None:
        if self.text is not None and self.text_size <= WORD_MAX:
            self.text.append(data)
            self.text_size += len(data)

    def end(self, name: str) -> None:
        local = _local(name)
        if self.text is not None and (local in _MODE_NAMES or local.endswith("Submode")):
            path, owner = self._path(local)
            self.census.value(path, word("".join(self.text)))
            if owner is not None:
                owner.holds.add(path.partition("/")[2])
            self.text = None
        if self.owners and self.owners[-1].depth == len(self.stack):
            done = self.owners.pop()
            self.census.held(done.name, done.holds)
        self.stack.pop()


def _refuse(*_args: Any) -> None:
    raise Refused("the document declares an entity")


def _refuse_external(
    _context: str, _base: str | None, _system_id: str | None, _public_id: str | None
) -> int:
    """expat's `ExternalEntityRefHandler`, of its type: never loads anything."""
    raise Refused("the document names an external entity")


def _parser(reader: _Reader) -> Any:
    parser = expat.ParserCreate(namespace_separator="}")  # nosec B314 - hardened below
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.EntityDeclHandler = _refuse
    parser.ExternalEntityRefHandler = _refuse_external
    parser.buffer_text = True
    parser.StartElementHandler = reader.start
    parser.EndElementHandler = reader.end
    parser.CharacterDataHandler = reader.characters
    return parser


def read_member(source: IO[bytes], census: Census, member: Member) -> None:
    """Count one XML member, streamed."""
    reader = _Reader(census, member)
    parser = _parser(reader)
    while chunk := source.read(_CHUNK):
        parser.Parse(chunk, False)
    parser.Parse(b"", True)


def read_archive(path: str) -> tuple[Census, list[Member], int]:
    """The census of every XML member of the zip at `path`, the members, and
    the number of members that are not XML."""
    census = Census()
    members: list[Member] = []
    others = 0
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            if not info.filename.lower().endswith(".xml"):
                others += 1
                continue
            member = Member(len(members) + 1, info.file_size, info.compress_size)
            members.append(member)
            with zf.open(info) as source:
                read_member(source, census, member)
    return census, members, others


def _counts(counter: Counter[str], names: tuple[str, ...] | None = None) -> str:
    keys = names if names is not None else tuple(sorted(counter))
    return ", ".join(f"{name} {counter[name]:,}" for name in keys if names is None or counter[name])


def report(census: Census, members: list[Member], others: int) -> list[str]:
    """The lines to print."""
    lines = [f"netex mode census: {len(members)} XML members, {others} other members"]
    lines.append("== members")
    for m in members:
        lines.append(
            f"member {m.number}: {m.unpacked:,} bytes unpacked ({m.packed:,} packed), "
            f"root {m.root or '(none)'}, {m.total:,} elements; "
            f"{_counts(m.owners, OWNERS) or 'none of the counted elements'}"
        )
    lines.append("== elements over the archive")
    lines.extend(f"{name} {census.elements[name]:,}" for name in OWNERS)
    frames = Counter({k: v for k, v in census.elements.items() if k.endswith("Frame")})
    lines.append(f"frames: {_counts(frames) or 'none'}")
    lines.append(f"namespaces of the elements: {_counts(census.namespaces) or 'none'}")
    lines.append("== mode elements: where they are, and their values")
    for path in sorted(census.values):
        lines.append(f"{path}: {_counts(census.values[path])}")
    lines.append("== references: where they are")
    lines.extend(f"{path} {count:,}" for path, count in sorted(census.refs.items()))
    lines.append("== what each kind of element holds (elements holding it at least once)")
    for owner in OWNERS:
        held = census.holders.get(owner)
        if held:
            lines.append(f"{owner} ({census.elements[owner]:,}): {_counts(held)}")
    return lines


def _utf8(stream: IO[str]) -> IO[str]:
    """`stream` writing UTF-8, a character it cannot write replaced: a
    console of another encoding (a Windows code page) never ends the census
    with a traceback."""
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        # A stream that cannot change is written as it is.
        with contextlib.suppress(ValueError, OSError):
            reconfigure(encoding="utf-8", errors="replace")
    return stream


def main(argv: list[str] | None = None, out: IO[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("archive", help="the NeTEx zip")
    args = parser.parse_args(argv)
    stream = _utf8(out if out is not None else sys.stdout)
    try:
        census, members, others = read_archive(args.archive)
    except Exception as exc:  # any end, an encrypted or oddly packed member too: its type only,
        # never a traceback, which would name the member
        print(f"netex mode census: cannot read the archive ({type(exc).__name__})", file=stream)
        return 1
    for line in report(census, members, others):
        print(line, file=stream)
    return 0


if __name__ == "__main__":
    sys.exit(main())
