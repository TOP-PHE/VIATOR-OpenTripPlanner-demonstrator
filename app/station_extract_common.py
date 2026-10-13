"""What the GTFS and NeTEx readers of the stop extract share.

A reader turns one timetable archive into `Station`s: one per station-level
stop at which at least one rail trip calls (MSMM `docs/service-design.md`,
22.2, points 1 to 3), with the raw values it saw. Cleaning to MSMM's rules
happens once, when `app/station_extract.py` writes the file.
"""

from __future__ import annotations

import collections
import math
import zipfile
from dataclasses import dataclass, field

# The two modes MSMM takes (22.2, point 6).
RAIL_MAIN = "RAIL_MAIN"
RAIL_SUBURBAN = "RAIL_SUBURBAN"


class ArchiveError(Exception):
    """The archive cannot be read as the format it was declared as. `reason`
    is a short word: `bad_archive` or `unsupported_format`."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclass
class Station:
    """One station-level stop, as a reader saw it.

    `codes` keeps the order codes were first seen in, the station's own
    first; a code is a (field, value) pair. `calls` counts rail calls by mode.
    """

    key: str
    name: str = ""
    lat: float | None = None
    lon: float | None = None
    calls: collections.Counter[str] = field(default_factory=collections.Counter)
    operators: set[str] = field(default_factory=set)
    codes: dict[tuple[str, str], None] = field(default_factory=dict)

    def add_code(self, name: str, value: str | None) -> None:
        value = (value or "").strip()
        if value:
            self.codes.setdefault((name, value), None)

    def code_pairs(self) -> list[tuple[str, str]]:
        """The codes as (field, value) pairs, in the order first seen."""
        return list(self.codes)

    def place(self, name: str, lat: float | None, lon: float | None) -> None:
        """Name and position, the first ones given win."""
        if name and not self.name:
            self.name = name
        if lat is not None and lon is not None and self.lat is None:
            self.lat, self.lon = lat, lon


def members(zf: zipfile.ZipFile) -> dict[str, str]:
    """Base name in small letters -> member name, the shallowest one winning
    (a feed zipped with its folder keeps its files one level down)."""
    found: dict[str, str] = {}
    for name in sorted(zf.namelist(), key=lambda n: (n.count("/"), n)):
        if name.endswith("/"):
            continue
        found.setdefault(name.rsplit("/", 1)[-1].lower(), name)
    return found


def coordinate(text: str | None) -> float | None:
    """A finite decimal number, or None for anything else (empty, `nan`,
    `inf`, words)."""
    try:
        value = float((text or "").strip())
    except ValueError:
        return None
    return value if math.isfinite(value) else None
