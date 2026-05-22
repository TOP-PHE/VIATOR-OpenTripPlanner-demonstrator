"""Unit tests for the federated planner's pure helpers (app/journey/federated_planner.py).

The orchestration (`plan_federated`) is network/DB-bound and integration-tested
separately; here we pin the deterministic logic: UIC extraction, hub
intersection, MCT arithmetic, stitch assembly, and dedup/rank.
"""

from __future__ import annotations

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
