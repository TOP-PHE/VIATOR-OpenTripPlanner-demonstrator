"""A trimmed copy of a GTFS zip, for MOTIS.

Renfe Cercanías (`renfe-cerc`, measured 2026-10-29) pads every line of every
file with spaces to about 150 characters: calendar.txt's header ends in
`end_date` followed by spaces, routes.txt's ids read `10T0001C1  ` while
trips.txt names `10T0001C1`, and the last field of every file is padded.
MOTIS (nigiri's GTFS loader) matches column names and ids exactly and trims
nothing: no service had an end date, no trip found its route, and the feed
served no station, with no error in `logs/tt.txt`.

`clean_zip` writes a copy in which every field of a padded member is
stripped of the spaces and tabs around it. A member is padded when one of its
first `_PROBE_ROWS` rows has such a field; the other members are copied byte
for byte, and a zip with no padded member is not copied at all (None). The
csv module reads and writes the rows, so quoted fields keep their commas and
quotes; bytes that are not UTF-8 pass through unchanged (surrogateescape).

Only the MOTIS build uses the copy (`worker._motis_timetable_path`); the
inbox keeps the published file, which OTP reads as it is.
"""

from __future__ import annotations

import csv
import io
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

# Bumped whenever the output changes, so cached copies are redone.
CLEANER_VERSION = 1

_CHUNK = 8 * 1024 * 1024
_PROBE_ROWS = 20
_BLANKS = " \t"


@dataclass
class CleanStats:
    members: list[str] = field(default_factory=list)  # members trimmed
    fields: int = 0  # fields that lost spaces
    failed: list[str] = field(default_factory=list)  # padded, left as published

    def summary(self) -> str:
        names = ", ".join(self.members[:4]) + (", ..." if len(self.members) > 4 else "")
        text = f"{self.fields} padded fields trimmed in {len(self.members)} files ({names})"
        if self.failed:
            text += f"; {len(self.failed)} left as published ({', '.join(self.failed[:4])})"
        return text


def _text(raw: IO[bytes]) -> io.TextIOWrapper:
    return io.TextIOWrapper(raw, encoding="utf-8-sig", errors="surrogateescape", newline="")


def _padded(row: list[str]) -> bool:
    return any(f != f.strip(_BLANKS) for f in row)


def _is_padded(zin: zipfile.ZipFile, info: zipfile.ZipInfo) -> bool:
    """True when one of the member's first rows has a field with spaces around it."""
    with zin.open(info) as raw:
        try:
            for i, row in enumerate(csv.reader(_text(raw))):
                if _padded(row):
                    return True
                if i + 1 >= _PROBE_ROWS:
                    return False
        except csv.Error:  # unreadable as CSV: MOTIS's business, not ours
            return False
    return False


def _trim(zin: zipfile.ZipFile, info: zipfile.ZipInfo, out: IO[bytes]) -> int:
    """Write the member to `out` with every field trimmed; how many changed.
    Raises csv.Error on a member the csv module cannot read."""
    changed = 0
    with zin.open(info) as raw:
        text_out = io.TextIOWrapper(out, encoding="utf-8", errors="surrogateescape", newline="")
        writer = csv.writer(text_out, lineterminator="\n")
        for row in csv.reader(_text(raw)):
            if not row:
                continue
            trimmed = [f.strip(_BLANKS) for f in row]
            changed += sum(a != b for a, b in zip(row, trimmed, strict=True))
            writer.writerow(trimmed)
        text_out.flush()
        text_out.detach()
    return changed


def clean_zip(src: Path, dest: Path) -> CleanStats | None:
    """Write a copy of the GTFS zip `src` to `dest` with its padded members
    trimmed. Returns None — and leaves `dest` absent — when no member is
    padded (or none could be trimmed)."""
    with zipfile.ZipFile(src) as zin:
        padded = {
            info.filename
            for info in zin.infolist()
            if info.filename.lower().endswith(".txt") and _is_padded(zin, info)
        }
        if not padded:
            return None
        stats = CleanStats()
        tmp = dest.with_name(dest.name + ".part")
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                if info.filename in padded and _trimmed(zin, zout, info, stats, dest.parent):
                    continue
                with zin.open(info) as raw, zout.open(info.filename, "w", force_zip64=True) as fout:
                    shutil.copyfileobj(raw, fout, _CHUNK)
    if not stats.members:
        tmp.unlink(missing_ok=True)
        return None
    tmp.replace(dest)
    return stats


def _trimmed(
    zin: zipfile.ZipFile,
    zout: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    stats: CleanStats,
    work: Path,
) -> bool:
    """Write the member trimmed; False, writing nothing, when the csv module
    cannot read it (e.g. a field over its size limit): it is then copied as
    published. Trimmed into a temporary file first, so a failure halfway
    leaves no half member in the copy."""
    with tempfile.TemporaryFile(dir=work) as spool:
        try:
            changed = _trim(zin, info, spool)
        except csv.Error:
            stats.failed.append(info.filename)
            return False
        spool.seek(0)
        with zout.open(info.filename, "w", force_zip64=True) as fout:
            shutil.copyfileobj(spool, fout, _CHUNK)
    stats.fields += changed
    stats.members.append(info.filename)
    return True
