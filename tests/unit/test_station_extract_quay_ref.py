"""A NeTEx StopPlaceRef that names a Quay is read through the Quay's StopPlace.

An export may give every PassengerStopAssignment a `StopPlaceRef` whose
`ref` is the id of a Quay (and no `QuayRef`): no assignment then names a
StopPlace, and the extract ended `no_rail_stop`. Such a ref is now read as a
`QuayRef` would be, in whatever order the frames and members come; a ref
that names a StopPlace keeps its meaning, one that names neither stays as
it was. The census counts the places so read, and the summary of a written
extract says it. Every archive is invented (ids with ZZ, numbers of 9s).
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from app import station_extract
from app.station_extract import ExtractError, build_extract
from app.station_extract_common import RAIL_MAIN, SUMMARY
from app.station_extract_netex import NetexCensus, read_netex
from tests.unit.test_station_extract import (
    WHEN,
    _by_key,
    _delivery,
    _msmm_check,
    _point,
    _provider,
    _psa,
    _zip,
    feed_key_for,
)
from tests.unit.test_station_extract_census import _fields, _site
from tests.unit.test_station_extract_default_mode import RAIL_DEFAULT, _journeys

SHA = "e" * 64
PARENTS = {"ZZ:StopPlace:991", "ZZ:StopPlace:992"}
QUAY_REFS = "stop place refs naming a quay: 2"


def _parent(n: int, kind: str = "") -> str:
    """A StopPlace with two Quays, ZZ:Quay:99n1 and ZZ:Quay:99n2."""
    kind_text = f"<StopPlaceType>{kind}</StopPlaceType>" if kind else ""
    return (
        f'<StopPlace id="ZZ:StopPlace:99{n}"><Name>Zzstadt {n}</Name>'
        "<Centroid><Location><Longitude>9.5</Longitude><Latitude>45.5</Latitude></Location>"
        f'</Centroid>{kind_text}<quays><Quay id="ZZ:Quay:99{n}1"/><Quay id="ZZ:Quay:99{n}2"/>'
        "</quays></StopPlace>"
    )


def _patterns(first: str, second: str, quay: str = "") -> str:
    """Two assignments whose StopPlaceRef is `first` and `second` (a
    QuayRef `quay` on the first too, when given), and the pattern."""
    return (
        '<ServiceFrame id="ZZ:SVF:P"><stopAssignments>'
        + _psa(1, "ZZ:SSP:1", first, quay=quay)
        + _psa(2, "ZZ:SSP:2", second)
        + '</stopAssignments><journeyPatterns><ServiceJourneyPattern id="ZZ:SJP:1">'
        "<pointsInSequence>"
        + _point(1, "ZZ:SSP:1")
        + _point(2, "ZZ:SSP:2")
        + "</pointsInSequence></ServiceJourneyPattern></journeyPatterns></ServiceFrame>"
    )


QUAY_PATTERNS = _patterns("ZZ:Quay:9911", "ZZ:Quay:9922")


def _document(kind: str = "", mode: str = "", patterns: str = QUAY_PATTERNS) -> str:
    return _delivery(_site(_parent(1, kind), _parent(2, kind)) + patterns + _journeys(2, mode=mode))


def _extract(
    tmp_path: Path, files: dict[str, str], default: bool = False
) -> tuple[dict[str, dict[str, str]], station_extract.ExtractResult] | ExtractError:
    tmp_path.mkdir(parents=True, exist_ok=True)
    archive = _zip(tmp_path / "zz.zip", files)
    provider = _provider("netex_epip", **(RAIL_DEFAULT if default else {}))
    try:
        made = build_extract(provider, archive, SHA, extracted_at=WHEN, cache_dir=tmp_path / "x")
    except ExtractError as exc:
        return exc
    return _by_key(_msmm_check(made.path_gz, str(feed_key_for(provider)))), made


def _rows(tmp_path: Path, files: dict[str, str], default: bool = False) -> dict[str, dict]:
    found = _extract(tmp_path, files, default)
    assert not isinstance(found, ExtractError), found
    return found[0]


def _read(document: str | dict[str, str], default: bool = False) -> tuple[dict, NetexCensus]:
    """The reader's stations of an archive, and its census."""
    files = {"zz.xml": document} if isinstance(document, str) else document
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    census = NetexCensus()
    with zipfile.ZipFile(buffer) as zf:
        return read_netex(zf, census, default_rail=default), census


# ── The reader ────────────────────────────────────────────────────────────


def test_a_stop_place_ref_naming_a_quay_gives_the_quays_stop_place_rail_by_default(
    tmp_path: Path,
) -> None:
    rows = _rows(tmp_path, {"zz.xml": _document()}, default=True)
    assert set(rows) == PARENTS
    for row in rows.values():
        assert (row["modes"], row["n_calls"]) == (RAIL_MAIN, "2")


def test_a_railway_station_parent_decides_without_rail_by_default(tmp_path: Path) -> None:
    rows = _rows(tmp_path, {"zz.xml": _document(kind="railStation")})
    assert set(rows) == PARENTS
    assert {row["n_calls"] for row in rows.values()} == {"2"}


def test_an_untyped_parent_without_rail_by_default_still_has_no_rail_stop(
    tmp_path: Path,
) -> None:
    """Nothing says rail: the fallback places the calls, the parents decide."""
    error = _extract(tmp_path, {"zz.xml": _document()})
    assert isinstance(error, ExtractError)
    assert error.reason == "no_rail_stop"
    assert _fields(error.census)["stop_place_ref_quays"] == "2"
    assert SUMMARY.fullmatch(error.summary)
    assert error.summary.endswith(", 2 stop places (0 rail), quay refs: 2")  # the short form


def test_rail_journeys_are_placed_at_the_quays_stop_place() -> None:
    """Journeys that say rail: before, the station was keyed by the Quay id
    (no StopPlace of that id, no name, no position)."""
    stations, _ = _read(_document(mode="rail"))
    assert set(stations) == PARENTS
    for key in PARENTS:
        assert stations[key].name.startswith("Zzstadt")
        assert stations[key].lat is not None


@pytest.mark.parametrize(
    "files",
    [
        pytest.param(
            {
                "a.xml": _delivery(QUAY_PATTERNS + _journeys(2)),
                "b.xml": _delivery(_site(_parent(1, "railStation"), _parent(2, "railStation"))),
            },
            id="site-frame-after-in-another-member",
        ),
        pytest.param(
            {
                "a.xml": _delivery(_site(_parent(1, "railStation"), _parent(2, "railStation"))),
                "b.xml": _delivery(QUAY_PATTERNS + _journeys(2)),
            },
            id="site-frame-before-in-another-member",
        ),
        pytest.param(
            {
                "zz.xml": _delivery(
                    QUAY_PATTERNS
                    + _journeys(2)
                    + _site(_parent(1, "railStation"), _parent(2, "railStation"))
                )
            },
            id="site-frame-after-in-the-same-member",
        ),
    ],
)
def test_the_order_of_frames_and_members_does_not_matter(files: dict[str, str]) -> None:
    stations, census = _read(files)
    assert set(stations) == PARENTS
    assert census.stop_place_ref_quays == 2


def test_a_stop_place_ref_naming_neither_stays_as_it_was() -> None:
    """A ref to nothing the archive defines: the calls stay at that ref, as
    before (the railStation fallback cannot hold there), and nothing is
    counted."""
    patterns = _patterns("ZZ:Elsewhere:991", "ZZ:Elsewhere:992")
    stations, census = _read(_document(mode="rail", patterns=patterns))
    assert set(stations) == {"ZZ:Elsewhere:991", "ZZ:Elsewhere:992"}
    assert census.stop_place_ref_quays == 0
    nothing, _ = _read(_document(kind="railStation", patterns=patterns))
    assert nothing == {}


def test_a_stop_place_ref_naming_a_stop_place_keeps_its_meaning() -> None:
    """An id both a StopPlace's and a Quay's: as a StopPlaceRef it names the
    StopPlace, as a QuayRef the Quay."""
    site = _site(
        _parent(1, "railStation"),
        '<StopPlace id="ZZ:StopPlace:992"><Name>Zzstadt 2</Name><StopPlaceType>railStation'
        '</StopPlaceType><quays><Quay id="ZZ:StopPlace:991"/></quays></StopPlace>',
    )
    patterns = (
        '<ServiceFrame id="ZZ:SVF:P"><stopAssignments>'
        + _psa(1, "ZZ:SSP:1", "ZZ:StopPlace:991")
        + _psa(2, "ZZ:SSP:2", quay="ZZ:StopPlace:991")
        + '</stopAssignments><journeyPatterns><ServiceJourneyPattern id="ZZ:SJP:1">'
        "<pointsInSequence>"
        + _point(1, "ZZ:SSP:1")
        + _point(2, "ZZ:SSP:2")
        + "</pointsInSequence></ServiceJourneyPattern></journeyPatterns></ServiceFrame>"
    )
    stations, census = _read(_delivery(site + patterns + _journeys(2)))
    assert set(stations) == PARENTS
    assert stations["ZZ:StopPlace:991"].calls[RAIL_MAIN] == 2
    assert stations["ZZ:StopPlace:992"].calls[RAIL_MAIN] == 2
    assert census.stop_place_ref_quays == 0


def test_a_known_quay_ref_wins_over_a_stop_place_ref_naming_another_quay() -> None:
    patterns = _patterns("ZZ:Quay:9922", "ZZ:Quay:9921", quay="ZZ:Quay:9911")
    stations, census = _read(_document(kind="railStation", patterns=patterns))
    assert set(stations) == PARENTS
    assert stations["ZZ:StopPlace:991"].calls[RAIL_MAIN] == 2
    assert census.stop_place_ref_quays == 1  # the second assignment only


def test_an_unknown_quay_ref_falls_back_on_the_stop_place_ref() -> None:
    patterns = _patterns("ZZ:Quay:9911", "ZZ:Quay:9922", quay="ZZ:Quay:999")
    stations, census = _read(_document(kind="railStation", patterns=patterns))
    assert set(stations) == PARENTS
    assert census.stop_place_ref_quays == 2


def test_a_feed_without_such_refs_counts_none_and_says_nothing(tmp_path: Path) -> None:
    patterns = _patterns("ZZ:StopPlace:991", "ZZ:StopPlace:992")
    document = _document(kind="railStation", patterns=patterns)
    stations, census = _read(document)
    assert set(stations) == PARENTS
    assert census.stop_place_ref_quays == 0
    assert census.default_summary() == ""
    found = _extract(tmp_path, {"zz.xml": document})
    assert not isinstance(found, ExtractError)
    rows, made = found
    assert set(rows) == PARENTS
    assert made.summary == ""
    assert not station_extract._counts_path(made.path_gz).exists()


# ── The census and the summary ────────────────────────────────────────────


def test_a_written_extract_says_its_stop_place_refs_were_read_through_their_quay(
    tmp_path: Path,
) -> None:
    files = {"zz.xml": _document(kind="railStation")}
    found = _extract(tmp_path, files)
    assert not isinstance(found, ExtractError)
    _, made = found
    assert made.summary == QUAY_REFS
    cached = _extract(tmp_path, files)
    assert not isinstance(cached, ExtractError)
    assert cached[1].dropped == {}  # from the cache
    assert cached[1].summary == QUAY_REFS


def test_rail_by_default_and_the_quay_refs_share_the_summary(tmp_path: Path) -> None:
    found = _extract(tmp_path, {"zz.xml": _document()}, default=True)
    assert not isinstance(found, ExtractError)
    assert found[1].summary == f"rail by default: 4 calls at 2 stop places, {QUAY_REFS}"


@pytest.mark.parametrize(
    ("calls", "places", "refs", "expected"),
    [
        pytest.param(0, 0, 0, "", id="nothing"),
        pytest.param(0, 0, None, "", id="unread"),
        pytest.param(9, 2, 0, "rail by default: 9 calls at 2 stop places", id="default-only"),
        pytest.param(
            999_999_999,
            999_999_999,
            999_999_999,
            "rail by default: 999,999,999 calls at 999,999,999 stop places, quay refs: 999,999,999",
            id="long-counts-take-the-short-form",
        ),
    ],
)
def test_the_summary_stays_within_its_bound(
    calls: int, places: int, refs: int | None, expected: str
) -> None:
    census = NetexCensus(
        rail_by_default=calls, rail_by_default_places=places, stop_place_ref_quays=refs
    )
    summary = census.default_summary()
    assert summary == expected
    assert summary == "" or SUMMARY.fullmatch(summary)


def test_the_whole_census_names_the_count() -> None:
    _, census = _read(_document(kind="railStation"))
    assert "stop_place_ref_quays=2" in census.full().split(" ")
    assert NetexCensus().full().count("stop_place_ref_quays=unread") == 1
