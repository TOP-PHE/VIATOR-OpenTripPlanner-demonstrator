"""Unit tests for the federated planner's pure helpers (app/journey/federated_planner.py).

The orchestration (`plan_federated`) is network/DB-bound and integration-tested
separately; here we pin the deterministic logic: UIC extraction, hub
intersection, MCT arithmetic, stitch assembly, and dedup/rank.
"""

from __future__ import annotations

import logging
import types
from datetime import UTC, datetime

import pytest

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


def test_rank_hubs_prefers_destination_country_over_lower_detour():
    # Paris (FR) -> Fribourg (CH). Besancon (FR, 87) is geometrically closer to
    # the straight line (lower detour) than Basel (CH, 85), but cutting in France
    # forces the Swiss spoke to backtrack — so the CH hub must rank first.
    coords = {
        "8768600": (48.844, 2.374),  # Paris (FR)
        "8504200": (46.803, 7.151),  # Fribourg (CH)
        "8730086": (47.308, 5.954),  # Besancon-Franche-Comte TGV (FR) — lower detour
        "8500010": (47.547, 7.589),  # Basel SBB (CH) — higher detour
    }
    out = fp.rank_hubs({"8730086", "8500010"}, coords, "8768600", "8504200")
    assert out == ["8500010", "8730086"]  # CH gateway first despite the longer detour


def test_rank_hubs_within_dest_country_orders_by_detour():
    # Both hubs are Swiss (85): the closer-to-route one wins.
    coords = {
        "8768600": (48.844, 2.374),  # Paris (FR)
        "8504200": (46.803, 7.151),  # Fribourg (CH)
        "8507000": (46.949, 7.439),  # Bern (CH) — ~30 km from Fribourg
        "8501008": (46.210, 6.142),  # Geneve (CH) — farther
    }
    out = fp.rank_hubs({"8501008", "8507000"}, coords, "8768600", "8504200")
    assert out == ["8507000", "8501008"]  # Bern (nearer the line) before Geneve


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


def test_assemble_stitch_drops_phantom_hub_walks():
    # Each per-leg OTP search wraps its ride in access/egress walks. Once
    # stitched, the egress of leg-1 and the access of leg-2 are the two halves of
    # one platform change at the hub — they must be dropped, but the genuine
    # origin-access and destination-egress walks kept.
    def _walk(frm, to, dep, arr):
        return _leg(frm, to, "", dep, arr, mode="WALK")

    t1 = _trip(
        "2026-05-22T07:49:00Z",
        "2026-05-22T11:41:00Z",
        0,
        [
            _walk("ORIG", "GDL", "2026-05-22T07:49:00Z", "2026-05-22T07:55:00Z"),  # kept
            _leg("GDL", "8501120", "TGV", "2026-05-22T07:56:00Z", "2026-05-22T11:39:00Z"),
            _walk("8501120", "DEST", "2026-05-22T11:39:00Z", "2026-05-22T11:41:00Z"),  # dropped
        ],
        modes="WALK,RAIL",
    )
    t2 = _trip(
        "2026-05-22T12:13:00Z",
        "2026-05-22T13:08:00Z",
        0,
        [
            _walk("ORIG", "8501120", "2026-05-22T12:13:00Z", "2026-05-22T12:17:00Z"),  # dropped
            _leg("8501120", "8504200", "IC", "2026-05-22T12:17:00Z", "2026-05-22T13:02:00Z"),
            _walk("8504200", "DEST", "2026-05-22T13:02:00Z", "2026-05-22T13:08:00Z"),  # kept
        ],
        modes="WALK,RAIL",
    )
    s = fp.assemble_stitch([t1, t2], via_hubs=["8501120"], session_ids=["fr", "ch"])
    assert [leg["mode"] for leg in s["legs"]] == ["WALK", "RAIL", "RAIL", "WALK"]
    assert s["departure_at"] == "2026-05-22T07:49:00Z"  # endpoints unchanged
    assert s["arrival_at"] == "2026-05-22T13:08:00Z"
    assert s["num_transfers"] == 1  # 0 internal + 1 per stitch (the hub change)


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
    # Equal duration + transfers ⇒ same generalized time ⇒ earliest arrival wins.
    late = _stitch("2026-05-22T13:00:00Z", "A")
    early = _stitch("2026-05-22T12:00:00Z", "B")
    out = fp.dedup_and_rank([late, early])
    assert [s["arrival_at"] for s in out] == [
        "2026-05-22T12:00:00Z",
        "2026-05-22T13:00:00Z",
    ]


def test_dedup_and_rank_prefers_fewer_changes_over_earliest_arrival():
    # A clean 1-change journey arriving a little later must beat a 3-change slog
    # that merely arrives earlier — that was the "6h29/3-transfer ranked above
    # 5h19/1-transfer" bug. dur+penalty: clean 5h17+20m=5h37 < slog 6h30+60m=7h30.
    slog = _stitch("2026-05-22T12:30:00Z", "SLOG", dur_h=6, transfers=3)
    clean = _stitch("2026-05-22T13:00:00Z", "CLEAN", dur_h=5, transfers=1)
    out = fp.dedup_and_rank([slog, clean])
    assert [s["num_transfers"] for s in out] == [1, 3]  # clean first despite later arrival


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


# ──────────────────────── feed-id / stop-id helpers ────────────────────────


def test_primary_feed_id_reads_first_provider():
    s = types.SimpleNamespace(config={"sources": {"providers": [{"id": "SBB"}, {"id": "DB"}]}})
    assert fp._primary_feed_id(s) == "SBB"


def test_primary_feed_id_none_when_missing():
    assert fp._primary_feed_id(types.SimpleNamespace(config={})) is None
    assert fp._primary_feed_id(types.SimpleNamespace()) is None  # no config attr at all


def test_stop_id_builds_namespaced_or_none():
    assert fp._stop_id("SBB", "8500010") == "SBB:8500010"
    assert fp._stop_id(None, "8500010") is None


async def test_plan_federated_forwards_namespaced_stop_ids(monkeypatch):
    """Each leg should route by `<feedId>:<uic>` for its own session's feed."""
    origin, hub, dest = "8768600", "8500010", "8504200"
    corr = types.SimpleNamespace(id="corr", config={"sources": {"providers": [{"id": "SNCF"}]}})
    ch = types.SimpleNamespace(id="ch", config={"sources": {"providers": [{"id": "SBB"}]}})
    monkeypatch.setattr(
        fp, "_session_served_uics", lambda s: {"corr": {origin, hub}, "ch": {hub, dest}}[s.id]
    )
    rows = [
        types.SimpleNamespace(uic=origin, latitude=48.84, longitude=2.37),
        types.SimpleNamespace(uic=hub, latitude=47.55, longitude=7.59),
        types.SimpleNamespace(uic=dest, latitude=46.80, longitude=7.15),
    ]
    seen: list[tuple[str, str | None, str | None]] = []

    from app.journey import otp_client

    async def _fake(*, session_id, from_stop_id=None, to_stop_id=None, **_kw):
        seen.append((session_id, from_stop_id, to_stop_id))
        return (
            {},
            [
                {
                    "departure_at": "2026-05-22T08:00:00Z",
                    "arrival_at": "2026-05-22T09:00:00Z",
                    "num_transfers": 0,
                    "modes": "RAIL",
                    "legs": [
                        _otp_leg("a", "b", "R", "2026-05-22T08:00:00Z", "2026-05-22T09:00:00Z")
                    ],
                }
            ],
        )

    monkeypatch.setattr(otp_client, "fetch_plan", _fake)
    await fp.plan_federated(
        _FakeDb(rows),
        origin_uic=origin,
        dest_uic=dest,
        when=datetime(2026, 5, 22, 8, 0, tzinfo=UTC),
        sessions=[corr, ch],
        timeout_ms=5000,
    )
    assert ("corr", "SNCF:8768600", "SNCF:8500010") in seen  # leg1 on corridors feed
    assert ("ch", "SBB:8500010", "SBB:8504200") in seen  # leg2 on Swiss feed


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


# ───────────── endpoint positions: master_stations, then the request (#331) ─────────────
# Invented codes and positions only. Since MSMM step 2 the typeahead can send the
# station module's code, which master_stations may not hold; the request's
# validated lat/lon then stands in for the missing position.

_ORIGIN, _HUB, _DEST = "9900001", "9900002", "9900003"
_MASTER_ORIGIN = (45.10, 3.10)
_MASTER_HUB = (45.50, 3.50)
_MASTER_DEST = (45.90, 3.90)
_REQUEST_ORIGIN = (45.11, 3.11)
_REQUEST_DEST = (45.91, 3.91)


def _row(code, position):
    lat, lon = position if position else (None, None)
    return types.SimpleNamespace(uic=code, latitude=lat, longitude=lon)


def _zz_sessions(monkeypatch):
    """Two invented sessions sharing the invented hub; origin in one, dest in the other."""
    so = types.SimpleNamespace(id="zz-origin", config={"sources": {"providers": [{"id": "ZZA"}]}})
    sd = types.SimpleNamespace(id="zz-dest", config={"sources": {"providers": [{"id": "ZZB"}]}})
    served = {"zz-origin": {_ORIGIN, _HUB}, "zz-dest": {_HUB, _DEST}}
    monkeypatch.setattr(fp, "_session_served_uics", lambda s: served[s.id])
    return [so, sd]


def _recording_otp(monkeypatch):
    """Fake OTP: one trip per leg; records each call's session, positions and stop ids."""
    from app.journey import otp_client

    calls: list[dict] = []

    async def _fake(*, session_id, from_lat, from_lon, to_lat, to_lon, **kw):
        calls.append(
            {
                "session": session_id,
                "from": (from_lat, from_lon),
                "to": (to_lat, to_lon),
                "from_stop_id": kw.get("from_stop_id"),
                "to_stop_id": kw.get("to_stop_id"),
            }
        )
        if session_id == "zz-origin":
            dep, arr, frm, to = "2026-05-22T08:00:00Z", "2026-05-22T09:00:00Z", _ORIGIN, _HUB
        else:
            dep, arr, frm, to = "2026-05-22T09:20:00Z", "2026-05-22T10:00:00Z", _HUB, _DEST
        trip = {
            "departure_at": dep,
            "arrival_at": arr,
            "num_transfers": 0,
            "modes": "RAIL",
            "legs": [_otp_leg(frm, to, "ZZ1", dep, arr)],
        }
        return ({}, [trip])

    monkeypatch.setattr(otp_client, "fetch_plan", _fake)
    return calls


async def _plan(rows, sessions, **kw):
    return await fp.plan_federated(
        _FakeDb(rows),
        origin_uic=_ORIGIN,
        dest_uic=_DEST,
        when=datetime(2026, 5, 22, 8, 0, tzinfo=UTC),
        sessions=sessions,
        timeout_ms=5000,
        **kw,
    )


def test_fill_endpoint_positions_only_fills_missing_codes():
    coords = {_ORIGIN: _MASTER_ORIGIN}
    used = fp._fill_endpoint_positions(coords, [(_ORIGIN, _REQUEST_ORIGIN), (_DEST, _REQUEST_DEST)])
    assert used is True
    assert coords == {_ORIGIN: _MASTER_ORIGIN, _DEST: _REQUEST_DEST}  # found code kept


def test_fill_endpoint_positions_without_request_positions_changes_nothing():
    coords = {_ORIGIN: _MASTER_ORIGIN}
    assert fp._fill_endpoint_positions(coords, [(_ORIGIN, None), (_DEST, None)]) is False
    assert coords == {_ORIGIN: _MASTER_ORIGIN}


async def test_plan_federated_uses_request_positions_for_codes_master_lacks(monkeypatch):
    """No master_stations row for either endpoint: the request's positions are used."""
    sessions = _zz_sessions(monkeypatch)
    calls = _recording_otp(monkeypatch)
    out = await _plan(
        [_row(_HUB, _MASTER_HUB)],
        sessions,
        origin_position=_REQUEST_ORIGIN,
        dest_position=_REQUEST_DEST,
    )
    assert len(out) == 1
    assert out[0]["via_hubs"] == [_HUB]
    assert out[0]["stitched_from_sessions"] == ["zz-origin", "zz-dest"]
    leg1, leg2 = calls
    assert leg1["from"] == _REQUEST_ORIGIN
    assert leg1["to"] == _MASTER_HUB
    assert leg2["from"] == _MASTER_HUB
    assert leg2["to"] == _REQUEST_DEST
    # the code still routes by stop id; OTP falls back to the position if unknown
    assert leg1["from_stop_id"] == f"ZZA:{_ORIGIN}"
    assert leg2["to_stop_id"] == f"ZZB:{_DEST}"


async def test_plan_federated_uses_request_position_when_master_row_has_none(monkeypatch):
    """A master_stations row without a position counts as missing, end by end."""
    sessions = _zz_sessions(monkeypatch)
    calls = _recording_otp(monkeypatch)
    rows = [_row(_ORIGIN, None), _row(_HUB, _MASTER_HUB), _row(_DEST, _MASTER_DEST)]
    out = await _plan(rows, sessions, origin_position=_REQUEST_ORIGIN, dest_position=_REQUEST_DEST)
    assert len(out) == 1
    leg1, leg2 = calls
    assert leg1["from"] == _REQUEST_ORIGIN  # no master position: the request's
    assert leg2["to"] == _MASTER_DEST  # master position: never the request's


async def test_plan_federated_found_codes_behave_exactly_as_before(monkeypatch):
    """Codes master_stations places: same OTP calls and same result, request
    positions passed or not."""
    rows = [_row(_ORIGIN, _MASTER_ORIGIN), _row(_HUB, _MASTER_HUB), _row(_DEST, _MASTER_DEST)]

    sessions = _zz_sessions(monkeypatch)
    calls_before = _recording_otp(monkeypatch)
    out_before = await _plan(rows, sessions)

    calls_after = _recording_otp(monkeypatch)
    out_after = await _plan(
        rows, sessions, origin_position=_REQUEST_ORIGIN, dest_position=_REQUEST_DEST
    )

    assert out_after == out_before
    assert len(out_before) == 1
    assert calls_after == calls_before
    assert calls_before[0]["from"] == _MASTER_ORIGIN
    assert calls_before[1]["to"] == _MASTER_DEST


async def test_plan_federated_no_position_anywhere_returns_empty(monkeypatch):
    """Check 2 still ends the try when neither master_stations nor the request
    gives an endpoint a position; OTP is never asked."""
    sessions = _zz_sessions(monkeypatch)
    calls = _recording_otp(monkeypatch)
    out = await _plan([_row(_HUB, _MASTER_HUB)], sessions, origin_position=_REQUEST_ORIGIN)
    assert out == []
    assert calls == []


async def test_plan_federated_code_no_feed_serves_returns_empty(monkeypatch):
    """Check 1 stays: a code no session's feed serves ends the try, even with
    positions in the request (they cannot say which sessions serve the end)."""
    sessions = _zz_sessions(monkeypatch)
    calls = _recording_otp(monkeypatch)
    out = await fp.plan_federated(
        _FakeDb([_row(_HUB, _MASTER_HUB)]),
        origin_uic="9900009",  # served by no session
        dest_uic=_DEST,
        when=datetime(2026, 5, 22, 8, 0, tzinfo=UTC),
        sessions=sessions,
        timeout_ms=5000,
        origin_position=_REQUEST_ORIGIN,
        dest_position=_REQUEST_DEST,
    )
    assert out == []
    assert calls == []


# ───────────── how a try ended: counter and log line (#331) ─────────────


def _tries(outcome):
    from prometheus_client.registry import REGISTRY

    value = REGISTRY.get_sample_value("viator_federated_planner_tries_total", {"outcome": outcome})
    assert value is not None, f"series {outcome} should exist from import time"
    return value


def _counts():
    from app.metrics import FEDERATED_PLANNER_OUTCOMES

    return {o: _tries(o) for o in FEDERATED_PLANNER_OUTCOMES}


def _delta(before):
    after = _counts()
    return {o: after[o] - before[o] for o in after if after[o] != before[o]}


@pytest.fixture
def live_log(monkeypatch):
    # alembic's fileConfig (run by the integration tests) disables every logger
    # that exists at that moment; this one must be live for caplog.
    monkeypatch.setattr(fp.log, "disabled", False)


def _assert_log_has_no_code(caplog):
    text = " ".join(r.getMessage() for r in caplog.records)
    for code in (_ORIGIN, _HUB, _DEST, "9900009"):
        assert code not in text


@pytest.mark.parametrize(
    ("origin_lacks", "dest_lacks", "word"),
    [(True, False, "origin"), (False, True, "destination"), (True, True, "both")],
)
def test_ends_lacking_words(origin_lacks, dest_lacks, word):
    assert fp._ends_lacking(origin_lacks, dest_lacks) == word


async def test_counter_check1_code_not_served(monkeypatch, caplog, live_log):
    sessions = _zz_sessions(monkeypatch)
    _recording_otp(monkeypatch)
    before = _counts()
    with caplog.at_level(logging.INFO, logger=fp.__name__):
        await fp.plan_federated(
            _FakeDb([]),
            origin_uic=_ORIGIN,
            dest_uic="9900009",  # served by no session
            when=datetime(2026, 5, 22, 8, 0, tzinfo=UTC),
            sessions=sessions,
            timeout_ms=5000,
        )
    assert _delta(before) == {"code_not_served": 1.0}
    assert "federated try ended: code_not_served (destination)" in caplog.text
    _assert_log_has_no_code(caplog)


async def test_counter_check2_position_missing(monkeypatch, caplog, live_log):
    sessions = _zz_sessions(monkeypatch)
    _recording_otp(monkeypatch)
    before = _counts()
    with caplog.at_level(logging.INFO, logger=fp.__name__):
        await _plan([_row(_HUB, _MASTER_HUB)], sessions)
    assert _delta(before) == {"position_missing": 1.0}
    assert "federated try ended: position_missing (both)" in caplog.text
    _assert_log_has_no_code(caplog)


async def test_counter_no_shared_hub(monkeypatch, caplog, live_log):
    a = types.SimpleNamespace(id="zz-a")
    b = types.SimpleNamespace(id="zz-b")
    served = {"zz-a": {_ORIGIN}, "zz-b": {_DEST}}
    monkeypatch.setattr(fp, "_session_served_uics", lambda s: served[s.id])
    before = _counts()
    with caplog.at_level(logging.INFO, logger=fp.__name__):
        await _plan([], [a, b])
    assert _delta(before) == {"no_shared_hub": 1.0}
    assert "federated try ended" not in caplog.text  # only checks 1 and 2 log


async def test_counter_planned_with_request_positions(monkeypatch, caplog, live_log):
    sessions = _zz_sessions(monkeypatch)
    _recording_otp(monkeypatch)
    before = _counts()
    with caplog.at_level(logging.INFO, logger=fp.__name__):
        await _plan(
            [_row(_HUB, _MASTER_HUB), _row(_DEST, _MASTER_DEST)],
            sessions,
            origin_position=_REQUEST_ORIGIN,
            dest_position=_REQUEST_DEST,
        )
    assert _delta(before) == {"planned_request_positions": 1.0}
    assert "federated try ended" not in caplog.text


async def test_counter_planned_with_master_positions(monkeypatch):
    sessions = _zz_sessions(monkeypatch)
    _recording_otp(monkeypatch)
    rows = [_row(_ORIGIN, _MASTER_ORIGIN), _row(_HUB, _MASTER_HUB), _row(_DEST, _MASTER_DEST)]
    before = _counts()
    await _plan(rows, sessions, origin_position=_REQUEST_ORIGIN, dest_position=_REQUEST_DEST)
    assert _delta(before) == {"planned_master_positions": 1.0}


async def test_counter_not_touched_without_codes():
    before = _counts()
    await fp.plan_federated(
        _FakeDb([]),
        origin_uic=None,
        dest_uic=_DEST,
        when=datetime(2026, 5, 22, 8, 0, tzinfo=UTC),
        sessions=[],
        timeout_ms=1000,
    )
    assert _delta(before) == {}
