"""Rail by default: a provider's "This feed is rail when the file names no mode".

The owner's census of a national railway's NeTEx export (17 October): one
operator, Lines, journeys, patterns, assignments to StopPlaces with Quays,
and no mode anywhere, no `TransportMode`, no `StopPlaceType`, no submode:
the extract ends `no_rail_stop`. With `timetable.default_mode: "rail"` a
journey without a mode counts at a StopPlace that gives neither mode nor
type; an explicit mode always wins. Such an extract has its own extractor
version (cache, remembered failure, the module's version, the HEAD), the
setting is validated on save, kept across a NAP source switch, and shown on
the Sessions page. Every archive is invented (ids with ZZ, numbers of 9s).
"""

from __future__ import annotations

import dataclasses
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest

from app import ingestion, nap_source_map, station_extract, station_extract_gtfs
from app import station_extract_upload as up
from app.station_extract import ExtractError, build_extract
from app.station_extract_common import RAIL_MAIN, RAIL_SUBURBAN, SUMMARY
from tests.unit.test_station_extract import (
    ROUTES,
    WHEN,
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
from tests.unit.test_station_extract_census import PATTERNS, _fields, _lines, _place, _site
from tests.unit.test_station_extract_upload import FakeClock, Module, Setup, _run

SHA = "c" * 64
BOTH = {"ZZ:StopPlace:991", "ZZ:StopPlace:992"}
TEMPLATE = Path(__file__).resolve().parents[2] / "app" / "templates" / "admin" / "sessions.html"
RAIL_DEFAULT = {"default_mode": "rail"}


def _journeys(count: int, mode: str = "", line: str = "", submode: str = "") -> str:
    own = f"<TransportMode>{mode}</TransportMode>" if mode else ""
    sub = (
        f"<TransportSubmode><RailSubmode>{submode}</RailSubmode></TransportSubmode>"
        if submode
        else ""
    )
    ref = f'<LineRef ref="{line}"/>' if line else ""
    journeys = "".join(
        f'<ServiceJourney id="ZZ:SJ:9{n}">{own}{sub}<ServiceJourneyPatternRef ref="ZZ:SJP:1"/>'
        f"{ref}</ServiceJourney>"
        for n in range(count)
    )
    return (
        f'<TimetableFrame id="ZZ:TF"><vehicleJourneys>{journeys}</vehicleJourneys></TimetableFrame>'
    )


def _document(
    place_mode: str = "", place_kind: str = "", journey: str = "", line: str = "", sub: str = ""
) -> str:
    return _delivery(
        _site(
            _place(1, mode=place_mode, kind=place_kind), _place(2, mode=place_mode, kind=place_kind)
        )
        + (_lines(line) if line else "")
        + PATTERNS
        + _journeys(2, mode=journey, line="ZZ:Line:91" if line else "", submode=sub)
    )


def _extract(
    tmp_path: Path, document: str, default: bool
) -> dict[str, dict[str, str]] | ExtractError:
    """The rows of the extract (by stop key), or the refusal."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    archive = _zip(tmp_path / "zz.zip", {"zz.xml": document})
    provider = _provider("netex_epip", **(RAIL_DEFAULT if default else {}))
    try:
        result = build_extract(provider, archive, SHA, extracted_at=WHEN, cache_dir=tmp_path / "x")
    except ExtractError as exc:
        return exc
    return _by_key(_msmm_check(result.path_gz, str(feed_key_for(provider))))


def _stations(tmp_path: Path, document: str, default: bool = True) -> set[str]:
    found = _extract(tmp_path, document, default)
    if isinstance(found, ExtractError):
        assert found.reason == "no_rail_stop"
        return set()
    return set(found)


# ── The NeTEx reader ──────────────────────────────────────────────────────


def test_a_feed_that_names_no_mode_gives_its_stations_rail_by_default(tmp_path: Path) -> None:
    off = _extract(tmp_path / "off", _document(), default=False)
    assert isinstance(off, ExtractError)
    assert off.reason == "no_rail_stop"

    rows = _extract(tmp_path / "on", _document(), default=True)
    assert not isinstance(rows, ExtractError)
    assert set(rows) == BOTH
    for row in rows.values():
        assert row["modes"] == RAIL_MAIN
        assert row["n_calls"] == "2"
        assert row["extractor_version"] == "viator-stop-extract-2+rail-default"


@pytest.mark.parametrize(
    ("journey", "line", "place_mode", "place_kind"),
    [
        pytest.param("bus", "", "", "", id="journey-bus"),
        pytest.param("coach", "", "", "", id="journey-coach"),
        pytest.param("other", "", "", "", id="journey-other"),
        pytest.param("", "tram", "", "", id="line-tram"),
        pytest.param("unknown", "bus", "", "", id="unknown-journey-bus-line"),
        pytest.param("", "", "bus", "", id="stop-place-bus"),
        pytest.param("", "", "metro", "", id="stop-place-metro"),
        pytest.param("", "", "", "busStation", id="stop-place-bus-station"),
        pytest.param("", "", "", "onstreetTram", id="stop-place-tram-type"),
    ],
)
def test_an_explicit_mode_that_is_not_rail_stays_not_rail(
    tmp_path: Path, journey: str, line: str, place_mode: str, place_kind: str
) -> None:
    document = _document(place_mode, place_kind, journey, line)
    assert _stations(tmp_path, document) == set()


@pytest.mark.parametrize(
    ("journey", "line", "place_mode", "place_kind"),
    [
        pytest.param("rail", "", "bus", "", id="journey-rail"),
        pytest.param("", "rail", "", "busStation", id="line-rail"),
        pytest.param("", "", "rail", "", id="stop-place-rail"),
        pytest.param("", "", "", "railStation", id="rail-station"),
        pytest.param("unknown", "", "unknown", "", id="modes-that-name-no-mode"),
    ],
)
def test_an_explicit_rail_mode_stays_rail(
    tmp_path: Path, journey: str, line: str, place_mode: str, place_kind: str
) -> None:
    document = _document(place_mode, place_kind, journey, line)
    assert _stations(tmp_path, document) == BOTH


def _submode(kind: str, word: str) -> str:
    return f"<TransportSubmode><{kind}>{word}</{kind}></TransportSubmode>"


@pytest.mark.parametrize(
    ("kind", "word"),
    [
        pytest.param("BusSubmode", "localBus", id="bus"),
        pytest.param("TramSubmode", "cityTram", id="tram"),
        pytest.param("MetroSubmode", "urbanRailway", id="metro-urban-railway"),
    ],
)
def test_a_submode_of_another_mode_is_not_rail_by_default(
    tmp_path: Path, kind: str, word: str
) -> None:
    """Review round 1 of #369: a journey or Line without a mode but with a
    bus or tram submode is not rail; a StopPlace with one is not modeless."""
    sub = _submode(kind, word)
    on_journey = _document().replace("<ServiceJourneyPatternRef", sub + "<ServiceJourneyPatternRef")
    assert _stations(tmp_path / "j", on_journey) == set()
    line = f'<ServiceFrame id="ZZ:SVF:L"><lines><Line id="ZZ:Line:91">{sub}</Line></lines></ServiceFrame>'
    on_line = _delivery(
        _site(_place(1), _place(2)) + line + PATTERNS + _journeys(2, line="ZZ:Line:91")
    )
    assert _stations(tmp_path / "l", on_line) == set()
    on_place = _document().replace("<Name>Zzstadt", f"<{kind}>{word}</{kind}><Name>Zzstadt")
    assert _stations(tmp_path / "p", on_place) == set()
    wrapped = _document().replace("<Name>Zzstadt", sub + "<Name>Zzstadt")
    assert _stations(tmp_path / "w", wrapped) == set()


def test_a_rail_submode_stays_rail_by_default(tmp_path: Path) -> None:
    on_place = _document().replace("<Name>Zzstadt", "<RailSubmode>local</RailSubmode><Name>Zzstadt")
    assert _stations(tmp_path / "p", on_place) == BOTH
    assert _stations(tmp_path / "j", _document(sub="local")) == BOTH


def test_a_bus_submode_still_counts_at_a_railway_station(tmp_path: Path) -> None:
    """Without the setting nothing changes: the railStation fallback counts
    a journey without a mode whatever its submode, as before."""
    document = _document(place_kind="railStation").replace(
        "<ServiceJourneyPatternRef",
        _submode("BusSubmode", "localBus") + "<ServiceJourneyPatternRef",
    )
    assert _stations(tmp_path / "off", document, default=False) == BOTH
    assert _stations(tmp_path / "on", document, default=True) == BOTH


def test_the_journeys_own_submode_counts_under_a_line_without_mode(tmp_path: Path) -> None:
    document = _delivery(
        _site(_place(1), _place(2))
        + '<ServiceFrame id="ZZ:SVF:L"><lines><Line id="ZZ:Line:91"><Name>Zz</Name></Line></lines>'
        "</ServiceFrame>"
        + PATTERNS
        + _journeys(2, line="ZZ:Line:91").replace(
            "<ServiceJourneyPatternRef",
            _submode("BusSubmode", "localBus") + "<ServiceJourneyPatternRef",
        )
    )
    assert _stations(tmp_path, document) == set()


def test_a_suburban_submode_is_suburban_rail_by_default(tmp_path: Path) -> None:
    rows = _extract(tmp_path, _document(sub="suburbanRailway"), default=True)
    assert not isinstance(rows, ExtractError)
    assert {row["modes"] for row in rows.values()} == {RAIL_SUBURBAN}


def test_without_the_setting_the_rows_of_other_feeds_do_not_change(tmp_path: Path) -> None:
    """The railStation fallback reads a journey without a mode as main rail,
    its submode suburban or not, as before: the version stays 2."""
    rows = _extract(
        tmp_path, _document(place_kind="railStation", sub="suburbanRailway"), default=False
    )
    assert not isinstance(rows, ExtractError)
    assert {row["modes"] for row in rows.values()} == {RAIL_MAIN}
    assert {row["extractor_version"] for row in rows.values()} == {"viator-stop-extract-2"}
    assert station_extract.EXTRACTOR_VERSION == "viator-stop-extract-2"


def test_the_operators_of_journeys_without_a_mode_are_kept(tmp_path: Path) -> None:
    operator = '<OperatorRef ref="ZZ:Operator:99"/>'
    document = _document().replace(
        "<ServiceJourneyPatternRef", operator + "<ServiceJourneyPatternRef"
    )
    rows = _extract(tmp_path, document, default=True)
    assert not isinstance(rows, ExtractError)
    assert {row["operators"] for row in rows.values()} == {"ZZ:Operator:99"}


def test_main_rail_takes_the_operators_of_journeys_without_a_mode_as_before(
    tmp_path: Path,
) -> None:
    """Rows of every other feed unchanged: a station's main-rail calls take
    the operators of the journeys without a mode that call there too, even
    where those calls do not count (the reader's behaviour before rail by
    default, kept so that EXTRACTOR_VERSION stays 2)."""
    rail = (
        '<ServiceJourney id="ZZ:SJ:91"><TransportMode>rail</TransportMode>'
        '<OperatorRef ref="ZZ:Operator:91"/><ServiceJourneyPatternRef ref="ZZ:SJP:1"/>'
        "</ServiceJourney>"
    )
    modeless = (
        '<ServiceJourney id="ZZ:SJ:92"><OperatorRef ref="ZZ:Operator:92"/>'
        '<ServiceJourneyPatternRef ref="ZZ:SJP:1"/></ServiceJourney>'
    )
    document = _delivery(
        _site(_place(1, kind="busStation"), _place(2, kind="busStation"))
        + PATTERNS
        + f'<TimetableFrame id="ZZ:TF"><vehicleJourneys>{rail}{modeless}</vehicleJourneys>'
        "</TimetableFrame>"
    )
    for default in (False, True):
        rows = _extract(tmp_path / str(default), document, default=default)
        assert not isinstance(rows, ExtractError)
        assert {row["n_calls"] for row in rows.values()} == {"1"}
        assert {row["operators"] for row in rows.values()} == {"ZZ:Operator:91|ZZ:Operator:92"}


def test_the_census_says_how_many_calls_were_rail_by_default(tmp_path: Path) -> None:
    """A feed made rail by default that still has no station (the stop
    places give a type): the summary says the setting was applied, first."""
    error = _extract(tmp_path / "a", _document(place_kind="busStation"), default=True)
    assert isinstance(error, ExtractError)
    assert error.summary.startswith("rail by default: 0 calls at 0 stop places, ")
    assert SUMMARY.fullmatch(error.summary)
    fields = _fields(error.census)
    assert (fields["rail_by_default"], fields["rail_by_default_places"]) == ("0", "0")

    off = _extract(tmp_path / "b", _document(), default=False)
    assert isinstance(off, ExtractError)
    assert "rail by default" not in off.summary
    assert _fields(off.census)["rail_by_default"] == "unread"


def test_the_census_counts_the_calls_taken_as_rail(tmp_path: Path) -> None:
    """Stations found rail by default but not written (no position)."""
    document = _delivery(
        _site(_place(1, position=False), _place(2, position=False)) + PATTERNS + _journeys(2)
    )
    error = _extract(tmp_path, document, default=True)
    assert isinstance(error, ExtractError)
    assert error.summary.startswith("rail by default: 4 calls at 2 stop places, ")
    fields = _fields(error.census)
    assert (fields["rail_by_default"], fields["rail_by_default_places"]) == ("4", "2")


def test_the_stop_place_a_quay_belongs_to_decides(tmp_path: Path) -> None:
    """An assignment to a Quay places the call at the Quay's StopPlace: its
    mode and type decide, as for the railStation fallback."""
    quays = (
        '<StopPlace id="ZZ:StopPlace:991"><Name>Zzstadt 1</Name>'
        "<Centroid><Location><Longitude>9.5</Longitude><Latitude>45.5</Latitude></Location>"
        '</Centroid><quays><Quay id="ZZ:Quay:991"/></quays></StopPlace>'
        '<StopPlace id="ZZ:StopPlace:992"><Name>Zzstadt 2</Name>'
        "<Centroid><Location><Longitude>9.6</Longitude><Latitude>45.6</Latitude></Location>"
        "</Centroid><StopPlaceType>busStation</StopPlaceType>"
        '<quays><Quay id="ZZ:Quay:992"/></quays></StopPlace>'
    )
    patterns = (
        '<ServiceFrame id="ZZ:SVF:P"><stopAssignments>'
        + _psa(1, "ZZ:SSP:1", quay="ZZ:Quay:991")
        + _psa(2, "ZZ:SSP:2", quay="ZZ:Quay:992")
        + '</stopAssignments><journeyPatterns><ServiceJourneyPattern id="ZZ:SJP:1">'
        "<pointsInSequence>"
        + _point(1, "ZZ:SSP:1")
        + _point(2, "ZZ:SSP:2")
        + "</pointsInSequence></ServiceJourneyPattern></journeyPatterns></ServiceFrame>"
    )
    document = _delivery(_site(quays) + patterns + _journeys(1))
    assert _stations(tmp_path, document) == {"ZZ:StopPlace:991"}


# ── GTFS ──────────────────────────────────────────────────────────────────


def _gtfs_rows(
    tmp_path: Path, routes: str, default: bool
) -> dict[str, dict[str, str]] | ExtractError:
    tmp_path.mkdir(parents=True, exist_ok=True)
    archive = _gtfs(tmp_path, routes=routes)
    provider = _provider("gtfs", **(RAIL_DEFAULT if default else {}))
    try:
        result = build_extract(provider, archive, SHA, extracted_at=WHEN, cache_dir=tmp_path / "x")
    except ExtractError as exc:
        return exc
    return _by_key(_msmm_check(result.path_gz, str(feed_key_for(provider))))


def test_a_gtfs_route_without_a_route_type_is_rail_by_default(tmp_path: Path) -> None:
    routes = "route_id,agency_id,route_short_name,route_type\nR1,ZZR,IC,\nR2,ZZB,B,3\n"
    off = _gtfs_rows(tmp_path / "off", routes, default=False)
    assert isinstance(off, ExtractError)
    assert off.reason == "no_rail_stop"
    on = _gtfs_rows(tmp_path / "on", routes, default=True)
    assert not isinstance(on, ExtractError)
    assert set(on) == {"ZZ:S1", "ZZ:S2"}  # R1's calls; R2, a bus, is not rail
    assert {row["modes"] for row in on.values()} == {RAIL_MAIN}


def test_a_gtfs_without_the_route_type_column_is_rail_by_default(tmp_path: Path) -> None:
    routes = "route_id,agency_id,route_short_name\nR1,ZZR,IC\n"
    on = _gtfs_rows(tmp_path, routes, default=True)
    assert not isinstance(on, ExtractError)
    assert set(on) == {"ZZ:S1", "ZZ:S2"}


def test_a_gtfs_route_type_given_keeps_its_meaning(tmp_path: Path) -> None:
    plain = _gtfs_rows(tmp_path / "a", ROUTES, default=False)
    by_default = _gtfs_rows(tmp_path / "b", ROUTES, default=True)
    assert not isinstance(plain, ExtractError)
    assert not isinstance(by_default, ExtractError)
    assert {k: v["modes"] for k, v in plain.items()} == {
        k: v["modes"] for k, v in by_default.items()
    }


def test_the_gtfs_census_counts_the_routes_taken_as_rail(tmp_path: Path) -> None:
    census = station_extract_gtfs.GtfsCensus()
    census.rail_by_default = 0
    assert station_extract_gtfs._route_mode(" ", census) == RAIL_MAIN
    assert station_extract_gtfs._route_mode("3", census) is None
    assert census.rail_by_default == 1
    assert census.summary().startswith("rail by default: 1 routes")
    plain = station_extract_gtfs.GtfsCensus()
    assert station_extract_gtfs._route_mode("", plain) is None
    assert plain.rail_by_default is None


# ── Its own version: cache, remembered failure, the module ───────────────


def test_the_extractor_version_names_the_setting() -> None:
    assert station_extract.extractor_version_for(_provider("netex_epip")) == "viator-stop-extract-2"
    assert (
        station_extract.extractor_version_for(_provider("netex_epip", **RAIL_DEFAULT))
        == "viator-stop-extract-2+rail-default"
    )
    assert station_extract.extractor_version_for(_provider(), "zz-9") == "zz-9"
    assert not station_extract.default_rail({"timetable": "zz"})
    assert not station_extract.default_rail(_provider(default_mode="bus"))


def test_a_remembered_failure_is_not_reused_once_the_box_is_ticked(tmp_path: Path) -> None:
    """The plain extract of an archive ends `no_rail_stop`, remembered; the
    same archive rail by default is another file with no failure of its own,
    so it is read again."""
    archive = _zip(tmp_path / "zz.zip", {"zz.xml": _document()})
    plain = station_extract._plan(_provider("netex_epip"), archive, SHA, WHEN, tmp_path / "x")
    ticked = station_extract._plan(
        _provider("netex_epip", **RAIL_DEFAULT), archive, SHA, WHEN, tmp_path / "x"
    )
    assert plain.final != ticked.final
    station_extract._remember(
        plain.final, "ZZRAIL", "netex_epip", ExtractError("no_rail_stop", "stand-in")
    )
    assert station_extract._remembered(plain.final, "netex_epip") == "no_rail_stop"
    assert station_extract._remembered(ticked.final, "netex_epip") is None
    result = build_extract(
        _provider("netex_epip", **RAIL_DEFAULT),
        archive,
        SHA,
        extracted_at=WHEN,
        cache_dir=tmp_path / "x",
    )
    assert result.extractor_version == "viator-stop-extract-2+rail-default"
    assert result.path_gz == ticked.final
    # Rail by default keeps the archive's one file: the remembered failure
    # of the plain extract is pruned with the provider's older files.
    assert not station_extract._failed_path(plain.final).exists()


@pytest.mark.usefixtures("token")
@pytest.mark.parametrize(
    ("summary", "shown"),
    [
        pytest.param("rail by default: 9 calls at 2 stop places", True, id="some"),
        pytest.param("", False, id="none"),
        pytest.param("<b>zz</b>", False, id="other-form"),
    ],
)
def test_a_sent_extract_says_what_rail_by_default_made_rail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, summary: str, shown: bool
) -> None:
    """Review round 1 of #369: the counts of the extract itself, nothing
    when the setting made nothing rail; the module asked for the version
    rail by default."""
    setup = Setup(tmp_path, monkeypatch)
    setup.feeds[0].provider_cfg["timetable"]["default_mode"] = "rail"
    made = setup.extractor

    def with_summary(*args: Any, **kwargs: Any) -> station_extract.ExtractResult:
        return dataclasses.replace(made(*args, **kwargs), summary=summary)

    monkeypatch.setattr(station_extract, "ensure_extract", with_summary)
    module = Module()
    result = _run(setup, module, FakeClock())
    (head,) = module.head_requests
    assert dict(head.url.params) == {"extractor_version": "viator-stop-extract-2+rail-default"}
    (outcome,) = result.outcomes
    assert outcome.state == up.SENT
    assert outcome.summary == (summary if shown else None)


@pytest.mark.usefixtures("token")
def test_a_held_extract_shows_no_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """HEAD 200: nothing was made, so nothing is known of what it made rail."""
    setup = Setup(tmp_path, monkeypatch)
    setup.feeds[0].provider_cfg["timetable"]["default_mode"] = "rail"
    module = Module()
    module.heads = [httpx.Response(200)]
    (outcome,) = _run(setup, module, FakeClock()).outcomes
    assert (outcome.state, outcome.summary) == (up.HELD, None)


def test_the_result_carries_the_counts_and_the_cache_keeps_them(tmp_path: Path) -> None:
    archive = _zip(tmp_path / "zz.zip", {"zz.xml": _document()})
    provider = _provider("netex_epip", **RAIL_DEFAULT)
    made = build_extract(provider, archive, SHA, extracted_at=WHEN, cache_dir=tmp_path / "x")
    assert made.summary == "rail by default: 4 calls at 2 stop places"
    cached = build_extract(provider, archive, SHA, extracted_at=WHEN, cache_dir=tmp_path / "x")
    assert cached.dropped == {}  # from the cache
    assert cached.summary == made.summary
    station_extract._counts_path(made.path_gz).write_text('{"summary": "<b>zz</b>"}')
    again = build_extract(provider, archive, SHA, extracted_at=WHEN, cache_dir=tmp_path / "x")
    assert again.summary == ""
    station_extract._counts_path(made.path_gz).write_text("zz")
    assert (
        build_extract(provider, archive, SHA, extracted_at=WHEN, cache_dir=tmp_path / "x").summary
        == ""
    )


def test_nothing_made_rail_gives_no_summary(tmp_path: Path) -> None:
    """Ticked, but every station calls itself rail: nothing by default."""
    archive = _zip(tmp_path / "zz.zip", {"zz.xml": _document(place_kind="railStation")})
    provider = _provider("netex_epip", **RAIL_DEFAULT)
    made = build_extract(provider, archive, SHA, extracted_at=WHEN, cache_dir=tmp_path / "x")
    assert made.summary == ""
    assert not station_extract._counts_path(made.path_gz).exists()
    plain = build_extract(
        _provider("netex_epip"), archive, SHA, extracted_at=WHEN, cache_dir=tmp_path / "y"
    )
    assert plain.summary == ""


def test_the_kept_summary_is_pruned_with_its_extract(tmp_path: Path) -> None:
    folder = tmp_path / "x"
    provider = _provider("netex_epip", **RAIL_DEFAULT)
    first = _zip(tmp_path / "a.zip", {"zz.xml": _document()})
    old = build_extract(provider, first, SHA, extracted_at=WHEN, cache_dir=folder)
    assert station_extract._counts_path(old.path_gz).exists()
    new = build_extract(provider, first, "d" * 64, extracted_at=WHEN, cache_dir=folder)
    assert sorted(p.name for p in folder.iterdir()) == sorted(
        [new.path_gz.name, station_extract._counts_path(new.path_gz).name]
    )


def test_a_feed_that_ends_before_its_stations_says_the_setting_was_on(tmp_path: Path) -> None:
    """No journey kept at all (every journey a bus): the summary still says
    rail by default was on, with 0."""
    error = _extract(tmp_path, _document(journey="bus"), default=True)
    assert isinstance(error, ExtractError)
    assert error.summary.startswith("rail by default: 0 calls at 0 stop places, ")


@pytest.fixture
def token(monkeypatch: pytest.MonkeyPatch) -> None:
    import secrets

    from pydantic import SecretStr

    from app.settings import settings

    monkeypatch.setattr(settings, "station_module_url", "http://msmm.invalid:8000")
    monkeypatch.setattr(settings, "station_module_token", SecretStr(secrets.token_hex(32)))


# ── The setting: validated on save, kept across a NAP switch ─────────────


def _validated(**timetable: Any) -> dict[str, Any]:
    raw = {
        "id": "ZZRAIL",
        "country_iso": "ZZ",
        "timetable": {"format": "netex_epip", "url": "https://zz.invalid/zz.zip", **timetable},
    }
    return ingestion._validate_provider(raw, 0)["timetable"]


@pytest.mark.parametrize("value", ["rail", " Rail ", "RAIL"])
def test_rail_is_the_one_default_mode(value: str) -> None:
    assert _validated(default_mode=value)["default_mode"] == "rail"


@pytest.mark.parametrize("value", [None, "", False])
def test_an_absent_default_mode_is_not_stored(value: Any) -> None:
    assert "default_mode" not in _validated(default_mode=value)
    assert "default_mode" not in _validated()


@pytest.mark.parametrize("value", ["bus", "tram", True, 1, ["rail"]])
def test_any_other_default_mode_is_refused(value: Any) -> None:
    with pytest.raises(ValueError, match=r"default_mode=.* must be \"rail\" or absent"):
        _validated(default_mode=value)


def test_a_derived_feed_takes_no_default_mode() -> None:
    raw = {
        "id": "ZZX",
        "country_iso": "ZZ",
        "timetable": {
            "format": "gtfs",
            "source": "cross_border_filter",
            "derived_from": {"session_id": "zz", "provider_id": "ZZRAIL"},
            "default_mode": "rail",
        },
    }
    assert "default_mode" not in ingestion._validate_provider(raw, 0)["timetable"]


def test_a_nap_source_switch_keeps_rail_by_default() -> None:
    config = {
        "sources": {
            "providers": [
                {
                    "id": "ZZRAIL",
                    "timetable": {
                        "format": "netex_epip",
                        "source": "url",
                        "url": "https://zz.invalid/zz.zip",
                        "default_mode": "rail",
                    },
                },
                {
                    "id": "ZZBUS",
                    "timetable": {"format": "gtfs", "source": "url", "url": "https://zz.invalid/b"},
                },
            ]
        }
    }
    resolver = {"type": "tdg", "dataset_id": "zz", "resource_id": 99}
    source_map = {
        "ZZRAIL": {"format": "netex_epip", "source": "nap", "resolver": resolver},
        "ZZBUS": {"format": "gtfs", "source": "nap", "resolver": resolver},
    }
    new_config, changed, _ = nap_source_map.apply(config, {"ZZRAIL", "ZZBUS"}, source_map)
    rail, bus = new_config["sources"]["providers"]
    assert changed == ["ZZBUS", "ZZRAIL"]
    assert rail["timetable"] == {**source_map["ZZRAIL"], "default_mode": "rail"}
    assert bus["timetable"] == source_map["ZZBUS"]
    assert "default_mode" not in source_map["ZZRAIL"]  # the map is not changed


# ── The provider card ─────────────────────────────────────────────────────


_READ = (
    "if (ttSource !== 'cross_border_filter' && card.querySelector('.prov-tt-default-rail')?.checked) {"
    "\n    timetable.default_mode = 'rail';\n  }"
)


def _card_script(tt: dict[str, Any], checked: bool, source: str = "url") -> str:
    text = TEMPLATE.read_text(encoding="utf-8")
    start = text.index("function timetableDefaultRailHTML(tt, ttSource)")
    function = text[start : text.index("\n}\n", start) + 3]
    return f"""
function escAttr(s) {{ return String(s); }}
{function}
const ttSource = {json.dumps(source)};
const html = timetableDefaultRailHTML({json.dumps(tt)}, ttSource);
const card = {{querySelector: sel => sel === '.prov-tt-default-rail' ? {{checked: {json.dumps(checked)}}} : null}};
const timetable = {{}};
{_READ}
console.log(JSON.stringify({{html, timetable}}));
"""


def _node(script: str) -> dict[str, Any]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_the_card_shows_the_checkbox_ticked_as_saved() -> None:
    ticked = _node(_card_script({"default_mode": "rail"}, True))
    plain = _node(_card_script({}, False))
    assert "This feed is rail when the file names no mode" in ticked["html"]
    assert 'class="prov-tt-default-rail" type="checkbox" checked' in ticked["html"]
    assert 'type="checkbox" >' in plain["html"]
    assert "Never for a feed" in plain["html"]
    assert '<div class="prov-tt-default-rail-row" >' in plain["html"]
    assert ticked["timetable"] == {"default_mode": "rail"}
    assert plain["timetable"] == {}


def test_a_derived_card_hides_the_checkbox_and_sends_no_default() -> None:
    derived = _node(_card_script({"default_mode": "rail"}, True, "cross_border_filter"))
    assert '<div class="prov-tt-default-rail-row" hidden>' in derived["html"]
    assert derived["timetable"] == {}
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "if (defaultRailRow) defaultRailRow.hidden = src === 'cross_border_filter';" in text


def test_the_card_reads_the_checkbox_back() -> None:
    text = TEMPLATE.read_text(encoding="utf-8")
    start = text.index("function readProviderCard(card)")
    reader = text[start : text.index("\n}\n", start)]
    assert _READ in reader
    html = text[text.index("function providerCardHTML(p, opts)") :]
    assert "${timetableDefaultRailHTML(tt, ttSource)}" in html[: html.index("\n}\n")]
