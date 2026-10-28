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
) -> bytes:
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><PublicationDelivery xmlns="{NETEX}" version="1.10">'
        "<dataObjects><CompositeFrame id='c'><frames>"
        f"{resources}"
        f"<ServiceFrame id='sf'><lines>{lines}</lines><journeyPatterns>"
        f'<ServiceJourneyPattern id="P1" version="1"><RouteView><LineRef ref="{line_ref}"/></RouteView>'
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
    return problems


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
    assert nc.CONVERTER_VERSION >= 4
