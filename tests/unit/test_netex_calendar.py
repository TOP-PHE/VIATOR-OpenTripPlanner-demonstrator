"""NeTEx calendars rewritten into the model MOTIS reads (app/netex_calendar.py).

The fixture mirrors the Slovenian national file (nap.si `b2b.netex.lines`,
2026-10-02): `OperatingDay`s, plain `OperatingPeriod`s, `DayTypeAssignment`s by
`OperatingDayRef` and by `OperatingPeriodRef`, and `isAvailable` exclusions.
MOTIS 2.10/2.11 dropped that whole file, so no Slovenian stop was loaded.
"""

from __future__ import annotations

import io
import re
import zipfile
from datetime import date, timedelta
from pathlib import Path

import pytest

from app import netex_calendar as nc

NS = 'xmlns="http://www.netex.org.uk/netex" xmlns:gml="http://www.opengis.net/gml/3.2"'

CALENDAR = """<ServiceCalendarFrame version="1" id="SI:SCF:1">
  <dayTypes>
    <DayType version="1" id="SI:DT:dated"/>
    <DayType version="1" id="SI:DT:weekdays">
      <properties><PropertyOfDay><DaysOfWeek>Weekdays</DaysOfWeek></PropertyOfDay></properties>
    </DayType>
    <DayType version="1" id="SI:DT:unused"/>
  </dayTypes>
  <operatingDays>
    <OperatingDay version="1" id="SI:OD:1"><CalendarDate>2026-10-05</CalendarDate></OperatingDay>
    <OperatingDay version="1" id="SI:OD:2"><CalendarDate>2026-10-07</CalendarDate></OperatingDay>
    <OperatingDay version="1" id="SI:OD:3"><CalendarDate>2026-10-08</CalendarDate></OperatingDay>
  </operatingDays>
  <operatingPeriods>
    <OperatingPeriod version="1" id="SI:OP:oct">
      <FromDate>2026-10-01T00:00:00</FromDate><ToDate>2026-10-11T00:00:00</ToDate>
    </OperatingPeriod>
  </operatingPeriods>
  <dayTypeAssignments>
    <DayTypeAssignment version="1" id="SI:DTA:1" order="1">
      <OperatingDayRef ref="SI:OD:1"/><DayTypeRef ref="SI:DT:dated"/>
    </DayTypeAssignment>
    <DayTypeAssignment version="1" id="SI:DTA:2" order="2">
      <OperatingDayRef ref="SI:OD:2"/><DayTypeRef ref="SI:DT:dated"/>
    </DayTypeAssignment>
    <DayTypeAssignment version="1" id="SI:DTA:3" order="3">
      <OperatingPeriodRef ref="SI:OP:oct"/><DayTypeRef ref="SI:DT:weekdays"/>
    </DayTypeAssignment>
    <DayTypeAssignment version="1" id="SI:DTA:4" order="4">
      <OperatingDayRef ref="SI:OD:3"/><DayTypeRef ref="SI:DT:weekdays"/><isAvailable>false</isAvailable>
    </DayTypeAssignment>
  </dayTypeAssignments>
</ServiceCalendarFrame>"""

BEFORE = "<SiteFrame id='s'><stopPlaces><StopPlace id='SI:SP:1'/></stopPlaces></SiteFrame>"
AFTER = "<TimetableFrame id='t'><vehicleJourneys/></TimetableFrame>"


def _doc(calendar: str = CALENDAR, ns: str = NS) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f"<PublicationDelivery {ns} version='1.1'><dataObjects><CompositeFrame id='c'><frames>"
        f"{BEFORE}{calendar}{AFTER}"
        "</frames></CompositeFrame></dataObjects></PublicationDelivery>"
    ).encode()


def _zip(tmp_path: Path, body: bytes, name: str = "NETEX_SI.XML") -> Path:
    src = tmp_path / "si-nap.zip"
    with zipfile.ZipFile(src, "w") as z:
        z.writestr(name, body)
    return src


def _convert(tmp_path: Path, body: bytes) -> tuple[nc.ConvertStats | None, bytes]:
    src = _zip(tmp_path, body)
    dest = tmp_path / "out" / "si-nap.zip"
    dest.parent.mkdir()
    stats = nc.convert_zip(src, dest)
    if stats is None:
        assert not dest.exists()
        return None, b""
    with zipfile.ZipFile(dest) as z:
        assert z.namelist() == ["NETEX_SI.XML"]
        return stats, z.read("NETEX_SI.XML")


def _periods(xml: str) -> dict[str, tuple[date, str]]:
    """DayTypeRef -> (FromDate, ValidDayBits) as MOTIS will read them."""
    periods = {
        m["id"]: (date.fromisoformat(m["from"]), m["bits"])
        for m in re.finditer(
            r'<UicOperatingPeriod version="1" id="(?P<id>[^"]+)">'
            r"<FromDate>(?P<from>[\d-]{10})T00:00:00</FromDate><ToDate>[^<]+</ToDate>"
            r"<ValidDayBits>(?P<bits>[01]+)</ValidDayBits>",
            xml,
        )
    }
    return {
        m["dt"]: periods[m["op"]]
        for m in re.finditer(
            r'<OperatingPeriodRef ref="(?P<op>[^"]+)"/><DayTypeRef ref="(?P<dt>[^"]+)"/>', xml
        )
    }


def _days(start: date, bits: str) -> set[date]:
    return {start + timedelta(days=i) for i, b in enumerate(bits) if b == "1"}


def test_slovenian_calendar_becomes_valid_day_bits(tmp_path: Path) -> None:
    stats, out = _convert(tmp_path, _doc())
    assert stats is not None
    xml = out.decode()
    got = {dt: _days(*p) for dt, p in _periods(xml).items()}
    assert got["SI:DT:dated"] == {date(2026, 10, 5), date(2026, 10, 7)}
    # Oct 1-11 narrowed to weekdays, minus the excluded Oct 8.
    weekdays = {date(2026, 10, d) for d in (1, 2, 5, 6, 7, 9)}
    assert got["SI:DT:weekdays"] == weekdays
    # A day type nobody assigns still gets a (never-running) period: MOTIS looks
    # every journey's DayTypeRef up and drops the file on a miss.
    assert got["SI:DT:unused"] == set()
    assert stats.summary() == "4 day-type assignments rewritten as 3 UicOperatingPeriod"


def test_only_the_calendar_changes(tmp_path: Path) -> None:
    _, out = _convert(tmp_path, _doc())
    xml = out.decode()
    head, _, rest = xml.partition("<ServiceCalendarFrame")
    tail = rest.partition("</ServiceCalendarFrame>")[2]
    original = _doc().decode()
    assert head == original.partition("<ServiceCalendarFrame")[0]
    assert tail == original.partition("</ServiceCalendarFrame>")[2]
    # Old assignments are gone, the published periods and days are kept.
    assert "SI:DTA:1" not in xml
    assert 'id="SI:OP:oct"' in xml
    assert 'id="SI:OD:3"' in xml
    assert xml.count("<DayTypeAssignment ") == 3


def test_a_file_already_in_the_swiss_model_is_left_alone(tmp_path: Path) -> None:
    swiss = """<ServiceCalendarFrame id="CH:SCF">
      <operatingPeriods><UicOperatingPeriod id="CH:UOP:1"><FromDate>2026-10-01T00:00:00</FromDate>
        <ToDate>2026-10-03T00:00:00</ToDate><ValidDayBits>101</ValidDayBits></UicOperatingPeriod>
      </operatingPeriods>
      <dayTypeAssignments><DayTypeAssignment id="CH:DTA:1" order="1">
        <OperatingPeriodRef ref="CH:UOP:1"/><DayTypeRef ref="CH:DT:1"/></DayTypeAssignment>
      </dayTypeAssignments></ServiceCalendarFrame>"""
    stats, _ = _convert(tmp_path, _doc(swiss))
    assert stats is None


def test_a_file_without_calendar_frame_is_left_alone(tmp_path: Path) -> None:
    stats, _ = _convert(tmp_path, _doc(calendar=""))
    assert stats is None


def test_prefixed_namespace_keeps_its_prefix(tmp_path: Path) -> None:
    prefixed = re.sub(r"<(/?)(\w)", r"<\1netex:\2", CALENDAR)
    stats, out = _convert(
        tmp_path, _doc(prefixed, ns='xmlns:netex="http://www.netex.org.uk/netex"')
    )
    assert stats is not None
    xml = out.decode()
    assert "<netex:UicOperatingPeriod " in xml
    assert "<netex:dayTypeAssignments><netex:DayTypeAssignment " in xml


def test_frame_split_across_read_chunks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(nc, "_CHUNK", 37)  # forces the start tag and the frame across reads
    stats, out = _convert(tmp_path, _doc())
    assert stats is not None
    got = {dt: _days(*p) for dt, p in _periods(out.decode()).items()}
    assert got["SI:DT:dated"] == {date(2026, 10, 5), date(2026, 10, 7)}


def test_operating_period_by_day_refs_and_unresolved_refs(tmp_path: Path) -> None:
    cal = """<ServiceCalendarFrame id="X">
      <operatingDays>
        <OperatingDay id="D1"><CalendarDate>2026-12-30</CalendarDate></OperatingDay>
        <OperatingDay id="D2"><CalendarDate>2027-01-02</CalendarDate></OperatingDay>
      </operatingDays>
      <operatingPeriods><OperatingPeriod id="P1">
        <FromOperatingDayRef ref="D1"/><ToOperatingDayRef ref="D2"/></OperatingPeriod></operatingPeriods>
      <dayTypeAssignments>
        <DayTypeAssignment id="A1" order="1"><OperatingPeriodRef ref="P1"/><DayTypeRef ref="T1"/></DayTypeAssignment>
        <DayTypeAssignment id="A2" order="2"><OperatingDayRef ref="missing"/><DayTypeRef ref="T1"/></DayTypeAssignment>
        <DayTypeAssignment id="A3" order="3"><Date>2027-01-10</Date><DayTypeRef ref="T2"/></DayTypeAssignment>
      </dayTypeAssignments></ServiceCalendarFrame>"""
    stats, out = _convert(tmp_path, _doc(cal))
    assert stats is not None
    got = {dt: _days(*p) for dt, p in _periods(out.decode()).items()}
    assert got["T1"] == {date(2026, 12, 30) + timedelta(days=i) for i in range(4)}
    assert got["T2"] == {date(2027, 1, 10)}
    assert stats.summary().endswith("(1 unresolved, ignored)")
    # A frame without <operatingPeriods> of its own still gets one.
    no_periods = cal.replace(
        cal[cal.index("<operatingPeriods>") : cal.index("<dayTypeAssignments>")], ""
    )
    (tmp_path / "b").mkdir()
    stats2, out2 = _convert(tmp_path / "b", _doc(no_periods))
    assert stats2 is not None
    assert "<operatingPeriods><UicOperatingPeriod " in out2.decode()


def test_malformed_frame_is_left_as_published(tmp_path: Path) -> None:
    broken = CALENDAR.replace("</dayTypes>", "")  # unbalanced
    stats, _ = _convert(tmp_path, _doc(broken))
    assert stats is None


def test_non_xml_members_are_copied(tmp_path: Path) -> None:
    src = tmp_path / "in.zip"
    with zipfile.ZipFile(src, "w") as z:
        z.writestr("readme.txt", "hello")
        z.writestr("data.xml", _doc())
    dest = tmp_path / "out.zip"
    assert nc.convert_zip(src, dest) is not None
    with zipfile.ZipFile(dest) as z:
        assert z.read("readme.txt") == b"hello"


def test_rewrite_stream_passes_a_truncated_document_through() -> None:
    body = _doc().split(b"</ServiceCalendarFrame>")[0]
    out = io.BytesIO()
    assert nc._rewrite_stream(io.BytesIO(body), out, nc.ConvertStats()) is False
    assert out.getvalue() == body


# ── the MOTIS build reads the converted copy ─────────────────────────


def _worker_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    from app import worker

    graphs = tmp_path / "graphs"
    monkeypatch.setattr(worker.settings, "graph_dir", graphs)
    netex = tmp_path / "inbox" / "eu19" / "netex"
    netex.mkdir(parents=True)
    return graphs / "motis" / "eu19", netex


def test_build_uses_a_cached_converted_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import worker

    motis_root, netex = _worker_dirs(tmp_path, monkeypatch)
    src = netex / "si-nap.zip"
    with zipfile.ZipFile(src, "w") as z:
        z.writestr("NETEX_SI.XML", _doc())
    notes: list[str] = []
    path = worker._motis_timetable_path("eu19", src, motis_root, notes)
    assert path == "/graphs/motis/eu19/_netex/si-nap.zip"
    assert notes == [
        "netex calendar: si-nap.zip converted for MOTIS "
        "(4 day-type assignments rewritten as 3 UicOperatingPeriod)"
    ]
    calls: list[Path] = []
    monkeypatch.setattr(worker.netex_calendar, "convert_zip", lambda s, d: calls.append(s))
    assert worker._motis_timetable_path("eu19", src, motis_root, []) == path
    assert calls == []  # unchanged source: no second conversion


def test_build_reads_the_original_when_nothing_to_convert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import worker

    motis_root, netex = _worker_dirs(tmp_path, monkeypatch)
    src = netex / "sbb.zip"
    with zipfile.ZipFile(src, "w") as z:
        z.writestr("ch.xml", _doc(calendar=""))
    notes: list[str] = []
    assert (
        worker._motis_timetable_path("eu19", src, motis_root, notes) == "/inbox/eu19/netex/sbb.zip"
    )
    assert notes == []
    gtfs = tmp_path / "inbox" / "eu19" / "gtfs" / "zou.zip"
    assert (
        worker._motis_timetable_path("eu19", gtfs, motis_root, notes) == "/inbox/eu19/gtfs/zou.zip"
    )


def test_build_falls_back_to_the_original_on_a_conversion_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import worker

    monkeypatch.setattr(worker.log, "disabled", False)
    motis_root, netex = _worker_dirs(tmp_path, monkeypatch)
    src = netex / "si-nap.zip"
    src.write_bytes(b"not a zip")
    notes: list[str] = []
    assert (
        worker._motis_timetable_path("eu19", src, motis_root, notes)
        == "/inbox/eu19/netex/si-nap.zip"
    )
    assert notes == ["netex calendar: si-nap.zip conversion failed, original used"]


def test_pruning_old_builds_keeps_the_conversion_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import worker

    motis_root, _ = _worker_dirs(tmp_path, monkeypatch)
    for name in ("20261001-000000", "20261002-000000", "20261003-000000", "_netex"):
        (motis_root / name).mkdir(parents=True)
    worker._prune_old_motis_imports("eu19", keep=1)
    assert sorted(p.name for p in motis_root.iterdir()) == ["20261003-000000", "_netex"]


def test_date_only_assignments_are_left_as_published(tmp_path: Path) -> None:
    """SBB's calendar file assigns its public-holiday day types by `Date` only;
    its journeys take their days from AvailabilityConditions. Left alone."""
    sbb = """<ServiceCalendarFrame id="ch:1:SCF">
      <dayTypes><DayType id="ch:1:DayType:Dreikoenigstag"/></dayTypes>
      <dayTypeAssignments><DayTypeAssignment id="ch:1:DayTypeAssignment:1" order="1">
        <Date>2026-01-06</Date><DayTypeRef ref="ch:1:DayType:Dreikoenigstag"/></DayTypeAssignment>
      </dayTypeAssignments></ServiceCalendarFrame>"""
    stats, _ = _convert(tmp_path, _doc(sbb))
    assert stats is None


def test_generated_ids_are_named_after_the_day_type(tmp_path: Path) -> None:
    _, out = _convert(tmp_path, _doc())
    xml = out.decode()
    assert 'id="VIATOR:UicOperatingPeriod:SI:DT:weekdays"' in xml
    assert 'id="VIATOR:DayTypeAssignment:SI:DT:weekdays"' in xml


def test_a_reference_to_a_period_defined_elsewhere_is_not_a_reason_to_rewrite(
    tmp_path: Path,
) -> None:
    """DB, CFL, CIS-CZ and NMBS assign by OperatingPeriodRef to UicOperatingPeriods;
    when the period sits in another frame or file the frame alone cannot see
    it, and rewriting would empty those day types."""
    split = """<ServiceCalendarFrame id="DE:SCF:2">
      <dayTypeAssignments><DayTypeAssignment id="DE:DTA:1" order="1">
        <OperatingPeriodRef ref="DE:UOP:in-another-file"/><DayTypeRef ref="DE:DT:1"/>
        <isAvailable>true</isAvailable></DayTypeAssignment>
      </dayTypeAssignments></ServiceCalendarFrame>"""
    stats, _ = _convert(tmp_path, _doc(split))
    assert stats is None
