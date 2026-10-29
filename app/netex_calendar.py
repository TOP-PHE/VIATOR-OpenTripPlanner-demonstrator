"""Rewrite a NeTEx calendar into the one model MOTIS reads.

NeTEx allows several equivalent calendar models. MOTIS (nigiri's NeTEx loader)
reads exactly one: `UicOperatingPeriod` with `ValidDayBits`, each
`DayTypeAssignment` pointing at one by `OperatingPeriodRef` — the Swiss
profile it was built on. It keeps only the FIRST assignment of each day type
(`days.emplace`) and reads one without `OperatingPeriodRef` as "every day".
Other feeds describe the same days differently:

  * `DayTypeAssignment` → `OperatingDayRef` (one dated `OperatingDay`) or a
    bare `Date`;
  * `DayTypeAssignment` → `OperatingPeriodRef` to a plain `OperatingPeriod`
    (`FromDate`/`ToDate` or `FromOperatingDayRef`/`ToOperatingDayRef`),
    narrowed by the `DayType`'s `DaysOfWeek`;
  * `isAvailable="false"` assignments that remove days;
  * a day type given a `UicOperatingPeriod` AND single dates (NMBS,
    2026-10-29: each of its 59 838 day types has a period whose ValidDayBits
    are all 0, then one `Date` assignment per service day, 5 million in all).
    MOTIS keeps the empty period: not one Belgian train ran.

MOTIS resolves an `OperatingPeriodRef` only among `UicOperatingPeriod`s and
drops the WHOLE file on the first reference it cannot resolve, printing one
`ERROR:` line and carrying on (2026-10-02: Slovenia's 495 MB national file
loaded zero stops). An `OperatingDayRef` it would silently read as "every day".

`convert_zip` computes each day type's actual service days and writes them back
as one `UicOperatingPeriod` + `ValidDayBits` and one `DayTypeAssignment` per
day type, inside each `ServiceCalendarFrame`. Everything else in the file is
streamed through byte for byte. Feeds already in the Swiss model are left
alone (`convert_zip` returns None). A frame larger than `_FRAME_IN_MEMORY`
(NMBS's is 1.1 GB) is copied to a temporary file and read from it as a
stream instead of being parsed whole.

The same pass repairs the forms MOTIS drops a whole file for
(app/netex_motis_repair.py; ÖBB, 2026-10-28), and renames the shared files a
zip's line files take their day types from, so that MOTIS reads them as base
files (NMBS, 2026-10-29).

Only the MOTIS build uses the converted copy (`worker.run_build_motis`); the
inbox keeps the published file, which OTP reads as it is.
"""

from __future__ import annotations

import io
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import IO
from xml.etree import ElementTree as ET
from xml.sax.saxutils import quoteattr

from defusedxml.ElementTree import ParseError
from defusedxml.ElementTree import fromstring as _xml_fromstring
from defusedxml.ElementTree import iterparse as _xml_iterparse

from . import netex_motis_repair

# Bumped whenever the output changes, so cached conversions are redone.
CONVERTER_VERSION = 5

_CHUNK = 8 * 1024 * 1024
# A ServiceCalendarFrame larger than this is converted from a temporary file,
# read as a stream, instead of being parsed whole (NMBS: 1.1 GB, 5 million
# DayTypeAssignments, several GB as a tree).
_FRAME_IN_MEMORY = 64 * 1024 * 1024
_FRAME_START_RE = re.compile(rb"<(?:([A-Za-z_][\w.-]*):)?ServiceCalendarFrame\b")
_ROOT_RE = re.compile(rb"<(?:[A-Za-z_][\w.-]*:)?PublicationDelivery\b[^>]*>", re.DOTALL)
_XMLNS_RE = re.compile(rb"""\sxmlns(?::[\w.-]+)?=(?:"[^"]*"|'[^']*')""")
# A start tag split across two chunks is never longer than this.
_TAIL_KEEP = 256
_WRAP_END = b"</viator-wrap>"
_NONZERO_RE = re.compile(r"[^0]")
_BIT_TEXT = bytes.maketrans(b"\x00\x01", b"01")

_WEEKDAYS = {
    "monday": {0},
    "tuesday": {1},
    "wednesday": {2},
    "thursday": {3},
    "friday": {4},
    "saturday": {5},
    "sunday": {6},
    "weekdays": {0, 1, 2, 3, 4},
    "weekend": {5, 6},
    "everyday": {0, 1, 2, 3, 4, 5, 6},
}


@dataclass
class FrameStats:
    day_types: int = 0
    assignments_in: int = 0
    unresolved: int = 0  # assignments whose day or period could not be found
    streamed: bool = False  # read from a temporary file, not parsed whole
    out_of_order: int = 0  # streamed assignments listed before a lower `order`


@dataclass
class ConvertStats:
    frames: list[FrameStats] = field(default_factory=list)
    repairs: netex_motis_repair.RepairStats = field(default_factory=netex_motis_repair.RepairStats)
    base_files: list[str] = field(default_factory=list)  # shared members MOTIS reads first

    def summary(self) -> str:
        parts = []
        if self.frames:
            parts.append(self._calendar_summary())
        if self.base_files:
            names = ", ".join(self.base_files[:3])
            more = f" and {len(self.base_files) - 3} more" if len(self.base_files) > 3 else ""
            parts.append(f"{len(self.base_files)} shared files read as base files ({names}{more})")
        if repaired := self.repairs.summary():
            parts.append(repaired)
        return "; ".join(parts)

    def _calendar_summary(self) -> str:
        dt = sum(f.day_types for f in self.frames)
        a = sum(f.assignments_in for f in self.frames)
        u = sum(f.unresolved for f in self.frames)
        o = sum(f.out_of_order for f in self.frames)
        s = sum(f.streamed for f in self.frames)
        notes = [
            text
            for count, text in (
                (u, f"{u} unresolved, ignored"),
                (o, f"{o} out of order, applied as listed"),
                (s, f"{s} large calendar frames read from disk"),
            )
            if count
        ]
        text = f"{a} day-type assignments rewritten as {dt} UicOperatingPeriod"
        return text + (f" ({', '.join(notes)})" if notes else "")


# ─────────────────────────── calendar model ───────────────────────────


def _local(el: ET.Element) -> str:
    return el.tag.rsplit("}", 1)[-1]


def _child(el: ET.Element, name: str) -> ET.Element | None:
    return next((c for c in el if _local(c) == name), None)


def _text(el: ET.Element, name: str) -> str:
    c = _child(el, name)
    return (c.text or "").strip() if c is not None else ""


def _ref(el: ET.Element, name: str) -> str:
    c = _child(el, name)
    return c.get("ref", "") if c is not None else ""


def _date(s: str) -> date | None:
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def _order(a: ET.Element) -> int:
    order = a.get("order") or ""
    return int(order) if order.isdigit() else 0


def _weekdays(day_type: ET.Element) -> set[int] | None:
    """Weekdays a DayType is restricted to, or None for no restriction."""
    days: set[int] = set()
    for el in day_type.iter():
        if _local(el) == "DaysOfWeek":
            for token in (el.text or "").split():
                days |= _WEEKDAYS.get(token.lower(), set())
    return days or None


def _is_available(a: ET.Element) -> bool:
    return (a.get("isAvailable") or _text(a, "isAvailable") or "true") != "false"


@dataclass(slots=True)
class _Period:
    start: date | None
    end: date | None
    from_day: str  # FromOperatingDayRef, when there is no FromDate
    to_day: str
    bits: str
    uic: bool  # a UicOperatingPeriod (MOTIS reads it), not a plain OperatingPeriod


class _DayBits:
    """A day type's service days: one byte per day, from date ordinal `base`."""

    __slots__ = ("base", "days")

    def __init__(self) -> None:
        self.base = 0
        self.days = bytearray()

    def set(self, ordinal: int, on: bool) -> None:
        if not self.days:
            if on:
                self.base, self.days = ordinal, bytearray(b"\x01")
            return
        i = ordinal - self.base
        if not 0 <= i < len(self.days):
            if not on:
                return
            if i < 0:
                self.days[0:0] = bytes(-i)
                self.base, i = ordinal, 0
            else:
                self.days.extend(bytes(i + 1 - len(self.days)))
        self.days[i] = int(on)

    def span(self) -> tuple[date, str] | None:
        """(first day, ValidDayBits from it to the last day), None when it never runs."""
        first = self.days.find(1)
        if first < 0:
            return None
        last = self.days.rfind(1)
        bits = bytes(self.days[first : last + 1]).translate(_BIT_TEXT).decode()
        return date.fromordinal(self.base + first), bits


class _Calendar:
    """One ServiceCalendarFrame's calendar, fed element by element.

    `define` keeps what assignments use (OperatingDay, OperatingPeriod,
    UicOperatingPeriod, DayType). `note` records what MOTIS would make of an
    assignment, `apply` adds its days to (or removes them from) its day type.
    Assignments are applied in the order they are given: `convert_frame`
    sorts them by `order`; a frame read as a stream applies them as listed,
    and must define what they name before them, the schema's order."""

    def __init__(self) -> None:
        self.op_days: dict[str, date] = {}
        self.periods: dict[str, _Period] = {}
        self.weekdays: dict[str, set[int] | None] = {}  # every DayType defined here
        self.days: dict[str, _DayBits] = {}
        self.stats = FrameStats()
        self._by_operating_day = False
        self._period_refs: list[tuple[str, str]] = []  # (DayTypeRef, OperatingPeriodRef)
        self._dated: set[str] = set()  # day types assigned single dates

    def define(self, el: ET.Element) -> bool:
        """Keep `el` if assignments may name it; True if it was such a definition."""
        name = _local(el)
        ident = el.get("id", "")
        if name == "OperatingDay":
            if d := _date(_text(el, "CalendarDate")):
                self.op_days[ident] = d
        elif name in ("OperatingPeriod", "UicOperatingPeriod"):
            self.periods[ident] = _Period(
                start=_date(_text(el, "FromDate")),
                end=_date(_text(el, "ToDate")),
                from_day=_ref(el, "FromOperatingDayRef"),
                to_day=_ref(el, "ToOperatingDayRef"),
                bits=_text(el, "ValidDayBits"),
                uic=name == "UicOperatingPeriod",
            )
        elif name == "DayType":
            self.weekdays[ident] = _weekdays(el)
        else:
            return False
        return True

    def note(self, a: ET.Element) -> None:
        self.stats.assignments_in += 1
        dt = _ref(a, "DayTypeRef")
        if _ref(a, "OperatingDayRef"):
            self._by_operating_day = True
        if period := _ref(a, "OperatingPeriodRef"):
            self._period_refs.append((dt, period))
        elif _text(a, "Date"):
            self._dated.add(dt)

    def needs_conversion(self) -> bool:
        """True only on positive evidence, in this frame, of a model MOTIS
        misreads: an assignment by `OperatingDayRef`; one by `OperatingPeriodRef`
        to a plain `OperatingPeriod` defined here; or a day type given both a
        period and single dates, when every period named is defined here (NMBS).

        A reference to a period defined in another frame or file (DB splits its
        data over ~28 000 files) is never a reason to rewrite: the frame cannot
        resolve it either way, and rewriting would empty those day types. Nor
        are dates alone (SBB's holiday day types, whose journeys take their days
        from AvailabilityConditions). Measured 2026-10-03 on the eu19 inbox,
        only SI-NAP qualified; NMBS's dates beside a period, 2026-10-29."""
        if self._by_operating_day:
            return True
        if any(not self.periods[p].uic for _, p in self._period_refs if p in self.periods):
            return True
        if not self._dated or any(p not in self.periods for _, p in self._period_refs):
            return False
        return any(dt in self._dated for dt, _ in self._period_refs)

    def apply(self, a: ET.Element) -> None:
        dt = _ref(a, "DayTypeRef")
        days = self._assignment_days(a, dt) if dt else None
        if days is None:
            self.stats.unresolved += 1
            return
        target = self.days.get(dt)
        if target is None:
            target = self.days[dt] = _DayBits()
        on = _is_available(a)
        for ordinal in days:
            target.set(ordinal, on)

    def _assignment_days(self, a: ET.Element, dt: str) -> list[int] | None:
        """The days (date ordinals) one assignment names, or None when its
        reference is unknown."""
        if period := _ref(a, "OperatingPeriodRef"):
            days = self._period_days(period)
            allowed = self.weekdays.get(dt)
            if days is None or allowed is None:
                return days
            # Ordinal 1 (0001-01-01) is a Monday.
            return [o for o in days if (o - 1) % 7 in allowed]
        if op_day := _ref(a, "OperatingDayRef"):
            d = self.op_days.get(op_day)
            return [d.toordinal()] if d else None
        d = _date(_text(a, "Date"))
        return [d.toordinal()] if d else None

    def _period_days(self, pid: str) -> list[int] | None:
        p = self.periods.get(pid)
        if p is None:
            return None
        start = p.start or self.op_days.get(p.from_day)
        end = p.end or self.op_days.get(p.to_day)
        if start is None or end is None or end < start:
            return None
        first, n = start.toordinal(), (end - start).days + 1
        if not p.bits:
            return list(range(first, first + n))
        return [first + m.start() for m in _NONZERO_RE.finditer(p.bits, 0, n)]

    def render(self, prefix: str) -> tuple[bytes, bytes]:
        """(UicOperatingPeriod elements, dayTypeAssignments block), one each
        per day type, named after it so ids stay unique across frames and files."""
        p = f"{prefix}:" if prefix else ""
        day_types = sorted(self.weekdays.keys() | self.days.keys())
        self.stats.day_types = len(day_types)
        periods, assigns = [], []
        for dt in day_types:
            bits_of = self.days.get(dt)
            span = bits_of.span() if bits_of is not None else None
            # Referenced by journeys maybe: must exist, or MOTIS drops the file.
            first, bits = span or (date(2000, 1, 1), "0")
            last = date.fromordinal(first.toordinal() + len(bits) - 1)
            pid = quoteattr(f"VIATOR:UicOperatingPeriod:{dt}")
            periods.append(
                f'<{p}UicOperatingPeriod version="1" id={pid}>'
                f"<{p}FromDate>{first.isoformat()}T00:00:00</{p}FromDate>"
                f"<{p}ToDate>{last.isoformat()}T00:00:00</{p}ToDate>"
                f"<{p}ValidDayBits>{bits}</{p}ValidDayBits>"
                f"</{p}UicOperatingPeriod>"
            )
            aid = quoteattr(f"VIATOR:DayTypeAssignment:{dt}")
            assigns.append(
                f'<{p}DayTypeAssignment version="1" id={aid} order="1">'
                f"<{p}OperatingPeriodRef ref={pid}/>"
                f"<{p}DayTypeRef ref={quoteattr(dt)}/>"
                f"</{p}DayTypeAssignment>"
            )
        block = f"<{p}dayTypeAssignments>{''.join(assigns)}</{p}dayTypeAssignments>"
        return "".join(periods).encode("utf-8"), block.encode("utf-8")


# ─────────────────────────── frame rewrite ───────────────────────────


def convert_frame(
    frame_xml: bytes, prefix: str, ns_decls: bytes
) -> tuple[bytes, FrameStats] | None:
    """Rewritten `ServiceCalendarFrame` bytes, or None when it needs nothing."""
    wrapped = b"<viator-wrap" + ns_decls + b">" + frame_xml + _WRAP_END
    try:  # without an XML declaration: a frame that is not UTF-8 fails here too
        frame = next(iter(_xml_fromstring(wrapped)))
    except (ParseError, StopIteration):  # malformed: leave it as published
        return None
    cal = _Calendar()
    assignments = []
    for el in frame.iter():
        if _local(el) == "DayTypeAssignment":
            cal.note(el)
            assignments.append(el)
        else:
            cal.define(el)
    if not cal.needs_conversion():
        return None
    for _, a in sorted(enumerate(assignments), key=lambda p: (_order(p[1]), p[0])):
        cal.apply(a)
    out = io.BytesIO()
    if not _write_converted(io.BytesIO(frame_xml), 0, len(frame_xml), prefix, cal, out):
        return None
    return out.getvalue(), cal.stats


def _write_converted(
    src: IO[bytes], start: int, end: int, prefix: str, cal: _Calendar, dst: IO[bytes]
) -> bool:
    """Write the frame at bytes [start, end) of `src` to `dst` with `cal`'s
    assignments in place of its dayTypeAssignments block and its periods added
    to operatingPeriods. False, writing nothing, if the block cannot be found."""
    p = re.escape(f"{prefix}:" if prefix else "").encode()
    block = _find(src, start, end, re.compile(rb"<" + p + rb"dayTypeAssignments\b[^>]*?(/?)>"))
    if block is None:  # none: put it where the schema expects it, last in the frame
        close = end - len(_end_tag(prefix.encode() or None))
        span = (close, close)
    elif block[2]:  # <dayTypeAssignments/>
        span = (block[0], block[1])
    else:
        close_m = _find(src, block[1], end, re.compile(rb"</" + p + rb"dayTypeAssignments\s*>"))
        if close_m is None:
            return False
        span = (block[0], close_m[1])
    periods, assignments = cal.render(prefix)
    periods_end = _find(src, start, end, re.compile(rb"</" + p + rb"operatingPeriods\s*>"))
    if periods_end is None:  # a container of its own, before the assignments
        tag = f"{prefix}:operatingPeriods" if prefix else "operatingPeriods"
        wrapped = f"<{tag}>".encode() + periods + f"</{tag}>".encode()
        edits = [(span[0], span[1], wrapped + assignments)]
    else:
        edits = sorted([(periods_end[0], periods_end[0], periods), (span[0], span[1], assignments)])
    _copy_edited(src, start, end, edits, dst)
    return True


def _find(
    src: IO[bytes], start: int, end: int, rx: re.Pattern[bytes]
) -> tuple[int, int, bytes] | None:
    """(start, end, first group) of the first match of `rx` in bytes
    [start, end) of `src`, read in chunks. A match is a tag: never longer
    than `_TAIL_KEEP`, and complete once it matches."""
    src.seek(start)
    pos, carry = start, b""
    while pos < end:
        chunk = src.read(min(_CHUNK, end - pos))
        if not chunk:
            break
        buf, base = carry + chunk, pos - len(carry)
        pos += len(chunk)
        if m := rx.search(buf):
            return base + m.start(), base + m.end(), m.group(1) if rx.groups else b""
        carry = buf[-_TAIL_KEEP:]
    return None


def _copy_range(src: IO[bytes], start: int, end: int, dst: IO[bytes]) -> None:
    src.seek(start)
    left = end - start
    while left > 0 and (chunk := src.read(min(_CHUNK, left))):
        dst.write(chunk)
        left -= len(chunk)


def _copy_edited(
    src: IO[bytes], start: int, end: int, edits: list[tuple[int, int, bytes]], dst: IO[bytes]
) -> None:
    """Copy bytes [start, end) of `src`, each (a, b, text) of `edits` (sorted)
    replacing bytes [a, b) with `text`."""
    pos = start
    for a, b, text in edits:
        _copy_range(src, pos, a, dst)
        dst.write(text)
        pos = b
    _copy_range(src, pos, end, dst)


def _end_tag(prefix: bytes | None) -> bytes:
    return b"</" + (prefix + b":" if prefix else b"") + b"ServiceCalendarFrame>"


def _ns_decls(buf: bytes) -> bytes:
    root = _ROOT_RE.search(buf)
    return b"".join(_XMLNS_RE.findall(root.group(0))) if root else b""


def _complete_frame(buf: bytes) -> tuple[int, int, str] | None:
    """(start, end, prefix) of the first whole ServiceCalendarFrame in `buf`."""
    start = _FRAME_START_RE.search(buf)
    if start is None:
        return None
    end_tag = _end_tag(start.group(1))
    end = buf.find(end_tag, start.start())
    if end < 0:
        return None
    return start.start(), end + len(end_tag), (start.group(1) or b"").decode()


def _write_frame(
    dst: IO[bytes], frame: bytes, prefix: str, ns_decls: bytes, stats: ConvertStats
) -> bool:
    result = convert_frame(frame, prefix, ns_decls)
    if result is None:
        dst.write(frame)
        return False
    dst.write(result[0])
    stats.frames.append(result[1])
    return True


def _rewrite_stream(
    src: IO[bytes], dst: IO[bytes], stats: ConvertStats, spool_dir: Path | None = None
) -> bool:
    """Copy `src` to `dst`, rewriting every ServiceCalendarFrame. True if any changed.

    Only the bytes after the last possible frame start are held back; a frame
    that has started is buffered whole, up to `_FRAME_IN_MEMORY`, beyond which
    it goes to a temporary file in `spool_dir` (`_spool_frame`). A truncated
    document is passed through untouched."""
    buf, ns_decls, changed, eof = b"", b"", False, False
    while True:
        ns_decls = ns_decls or _ns_decls(buf)
        found = _complete_frame(buf)
        if found is not None:
            start, end, prefix = found
            dst.write(buf[:start])
            changed |= _write_frame(dst, buf[start:end], prefix, ns_decls, stats)
            buf = buf[end:]
            continue
        if eof:
            dst.write(buf)
            return changed
        frame_start = _FRAME_START_RE.search(buf)
        if frame_start is None:
            keep = min(len(buf), _TAIL_KEEP)
            dst.write(buf[: len(buf) - keep])
            buf = buf[len(buf) - keep :]
        elif len(buf) - frame_start.start() > _FRAME_IN_MEMORY:
            dst.write(buf[: frame_start.start()])
            prefix = (frame_start.group(1) or b"").decode()
            head = buf[frame_start.start() :]
            frame_changed, buf = _spool_frame(src, head, prefix, ns_decls, dst, stats, spool_dir)
            changed |= frame_changed
            continue
        chunk = src.read(_CHUNK)
        eof = not chunk
        buf += chunk


# ─────────────────────────── frames too large for memory ───────────────────────────


def _spool_frame(
    src: IO[bytes],
    head: bytes,
    prefix: str,
    ns_decls: bytes,
    dst: IO[bytes],
    stats: ConvertStats,
    spool_dir: Path | None,
) -> tuple[bool, bytes]:
    """Convert a frame too large for memory, whose first bytes are `head`:
    copied to a temporary file as `src` is read, scanned there, then written
    to `dst`. Returns (changed, the bytes read past the frame's end)."""
    end_tag = _end_tag(prefix.encode() or None)
    wrap = b"<viator-wrap" + ns_decls + b">"
    keep = len(end_tag) - 1
    with tempfile.TemporaryFile(dir=spool_dir) as spool:
        spool.write(wrap)
        pending = head
        while (i := pending.find(end_tag)) < 0:
            chunk = src.read(_CHUNK)
            if not chunk:  # a truncated document: as published
                spool.write(pending)
                _copy_range(spool, len(wrap), spool.tell(), dst)
                return False, b""
            cut = max(0, len(pending) - keep)
            spool.write(pending[:cut])
            pending = pending[cut:] + chunk
        spool.write(pending[: i + len(end_tag)])
        frame_end = spool.tell()
        spool.write(_WRAP_END)
        changed = _convert_spooled(spool, len(wrap), frame_end, prefix, dst, stats)
        return changed, pending[i + len(end_tag) :]


def _convert_spooled(
    spool: IO[bytes], start: int, end: int, prefix: str, dst: IO[bytes], stats: ConvertStats
) -> bool:
    """Write the frame at bytes [start, end) of `spool`, converted if it needs it."""
    spool.seek(0)
    cal = _Calendar()
    try:
        _scan(spool, cal)
        needed = cal.needs_conversion()
    except ParseError:  # malformed: leave it as published
        needed = False
    if needed:
        cal.stats.streamed = True
        if _write_converted(spool, start, end, prefix, cal, dst):
            stats.frames.append(cal.stats)
            return True
    _copy_range(spool, start, end, dst)
    return False


def _scan(f: IO[bytes], cal: _Calendar) -> None:
    """Feed `cal` every definition and assignment of the frame in `f`, as it
    is parsed, each freed once used: memory stays that of the calendar, not
    of the tree."""
    parents: list[ET.Element] = []
    last_order: dict[str, int] = {}
    for event, el in _xml_iterparse(f, events=("start", "end")):
        if event == "start":
            parents.append(el)
            continue
        parents.pop()
        if _local(el) == "DayTypeAssignment":
            dt, order = _ref(el, "DayTypeRef"), _order(el)
            if order < last_order.get(dt, order):
                cal.stats.out_of_order += 1
            last_order[dt] = order
            cal.note(el)
            cal.apply(el)
        elif not cal.define(el):
            continue
        if parents:
            parents[-1].remove(el)


# ─────────────────────────── the zip ───────────────────────────


def convert_zip(src: Path, dest: Path) -> ConvertStats | None:
    """Write a converted copy of the NeTEx zip `src` to `dest`: calendars
    rewritten, the forms MOTIS drops a whole file for repaired
    (app/netex_motis_repair.py), shared files renamed as base files when the
    line files take their day types from them. Returns None — and leaves
    `dest` absent — when nothing needed any of it.

    One pass. An XML member up to `netex_motis_repair.MAX_MEMBER_BYTES` is read
    whole and repaired; a larger one, or any member of a zip with base files,
    is streamed with its calendar rewritten only. A member that names a Line
    it does not define, or that holds no course (a shared file), waits in a
    temporary folder until every member has been seen."""
    stats = ConvertStats()
    tmp = dest.with_name(dest.name + ".part")
    with (
        zipfile.ZipFile(src) as zin,
        zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout,
        tempfile.TemporaryDirectory(dir=dest.parent) as work_dir,
    ):
        work = Path(work_dir)
        if netex_motis_repair.has_base_files(zin.namelist()):
            changed = False
            for info in zin.infolist():
                changed |= _stream_member(zin, zout, info, stats, work)
        else:
            zip_pass = _ZipPass(zin, zout, work, stats)
            for info in zin.infolist():
                zip_pass.member(info)
            changed = zip_pass.finish()
    if not changed:
        tmp.unlink(missing_ok=True)
        return None
    tmp.replace(dest)
    return stats


@dataclass
class _Held:
    """A member written after every other: one naming a Line of another file,
    or a shared one (no course), which MOTIS may have to read as a base file."""

    info: zipfile.ZipInfo
    path: Path
    shared: bool
    raw: bool = True  # still to repair; else already written out, as it goes in


class _ZipPass:
    """The pass over a zip without base files (`convert_zip`)."""

    def __init__(
        self, zin: zipfile.ZipFile, zout: zipfile.ZipFile, work: Path, stats: ConvertStats
    ) -> None:
        self.zin, self.zout, self.work, self.stats = zin, zout, work, stats
        self.changed = False
        self.homes: dict[bytes, str] = {}  # Line id -> the member defining it
        self.held: list[_Held] = []
        self.foreign: set[bytes] = set()  # DayTypes line files name but do not define
        self.shared_day_types: set[bytes] = set()  # DayTypes the shared files define

    def member(self, info: zipfile.ZipInfo) -> None:
        if not info.filename.lower().endswith(".xml"):
            self.changed |= _stream_member(self.zin, self.zout, info, self.stats, self.work)
        elif info.file_size > netex_motis_repair.MAX_MEMBER_BYTES:
            self._large(info)
        else:
            self._whole(info)

    def _large(self, info: zipfile.ZipInfo) -> None:
        """Too large to read whole: its calendars rewritten as it streams."""
        self.stats.repairs.too_large += 1
        courses, day_types = _scan_member(self.zin, info)
        if courses:
            self.changed |= _stream_member(self.zin, self.zout, info, self.stats, self.work)
            return
        path = self._path()
        with self.zin.open(info) as fin, path.open("wb") as fout:
            self.changed |= _rewrite_stream(fin, fout, self.stats, self.work)
        self.shared_day_types |= day_types
        self.held.append(_Held(info, path, shared=True, raw=False))

    def _whole(self, info: zipfile.ZipInfo) -> None:
        data = self.zin.read(info)
        for line_id in netex_motis_repair.line_ids(data):
            self.homes.setdefault(line_id, info.filename)
        shared = not netex_motis_repair.has_courses(data)
        if shared:
            self.shared_day_types |= netex_motis_repair.day_type_ids(data)
        else:
            self.foreign |= netex_motis_repair.foreign_day_types(data)
        if shared or netex_motis_repair.missing_lines(data):
            path = self._path()
            path.write_bytes(data)
            self.held.append(_Held(info, path, shared=shared))
            return
        with self.zout.open(info.filename, "w", force_zip64=True) as fout:
            self.changed |= _write_member(fout, data, None, self.stats, self.work)

    def finish(self) -> bool:
        """Write the held members; True if anything changed. The shared ones
        are renamed as base files when a line file names a DayType only they
        define (NMBS: every line file's calendar is in common.xml)."""
        lines = _line_source(self.zin, self.homes)
        as_base = bool(self.foreign & self.shared_day_types)
        for held in self.held:
            name = held.info.filename
            if as_base and held.shared:
                name = netex_motis_repair.base_file_name(name)
                self.stats.base_files.append(held.info.filename)
            if not held.raw:
                self.zout.write(held.path, name)
                continue
            with self.zout.open(name, "w", force_zip64=True) as fout:
                data = held.path.read_bytes()
                self.changed |= _write_member(fout, data, lines, self.stats, self.work)
        return self.changed or bool(self.stats.base_files)

    def _path(self) -> Path:
        return self.work / f"held-{len(self.held)}"


def _scan_member(zin: zipfile.ZipFile, info: zipfile.ZipInfo) -> tuple[bool, set[bytes]]:
    """(does the member hold a course, the DayTypes it defines), read as a stream."""
    day_types: set[bytes] = set()
    carry = b""
    with zin.open(info) as f:
        while chunk := f.read(_CHUNK):
            buf = carry + chunk
            if netex_motis_repair.has_courses(buf):
                return True, set()
            cut = max(0, len(buf) - _TAIL_KEEP)
            day_types |= netex_motis_repair.day_type_ids(buf, before=cut)
            carry = buf[cut:]
    return False, day_types | netex_motis_repair.day_type_ids(carry)


def _stream_member(
    zin: zipfile.ZipFile,
    zout: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    stats: ConvertStats,
    work: Path,
) -> bool:
    """Copy a member as it streams, an XML one with its calendars rewritten;
    True if anything changed."""
    with zin.open(info) as fin, zout.open(info.filename, "w", force_zip64=True) as fout:
        if info.filename.lower().endswith(".xml"):
            return _rewrite_stream(fin, fout, stats, work)
        shutil.copyfileobj(fin, fout, _CHUNK)
        return False


def _write_member(
    fout: IO[bytes],
    data: bytes,
    lines: netex_motis_repair.LineSource | None,
    stats: ConvertStats,
    work: Path,
) -> bool:
    """Repair `data`, rewrite its calendars, write it; True if anything changed."""
    repaired = netex_motis_repair.repair(data, lines, stats.repairs)
    rewritten = _rewrite_stream(io.BytesIO(repaired), fout, stats, work)
    return rewritten or repaired is not data


def _line_source(zin: zipfile.ZipFile, homes: dict[bytes, str]) -> netex_motis_repair.LineSource:
    """A Line and what it names, read from the member that defines it."""
    cache: dict[str, bytes] = {}

    def source(line_id: bytes) -> netex_motis_repair.LineCopy | None:
        name = homes.get(line_id)
        if name is None:
            return None
        if name not in cache:
            if len(cache) >= 8:
                cache.pop(next(iter(cache)))
            cache[name] = zin.read(name)
        return netex_motis_repair.line_copy(cache[name], line_id)

    return source
