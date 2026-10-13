"""What the GTFS and NeTEx readers of the stop extract share.

A reader turns one timetable archive into `Station`s: one per station-level
stop at which at least one rail trip calls (MSMM `docs/service-design.md`,
22.2, points 1 to 3), with the raw values it saw. Cleaning to MSMM's rules
happens once, when `app/station_extract.py` writes the file.

The readers bound what they read, not only what they write (review of pull
request 4): the bytes unpacked from each member and from the archive, the
size of a record, the number of stations and of codes per station. Past a
bound they raise `ArchiveError`; nothing grows with a hostile input.
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
# reading pass. A national feed is a few GB unpacked; a zip bomb stops here.
MEMBER_MAX_BYTES = 4 * GIGABYTE
ARCHIVE_MAX_BYTES = 8 * GIGABYTE
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


class Budget:
    """The bytes unpacked in one reading pass of an archive."""

    def __init__(self, total: int | None = None) -> None:
        self.total = ARCHIVE_MAX_BYTES if total is None else total
        self.used = 0


class _Counted(io.RawIOBase):
    """A zip member that counts the bytes really unpacked (a header can lie)
    and refuses past `MEMBER_MAX_BYTES` or the pass's budget."""

    def __init__(self, raw: IO[bytes], budget: Budget) -> None:
        self._raw = raw
        self._budget = budget
        self._used = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: memoryview) -> int:  # type: ignore[override]
        data = self._raw.read(len(buffer))
        self._used += len(data)
        self._budget.used += len(data)
        if self._used > MEMBER_MAX_BYTES or self._budget.used > self._budget.total:
            raise ArchiveError("input_too_large", "the archive unpacks past VIATOR's bound")
        buffer[: len(data)] = data
        return len(data)

    def close(self) -> None:
        self._raw.close()
        super().close()


def open_member(zf: zipfile.ZipFile, name: str, budget: Budget) -> IO[bytes]:
    """A member of `zf`, read through the byte counter of `budget`."""
    return io.BufferedReader(_Counted(zf.open(name), budget), MEGABYTE)


def coordinate(text: str | None) -> float | None:
    """A finite decimal number, or None for anything else (empty, `nan`,
    `inf`, words)."""
    try:
        value = float((text or "").strip())
    except ValueError:
        return None
    return value if math.isfinite(value) else None
