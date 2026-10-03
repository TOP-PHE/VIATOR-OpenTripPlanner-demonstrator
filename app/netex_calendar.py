"""Rewrite a NeTEx calendar into the one model MOTIS reads.

NeTEx allows several equivalent calendar models. MOTIS (nigiri's NeTEx loader)
reads exactly one: `UicOperatingPeriod` with `ValidDayBits`, each
`DayTypeAssignment` pointing at one by `OperatingPeriodRef` — the Swiss
profile it was built on. Feeds following the European passenger-information
profile (EPIP) describe the same days differently:

  * `DayTypeAssignment` → `OperatingDayRef` (one dated `OperatingDay`) or a
    bare `Date`;
  * `DayTypeAssignment` → `OperatingPeriodRef` to a plain `OperatingPeriod`
    (`FromDate`/`ToDate` or `FromOperatingDayRef`/`ToOperatingDayRef`),
    narrowed by the `DayType`'s `DaysOfWeek`;
  * `isAvailable="false"` assignments that remove days.

MOTIS resolves an `OperatingPeriodRef` only among `UicOperatingPeriod`s and
drops the WHOLE file on the first reference it cannot resolve, printing one
`ERROR:` line and carrying on (2026-10-02: Slovenia's 495 MB national file
loaded zero stops). An `OperatingDayRef` it would silently read as "every day".

`convert_zip` computes each day type's actual service days and writes them back
as one `UicOperatingPeriod` + `ValidDayBits` and one `DayTypeAssignment` per
day type, inside each `ServiceCalendarFrame`. Everything else in the file is
streamed through byte for byte. Feeds already in the Swiss model are left
alone (`convert_zip` returns None).

Only the MOTIS build uses the converted copy (`worker.run_build_motis`); the
inbox keeps the published file, which OTP reads as it is.
"""

from __future__ import annotations

import re
import shutil
import zipfile
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import IO
from xml.etree import ElementTree as ET
from xml.sax.saxutils import quoteattr

from defusedxml.ElementTree import ParseError
from defusedxml.ElementTree import fromstring as _xml_fromstring

# Bumped whenever the output changes, so cached conversions are redone.
CONVERTER_VERSION = 1

_CHUNK = 8 * 1024 * 1024
_FRAME_START_RE = re.compile(rb"<(?:([A-Za-z_][\w.-]*):)?ServiceCalendarFrame\b")
_ROOT_RE = re.compile(rb"<(?:[A-Za-z_][\w.-]*:)?PublicationDelivery\b[^>]*>", re.DOTALL)
_XMLNS_RE = re.compile(rb"""\sxmlns(?::[\w.-]+)?=(?:"[^"]*"|'[^']*')""")
# A start tag split across two chunks is never longer than this.
_TAIL_KEEP = 256

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


@dataclass
class ConvertStats:
    frames: list[FrameStats] = field(default_factory=list)

    def summary(self) -> str:
        dt = sum(f.day_types for f in self.frames)
        a = sum(f.assignments_in for f in self.frames)
        u = sum(f.unresolved for f in self.frames)
        text = f"{a} day-type assignments rewritten as {dt} UicOperatingPeriod"
        return text + (f" ({u} unresolved, ignored)" if u else "")


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


def _days_between(a: date, b: date) -> list[date]:
    return [a + timedelta(days=i) for i in range((b - a).days + 1)]


def _weekdays(day_type: ET.Element) -> set[int] | None:
    """Weekdays a DayType is restricted to, or None for no restriction."""
    days: set[int] = set()
    for el in day_type.iter():
        if _local(el) == "DaysOfWeek":
            for token in (el.text or "").split():
                days |= _WEEKDAYS.get(token.lower(), set())
    return days or None


def _periods(frame: ET.Element, op_days: dict[str, date]) -> dict[str, list[date]]:
    out: dict[str, list[date]] = {}
    for el in frame.iter():
        name = _local(el)
        if name not in ("OperatingPeriod", "UicOperatingPeriod"):
            continue
        start = _date(_text(el, "FromDate")) or op_days.get(_ref(el, "FromOperatingDayRef"))
        end = _date(_text(el, "ToDate")) or op_days.get(_ref(el, "ToOperatingDayRef"))
        if start is None or end is None or end < start:
            continue
        days = _days_between(start, end)
        bits = _text(el, "ValidDayBits")
        if bits:
            days = [d for i, d in enumerate(days) if i < len(bits) and bits[i] != "0"]
        out[el.get("id", "")] = days
    return out


def _needs_conversion(frame: ET.Element) -> bool:
    uic = {el.get("id") for el in frame.iter() if _local(el) == "UicOperatingPeriod"}
    for el in frame.iter():
        if _local(el) != "DayTypeAssignment":
            continue
        if el.get("isAvailable") == "false" or _text(el, "isAvailable") == "false":
            return True
        period = _ref(el, "OperatingPeriodRef")
        if not period or period not in uic:
            return True
    return False


def _service_days(frame: ET.Element, stats: FrameStats) -> dict[str, set[date]]:
    op_days = {
        el.get("id", ""): d
        for el in frame.iter()
        if _local(el) == "OperatingDay" and (d := _date(_text(el, "CalendarDate")))
    }
    periods = _periods(frame, op_days)
    day_types = {el.get("id", ""): el for el in frame.iter() if _local(el) == "DayType"}
    result: dict[str, set[date]] = {dt: set() for dt in day_types}

    assignments = [el for el in frame.iter() if _local(el) == "DayTypeAssignment"]
    stats.assignments_in = len(assignments)
    ordered = sorted(enumerate(assignments), key=lambda p: (int(p[1].get("order") or 0), p[0]))
    for _, a in ordered:
        dt = _ref(a, "DayTypeRef")
        days = _assignment_days(a, day_types.get(dt), periods, op_days) if dt else None
        if days is None:
            stats.unresolved += 1
            continue
        target = result.setdefault(dt, set())
        if _is_available(a):
            target.update(days)
        else:
            target.difference_update(days)
    stats.day_types = len(result)
    return result


def _assignment_days(
    a: ET.Element,
    day_type: ET.Element | None,
    periods: dict[str, list[date]],
    op_days: dict[str, date],
) -> list[date] | None:
    """The days one DayTypeAssignment names, or None when its reference is unknown."""
    if period := _ref(a, "OperatingPeriodRef"):
        days = periods.get(period)
        allowed = _weekdays(day_type) if day_type is not None else None
        if days is None or allowed is None:
            return days
        return [d for d in days if d.weekday() in allowed]
    if op_day := _ref(a, "OperatingDayRef"):
        return [op_days[op_day]] if op_day in op_days else None
    d = _date(_text(a, "Date"))
    return [d] if d else None


def _is_available(a: ET.Element) -> bool:
    return (a.get("isAvailable") or _text(a, "isAvailable") or "true") != "false"


# ─────────────────────────── frame rewrite ───────────────────────────


def _render(prefix: str, days_by_type: dict[str, set[date]]) -> tuple[str, str]:
    """(UicOperatingPeriod elements, dayTypeAssignments block)."""
    p = f"{prefix}:" if prefix else ""
    periods, assigns = [], []
    for n, (dt, days) in enumerate(sorted(days_by_type.items()), start=1):
        if days:
            first, last = min(days), max(days)
            bits = "".join("1" if d in days else "0" for d in _days_between(first, last))
        else:  # referenced by journeys maybe; must exist or MOTIS drops the file
            first = last = date(2000, 1, 1)
            bits = "0"
        pid = quoteattr(f"VIATOR:UicOperatingPeriod:{n}")
        periods.append(
            f'<{p}UicOperatingPeriod version="1" id={pid}>'
            f"<{p}FromDate>{first.isoformat()}T00:00:00</{p}FromDate>"
            f"<{p}ToDate>{last.isoformat()}T00:00:00</{p}ToDate>"
            f"<{p}ValidDayBits>{bits}</{p}ValidDayBits>"
            f"</{p}UicOperatingPeriod>"
        )
        aid = quoteattr(f"VIATOR:DayTypeAssignment:{n}")
        assigns.append(
            f'<{p}DayTypeAssignment version="1" id={aid} order="1">'
            f"<{p}OperatingPeriodRef ref={pid}/>"
            f"<{p}DayTypeRef ref={quoteattr(dt)}/>"
            f"</{p}DayTypeAssignment>"
        )
    return "".join(periods), f"<{p}dayTypeAssignments>{''.join(assigns)}</{p}dayTypeAssignments>"


def convert_frame(
    frame_xml: bytes, prefix: str, ns_decls: bytes
) -> tuple[bytes, FrameStats] | None:
    """Rewritten `ServiceCalendarFrame` bytes, or None when it needs nothing."""
    wrapped = b"<viator-wrap" + ns_decls + b">" + frame_xml + b"</viator-wrap>"
    try:
        frame = next(iter(_xml_fromstring(wrapped)))
    except (ParseError, StopIteration):  # malformed or not UTF-8: leave it as published
        return None
    if not _needs_conversion(frame):
        return None
    stats = FrameStats()
    periods, assignments = _render(prefix, _service_days(frame, stats))

    try:
        text = frame_xml.decode("utf-8")
    except UnicodeDecodeError:  # a non-UTF-8 feed: leave its calendar as published
        return None
    p = re.escape(f"{prefix}:" if prefix else "")
    block = re.compile(
        rf"<{p}dayTypeAssignments\b.*?</{p}dayTypeAssignments>|<{p}dayTypeAssignments\s*/>", re.S
    )
    text, n = block.subn(lambda _m: assignments, text, count=1)
    if n == 0:  # no block: put it where the schema expects it, last in the frame
        close = f"</{prefix + ':' if prefix else ''}ServiceCalendarFrame>"
        text = text.replace(close, assignments + close, 1)

    tag = f"{prefix}:operatingPeriods" if prefix else "operatingPeriods"
    if f"</{tag}>" in text:
        text = text.replace(f"</{tag}>", periods + f"</{tag}>", 1)
    else:
        text = text.replace(assignments, f"<{tag}>{periods}</{tag}>" + assignments, 1)
    return text.encode("utf-8"), stats


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


def _rewrite_stream(src: IO[bytes], dst: IO[bytes], stats: ConvertStats) -> bool:
    """Copy `src` to `dst`, rewriting every ServiceCalendarFrame. True if any changed.

    Only the bytes after the last possible frame start are held back; a frame
    that has started is buffered whole (calendar frames are small). A
    truncated document is passed through untouched."""
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
        if _FRAME_START_RE.search(buf) is None:
            keep = min(len(buf), _TAIL_KEEP)
            dst.write(buf[: len(buf) - keep])
            buf = buf[len(buf) - keep :]
        chunk = src.read(_CHUNK)
        eof = not chunk
        buf += chunk


def convert_zip(src: Path, dest: Path) -> ConvertStats | None:
    """Write a converted copy of the NeTEx zip `src` to `dest`. Returns None —
    and leaves `dest` absent — when no calendar needed converting."""
    stats = ConvertStats()
    tmp = dest.with_name(dest.name + ".part")
    changed = False
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            with zin.open(info) as fin, zout.open(info.filename, "w", force_zip64=True) as fout:
                if info.filename.lower().endswith(".xml"):
                    changed |= _rewrite_stream(fin, fout, stats)
                else:
                    shutil.copyfileobj(fin, fout, _CHUNK)
    if not changed:
        tmp.unlink(missing_ok=True)
        return None
    tmp.replace(dest)
    return stats
