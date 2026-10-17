"""What the stop extractor saw of a feed that gives no extract (the census),
the NeTEx mode values that name no mode, and the owner's diagnostic script
`scripts/netex_mode_census.py`.

Production, 16 October: the national NeTEx of a railway ended `no_rail_stop`
("no rail station in this feed") with nothing more to go on. The census
says what the reader saw (Lines and their modes, journeys kept, stop places,
stations and why their rows were not written), in counts and enumerated
words only. Every archive here is invented: ids with `ZZ`, numbers made of
9s, made-up names.
"""

from __future__ import annotations

import importlib.util
import io
import json
import logging
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from app import station_extract, station_extract_netex
from app import station_extract_upload as up
from app.settings import settings
from app.station_extract import ExtractError, ensure_extract
from app.station_extract_common import (
    CENSUS_VALUES_MAX,
    NOT_A_WORD,
    OTHER_VALUES,
    SUMMARY,
    SUMMARY_MAX,
    ValueTally,
    pack,
)
from tests.unit.test_station_extract import (
    WHEN,
    _build,
    _by_key,
    _delivery,
    _gtfs,
    _msmm_check,
    _point,
    _provider,
    _psa,
    _zip,
    feed_key_for,
)

SHA = "a" * 64
TEMPLATE = Path(__file__).resolve().parents[2] / "app" / "templates" / "admin" / "sessions.html"
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "netex_mode_census.py"


# ── Invented NeTEx ────────────────────────────────────────────────────────


def _place(n: int, mode: str = "", kind: str = "", position: bool = True) -> str:
    where = (
        "<Centroid><Location><Longitude>9.5</Longitude><Latitude>45.5</Latitude>"
        "</Location></Centroid>"
        if position
        else ""
    )
    own = f"<TransportMode>{mode}</TransportMode>" if mode else ""
    kind_text = f"<StopPlaceType>{kind}</StopPlaceType>" if kind else ""
    return (
        f'<StopPlace id="ZZ:StopPlace:99{n}"><Name>Zzstadt {n}</Name>'
        f"{where}{own}{kind_text}</StopPlace>"
    )


def _site(*places: str) -> str:
    return f'<SiteFrame id="ZZ:SF"><stopPlaces>{"".join(places)}</stopPlaces></SiteFrame>'


def _lines(*modes: str) -> str:
    lines = "".join(
        f'<Line id="ZZ:Line:9{n}"><Name>Zz {n}</Name>'
        + (f"<TransportMode>{mode}</TransportMode>" if mode else "")
        + "</Line>"
        for n, mode in enumerate(modes, start=1)
    )
    return f'<ServiceFrame id="ZZ:SVF:L"><lines>{lines}</lines></ServiceFrame>'


PATTERNS = (
    '<ServiceFrame id="ZZ:SVF:P"><stopAssignments>'
    + _psa(1, "ZZ:SSP:1", "ZZ:StopPlace:991")
    + _psa(2, "ZZ:SSP:2", "ZZ:StopPlace:992")
    + '</stopAssignments><journeyPatterns><ServiceJourneyPattern id="ZZ:SJP:1">'
    "<pointsInSequence>"
    + _point(1, "ZZ:SSP:1")
    + _point(2, "ZZ:SSP:2")
    + "</pointsInSequence></ServiceJourneyPattern></journeyPatterns></ServiceFrame>"
)


def _journeys(count: int, mode: str = "", line: str = "") -> str:
    own = f"<TransportMode>{mode}</TransportMode>" if mode else ""
    ref = f'<LineRef ref="{line}"/>' if line else ""
    journeys = "".join(
        f'<ServiceJourney id="ZZ:SJ:9{n}">{own}<ServiceJourneyPatternRef ref="ZZ:SJP:1"/>'
        f"{ref}</ServiceJourney>"
        for n in range(count)
    )
    return (
        f'<TimetableFrame id="ZZ:TF"><vehicleJourneys>{journeys}</vehicleJourneys></TimetableFrame>'
    )


def _refused(tmp_path: Path, files: dict[str, str], fmt: str = "netex_epip") -> ExtractError:
    tmp_path.mkdir(parents=True, exist_ok=True)
    archive = _zip(tmp_path / "zz.zip", files)
    provider = _provider(fmt)
    with pytest.raises(ExtractError) as caught:
        _build(provider, archive, tmp_path)
    return caught.value


def _rows(tmp_path: Path, files: dict[str, str]) -> dict[str, dict[str, str]]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    archive = _zip(tmp_path / "zz.zip", files)
    provider = _provider("netex_epip")
    result = _build(provider, archive, tmp_path)
    return _by_key(_msmm_check(result.path_gz, str(feed_key_for(provider))))


def _fields(census: str) -> dict[str, str]:
    return dict(item.split("=", 1) for item in census.split(" "))


# ── The tally, the summary ────────────────────────────────────────────────


def test_a_tally_keeps_one_word_values_only() -> None:
    tally = ValueTally()
    for raw in ("rail", "", "ZZ:Line:99", "https://zz.example.invalid/x", "Zzstadt Nord", "a" * 25):
        tally.add(raw)
    tally.add("rail", 2)
    assert tally.counts == {"rail": 3, "none": 1, NOT_A_WORD: 4}
    assert tally.text(2) == "unreadable 4, rail 3"
    numbers = ValueTally(digits=True)
    for raw in ("3", "700", "3", "x", "3.5"):
        numbers.add(raw)
    assert numbers.text(3) == "3: 2, unreadable: 2, 700: 1"


def test_a_tally_keeps_a_bounded_number_of_values() -> None:
    tally = ValueTally()
    words = [f"zz{chr(97 + n)}" for n in range(CENSUS_VALUES_MAX + 4)]
    for word in words:
        tally.add(word)
    assert len(tally.counts) == CENSUS_VALUES_MAX + 1
    assert tally.counts[OTHER_VALUES] == 4
    assert tally.total == len(words)


def test_the_summary_keeps_what_fits() -> None:
    long = "z" * 60
    assert pack([["a"], [long + long, long], ["b" * 40], ["c"]]) == f"a, {long}, c"
    assert len(pack([["z" * 98], ["y"]])) == 98
    assert pack([["z" * SUMMARY_MAX]]) == "z" * SUMMARY_MAX
    assert pack([["z" * (SUMMARY_MAX + 1)]]) == ""


# ── NeTEx ─────────────────────────────────────────────────────────────────


def test_netex_without_any_mode(tmp_path: Path) -> None:
    """Journeys without a mode and without a Line, at stop places that call
    themselves nothing: no rail station, and the census says why."""
    error = _refused(
        tmp_path, {"zz.xml": _delivery(_site(_place(1), _place(2)) + PATTERNS + _journeys(2))}
    )
    assert error.reason == "no_rail_stop"
    assert error.summary == (
        "0 lines, 2 journeys (2 kept), 0 stations, 2 without a line, 2 stop places (0 rail)"
    )
    fields = _fields(error.census)
    assert fields["journey_modes"] == "none:2"
    assert fields["kept"] == "none:2"
    assert (fields["no_line_ref"], fields["line_undefined"]) == ("2", "0")
    assert (fields["patterns"], fields["patterns_used"], fields["points"]) == ("1", "1", "2")
    assert (fields["assignments"], fields["places_called"]) == ("2", "2")
    assert fields["stop_place_modes"] == fields["stop_place_types"] == "none:2"
    assert fields["rail_stop_places"] == "0"


def test_netex_lines_in_a_later_member(tmp_path: Path) -> None:
    """The Lines come after the journeys, in another member: their bus mode
    drops the journeys, and the stop places are never read."""
    first = _delivery(_site(_place(1), _place(2)) + PATTERNS + _journeys(2, line="ZZ:Line:91"))
    error = _refused(tmp_path, {"a.xml": first, "b.xml": _delivery(_lines("bus"))})
    assert error.summary == "1 lines (bus 1), 2 journeys (0 kept), 0 stations"
    fields = _fields(error.census)
    assert (fields["members"], fields["line_modes"], fields["kept"]) == ("2", "bus:1", "0")
    assert fields["stop_places"] == fields["stop_place_modes"] == fields["points"] == "unread"


def test_netex_rows_not_written_are_counted(tmp_path: Path) -> None:
    """Rail stations whose rows cannot be written (no position): the census
    says so, and what does not fit in 100 characters is left out."""
    first = _delivery(
        _site(_place(1, position=False), _place(2, position=False))
        + PATTERNS
        + _journeys(2, line="ZZ:Line:91")
    )
    error = _refused(tmp_path, {"a.xml": first, "b.xml": _delivery(_lines("rail"))})
    assert error.summary == (
        "1 lines (rail 1), 2 journeys (2 kept), 2 stations (not written: no_position 2)"
    )
    fields = _fields(error.census)
    assert (fields["stations"], fields["dropped"], fields["kept"]) == (
        "2",
        "no_position:2",
        "rail:2",
    )
    assert fields["stop_places"] == "2"


def test_netex_rail_stop_places_are_counted(tmp_path: Path) -> None:
    """Journeys without a mode at stop places that are railway stations, none
    with a position: the stop places that call themselves rail are counted."""
    places = _site(
        _place(1, kind="railStation", position=False), _place(2, mode="rail", position=False)
    )
    error = _refused(tmp_path, {"zz.xml": _delivery(places + PATTERNS + _journeys(2))})
    fields = _fields(error.census)
    assert (fields["stop_places"], fields["rail_stop_places"]) == ("2", "2")
    assert fields["stop_place_modes"] == "none:1,rail:1"
    assert fields["stop_place_types"] == "none:1,railStation:1"
    assert (fields["stations"], fields["dropped"]) == ("2", "no_position:2")


def test_netex_without_stop_place(tmp_path: Path) -> None:
    error = _refused(tmp_path, {"zz.xml": _delivery(PATTERNS + _journeys(1, mode="rail"))})
    assert error.reason == "no_stop_place"
    assert error.summary == "0 lines, 1 journeys (1 kept), 1 without a line, 0 stop places (0 rail)"
    # the assignments name stop places the archive does not define
    assert _fields(error.census)["places_called"] == "2"


def test_netex_census_holds_no_id_or_name(tmp_path: Path) -> None:
    """Values that are not one word (an id, a URL, a name with a space) are
    `unreadable`; no id, name or URL of the feed reaches the census."""
    modes = ("ZZ:Line:99", "https://zz.example.invalid/x", "Zzstadt Nord", "rail")
    document = _delivery(
        _site(_place(1, mode="ZZ:Mode:99"), _place(2, kind="Zzstadt Nord"))
        + _lines(*modes)
        + PATTERNS
        + _journeys(2, mode="https://zz.example.invalid/y", line="ZZ:Line:94")
    )
    error = _refused(tmp_path, {"zz.xml": document})
    for text in (error.summary, error.census):
        for leak in ("ZZ", "99", "zz", "http", "Nord", "Zzstadt", "/", "."):
            assert leak not in text
    fields = _fields(error.census)
    assert fields["line_modes"] == "unreadable:3,rail:1"
    assert fields["journey_modes"] == "unreadable:2"
    assert SUMMARY.fullmatch(error.summary)


def test_netex_census_is_short_with_many_modes(tmp_path: Path) -> None:
    modes = [f"zz{chr(97 + n)}{'q' * 21}" for n in range(12)]
    document = _delivery(_site(_place(1), _place(2)) + _lines(*modes) + PATTERNS + _journeys(1))
    error = _refused(tmp_path, {"zz.xml": document})
    assert len(error.summary) <= SUMMARY_MAX
    assert error.summary.startswith("12 lines (")
    line_modes = _fields(error.census)["line_modes"]
    assert line_modes.startswith(f"{OTHER_VALUES}:4,")
    assert line_modes.count(":") == CENSUS_VALUES_MAX + 1


# ── GTFS ──────────────────────────────────────────────────────────────────


def test_gtfs_bus_only(tmp_path: Path) -> None:
    routes = "route_id,agency_id,route_type\nR1,ZZR,3\nR2,ZZR,3\nR3,ZZR,700\n"
    archive = _gtfs(tmp_path, name="b.zip", routes=routes)
    provider = _provider()
    with pytest.raises(ExtractError) as caught:
        _build(provider, archive, tmp_path)
    error = caught.value
    assert error.reason == "no_rail_stop"
    assert error.summary == "3 routes (route_type 3: 2, 700: 1), 0 rail routes, 0 stations"
    fields = _fields(error.census)
    assert fields["route_types"] == "3:2,700:1"
    assert fields["trips"] == fields["rail_trips"] == fields["called_stops"] == "unread"


def test_gtfs_rail_stations_not_written(tmp_path: Path) -> None:
    stops = (
        "stop_id,stop_name,stop_lat,stop_lon\n"
        "ZZ:S1,ZZ Nord,,\nZZ:S2,ZZ Sud,,\nZZ:S3,ZZ Ville,,\nZZ:B1,ZZ Arret,,\n"
    )
    archive = _gtfs(tmp_path, name="p.zip", stops=stops)
    provider = _provider()
    with pytest.raises(ExtractError) as caught:
        _build(provider, archive, tmp_path)
    error = caught.value
    assert error.summary == (
        "4 routes (route_type 2: 1, 3: 1, 400: 1), 2 rail routes, "
        "2 stations (not written: no_position 2)"
    )
    fields = _fields(error.census)
    assert (fields["rail_routes"], fields["trips"], fields["rail_trips"]) == ("2", "5", "3")
    assert (fields["rail_calls"], fields["called_stops"]) == ("6", "4")
    assert fields["dropped"] == "no_position:2"


def test_no_census_for_other_reasons(tmp_path: Path) -> None:
    archive = _zip(tmp_path / "bad.zip", {"a.xml": "<PublicationDelivery><oops>"})
    provider = _provider("netex_epip")
    with pytest.raises(ExtractError) as caught:
        _build(provider, archive, tmp_path)
    assert (caught.value.reason, caught.value.summary, caught.value.census) == (
        "bad_archive",
        "",
        "",
    )


# ── Mode values that name no mode, and a deprecated name of rail ──────────


def _one_station(tmp_path: Path, journey: str, line: str, place: str, kind: str = "") -> set[str]:
    """The stations of a feed with one pattern over two stop places, whose
    journeys and Line carry the modes given ('' for none)."""
    document = _delivery(
        _site(_place(1, mode=place, kind=kind), _place(2, mode=place, kind=kind))
        + (_lines(line) if line else "")
        + PATTERNS
        + _journeys(1, mode=journey, line="ZZ:Line:91" if line else "")
    )
    try:
        return set(_rows(tmp_path, {"zz.xml": document}))
    except ExtractError as exc:
        assert exc.reason == "no_rail_stop"
        return set()


BOTH = {"ZZ:StopPlace:991", "ZZ:StopPlace:992"}


@pytest.mark.parametrize("mode", ["unknown", "all", "anyMode", "urbanRail"])
def test_a_journey_mode_that_names_no_mode_lets_the_station_decide(
    tmp_path: Path, mode: str
) -> None:
    assert _one_station(tmp_path, mode, "", "", "railStation") == BOTH
    assert _one_station(tmp_path / "b", mode, "", "", "busStation") == set()
    assert _one_station(tmp_path / "c", "", mode, "", "railStation") == BOTH  # the Line's
    assert _one_station(tmp_path / "d", mode, "rail", "", "") == BOTH  # the Line says rail


def test_other_is_a_mode_that_is_not_rail(tmp_path: Path) -> None:
    """`other` says a mode outside NeTEx's list: not rail, at a railway
    station too; a journey that says `unknown` takes its Line's bus."""
    assert _one_station(tmp_path, "", "other", "", "railStation") == set()
    assert _one_station(tmp_path / "b", "other", "", "", "railStation") == set()
    assert _one_station(tmp_path / "c", "unknown", "bus", "", "railStation") == set()
    assert station_extract_netex._mode("other") == "other"
    assert station_extract_netex._mode("unknown") == ""


def test_intercity_rail_is_rail(tmp_path: Path) -> None:
    assert _one_station(tmp_path, "intercityRail", "", "", "") == BOTH
    assert _one_station(tmp_path / "b", "", "intercityRail", "", "") == BOTH
    # a stop place that calls itself so counts a journey without a mode
    assert _one_station(tmp_path / "c", "", "", "intercityRail", "") == BOTH


def test_other_modes_stay_what_they_were(tmp_path: Path) -> None:
    assert _one_station(tmp_path, "bus", "", "", "railStation") == set()
    assert _one_station(tmp_path / "b", "metro", "", "rail", "railStation") == set()
    assert _one_station(tmp_path / "c", "", "", "", "") == set()
    assert _one_station(tmp_path / "d", "", "", "bus", "railStation") == set()
    assert station_extract.EXTRACTOR_VERSION == "viator-stop-extract-2"


def test_the_census_follows_the_line_a_pattern_gives(tmp_path: Path) -> None:
    """A journey that names no Line, whose pattern names a bus Line: counted
    as given its Line by the pattern, and not kept."""
    patterns = PATTERNS.replace(
        '<ServiceJourneyPattern id="ZZ:SJP:1">',
        '<ServiceJourneyPattern id="ZZ:SJP:1"><RouteView><LineRef ref="ZZ:Line:91"/></RouteView>',
    )
    document = _delivery(_site(_place(1), _place(2)) + _lines("bus") + patterns + _journeys(2))
    error = _refused(tmp_path, {"zz.xml": document})
    fields = _fields(error.census)
    assert (fields["no_line_ref"], fields["line_from_pattern"], fields["kept"]) == ("2", "2", "0")
    assert (
        error.summary == "1 lines (bus 1), 2 journeys (0 kept), 0 stations, 2 stop places (0 rail)"
    )
    rail = _delivery(_site(_place(1), _place(2)) + _lines("rail") + patterns + _journeys(2))
    assert set(_rows(tmp_path / "r", {"zz.xml": rail})) == BOTH


def test_a_pattern_defined_twice_is_counted_once(tmp_path: Path) -> None:
    """A pattern id defined twice (a versioned export), naming a bus Line,
    followed by journeys that name none: the census moves them once, never
    below zero, and the summary stays of its form."""
    patterns = _bus_pattern()
    document = _delivery(
        _site(_place(1), _place(2)) + _lines("bus") + patterns + patterns + _journeys(1)
    )
    error = _refused(tmp_path, {"zz.xml": document})
    fields = _fields(error.census)
    assert (fields["patterns"], fields["patterns_used"]) == ("2", "1")
    assert (fields["line_from_pattern"], fields["kept"]) == ("1", "0")
    assert (
        error.summary == "1 lines (bus 1), 1 journeys (0 kept), 0 stations, 2 stop places (0 rail)"
    )
    assert SUMMARY.fullmatch(error.summary)


def test_a_pattern_naming_an_undefined_line_gives_none(tmp_path: Path) -> None:
    document = _delivery(_site(_place(1), _place(2)) + _bus_pattern() + _journeys(2))
    error = _refused(tmp_path, {"zz.xml": document})
    fields = _fields(error.census)
    assert (fields["no_line_ref"], fields["line_from_pattern"], fields["kept"]) == (
        "2",
        "0",
        "none:2",
    )
    assert "2 without a line" in error.summary


def test_a_tally_never_shows_a_negative_count() -> None:
    tally = ValueTally()
    tally.add("rail", 2)
    tally.add("none", -1)
    assert tally.total == 2
    assert tally.top() == [("rail", 2)]


def _bus_pattern() -> str:
    """The pattern, naming the Line ZZ:Line:91 through its RouteView."""
    return PATTERNS.replace(
        '<ServiceJourneyPattern id="ZZ:SJP:1">',
        '<ServiceJourneyPattern id="ZZ:SJP:1"><RouteView><LineRef ref="ZZ:Line:91"/></RouteView>',
    )


# ── Remembered, logged, sent to the admin status ──────────────────────────


class _Child:
    """`_run_child` in this process, failing as told."""

    def __init__(self, fail: ExtractError) -> None:
        self.fail = fail
        self.runs = 0

    def __call__(self, _target: Any, _args: tuple[Any, ...]) -> Any:
        self.runs += 1
        raise self.fail


def _ensure(archive: Path, folder: Path) -> ExtractError:
    provider = _provider()
    with pytest.raises(ExtractError) as caught:
        ensure_extract(provider, archive, SHA, extracted_at=WHEN, session_id="zz", cache_dir=folder)
    return caught.value


@pytest.fixture
def folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(station_extract.settings, "inbox_dir", tmp_path / "inbox")
    (tmp_path / "inbox").mkdir(exist_ok=True)
    return tmp_path / "x"


def test_the_summary_is_remembered_and_the_census_logged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    folder: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    summary = "1 lines (bus 1), 2 journeys (0 kept)"
    child = _Child(ExtractError("no_rail_stop", "zz", summary=summary, census="lines=1 kept=0"))
    monkeypatch.setattr(station_extract, "_run_child", child)
    archive = _gtfs(tmp_path)
    with caplog.at_level(logging.WARNING, logger="app.station_extract"):
        first = _ensure(archive, folder)
    assert (first.summary, first.census) == (summary, "lines=1 kept=0")
    assert (
        "station extract census: provider=ZZRAIL format=gtfs reason=no_rail_stop lines=1 kept=0"
        in caplog.text
    )
    second = _ensure(archive, folder)
    assert (second.reason, second.detail, second.summary, second.census) == (
        "no_rail_stop",
        "remembered",
        summary,
        "",
    )
    assert child.runs == 1
    (marker,) = folder.glob("*.failed")
    record = json.loads(marker.read_text())
    assert record["summary"] == summary
    record["summary"] = "<b>ZZ</b>"
    marker.write_text(json.dumps(record))
    assert _ensure(archive, folder).summary == ""
    del record["summary"]
    marker.write_text(json.dumps(record))
    assert _ensure(archive, folder).summary == ""
    assert child.runs == 1


def test_a_summary_of_another_form_is_not_remembered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, folder: Path
) -> None:
    child = _Child(ExtractError("no_rail_stop", summary="zz/99"))
    monkeypatch.setattr(station_extract, "_run_child", child)
    archive = _gtfs(tmp_path)
    _ensure(archive, folder)
    (marker,) = folder.glob("*.failed")
    assert "summary" not in json.loads(marker.read_text())


def test_the_child_sends_what_the_reader_saw(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[Any] = []

    class Conn:
        def send(self, message: Any) -> None:
            sent.append(message)

        def close(self) -> None:
            """Nothing to close."""

    def target() -> Any:
        raise ExtractError("no_rail_stop", "zz", summary="0 lines", census="lines=0")

    monkeypatch.setattr(station_extract, "_limit_self", lambda _limits: None)
    station_extract._child_main(Conn(), target, (), station_extract._Limits(1, 1, 0))
    (message,) = sent
    error = station_extract._child_error(message)
    assert (error.reason, error.detail, error.summary, error.census) == (
        "no_rail_stop",
        "zz",
        "0 lines",
        "lines=0",
    )
    older = station_extract._child_error(("error", "memory_limit", "out of memory"))
    assert (older.reason, older.detail, older.summary, older.census) == (
        "memory_limit",
        "out of memory",
        "",
        "",
    )


def _feed(tmp_path: Path) -> up.Feed:
    archive = tmp_path / "zz.zip"
    archive.write_bytes(b"PK stand-in")
    return up.Feed(
        "ZZ0", {"id": "ZZ0", "timetable": {"format": "gtfs"}}, "url:9", archive, "9" * 64
    )


@pytest.mark.parametrize(
    ("summary", "expected"),
    [("2 lines (bus 2), 9 journeys (0 kept)", "2 lines (bus 2), 9 journeys (0 kept)"), ("", None)],
)
def test_the_admin_status_shows_the_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    summary: str,
    expected: str | None,
) -> None:
    monkeypatch.setattr(settings, "inbox_dir", tmp_path / "inbox")
    monkeypatch.setattr(up.station_module, "enabled", lambda: True)

    def extract(*_args: Any) -> Any:
        return up._refusal(ExtractError("no_rail_stop", summary=summary))

    def calls(*_args: Any) -> Any:
        return up._Answer(404, b"", None)

    monkeypatch.setattr(up._Calls, "head", calls)
    with caplog.at_level(logging.WARNING, logger="app.station_extract_upload"):
        result = up.send_session_extracts(
            "zz", [_feed(tmp_path)], budget=60.0, extract=extract, ready=lambda *_a: True
        )
    (outcome,) = result.outcomes
    assert (outcome.state, outcome.reason, outcome.summary) == (
        up.SKIPPED,
        "no_rail_stop",
        expected,
    )
    loaded = up.load_outcome("zz", "ZZ0")
    assert loaded is not None
    assert (loaded["reason"], loaded["summary"]) == ("no_rail_stop", expected)
    assert loaded["detail"] == "no rail station in this feed"
    saw = f" saw: {summary}" if summary else ""
    assert f"station_extract.skipped provider=ZZ0 reason=no_rail_stop{saw}\n" in caplog.text + "\n"


def test_a_summary_of_another_form_never_reaches_the_admin_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert up._refusal(ExtractError("no_rail_stop", summary="<b>zz</b>")) == "no_rail_stop"
    assert up._refusal(ExtractError("no_rail_stop", summary="z" * 101)) == "no_rail_stop"
    assert up._refusal(ExtractError("bad word", summary="0 lines")) == up.NO_EXTRACT
    assert up._refusal(ExtractError("no_rail_stop", summary="0 lines")) == up.NoExtract(
        "no_rail_stop", "0 lines"
    )
    monkeypatch.setattr(settings, "inbox_dir", tmp_path)
    up.record_outcome("zz", up.FeedOutcome("ZZ0", up.SKIPPED, "no_rail_stop", "0 lines"))
    path = tmp_path / "zz" / "_fetch_state" / "provider_ZZ0_.station_extract.json"
    record = json.loads(path.read_text())
    assert record["summary"] == "0 lines"
    record["summary"] = "<script>"
    path.write_text(json.dumps(record))
    loaded = up.load_outcome("zz", "ZZ0")
    assert loaded is not None
    assert loaded["summary"] is None
    up.record_outcome("zz", up.FeedOutcome("ZZ0", up.SENT))
    assert "summary" not in json.loads(path.read_text())


# ── The owner's script ────────────────────────────────────────────────────


def _script() -> Any:
    spec = importlib.util.spec_from_file_location("netex_mode_census", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["netex_mode_census"] = module
    spec.loader.exec_module(module)
    return module


def _run_script(archive: Path) -> tuple[int, list[str]]:
    out = io.StringIO()
    code = _script().main([str(archive)], out=out)
    return code, out.getvalue().splitlines()


def test_the_script_counts_modes_and_references(tmp_path: Path) -> None:
    patterns = PATTERNS.replace(
        '<ServiceJourneyPattern id="ZZ:SJP:1">',
        '<ServiceJourneyPattern id="ZZ:SJP:1"><RouteView><LineRef ref="ZZ:Line:91"/></RouteView>',
    )
    rail = (
        '<Line id="ZZ:Line:93"><TransportMode>rail</TransportMode><TransportSubmode>'
        "<RailSubmode>longDistance</RailSubmode></TransportSubmode></Line>"
    )
    lines = _lines("bus", "ZZ:Line:99").replace("</lines>", rail + "</lines>")
    archive = _zip(
        tmp_path / "zz.zip",
        {
            "zz_Zzstadt_lines.xml": _delivery(lines + patterns),
            "zz_Zzstadt_rest.xml": _delivery(
                _site(_place(1, mode="rail"), _place(2, kind="railStation"))
                + _journeys(2, mode="unknown")
                + _journeys(1, line="ZZ:Line:91")
            ),
            "readme.txt": "Zzstadt",
        },
    )
    code, lines_out = _run_script(archive)
    assert code == 0
    text = "\n".join(lines_out)
    for leak in ("ZZ", "zz", "99", "Zzstadt", "http", ".xml"):
        assert leak not in text
    assert lines_out[0] == "netex mode census: 2 XML members, 1 other members"
    assert lines_out[2].startswith("member 1: ")
    assert lines_out[2].endswith(
        "root PublicationDelivery, 33 elements; Line 3, ServiceJourneyPattern 1, PassengerStopAssignment 2"
    )
    assert "Line 3" in lines_out
    assert "ServiceJourney 3" in lines_out
    assert "Line/TransportMode: bus 1, rail 1, unreadable 1" in lines_out
    assert "Line/TransportSubmode/RailSubmode: longDistance 1" in lines_out
    assert "ServiceJourney/TransportMode: unknown 2" in lines_out
    assert "StopPlace/TransportMode: rail 1" in lines_out
    assert "StopPlace/StopPlaceType: railStation 1" in lines_out
    assert "ServiceJourneyPattern/RouteView/LineRef 1" in lines_out
    assert "ServiceJourney/LineRef 1" in lines_out
    assert "ServiceJourney (3): LineRef 1, ServiceJourneyPatternRef 3, TransportMode 2" in lines_out
    assert "frames: CompositeFrame 2, ServiceFrame 2, SiteFrame 1, TimetableFrame 2" in lines_out


def test_the_script_refuses_entities_and_bad_archives(tmp_path: Path) -> None:
    bomb = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "zz">]><x>&a;</x>'
    code, out = _run_script(_zip(tmp_path / "e.zip", {"a.xml": bomb}))
    assert (code, out) == (1, ["netex mode census: cannot read the archive (Refused)"])
    not_zip = tmp_path / "n.zip"
    not_zip.write_bytes(b"not a zip")
    code, out = _run_script(not_zip)
    assert (code, out) == (1, ["netex mode census: cannot read the archive (BadZipFile)"])
    code, out = _run_script(_zip(tmp_path / "b.zip", {"a.xml": "<x><y></x>"}))
    assert (code, out) == (1, ["netex mode census: cannot read the archive (ExpatError)"])


def test_the_script_bounds_what_it_keeps(tmp_path: Path) -> None:
    module = _script()
    assert module.word("rail") == "rail"
    assert module.word("  ") == "empty"
    assert module.word("ZZ:Mode:99") == "unreadable"
    assert module.word("a" * 33) == "unreadable"
    census = module.Census()
    for n in range(module.VALUES_MAX + 5):
        census.value("Line/TransportMode", f"zz{n}")
    assert len(census.values["Line/TransportMode"]) == module.VALUES_MAX + 1
    assert census.values["Line/TransportMode"][module.OTHER] == 5
    deep = "<a><b><c><d><e><TransportMode>rail</TransportMode></e></d></c></b></a>"
    archive = _zip(
        tmp_path / "d.zip",
        {"a.xml": _delivery(_lines("rail").replace("</Line>", deep + "</Line>"))},
    )
    code, out = _run_script(archive)
    assert code == 0
    assert "Line/.../c/d/e/TransportMode: rail 1" in out


def test_the_script_never_names_a_member_on_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An encrypted member (RuntimeError) or one packed in a way zipfile
    cannot read (NotImplementedError): the type only, never the message,
    which names the member."""
    module = _script()
    for error in (RuntimeError, NotImplementedError, KeyError):

        def fail(_path: str, error: type[Exception] = error) -> Any:
            raise error("File zz_Zzstadt.xml is encrypted")

        monkeypatch.setattr(module, "read_archive", fail)
        out = io.StringIO()
        assert module.main([str(tmp_path / "zz.zip")], out=out) == 1
        assert out.getvalue() == f"netex mode census: cannot read the archive ({error.__name__})\n"


def test_the_script_names_no_other_namespace(tmp_path: Path) -> None:
    foreign = (
        '<zz:extension xmlns:zz="https://zz.example.invalid/ns">'
        "<TransportMode>rail</TransportMode></zz:extension>"
    )
    document = _delivery(_lines("bus").replace("</Line>", foreign + "</Line>"))
    code, out = _run_script(_zip(tmp_path / "f.zip", {"a.xml": document}))
    assert code == 0
    assert "Line/(other ns)/TransportMode: rail 1" in out
    assert "Line/TransportMode: bus 1" in out
    assert "extension" not in "\n".join(out)


def test_the_script_prints_an_odd_element_name_as_such(tmp_path: Path) -> None:
    """Review of #367: an element name is printed only when it is of NeTEx's
    form (letters, digits, `_`, at most 64); a crafted one could carry text."""
    module = _script()
    long_name = "Zz" + "z" * 70
    odd = (
        f"<Zz\u00e9Ref/><zz-99Ref/><{long_name}Ref/><Zz.Frame/>"
        "<Zz\u00e9Submode>rail</Zz\u00e9Submode>"
    )
    document = _delivery(_lines("rail").replace("</Line>", odd + "</Line>"))
    code, out = _run_script(_zip(tmp_path / "o.zip", {"a.xml": document}))
    assert code == 0
    text = "\n".join(out)
    for leak in ("Zz", "zz", "99", "\u00e9"):
        assert leak not in text
    assert "Line/(odd name) 3" not in out  # an odd name is not a reference
    assert "Line (1): TransportMode 1" in out
    assert module._local("Zz\u00e9Frame") == module.ODD_NAME
    assert module._local("{http://www.netex.org.uk/netex}" + "A" * 64) == "A" * 64
    assert module._local("{http://www.netex.org.uk/netex}" + "A" * 65) == module.ODD_NAME
    assert module._local("9Line") == module.ODD_NAME
    assert module._local("_Line9") == "_Line9"


def test_the_script_caps_the_distinct_frame_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _script()
    monkeypatch.setattr(module, "PATHS_MAX", 3)
    frames = "".join(f'<Zz{n}Frame id="ZZ:F:{n}"/>' for n in "ABCDE")
    archive = _zip(tmp_path / "f.zip", {"a.xml": _delivery(frames)})
    out = io.StringIO()
    assert module.main([str(archive)], out=out) == 0
    lines = out.getvalue().splitlines()
    (frames_line,) = [line for line in lines if line.startswith("frames: ")]
    # CompositeFrame and two of the five: three distinct names, the rest together.
    assert frames_line == "frames: (other)Frame 3, CompositeFrame 1, ZzAFrame 1, ZzBFrame 1"
    census = module.Census()
    for n in range(10):
        census.element(f"Zz{n}Frame")
    census.element("Line")
    assert census.frames == 3
    assert census.elements["Line"] == 1
    assert census.elements[module.OTHER_FRAME] == 7


def test_the_script_writes_utf8_whatever_the_console(tmp_path: Path) -> None:
    module = _script()
    raw = io.BytesIO()
    ascii_console = io.TextIOWrapper(raw, encoding="ascii", errors="strict")
    stream = module._utf8(ascii_console)
    stream.write("\u00e9\n")
    stream.flush()
    assert raw.getvalue() == "\u00e9\n".encode()
    plain = io.StringIO()
    assert module._utf8(plain) is plain  # no reconfigure: as it is
    archive = _zip(tmp_path / "a.zip", {"a.xml": _delivery(_lines("rail"))})
    run = subprocess.run(
        [sys.executable, str(SCRIPT), str(archive)],
        capture_output=True,
        env={"PYTHONIOENCODING": "ascii:strict", "PATH": "/usr/bin:/bin"},
        check=False,
    )
    assert run.returncode == 0
    assert run.stderr == b""
    assert run.stdout.startswith(b"netex mode census: 1 XML members")


def test_the_script_refuses_an_external_entity_with_expats_type(tmp_path: Path) -> None:
    module = _script()
    parser = module._parser(module._Reader(module.Census(), module.Member(1, 0, 0)))
    assert parser.ExternalEntityRefHandler is module._refuse_external
    with pytest.raises(module.Refused):
        module._refuse_external("zz", None, "zz.dtd", None)


_RENDER = """
const escHTML = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
  .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const escAttr = escHTML;
const _relativeTimeShort = () => '';
%(block)s
console.log(JSON.stringify(%(cases)s.map(feedStationExtractHTML)));
"""


def test_the_station_module_column_shows_what_the_extractor_saw() -> None:
    """The Sessions page's JS, run in Node: `saw: <summary>` under the
    reason's sentence, escaped; nothing without a summary."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    text = TEMPLATE.read_text(encoding="utf-8")
    block = text[text.index("const _STATION_EXTRACT_FLAVOURS") : text.index("function feedRowHTML")]
    base = {"text": "skipped", "state": "skipped", "reason": "no_rail_stop"}
    cases = [
        {**base, "detail": "no rail station in this feed", "summary": "1 lines (bus 1)"},
        {**base, "detail": None, "summary": "<b>zz</b>"},
        {**base, "detail": "no rail station in this feed", "summary": None},
    ]
    script = _RENDER % {"block": block, "cases": json.dumps(cases)}
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True)
    saw, escaped, without = json.loads(out.stdout)
    assert saw.endswith(
        '<div class="feed-extract-detail">no rail station in this feed</div>'
        '<div class="feed-extract-detail" title="What the extractor saw in this feed">'
        "saw: 1 lines (bus 1)</div>"
    )
    assert "saw: &lt;b&gt;zz&lt;/b&gt;</div>" in escaped
    assert "<b>" not in escaped
    assert "saw:" not in without
    assert without.endswith("no rail station in this feed</div>")
