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
  nigiri's loader.

Warnings never block the file: keeping an old timetable silently would hide
the change just as well. Counting is a byte scan, not an XML parse (~110 MB/s
of XML; a full parse took 40 min on DB's 2 GB zip).
"""

from __future__ import annotations

import collections
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Bumped when the fingerprint changes shape, so old ones are not compared.
FINGERPRINT_VERSION = 1

_CHUNK = 8 * 1024 * 1024
# `<name` of every start tag; closing tags (`</`), comments (`<!`) and
# processing instructions (`<?`) do not match. The prefix is stripped later.
_START_TAG_RE = re.compile(rb"<([A-Za-z_][\w.:-]*)")
_ROOT_RE = re.compile(rb"<(?:[\w.-]+:)?PublicationDelivery\b[^>]*>", re.S)
_VERSION_RE = re.compile(rb"""\sversion=["']([^"']*)["']""")
_XSD_RE = re.compile(rb"""schemaLocation=["'][^"']*?/([\d.]+)/xsd""")
_PROFILE_RE = re.compile(rb"<!--\s*Profile:?\s*([^-]*?)\s*-->")
_PARTICIPANT_RE = re.compile(rb"<(?:[\w.-]+:)?ParticipantRef>([^<]*)<")

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

    def to_state(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "header": self.header,
            "xml_files": self.xml_files,
            "elements": self.elements,
            "prefixes": self.prefixes,
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


def _count(stream: Any, counts: collections.Counter[bytes]) -> bytes:
    """Count start tags of one XML stream; returns its first bytes."""
    head = b""
    tail = b""
    while chunk := stream.read(_CHUNK):
        if not head:
            head = chunk[:8192]
        buf = tail + chunk
        cut = buf.rfind(b"<")  # a tag split across reads is counted next time
        counts.update(_START_TAG_RE.findall(buf, 0, cut))
        tail = buf[cut:]
    counts.update(_START_TAG_RE.findall(tail))
    return head


def fingerprint(path: Path) -> Fingerprint:
    """Fingerprint a NeTEx zip (or a bare XML file)."""
    counts: collections.Counter[bytes] = collections.Counter()
    fp = Fingerprint()
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            names = sorted(n for n in z.namelist() if n.lower().endswith(".xml"))
            for name in names:
                with z.open(name) as f:
                    head = _count(f, counts)
                if not fp.header:
                    fp.header = _header(head)
            fp.xml_files = len(names)
    else:
        with path.open("rb") as f:
            fp.header = _header(_count(f, counts))
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
    return fp


def _journeys_without_calendar(elements: dict[str, int]) -> bool:
    has_journeys = elements.get("ServiceJourney", 0) > 0
    calendars = (
        elements.get("DayTypeAssignment", 0)
        + elements.get("AvailabilityCondition", 0)
        + elements.get("UicOperatingPeriod", 0)
    )
    return has_journeys and calendars == 0


def assess(current: Fingerprint, previous: dict[str, Any] | None) -> Assessment:
    """Rules against MOTIS, then the difference with the previous download."""
    result = Assessment()
    el = current.elements
    if not el.get("ServiceJourney"):
        result.add(RED, "no ServiceJourney in the file: no course can be loaded")
    elif _journeys_without_calendar(el):
        result.add(RED, "courses but no calendar (DayTypeAssignment, AvailabilityCondition)")
    for rule in RULES:
        if el.get(rule.element):
            result.add(rule.level, f"{rule.element} (x{el[rule.element]}): {rule.message}")
    for level, message in _ratio_rules(el):
        result.add(level, message)
    # gml:pos is read with its literal prefix (nigiri:156-171); any other
    # prefixed element is invisible to the loader's unprefixed XPaths.
    for prefix, n in current.prefixes.items():
        if prefix != "gml":
            result.add(
                WARN, f"{n} elements written with prefix '{prefix}:': MOTIS reads unprefixed names"
            )

    if not previous or previous.get("version") != FINGERPRINT_VERSION:
        return result
    before_header = previous.get("header") or {}
    for key, now in current.header.items():
        was = before_header.get(key, "")
        if was != now:
            result.add(WARN, f"header {key} changed: {was or '∅'} → {now or '∅'}")
    before = previous.get("elements") or {}
    appeared = sorted(set(el) - set(before))
    vanished = sorted(set(before) - set(el))
    if appeared:
        result.add(WARN, "new elements since last download: " + ", ".join(appeared[:20]))
    if vanished:
        result.add(WARN, "elements gone since last download: " + ", ".join(vanished[:20]))
    return result
