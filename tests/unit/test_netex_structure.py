"""NeTEx structure check at download (app/netex_structure.py).

Slovenia's national file was silently dropped by MOTIS for a calendar model
it does not read, and only a manual search revealed it. Every NeTEx download
is now fingerprinted (header + element vocabulary) and assessed: rules for the
forms MOTIS does not read, and the difference with the previous download.
Warnings never block the file.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest

from app import feed_fetch, netex_structure
from app.api.admin import sessions as api
from app.netex_structure import RED, WARN, Fingerprint, assess, fingerprint

SBB_HEAD = (
    b'<?xml version="1.0" encoding="utf-8"?>\n'
    b'<PublicationDelivery xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
    b'xsi:schemaLocation="http://www.netex.org.uk/netex '
    b'http://netex.uk/netex/schema/1.10/xsd/NeTEx_publication.xsd" version="1.10" '
    b'xmlns="http://www.netex.org.uk/netex">\n'
    b"  <!--Profile: SBBSplitted-->\n"
    b"  <PublicationTimestamp>2026-09-29T08:10:21</PublicationTimestamp>\n"
    b"  <ParticipantRef>MENTZ</ParticipantRef>\n"
)
SI_HEAD = (
    b'<PublicationDelivery xmlns="http://www.netex.org.uk/netex" version="2.0:EU_PI-1.0">'
    b"<ParticipantRef>NAP</ParticipantRef>"
)
SBB_BODY = (
    b"<dataObjects><CompositeFrame><frames><TimetableFrame><vehicleJourneys>"
    b'<ServiceJourney id="j1"><validityConditions><AvailabilityConditionRef ref="n1"/>'
    b"</validityConditions></ServiceJourney></vehicleJourneys></TimetableFrame>"
    b"<ServiceCalendarFrame><validityConditions>"
    b'<AvailabilityCondition id="n1"><ValidDayBits>101</ValidDayBits></AvailabilityCondition>'
    b"</validityConditions></ServiceCalendarFrame></frames></CompositeFrame></dataObjects>"
    b"</PublicationDelivery>"
)
SI_BODY = (
    b"<ServiceCalendarFrame><operatingDays><OperatingDay id='d'/></operatingDays>"
    b"<operatingPeriods><OperatingPeriod id='p'/></operatingPeriods>"
    b"<dayTypeAssignments><DayTypeAssignment><OperatingDayRef ref='d'/><DayTypeRef ref='t'/>"
    b"</DayTypeAssignment></dayTypeAssignments></ServiceCalendarFrame>"
    b"<TimetableFrame><vehicleJourneys><ServiceJourney id='j'><dayTypes><DayTypeRef ref='t'/>"
    b"</dayTypes></ServiceJourney></vehicleJourneys></TimetableFrame></PublicationDelivery>"
)


def _zip(tmp_path: Path, members: dict[str, bytes], name: str = "feed.zip") -> Path:
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as z:
        for member, body in members.items():
            z.writestr(member, body)
    return path


# ── fingerprint ─────────────────────────────────────────────────────


def test_header_of_a_split_sbb_style_file(tmp_path: Path) -> None:
    fp = fingerprint(_zip(tmp_path, {"A_RESOURCE.xml": SBB_HEAD + SBB_BODY, "B.xml": SI_HEAD}))
    assert fp.header == {
        "version": "1.10",
        "xsd": "1.10",
        "profile": "SBBSplitted",
        "participant": "MENTZ",
    }
    assert fp.xml_files == 2


def test_header_of_an_si_style_file(tmp_path: Path) -> None:
    fp = fingerprint(_zip(tmp_path, {"SI.XML": SI_HEAD + SI_BODY}))
    assert fp.header == {"version": "2.0:EU_PI-1.0", "xsd": "", "profile": "", "participant": "NAP"}


def test_vocabulary_counts_start_tags_only_and_drops_prefixes(tmp_path: Path) -> None:
    body = (
        b'<?xml version="1.0"?><!-- a comment --><netex:PublicationDelivery xmlns:netex="x">'
        b"<netex:ServiceJourney/><ServiceJourney></ServiceJourney>"
        b"<UicOperatingPeriod/><OperatingPeriod/><OperatingPeriodRef ref='p'/>"
        b"</netex:PublicationDelivery>"
    )
    el = fingerprint(_zip(tmp_path, {"a.xml": body})).elements
    assert el["ServiceJourney"] == 2
    # Exact names: none of these swallows another.
    assert el["UicOperatingPeriod"] == 1
    assert el["OperatingPeriod"] == 1
    assert el["OperatingPeriodRef"] == 1
    assert "xml" not in el
    assert "--" not in el


def test_tags_split_across_reads_are_counted_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(netex_structure, "_CHUNK", 7)
    el = fingerprint(_zip(tmp_path, {"SI.XML": SI_HEAD + SI_BODY})).elements
    assert el["ServiceJourney"] == 1
    assert el["OperatingDayRef"] == 1
    assert el["DayTypeRef"] == 2


def test_a_bare_xml_file_is_fingerprinted_too(tmp_path: Path) -> None:
    path = tmp_path / "feed.xml"
    path.write_bytes(SI_HEAD + SI_BODY)
    fp = fingerprint(path)
    assert fp.xml_files == 1
    assert fp.elements["OperatingDay"] == 1


# ── assessment ──────────────────────────────────────────────────────


def _fp(elements: dict[str, int], **header: str) -> Fingerprint:
    base = {"version": "1.10", "xsd": "", "profile": "", "participant": "X"}
    return Fingerprint(header={**base, **header}, xml_files=1, elements=elements)


def test_the_sbb_model_is_ok(tmp_path: Path) -> None:
    fp = fingerprint(_zip(tmp_path, {"a.xml": SBB_HEAD + SBB_BODY}))
    result = assess(fp, None)
    assert result.level == "ok"
    assert result.messages == []


def test_the_slovenian_calendar_is_flagged(tmp_path: Path) -> None:
    result = assess(fingerprint(_zip(tmp_path, {"SI.XML": SI_HEAD + SI_BODY})), None)
    assert result.level == WARN
    assert any(
        m.startswith("OperatingDayRef (x1): calendar by dated OperatingDay")
        for m in result.messages
    )
    assert any(m.startswith("OperatingPeriod (x1): plain OperatingPeriod") for m in result.messages)


def test_a_file_without_courses_is_red() -> None:
    result = assess(_fp({"StopPlace": 3}), None)
    assert result.level == RED
    assert result.messages == ["no ServiceJourney in the file: no course can be loaded"]


def test_courses_without_any_calendar_are_red() -> None:
    result = assess(_fp({"ServiceJourney": 5, "DayTypeRef": 5}), None)
    assert result.level == RED
    assert "courses but no calendar" in result.messages[0]


def test_new_and_vanished_elements_since_the_last_download_are_warned() -> None:
    before = _fp({"ServiceJourney": 5, "UicOperatingPeriod": 5, "calls": 5}).to_state()
    now = _fp({"ServiceJourney": 5, "UicOperatingPeriod": 5, "passingTimes": 5})
    result = assess(now, before)
    assert result.level == WARN
    assert "new elements since last download: passingTimes" in result.messages
    assert "elements gone since last download: calls" in result.messages


def test_a_header_change_is_warned() -> None:
    before = _fp({"ServiceJourney": 1, "UicOperatingPeriod": 1}).to_state()
    now = _fp({"ServiceJourney": 1, "UicOperatingPeriod": 1}, version="2.0:EU_PI-1.0")
    result = assess(now, before)
    assert result.messages == ["header version changed: 1.10 → 2.0:EU_PI-1.0"]


def test_an_older_fingerprint_format_is_not_compared() -> None:
    before = {**_fp({"calls": 1}).to_state(), "version": 0}
    result = assess(_fp({"ServiceJourney": 1, "UicOperatingPeriod": 1}), before)
    assert result.level == "ok"


def test_red_is_not_lowered_by_a_later_warning() -> None:
    result = netex_structure.Assessment()
    result.add(RED, "a")
    result.add(WARN, "b")
    assert result.level == RED


# ── download and Feed status ────────────────────────────────────────


def _netex_zip(body: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("SI.XML", body)
    return buf.getvalue()


async def _fetch(tmp_path: Path, body: bytes, previous: dict[str, Any]) -> feed_fetch.FetchResult:
    staging = tmp_path / "staging"
    staging.mkdir(exist_ok=True)
    payload = _netex_zip(body)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=payload))
    ) as c:
        return await feed_fetch.fetch_validated(
            c,
            fetch_url="https://b2b.nap.example/data",
            state_url="https://b2b.nap.example/data",
            extra_headers={},
            kind="NeTEx-EPIP",
            staging=staging,
            base_name="si",
            suffix=".zip",
            previous=previous,
            have_current=False,
        )


async def test_a_netex_download_stores_its_fingerprint_and_assessment(tmp_path: Path) -> None:
    result = await _fetch(tmp_path, SI_HEAD + SI_BODY, {})
    assert result.status == "fetched"
    structure = result.state["structure"]
    assert structure["fingerprint"]["header"]["participant"] == "NAP"
    assert structure["assessment"]["level"] == WARN


async def test_the_next_download_is_compared_with_the_previous_fingerprint(tmp_path: Path) -> None:
    first = await _fetch(tmp_path, SI_HEAD + SI_BODY, {})
    changed = SI_BODY.replace(b"<dayTypes>", b"<dayTypes><Extra/>")
    second = await _fetch(tmp_path, SI_HEAD + changed, first.state)
    assert (
        "new elements since last download: Extra"
        in second.state["structure"]["assessment"]["messages"]
    )


async def test_a_failing_check_never_fails_the_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_path: Path) -> Fingerprint:
        raise ValueError("odd member")

    monkeypatch.setattr(netex_structure, "fingerprint", boom)
    result = await _fetch(tmp_path, SI_HEAD + SI_BODY, {})
    assert result.status == "fetched"
    assessment = result.state["structure"]["assessment"]
    assert assessment == {"level": WARN, "messages": ["structure check could not run: odd member"]}


def test_feed_status_carries_the_assessment() -> None:
    status = api.ProviderStatus(feed_id="SI-NAP", state="ok")
    state = {"structure": {"assessment": {"level": WARN, "messages": ["x"]}}}
    api._decorate_status(
        status, {"id": "SI-NAP", "timetable": {"format": "netex_epip"}}, state, None
    )
    assert status.structure == {"level": WARN, "messages": ["x"]}
    plain = api.ProviderStatus(feed_id="SNCF", state="ok")
    api._decorate_status(plain, {"id": "SNCF"}, {}, None)
    assert plain.structure is None


def test_the_page_shows_structure_findings() -> None:
    html = (Path(__file__).resolve().parents[2] / "app/templates/admin/sessions.html").read_text(
        encoding="utf-8"
    )
    assert "function feedStructureHTML(st)" in html
    assert "return base + feedStructureHTML(s.structure);" in html


# ── rules from the nigiri gap analysis ──────────────────────────────


def _messages(elements: dict[str, int], prefixes: dict[str, int] | None = None) -> list[str]:
    base = {"ServiceJourney": 1, "UicOperatingPeriod": 1, "DayTypeAssignment": 1}
    fp = _fp({**base, **elements})
    fp.prefixes = prefixes or {}
    return assess(fp, None).messages


def test_journey_types_motis_does_not_read_are_flagged() -> None:
    msgs = _messages({"DatedServiceJourney": 3, "TemplateServiceJourney": 1})
    assert any(
        m.startswith("DatedServiceJourney (x3): DatedServiceJourney: not read") for m in msgs
    )
    assert any(m.startswith("TemplateServiceJourney (x1)") for m in msgs)


def test_interchanges_warn_about_stay_seated_and_the_import_abort() -> None:
    msgs = _messages({"ServiceJourneyInterchange": 2})
    assert any("missing StaySeated as true" in m and "aborts the whole import" in m for m in msgs)


def test_references_without_their_target_in_the_file_are_flagged() -> None:
    msgs = _messages({"TrainNumberRef": 4, "OperatorRef": 2, "Line": 1})
    assert any(m.startswith("TrainNumberRef without any TrainNumber") for m in msgs)
    assert any(m.startswith("OperatorRef but no Operator") for m in msgs)
    assert any(m.startswith("lines without additionalOperators") for m in msgs)
    ok = _messages({"TrainNumberRef": 4, "TrainNumber": 4, "OperatorRef": 2, "Operator": 1})
    assert not any("TrainNumber" in m or "Operator" in m for m in ok)


def test_product_categories_outside_a_value_set_are_flagged() -> None:
    assert any("outside a ValueSet" in m for m in _messages({"TypeOfProductCategory": 2}))
    assert not any(
        "outside a ValueSet" in m for m in _messages({"TypeOfProductCategory": 2, "ValueSet": 1})
    )


def test_prefixed_elements_are_flagged_except_gml(tmp_path: Path) -> None:
    body = (
        b'<netex:PublicationDelivery xmlns:netex="x" xmlns:gml="g"><netex:ServiceJourney/>'
        b"<gml:pos>1 2</gml:pos></netex:PublicationDelivery>"
    )
    fp = fingerprint(_zip(tmp_path, {"a.xml": body}))
    assert fp.prefixes == {"gml": 1, "netex": 2}
    msgs = assess(fp, None).messages
    assert "2 elements written with prefix 'netex:': MOTIS reads unprefixed names" in msgs
    assert not any("'gml:'" in m for m in msgs)
