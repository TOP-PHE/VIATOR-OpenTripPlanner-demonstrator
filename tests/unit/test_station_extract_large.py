"""Large national archives: the stop extract reads them, within its bounds.

The production symptom (14 October): a national NeTEx of about 2 GB zipped
was refused `input_too_large` by the absolute caps of 4 GiB per member and
8 GiB per pass, and the small feeds of the same session waited behind it.
These tests hold the repair:

- the bounds: a member may unpack to `RATIO_MAX` times its packed size and
  64 GiB, checked first from the central directory (an archive that says it
  is too large is refused before a byte is read), then while reading;
- the NeTEx skip filter (`app/station_extract_skip.py`) drops exactly the
  elements it names, whatever the chunking, comments, CDATA, processing
  instructions, quoted `>` and nesting, keeping a few bytes between chunks;
  extracts are byte-identical with and without it;
- the GTFS reader gives the same stations, and refuses the same archives
  with the same reason, as the reader before it (re-stated here);
- a failure the same archive would meet again is remembered, so it is not
  read twice, until the archive, the extractor or a bound changes;
- the warm makes the smallest extracts first.

Every value is invented (`ZZ`, numbers of 9s), every archive built here.
"""

from __future__ import annotations

import collections
import csv
import gzip
import io
import json
import os
import random
import signal
import time
import tracemalloc
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from app import feed_fetch, station_extract, station_extract_common
from app import station_extract_gtfs as gtfs
from app import station_extract_netex as netex
from app.station_extract import ExtractError, build_extract, ensure_extract
from app.station_extract_common import ArchiveError, Budget, member_bound, members
from app.station_extract_skip import SkipFilter, root_prefix
from tests.unit.test_station_extract import (
    AGENCY,
    EPIP_SERVICE,
    EPIP_SITE,
    EPIP_TIMETABLE,
    NORDIC_LINE,
    NORDIC_SHARED,
    NS,
    ROUTES,
    SHA_A,
    STOPS,
    TRIPS,
    WHEN,
    _delivery,
    _gtfs,
    _large_netex,
    _provider,
    _zip,
    download_state,
)

MIB = 1024 * 1024
GIB = 1024 * MIB


def _reason(provider: dict[str, Any], archive: Path, tmp_path: Path) -> str:
    folder = tmp_path / f"r-{os.urandom(4).hex()}"
    try:
        build_extract(provider, archive, SHA_A, extracted_at=WHEN, cache_dir=folder)
    except ExtractError as exc:
        return exc.reason
    return "ok"


# ── The bounds ────────────────────────────────────────────────────────────


def test_the_bounds_are_the_documented_ones() -> None:
    assert station_extract_common.RATIO_MAX == 200
    assert station_extract_common.RATIO_FLOOR_BYTES == 64 * MIB
    assert station_extract_common.MEMBER_MAX_BYTES == 64 * GIB
    assert station_extract_common.ARCHIVE_MAX_BYTES == 64 * GIB


def test_member_bound() -> None:
    def info(packed: int) -> zipfile.ZipInfo:
        found = zipfile.ZipInfo("zz.xml")
        found.compress_size = packed
        return found

    assert member_bound(info(0)) == 64 * MIB  # a small member: the floor
    assert member_bound(info(MIB)) == 200 * MIB
    assert member_bound(info(2 * GIB)) == 64 * GIB  # the absolute cap
    assert member_bound(info(64 * MIB + 1)) == 200 * (64 * MIB + 1)


def _stop_times_rows(count: int, rng: random.Random) -> Iterator[bytes]:
    """Rows shaped like a national stop_times.txt: they pack some 5 to 10 times."""
    yield b"trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
    rows = []
    for n in range(count):
        minute = rng.randrange(24 * 60)
        clock = f"{minute // 60:02d}:{minute % 60:02d}:00"
        stop = "ZZ:1" if n < 20 else f"ZZ:{rng.randrange(99_999)}"  # trip ZZT0 calls ZZ:1
        rows.append(f"ZZT{n // 20},{clock},{clock},{stop},{n % 20}\n")
        if len(rows) == 5_000:
            yield "".join(rows).encode()
            rows = []
    yield "".join(rows).encode()


def _gtfs_member(path: Path, name: str, chunks: Iterator[bytes]) -> Path:
    head = {
        "agency.txt": "agency_id,agency_name\nZZ,ZZ Rail\n",
        "routes.txt": "route_id,agency_id,route_type\nR,ZZ,2\n",
        "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\nZZ:1,ZZ Gare,45.1,9.1\n",
        "trips.txt": "route_id,trip_id\nR,ZZT0\n",
        "stop_times.txt": "trip_id,stop_id\nZZT0,ZZ:1\n",
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for member, text in head.items():
            if member == name:
                with zf.open(member, "w", force_zip64=True) as out:
                    for chunk in chunks:
                        out.write(chunk)
            else:
                zf.writestr(member, text)
    return path


def test_ratio_guard_accepts_a_big_normal_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A member past the floor that packs like a real feed is read whole."""
    monkeypatch.setattr(station_extract_common, "RATIO_FLOOR_BYTES", MIB)
    archive = _gtfs_member(
        tmp_path / "big.zip", "stop_times.txt", _stop_times_rows(150_000, random.Random(9))
    )
    with zipfile.ZipFile(archive) as zf:
        info = zf.getinfo("stop_times.txt")
    assert info.file_size > 4 * MIB
    assert info.file_size < 200 * info.compress_size
    assert _reason(_provider(), archive, tmp_path) == "ok"


def _bomb(size: int) -> Iterator[bytes]:
    yield b"trip_id,stop_id\n"
    for _ in range(size // MIB):
        yield b"\n" * MIB  # deflate packs this some 1,000 times


def test_ratio_guard_refuses_a_bomb_before_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(station_extract_common, "RATIO_FLOOR_BYTES", MIB)
    archive = _gtfs_member(tmp_path / "bomb.zip", "stop_times.txt", _bomb(24 * MIB))
    with zipfile.ZipFile(archive) as zf:
        info = zf.getinfo("stop_times.txt")
    assert info.file_size > 200 * info.compress_size

    def never(*_args: Any) -> int:
        raise AssertionError("a byte was read")

    monkeypatch.setattr(station_extract_common._Counted, "readinto", never)
    assert _reason(_provider(), archive, tmp_path) == "input_too_large"


def test_ratio_guard_while_reading(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A member whose central directory is believed still stops at its bound
    while it is read (the check of the bytes really unpacked)."""
    monkeypatch.setattr(station_extract_common, "RATIO_FLOOR_BYTES", MIB)
    monkeypatch.setattr(gtfs, "check_declared", lambda *_a: None)
    monkeypatch.setattr(netex, "check_declared", lambda *_a: None)
    archive = _gtfs_member(tmp_path / "bomb.zip", "stop_times.txt", _bomb(24 * MIB))
    assert _reason(_provider(), archive, tmp_path) == "input_too_large"
    xml = _gtfs_member(tmp_path / "x.zip", "stop_times.txt", iter([b""]))
    with zipfile.ZipFile(xml, "a") as zf, zf.open("zz.xml", "w", force_zip64=True) as out:
        out.write(f"<PublicationDelivery {NS}><dataObjects>".encode())
        for _ in range(24):
            out.write(b" " * MIB)
        out.write(b"</dataObjects></PublicationDelivery>")
    assert _reason(_provider("netex_epip"), xml, tmp_path) == "input_too_large"


@pytest.mark.parametrize("constant", ["MEMBER_MAX_BYTES", "ARCHIVE_MAX_BYTES"])
def test_absolute_caps_refuse_from_the_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, constant: str
) -> None:
    monkeypatch.setattr(station_extract_common, constant, 300)

    def never(*_args: Any) -> Any:
        raise AssertionError("a member was opened")

    monkeypatch.setattr(gtfs, "open_member", never)
    monkeypatch.setattr(netex, "open_member", never)
    assert _reason(_provider(), _gtfs(tmp_path), tmp_path) == "input_too_large"
    archive = _zip(
        tmp_path / "n.zip", {"a.xml": _delivery(EPIP_SITE + EPIP_SERVICE + EPIP_TIMETABLE)}
    )
    assert _reason(_provider("netex_epip"), archive, tmp_path) == "input_too_large"


def test_the_pass_budget_counts_every_read(tmp_path: Path) -> None:
    """The same member read twice in a pass counts twice (stops.txt is)."""
    archive = _gtfs(tmp_path)
    with zipfile.ZipFile(archive) as zf:
        budget = Budget()
        size = zf.getinfo("stops.txt").file_size
        for _ in range(2):
            with station_extract_common.open_member(zf, "stops.txt", budget) as fh:
                fh.read()
        assert budget.used == 2 * size
        budget = Budget(size + 1)
        with station_extract_common.open_member(zf, "stops.txt", budget) as fh:
            fh.read()
        with (
            station_extract_common.open_member(zf, "stops.txt", budget) as fh,
            pytest.raises(ArchiveError, match="input_too_large"),
        ):
            fh.read()


# ── The skip filter ───────────────────────────────────────────────────────

_SKIP = frozenset({"passingTimes", "dayTypes", "Line"})
_KEEP = ("ServiceJourney", "LineRef", "Lines", "dayType", "passingTime", "zz")


@dataclass
class _Node:
    name: str
    prefix: str = ""
    attrs: str = ""
    children: list[Any] = field(default_factory=list)
    closed: bool = False  # written `<name/>`
    spaced_end: bool = False  # written `</name >`

    def render(self, cut: frozenset[str], prefix: str) -> str:
        qname = f"{self.prefix}:{self.name}" if self.prefix else self.name
        if cut and self.name in cut and self.prefix == prefix:
            return ""
        if self.closed:
            return f"<{qname}{self.attrs}/>"
        inner = "".join(c if isinstance(c, str) else c.render(cut, prefix) for c in self.children)
        end = f"</{qname} >" if self.spaced_end else f"</{qname}>"
        return f"<{qname}{self.attrs}>{inner}{end}"


_TRAPS = (
    "<!-- <passingTimes> </Line> -->",
    "<![CDATA[ <dayTypes> </passingTimes> ]] > ]]>",
    "<?zz <Line> ? > ?>",
    "zz &gt; text > here",
    "<!---->",
)


def _attrs(rng: random.Random) -> str:
    choices = ["", ' a="1>2"', " b='/>'", ' c="it\'s > -- no"', " d='say \"/>\"'", ' e="x"']
    return "".join(rng.choice(choices) for _ in range(rng.randrange(3)))


def _tree(rng: random.Random, depth: int, prefixes: tuple[str, ...]) -> _Node:
    names = (*_SKIP, *_KEEP)
    node = _Node(rng.choice(names), rng.choice(prefixes), _attrs(rng))
    node.closed = rng.random() < 0.15
    node.spaced_end = rng.random() < 0.2
    if node.closed:
        return node
    for _ in range(rng.randrange(4) if depth < 5 else 0):
        roll = rng.random()
        if roll < 0.25:
            node.children.append(rng.choice(_TRAPS))
        elif roll < 0.35 and node.name in _SKIP:
            # the same name nested inside itself
            node.children.append(_Node(node.name, node.prefix, _attrs(rng)))
        else:
            node.children.append(_tree(rng, depth + 1, prefixes))
    return node


def _feed_in_pieces(skip: SkipFilter, data: bytes, rng: random.Random) -> bytes:
    out = []
    at = 0
    while at < len(data):
        size = rng.randrange(1, 40)
        out.append(skip.feed(data[at : at + size]))
        at += size
    out.append(skip.finish())
    return b"".join(out)


@pytest.mark.parametrize("prefix", ["", "netex"], ids=["no-prefix", "prefix"])
def test_skip_filter_drops_exactly_the_named_elements(prefix: str) -> None:
    rng = random.Random(99)
    prefixes = (prefix, prefix, "zz") if prefix else ("", "", "zz")
    for _ in range(300):
        root = _Node("root", prefix, children=[_tree(rng, 0, prefixes) for _ in range(3)])
        whole = root.render(frozenset(), prefix).encode()
        expected = root.render(_SKIP, prefix).encode()
        cut: list[str] = []
        got = _feed_in_pieces(SkipFilter(_SKIP, prefix.encode(), cut.append), whole, rng)
        assert got == expected
        whole_at_once = SkipFilter(_SKIP, prefix.encode(), lambda _n: None)
        assert whole_at_once.feed(whole) + whole_at_once.finish() == expected
        assert set(cut) <= _SKIP


def test_skip_filter_reports_what_it_drops() -> None:
    cut: list[str] = []
    skip = SkipFilter(_SKIP, b"", cut.append)
    data = b"<a><Line/><LineRef ref='x'/><dayTypes><x/></dayTypes><Line id='1'>z</Line></a>"
    assert skip.feed(data) + skip.finish() == b"<a><LineRef ref='x'/></a>"
    assert cut == ["Line", "dayTypes", "Line"]


def test_skip_filter_keeps_a_few_bytes_whatever_it_drops() -> None:
    skip = SkipFilter(_SKIP, b"", lambda _n: None)
    assert skip.feed(b"<a><passingTimes>") == b"<a>"
    for piece in (b"<T x='>'/>" * 6_000, b"<!-- " + b"z" * 70_000, b" --><y/>" * 100):
        assert skip.feed(piece) == b""
        assert len(skip._carry) <= 64
    tail = skip.feed(b"</passingTimes><b/></a>") + skip.finish()
    assert tail == b"<b/></a>"


def test_skip_filter_leaves_an_unclosed_element_to_expat(tmp_path: Path) -> None:
    skip = SkipFilter(_SKIP, b"", lambda _n: None)
    assert skip.feed(b"<a><x/><dayTypes><b>") + skip.finish() == b"<a><x/>"
    # past the first piece expat reads whole, so that the filter cuts it
    archive = _zip(
        tmp_path / "cut.zip",
        {"a.xml": f"<PublicationDelivery {NS}><dataObjects>{' ' * 2048}<dayTypes><x/>"},
    )
    assert _reason(_provider("netex_epip"), archive, tmp_path) == "bad_archive"


def test_root_prefix() -> None:
    assert root_prefix(b'<?xml version="1.0"?><netex:PublicationDelivery x="1">', 21) == b"netex"
    assert root_prefix(b"<PublicationDelivery>", 0) == b""
    assert root_prefix(b"<a>", 9) == b""
    assert root_prefix(b"<1x:a>", 0) == b""


# ── NeTEx: the same extract with and without the filter ───────────────────


class _NoSkip:
    """The reader before the filter: every byte to expat."""

    def __init__(self, *_args: Any) -> None:
        pass

    def feed(self, data: bytes) -> bytes:
        return data

    def finish(self) -> bytes:
        return b""


def _extract_bytes(provider: dict[str, Any], archive: Path, folder: Path) -> bytes:
    result = build_extract(provider, archive, SHA_A, extracted_at=WHEN, cache_dir=folder)
    return result.path_gz.read_bytes()


def _same_with_and_without(
    archive: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fmt: str = "netex_epip"
) -> bytes:
    provider = _provider(fmt)
    filtered = _extract_bytes(provider, archive, tmp_path / "filtered")
    with monkeypatch.context() as patch:
        patch.setattr(netex, "SkipFilter", _NoSkip)
        unfiltered = _extract_bytes(provider, archive, tmp_path / "unfiltered")
    assert filtered == unfiltered
    return filtered


_PREFIXED = (
    '<?xml version="1.0"?>\n<!-- <passingTimes> -->\n'
    '<netex:PublicationDelivery xmlns:netex="http://www.netex.org.uk/netex" version="1.1">'
    "<netex:dataObjects><netex:CompositeFrame id='ZZ:CF'><netex:frames>"
    "<netex:ServiceCalendarFrame id='ZZ:SCF'><netex:dayTypes><netex:DayType id='ZZ:D'/>"
    "</netex:dayTypes></netex:ServiceCalendarFrame>"
    "<netex:SiteFrame id='ZZ:SF'><netex:stopPlaces>"
    "<netex:StopPlace id='ZZ:SP:9'><netex:Name>ZZ Ville &amp; Port</netex:Name>"
    "<netex:keyList><netex:KeyValue><netex:Key>uic</netex:Key><netex:Value>9900099"
    "</netex:Value></netex:KeyValue></netex:keyList>"
    "<netex:Centroid><netex:Location><netex:Longitude>9.9</netex:Longitude>"
    "<netex:Latitude>45.9</netex:Latitude></netex:Location></netex:Centroid>"
    "<netex:TransportMode>rail</netex:TransportMode>"
    "<netex:quays><netex:Quay id='ZZ:Q:9'><netex:PublicCode>9</netex:PublicCode></netex:Quay>"
    "</netex:quays></netex:StopPlace></netex:stopPlaces></netex:SiteFrame>"
    "<netex:ServiceFrame id='ZZ:SVF'><netex:lines><netex:Line id='ZZ:L' a='x>y'>"
    "<netex:TransportMode>rail</netex:TransportMode><netex:OperatorRef ref='ZZ:O'/>"
    "</netex:Line></netex:lines><netex:scheduledStopPoints>"
    "<netex:ScheduledStopPoint id='ZZ:SSP'/></netex:scheduledStopPoints>"
    "<netex:stopAssignments><netex:PassengerStopAssignment id='ZZ:PSA' order='1'>"
    "<netex:ScheduledStopPointRef ref='ZZ:SSP'/><netex:QuayRef ref='ZZ:Q:9'/>"
    "</netex:PassengerStopAssignment></netex:stopAssignments>"
    "<netex:journeyPatterns><netex:ServiceJourneyPattern id='ZZ:SJP'>"
    "<netex:RouteView><netex:LineRef ref='ZZ:L'/></netex:RouteView><netex:pointsInSequence>"
    "<netex:StopPointInJourneyPattern id='ZZ:P' order='1'>"
    "<netex:ScheduledStopPointRef ref='ZZ:SSP'/></netex:StopPointInJourneyPattern>"
    "</netex:pointsInSequence></netex:ServiceJourneyPattern></netex:journeyPatterns>"
    "</netex:ServiceFrame>"
    "<netex:TimetableFrame id='ZZ:TF'><netex:vehicleJourneys>"
    "<netex:ServiceJourney id='ZZ:SJ'><!-- </netex:ServiceJourney> -->"
    "<netex:dayTypes><netex:DayTypeRef ref='ZZ:D'/></netex:dayTypes>"
    "<netex:ServiceJourneyPatternRef ref='ZZ:SJP'/>"
    "<netex:passingTimes><![CDATA[</netex:passingTimes>]]><netex:TimetabledPassingTime>"
    "<netex:DepartureTime>09:09:00</netex:DepartureTime></netex:TimetabledPassingTime>"
    "</netex:passingTimes></netex:ServiceJourney>"
    "</netex:vehicleJourneys></netex:TimetableFrame>"
    "</netex:frames></netex:CompositeFrame></netex:dataObjects></netex:PublicationDelivery>"
)


def test_netex_extracts_identical_with_the_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    epip = _zip(
        tmp_path / "epip.zip", {"zz.xml": _delivery(EPIP_TIMETABLE + EPIP_SERVICE + EPIP_SITE)}
    )
    _same_with_and_without(epip, tmp_path / "epip", monkeypatch)
    nordic = _zip(
        tmp_path / "nordic.zip",
        {"_ZZ_shared_data.xml": NORDIC_SHARED, "ZZ_line_L1.xml": NORDIC_LINE},
    )
    _same_with_and_without(nordic, tmp_path / "nordic", monkeypatch, "netex_nordic")
    large = _large_netex(tmp_path / "large.zip", journeys=2_000)
    _same_with_and_without(large, tmp_path / "large", monkeypatch)
    prefixed = _zip(tmp_path / "prefixed.zip", {"zz.xml": _PREFIXED})
    body = gzip.decompress(_same_with_and_without(prefixed, tmp_path / "prefixed", monkeypatch))
    row = next(csv.DictReader(io.StringIO(body.decode())))
    assert (row["stop_key"], row["stop_name"], row["n_calls"]) == (
        "ZZ:SP:9",
        "ZZ Ville & Port",
        "1",
    )
    assert row["operators"] == "ZZ:O"
    assert row["codes"] == "@id=ZZ:SP:9|uic=9900099|Quay/@id=ZZ:Q:9|Quay/PublicCode=9"


def test_netex_filter_drops_the_bulk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The passing times never reach expat, nor the journeys in pass 2."""
    seen: collections.Counter[str] = collections.Counter()
    start = netex._Builder.start

    def counting(self: Any, name: str, attrs: dict[str, str]) -> None:
        seen[self._local(name)] += 1
        start(self, name, attrs)

    monkeypatch.setattr(netex._Builder, "start", counting)
    large = _large_netex(tmp_path / "large.zip", journeys=300)
    _extract_bytes(_provider("netex_epip"), large, tmp_path / "x")
    # The first kilobyte of a document goes to expat before the filter takes
    # over, in passes 1 and 2: at most part of the first journey (20 passing
    # times each, 6,000 in all) is parsed there.
    assert seen["TimetabledPassingTime"] <= 40
    assert seen["DepartureTime"] <= 40
    assert 300 <= seen["ServiceJourney"] <= 302  # pass 1, and not pass 2


def test_netex_filter_uses_the_root_prefix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A document written with a prefix is cut with that prefix."""
    seen: collections.Counter[str] = collections.Counter()
    start = netex._Builder.start

    def counting(self: Any, name: str, attrs: dict[str, str]) -> None:
        seen[self._local(name)] += 1
        start(self, name, attrs)

    monkeypatch.setattr(netex._Builder, "start", counting)
    padded = _PREFIXED.replace("<netex:frames>", "<netex:frames>" + " " * 2048, 1)
    archive = _zip(tmp_path / "prefixed.zip", {"zz.xml": padded})
    _extract_bytes(_provider("netex_epip"), archive, tmp_path / "x")
    assert seen["TimetabledPassingTime"] == 0
    assert seen["DayType"] == 0
    assert seen["ScheduledStopPoint"] == 0
    assert seen["ServiceJourney"] == 1  # pass 1 only


def test_netex_memory_flat_on_a_national_shape(tmp_path: Path) -> None:
    """Twenty times the journeys, the same peak: what is kept is bounded by
    the rail part, never by the passing times."""
    small = _large_netex(tmp_path / "small.zip", journeys=400)
    large = _large_netex(tmp_path / "large.zip", journeys=8_000)
    peaks = []
    for archive in (small, large):
        tracemalloc.start()
        try:
            _extract_bytes(_provider("netex_epip"), archive, tmp_path / archive.stem)
            peaks.append(tracemalloc.get_traced_memory()[1])
        finally:
            tracemalloc.stop()
    assert peaks[1] < 8 * MIB, peaks
    assert peaks[1] < 2 * peaks[0] + MIB, peaks


# ── GTFS: the same stations, and the same refusals, as the reader before ──


def _legacy_lines(text: io.TextIOWrapper, size: list[int]) -> Iterator[str]:
    while line := text.readline(gtfs.RECORD_MAX + 1):
        size[0] += len(line)
        if size[0] > gtfs.RECORD_MAX:
            raise ArchiveError("input_too_large", "a GTFS record longer than 1 MiB")
        yield line


def _legacy_checked(record: list[str], header: list[str]) -> dict[str, str]:
    if len(record) > gtfs.FIELDS_MAX:
        raise ArchiveError("input_too_large", "a GTFS record with too many fields")
    row = {name: value.strip() for name, value in zip(header, record, strict=False)}
    if any(len(row.get(name, "")) > gtfs.ID_MAX for name in gtfs._ID_COLUMNS):
        raise ArchiveError("input_too_large", "a GTFS id longer than 255 characters")
    return row


def _legacy_rows(zf: zipfile.ZipFile, member: str, budget: Budget) -> Iterator[dict[str, str]]:
    with station_extract_common.open_member(zf, member, budget) as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8-sig", errors="replace", newline="")
        size = [0]
        reader = csv.reader(_legacy_lines(text, size))
        header = next(reader, [])
        if len(header) > gtfs.FIELDS_MAX:
            raise ArchiveError("input_too_large", "a GTFS header with too many fields")
        header = [name.strip() for name in header]
        size[0] = 0
        for record in reader:
            size[0] = 0
            if record:
                yield _legacy_checked(record, header)


def _legacy_calls(
    zf: zipfile.ZipFile, files: dict[str, str], trips: dict[str, int], budget: Budget
) -> dict[str, collections.Counter[int]]:
    calls: dict[str, collections.Counter[int]] = {}
    for row in _legacy_rows(zf, files["stop_times.txt"], budget):
        profile = trips.get(row.get("trip_id", ""))
        if profile is None:
            continue
        if row.get("pickup_type") == "1" and row.get("drop_off_type") == "1":
            continue
        stop_id = row.get("stop_id", "")
        if not stop_id:
            continue
        if stop_id not in calls and len(calls) >= gtfs.CALLED_STOPS_MAX:
            raise ArchiveError("too_many_rows", "more called stops than VIATOR keeps")
        calls.setdefault(stop_id, collections.Counter())[profile] += 1
    return calls


def _outcome(archive: Path) -> Any:
    try:
        with zipfile.ZipFile(archive) as zf:
            return gtfs.read_gtfs(zf, "ZZRAIL")
    except ArchiveError as exc:
        return ("refused", exc.reason, exc.detail)
    except csv.Error as exc:
        return ("csv", str(exc))


def _both(archive: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any]:
    new = _outcome(archive)
    with monkeypatch.context() as patch:
        patch.setattr(gtfs, "_rows", _legacy_rows)
        patch.setattr(gtfs, "_calls", _legacy_calls)
        old = _outcome(archive)
    return new, old


_ODD_STOP_TIMES = {
    "plain": "trip_id,stop_id\nT1,ZZ:S1:P1\nT2,ZZ:S2\n",
    "crlf": "trip_id,stop_id\r\nT1,ZZ:S1:P1\r\nT4,ZZ:S3\r\n",
    "bare-cr": "trip_id,stop_id\rT1,ZZ:S1:P1\rT4,ZZ:S3",
    "bom-padded": "﻿trip_id , stop_id \n T1 , ZZ:S1:P1 \n\nT2,ZZ:S2",
    "quoted": 'trip_id,stop_id\n"T1","ZZ:S1:P1"\n"T2","ZZ:S2"\n',
    "quoted-newline": 'trip_id,note,stop_id\nT1,"a\nb, c",ZZ:S1:P1\nT2,"x""y",ZZ:S2\n',
    "breaks": "trip_id,note,stop_id\nT1,a\x85b\u2028c\x0cd,ZZ:S1:P1\nT2,\x1c,ZZ:S2\n",
    "nul": "trip_id,stop_id\nT1,ZZ:S1:P1\x00\nT2,ZZ:S2\n",
    "twice": "trip_id,stop_id,trip_id\nT9,ZZ:S1:P1,T1\nT2,ZZ:S2\nT1\n",
    "short-long": "trip_id,stop_id,pickup_type,drop_off_type\nT1,ZZ:S2,1,1,9,9\nT2\n",
    "no-newline": "trip_id,stop_id\nT1,ZZ:S1:P1",
    "long-id": "trip_id,stop_id\nT1,ZZ:S1:P1\nT3," + "9" * 256 + "\n",
    "long-padded-id": "trip_id,stop_id\nT1,  " + "9" * 255 + "  \n",
    "many-fields": "trip_id,stop_id\nT1,ZZ:S2" + ",9" * 250 + "\n",
    "unclosed-quote": 'trip_id,stop_id\nT1,"ZZ:S1:P1\nT2,ZZ:S2\n',
}


def test_gtfs_reads_bad_utf8_as_before(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive = _zip(
        tmp_path / "latin.zip",
        {
            "agency.txt": AGENCY,
            "stops.txt": STOPS,
            "routes.txt": ROUTES,
            "trips.txt": TRIPS,
            "stop_times.txt": b"trip_id,stop_id\nT1,ZZ:S\xe9\xff\nT1,ZZ:S1:P1\n",
        },
    )
    for block in (1, 4, 4096):
        monkeypatch.setattr(gtfs, "_TEXT_BLOCK", block)
        new, old = _both(archive, monkeypatch)
        assert new == old


@pytest.mark.parametrize("case", sorted(_ODD_STOP_TIMES))
def test_gtfs_reads_as_before(case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive = _gtfs(tmp_path, stop_times=_ODD_STOP_TIMES[case])
    for block in (1, 2, 3, 7, gtfs._TEXT_BLOCK):
        monkeypatch.setattr(gtfs, "_TEXT_BLOCK", block)
        new, old = _both(archive, monkeypatch)
        assert new == old, (case, block)


def test_gtfs_record_bound_as_before(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """At the bound and one past it, on one line and across quoted lines."""
    monkeypatch.setattr(gtfs, "RECORD_MAX", 100)  # past every line of the other files
    for length in range(96, 105):
        for text in (
            "trip_id,stop_id\nT1," + "S" * (length - 4) + "\n",
            'trip_id,stop_id\nT1,"' + "S" * (length // 2) + "\n" + "S" * (length // 2) + '"\n',
            'trip_id,stop_id\nT1,"' + "\n".join(["S" * (length // 3)] * 3) + '"\n',
            "trip_id,stop_id\r\nT1," + "S" * (length - 5) + "\r\n",
        ):
            archive = _gtfs(tmp_path, name=f"b{length}.zip", stop_times=text)
            for block in (5, 64):
                monkeypatch.setattr(gtfs, "_TEXT_BLOCK", block)
                new, old = _both(archive, monkeypatch)
                assert new == old, (length, text, block)


def test_gtfs_fuzz_as_before(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rng = random.Random(9999)
    pieces = ["T1", "T2", "T4", "ZZ:S1:P1", "ZZ:S2", "ZZ:S3", "1", "0", "", " ", '"', ",", "\r"]
    pieces += ["\n", "\r\n", '""', "\x85", "x"]
    for n in range(120):
        header = rng.choice(
            [
                "trip_id,stop_id,pickup_type,drop_off_type",
                "stop_id,trip_id",
                "trip_id,stop_id,trip_id",
                '"trip_id","stop_id"',
            ]
        )
        body = "".join(rng.choice(pieces) for _ in range(rng.randrange(200)))
        archive = _gtfs(tmp_path, name=f"f{n}.zip", stop_times=f"{header}\n{body}")
        monkeypatch.setattr(gtfs, "_TEXT_BLOCK", rng.choice([1, 3, 16, 4096]))
        new, old = _both(archive, monkeypatch)
        assert new == old, (header, body)


def test_gtfs_lines_split_as_readline(monkeypatch: pytest.MonkeyPatch) -> None:
    rng = random.Random(99)
    alphabet = ["a", ",", '"', "\r", "\n", "\r\n", "\x85", "\u2028", "\x0b", "\x1d", "é"]
    for _ in range(300):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randrange(80)))
        monkeypatch.setattr(gtfs, "_TEXT_BLOCK", rng.choice([1, 2, 5, 64]))

        def stream(value: str = text) -> io.TextIOWrapper:
            return io.TextIOWrapper(io.BytesIO(value.encode()), encoding="utf-8", newline="")

        expected = list(_legacy_lines(stream(), [0]))
        size = [0]
        got = []
        for line in gtfs._lines(stream(), size):
            got.append(line)
            size[0] = 0  # as the reader does after each record
        assert got == expected, text


def test_gtfs_lines_split_in_linear_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hostile block with no line ending, and a break `str.splitlines`
    would take (so the slow path runs), is split in one pass: a regular
    expression that scans each line from every position would take minutes
    here (a quadratic 10^11 steps), this takes milliseconds."""
    block = "\x85" + "9" * 400_000
    started = time.monotonic()
    lines, carry = gtfs._split(block)
    assert time.monotonic() - started < 2
    assert (lines, carry) == ([], block)
    lines, carry = gtfs._split("a\x85b\r\nc\rd\ne\r")
    assert (lines, carry) == (["a\x85b\r\n", "c\r", "d\n"], "e\r")
    # and the whole reader, on such a file, refuses it at the record bound
    monkeypatch.setattr(gtfs, "RECORD_MAX", 300_000)
    size = [0]
    stream = io.TextIOWrapper(io.BytesIO(block.encode()), encoding="utf-8", newline="")
    started = time.monotonic()
    with pytest.raises(ArchiveError, match="input_too_large"):
        list(gtfs._lines(stream, size))
    assert time.monotonic() - started < 2


def test_gtfs_stop_times_rows_never_dictionaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """stop_times.txt goes through the column reader, not `_rows`."""
    read: list[str] = []
    rows = gtfs._rows

    def recording(zf: zipfile.ZipFile, member: str, budget: Budget) -> Iterator[dict[str, str]]:
        read.append(member)
        return rows(zf, member, budget)

    monkeypatch.setattr(gtfs, "_rows", recording)
    with zipfile.ZipFile(_gtfs(tmp_path)) as zf:
        assert gtfs.read_gtfs(zf, "ZZRAIL")
        assert members(zf)["stop_times.txt"] not in read


# ── Remembered failures ───────────────────────────────────────────────────


class _Child:
    """`_run_child` in this process: counts the extractions, fails when told."""

    def __init__(self, fail: ExtractError | None = None) -> None:
        self.fail = fail
        self.runs = 0

    def __call__(self, target: Any, args: tuple[Any, ...]) -> station_extract.ExtractResult:
        self.runs += 1
        if self.fail is not None:
            raise self.fail
        result: station_extract.ExtractResult = target(*args)
        return result


def _ensure(archive: Path, folder: Path, **kwargs: Any) -> Any:
    try:
        return ensure_extract(
            _provider(),
            archive,
            SHA_A,
            extracted_at=WHEN,
            session_id="zz",
            cache_dir=folder,
            **kwargs,
        )
    except ExtractError as exc:
        return (exc.reason, exc.detail)


@pytest.fixture
def lock_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(station_extract.settings, "inbox_dir", tmp_path / "inbox")
    (tmp_path / "inbox").mkdir(exist_ok=True)
    return tmp_path / "x"


def test_a_failure_is_remembered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lock_dir: Path
) -> None:
    child = _Child(ExtractError("input_too_large", "zz"))
    monkeypatch.setattr(station_extract, "_run_child", child)
    archive = _gtfs(tmp_path)
    assert _ensure(archive, lock_dir) == ("input_too_large", "zz")
    assert _ensure(archive, lock_dir) == ("input_too_large", "remembered")
    assert child.runs == 1
    # the worker's pass before a build hears the real reason, not not_cached
    assert _ensure(archive, lock_dir, cache_only=True) == ("input_too_large", "remembered")
    (marker,) = lock_dir.glob("*.failed")
    assert json.loads(marker.read_text())["reason"] == "input_too_large"


def test_extract_known(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(station_extract.settings, "inbox_dir", tmp_path)
    monkeypatch.setattr(station_extract, "_run_child", _Child(ExtractError("bad_archive")))
    assert not station_extract.extract_known(_provider(), SHA_A, session_id="zz")
    _ensure(_gtfs(tmp_path), station_extract.extracts_dir("zz"))
    assert station_extract.extract_known(_provider(), SHA_A, session_id="zz")
    assert not station_extract.extract_known(_provider(), "a" * 63, session_id="zz")


@pytest.mark.parametrize(
    ("reason", "detail", "remembered"),
    [
        ("bad_archive", "BadZipFile", True),
        ("no_rail_stop", "", True),
        ("too_many_rows", "", True),
        ("resource_limit", "out of memory", True),
        ("resource_limit", f"the child ended with {-signal.SIGXCPU}", True),
        ("resource_limit", "wall-clock time", False),
        ("resource_limit", f"the child ended with {-signal.SIGKILL}", False),
        ("busy", "another extraction runs", False),
        ("error", "OSError", False),
        ("cache", "OSError", False),
    ],
)
def test_which_failures_are_remembered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lock_dir: Path,
    reason: str,
    detail: str,
    remembered: bool,
) -> None:
    child = _Child(ExtractError(reason, detail))
    monkeypatch.setattr(station_extract, "_run_child", child)
    archive = _gtfs(tmp_path)
    _ensure(archive, lock_dir)
    second = _ensure(archive, lock_dir)
    assert child.runs == (1 if remembered else 2)
    assert second == ((reason, "remembered") if remembered else (reason, detail))


def test_a_remembered_failure_counts_under_its_bounds_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lock_dir: Path
) -> None:
    child = _Child(ExtractError("input_too_large"))
    monkeypatch.setattr(station_extract, "_run_child", child)
    archive = _gtfs(tmp_path)
    for module, name, value in (
        (station_extract_common, "RATIO_MAX", 300),
        (station_extract_common, "ARCHIVE_MAX_BYTES", 99 * GIB),
        (station_extract, "CHILD_CPU_SECONDS", 99),
        (station_extract, "CHILD_MEMORY_BYTES", 99),
        (netex, "TARGET_ELEMENTS_MAX", 99),
        (gtfs, "RECORD_MAX", 99),
    ):
        # under the bounds of the release: remembered after one reading
        _ensure(archive, lock_dir)
        runs = child.runs
        assert _ensure(archive, lock_dir) == ("input_too_large", "remembered")
        assert child.runs == runs
        # one bound changed: read again
        with monkeypatch.context() as patch:
            patch.setattr(module, name, value)
            _ensure(archive, lock_dir)
            assert child.runs == runs + 1, name
    # back under the first bounds, the marker last written (under other
    # bounds) does not count: the archive is read again, and now succeeds
    child.fail = None
    made = _ensure(archive, lock_dir)
    assert isinstance(made, station_extract.ExtractResult)


def test_a_damaged_marker_is_not_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lock_dir: Path
) -> None:
    child = _Child(ExtractError("input_too_large"))
    monkeypatch.setattr(station_extract, "_run_child", child)
    archive = _gtfs(tmp_path)
    _ensure(archive, lock_dir)
    (marker,) = lock_dir.glob("*.failed")
    signature = station_extract._limits_signature()
    for damaged in (
        b"{",
        b"[]",
        json.dumps({"reason": "busy", "limits": signature}).encode(),
        json.dumps({"reason": 9, "limits": signature}).encode(),
        b" " * 2_000 + json.dumps({"reason": "bad_archive", "limits": signature}).encode(),
    ):
        marker.write_bytes(damaged)
        runs = child.runs
        _ensure(archive, lock_dir)
        assert child.runs == runs + 1, damaged


def test_an_extract_replaces_the_remembered_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lock_dir: Path
) -> None:
    child = _Child(ExtractError("input_too_large"))
    monkeypatch.setattr(station_extract, "_run_child", child)
    archive = _gtfs(tmp_path)
    _ensure(archive, lock_dir)
    assert list(lock_dir.glob("*.failed"))
    # the next archive of the provider is extracted: the old marker goes
    child.fail = None
    made = ensure_extract(
        _provider(), archive, "b" * 64, extracted_at=WHEN, session_id="zz", cache_dir=lock_dir
    )
    assert made.rows > 0
    assert not list(lock_dir.glob("*.failed"))
    # and a failure of a later archive replaces that extract
    child.fail = ExtractError("bad_archive")
    _ensure(archive, lock_dir)
    assert [p.name.endswith(".failed") for p in lock_dir.iterdir()] == [True]


def test_the_warm_does_not_read_a_remembered_archive_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inbox = tmp_path / "inbox"
    monkeypatch.setattr(station_extract.settings, "inbox_dir", inbox)
    (inbox / "s" / "gtfs").mkdir(parents=True)
    slot = inbox / "s" / "gtfs" / "zzrail.zip"
    slot.write_bytes(b"not a zip")
    feed_fetch.save_state(
        inbox / "s" / "_fetch_state", "provider[ZZRAIL].timetable(gtfs)", download_state(slot)
    )
    config = {"sources": {"providers": [_provider()]}}
    assert station_extract.warm_session_extracts("s", config) == {"ZZRAIL": "bad_archive"}
    hashed: list[Path] = []
    monkeypatch.setattr(station_extract, "_verify", lambda snap, _e: hashed.append(snap) or "")
    assert station_extract.warm_session_extracts("s", config) == {"ZZRAIL": "bad_archive"}
    assert hashed == []  # neither snapshotted nor hashed


# ── The warm: the smallest first ──────────────────────────────────────────


def _slots(inbox: Path) -> list[dict[str, Any]]:
    """Four providers of session `s`: a big, a small and a middle timetable
    file, and one without a file yet."""
    (inbox / "s" / "gtfs").mkdir(parents=True)
    providers = []
    for pid, extra in (("ZZBIG", 90_000), ("ZZSMALL", 0), ("ZZMID", 9_000), ("ZZNONE", -1)):
        provider = {**_provider(url=f"https://zz.example.invalid/{pid}.zip"), "id": pid}
        providers.append(provider)
        if extra < 0:
            continue  # no file yet
        slot = inbox / "s" / "gtfs" / f"{pid.lower()}.zip"
        _gtfs(slot.parent, name=slot.name, stops="stop_id\n" + os.urandom(extra).hex())
        feed_fetch.save_state(
            inbox / "s" / "_fetch_state",
            f"provider[{pid}].timetable(gtfs)",
            download_state(slot),
        )
    return providers


def test_the_warm_makes_the_smallest_extracts_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inbox = tmp_path / "inbox"
    monkeypatch.setattr(station_extract.settings, "inbox_dir", inbox)
    providers = _slots(inbox)
    order: list[str] = []

    def making(provider_cfg: dict[str, Any], *_a: Any, **_kw: Any) -> Any:
        order.append(provider_cfg["id"])
        raise ExtractError("busy")

    monkeypatch.setattr(station_extract, "ensure_extract", making)
    outcome = station_extract.warm_session_extracts("s", {"sources": {"providers": providers}})
    assert order == ["ZZSMALL", "ZZMID", "ZZBIG"]
    assert list(outcome) == ["ZZNONE", "ZZSMALL", "ZZMID", "ZZBIG"]


def test_the_warm_thread_works_through_every_feed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The warm thread has no budget: after a refresh it makes the extract of
    every feed, the smallest first, however long one takes, so that the
    next pass (a refresh's, or the worker's before a build, which takes only
    extracts already made) finds them in the cache."""
    import queue as queue_module
    import threading

    inbox = tmp_path / "inbox"
    monkeypatch.setattr(station_extract.settings, "inbox_dir", inbox)
    monkeypatch.setattr(station_extract, "_QUEUE", queue_module.Queue())
    monkeypatch.setattr(station_extract, "_PENDING", {})
    monkeypatch.setattr(station_extract, "_WORKER", [])
    providers = _slots(inbox)
    order: list[str] = []
    long_one = threading.Event()

    def making(provider_cfg: dict[str, Any], *_a: Any, **_kw: Any) -> Any:
        order.append(provider_cfg["id"])
        if provider_cfg["id"] == "ZZMID":
            long_one.wait(0.5)  # a long extraction: the ones after it still come
        raise ExtractError("resource_limit", "wall-clock time")

    monkeypatch.setattr(station_extract, "ensure_extract", making)
    assert station_extract.queue_warm("s", {"sources": {"providers": providers}})
    station_extract._QUEUE.join()
    assert order == ["ZZSMALL", "ZZMID", "ZZBIG"]
