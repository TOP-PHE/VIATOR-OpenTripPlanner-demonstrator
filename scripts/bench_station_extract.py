"""Measure the stop extractor on synthetic archives shaped like national feeds.

Run by hand, never in CI:

    python scripts/bench_station_extract.py netex --mb 1000
    python scripts/bench_station_extract.py gtfs --rows 10000000

Every value is invented (ids with `ZZ`, numbers made of 9s). The archive is
generated in a temporary folder, read once by `build_extract` in this process
(the reading the child process of `ensure_extract` does), and removed. The
script prints the unpacked size, the time, the throughput, the peak of the
Python heap (`tracemalloc`, with `--heap`, which slows the run down) and the
peak resident memory of the process, so the throughput can be extrapolated to
a national feed of tens of GB.

The NeTEx archive copies the structure of a national EPIP export: one shared
file with the StopPlaces and a calendar, and one file per line holding a
ServiceFrame (Line, Route, scheduled stop points, links, assignments,
patterns), a ServiceCalendarFrame (day types and their assignments) and a
TimetableFrame whose journeys carry their passing times, which make up most
of the bytes. One line in eight is rail. The GTFS archive has one route in
eight rail, about 25 calls per trip.
"""

from __future__ import annotations

import argparse
import random
import resource
import sys
import tempfile
import time
import tracemalloc
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.station_extract import build_extract

NS = 'xmlns="http://www.netex.org.uk/netex" xmlns:gml="http://www.opengis.net/gml/3.2"'
SHA = "9" * 64
WHEN = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
MB = 1024 * 1024


def _delivery_head() -> bytes:
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n<PublicationDelivery {NS} version="1.1">'
        "<PublicationTimestamp>2026-10-14T00:00:00</PublicationTimestamp>"
        '<ParticipantRef>ZZ</ParticipantRef><dataObjects><CompositeFrame id="ZZ:CF" '
        'version="1"><frames>\n'
    ).encode()


_DELIVERY_TAIL = b"</frames></CompositeFrame></dataObjects></PublicationDelivery>\n"


def _stop_place(n: int, rng: random.Random) -> str:
    lat = 47.0 + rng.random() * 8
    lon = 6.0 + rng.random() * 9
    quays = "".join(
        f'<Quay id="ZZ:Quay:{n}-{q}" version="1"><keyList><KeyValue><Key>GlobalID</Key>'
        f"<Value>zz:99:{n}:{q}</Value></KeyValue></keyList><Name>Gleis {q}</Name>"
        f"<Centroid><Location><Longitude>{lon:.6f}</Longitude><Latitude>{lat:.6f}</Latitude>"
        f"</Location></Centroid><PublicCode>{q}</PublicCode></Quay>"
        for q in (1, 2)
    )
    mode = "rail" if n % 8 == 0 else "bus"
    return (
        f'<StopPlace id="ZZ:StopPlace:{n}" version="1"><keyList><KeyValue><Key>GlobalID</Key>'
        f"<Value>zz:99:{n}</Value></KeyValue></keyList><Name>ZZ Ort {n}</Name>"
        f"<Centroid><Location><Longitude>{lon:.6f}</Longitude><Latitude>{lat:.6f}</Latitude>"
        f"</Location></Centroid><TransportMode>{mode}</TransportMode>"
        f"<StopPlaceType>{'railStation' if mode == 'rail' else 'onstreetBus'}</StopPlaceType>"
        f"<quays>{quays}</quays></StopPlace>\n"
    )


def _shared(places: int, rng: random.Random) -> Iterator[bytes]:
    yield _delivery_head()
    yield b'<SiteFrame id="ZZ:SF" version="1"><stopPlaces>\n'
    for n in range(places):
        yield _stop_place(n, rng).encode()
    yield b"</stopPlaces></SiteFrame>\n"
    yield b'<ServiceCalendarFrame id="ZZ:SCF" version="1"><dayTypes>'
    for d in range(400):
        yield (
            f'<DayType id="ZZ:DayType:{d}" version="1"><properties><PropertyOfDay>'
            f"<DaysOfWeek>Monday Tuesday</DaysOfWeek></PropertyOfDay></properties></DayType>\n"
        ).encode()
    yield b"</dayTypes></ServiceCalendarFrame>\n"
    yield _DELIVERY_TAIL


def _line_file(line: int, places: int, journeys: int, rng: random.Random) -> Iterator[bytes]:
    rail = line % 8 == 0
    stops = rng.randint(15, 35)
    route = [rng.randrange(0, places, 8) if rail else rng.randrange(places) for _ in range(stops)]
    mode = "rail" if rail else "bus"
    yield _delivery_head()
    out = [f'<ServiceFrame id="ZZ:SVF:{line}" version="1"><lines>']
    out.append(
        f'<Line id="ZZ:Line:{line}" version="1"><Name>ZZ {line}</Name>'
        f"<TransportMode>{mode}</TransportMode>"
        f'<OperatorRef ref="ZZ:Operator:{line % 97}"/></Line></lines>'
        f'<routes><Route id="ZZ:Route:{line}" version="1"><LineRef ref="ZZ:Line:{line}"/>'
        "<pointsInSequence>"
    )
    out.extend(
        f'<PointOnRoute id="ZZ:PoR:{line}:{i}" order="{i + 1}" version="1">'
        f'<RoutePointRef ref="ZZ:RP:{line}:{i}"/></PointOnRoute>'
        for i in range(stops)
    )
    out.append("</pointsInSequence></Route></routes><scheduledStopPoints>")
    out.extend(
        f'<ScheduledStopPoint id="ZZ:SSP:{line}:{i}" version="1"><Name>ZZ Ort {p}</Name>'
        f"<Location><Longitude>9.{p}</Longitude><Latitude>50.{p}</Latitude></Location>"
        "</ScheduledStopPoint>"
        for i, p in enumerate(route)
    )
    out.append("</scheduledStopPoints><serviceLinks>")
    out.extend(
        f'<ServiceLink id="ZZ:SL:{line}:{i}" version="1">'
        f'<FromPointRef ref="ZZ:SSP:{line}:{i}"/><ToPointRef ref="ZZ:SSP:{line}:{i + 1}"/>'
        f'<projections><LinkSequenceProjection id="ZZ:LSP:{line}:{i}" version="1"><gml:LineString '
        f'gml:id="ZZ_LS_{line}_{i}"><gml:posList>'
        + " ".join(f"{50 + rng.random():.5f} {9 + rng.random():.5f}" for _ in range(6))
        + "</gml:posList></gml:LineString></LinkSequenceProjection></projections></ServiceLink>"
        for i in range(stops - 1)
    )
    out.append("</serviceLinks><stopAssignments>")
    out.extend(
        f'<PassengerStopAssignment id="ZZ:PSA:{line}:{i}" order="1" version="1">'
        f'<ScheduledStopPointRef ref="ZZ:SSP:{line}:{i}"/>'
        f'<StopPlaceRef ref="ZZ:StopPlace:{p}"/><QuayRef ref="ZZ:Quay:{p}-1"/>'
        "</PassengerStopAssignment>"
        for i, p in enumerate(route)
    )
    out.append(
        f'</stopAssignments><journeyPatterns><ServiceJourneyPattern id="ZZ:SJP:{line}" '
        f'version="1"><RouteRef ref="ZZ:Route:{line}"/><pointsInSequence>'
    )
    out.extend(
        f'<StopPointInJourneyPattern id="ZZ:SPJP:{line}:{i}" order="{i + 1}" version="1">'
        f'<ScheduledStopPointRef ref="ZZ:SSP:{line}:{i}"/></StopPointInJourneyPattern>'
        for i in range(stops)
    )
    out.append("</pointsInSequence></ServiceJourneyPattern></journeyPatterns></ServiceFrame>\n")
    yield "".join(out).encode()
    yield f'<ServiceCalendarFrame id="ZZ:SCF:{line}" version="1"><dayTypeAssignments>'.encode()
    for j in range(journeys):
        yield (
            f'<DayTypeAssignment id="ZZ:DTA:{line}:{j}" order="{j}" version="1">'
            f"<Date>2026-{1 + j % 12:02d}-{1 + j % 28:02d}</Date>"
            f'<DayTypeRef ref="ZZ:DayType:{line}:{j % 40}"/>'
            "<isAvailable>true</isAvailable></DayTypeAssignment>"
        ).encode()
    yield b"</dayTypeAssignments></ServiceCalendarFrame>\n"
    yield f'<TimetableFrame id="ZZ:TF:{line}" version="1"><vehicleJourneys>'.encode()
    for j in range(journeys):
        start = rng.randrange(4 * 60, 23 * 60)
        times = []
        for i in range(stops):
            minute = start + 3 * i
            clock = f"{(minute // 60) % 24:02d}:{minute % 60:02d}:00"
            times.append(
                f'<TimetabledPassingTime id="ZZ:TPT:{line}:{j}:{i}" version="1">'
                f'<StopPointInJourneyPatternRef ref="ZZ:SPJP:{line}:{i}"/>'
                f"<ArrivalTime>{clock}</ArrivalTime><DepartureTime>{clock}</DepartureTime>"
                "</TimetabledPassingTime>"
            )
        yield (
            f'<ServiceJourney id="ZZ:SJ:{line}:{j}" version="1"><dayTypes>'
            f'<DayTypeRef ref="ZZ:DayType:{line}:{j % 40}"/></dayTypes>'
            f'<ServiceJourneyPatternRef ref="ZZ:SJP:{line}"/>'
            f'<OperatorRef ref="ZZ:Operator:{line % 97}"/><LineRef ref="ZZ:Line:{line}"/>'
            f"<passingTimes>{''.join(times)}</passingTimes></ServiceJourney>\n"
        ).encode()
    yield b"</vehicleJourneys></TimetableFrame>\n"
    yield _DELIVERY_TAIL


def _write_member(zf: zipfile.ZipFile, name: str, chunks: Iterator[bytes]) -> int:
    size = 0
    with zf.open(name, "w", force_zip64=True) as out:
        for chunk in chunks:
            out.write(chunk)
            size += len(chunk)
    return size


def make_netex(path: Path, target_mb: int, places: int = 30_000, seed: int = 99) -> int:
    """A NeTEx archive of about `target_mb` MB unpacked, with `places`
    StopPlaces; returns its unpacked size."""
    rng = random.Random(seed)  # noqa: S311  # invented data, not a secret
    total = 0
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        total += _write_member(zf, "_ZZ_shared_data.xml", _shared(places, rng))
        line = 0
        while total < target_mb * MB:
            total += _write_member(
                zf, f"ZZ_line_{line}.xml", _line_file(line, places, rng.randint(80, 400), rng)
            )
            line += 1
    return total


_STOPS = 250_000  # one in eight a platform of a rail station
_RAIL_STATIONS = 4_000


def _gtfs_stop_times(rail: list[bool], rng: random.Random) -> Iterator[bytes]:
    yield b"trip_id,arrival_time,departure_time,stop_id,stop_sequence,pickup_type,drop_off_type\n"
    batch = []
    for t, is_rail in enumerate(rail):
        stops = 25
        start = rng.randrange(4 * 60, 23 * 60)
        base = rng.randrange(0, _RAIL_STATIONS if is_rail else _STOPS)
        for i in range(stops):
            minute = start + 2 * i
            clock = f"{minute // 60:02d}:{minute % 60:02d}:00"
            stop = 8 * ((base + i * 7) % _RAIL_STATIONS) if is_rail else (base + i * 7) % _STOPS
            batch.append(f"ZZT{t},{clock},{clock},ZZ:{stop},{i + 1},0,0\n")
        if len(batch) > 20_000:
            yield "".join(batch).encode()
            batch = []
    yield "".join(batch).encode()


def make_gtfs(path: Path, rows: int, seed: int = 99) -> int:
    rng = random.Random(seed)  # noqa: S311  # invented data, not a secret
    trips = max(1, rows // 25)
    routes = max(8, trips // 50)
    total = 0
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        total += _write_member(
            zf, "agency.txt", iter([b"agency_id,agency_name\nZZR,ZZ Rail\nZZB,ZZ Bus\n"])
        )
        stops = ["stop_id,stop_name,stop_lat,stop_lon,location_type,parent_station\n"]
        for s in range(_STOPS):
            parent = f"ZZP:{s // 16}" if s % 8 == 0 else ""
            stops.append(f"ZZ:{s},ZZ Halt {s},50.{s:06d},9.{s:06d},0,{parent}\n")
        stops.extend(
            f"ZZP:{p},ZZ Bahnhof {p},50.{p:06d},9.{p:06d},1,\n" for p in range(_STOPS // 16)
        )
        total += _write_member(zf, "stops.txt", iter(["".join(stops).encode()]))
        route_rows = ["route_id,agency_id,route_short_name,route_type\n"]
        route_rows.extend(
            f"ZZR{r},{'ZZR' if r % 8 == 0 else 'ZZB'},{r},{2 if r % 8 == 0 else 3}\n"
            for r in range(routes)
        )
        total += _write_member(zf, "routes.txt", iter(["".join(route_rows).encode()]))
        trip_routes = [rng.randrange(routes) for _ in range(trips)]
        trip_rows = ["route_id,service_id,trip_id\n"]
        trip_rows.extend(f"ZZR{r},ZZW,ZZT{t}\n" for t, r in enumerate(trip_routes))
        total += _write_member(zf, "trips.txt", iter(["".join(trip_rows).encode()]))
        rail = [r % 8 == 0 for r in trip_routes]
        total += _write_member(zf, "stop_times.txt", _gtfs_stop_times(rail, rng))
    return total


def run(fmt: str, archive: Path, heap: bool) -> None:
    provider = {
        "id": "ZZBENCH",
        "label": "ZZ Bench",
        "country_iso": "ZZ",
        "timetable": {
            "format": "netex_epip" if fmt == "netex" else "gtfs",
            "source": "url",
            "url": "https://zz.example.invalid/zz.zip",
        },
    }
    with zipfile.ZipFile(archive) as zf:
        unpacked = sum(info.file_size for info in zf.infolist())
    if heap:
        tracemalloc.start()
    with tempfile.TemporaryDirectory(prefix="zz-bench-out-") as out:
        cpu0 = time.process_time()
        wall0 = time.monotonic()
        result = build_extract(provider, archive, SHA, extracted_at=WHEN, cache_dir=Path(out))
        cpu = time.process_time() - cpu0
        wall = time.monotonic() - wall0
    peak_heap = tracemalloc.get_traced_memory()[1] if heap else 0
    rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    print(
        f"{fmt}: zip {archive.stat().st_size / MB:.0f} MB, unpacked {unpacked / MB:.0f} MB, "
        f"rows {result.rows}, cpu {cpu:.1f} s, wall {wall:.1f} s, "
        f"{unpacked / MB / cpu:.1f} MB/s (cpu), peak heap {peak_heap / MB:.0f} MiB, "
        f"peak RSS {rss_kb / 1024:.0f} MiB"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("format", choices=("netex", "gtfs"))
    parser.add_argument("--mb", type=int, default=500, help="NeTEx: MB unpacked")
    parser.add_argument("--places", type=int, default=30_000, help="NeTEx: StopPlaces")
    parser.add_argument("--rows", type=int, default=5_000_000, help="GTFS: stop_times rows")
    parser.add_argument("--heap", action="store_true", help="trace the Python heap (slower)")
    parser.add_argument("--keep", type=Path, help="keep the archive at this path")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="zz-bench-") as folder:
        archive = args.keep or Path(folder) / "zz.zip"
        if not archive.exists():
            if args.format == "netex":
                make_netex(archive, args.mb, args.places)
            else:
                make_gtfs(archive, args.rows)
        run(args.format, archive, args.heap)


if __name__ == "__main__":
    main()
