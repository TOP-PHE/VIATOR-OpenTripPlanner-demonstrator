"""The stop extract under hostile or odd input, and its one entry point.

Review of pull request 4 (repair round 1): the readers bound what they read
(records, ids, nesting, texts, tokens, elements, stations, codes, unpacked
bytes); `ensure_extract` runs the extraction in a spawned child under a
memory, processor-time and wall-clock limit, one at a time on the machine,
and raises `ExtractError` only; the cache heals a damaged file; the slot and
its sha256 are paired from one open file; the parsing points of the
contract review (both references of an assignment, dangling parents,
positions without a system, the railStation fallback, direct calls).

Every archive is invented here (ZZ, 99); the hostile shapes are those of the
security review's probes, at a size a unit test can build in a second.
"""

from __future__ import annotations

import fcntl
import gzip
import logging
import os
import signal
import time
import tracemalloc
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from app import feed_fetch, station_extract, station_extract_common
from app import station_extract_gtfs as gtfs
from app import station_extract_netex as netex
from app.station_extract import ExtractError, build_extract, ensure_extract, feed_key_for
from tests.unit.test_station_extract import (
    EPIP_SERVICE,
    EPIP_SITE,
    EPIP_TIMETABLE,
    NS,
    SHA_A,
    SHA_B,
    STOP_TIMES,
    STOPS,
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
)

MIB = 1024 * 1024


def _rows(result: station_extract.ExtractResult, provider: dict[str, Any]) -> dict[str, Any]:
    return _by_key(_msmm_check(result.path_gz, str(feed_key_for(provider))))


def _reason(provider: dict[str, Any], archive: Path, tmp_path: Path) -> str:
    folder = tmp_path / f"r-{os.urandom(4).hex()}"  # never a cache hit of another archive
    try:
        build_extract(provider, archive, SHA_A, extracted_at=WHEN, cache_dir=folder)
    except ExtractError as exc:
        return exc.reason
    return "ok"


def _streamed(path: Path, name: str, chunks: Iterator[bytes]) -> Path:
    with (
        zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf,
        zf.open(name, "w", force_zip64=True) as out,
    ):
        for chunk in chunks:
            out.write(chunk)
    return path


def _repeat(head: bytes, unit: bytes, count: int, tail: bytes) -> Iterator[bytes]:
    yield head
    block = unit * max(1, (1 << 16) // len(unit))
    full, rest = divmod(count, (1 << 16) // len(unit) or 1)
    for _ in range(full):
        yield block
    yield unit * rest
    yield tail


def _peak(provider: dict[str, Any], archive: Path, folder: Path) -> tuple[int, str]:
    tracemalloc.start()
    try:
        try:
            build_extract(provider, archive, SHA_A, extracted_at=WHEN, cache_dir=folder)
            reason = "ok"
        except ExtractError as exc:
            reason = exc.reason
        return tracemalloc.get_traced_memory()[1], reason
    finally:
        tracemalloc.stop()


GTFS_HEAD = {
    "agency.txt": "agency_id,agency_name\nZZ,ZZ Rail\n",
    "routes.txt": "route_id,agency_id,route_type\nR,ZZ,2\n",
    "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\nZZ:S1,ZZ Gare,45.1,9.1\n",
    "trips.txt": "route_id,trip_id\nR,T1\n",
    "stop_times.txt": "trip_id,stop_id\nT1,ZZ:S1\n",
}


def _gtfs_with(path: Path, name: str, chunks: Iterator[bytes]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for member, text in GTFS_HEAD.items():
            if member == name:
                with zf.open(member, "w", force_zip64=True) as out:
                    for chunk in chunks:
                        out.write(chunk)
            else:
                zf.writestr(member, text)
    return path


# ── The probe shapes: refused early, memory flat ──────────────────────────


@pytest.mark.parametrize(
    ("shape", "fmt", "reason"),
    [
        ("gtfs_commas", "gtfs", "input_too_large"),
        ("gtfs_no_newline", "gtfs", "input_too_large"),
        ("gtfs_long_id", "gtfs", "input_too_large"),
        ("gtfs_child_codes", "gtfs", "ok"),
        ("netex_text", "netex_epip", "input_too_large"),
        ("netex_attribute", "netex_epip", "input_too_large"),
        ("netex_deep", "netex_epip", "input_too_large"),
        ("netex_whitespace", "netex_epip", "input_too_large"),
        ("netex_rail_quays", "netex_epip", "input_too_large"),
    ],
)
def test_probe_shapes_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str, fmt: str, reason: str
) -> None:
    archive = tmp_path / "a.zip"
    if shape == "gtfs_commas":
        _gtfs_with(archive, "trips.txt", _repeat(b"route_id,trip_id\nR,", b",", 24 * MIB, b"\n"))
    elif shape == "gtfs_no_newline":
        _gtfs_with(archive, "routes.txt", _repeat(b"route_id,route_type\n", b"a", 24 * MIB, b""))
    elif shape == "gtfs_long_id":
        _gtfs_with(archive, "trips.txt", iter([b"route_id,trip_id\nR,", b"T" * 300, b"\n"]))
    elif shape == "gtfs_child_codes":
        monkeypatch.setattr(station_extract_common, "RAW_CODES_MAX", 50)
        children = (
            f"ZZ:C{n:060d},ZZ Quai,45.1,9.1,ZZ:S1,{n:060d}\n".encode() for n in range(20_000)
        )
        head = b"stop_id,stop_name,stop_lat,stop_lon,parent_station,stop_code\n"
        _gtfs_with(archive, "stops.txt", iter([head, b"ZZ:S1,ZZ Gare,45.1,9.1,,\n", *children]))
    elif shape == "netex_text":
        head = f'<PublicationDelivery {NS}><ServiceJourney id="j"><Description>'.encode()
        _streamed(archive, "a.xml", _repeat(head, b"A", 24 * MIB, b"</Description>"))
    elif shape == "netex_attribute":
        head = f'<PublicationDelivery {NS}><Junk v="'.encode()
        _streamed(archive, "a.xml", _repeat(head, b"A", 24 * MIB, b'"/></PublicationDelivery>'))
    elif shape == "netex_deep":
        head = f"<PublicationDelivery {NS}>".encode()
        _streamed(archive, "a.xml", iter([head, b"<d>" * 200_000, b"</d>" * 200_000]))
    elif shape == "netex_whitespace":
        head = f"<PublicationDelivery {NS}>".encode()
        _streamed(archive, "a.xml", _repeat(head, b" ", 24 * MIB, b"</PublicationDelivery>"))
    elif shape == "netex_rail_quays":
        monkeypatch.setattr(netex, "TARGET_ELEMENTS_MAX", 20_000)
        head = (
            f"<PublicationDelivery {NS}><dataObjects>"
            '<Line id="ZZ:L"><TransportMode>rail</TransportMode></Line>'
            '<ServiceJourney id="J"><LineRef ref="ZZ:L"/><ServiceJourneyPatternRef ref="P"/>'
            '</ServiceJourney><ServiceJourneyPattern id="P"><pointsInSequence>'
            + _point(1, "S1")
            + "</pointsInSequence></ServiceJourneyPattern>"
            + _psa(1, "S1", "ZZ:SP")
            + '<StopPlace id="ZZ:SP"><Name>ZZ</Name><quays>'
        ).encode()
        quays = (f'<Quay id="ZZ:Q:{n:080d}"/>'.encode() for n in range(60_000))
        tail = b"</quays></StopPlace></dataObjects></PublicationDelivery>"
        _streamed(archive, "a.xml", iter([head, *quays, tail]))
    peak, got = _peak(_provider(fmt), archive, tmp_path / "x")
    assert got == reason
    assert peak < 24 * MIB, (shape, peak)


def test_gtfs_child_codes_capped_while_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(station_extract_common, "RAW_CODES_MAX", 5)
    children = "".join(f"ZZ:S1:Q{n},,quai,48.8,2.3,0,ZZ:S1,\n" for n in range(10))
    result = _build(_provider(), _gtfs(tmp_path, stops=STOPS + children), tmp_path)
    codes = _rows(result, _provider())["ZZ:S1"]["codes"].split("|")
    assert len(codes) == 5
    assert result.dropped["codes_over_cap"] == 16 - 5


def test_gtfs_record_bounds(tmp_path: Path) -> None:
    # a quoted field over two lines is one record, and is read
    stops = STOPS.replace('"Gare, ZZ Nord"', '"Gare, ZZ\nNord"')
    rows = _rows(_build(_provider(), _gtfs(tmp_path, stops=stops), tmp_path), _provider())
    assert rows["ZZ:S1"]["stop_name"] == "Gare, ZZ Nord"
    wide = "route_id,trip_id," + ",".join(f"c{n}" for n in range(250)) + "\nR,T1\n"
    assert _reason(_provider(), _gtfs(tmp_path, name="w.zip", trips=wide), tmp_path) == (
        "input_too_large"
    )
    many = "route_id,trip_id\nR,T1" + "," * 300 + "\n"
    assert _reason(_provider(), _gtfs(tmp_path, name="m.zip", trips=many), tmp_path) == (
        "input_too_large"
    )
    field_over_lines = '"' + ("x" * 999 + "\n") * 100 + '"'  # 100 KB, under the csv field cap
    spanning = "stop_id,stop_name\nZZ:S1," + ",".join([field_over_lines] * 12) + "\n"
    assert _reason(_provider(), _gtfs(tmp_path, name="s.zip", stops=spanning), tmp_path) == (
        "input_too_large"
    )


@pytest.mark.parametrize("constant", ["MEMBER_MAX_BYTES", "ARCHIVE_MAX_BYTES"])
def test_unpacked_bytes_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, constant: str
) -> None:
    monkeypatch.setattr(station_extract_common, constant, 300)
    assert _reason(_provider(), _gtfs(tmp_path), tmp_path) == "input_too_large"
    netex_archive = _zip(
        tmp_path / "n.zip", {"a.xml": _delivery(EPIP_SITE + EPIP_SERVICE + EPIP_TIMETABLE)}
    )
    assert _reason(_provider("netex_epip"), netex_archive, tmp_path) == "input_too_large"


def test_station_and_called_stop_bounds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(station_extract_common, "STATIONS_MAX", 2)
    assert _reason(_provider(), _gtfs(tmp_path), tmp_path) == "too_many_rows"
    netex_archive = _zip(
        tmp_path / "n.zip", {"a.xml": _delivery(EPIP_SITE + EPIP_SERVICE + EPIP_TIMETABLE)}
    )
    monkeypatch.setattr(station_extract_common, "STATIONS_MAX", 1)
    assert _reason(_provider("netex_epip"), netex_archive, tmp_path) == "too_many_rows"
    monkeypatch.setattr(station_extract_common, "STATIONS_MAX", 200_000)
    monkeypatch.setattr(gtfs, "CALLED_STOPS_MAX", 2)
    assert _reason(_provider(), _gtfs(tmp_path, name="c.zip"), tmp_path) == "too_many_rows"


def test_netex_bounds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    good = _delivery(EPIP_SITE + EPIP_SERVICE + EPIP_TIMETABLE)
    provider = _provider("netex_epip")
    # 64 levels is fine, 65 is not
    root_depth = 6  # PublicationDelivery/dataObjects/CompositeFrame/frames/SiteFrame/...
    ok_nested = good.replace(
        "<Name>ZZ Bus</Name>", "<a>" * 40 + "</a>" * 40 + "<Name>ZZ Bus</Name>"
    )
    assert _reason(provider, _zip(tmp_path / "d1.zip", {"a.xml": ok_nested}), tmp_path) == "ok"
    deep = good.replace("<Name>ZZ Bus</Name>", "<a>" * 64 + "</a>" * 64)
    assert root_depth + 64 > netex.XML_DEPTH_MAX
    assert _reason(provider, _zip(tmp_path / "d2.zip", {"a.xml": deep}), tmp_path) == (
        "input_too_large"
    )
    monkeypatch.setattr(netex, "TARGET_ELEMENTS_MAX", 5)
    assert _reason(provider, _zip(tmp_path / "t.zip", {"a.xml": good}), tmp_path) == (
        "input_too_large"
    )
    monkeypatch.setattr(netex, "TARGET_ELEMENTS_MAX", 200_000)
    monkeypatch.setattr(netex, "GROUPS_MAX", 1)
    assert _reason(provider, _zip(tmp_path / "g.zip", {"a.xml": good}), tmp_path) == (
        "input_too_large"
    )


def test_netex_entities_and_subsets(tmp_path: Path) -> None:
    provider = _provider("netex_epip")
    body = _delivery(EPIP_SITE + EPIP_SERVICE + EPIP_TIMETABLE).split("?>", 1)[1]
    internal = '<?xml version="1.0"?><!DOCTYPE PublicationDelivery [<!ENTITY z "ZZ">]>' + body
    assert _reason(provider, _zip(tmp_path / "i.zip", {"a.xml": internal}), tmp_path) == (
        "bad_archive"
    )
    unparsed = (
        '<?xml version="1.0"?><!DOCTYPE PublicationDelivery [<!NOTATION n SYSTEM "x">'
        '<!ENTITY u SYSTEM "x" NDATA n>]>' + body
    )
    assert _reason(provider, _zip(tmp_path / "u.zip", {"a.xml": unparsed}), tmp_path) == (
        "bad_archive"
    )
    # a DTD the document names is never fetched: the document is read without it
    named = '<?xml version="1.0"?><!DOCTYPE PublicationDelivery SYSTEM "http://127.0.0.1:9/x.dtd">'
    assert _reason(provider, _zip(tmp_path / "n.zip", {"a.xml": named + body}), tmp_path) == "ok"


def test_netex_external_entity_handler_refuses() -> None:
    with pytest.raises(station_extract_common.ArchiveError):
        netex._refuse_external("c", None, "x", None)


# ── The one entry point: a child process, one at a time ───────────────────


def _allocate() -> station_extract.ExtractResult:
    """A child target that takes more memory than its limit."""
    hog = bytearray(1024 * MIB)
    raise AssertionError(len(hog))


def _kill_self() -> station_extract.ExtractResult:
    os.kill(os.getpid(), signal.SIGKILL)
    raise AssertionError("unreachable")


def _fail() -> station_extract.ExtractResult:
    raise ValueError("zz")


def _sleep() -> station_extract.ExtractResult:
    time.sleep(30)
    raise AssertionError("unreachable")


def test_ensure_extract_in_a_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(station_extract.settings, "inbox_dir", tmp_path / "inbox")
    archive = _gtfs(tmp_path)
    result = ensure_extract(_provider(), archive, SHA_A, extracted_at=WHEN, session_id="zz-session")
    assert result.rows == 3
    assert result.path_gz.parent == tmp_path / "inbox" / "zz-session" / "_extracts"
    _msmm_check(result.path_gz, result.feed_key)

    def no_child(*_a: Any) -> None:
        raise AssertionError("a cache hit starts no child")

    monkeypatch.setattr(station_extract, "_run_child", no_child)
    again = ensure_extract(_provider(), archive, SHA_A, extracted_at=WHEN, session_id="zz-session")
    assert again.body_sha256 == result.body_sha256


def test_ensure_extract_child_failures_are_extract_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(station_extract.settings, "inbox_dir", tmp_path / "inbox")
    not_zip = tmp_path / "nz.zip"
    not_zip.write_bytes(b"not a zip")
    with pytest.raises(ExtractError) as caught:
        ensure_extract(_provider(), not_zip, SHA_A, extracted_at=WHEN, session_id="s")
    assert caught.value.reason == "bad_archive"
    monkeypatch.setattr(station_extract, "CHILD_MEMORY_BYTES", 512 * MIB)
    with pytest.raises(ExtractError) as caught:
        station_extract._run_child(_allocate, ())
    assert caught.value.reason == "resource_limit"
    with pytest.raises(ExtractError) as caught:
        station_extract._run_child(_kill_self, ())
    assert caught.value.reason == "resource_limit"
    with pytest.raises(ExtractError) as caught:
        station_extract._run_child(_fail, ())
    assert caught.value.reason == "error"
    monkeypatch.setattr(station_extract, "CHILD_WALL_SECONDS", 1)
    with pytest.raises(ExtractError) as caught:
        station_extract._run_child(_sleep, ())
    assert caught.value.reason == "resource_limit"


def test_child_limits_are_set(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []
    monkeypatch.setattr(station_extract.resource, "setrlimit", lambda *a: calls.append(a))
    monkeypatch.setattr(station_extract.os, "nice", lambda n: calls.append(("nice", n)))
    station_extract._limit_self(station_extract._Limits(9, 99, 10))
    assert calls == [
        (station_extract.resource.RLIMIT_AS, (9, 9)),
        (station_extract.resource.RLIMIT_CPU, (99, 129)),
        ("nice", 10),
    ]
    assert station_extract.CHILD_MEMORY_BYTES == 2 * 1024 * MIB
    assert station_extract.CHILD_CPU_SECONDS == 15 * 60
    assert station_extract.CHILD_NICE == 10


def test_one_extraction_at_a_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(station_extract.settings, "inbox_dir", tmp_path / "inbox")
    (tmp_path / "inbox").mkdir(exist_ok=True)
    archive = _gtfs(tmp_path)
    assert station_extract._ONE_HERE.acquire(timeout=1)
    try:
        with pytest.raises(ExtractError) as caught:
            ensure_extract(
                _provider(), archive, SHA_A, extracted_at=WHEN, session_id="s", wait_seconds=0.1
            )
        assert caught.value.reason == "busy"
    finally:
        station_extract._ONE_HERE.release()
    # another process (here: another open file) holds the machine's lock
    with (tmp_path / "inbox" / station_extract._LOCK_NAME).open("a") as other:
        fcntl.flock(other, fcntl.LOCK_EX)
        with pytest.raises(ExtractError) as caught:
            ensure_extract(
                _provider(), archive, SHA_A, extracted_at=WHEN, session_id="s", wait_seconds=1.5
            )
        assert caught.value.reason == "busy"
        fcntl.flock(other, fcntl.LOCK_UN)


def test_ensure_extract_input_refusals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(station_extract.settings, "inbox_dir", tmp_path / "inbox")
    archive = _gtfs(tmp_path)
    provider = _provider(url="")
    with pytest.raises(ExtractError, match="no_feed_key"):
        ensure_extract(provider, archive, SHA_A, extracted_at=WHEN, session_id="s")

    def broken(*_a: Any, **_k: Any) -> None:
        raise OSError("zz")

    monkeypatch.setattr(station_extract, "_cached", broken)
    provider = _provider()
    with pytest.raises(ExtractError, match="error"):
        ensure_extract(provider, archive, SHA_A, extracted_at=WHEN, session_id="s")


def test_build_extract_raises_only_extract_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def oom(*_a: Any) -> None:
        raise MemoryError

    def other(*_a: Any) -> None:
        raise KeyError("zz")

    archive = _gtfs(tmp_path)
    monkeypatch.setattr(station_extract, "_read_stations", oom)
    assert _reason(_provider(), archive, tmp_path) == "resource_limit"
    monkeypatch.setattr(station_extract, "_read_stations", other)
    assert _reason(_provider(), archive, tmp_path) == "error"


# ── The cache heals ───────────────────────────────────────────────────────


@pytest.mark.parametrize("damage", ["cut", "empty", "not_gzip", "header_only"])
def test_damaged_cache_is_made_again(tmp_path: Path, damage: str) -> None:
    archive = _gtfs(tmp_path)
    first = _build(_provider(), archive, tmp_path)
    body = first.path_gz.read_bytes()
    if damage == "cut":
        first.path_gz.write_bytes(body[:10])
    elif damage == "empty":
        first.path_gz.write_bytes(b"")
    elif damage == "not_gzip":
        first.path_gz.write_bytes(b"zz" * 100)
    else:
        first.path_gz.write_bytes(gzip.compress(b"format_version\n", mtime=0))
    again = _build(_provider(), archive, tmp_path)
    assert again.path_gz == first.path_gz
    assert again.rows == 3
    assert again.path_gz.read_bytes() == body


def test_temporary_file_synced_before_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    synced: list[int] = []
    real = os.fsync
    monkeypatch.setattr(station_extract.os, "fsync", lambda fd: (synced.append(fd), real(fd)))
    _build(_provider(), _gtfs(tmp_path), tmp_path)
    assert synced


def test_unwritable_cache_is_an_extract_error(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("zz")
    with pytest.raises(ExtractError) as caught:
        build_extract(
            _provider(), _gtfs(tmp_path), SHA_A, extracted_at=WHEN, cache_dir=blocker / "x"
        )
    assert caught.value.reason == "cache"


def test_sweep_removes_old_leftovers(tmp_path: Path) -> None:
    folder = tmp_path / "x"
    folder.mkdir()
    old_tmp, new_tmp = folder / ".ZZRAIL.a.tmp", folder / ".ZZRAIL.b.tmp"
    old_snap, extract = folder / ".ZZRAIL.c.snap", folder / "ZZRAIL.x.csv.gz"
    for path in (old_tmp, new_tmp, old_snap, extract):
        path.write_text("zz")
    past = time.time() - station_extract.STALE_SECONDS - 10
    for path in (old_tmp, old_snap, extract):
        os.utime(path, (past, past))
    station_extract._sweep(folder)
    assert sorted(p.name for p in folder.iterdir()) == [".ZZRAIL.b.tmp", "ZZRAIL.x.csv.gz"]


# ── The slot and its sha256, from one open file ───────────────────────────


def _session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    inbox = tmp_path / "inbox"
    monkeypatch.setattr(station_extract.settings, "inbox_dir", inbox)
    session = inbox / "zz-session"
    (session / "gtfs").mkdir(parents=True)
    _gtfs(session / "gtfs", name="zzrail.zip")
    feed_fetch.save_state(
        session / "_fetch_state", "provider[ZZRAIL].timetable(gtfs)", {"sha256": SHA_A}
    )
    return session


def test_snapshot_pairs_the_open_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session = _session(tmp_path, monkeypatch)
    slot = session / "gtfs" / "zzrail.zip"
    with station_extract.provider_archive_snapshot("zz-session", _provider()) as found:
        assert found is not None
        snapshot, sha = found
        assert sha == SHA_A
        assert snapshot.parent == session / "_extracts"
        assert snapshot.read_bytes() == slot.read_bytes()
        assert snapshot.stat().st_ino == slot.stat().st_ino
    assert not snapshot.exists()


def test_snapshot_refuses_a_slot_replaced_meanwhile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(tmp_path, monkeypatch)
    slot = session / "gtfs" / "zzrail.zip"
    real = station_extract._sha_of_source

    def replaced(*args: Any) -> str | None:
        newer = slot.with_suffix(".new")
        newer.write_bytes(b"another file")
        newer.replace(slot)  # a refresh lands between the open and the check
        return real(*args)

    monkeypatch.setattr(station_extract, "_sha_of_source", replaced)
    with station_extract.provider_archive_snapshot("zz-session", _provider()) as found:
        assert found is None


def test_snapshot_without_state_or_slot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session = _session(tmp_path, monkeypatch)
    (session / "_fetch_state" / "provider_ZZRAIL_.timetable_gtfs_.json").unlink(missing_ok=True)
    for state in (session / "_fetch_state").iterdir():
        state.unlink()
    with station_extract.provider_archive_snapshot("zz-session", _provider()) as found:
        assert found is None
    other = {**_provider(), "id": "ZZNONE"}
    with station_extract.provider_archive_snapshot("zz-session", other) as found:
        assert found is None
    upload = {"id": "ZZRAIL", "timetable": {"format": "gtfs", "source": "upload"}}
    with station_extract.provider_archive_snapshot(
        "zz-session", upload, upload_sha256=SHA_B
    ) as found:
        assert found is not None
        assert found[1] == SHA_B
    with station_extract.provider_archive_snapshot("zz-session", upload) as found:
        assert found is None


# ── Warming: one at a time, logged, never stopped by one provider ─────────


def test_warm_drops_duplicates_and_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _session(tmp_path, monkeypatch)
    config = {
        "sources": {
            "providers": [
                _provider(),
                {"id": "ZZWAIT", "timetable": {"format": "gtfs", "source": "upload"}},
            ]
        }
    }
    key = ("zz-session", "ZZRAIL", SHA_A)
    station_extract._IN_FLIGHT.add(key)
    try:
        with caplog.at_level(logging.INFO, logger="app.station_extract"):
            out = station_extract.warm_session_extracts("zz-session", config)
    finally:
        station_extract._IN_FLIGHT.discard(key)
    assert out == {"ZZRAIL": "in_flight", "ZZWAIT": "skipped"}
    text = caplog.text
    assert "sid=zz-session provider=ZZRAIL reason=in_flight" in text
    assert "sid=zz-session provider=ZZWAIT reason=skipped" in text


def test_warm_one_provider_never_stops_the_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _session(tmp_path, monkeypatch)
    config = {
        "sources": {
            "providers": [
                {**_provider(), "id": "ZZBAD"},
                {"id": "ZZWAIT", "timetable": {"format": "gtfs", "source": "upload"}},
            ]
        }
    }
    real = station_extract._warm_one

    def flaky(session_id: str, provider: Any, sha: str | None) -> Any:
        if provider["id"] == "ZZBAD":
            raise RuntimeError("zz")
        return real(session_id, provider, sha)

    monkeypatch.setattr(station_extract, "_warm_one", flaky)
    with caplog.at_level(logging.WARNING, logger="app.station_extract"):
        out = station_extract.warm_session_extracts("zz-session", config)
    assert out == {"ZZBAD": "error", "ZZWAIT": "skipped"}
    assert "sid=zz-session provider=ZZBAD error=RuntimeError" in caplog.text


def test_warm_reasons_are_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    session = _session(tmp_path, monkeypatch)
    (session / "gtfs" / "zzrail.zip").write_bytes(b"not a zip")

    def refused(*_a: Any, **_k: Any) -> None:
        raise ExtractError("bad_archive", "zz")

    monkeypatch.setattr(station_extract, "ensure_extract", refused)
    with caplog.at_level(logging.INFO, logger="app.station_extract"):
        out = station_extract.warm_session_extracts(
            "zz-session", {"sources": {"providers": [_provider()]}}
        )
    assert out == {"ZZRAIL": "bad_archive"}
    assert "station extract not written: sid=zz-session provider=ZZRAIL reason=bad_archive" in (
        caplog.text
    )


# ── Contract points of the review ─────────────────────────────────────────


def _netex(tmp_path: Path, name: str, site: str, service: str, timetable: str) -> Path:
    return _zip(tmp_path / name, {"a.xml": _delivery(site + service + timetable)})


RAIL_LINE = '<lines><Line id="ZZ:L"><TransportMode>rail</TransportMode></Line></lines>'


def _one_pattern(points: str, line: str = "ZZ:L") -> str:
    return (
        f'<journeyPatterns><ServiceJourneyPattern id="ZZ:P"><RouteView><LineRef ref="{line}"/>'
        f"</RouteView><pointsInSequence>{points}</pointsInSequence></ServiceJourneyPattern>"
        "</journeyPatterns>"
    )


ONE_JOURNEY = (
    '<TimetableFrame id="T"><vehicleJourneys><ServiceJourney id="J">'
    '<ServiceJourneyPatternRef ref="ZZ:P"/></ServiceJourney></vehicleJourneys></TimetableFrame>'
)


def _place(ident: str, extra: str = "", lon: str = "9.1") -> str:
    return (
        f'<StopPlace id="{ident}"><Name>ZZ {ident}</Name><Centroid><Location>'
        f"<Longitude>{lon}</Longitude><Latitude>45.1</Latitude></Location></Centroid>"
        f"{extra}</StopPlace>"
    )


def test_assignment_with_both_references(tmp_path: Path) -> None:
    site = (
        '<SiteFrame id="S"><stopPlaces>'
        + _place("ZZ:SP:6")
        + _place("ZZ:SP:7", '<quays><Quay id="ZZ:Q:7"/></quays>')
        + _place("ZZ:SP:8")
        + "</stopPlaces></SiteFrame>"
    )
    service = (
        '<ServiceFrame id="F">'
        + RAIL_LINE
        + "<stopAssignments>"
        + _psa(1, "S1", "ZZ:SP:6", "ZZ:Q:undefined")  # Quay unknown: the StopPlace
        + _psa(2, "S2", "ZZ:SP:8", "ZZ:Q:7")  # Quay known: the Quay's StopPlace
        + _psa(3, "S3", "", "ZZ:Q:nowhere")  # nothing to place it at
        + "</stopAssignments>"
        + _one_pattern(_point(1, "S1") + _point(2, "S2") + _point(3, "S3"))
        + "</ServiceFrame>"
    )
    provider = _provider("netex_epip")
    result = _build(provider, _netex(tmp_path, "b.zip", site, service, ONE_JOURNEY), tmp_path)
    assert set(_rows(result, provider)) == {"ZZ:SP:6", "ZZ:SP:7"}


def test_direct_calls_without_a_pattern(tmp_path: Path) -> None:
    site = '<SiteFrame id="S"><stopPlaces>' + _place("ZZ:SP:1") + "</stopPlaces></SiteFrame>"
    service = (
        '<ServiceFrame id="F">'
        + RAIL_LINE
        + "<stopAssignments>"
        + _psa(1, "S1", "ZZ:SP:1")
        + "</stopAssignments></ServiceFrame>"
    )
    journeys = (
        '<TimetableFrame id="T"><vehicleJourneys><ServiceJourney id="J"><LineRef ref="ZZ:L"/>'
        '<calls><Call id="c1"><ScheduledStopPointRef ref="S1"/></Call></calls>'
        "</ServiceJourney></vehicleJourneys></TimetableFrame>"
    )
    provider = _provider("netex_epip")
    rows = _rows(
        _build(provider, _netex(tmp_path, "c.zip", site, service, journeys), tmp_path), provider
    )
    assert rows["ZZ:SP:1"]["n_calls"] == "1"


def test_netex_parents_and_positions(tmp_path: Path) -> None:
    site = (
        '<SiteFrame id="S"><stopPlaces>'
        # a child whose parent the archive does not define is its own station
        + _place("ZZ:SP:5c", '<ParentSiteRef ref="ZZ:SP:5-missing"/>')
        # a root without a position takes its child's
        + '<StopPlace id="ZZ:SP:9"><Name>ZZ Neuf</Name></StopPlace>'
        + _place("ZZ:SP:9c", '<ParentSiteRef ref="ZZ:SP:9"/>', lon="9.9")
        # a cycle of parents ends
        + _place("ZZ:SP:A", '<ParentSiteRef ref="ZZ:SP:B"/>')
        + _place("ZZ:SP:B", '<ParentSiteRef ref="ZZ:SP:A"/>')
        + "</stopPlaces></SiteFrame>"
    )
    service = (
        '<ServiceFrame id="F">'
        + RAIL_LINE
        + "<stopAssignments>"
        + _psa(1, "S1", "ZZ:SP:5c")
        + _psa(2, "S2", "ZZ:SP:9c")
        + _psa(3, "S3", "ZZ:SP:A")
        + "</stopAssignments>"
        + _one_pattern(_point(1, "S1") + _point(2, "S2") + _point(3, "S3"))
        + "</ServiceFrame>"
    )
    provider = _provider("netex_epip")
    rows = _rows(
        _build(provider, _netex(tmp_path, "p.zip", site, service, ONE_JOURNEY), tmp_path), provider
    )
    assert set(rows) == {"ZZ:SP:5c", "ZZ:SP:9", "ZZ:SP:A"}
    assert (rows["ZZ:SP:9"]["lat"], rows["ZZ:SP:9"]["lon"]) == ("45.1000000", "9.9000000")
    assert rows["ZZ:SP:9"]["codes"] == "@id=ZZ:SP:9|StopPlace/@id=ZZ:SP:9c"


def test_railstation_fallback_follows_the_reference(tmp_path: Path) -> None:
    site = (
        '<SiteFrame id="S"><stopPlaces>'
        + _place(
            "ZZ:SP:7",
            "<TransportMode>bus</TransportMode><StopPlaceType>railStation</StopPlaceType>",
        )
        + _place("ZZ:SP:8", "<TransportMode>rail</TransportMode>")
        + _place("ZZ:SP:9", "<StopPlaceType>railStation</StopPlaceType>")
        + _place(
            "ZZ:SP:10",
            "<TransportMode>other</TransportMode><StopPlaceType>railStation</StopPlaceType>",
        )
        + _place("ZZ:SP:11")
        + "</stopPlaces></SiteFrame>"
    )
    service = (
        '<ServiceFrame id="F"><stopAssignments>'
        + "".join(_psa(n, f"S{n}", f"ZZ:SP:{n}") for n in range(7, 12))
        + "</stopAssignments>"
        '<journeyPatterns><ServiceJourneyPattern id="ZZ:P"><pointsInSequence>'
        + "".join(_point(n, f"S{n}") for n in range(7, 12))
        + "</pointsInSequence></ServiceJourneyPattern></journeyPatterns></ServiceFrame>"
    )
    provider = _provider("netex_epip")
    rows = _rows(
        _build(provider, _netex(tmp_path, "r.zip", site, service, ONE_JOURNEY), tmp_path), provider
    )
    assert set(rows) == {"ZZ:SP:8", "ZZ:SP:9", "ZZ:SP:10"}


def test_rail_journey_takes_the_lines_submode(tmp_path: Path) -> None:
    site = '<SiteFrame id="S"><stopPlaces>' + _place("ZZ:SP:1") + "</stopPlaces></SiteFrame>"
    line = (
        '<lines><Line id="ZZ:L"><TransportMode>rail</TransportMode><TransportSubmode>'
        "<RailSubmode>suburbanRailway</RailSubmode></TransportSubmode></Line></lines>"
    )
    service = (
        f'<ServiceFrame id="F">{line}<stopAssignments>'
        + _psa(1, "S1", "ZZ:SP:1")
        + "</stopAssignments>"
        + _one_pattern(_point(1, "S1"))
        + "</ServiceFrame>"
    )
    journeys = (
        '<TimetableFrame id="T"><vehicleJourneys><ServiceJourney id="J">'
        '<TransportMode>rail</TransportMode><LineRef ref="ZZ:L"/>'
        '<ServiceJourneyPatternRef ref="ZZ:P"/></ServiceJourney></vehicleJourneys></TimetableFrame>'
    )
    provider = _provider("netex_epip")
    for name, timetable in (("before.zip", journeys + service), ("after.zip", service + journeys)):
        archive = _zip(tmp_path / name, {"a.xml": _delivery(site + timetable)})
        result = build_extract(
            provider, archive, SHA_A, extracted_at=WHEN, cache_dir=tmp_path / f"{name}-x"
        )
        assert _rows(result, provider)["ZZ:SP:1"]["modes"] == "RAIL_SUBURBAN"


def test_no_stop_place(tmp_path: Path) -> None:
    provider = _provider("netex_epip")
    no_sites = _netex(tmp_path, "n.zip", "", EPIP_SERVICE, EPIP_TIMETABLE)
    assert _reason(provider, no_sites, tmp_path) == "no_stop_place"
    service = (
        EPIP_SERVICE.split("<stopAssignments>")[0] + EPIP_SERVICE.split("</stopAssignments>")[1]
    )
    no_assignments = _netex(tmp_path, "a.zip", EPIP_SITE, service, EPIP_TIMETABLE)
    assert _reason(provider, no_assignments, tmp_path) == "no_stop_place"


def test_gtfs_parents_and_positions(tmp_path: Path) -> None:
    stops = (
        "stop_id,stop_name,stop_lat,stop_lon,location_type,parent_station,stop_code\n"
        "ZZ:S4:P,ZZ Quai orphelin,48.2,2.2,0,ZZ:MISSING,\n"
        "ZZ:S5,ZZ Sans position,,,1,,\n"
        "ZZ:S5:P,ZZ Quai,48.3,2.3,0,ZZ:S5,\n"
        "ZZ:S5:E,ZZ Entree,48.3,2.3,2,ZZ:S5,9901\n"
        "ZZ:S5:N,ZZ Noeud,48.3,2.3,3,ZZ:S5,9902\n"
    )
    stop_times = "trip_id,stop_id\nT1,ZZ:S4:P\nT1,ZZ:S5:P\n"
    archive = _gtfs(tmp_path, stops=stops, stop_times=stop_times)
    rows = _rows(_build(_provider(), archive, tmp_path), _provider())
    assert set(rows) == {"ZZ:S4:P", "ZZ:S5"}
    assert (rows["ZZ:S5"]["lat"], rows["ZZ:S5"]["lon"]) == ("48.3000000", "2.3000000")
    assert rows["ZZ:S5"]["codes"] == "stop_id=ZZ:S5|stop_id=ZZ:S5:P"  # no entrance, no node


def test_stop_key_and_provider_columns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    long_key = "Z" * 201
    stops = STOPS + f"{long_key},ZZ Longue,45,9,0,,\nZZ:\u0085X,ZZ Controle,45,9,0,,\n"
    stop_times = (
        STOP_TIMES + f"T1,08:30:00,08:30:00,{long_key},9,,\nT1,08:40:00,08:40:00,ZZ:\u0085X,10,,\n"
    )
    monkeypatch.setattr(station_extract, "MAX_CALLS", 2)
    provider = {**_provider(), "country_iso": "zz9"}
    result = _build(provider, _gtfs(tmp_path, stops=stops, stop_times=stop_times), tmp_path)
    rows = _rows(result, provider)
    assert set(rows) == {"ZZ:S1", "ZZ:S2", "ZZ:S3"}
    assert result.dropped["stop_key"] == 2
    assert rows["ZZ:S1"]["provider_country_iso"] == ""
    assert rows["ZZ:S1"]["n_calls"] == "2"  # capped


def test_cache_key_includes_feed_key_and_provider_columns(tmp_path: Path) -> None:
    archive = _gtfs(tmp_path)
    names = set()
    for provider in (
        _provider(),
        {**_provider(), "label": "ZZ Rail 2"},
        {**_provider(), "country_iso": "ZY"},
        {**_provider(), "provenance": "community"},
        _provider(url="https://zz.example.invalid/feeds/other.zip"),
    ):
        names.add(_build(provider, archive, tmp_path).path_gz.name)
    assert len(names) == 5


def test_extracted_at_in_utc(tmp_path: Path) -> None:
    when = datetime(2026, 10, 13, 11, 30, tzinfo=timezone(timedelta(hours=2)))
    result = build_extract(
        _provider(), _gtfs(tmp_path), SHA_A, extracted_at=when, cache_dir=tmp_path / "x"
    )
    assert _msmm_check(result.path_gz, result.feed_key)[0]["extracted_at"] == (
        "2026-10-13T09:30:00+00:00"
    )
    assert datetime.fromisoformat("2026-10-13T09:30:00+00:00") == when.astimezone(UTC)
