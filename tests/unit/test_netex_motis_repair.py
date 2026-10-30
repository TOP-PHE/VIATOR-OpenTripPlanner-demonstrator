"""Repairs, in MOTIS's copy of a NeTEx file, of the forms MOTIS drops a whole
file for (app/netex_motis_repair.py, run by netex_calendar.convert_zip).

ÖBB's NAP file, 2026-10-28: 247 of 431 line files dropped by MOTIS 2.11.2.
232 had courses whose passing times skip points of their pattern (7 for 8),
59 a LineRef to a Line of another file, 60 a dangling ParentSiteRef.

`_motis_problems` mirrors what nigiri checks (its `utl::zip` + `utl::verify`
on passing times, `stops.at(parent)`, `lines.at(...)`): every repaired file
must pass it.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from defusedxml.ElementTree import fromstring

from app import netex_calendar as nc
from app import netex_motis_repair as rp

NETEX = "http://www.netex.org.uk/netex"
NS = "{" + NETEX + "}"


def _points(pattern: str, n: int) -> str:
    return "".join(
        f'<StopPointInJourneyPattern id="{pattern}-{i}" order="{i}">'
        f'<ScheduledStopPointRef ref="SSP{i}"/></StopPointInJourneyPattern>'
        for i in range(1, n + 1)
    )


def _course(jid: str, pattern: str, points: list[int]) -> str:
    times = "".join(
        f'<TimetabledPassingTime><StopPointInJourneyPatternRef ref="{pattern}-{i}"/>'
        f"<DepartureTime>0{i}:00:00</DepartureTime></TimetabledPassingTime>"
        for i in points
    )
    return (
        f'<ServiceJourney id="{jid}" version="1"><ServiceJourneyPatternRef ref="{pattern}"/>'
        f"<passingTimes>{times}</passingTimes></ServiceJourney>"
    )


RESOURCES = (
    '<ResourceFrame id="r"><organisations>'
    '<Operator id="OP1" version="1"><Name>OEBB</Name></Operator></organisations>'
    '<typesOfValue><ValueSet id="vs"><values>'
    '<TypeOfProductCategory id="CAT-S" version="1"><Name>S</Name></TypeOfProductCategory>'
    "</values></ValueSet></typesOfValue></ResourceFrame>"
)
LINE_L1 = (
    '<Line id="L1" version="1"><Name>S1</Name><TypeOfProductCategoryRef ref="CAT-S"/>'
    '<additionalOperators><OperatorRef ref="OP1"/></additionalOperators></Line>'
)


def _doc(
    *,
    resources: str = RESOURCES,
    lines: str = LINE_L1,
    line_ref: str = "L1",
    courses: str = "",
    sites: str = "",
    meetings: str = "",
    points: int = 4,
    route_view: str | None = None,
    service_extra: str = "",
    calendar: str | None = None,
) -> bytes:
    view = (
        f'<RouteView><LineRef ref="{line_ref}"/></RouteView>' if route_view is None else route_view
    )
    frame = (
        ""
        if calendar is None
        else f"<ServiceCalendarFrame id='scf'>{calendar}</ServiceCalendarFrame>"
    )
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><PublicationDelivery xmlns="{NETEX}" version="1.10">'
        "<dataObjects><CompositeFrame id='c'><frames>"
        f"{resources}{frame}"
        f"<ServiceFrame id='sf'>{service_extra}<lines>{lines}</lines><journeyPatterns>"
        f'<ServiceJourneyPattern id="P1" version="1">{view}'
        f"<pointsInSequence>{_points('P1', points)}</pointsInSequence></ServiceJourneyPattern>"
        "</journeyPatterns></ServiceFrame>"
        f"<SiteFrame id='st'><stopPlaces>{sites}</stopPlaces></SiteFrame>"
        f"<TimetableFrame id='t'><vehicleJourneys>{courses}</vehicleJourneys>"
        f"<journeyMeetings>{meetings}</journeyMeetings></TimetableFrame>"
        "</frames></CompositeFrame></dataObjects></PublicationDelivery>"
    ).encode()


def _ids(root, tag: str) -> set[str]:
    return {e.get("id") for e in root.iter(NS + tag)}


def _motis_problems(data: bytes) -> list[str]:
    """What would make nigiri (MOTIS 2.11) drop this file."""
    root = fromstring(data)
    patterns = {
        p.get("id"): [sp.get("id") for sp in p.find(NS + "pointsInSequence")]
        for p in root.iter(NS + "ServiceJourneyPattern")
    }
    problems = []
    for sj in root.iter(NS + "ServiceJourney"):
        if sj.find(NS + "calls") is not None:
            continue
        ref = sj.find(NS + "ServiceJourneyPatternRef").get("ref")
        if ref not in patterns:
            continue
        times = [
            t.find(NS + "StopPointInJourneyPatternRef").get("ref")
            for t in sj.find(NS + "passingTimes")
        ]
        if times != patterns[ref] or len(times) < 2:
            problems.append(f"course {sj.get('id')}: {times} vs {patterns[ref]}")
    sites = _ids(root, "StopPlace") | _ids(root, "Quay")
    problems += [
        f"parent {p.get('ref')}"
        for p in root.iter(NS + "ParentSiteRef")
        if p.get("ref") not in sites
    ]
    lines = _ids(root, "Line")
    problems += [
        f"line {r.get('ref')}" for r in root.iter(NS + "LineRef") if r.get("ref") not in lines
    ]
    for tag, kind in (
        ("OperatorRef", "Operator"),
        ("TypeOfProductCategoryRef", "TypeOfProductCategory"),
    ):
        defined = _ids(root, kind)
        problems += [
            f"{tag} {r.get('ref')}" for r in root.iter(NS + tag) if r.get("ref") not in defined
        ]
    # A stay-seated meeting's condition is read from the ServiceCalendarFrame only.
    readable = {
        ac.get("id")
        for frame in root.iter(NS + "ServiceCalendarFrame")
        for vc in frame.findall(NS + "validityConditions")
        for ac in vc.findall(NS + "AvailabilityCondition")
    }
    problems += [
        f"condition {r.get('ref')}"
        for m in root.iter(NS + "JourneyMeeting")
        for r in m.iter(NS + "AvailabilityConditionRef")
        if r.get("ref") not in readable
    ]
    return problems


def _lineless_patterns(data: bytes) -> list[str]:
    """Patterns MOTIS would give its empty default line (shown as a tram)."""
    return [
        p.get("id")
        for p in fromstring(data).iter(NS + "ServiceJourneyPattern")
        if p.find(f"{NS}RouteView/{NS}LineRef") is None
    ]


def _course_pattern(data: bytes, jid: str) -> str | None:
    for sj in fromstring(data).iter(NS + "ServiceJourney"):
        if sj.get("id") == jid:
            return sj.find(NS + "ServiceJourneyPatternRef").get("ref")
    return None


# ── courses that skip points of their pattern ───────────────────────


ALL_COURSES = (
    _course("J-full", "P1", [1, 2, 3, 4])
    + _course("J-skip", "P1", [1, 2, 4])  # the ÖBB case: 3 of 4 points
    + _course("J-skip2", "P1", [1, 2, 4])  # same points: same derived pattern
    + _course("J-other", "P1", [1, 3])
    + _course("J-backwards", "P1", [2, 1])
    + _course("J-one", "P1", [1])
    + _course("J-elsewhere", "P-in-a-base-file", [1, 2])
    + '<ServiceJourney id="J-calls"><calls><Call/></calls></ServiceJourney>'
)
MEETINGS = (
    '<JourneyMeeting id="M1"><FromJourneyRef ref="J-backwards"/><ToJourneyRef ref="J-full"/></JourneyMeeting>'
    '<JourneyMeeting id="M2"><FromJourneyRef ref="J-full"/><ToJourneyRef ref="J-skip"/></JourneyMeeting>'
)


def test_a_course_skipping_points_gets_a_pattern_of_its_own() -> None:
    before = _doc(courses=ALL_COURSES, meetings=MEETINGS)
    assert any(p.startswith("course J-skip") for p in _motis_problems(before))
    stats = rp.RepairStats()
    out = rp.repair(before, None, stats)

    assert _motis_problems(out) == []
    assert _course_pattern(out, "J-full") == "P1"
    assert _course_pattern(out, "J-skip") == _course_pattern(out, "J-skip2") == "P1-viator-1"
    assert _course_pattern(out, "J-other") == "P1-viator-2"
    # A course whose pattern another (base) file may define is left alone.
    assert _course_pattern(out, "J-elsewhere") == "P-in-a-base-file"
    root = fromstring(out)
    derived = {
        p.get("id"): [sp.get("id") for sp in p.find(NS + "pointsInSequence")]
        for p in root.iter(NS + "ServiceJourneyPattern")
    }
    assert derived == {
        "P1": ["P1-1", "P1-2", "P1-3", "P1-4"],
        "P1-viator-1": ["P1-1", "P1-2", "P1-4"],
        "P1-viator-2": ["P1-1", "P1-3"],
    }
    # The derived pattern keeps the rest of its pattern: the line.
    assert out.count(b'<LineRef ref="L1"/>') == 3
    assert {sj.get("id") for sj in root.iter(NS + "ServiceJourney")} == {
        "J-full",
        "J-skip",
        "J-skip2",
        "J-other",
        "J-elsewhere",
        "J-calls",
    }
    assert _ids(root, "JourneyMeeting") == {"M2"}
    assert stats.journeys_refitted == 3
    assert stats.patterns_derived == 2
    assert stats.journeys_dropped == 2
    assert stats.links_dropped == 1
    assert stats.members == 1
    assert stats.summary() == (
        "3 courses given one of 2 patterns without the points they skip, "
        "2 unreadable courses dropped (1 meetings/interchanges)"
    )


def test_a_readable_file_is_returned_as_it_is() -> None:
    data = _doc(courses=_course("J", "P1", [1, 2, 3, 4]), sites='<StopPlace id="S1"/>')
    stats = rp.RepairStats()
    assert rp.repair(data, None, stats) is data
    assert stats == rp.RepairStats()
    assert stats.summary() == ""


def test_a_pattern_of_one_point_drops_its_courses() -> None:
    data = _doc(courses=_course("J", "P1", [1]), points=1)
    stats = rp.RepairStats()
    out = rp.repair(data, None, stats)
    assert b"<ServiceJourney " not in out
    assert stats.journeys_dropped == 1


# ── ParentSiteRef ───────────────────────────────────────────────────


def test_a_dangling_parent_is_removed_and_a_known_one_kept() -> None:
    sites = (
        '<StopPlace id="S1"><ParentSiteRef ref="at-44-42107" version="any"/><Name>A</Name></StopPlace>'
        '<StopPlace id="S2"><ParentSiteRef ref="S1"></ParentSiteRef>'
        '<quays><Quay id="Q1"><ParentSiteRef ref="S2"/></Quay></quays></StopPlace>'
    )
    data = _doc(courses=_course("J", "P1", [1, 2, 3, 4]), sites=sites)
    assert _motis_problems(data) == ["parent at-44-42107"]
    stats = rp.RepairStats()
    out = rp.repair(data, None, stats)
    assert _motis_problems(out) == []
    assert b"at-44-42107" not in out
    assert out.count(b"<ParentSiteRef") == 2
    assert stats.parents_dropped == 1
    assert stats.summary() == "1 dangling ParentSiteRef removed"


# ── a Line defined in another file ─────────────────────────────────


def test_a_missing_line_is_copied_with_what_it_names() -> None:
    home = _doc(courses=_course("J0", "P1", [1, 2, 3, 4]))
    # This file has organisations but no product category list.
    needing = _doc(
        resources='<ResourceFrame id="r"><organisations></organisations></ResourceFrame>',
        lines="",
        courses=_course("J", "P1", [1, 2, 4]),
    )
    assert rp.missing_lines(needing) == {b"L1"}
    assert rp.missing_lines(home) == set()
    stats = rp.RepairStats()
    out = rp.repair(needing, lambda line_id: rp.line_copy(home, line_id), stats)

    assert _motis_problems(out) == []
    root = fromstring(out)
    assert _ids(root, "Line") == {"L1"}
    assert _ids(root, "Operator") == {"OP1"}
    # No ValueSet to put the category in: the copy names none (MOTIS's default).
    assert b"TypeOfProductCategoryRef" not in out
    assert stats.lines_copied == 1
    assert stats.dependencies_copied == 1
    assert stats.dependencies_dropped == 1
    assert stats.journeys_refitted == 1
    assert "1 lines copied from another file" in stats.summary()


def test_a_line_found_nowhere_is_counted() -> None:
    data = _doc(lines="", courses=_course("J", "P1", [1, 2, 3, 4]))
    stats = rp.RepairStats()
    assert rp.repair(data, lambda _id: None, stats) is data
    assert stats.lines_not_found == 1


def test_a_known_dependency_is_not_copied_twice() -> None:
    home = _doc()
    needing = _doc(lines="")  # same resources: operator and category already there
    stats = rp.RepairStats()
    out = rp.repair(needing, lambda line_id: rp.line_copy(home, line_id), stats)
    assert out.count(b'<Operator id="OP1"') == 1
    assert out.count(b'<TypeOfProductCategory id="CAT-S"') == 1
    assert stats.dependencies_copied == 0
    assert _motis_problems(out) == []


def test_line_copy_of_an_unknown_line_is_none() -> None:
    assert rp.line_copy(_doc(), b"nope") is None


def test_base_files_are_recognised_as_motis_does() -> None:
    assert rp.has_base_files(["PROD_NETEX_TT_1_1_SCHWEIZ_SITE.xml", "line.xml"])
    assert not rp.has_base_files(["NX-PI_01_at_obb_LINE_obb_EU_PI_STOP_OFFER_20261022.xml"])


# ── AvailabilityConditions outside the ServiceCalendarFrame ─────────


def _condition(ident: str, bits: bool = True) -> str:
    days = "<ValidDayBits>0110</ValidDayBits>" if bits else ""
    return (
        f'<AvailabilityCondition id="{ident}" version="any">'
        f"<FromDate>2025-12-14T00:00:00</FromDate>{days}</AvailabilityCondition>"
    )


def _meeting(ident: str, condition: str) -> str:
    return (
        f'<JourneyMeeting id="{ident}"><validityConditions>'
        f'<AvailabilityConditionRef ref="{condition}"/></validityConditions>'
        '<FromJourneyRef ref="J"/><ToJourneyRef ref="J"/><StaySeated>true</StaySeated>'
        "</JourneyMeeting>"
    )


def test_conditions_in_the_service_frame_are_copied_into_the_calendar_frame() -> None:
    """ÖBB, 2026-10-28: 1 116 conditions under ServiceFrame/validityConditions,
    named by stay-seated meetings; MOTIS 2.11.2 dropped 44 files on them."""
    data = _doc(
        courses=_course("J", "P1", [1, 2, 3, 4]),
        service_extra="<validityConditions>"
        + _condition("AC1")
        + _condition("AC-no-bits", bits=False)
        + _condition("AC-known")
        + "</validityConditions>",
        calendar="<validityConditions>" + _condition("AC-known") + "</validityConditions>",
        meetings=_meeting("M1", "AC1"),
    )
    assert _motis_problems(data) == ["condition AC1"]
    stats = rp.RepairStats()
    out = rp.repair(data, None, stats)
    assert _motis_problems(out) == []
    assert stats.conditions_copied == 1  # neither the one without days nor the known one
    assert out.count(b'id="AC-known"') == 2
    assert out.count(b'id="AC-no-bits"') == 1
    assert "1 validity conditions copied into the ServiceCalendarFrame" in stats.summary()


def test_conditions_are_left_alone_without_a_calendar_frame() -> None:
    data = _doc(
        courses=_course("J", "P1", [1, 2, 3, 4]),
        service_extra="<validityConditions>" + _condition("AC1") + "</validityConditions>",
    )
    stats = rp.RepairStats()
    assert rp.repair(data, None, stats) is data
    assert stats.conditions_copied == 0


# ── a pattern without RouteView/LineRef ─────────────────────────────


def _course_on_line(jid: str, line: str) -> str:
    return _course(jid, "P1", [1, 2, 3, 4]).replace(
        "<ServiceJourneyPatternRef", f'<LineRef ref="{line}"/><ServiceJourneyPatternRef'
    )


@pytest.mark.parametrize(
    "view",
    ["", "<RouteView/>", "<RouteView><Name>x</Name></RouteView>"],
    ids=["no RouteView", "empty RouteView", "RouteView without LineRef"],
)
def test_a_pattern_gets_the_line_its_courses_name(view: str) -> None:
    """ÖBB, 2026-10-28: every train came out as a tram with no name, its
    patterns naming no line (MOTIS's default line has route type 0)."""
    lines = LINE_L1 + LINE_L1.replace('"L1"', '"L2"')
    courses = (
        _course_on_line("J1", "L2") + _course_on_line("J2", "L2") + _course_on_line("J3", "L1")
    )
    data = _doc(route_view=view, lines=lines, courses=courses)
    assert _lineless_patterns(data) == ["P1"]
    stats = rp.RepairStats()
    out = rp.repair(data, None, stats)
    assert _lineless_patterns(out) == []
    root = fromstring(out)
    pattern = next(root.iter(NS + "ServiceJourneyPattern"))
    assert pattern.find(f"{NS}RouteView/{NS}LineRef").get("ref") == "L2"  # most courses
    assert _motis_problems(out) == []
    assert stats.lines_given == 1
    assert "1 patterns given their line" in stats.summary()


def test_a_pattern_gets_its_routes_line_first() -> None:
    lines = LINE_L1 + LINE_L1.replace('"L1"', '"L2"')
    route = '<routes><Route id="R1"><LineRef ref="L2"/></Route></routes>'
    data = _doc(
        route_view='<RouteRef ref="R1"/>',
        lines=lines,
        service_extra=route,
        courses=_course_on_line("J1", "L1"),
    )
    out = rp.repair(data, None, rp.RepairStats())
    pattern = next(fromstring(out).iter(NS + "ServiceJourneyPattern"))
    assert pattern.find(f"{NS}RouteView/{NS}LineRef").get("ref") == "L2"


def test_a_pattern_gets_the_files_only_line_last() -> None:
    data = _doc(route_view="", courses=_course("J", "P1", [1, 2, 3, 4]))
    out = rp.repair(data, None, rp.RepairStats())
    assert _lineless_patterns(out) == []


def test_a_pattern_with_no_clue_is_counted() -> None:
    lines = LINE_L1 + LINE_L1.replace('"L1"', '"L2"')
    data = _doc(route_view="", lines=lines, courses=_course("J", "P1", [1, 2, 3, 4]))
    stats = rp.RepairStats()
    assert rp.repair(data, None, stats) is data
    assert stats.patterns_without_line == 1
    assert stats.summary() == "1 patterns with no line"


def test_a_derived_pattern_keeps_the_line_given() -> None:
    data = _doc(route_view="", courses=_course("J", "P1", [1, 2, 4]))
    stats = rp.RepairStats()
    out = rp.repair(data, None, stats)
    assert _lineless_patterns(out) == []
    assert _course_pattern(out, "J") == "P1-viator-1"
    assert stats.lines_given == 1
    assert stats.journeys_refitted == 1


# ── the build copy (netex_calendar.convert_zip) ────────────────────


def _zip(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for name, body in members.items():
            z.writestr(name, body)
    return path


def test_the_build_copy_repairs_every_file_of_the_zip(tmp_path: Path) -> None:
    """The file naming another file's Line comes FIRST in the zip: it waits
    until the Line's file has been seen, then gets the Line."""
    needing = _doc(lines="", courses=_course("J", "P1", [1, 2, 4]))
    home = _doc(courses=_course("J0", "P1", [1, 2, 3, 4]))
    src = _zip(
        tmp_path / "obb.zip", {"a_line.xml": needing, "b_line.xml": home, "readme.txt": b"x"}
    )
    dest = tmp_path / "out" / "obb.zip"
    dest.parent.mkdir()

    stats = nc.convert_zip(src, dest)
    assert stats is not None
    with zipfile.ZipFile(dest) as z:
        assert sorted(z.namelist()) == ["a_line.xml", "b_line.xml", "readme.txt"]
        assert _motis_problems(z.read("a_line.xml")) == []
        assert z.read("b_line.xml") == home  # needed nothing: byte for byte
        assert z.read("readme.txt") == b"x"
    assert stats.summary() == (
        "1 courses given one of 1 patterns without the points they skip, "
        "1 lines copied from another file"
    )
    assert not [p for p in dest.parent.iterdir() if p.name != "obb.zip"]  # no temp left


def test_a_zip_with_base_files_is_not_repaired(tmp_path: Path) -> None:
    data = _doc(lines="", courses=_course("J", "P1", [1, 2, 4]))
    src = _zip(
        tmp_path / "sbb.zip", {"PROD_NETEX_TT_1_1_SCHWEIZ_SERVICE.xml": _doc(), "l.xml": data}
    )
    dest = tmp_path / "out.zip"
    assert nc.convert_zip(src, dest) is None
    assert not dest.exists()


def test_a_member_too_large_is_only_streamed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _doc(courses=_course("J", "P1", [1, 2, 4]))
    src = _zip(tmp_path / "x.zip", {"big.xml": data})
    monkeypatch.setattr(rp, "MAX_MEMBER_BYTES", 10)
    assert nc.convert_zip(src, tmp_path / "out.zip") is None


def test_the_build_reads_the_repaired_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app import worker

    graphs = tmp_path / "graphs"
    monkeypatch.setattr(worker.settings, "graph_dir", graphs)
    netex = tmp_path / "inbox" / "eu19" / "netex"
    netex.mkdir(parents=True)
    src = _zip(netex / "obb.zip", {"l.xml": _doc(courses=_course("J", "P1", [1, 2, 4]))})
    notes: list[str] = []
    path = worker._motis_timetable_path("eu19", src, graphs / "motis" / "eu19", notes)
    assert path == "/graphs/motis/eu19/_netex/obb.zip"
    assert notes == [
        "netex for MOTIS: obb.zip rewritten "
        "(1 courses given one of 1 patterns without the points they skip)"
    ]


def test_a_new_converter_version_redoes_cached_copies() -> None:
    assert nc.CONVERTER_VERSION >= 5


# ── shared files read as MOTIS base files (NMBS, 2026-10-29) ───────────


def _publication(frames: str) -> bytes:
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><PublicationDelivery xmlns="{NETEX}">'
        f"<dataObjects><CompositeFrame id='c'><frames>{frames}</frames></CompositeFrame>"
        "</dataObjects></PublicationDelivery>"
    ).encode()


STOPS = _publication(
    "<SiteFrame id='s'><stopPlaces><StopPlace id='gs:S1'><Name>Evere</Name></StopPlace>"
    "</stopPlaces></SiteFrame>"
)


def _common(extra_day_types: int = 0) -> bytes:
    """NMBS's common.xml: the operator, every DayType, a period of zeros + dates."""
    extra = "".join(f'<DayType id="gc:unused-{i}"/>' for i in range(extra_day_types))
    return _publication(
        "<ResourceFrame id='r'><organisations><Operator id='nmbssncb'/></organisations>"
        f"</ResourceFrame><ServiceCalendarFrame id='scf'><dayTypes><DayType id=\"gc:DT1\"/>{extra}"
        '</dayTypes><operatingPeriods><UicOperatingPeriod id="gc:DT1">'
        "<FromDate>2026-11-01T23:00:00Z</FromDate><ToDate>2026-11-03T23:00:00Z</ToDate>"
        "<ValidDayBits>000</ValidDayBits></UicOperatingPeriod></operatingPeriods>"
        '<dayTypeAssignments><DayTypeAssignment id="a1" order="1"><OperatingPeriodRef ref="gc:DT1"/>'
        '<DayTypeRef ref="gc:DT1"/></DayTypeAssignment><DayTypeAssignment id="a2" order="2">'
        '<Date>2026-11-02</Date><DayTypeRef ref="gc:DT1"/></DayTypeAssignment>'
        "</dayTypeAssignments></ServiceCalendarFrame>"
    )


def _line_file(own_day_types: str = "") -> bytes:
    """A line file whose course runs on DayType gc:DT1, defined here or not."""
    course = _course("J", "P1", [1, 2, 3, 4]).replace(
        "<ServiceJourneyPatternRef",
        '<dayTypes><DayTypeRef ref="gc:DT1"/></dayTypes><ServiceJourneyPatternRef',
    )
    calendar = f"<dayTypes>{own_day_types}</dayTypes>" if own_day_types else None
    return _doc(courses=course, calendar=calendar)


def test_shared_files_become_base_files_when_line_files_take_their_day_types(
    tmp_path: Path,
) -> None:
    line = _line_file()
    src = _zip(
        tmp_path / "nmbs.zip", {"stops.xml": STOPS, "common.xml": _common(), "line-1.xml": line}
    )
    dest = tmp_path / "out" / "nmbs.zip"
    dest.parent.mkdir()

    stats = nc.convert_zip(src, dest)
    assert stats is not None
    with zipfile.ZipFile(dest) as z:
        names = sorted(z.namelist())
        assert names == ["_1_1_SCHWEIZ_common.xml", "_1_1_SCHWEIZ_stops.xml", "line-1.xml"]
        assert rp.has_base_files(names)
        assert z.read("line-1.xml") == line
        assert z.read("_1_1_SCHWEIZ_stops.xml") == STOPS
        common = z.read("_1_1_SCHWEIZ_common.xml").decode()
    assert '<UicOperatingPeriod version="1" id="VIATOR:UicOperatingPeriod:gc:DT1">' in common
    assert "<FromDate>2026-11-02T00:00:00</FromDate>" in common
    assert "<ValidDayBits>1</ValidDayBits>" in common
    assert stats.summary() == (
        "2 day-type assignments rewritten as 1 UicOperatingPeriod; "
        "2 shared files read as base files (stops.xml, common.xml)"
    )
    assert [p.name for p in dest.parent.iterdir()] == ["nmbs.zip"]  # no temp left


@pytest.mark.parametrize(
    "members",
    [
        # ÖBB-like: each line file defines its own day types.
        {"EU_PI_STOP_OFFER.xml": STOPS, "line-1.xml": _line_file('<DayType id="gc:DT1"/>')},
        # The DayType named is defined in no shared file.
        {"stops.xml": STOPS, "line-1.xml": _line_file()},
    ],
    ids=["own-day-types", "day-type-nowhere"],
)
def test_shared_files_keep_their_name_otherwise(tmp_path: Path, members: dict[str, bytes]) -> None:
    src = _zip(tmp_path / "x.zip", members)
    assert nc.convert_zip(src, tmp_path / "out.zip") is None
    assert not (tmp_path / "out.zip").exists()


def test_a_large_shared_file_is_scanned_as_a_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    common, line = _common(extra_day_types=200), _line_file()
    assert len(common) > len(line)
    monkeypatch.setattr(rp, "MAX_MEMBER_BYTES", len(line))
    monkeypatch.setattr(nc, "_CHUNK", 64)  # DayType tags across reads
    src = _zip(tmp_path / "nmbs.zip", {"common.xml": common, "line-1.xml": line})
    dest = tmp_path / "out" / "nmbs.zip"
    dest.parent.mkdir()

    stats = nc.convert_zip(src, dest)
    assert stats is not None
    with zipfile.ZipFile(dest) as z:
        assert sorted(z.namelist()) == ["_1_1_SCHWEIZ_common.xml", "line-1.xml"]
        assert "VIATOR:UicOperatingPeriod:gc:DT1" in z.read("_1_1_SCHWEIZ_common.xml").decode()
    assert stats.base_files == ["common.xml"]
    assert stats.repairs.too_large == 1


def test_base_file_names_and_member_kinds() -> None:
    assert rp.base_file_name("common.xml") == "_1_1_SCHWEIZ_common.xml"
    assert rp.base_file_name("nmbs/common.xml") == "nmbs/_1_1_SCHWEIZ_common.xml"
    assert rp.has_base_files([rp.base_file_name("x.xml")])
    assert rp.has_courses(b'<ServiceJourney id="J">')
    assert not rp.has_courses(b'<ServiceJourneyPattern id="P"/>')
    data = (
        b'<DayType id="A"/><DayType id="B"><Name/></DayType><DayTypeAssignment id="C"/>'
        b'<DayTypeRef ref="A"/><DayTypeRef ref="Z"/>'
    )
    assert rp.day_type_ids(data) == {b"A", b"B"}
    assert rp.day_type_ids(data, before=data.index(b'<DayType id="B"')) == {b"A"}
    assert rp.foreign_day_types(data) == {b"Z"}
    assert rp.foreign_day_types(b"<x/>") == set()
    assert rp.day_type_ids(b"<x/>") == set()
