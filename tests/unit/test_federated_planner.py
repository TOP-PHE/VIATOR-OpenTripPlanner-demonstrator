"""Unit tests for the federated planner's pure helpers (app/journey/federated_planner.py).

The orchestration (`plan_federated`) is network/DB-bound and integration-tested
separately; here we pin the deterministic logic: UIC extraction, hub
intersection, MCT arithmetic, stitch assembly, and dedup/rank.
"""

from __future__ import annotations

import types
from datetime import UTC, datetime

from app.journey import federated_planner as fp
from app.journey.signature import transit_fingerprint


def _leg(frm: str, to: str, route: str, dep: str, arr: str, mode: str = "RAIL") -> dict:
    return {
        "mode": mode,
        "from_stop_id": frm,
        "to_stop_id": to,
        "from_lat": 0.0,
        "from_lon": 0.0,
        "to_lat": 0.0,
        "to_lon": 0.0,
        "route_short_name": route,
        "departure": dep,
        "arrival": arr,
    }


def _trip(dep: str, arr: str, transfers: int, legs: list[dict], modes: str = "RAIL,WALK") -> dict:
    return {
        "departure_at": dep,
        "arrival_at": arr,
        "num_transfers": transfers,
        "modes": modes,
        "legs": legs,
    }


# ──────────────────────── served_uics ────────────────────────


def test_served_uics_parses_and_skips_non_uic():
    stops = [
        ("SBB:8500010", 47.5, 7.6),  # CH 7-digit
        ("StopPoint:OCETrain-87686006", 48.8, 2.3),  # SNCF 8-digit → 7-digit UIC
        ("IDFM:monomodalStopPlace:43098", 48.8, 2.3),  # no UIC → skipped
        (None, 0.0, 0.0),  # no id → skipped
    ]
    assert fp.served_uics(stops) == {"8500010", "8768600"}


def test_served_uics_empty():
    assert fp.served_uics([]) == set()


# ──────────────────────── connection_hubs ────────────────────────


def test_connection_hubs_is_intersection():
    assert fp.connection_hubs({"a", "b", "c"}, {"b", "c", "d"}) == {"b", "c"}
    assert fp.connection_hubs({"a"}, {"b"}) == set()


# ──────────────────────── rank_hubs (proximity) ────────────────────────


# Paris Gare de Lyon, Basel SBB, Bern, Zürich HB, Prague hl.n. — real-ish coords.
_PARIS = (48.844, 2.374)
_FRIBOURG = (46.803, 7.151)
_BASEL = (47.547, 7.589)  # on the Paris->Fribourg line
_ZURICH = (47.378, 8.540)  # east of the line
_PRAGUE = (50.083, 14.435)  # far off-route (the 5457076-style junk hub)
_COORDS = {
    "paris": _PARIS,
    "frib": _FRIBOURG,
    "8500010": _BASEL,
    "zurich": _ZURICH,
    "prague": _PRAGUE,
}


def test_haversine_km_known_distance():
    # Paris -> Basel great-circle is ~412 km; allow a few km of slack.
    d = fp._haversine_km(*_PARIS, *_BASEL)
    assert 405 < d < 420


def test_rank_hubs_orders_by_detour_basel_first():
    # Basel sits on the Paris->Fribourg line; Zürich detours east; Prague is
    # way off-route. Proximity ranking must surface Basel first and Prague last.
    out = fp.rank_hubs({"8500010", "zurich", "prague"}, _COORDS, "paris", "frib")
    assert out[0] == "8500010"
    assert out[-1] == "prague"


def test_rank_hubs_drops_hubs_without_coords():
    out = fp.rank_hubs({"8500010", "no-coords"}, _COORDS, "paris", "frib")
    assert out == ["8500010"]


def test_rank_hubs_empty_when_endpoints_lack_coords():
    assert fp.rank_hubs({"8500010"}, _COORDS, "missing-origin", "frib") == []
    assert fp.rank_hubs({"8500010"}, _COORDS, "paris", "missing-dest") == []


def test_rank_hubs_deterministic_tie_break_on_uic():
    # Two hubs at the same point ⇒ identical detour ⇒ sorted by UIC string.
    coords = {"o": (0.0, 0.0), "d": (0.0, 2.0), "b": (0.0, 1.0), "a": (0.0, 1.0)}
    assert fp.rank_hubs({"a", "b"}, coords, "o", "d") == ["a", "b"]


# ──────────────────────── earliest_next_departure ────────────────────────


def test_earliest_next_departure_adds_mct_utc():
    assert fp.earliest_next_departure("2026-05-22T10:00:00Z", 600) == datetime(
        2026, 5, 22, 10, 10, tzinfo=UTC
    )


def test_earliest_next_departure_normalises_offset():
    # 10:00+02:00 == 08:00Z; +5 min → 08:05Z
    assert fp.earliest_next_departure("2026-05-22T10:00:00+02:00", 300) == datetime(
        2026, 5, 22, 8, 5, tzinfo=UTC
    )


def test_earliest_next_departure_default_mct():
    assert fp.earliest_next_departure("2026-05-22T10:00:00Z") == datetime(
        2026, 5, 22, 10, 10, tzinfo=UTC
    )  # DEFAULT_MCT_SECONDS == 600


# ──────────────────────── assemble_stitch ────────────────────────


def test_assemble_stitch_two_legs():
    t1 = _trip(
        "2026-05-22T08:00:00Z",
        "2026-05-22T11:00:00Z",
        1,  # one internal transfer on the spine leg
        [_leg("87271007", "8500010", "TGV", "2026-05-22T08:00:00Z", "2026-05-22T11:00:00Z")],
        modes="RAIL,WALK",
    )
    t2 = _trip(
        "2026-05-22T11:15:00Z",
        "2026-05-22T12:00:00Z",
        0,
        [_leg("8500010", "8504200", "IC", "2026-05-22T11:15:00Z", "2026-05-22T12:00:00Z")],
        modes="RAIL",
    )
    s = fp.assemble_stitch([t1, t2], via_hubs=["8500010"], session_ids=["corr", "ch"])
    assert s["departure_at"] == "2026-05-22T08:00:00Z"
    assert s["arrival_at"] == "2026-05-22T12:00:00Z"
    assert s["duration_seconds"] == 4 * 3600  # 08:00 → 12:00, includes the transfer wait
    assert s["num_transfers"] == 1 + 0 + 1  # internal + one per stitch
    assert len(s["legs"]) == 2
    assert s["modes"] == "RAIL,WALK"
    assert s["via_hubs"] == ["8500010"]
    assert s["stitched_from_sessions"] == ["corr", "ch"]
    assert s["federated"] is True


# ──────────────────────── dedup_and_rank ────────────────────────


def _stitch(arr: str, route: str, dur_h: int = 4, transfers: int = 1) -> dict:
    dep = "2026-05-22T08:00:00Z"
    return {
        "departure_at": dep,
        "arrival_at": arr,
        "duration_seconds": dur_h * 3600,
        "num_transfers": transfers,
        "legs": [_leg("87271007", "8500010", route, dep, arr)],
    }


def test_dedup_and_rank_orders_by_arrival():
    late = _stitch("2026-05-22T13:00:00Z", "A")
    early = _stitch("2026-05-22T12:00:00Z", "B")
    out = fp.dedup_and_rank([late, early])
    assert [s["arrival_at"] for s in out] == [
        "2026-05-22T12:00:00Z",
        "2026-05-22T13:00:00Z",
    ]


def test_dedup_and_rank_collapses_identical_itineraries():
    a = _stitch("2026-05-22T12:00:00Z", "TGV")
    b = _stitch("2026-05-22T12:00:00Z", "TGV")  # same legs ⇒ same fingerprint
    out = fp.dedup_and_rank([a, b])
    assert len(out) == 1


def test_dedup_and_rank_drops_existing_fingerprint():
    s = _stitch("2026-05-22T12:00:00Z", "TGV")
    fp_existing = transit_fingerprint(s["legs"])
    out = fp.dedup_and_rank([s], existing_fingerprints={fp_existing})
    assert out == []


def test_dedup_and_rank_respects_limit():
    stitches = [_stitch(f"2026-05-22T1{i}:00:00Z", f"R{i}") for i in range(8)]
    out = fp.dedup_and_rank(stitches, limit=3)
    assert len(out) == 3
    # kept the three earliest arrivals
    assert [s["arrival_at"] for s in out] == [
        "2026-05-22T10:00:00Z",
        "2026-05-22T11:00:00Z",
        "2026-05-22T12:00:00Z",
    ]


# ──────────────────────── plan_federated (orchestration, mocked IO) ───────────


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *_a, **_k):
        return self

    def all(self):
        return self._rows


class _FakeDb:
    def __init__(self, rows):
        self._rows = rows

    def query(self, _model):
        return _FakeQuery(self._rows)


def _otp_leg(frm, to, route, dep, arr):
    return {
        "mode": "RAIL",
        "from_stop_id": f"X:{frm}",
        "to_stop_id": f"X:{to}",
        "from_lat": 0.0,
        "from_lon": 0.0,
        "to_lat": 0.0,
        "to_lon": 0.0,
        "route_short_name": route,
        "departure": dep,
        "arrival": arr,
    }


async def test_plan_federated_stitches_paris_fribourg(monkeypatch):
    origin, hub, dest = "8768600", "8500010", "8504200"  # Paris, Basel, Fribourg
    corr = types.SimpleNamespace(id="nap-eu-corridors")
    ch = types.SimpleNamespace(id="nap-ch-rail")

    served = {"nap-eu-corridors": {origin, hub}, "nap-ch-rail": {hub, dest}}
    monkeypatch.setattr(fp, "_session_served_uics", lambda s: served[s.id])

    rows = [
        types.SimpleNamespace(uic=origin, latitude=48.84, longitude=2.37),
        types.SimpleNamespace(uic=hub, latitude=47.55, longitude=7.59),
        types.SimpleNamespace(uic=dest, latitude=46.80, longitude=7.15),
    ]

    from app.journey import otp_client

    async def _fake_fetch_plan(*, session_id, **_kw):
        if session_id == "nap-eu-corridors":
            return (
                {},
                [
                    {
                        "departure_at": "2026-05-22T08:00:00Z",
                        "arrival_at": "2026-05-22T11:00:00Z",
                        "num_transfers": 0,
                        "modes": "RAIL",
                        "legs": [
                            _otp_leg(
                                origin, hub, "TGV", "2026-05-22T08:00:00Z", "2026-05-22T11:00:00Z"
                            )
                        ],
                    }
                ],
            )
        if session_id == "nap-ch-rail":
            return (
                {},
                [
                    {
                        "departure_at": "2026-05-22T11:15:00Z",
                        "arrival_at": "2026-05-22T12:00:00Z",
                        "num_transfers": 0,
                        "modes": "RAIL",
                        "legs": [
                            _otp_leg(
                                hub, dest, "IC", "2026-05-22T11:15:00Z", "2026-05-22T12:00:00Z"
                            )
                        ],
                    }
                ],
            )
        return ({}, [])

    monkeypatch.setattr(otp_client, "fetch_plan", _fake_fetch_plan)

    out = await fp.plan_federated(
        _FakeDb(rows),
        origin_uic=origin,
        dest_uic=dest,
        when=datetime(2026, 5, 22, 8, 0, tzinfo=UTC),
        sessions=[corr, ch],
        timeout_ms=5000,
    )
    assert len(out) == 1
    s = out[0]
    assert s["departure_at"] == "2026-05-22T08:00:00Z"
    assert s["arrival_at"] == "2026-05-22T12:00:00Z"
    assert s["via_hubs"] == [hub]
    assert s["stitched_from_sessions"] == ["nap-eu-corridors", "nap-ch-rail"]
    assert len(s["legs"]) == 2
    assert s["federated"] is True


async def test_plan_federated_no_uic_returns_empty():
    out = await fp.plan_federated(
        _FakeDb([]),
        origin_uic=None,
        dest_uic="8500010",
        when=datetime.now(UTC),
        sessions=[],
        timeout_ms=1000,
    )
    assert out == []


async def test_plan_federated_no_shared_hub_returns_empty(monkeypatch):
    a = types.SimpleNamespace(id="a")
    b = types.SimpleNamespace(id="b")
    served = {"a": {"1", "2"}, "b": {"3", "4"}}
    monkeypatch.setattr(fp, "_session_served_uics", lambda s: served[s.id])
    out = await fp.plan_federated(
        _FakeDb([]),
        origin_uic="1",  # served by a
        dest_uic="3",  # served by b
        when=datetime.now(UTC),
        sessions=[a, b],
        timeout_ms=1000,
    )
    assert out == []  # a and b share no hub


# ──────────────────────── served-uics IO + cache ────────────────────────


def _write_gtfs(gtfs_dir, stop_ids):
    import csv  # noqa: F401  (kept local; stdlib)
    import io
    import zipfile

    gtfs_dir.mkdir(parents=True, exist_ok=True)
    rows = "".join(f"{s},Stop {s},47.0,7.0\n" for s in stop_ids)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("stops.txt", "stop_id,stop_name,stop_lat,stop_lon\n" + rows)
    (gtfs_dir / "feed.zip").write_bytes(buf.getvalue())


def test_session_served_uics_reads_caches_and_invalidates(tmp_path, monkeypatch):
    from app.settings import settings

    sid = "ch-rail-uics-test"
    gtfs_dir = tmp_path / sid / "gtfs"
    _write_gtfs(gtfs_dir, ["8500010", "8504200", "IDFM:no-uic"])
    monkeypatch.setattr(settings, "inbox_dir", tmp_path)
    fp.invalidate_served_uics_cache()

    session = types.SimpleNamespace(id=sid)
    assert fp._session_served_uics(session) == {"8500010", "8504200"}  # junk id dropped

    # cache hit: deleting the feed doesn't change the cached answer
    (gtfs_dir / "feed.zip").unlink()
    assert fp._session_served_uics(session) == {"8500010", "8504200"}

    # invalidate → re-read (feed now gone ⇒ empty)
    fp.invalidate_served_uics_cache(sid)
    assert fp._session_served_uics(session) == set()
    fp.invalidate_served_uics_cache()  # cleanup (don't leak into other tests)


def test_read_stop_ids_missing_dir(tmp_path):
    assert fp._read_stop_ids(tmp_path / "does-not-exist") == []
