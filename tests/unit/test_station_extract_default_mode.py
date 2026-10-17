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
@pytest.mark.parametrize("held", [True, False], ids=["held", "sent"])
def test_the_module_is_asked_for_the_version_rail_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, held: bool
) -> None:
    setup = Setup(tmp_path, monkeypatch)
    setup.feeds[0].provider_cfg["timetable"]["default_mode"] = "rail"
    module = Module()
    if held:
        module.heads = [httpx.Response(200)]
    result = _run(setup, module, FakeClock())
    (head,) = module.head_requests
    assert dict(head.url.params) == {"extractor_version": "viator-stop-extract-2+rail-default"}
    (outcome,) = result.outcomes
    assert outcome.state == (up.HELD if held else up.SENT)
    assert outcome.summary == up.RAIL_BY_DEFAULT_SUMMARY
    assert SUMMARY.fullmatch(up.RAIL_BY_DEFAULT_SUMMARY)


@pytest.mark.usefixtures("token")
def test_a_plain_provider_shows_no_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    setup = Setup(tmp_path, monkeypatch)
    module = Module()
    module.heads = [httpx.Response(200)]
    (outcome,) = _run(setup, module, FakeClock()).outcomes
    assert outcome.summary is None


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


def _card_script(tt: dict[str, Any], checked: bool) -> str:
    text = TEMPLATE.read_text(encoding="utf-8")
    start = text.index("function timetableDefaultRailHTML(tt)")
    function = text[start : text.index("\n}\n", start) + 3]
    return f"""
function escAttr(s) {{ return String(s); }}
{function}
const html = timetableDefaultRailHTML({json.dumps(tt)});
const card = {{querySelector: sel => sel === '.prov-tt-default-rail' ? {{checked: {json.dumps(checked)}}} : null}};
const timetable = {{}};
if (card.querySelector('.prov-tt-default-rail')?.checked) timetable.default_mode = 'rail';
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
    assert ticked["timetable"] == {"default_mode": "rail"}
    assert plain["timetable"] == {}


def test_the_card_reads_the_checkbox_back() -> None:
    text = TEMPLATE.read_text(encoding="utf-8")
    start = text.index("function readProviderCard(card)")
    reader = text[start : text.index("\n}\n", start)]
    assert (
        "if (card.querySelector('.prov-tt-default-rail')?.checked) timetable.default_mode = 'rail';"
        in reader
    )
    html = text[text.index("function providerCardHTML(p, opts)") :]
    assert "${timetableDefaultRailHTML(tt)}" in html[: html.index("\n}\n")]
