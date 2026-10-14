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
    `RATIO_FLOOR_BYTES`, at most `ARCHIVE_MAX_BYTES`."""
    packed = sum(info.compress_size for info in zf.infolist())
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
            raise ArchiveError("bad_archive", "a member packed otherwise than deflate")
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
