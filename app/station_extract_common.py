"""What the GTFS and NeTEx readers of the stop extract share.

A reader turns one timetable archive into `Station`s: one per station-level
stop at which at least one rail trip calls (MSMM `docs/service-design.md`,
22.2, points 1 to 3), with the raw values it saw. Cleaning to MSMM's rules
happens once, when `app/station_extract.py` writes the file.

The readers bound what they read, not only what they write (review of pull
request 4): the bytes unpacked from each member and from the archive, the
size of a record, the number of stations and of codes per station. Past a
bound they raise `ArchiveError`; nothing grows with a hostile input.

**The unpacked bytes.** Nothing is ever written unpacked: every member is
streamed, so the bytes a member unpacks to cost processor time, not disk or
memory, and the child process's processor-time limit
(`station_extract.CHILD_CPU_SECONDS`) is the last stop. The bounds below stop
a zip bomb early and accept a national feed (a NeTEx of 2 GB zipped unpacks
to some 40 GB, a GTFS `stop_times.txt` to several GB):

- a member is stored or deflated (`PACKINGS`): bzip2 and lzma pack a run of
  the same bytes tens of thousands of times, deflate at most about 1,000
  times; another packing is `bad_archive`;
- a member read may unpack to at most `RATIO_MAX` (200) times its packed
  size (`RATIO_FLOOR_BYTES` at least, so that a small member is never
  refused). Timetable XML and CSV pack 5 to 30 times, the most regular ones
  some 60 times; only a crafted member nears deflate's 1,000;
- the members read together may unpack to at most 200 times the packed
  bytes of the whole archive (64 MiB at least), so that many small members
  cannot each use the floor;
- a member and one reading pass at most 160 GiB (`MEMBER_MAX_BYTES`,
  `ARCHIVE_MAX_BYTES`): a national NeTEx of 2 GB zipped packing 33 times is
  some 66 GB, and the child reads about 60 MB/s, so its 40 minutes of
  processor time end near 150 GB anyway.

The members a reader never reads (a PDF, a readme beside the timetable) are
never checked. Every bound is checked first on the sizes the zip's central
directory declares (`check_declared`), so an archive that says it is too
large is refused at once, before any reading; zipfile never gives more bytes
of a member than its declared size (and checks its CRC), and the bytes really
unpacked are counted all the same.
"""

from __future__ import annotations

import collections
import io
import math
import re
import zipfile
from dataclasses import dataclass, field
from typing import IO

# The two modes MSMM takes (22.2, point 6).
RAIL_MAIN = "RAIL_MAIN"
RAIL_SUBURBAN = "RAIL_SUBURBAN"

MEGABYTE = 1024 * 1024
GIGABYTE = 1024 * MEGABYTE
# The bytes unpacked from one member, and from the whole archive in one
# reading pass (see the module's text for the numbers).
MEMBER_MAX_BYTES = 160 * GIGABYTE
ARCHIVE_MAX_BYTES = 160 * GIGABYTE
RATIO_MAX = 200
RATIO_FLOOR_BYTES = 64 * MEGABYTE
# The packings of a member VIATOR reads: stored, deflated.
PACKINGS = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})
# The stations a reader keeps (MSMM takes 200,000 rows), and the raw codes of
# one station it keeps (MSMM takes 500; twice that leaves room for the codes
# cleaning drops, the rest is counted).
STATIONS_MAX = 200_000
RAW_CODES_MAX = 1_000


class ArchiveError(Exception):
    """The archive cannot be read, or would take more than a bound. `reason`
    is a short word: `bad_archive`, `input_too_large`, `too_many_rows`,
    `no_stop_place`."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclass
class Station:
    """One station-level stop, as a reader saw it.

    `codes` keeps the order codes were first seen in, the station's own
    first; a code is a (field, value) pair, at most `RAW_CODES_MAX` of them,
    the others counted in `codes_over_cap`. `calls` counts rail calls by mode.
    """

    key: str
    name: str = ""
    lat: float | None = None
    lon: float | None = None
    calls: collections.Counter[str] = field(default_factory=collections.Counter)
    operators: set[str] = field(default_factory=set)
    codes: dict[tuple[str, str], None] = field(default_factory=dict)
    codes_over_cap: int = 0

    def add_code(self, name: str, value: str | None) -> None:
        value = (value or "").strip()
        if not value or (name, value) in self.codes:
            return
        if len(self.codes) >= RAW_CODES_MAX:
            self.codes_over_cap += 1
            return
        self.codes[(name, value)] = None

    def code_pairs(self) -> list[tuple[str, str]]:
        """The codes as (field, value) pairs, in the order first seen."""
        return list(self.codes)

    def place(self, name: str, lat: float | None, lon: float | None) -> None:
        """Name and position, the first ones given win."""
        if name and not self.name:
            self.name = name
        if lat is not None and lon is not None and self.lat is None:
            self.lat, self.lon = lat, lon


def new_station(stations: dict[str, Station], key: str) -> Station:
    """The station of `key`, made when new; past `STATIONS_MAX` the archive is
    refused before it takes more memory."""
    station = stations.get(key)
    if station is None:
        if len(stations) >= STATIONS_MAX:
            raise ArchiveError("too_many_rows", "more stations than MSMM takes")
        station = stations[key] = Station(key=key)
    return station


def members(zf: zipfile.ZipFile) -> dict[str, str]:
    """Base name in small letters -> member name, the shallowest one winning
    (a feed zipped with its folder keeps its files one level down)."""
    found: dict[str, str] = {}
    for name in sorted(zf.namelist(), key=lambda n: (n.count("/"), n)):
        if name.endswith("/"):
            continue
        found.setdefault(name.rsplit("/", 1)[-1].lower(), name)
    return found


def member_bound(info: zipfile.ZipInfo) -> int:
    """The most bytes the member `info` may unpack to: `RATIO_MAX` times its
    packed size, at least `RATIO_FLOOR_BYTES`, at most `MEMBER_MAX_BYTES`."""
    return min(MEMBER_MAX_BYTES, max(RATIO_FLOOR_BYTES, RATIO_MAX * info.compress_size))


_TOO_LARGE_MEMBER = "a member unpacks past VIATOR's bound"
_TOO_LARGE_ARCHIVE = "the archive unpacks past VIATOR's bound"


def archive_bound(zf: zipfile.ZipFile) -> int:
    """The most bytes the members read together may unpack to: `RATIO_MAX`
    times the packed bytes of the whole archive, at least
    `RATIO_FLOOR_BYTES`, at most `ARCHIVE_MAX_BYTES`. The packed bytes are
    those the central directory declares, never more than lie before it
    (`start_dir`, its offset in the file): a member never opened is never
    checked, so its declared size is not believed past the file itself."""
    declared = sum(info.compress_size for info in zf.infolist())
    packed = min(declared, max(0, int(getattr(zf, "start_dir", declared))))
    return min(ARCHIVE_MAX_BYTES, max(RATIO_FLOOR_BYTES, RATIO_MAX * packed))


def check_declared(zf: zipfile.ZipFile, names: list[str]) -> None:
    """Refuse at once, before reading, an archive whose central directory
    declares a member of `names` packed otherwise than stored or deflated
    (`bad_archive`), larger than its bound, or more bytes in all than the
    archive's bound (`input_too_large`)."""
    total = 0
    for name in names:
        info = zf.getinfo(name)
        if info.compress_type not in PACKINGS:
            raise ArchiveError("bad_archive", "a member neither stored nor deflated")
        if info.file_size > member_bound(info):
            raise ArchiveError("input_too_large", _TOO_LARGE_MEMBER)
        total += info.file_size
    if total > archive_bound(zf):
        raise ArchiveError("input_too_large", _TOO_LARGE_ARCHIVE)


class Budget:
    """The bytes unpacked in one reading pass of an archive."""

    def __init__(self, total: int | None = None) -> None:
        self.total = ARCHIVE_MAX_BYTES if total is None else total
        self.used = 0


class _Counted(io.RawIOBase):
    """A zip member that counts the bytes really unpacked (a header can lie)
    and refuses past the member's bound (`member_bound`) or the pass's
    budget."""

    def __init__(self, raw: IO[bytes], budget: Budget, limit: int) -> None:
        self._raw = raw
        self._budget = budget
        self._limit = limit
        self._used = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: memoryview) -> int:  # type: ignore[override]
        data = self._raw.read(len(buffer))
        self._used += len(data)
        self._budget.used += len(data)
        if self._used > self._limit:
            raise ArchiveError("input_too_large", _TOO_LARGE_MEMBER)
        if self._budget.used > self._budget.total:
            raise ArchiveError("input_too_large", _TOO_LARGE_ARCHIVE)
        buffer[: len(data)] = data
        return len(data)

    def close(self) -> None:
        self._raw.close()
        super().close()


def open_member(zf: zipfile.ZipFile, name: str, budget: Budget) -> IO[bytes]:
    """A member of `zf`, read through the byte counter of `budget` and the
    member's own bound."""
    info = zf.getinfo(name)
    return io.BufferedReader(_Counted(zf.open(info), budget, member_bound(info)), MEGABYTE)


def coordinate(text: str | None) -> float | None:
    """A finite decimal number, or None for anything else (empty, `nan`,
    `inf`, words)."""
    try:
        value = float((text or "").strip())
    except ValueError:
        return None
    return value if math.isfinite(value) else None


# ── The census: what a reader saw, for a feed that gives no extract ──────

# When a feed ends `no_rail_stop` or `no_stop_place`, the readers say what
# they saw (`Census`): a short summary for the admin status and the
# remembered failure (`SUMMARY_MAX` characters), the whole census for the
# web log. Counts and enumerated words only, never an id, a name or a URL: a
# value read from the feed (a NeTEx `TransportMode`, a GTFS `route_type`) is
# kept only when it is one word of letters and `_` (digits for a
# `route_type`) of at most `CENSUS_VALUE_MAX` characters, as the words of an
# enumeration are, and counted as `unreadable` otherwise (an id, a URL, a
# text with spaces are never cut down to a part of themselves); at most
# `CENSUS_VALUES_MAX` distinct values are kept, the others counted together.
# Cheap: a few counters, never one entry per element.
CENSUS_VALUES_MAX = 8
CENSUS_VALUE_MAX = 24
SUMMARY_MAX = 100
NO_VALUE = "none"  # no value given
NOT_A_WORD = "unreadable"  # a value that is not one such word
OTHER_VALUES = "other_values"  # past `CENSUS_VALUES_MAX` distinct values
# What a summary may hold, checked again wherever one is read back.
SUMMARY = re.compile(r"[\w ,:()]{1,100}", re.ASCII)
CENSUS_REASONS = frozenset({"no_rail_stop", "no_stop_place"})
_WORD = re.compile(rf"[A-Za-z_]{{1,{CENSUS_VALUE_MAX}}}", re.ASCII)
_NUMBER = re.compile(rf"\d{{1,{CENSUS_VALUE_MAX}}}", re.ASCII)


class ValueTally:
    """Counts by value, the values checked and bounded (see above)."""

    def __init__(self, *, digits: bool = False) -> None:
        self.counts: dict[str, int] = {}
        self._form = _NUMBER if digits else _WORD
        self._between = ": " if digits else " "

    @property
    def total(self) -> int:
        return sum(max(0, count) for count in self.counts.values())

    def add(self, raw: str, count: int = 1) -> None:
        value = NO_VALUE
        if raw:
            value = raw if self._form.fullmatch(raw) else NOT_A_WORD
        if value not in self.counts and len(self.counts) >= CENSUS_VALUES_MAX:
            value = OTHER_VALUES
        self.counts[value] = self.counts.get(value, 0) + count

    def top(self) -> list[tuple[str, int]]:
        """The (value, count) pairs with a count, the most frequent first."""
        found = [item for item in self.counts.items() if item[1] > 0]
        return sorted(found, key=lambda item: (-item[1], item[0]))

    def text(self, limit: int) -> str:
        """The `limit` most frequent values: "bus 3,000, none 210" ("3: 38,
        700: 2" for numbers)."""
        return ", ".join(f"{value}{self._between}{count:,}" for value, count in self.top()[:limit])


def _field(value: object) -> str:
    """A value of the whole census: a number, `value:count,...`, or `unread`."""
    if value is None:
        return "unread"
    if isinstance(value, ValueTally):
        value = dict(value.top())
    if isinstance(value, dict):
        return ",".join(f"{word}:{count}" for word, count in value.items()) or "0"
    return str(value)


def pack(segments: list[list[str]]) -> str:
    """The segments joined with ", " within `SUMMARY_MAX` characters: of each
    segment, its first form that still fits; a segment none of whose forms
    fits is left out."""
    parts: list[str] = []
    for forms in segments:
        for form in forms:
            if len(", ".join([*parts, form])) <= SUMMARY_MAX:
                parts.append(form)
                break
    return ", ".join(parts)


def tally_forms(count: int, noun: str, tally: ValueTally, label: str = "") -> list[str]:
    """`count noun (label v1 n1, v2 n2, v3 n3)`, with fewer values when it is
    too long, last without any."""
    forms = [f"{count:,} {noun} ({label}{tally.text(n)})" for n in (3, 2, 1) if tally.top()]
    return [*forms, f"{count:,} {noun}"]


@dataclass
class Census:
    """What a reader saw, and what writing made of it: `stations` the
    stations read, `dropped` the rows not written by reason word (the
    reasons that drop a row only). The readers fill their own fields."""

    stations: int | None = None
    dropped: collections.Counter[str] = field(default_factory=collections.Counter)

    def segments(self) -> list[list[str]]:
        """The summary's segments, most telling first (one list of forms each)."""
        return [self.stations_forms()]

    def fields(self) -> list[tuple[str, object]]:
        """The whole census, as (name, value) pairs."""
        return [("stations", self.stations), ("dropped", dict(self.dropped))]

    def stations_forms(self) -> list[str]:
        if self.stations is None:
            return []
        plain = f"{self.stations:,} stations"
        if not self.dropped:
            return [plain]
        most = ", ".join(f"{word} {count:,}" for word, count in self.dropped.most_common(2))
        return [f"{plain} (not written: {most})", plain]

    def default_summary(self) -> str:
        """What rail by default made rail, for the admin status of a written
        extract (`station_extract.ExtractResult.summary`); '' when it made
        nothing rail or the setting is off. The readers say it."""
        return ""

    def summary(self) -> str:
        """At most `SUMMARY_MAX` characters, for the admin status."""
        return pack(self.segments())

    def full(self) -> str:
        """Every count, `name=value` separated by spaces, for the web log."""
        return " ".join(f"{name}={_field(value)}" for name, value in self.fields())
