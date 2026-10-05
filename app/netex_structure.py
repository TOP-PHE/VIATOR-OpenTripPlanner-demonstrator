"""Structure fingerprint of a NeTEx file, checked at every download.

NeTEx allows several valid ways to publish the same information, and MOTIS
(nigiri's NeTEx loader) reads only some of them; on a reference it cannot
resolve it silently drops the whole file. Slovenia's national file was lost
that way (2026-10-02) and was only noticed because trains were missing from a
search. A producer can change its export any day, so every downloaded NeTEx
file gets a fingerprint and an assessment, shown in the Feed status panel:

* the declared header: `version`, XSD location, a `<!-- Profile: … -->`
  comment, `ParticipantRef`;
* the file's vocabulary: how many times each element name occurs (start tags,
  namespace prefix dropped). Comparing it with the previous download reveals
  any new structure, in any part of NeTEx, without knowing it in advance;
* RULES: element names that mark a form MOTIS does not read, measured against
  nigiri's loader;
* the calendar: for each `DayType`, whether anything gives it dates. SI-NAP's
  2026-10-01 file defined every rail day type by its `Name` alone ("Vozi vsak
  dan.") and many bus ones by `DaysOfWeek` plus exclusions only; both load
  without error and run on no date, so nothing else would flag them.

Warnings never block the file: keeping an old timetable silently would hide
the change just as well. Counting is a byte scan, not an XML parse (~75 MB/s
of XML with the calendar scan; a full parse took 40 min on DB's 2 GB zip).
"""

from __future__ import annotations

import collections
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Bumped when the fingerprint changes shape, so old ones are not compared.
FINGERPRINT_VERSION = 2

_CHUNK = 8 * 1024 * 1024
# `<name` of every start tag; closing tags (`</`), comments (`<!`) and
# processing instructions (`<?`) do not match. The prefix is stripped later.
_START_TAG_RE = re.compile(rb"<([A-Za-z_][\w.:-]*)")
_ROOT_RE = re.compile(rb"<(?:[\w.-]+:)?PublicationDelivery\b[^>]*>", re.S)
_VERSION_RE = re.compile(rb"""\sversion=["']([^"']*)["']""")
_XSD_RE = re.compile(rb"""schemaLocation=["'][^"']*?/([\d.]+)/xsd""")
# Captured text is stripped by the caller; no nested quantifiers to backtrack on.
_PROFILE_RE = re.compile(rb"<!--\s*Profile:?([^-]*)-->")
_PARTICIPANT_RE = re.compile(rb"<(?:[\w.-]+:)?ParticipantRef>([^<]*)<")
# Calendar blocks: a DayType definition or a DayTypeAssignment, whole.
_CAL_START_RE = re.compile(rb"<(?:[\w.-]+:)?(DayType|DayTypeAssignment)[\s>]")
_DAY_TYPE_RE = re.compile(
    rb"""<(?:[\w.-]+:)?DayType\s[^>]*?\bid=["']([^"']+)["'][^>]*(?<!/)>(.*?)</(?:[\w.-]+:)?DayType>""",
    re.S,
)
_DTA_RE = re.compile(
    rb"<(?:[\w.-]+:)?DayTypeAssignment[\s>].*?</(?:[\w.-]+:)?DayTypeAssignment>", re.S
)
# Starts on the literal name so the scan stays fast; `DayTypeRef\s` is only
# ever the start tag (the end tag is `DayTypeRef>`).
_DAY_TYPE_REF_RE = re.compile(rb"""DayTypeRef\s[^>]*?\bref=["']([^"']+)["']""")
_NOT_AVAILABLE_RE = re.compile(rb"""isAvailable(?:=["']false["']|>\s*false\s*<)""")
_NAME_RE = re.compile(rb"<(?:[\w.-]+:)?Name>([^<]*)<")
_EXAMPLES = 3

OK, WARN, RED = "ok", "warn", "red"


@dataclass(frozen=True)
class Rule:
    """An element whose presence marks a form MOTIS reads differently or not at all."""

    element: str
    level: str  # WARN: handled by VIATOR or partly read; RED: lost in MOTIS
    message: str


# Measured against nigiri's NeTEx loader (src/loader/netex/load_timetable.cc)
# at the commit MOTIS 2.10.2 pins; identical for these points at the 2.11.3
# commit. `nigiri:N` = line N there. Each element is a valid NeTEx form
# (NeTEx-CEN XSD v2.0) that the loader does not read, or reads in a way that
# can drop the whole file: any unresolved reference throws, and the file is
# discarded with one `ERROR:` line on stderr (nigiri:1249).
RULES: tuple[Rule, ...] = (
    Rule(
        "OperatingDayRef",
        WARN,
        "calendar by dated OperatingDay: not read by MOTIS (nigiri:684-689), converted by VIATOR at build",
    ),
    Rule(
        "OperatingPeriod",
        WARN,
        "plain OperatingPeriod: not read by MOTIS (nigiri:649), converted by VIATOR at build",
    ),
    Rule(
        "FromOperatingDayRef",
        WARN,
        "period bounded by OperatingDay refs: MOTIS needs FromDate/ToDate (nigiri:653), file may be dropped",
    ),
    Rule(
        "ValidBetween",
        WARN,
        "validity by ValidBetween: not read by MOTIS (nigiri:961-969), those courses may run every day",
    ),
    Rule(
        "DatedServiceJourney",
        WARN,
        "DatedServiceJourney: not read by MOTIS (nigiri:940 reads ServiceJourney only)",
    ),
    Rule(
        "TemplateServiceJourney",
        WARN,
        "TemplateServiceJourney (frequency-based): not read by MOTIS (nigiri:940)",
    ),
    Rule("VehicleJourney", WARN, "VehicleJourney: not read by MOTIS (nigiri:940)"),
    Rule(
        "NormalDatedVehicleJourney",
        WARN,
        "NormalDatedVehicleJourney: not read by MOTIS (nigiri:940)",
    ),
    Rule("SpecialService", WARN, "SpecialService: not read by MOTIS (nigiri:940)"),
    Rule(
        "JourneyPattern",
        WARN,
        "plain JourneyPattern: MOTIS reads ServiceJourneyPattern only (nigiri:814), file may be dropped",
    ),
    Rule(
        "TimingPointInJourneyPattern",
        WARN,
        "TimingPointInJourneyPattern: not matched by MOTIS (nigiri:819, 1023), file may be dropped",
    ),
    Rule(
        "FlexibleLine",
        WARN,
        "FlexibleLine: not loaded by MOTIS (nigiri:470), references to it drop the file",
    ),
    Rule("GeneralFrame", WARN, "GeneralFrame: nothing inside it is read by MOTIS"),
    Rule("TrainStopAssignment", WARN, "TrainStopAssignment: not read by MOTIS (nigiri:605)"),
    Rule(
        "vehicleJourneyStopAssignments",
        WARN,
        "stop assignments on journeys: not read by MOTIS (nigiri:605)",
    ),
    Rule(
        "ServiceJourneyInterchange",
        WARN,
        "interchanges: MOTIS treats a missing StaySeated as true (XSD default false, nigiri:874); "
        "a link to a course it did not load aborts the whole import (nigiri:1602)",
    ),
    Rule(
        "JourneyMeeting",
        WARN,
        "JourneyMeeting: MOTIS turns every meeting into a stay-seated link (nigiri:869-874)",
    ),
)


def _ratio_rules(el: dict[str, int]) -> list[tuple[str, str]]:
    """Rules that compare two counts in the same file."""
    out = []
    if el.get("TrainNumberRef") and not el.get("TrainNumber"):
        out.append(
            (
                WARN,
                "TrainNumberRef without any TrainNumber in the file: "
                "MOTIS looks them up in the same file only (nigiri:952), file may be dropped",
            )
        )
    if el.get("TypeOfProductCategory") and not el.get("ValueSet"):
        out.append(
            (
                WARN,
                "TypeOfProductCategory outside a ValueSet: MOTIS reads "
                "typesOfValue/ValueSet/values only (nigiri:423), file may be dropped",
            )
        )
    for ref, target in (("OperatorRef", "Operator"), ("AuthorityRef", "Authority")):
        if el.get(ref) and not el.get(target):
            out.append(
                (
                    WARN,
                    f"{ref} but no {target} in the file: MOTIS resolves it only in the same "
                    "file or a declared base file (nigiri:88-106, 1278), file may be dropped",
                )
            )
    if el.get("Line") and not el.get("additionalOperators"):
        out.append(
            (
                WARN,
                "lines without additionalOperators: MOTIS reads the line operator "
                "only there (nigiri:483), lines get an empty operator",
            )
        )
    return out


@dataclass
class Fingerprint:
    version: int = FINGERPRINT_VERSION
    header: dict[str, str] = field(default_factory=dict)
    xml_files: int = 0
    elements: dict[str, int] = field(default_factory=dict)
    # Element start tags written with a namespace prefix, by prefix.
    prefixes: dict[str, int] = field(default_factory=dict)
    # _CalendarScan.summary(): day types that nothing gives dates to.
    calendar: dict[str, Any] = field(default_factory=dict)

    def to_state(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "header": self.header,
            "xml_files": self.xml_files,
            "elements": self.elements,
            "prefixes": self.prefixes,
            "calendar": self.calendar,
        }


@dataclass
class Assessment:
    level: str = OK
    messages: list[str] = field(default_factory=list)

    def add(self, level: str, message: str) -> None:
        self.messages.append(message)
        if level == RED or (level == WARN and self.level == OK):
            self.level = level

    def to_state(self) -> dict[str, Any]:
        return {"level": self.level, "messages": self.messages}


def _header(head: bytes) -> dict[str, str]:
    def first(rx: re.Pattern[bytes], text: bytes) -> str:
        m = rx.search(text)
        return m.group(1).decode("utf-8", "replace").strip() if m else ""

    root = _ROOT_RE.search(head)
    attrs = root.group(0) if root else b""
    return {
        "version": first(_VERSION_RE, attrs),
        "xsd": first(_XSD_RE, attrs),
        "profile": first(_PROFILE_RE, head),
        "participant": first(_PARTICIPANT_RE, head),
    }


class _CalendarScan:
    """For each DayType of the file: does anything give it dates?

    Collected on the same byte scan as the element counts. A DayType "has
    dates" when at least one DayTypeAssignment names it without
    `isAvailable=false`; nigiri reads assignments only (nigiri:684-689) and
    VIATOR's converter (app/netex_calendar.py) builds dates from them, so a
    DayType with `DaysOfWeek` but no positive assignment runs on no date too.
    """

    def __init__(self) -> None:
        self.weekdays: dict[str, bool] = {}  # DayType id -> has DaysOfWeek
        self.names: dict[str, str] = {}
        self.positive: set[str] = set()
        self.refs: collections.Counter[bytes] = collections.Counter()  # all DayTypeRef, raw
        self.assignment_refs: collections.Counter[str] = collections.Counter()

    def feed(self, buf: bytes) -> None:
        if b"DayType" not in buf:
            return
        self.refs.update(_DAY_TYPE_REF_RE.findall(buf))
        if b"DayType " in buf or b"DayType>" in buf:
            self._day_types(buf)
        if b"DayTypeAssignment" in buf:
            self._assignments(buf)

    def _day_types(self, buf: bytes) -> None:
        for m in _DAY_TYPE_RE.finditer(buf):
            dt = m.group(1).decode("utf-8", "replace")
            self.weekdays[dt] = b"DaysOfWeek>" in m.group(2)
            name = _NAME_RE.search(m.group(2))
            if name:
                self.names[dt] = name.group(1).decode("utf-8", "replace").strip()[:80]

    def _assignments(self, buf: bytes) -> None:
        for m in _DTA_RE.finditer(buf):
            ref = _DAY_TYPE_REF_RE.search(m.group(0))
            if ref is None:
                continue
            dt = ref.group(1).decode("utf-8", "replace")
            self.assignment_refs[dt] += 1
            if not _NOT_AVAILABLE_RE.search(m.group(0)):
                self.positive.add(dt)

    def summary(self) -> dict[str, Any]:
        """Day types with no positive assignment, split by whether they give
        DaysOfWeek, with how often journeys and other elements refer to them."""
        out: dict[str, Any] = {"day_types": len(self.weekdays)}
        for key, with_weekdays in (("no_dates", False), ("weekdays_only", True)):
            ids = sorted(
                dt
                for dt, dow in self.weekdays.items()
                if dow is with_weekdays and dt not in self.positive
            )
            uses = {dt: self.refs[dt.encode()] - self.assignment_refs[dt] for dt in ids}
            top = sorted(ids, key=uses.__getitem__, reverse=True)[:_EXAMPLES]
            out[key] = {
                "count": len(ids),
                "references": sum(uses.values()),
                "examples": [
                    {"id": dt, "name": self.names.get(dt, ""), "references": uses[dt]} for dt in top
                ],
            }
        return out


def _calendar_cut(buf: bytes, cut: int) -> int:
    """Move `cut` back to the last calendar block still open in `buf`, so a
    DayType or DayTypeAssignment split across reads is scanned whole."""
    window = max(0, cut - 1024 * 1024)  # blocks are a few kB
    starts = list(_CAL_START_RE.finditer(buf, window, cut))
    if not starts:
        return cut
    last = starts[-1]  # blocks do not nest: only the last one can be open
    close = re.compile(rb"</(?:[\w.-]+:)?" + last.group(1) + rb">")
    return cut if close.search(buf, last.end(), cut) else last.start()


def _count(stream: Any, counts: collections.Counter[bytes], calendar: _CalendarScan) -> bytes:
    """Count start tags of one XML stream; returns its first bytes."""
    head = b""
    tail = b""
    while chunk := stream.read(_CHUNK):
        if not head:
            head = chunk[:8192]
        buf = tail + chunk
        cut = buf.rfind(b"<")  # a tag split across reads is counted next time
        if cut < 0:
            cut = len(buf)
        cut = _calendar_cut(buf, cut)
        counts.update(_START_TAG_RE.findall(buf, 0, cut))
        calendar.feed(buf[:cut])
        tail = buf[cut:]
    counts.update(_START_TAG_RE.findall(tail))
    calendar.feed(tail)
    return head


def fingerprint(path: Path) -> Fingerprint:
    """Fingerprint a NeTEx zip (or a bare XML file)."""
    counts: collections.Counter[bytes] = collections.Counter()
    calendar = _CalendarScan()
    fp = Fingerprint()
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            names = sorted(n for n in z.namelist() if n.lower().endswith(".xml"))
            for name in names:
                with z.open(name) as f:
                    head = _count(f, counts, calendar)
                if not fp.header:
                    fp.header = _header(head)
            fp.xml_files = len(names)
    else:
        with path.open("rb") as f:
            fp.header = _header(_count(f, counts, calendar))
        fp.xml_files = 1
    merged: collections.Counter[str] = collections.Counter()
    prefixes: collections.Counter[str] = collections.Counter()
    for raw, n in counts.items():
        prefix, _, name = raw.decode("ascii", "replace").rpartition(":")
        merged[name] += n
        if prefix:
            prefixes[prefix] += n
    fp.elements = dict(sorted(merged.items()))
    fp.prefixes = dict(sorted(prefixes.items()))
    fp.calendar = calendar.summary()
    return fp


def _journeys_without_calendar(elements: dict[str, int]) -> bool:
    has_journeys = elements.get("ServiceJourney", 0) > 0
    calendars = (
        elements.get("DayTypeAssignment", 0)
        + elements.get("AvailabilityCondition", 0)
        + elements.get("UicOperatingPeriod", 0)
    )
    return has_journeys and calendars == 0


def _examples(entry: dict[str, Any]) -> str:
    names = [f'"{e["name"]}"' if e.get("name") else e["id"] for e in entry.get("examples", [])]
    return "; e.g. " + ", ".join(names) if names else ""


def _calendar_findings(calendar: dict[str, Any]) -> list[tuple[str, str]]:
    """Day types that run on no date in MOTIS, though the file loads cleanly."""
    out: list[tuple[str, str]] = []
    none = calendar.get("no_dates") or {}
    if none.get("count"):
        out.append(
            (
                RED,
                f"{none['count']} day types ({none['references']} references from journeys) have "
                "no DayTypeAssignment and no DaysOfWeek: their days are only in the name, "
                "they run on no date" + _examples(none),
            )
        )
    weekly = calendar.get("weekdays_only") or {}
    if weekly.get("count"):
        out.append(
            (
                RED,
                f"{weekly['count']} day types ({weekly['references']} references from journeys) give "
                "DaysOfWeek but no positive DayTypeAssignment: neither MOTIS (nigiri:684-689) nor "
                "VIATOR's converter turns weekdays into dates, they run on no date"
                + _examples(weekly),
            )
        )
    return out


def _rule_findings(current: Fingerprint) -> list[tuple[str, str]]:
    """What MOTIS will not read in this file, whatever came before."""
    el = current.elements
    out: list[tuple[str, str]] = []
    if not el.get("ServiceJourney"):
        out.append((RED, "no ServiceJourney in the file: no course can be loaded"))
    elif _journeys_without_calendar(el):
        out.append((RED, "courses but no calendar (DayTypeAssignment, AvailabilityCondition)"))
    out += [
        (rule.level, f"{rule.element} (x{el[rule.element]}): {rule.message}")
        for rule in RULES
        if el.get(rule.element)
    ]
    out += _ratio_rules(el)
    out += _calendar_findings(current.calendar)
    # gml:pos is read with its literal prefix (nigiri:156-171); any other
    # prefixed element is invisible to the loader's unprefixed XPaths.
    out += [
        (WARN, f"{n} elements written with prefix '{prefix}:': MOTIS reads unprefixed names")
        for prefix, n in current.prefixes.items()
        if prefix != "gml"
    ]
    return out


def _diff_findings(current: Fingerprint, previous: dict[str, Any]) -> list[tuple[str, str]]:
    """What changed since the previous download: header values, element names."""
    before_header = previous.get("header") or {}
    out = [
        (WARN, f"header {key} changed: {before_header.get(key, '') or '∅'} → {now or '∅'}")
        for key, now in current.header.items()
        if before_header.get(key, "") != now
    ]
    el = current.elements
    before = previous.get("elements") or {}
    appeared = sorted(set(el) - set(before))
    vanished = sorted(set(before) - set(el))
    if appeared:
        out.append((WARN, "new elements since last download: " + ", ".join(appeared[:20])))
    if vanished:
        out.append((WARN, "elements gone since last download: " + ", ".join(vanished[:20])))
    return out


def assess(current: Fingerprint, previous: dict[str, Any] | None) -> Assessment:
    """Rules against MOTIS, then the difference with the previous download."""
    findings = _rule_findings(current)
    if previous and previous.get("version") == FINGERPRINT_VERSION:
        findings += _diff_findings(current, previous)
    result = Assessment()
    for level, message in findings:
        result.add(level, message)
    return result
