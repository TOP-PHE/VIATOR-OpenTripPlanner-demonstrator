"""GTFS fields padded with spaces, trimmed in MOTIS's copy (app/gtfs_motis_clean.py).

Renfe Cercanías (2026-10-29) pads every line of every file to about 150
characters. MOTIS matches column names and ids exactly: calendar.txt had no
`end_date` column and no trip found its route (`10T0001C1  ` in routes.txt,
`10T0001C1` in trips.txt), so the feed served no station, without an error.
"""

from __future__ import annotations

import csv
import io
import zipfile
from pathlib import Path

import pytest

from app import gtfs_motis_clean as gc

PAD = " " * 30

RENFE = {
    "agency.txt": (
        f"agency_id,agency_name,agency_url,agency_timezone{PAD}\n"
        f"1071VC,Renfe Cercanias,https://www.renfe.com,Europe/Madrid{PAD}\n"
    ),
    "calendar.txt": (
        "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
        f"start_date,end_date{PAD}\n1001J,0,0,0,1,0,0,0,20261029,20261029{PAD}\n"
    ),
    "routes.txt": (
        f"route_id,route_short_name,route_long_name,route_type{PAD}\n"
        f'10T0001C1  ,C1  ,"Principe Pio, Madrid     -Aeropuerto-T4   ",2{PAD}\n'
    ),
    "trips.txt": (
        f"route_id,service_id,trip_id,trip_headsign,shape_id{PAD}\n"
        f"10T0001C1,1001J,1001J19799C1,       ,10_C1{PAD}\n"
        "\n"  # a blank line: dropped
    ),
    "shapes.txt": "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\n40_C6,39.98,-0.05,1\n",
    "readme.md": "not GTFS  \n",
}


def _zip(path: Path, members: dict[str, str | bytes]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        for name, body in members.items():
            z.writestr(name, body)
    return path


def _clean(tmp_path: Path, members: dict[str, str | bytes]) -> tuple[gc.CleanStats | None, Path]:
    src = _zip(tmp_path / "in" / "renfe-cerc.zip", members)
    dest = tmp_path / "out" / "renfe-cerc.zip"
    dest.parent.mkdir()
    return gc.clean_zip(src, dest), dest


def _rows(z: zipfile.ZipFile, name: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.TextIOWrapper(z.open(name), "utf-8")))


def test_renfes_padded_fields_are_trimmed(tmp_path: Path) -> None:
    stats, dest = _clean(tmp_path, RENFE)
    assert stats is not None
    with zipfile.ZipFile(dest) as z:
        assert z.namelist() == list(RENFE)
        assert _rows(z, "calendar.txt")[0]["end_date"] == "20261029"  # MOTIS finds the column
        routes, trips = _rows(z, "routes.txt"), _rows(z, "trips.txt")
        assert (
            routes[0]["route_id"] == trips[0]["route_id"] == "10T0001C1"
        )  # the trip finds its route
        assert z.read("routes.txt").decode().splitlines()[1] == (
            '10T0001C1,C1,"Principe Pio, Madrid     -Aeropuerto-T4",2'  # spaces inside stay
        )
        assert trips[0]["trip_headsign"] == ""
        assert z.read("shapes.txt") == RENFE["shapes.txt"].encode()  # not padded: as published
        assert z.read("readme.md") == RENFE["readme.md"].encode()
    assert stats.summary() == (
        "12 padded fields trimmed in 4 files (agency.txt, calendar.txt, routes.txt, trips.txt)"
    )
    assert [p.name for p in dest.parent.iterdir()] == ["renfe-cerc.zip"]  # no temp left


def test_a_feed_without_padding_is_not_copied(tmp_path: Path) -> None:
    stats, dest = _clean(
        tmp_path, {"shapes.txt": RENFE["shapes.txt"], "stops.txt": "stop_id\nS1\n"}
    )
    assert stats is None
    assert not dest.exists()
    assert list(dest.parent.iterdir()) == []


def test_only_the_first_rows_are_probed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gc, "_PROBE_ROWS", 1)
    stats, dest = _clean(tmp_path, {"stops.txt": "stop_id,stop_name\nS1,Nice   \n"})
    assert stats is None
    assert not dest.exists()


def test_bytes_that_are_not_utf8_and_a_bom_pass_through(tmp_path: Path) -> None:
    members: dict[str, str | bytes] = {
        "agency.txt": b"\xef\xbb\xbfagency_id,agency_name   \n1,Renfe Cercan\xedas   \n"
    }
    stats, dest = _clean(tmp_path, members)
    assert stats is not None
    with zipfile.ZipFile(dest) as z:
        assert z.read("agency.txt") == b"agency_id,agency_name\n1,Renfe Cercan\xedas\n"


def test_a_member_csv_cannot_read_is_left_as_published(tmp_path: Path) -> None:
    huge = "x" * (csv.field_size_limit() + 10)  # beyond the probed rows: found while trimming
    bad = "a,b   \n" + "1,2\n" * gc._PROBE_ROWS + f"3,{huge}\n"
    stats, dest = _clean(tmp_path, {"bad.txt": bad, "agency.txt": RENFE["agency.txt"]})
    assert stats is not None
    assert stats.failed == ["bad.txt"]
    assert stats.members == ["agency.txt"]
    assert stats.summary().endswith("; 1 left as published (bad.txt)")
    with zipfile.ZipFile(dest) as z:
        assert z.read("bad.txt") == bad.encode()
    # Alone, it gives no copy at all.
    (tmp_path / "solo").mkdir()
    assert _clean(tmp_path / "solo", {"bad.txt": bad})[0] is None


def test_a_member_unreadable_in_its_first_rows_is_not_probed_as_padded(tmp_path: Path) -> None:
    huge = "x" * (csv.field_size_limit() + 10)
    stats, _ = _clean(tmp_path, {"bad.txt": f"a   ,{huge}\n"})
    assert stats is None


def test_the_summary_names_at_most_four_files() -> None:
    stats = gc.CleanStats(members=[f"f{i}.txt" for i in range(6)], fields=6)
    assert (
        stats.summary()
        == "6 padded fields trimmed in 6 files (f0.txt, f1.txt, f2.txt, f3.txt, ...)"
    )


# ── the MOTIS build reads the trimmed copy ─────────────────────────


def test_the_build_reads_the_trimmed_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app import worker

    graphs = tmp_path / "graphs"
    monkeypatch.setattr(worker.settings, "graph_dir", graphs)
    src = _zip(tmp_path / "inbox" / "eu19" / "gtfs" / "renfe-cerc.zip", RENFE)
    motis_root = graphs / "motis" / "eu19"
    notes: list[str] = []
    path = worker._motis_timetable_path("eu19", src, motis_root, notes)
    assert path == "/graphs/motis/eu19/_gtfs/renfe-cerc.zip"
    assert notes == [
        "gtfs for MOTIS: renfe-cerc.zip rewritten (12 padded fields trimmed in 4 files "
        "(agency.txt, calendar.txt, routes.txt, trips.txt))"
    ]
    calls: list[Path] = []
    monkeypatch.setattr(worker.gtfs_motis_clean, "clean_zip", lambda s, d: calls.append(s))
    assert worker._motis_timetable_path("eu19", src, motis_root, []) == path
    assert calls == []  # unchanged source: no second copy


def test_the_build_reads_the_original_when_the_copy_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import worker

    monkeypatch.setattr(worker.settings, "graph_dir", tmp_path / "graphs")
    src = tmp_path / "inbox" / "eu19" / "gtfs" / "renfe-cerc.zip"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"not a zip")
    notes: list[str] = []
    motis_root = tmp_path / "graphs" / "motis" / "eu19"
    assert worker._motis_timetable_path("eu19", src, motis_root, notes) == (
        "/inbox/eu19/gtfs/renfe-cerc.zip"
    )
    assert notes == ["gtfs for MOTIS: renfe-cerc.zip not rewritten (failed), original used"]
