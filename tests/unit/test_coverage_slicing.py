"""PR-3 — K-slot time-slicing + per-run day-window/timezone + boarding-
time normalisation.

These tests pin the contracts the runner depends on without standing up
a full DB. The helpers under test (`_resolve_run_window`,
`_slot_boundaries`, `_trip_belongs_to_window`, `_coverage_dedup_key`,
`_fetch_plan_sliced`) are all DB-free by design — see the docstrings on
each in `app/network_coverage/runner.py` for the per-helper rationale.

Coverage:
  - K=1 parity: legacy single-call behaviour is bit-identical to PR-2.
  - K=6 dedup: same train surfaced by adjacent slots collapses to one.
  - Day-D filter on overnight train: a train BOARDING 23:50 on day D
    falls in the day-D window even though arrival is on day D+1.
  - Cross-TZ origin: a window 06:00-12:00 Europe/Vienna excludes a
    train boarding 12:30 UTC (= 14:30 Vienna in summer = outside).
  - Missing `first_transit_leg_departure_utc`: walk-only trips do NOT
    pass the day-D filter (no boarding event to anchor against).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.network_coverage import runner
from app.network_coverage.hubs import Hub


# Minimal helper: build a synthetic trip dict matching the canonical
# shape the planner clients emit (see app/journey/trip_normalize.py).
# Keeps the test data legible inline without forcing every test to
# stand up a fake fetch_plan response.
def _make_trip(
    *,
    legs: list[dict[str, Any]],
    first_transit_dep_utc: str | None,
    duration_seconds: int = 3600,
) -> dict[str, Any]:
    """Synthetic trip dict. `legs` is a list of canonical-shape legs
    (mode, from_stop_id, to_stop_id, route_short_name, departure)."""
    return {
        "duration_seconds": duration_seconds,
        "num_transfers": max(0, len([lg for lg in legs if lg.get("mode") != "WALK"]) - 1),
        "departure_at": legs[0].get("departure", "") if legs else "",
        "arrival_at": "",
        "modes": ",".join(sorted({lg["mode"] for lg in legs if lg.get("mode") != "WALK"})),
        "legs": legs,
        "first_transit_leg_departure_utc": first_transit_dep_utc,
    }


def _transit_leg(
    *,
    mode: str = "RAIL",
    from_stop_id: str = "feed:A",
    to_stop_id: str = "feed:B",
    route: str = "TGV-1",
    departure: str = "2026-07-01T08:00:00+00:00",
    arrival: str = "2026-07-01T09:00:00+00:00",
) -> dict[str, Any]:
    return {
        "mode": mode,
        "from_stop_id": from_stop_id,
        "to_stop_id": to_stop_id,
        "route_short_name": route,
        "departure": departure,
        "arrival": arrival,
    }


# ─────────────────────────── window resolution ───────────────────────────


def test_resolve_run_window_defaults_to_full_day_utc():
    """NULL across the board falls back to platform_config defaults:
    00:00-24:00 UTC on tomorrow at create_run. Here we pass an explicit
    reference_date to make the assertion deterministic across timezones."""
    cfg = runner.CoverageConfig()
    window = runner._resolve_run_window(
        window_start_local=None,
        window_end_local=None,
        window_timezone=None,
        reference_date_value=date(2026, 7, 1),
        cfg=cfg,
    )
    assert window.start_utc == datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
    # "24:00" sentinel resolves to NEXT day midnight (= end-of-day).
    assert window.end_utc == datetime(2026, 7, 2, 0, 0, tzinfo=UTC)


def test_resolve_run_window_cross_tz_vienna_morning_peak():
    """A 06:00-12:00 Europe/Vienna window in JULY (CEST = UTC+2)
    resolves to 04:00-10:00 UTC."""
    cfg = runner.CoverageConfig()
    window = runner._resolve_run_window(
        window_start_local="06:00",
        window_end_local="12:00",
        window_timezone="Europe/Vienna",
        reference_date_value=date(2026, 7, 1),
        cfg=cfg,
    )
    assert window.start_utc == datetime(2026, 7, 1, 4, 0, tzinfo=UTC)
    assert window.end_utc == datetime(2026, 7, 1, 10, 0, tzinfo=UTC)


def test_resolve_run_window_cross_midnight_night_train():
    """An 18:00-06:00 window must roll the end forward by one day —
    night-train operators care about the trains that BOARD on day D
    after 18:00 OR before 06:00 on D+1 (= 12-hour overnight slice)."""
    cfg = runner.CoverageConfig()
    window = runner._resolve_run_window(
        window_start_local="18:00",
        window_end_local="06:00",
        window_timezone="UTC",
        reference_date_value=date(2026, 7, 1),
        cfg=cfg,
    )
    assert window.start_utc == datetime(2026, 7, 1, 18, 0, tzinfo=UTC)
    assert window.end_utc == datetime(2026, 7, 2, 6, 0, tzinfo=UTC)


def test_resolve_run_window_accepts_dtime_objects():
    """The ORM column type is `datetime.time`; the helper accepts both
    that and "HH:MM" strings so callers from the runner (ORM rows) and
    the unit tests (strings) can use one signature."""
    cfg = runner.CoverageConfig()
    window = runner._resolve_run_window(
        window_start_local=dtime(8, 0),
        window_end_local=dtime(14, 0),
        window_timezone="UTC",
        reference_date_value=date(2026, 7, 1),
        cfg=cfg,
    )
    assert window.start_utc == datetime(2026, 7, 1, 8, 0, tzinfo=UTC)
    assert window.end_utc == datetime(2026, 7, 1, 14, 0, tzinfo=UTC)


def test_resolve_run_window_unknown_tz_falls_back_to_utc():
    """Unknown IANA zone should not crash the runner — must fall back
    to UTC silently (with a log warning, not tested here)."""
    cfg = runner.CoverageConfig()
    window = runner._resolve_run_window(
        window_start_local="00:00",
        window_end_local="24:00",
        window_timezone="Not/A/Zone",
        reference_date_value=date(2026, 7, 1),
        cfg=cfg,
    )
    assert window.tz_name == "UTC"


# ─────────────────────────── slot boundaries ───────────────────────────


def test_slot_boundaries_k_equals_one_returns_window_endpoints():
    """K=1 — the legacy single-call rollback path. Boundaries are just
    [start_utc, end_utc] so `_fetch_plan_sliced` can dispatch one call
    bit-identical to PR-2."""
    window = runner.ResolvedWindow(
        start_utc=datetime(2026, 7, 1, 0, 0, tzinfo=UTC),
        end_utc=datetime(2026, 7, 2, 0, 0, tzinfo=UTC),
        tz_name="UTC",
    )
    boundaries = runner._slot_boundaries(window, slot_count=1)
    assert boundaries == [window.start_utc, window.end_utc]


def test_slot_boundaries_k_equals_six_gives_four_hour_slots():
    """K=6 over a 24h window → 7 boundary instants, each 4h apart."""
    window = runner.ResolvedWindow(
        start_utc=datetime(2026, 7, 1, 0, 0, tzinfo=UTC),
        end_utc=datetime(2026, 7, 2, 0, 0, tzinfo=UTC),
        tz_name="UTC",
    )
    boundaries = runner._slot_boundaries(window, slot_count=6)
    assert len(boundaries) == 7
    assert boundaries[0] == datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
    assert boundaries[1] == datetime(2026, 7, 1, 4, 0, tzinfo=UTC)
    assert boundaries[6] == datetime(2026, 7, 2, 0, 0, tzinfo=UTC)


# ─────────────────────────── day-D filter ───────────────────────────


def test_trip_belongs_to_window_overnight_train_boards_day_d():
    """A train BOARDING 23:50 on day D belongs in the day-D 00:00-24:00
    window even though arrival is on D+1 — the runner anchors on
    BOARDING TIME, not arrival."""
    window = runner.ResolvedWindow(
        start_utc=datetime(2026, 7, 1, 0, 0, tzinfo=UTC),
        end_utc=datetime(2026, 7, 2, 0, 0, tzinfo=UTC),
        tz_name="UTC",
    )
    trip = _make_trip(
        legs=[
            _transit_leg(
                departure="2026-07-01T23:50:00+00:00",
                arrival="2026-07-02T07:30:00+00:00",
            )
        ],
        first_transit_dep_utc="2026-07-01T23:50:00+00:00",
    )
    assert runner._trip_belongs_to_window(trip, window) is True


def test_trip_belongs_to_window_excludes_train_boarding_outside_vienna_window():
    """Vienna 06:00-12:00 (= 04:00-10:00 UTC in CEST). A train boarding
    12:30 UTC (= 14:30 Vienna local) is outside and must NOT count."""
    window = runner._resolve_run_window(
        window_start_local="06:00",
        window_end_local="12:00",
        window_timezone="Europe/Vienna",
        reference_date_value=date(2026, 7, 1),
        cfg=runner.CoverageConfig(),
    )
    trip = _make_trip(
        legs=[_transit_leg(departure="2026-07-01T12:30:00+00:00")],
        first_transit_dep_utc="2026-07-01T12:30:00+00:00",
    )
    assert runner._trip_belongs_to_window(trip, window) is False


def test_trip_belongs_to_window_walk_only_returns_false():
    """A walk-only itinerary has no boarding event, so the day-D
    filter excludes it — the operator wanted "how many trains run on
    day D", not "is the OD pair reachable on foot"."""
    window = runner.ResolvedWindow(
        start_utc=datetime(2026, 7, 1, 0, 0, tzinfo=UTC),
        end_utc=datetime(2026, 7, 2, 0, 0, tzinfo=UTC),
        tz_name="UTC",
    )
    trip = _make_trip(
        legs=[{"mode": "WALK", "departure": "2026-07-01T08:00:00+00:00"}],
        first_transit_dep_utc=None,
    )
    assert runner._trip_belongs_to_window(trip, window) is False


def test_trip_belongs_to_window_missing_field_returns_false():
    """A trip dict that genuinely lacks `first_transit_leg_departure_utc`
    (legacy fixture, malformed input) is excluded rather than guessed
    against `departure_at` — guessing would silently let the
    walk-then-train edge case slip through."""
    window = runner.ResolvedWindow(
        start_utc=datetime(2026, 7, 1, 0, 0, tzinfo=UTC),
        end_utc=datetime(2026, 7, 2, 0, 0, tzinfo=UTC),
        tz_name="UTC",
    )
    trip = {
        "departure_at": "2026-07-01T08:00:00+00:00",
        "legs": [_transit_leg()],
        # No 'first_transit_leg_departure_utc' field at all
    }
    assert runner._trip_belongs_to_window(trip, window) is False


# ─────────────────────────── dedup key ───────────────────────────


def test_coverage_dedup_key_collapses_same_train_across_slots():
    """The whole point of K-slot dedup: same train surfaced by two
    adjacent slots must produce the same dedup key so it's counted
    once. The synthetic case: a TGV 08:00 → 09:00 returned by both
    the 06:00-anchored and 08:00-anchored slot calls."""
    trip = _make_trip(
        legs=[
            _transit_leg(
                from_stop_id="sncf:8727100",
                to_stop_id="sncf:8727101",
                route="TGV-6601",
                departure="2026-07-01T08:00:00+00:00",
            )
        ],
        first_transit_dep_utc="2026-07-01T08:00:00+00:00",
    )
    key_a = runner._coverage_dedup_key(trip)
    key_b = runner._coverage_dedup_key(trip)
    assert key_a == key_b
    # Verify the tuple shape is what we documented.
    assert key_a[0] == "8727100"  # from_stop_id_normalised
    assert "2026-07-01T08:00" in key_a[1]  # truncated-to-minute UTC
    assert key_a[2] == ("TGV-6601",)  # route signature
    assert key_a[3] == "8727101"  # to_stop_id_normalised


def test_coverage_dedup_key_cross_engine_normalisation():
    """OTP uses `<feed>:<local>` and MOTIS uses `<feed>_<local>` for
    stop ids. A train surfaced by both engines on a fanout coverage
    run must dedup — `_normalise_stop_id` strips the prefix on both
    forms."""
    trip_otp = _make_trip(
        legs=[
            _transit_leg(
                from_stop_id="sncf:8727100",
                to_stop_id="sncf:8727101",
                route="TGV-1",
            )
        ],
        first_transit_dep_utc="2026-07-01T08:00:00+00:00",
    )
    trip_motis = _make_trip(
        legs=[
            _transit_leg(
                from_stop_id="sncf_8727100",
                to_stop_id="sncf_8727101",
                route="TGV-1",
            )
        ],
        first_transit_dep_utc="2026-07-01T08:00:00+00:00",
    )
    assert runner._coverage_dedup_key(trip_otp) == runner._coverage_dedup_key(trip_motis)


def test_coverage_dedup_key_walk_only_collapses_to_sentinel():
    """A walk-only trip has no transit legs → returns the (\"\", \"\", (), \"\")
    sentinel so multiple walk-only trips collapse to one entry. Not
    that this matters in practice — `_trip_belongs_to_window` already
    excludes walk-only trips upstream."""
    trip = _make_trip(
        legs=[{"mode": "WALK", "from_stop_id": "", "to_stop_id": ""}],
        first_transit_dep_utc=None,
    )
    assert runner._coverage_dedup_key(trip) == ("", "", (), "")


# ─────────────────────────── _fetch_plan_sliced ───────────────────────────


@pytest.mark.asyncio
async def test_fetch_plan_sliced_k_equals_one_is_legacy_single_call():
    """K=1 — bit-identical to PR-2's single fetch_plan call. The
    rollback flag: set COVERAGE_SLOT_COUNT=1 in /admin/config and
    PR-3's behaviour collapses to the pre-PR-3 single-call shape.
    """
    fake_planner = AsyncMock()
    fake_planner.fetch_plan = AsyncMock(
        return_value=({"raw": "single"}, [_make_trip(legs=[], first_transit_dep_utc=None)])
    )
    cfg = runner.CoverageConfig(slot_count=1)
    window = runner.ResolvedWindow(
        start_utc=datetime(2026, 7, 1, 0, 0, tzinfo=UTC),
        end_utc=datetime(2026, 7, 2, 0, 0, tzinfo=UTC),
        tz_name="UTC",
    )

    with patch.object(runner.planner_dispatch, "planner_for_engine", return_value=fake_planner):
        raw, trips = await runner._fetch_plan_sliced(
            engine="otp",
            session_id="sess-1",
            origin=Hub(id="orig", name="O", short="O", region="", lat=0.0, lon=0.0),
            dest=Hub(id="dest", name="D", short="D", region="", lat=1.0, lon=1.0),
            window=window,
            cfg=cfg,
        )

    # Exactly one fetch_plan call. The K=1 branch passes the legacy
    # COVERAGE_NUM_ITINERARIES / COVERAGE_SEARCH_WINDOW_SECONDS so the
    # rollback to pre-PR-3 search depth is preserved.
    assert fake_planner.fetch_plan.call_count == 1
    call_kwargs = fake_planner.fetch_plan.call_args.kwargs
    assert call_kwargs["num_itineraries"] == cfg.num_itineraries
    assert call_kwargs["search_window_seconds"] == cfg.search_window_seconds
    # No filtering on K=1 — the raw payload's trips pass through
    # untouched so the rollback is genuinely bit-identical.
    assert raw == {"raw": "single"}
    assert len(trips) == 1


@pytest.mark.asyncio
async def test_fetch_plan_sliced_k_equals_six_dedups_and_filters():
    """K=6 — every slot returns a trip with the same dedup key (= same
    train). After filter + dedup the result must be exactly one trip
    (not six)."""
    same_train_trip = _make_trip(
        legs=[
            _transit_leg(
                from_stop_id="sncf:8727100",
                to_stop_id="sncf:8727101",
                route="TGV-6601",
                departure="2026-07-01T08:00:00+00:00",
            )
        ],
        first_transit_dep_utc="2026-07-01T08:00:00+00:00",
    )
    fake_planner = AsyncMock()
    fake_planner.fetch_plan = AsyncMock(return_value=({"raw": "slot"}, [same_train_trip]))
    cfg = runner.CoverageConfig(slot_count=6, within_pair_parallelism=3, slot_timeout_ms=5000)
    window = runner.ResolvedWindow(
        start_utc=datetime(2026, 7, 1, 0, 0, tzinfo=UTC),
        end_utc=datetime(2026, 7, 2, 0, 0, tzinfo=UTC),
        tz_name="UTC",
    )

    with patch.object(runner.planner_dispatch, "planner_for_engine", return_value=fake_planner):
        _raw, trips = await runner._fetch_plan_sliced(
            engine="otp",
            session_id="sess-1",
            origin=Hub(id="orig", name="O", short="O", region="", lat=0.0, lon=0.0),
            dest=Hub(id="dest", name="D", short="D", region="", lat=1.0, lon=1.0),
            window=window,
            cfg=cfg,
        )

    # Six fetch_plan calls (K=6), one per slot.
    assert fake_planner.fetch_plan.call_count == 6
    # Six identical trips collapse to one via the dedup pass.
    assert len(trips) == 1
    # Per-slot num_itineraries is the per-slot knob, not the legacy one.
    first_call_kwargs = fake_planner.fetch_plan.call_args_list[0].kwargs
    assert first_call_kwargs["num_itineraries"] == cfg.num_itineraries_per_slot


@pytest.mark.asyncio
async def test_fetch_plan_sliced_filters_out_of_window_trips():
    """Trips returned by a slot but landing OUTSIDE the window (e.g.
    OTP/MOTIS returned a "next train" past the upper bound) must be
    filtered out. Otherwise the matrix would inflate with trips the
    operator's window deliberately excluded."""
    in_window = _make_trip(
        legs=[_transit_leg(departure="2026-07-01T08:00:00+00:00")],
        first_transit_dep_utc="2026-07-01T08:00:00+00:00",
    )
    out_of_window = _make_trip(
        legs=[
            _transit_leg(
                route="LATER",
                from_stop_id="x:9",
                to_stop_id="x:10",
                departure="2026-07-02T01:30:00+00:00",
            )
        ],
        first_transit_dep_utc="2026-07-02T01:30:00+00:00",
    )
    fake_planner = AsyncMock()
    fake_planner.fetch_plan = AsyncMock(return_value=({"raw": "slot"}, [in_window, out_of_window]))
    cfg = runner.CoverageConfig(slot_count=2, within_pair_parallelism=2, slot_timeout_ms=5000)
    window = runner.ResolvedWindow(
        start_utc=datetime(2026, 7, 1, 0, 0, tzinfo=UTC),
        end_utc=datetime(2026, 7, 2, 0, 0, tzinfo=UTC),
        tz_name="UTC",
    )

    with patch.object(runner.planner_dispatch, "planner_for_engine", return_value=fake_planner):
        _raw, trips = await runner._fetch_plan_sliced(
            engine="otp",
            session_id="sess-1",
            origin=Hub(id="orig", name="O", short="O", region="", lat=0.0, lon=0.0),
            dest=Hub(id="dest", name="D", short="D", region="", lat=1.0, lon=1.0),
            window=window,
            cfg=cfg,
        )

    # Only the in-window trip survives the day-D filter.
    assert len(trips) == 1
    assert trips[0]["first_transit_leg_departure_utc"] == "2026-07-01T08:00:00+00:00"


@pytest.mark.asyncio
async def test_fetch_plan_sliced_tolerates_partial_slot_failure():
    """One slot raising shouldn't kill the pair — the rest of the slots
    contribute their trips and the pair gets partial coverage. Only
    when EVERY slot fails do we re-raise (so the caller can mark the
    pair as error/timeout)."""
    in_window = _make_trip(
        legs=[_transit_leg(departure="2026-07-01T08:00:00+00:00")],
        first_transit_dep_utc="2026-07-01T08:00:00+00:00",
    )
    fake_planner = AsyncMock()
    fake_planner.fetch_plan = AsyncMock(
        side_effect=[
            TimeoutError("slot 1 timed out"),
            ({"raw": "slot2"}, [in_window]),
        ]
    )
    cfg = runner.CoverageConfig(slot_count=2, within_pair_parallelism=2, slot_timeout_ms=5000)
    window = runner.ResolvedWindow(
        start_utc=datetime(2026, 7, 1, 0, 0, tzinfo=UTC),
        end_utc=datetime(2026, 7, 2, 0, 0, tzinfo=UTC),
        tz_name="UTC",
    )

    with patch.object(runner.planner_dispatch, "planner_for_engine", return_value=fake_planner):
        _raw, trips = await runner._fetch_plan_sliced(
            engine="otp",
            session_id="sess-1",
            origin=Hub(id="orig", name="O", short="O", region="", lat=0.0, lon=0.0),
            dest=Hub(id="dest", name="D", short="D", region="", lat=1.0, lon=1.0),
            window=window,
            cfg=cfg,
        )

    # Slot 1 raised; slot 2 contributed one trip — that's the pair's
    # coverage signal.
    assert len(trips) == 1


# ──────────────────── first_transit_leg_departure_utc ────────────────────
#
# The cross-client contract: every planner client (motis_client,
# otp_client, ojp_client) must emit `first_transit_leg_departure_utc`
# on every trip. The helper itself is unit-tested here against the
# canonical leg shape; client wiring is exercised in the existing
# client tests.


def test_first_transit_leg_departure_utc_walk_only_returns_none():
    from app.journey.trip_normalize import first_transit_leg_departure_utc

    legs = [{"mode": "WALK", "departure": "2026-07-01T08:00:00+00:00"}]
    assert first_transit_leg_departure_utc(legs) is None


def test_first_transit_leg_departure_utc_finds_first_transit():
    from app.journey.trip_normalize import first_transit_leg_departure_utc

    legs = [
        {"mode": "WALK", "departure": "2026-07-01T07:55:00+00:00"},
        {"mode": "RAIL", "departure": "2026-07-01T08:00:00+00:00"},
        {"mode": "RAIL", "departure": "2026-07-01T09:30:00+00:00"},
    ]
    assert first_transit_leg_departure_utc(legs) == "2026-07-01T08:00:00+00:00"


def test_first_transit_leg_departure_utc_normalises_to_utc():
    """A leg emitted with a non-UTC offset (= a client that didn't
    pre-normalise) must still produce a UTC-suffix ISO string so the
    coverage dedup key is stable across engines."""
    from app.journey.trip_normalize import first_transit_leg_departure_utc

    legs = [{"mode": "RAIL", "departure": "2026-07-01T10:00:00+02:00"}]
    # +02:00 → 08:00 UTC
    assert first_transit_leg_departure_utc(legs) == "2026-07-01T08:00:00+00:00"


def test_first_transit_leg_departure_utc_none_legs_returns_none():
    """`legs=None` is the planner-emit signal for "we couldn't construct
    leg detail" (rare but real on OJP error paths). Must return None so
    the runner's day-window filter drops the trip cleanly. Empty list
    same — no boarding event means no membership in any window."""
    from app.journey.trip_normalize import first_transit_leg_departure_utc

    assert first_transit_leg_departure_utc(None) is None
    assert first_transit_leg_departure_utc([]) is None


def test_coerce_utc_iso_handles_garbage_input():
    """Bad input (None, non-string, unparseable string) must return None,
    not raise — clients can emit dirty data, and the day-window filter
    is the right place to swallow it (the trip just won't match the
    window and gets dropped)."""
    from app.journey.trip_normalize import _coerce_utc_iso

    assert _coerce_utc_iso(None) is None
    assert _coerce_utc_iso("") is None
    assert _coerce_utc_iso(12345) is None  # not a string
    assert _coerce_utc_iso("not-a-date") is None
    assert _coerce_utc_iso("2026-13-99T99:99:99") is None  # parse fails


def test_coerce_utc_iso_naive_datetime_assumed_utc():
    """A naive ISO string (no offset suffix) is assumed UTC, not local
    time. This matches the convention used by every client in the
    codebase — naive means "the planner forgot to mark it, but we know
    it's UTC because the API contract says so". Test exists to lock the
    convention so a future contributor doesn't accidentally switch to
    "naive means local"."""
    from app.journey.trip_normalize import _coerce_utc_iso

    assert _coerce_utc_iso("2026-07-01T12:00:00") == "2026-07-01T12:00:00+00:00"


# ──────────────────── _accumulate_slot_trips ────────────────────
#
# Extracted helper from `_merge_slot_results` to keep its CC under
# Sonar's 15 ceiling. Tested directly so coverage on new code stays
# above the 80% gate.


def test_accumulate_slot_trips_filters_outside_window_and_dedups():
    """Two trips in window with the same dedup key collapse to one;
    a trip outside the window is dropped entirely."""
    window = runner.ResolvedWindow(
        start_utc=datetime(2026, 7, 1, 8, 0, tzinfo=UTC),
        end_utc=datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
        tz_name="UTC",
    )

    in_window = _make_trip(
        legs=[_transit_leg(departure="2026-07-01T09:00:00+00:00")],
        first_transit_dep_utc="2026-07-01T09:00:00+00:00",
    )
    # Same trip from an adjacent slot — same dedup key, must collapse.
    duplicate_from_neighbour_slot = _make_trip(
        legs=[_transit_leg(departure="2026-07-01T09:00:00+00:00")],
        first_transit_dep_utc="2026-07-01T09:00:00+00:00",
    )
    outside_window = _make_trip(
        legs=[_transit_leg(departure="2026-07-01T13:00:00+00:00", route="TGV-2")],
        first_transit_dep_utc="2026-07-01T13:00:00+00:00",
    )

    deduped: dict = {}
    runner._accumulate_slot_trips(
        [in_window, duplicate_from_neighbour_slot, outside_window], window, deduped
    )

    assert len(deduped) == 1, "duplicate collapses to one; outside-window dropped"


def test_accumulate_slot_trips_empty_input_is_a_noop():
    """A slot that returned zero trips must leave the dedup map
    untouched — exercises the no-iterations path that today's
    K=1-parity test doesn't cover."""
    window = runner.ResolvedWindow(
        start_utc=datetime(2026, 7, 1, 0, 0, tzinfo=UTC),
        end_utc=datetime(2026, 7, 2, 0, 0, tzinfo=UTC),
        tz_name="UTC",
    )
    sentinel = {"existing_key": {"sentinel": True}}
    runner._accumulate_slot_trips([], window, sentinel)

    assert sentinel == {"existing_key": {"sentinel": True}}


# ─────────────────── create_run with PR-3 window/tz/refdate args ──────────────
#
# Lines 715-718 + 771-796 in runner.py are uncovered without these tests
# — every existing create_run test ignored the new optional kwargs.


def _add_capture_run(captured: list) -> Any:
    """Capture the NetworkCoverageRun row that create_run passes to db.add."""
    from app.models import NetworkCoverageRun

    def _capture(row: Any) -> None:
        if isinstance(row, NetworkCoverageRun):
            captured.append(row)

    return _capture


def _stub_active_hubs(monkeypatch: Any) -> None:
    """Lightweight Hub list for create_run path tests — only needs to be
    non-empty so the function reaches the row-construction block."""

    def fake_load(_db: Any, countries: list[str] | None = None) -> list[Hub]:
        return [
            Hub(id="paris", name="Paris", short="PAR", region="", lat=48.85, lon=2.35),
            Hub(id="lyon", name="Lyon", short="LYO", region="", lat=45.76, lon=4.84),
        ]

    monkeypatch.setattr(runner, "_load_active_hubs", fake_load)


def test_create_run_window_fields_default_to_none_and_reference_date_to_depart_at_day(monkeypatch):
    """All-NULL window inputs reproduce pre-PR-3 behaviour, but
    reference_date now defaults to the CALENDAR DAY OF depart_at — not
    "tomorrow at create time".

    Regression guard for the wrong-day bug: `_resolve_run_window` builds
    the K-slot search grid from `reference_date` alone, while every
    downstream comparison (the ÖBB verify sweep, PR #221's trip filter)
    anchors on `depart_at`. When the two named different days the run
    searched a day the operator never asked for, and
    `departure_at >= depart_at` matched zero of the run's own trips.
    """
    from unittest.mock import MagicMock

    _stub_active_hubs(monkeypatch)
    added: list = []
    db = MagicMock()
    db.add.side_effect = _add_capture_run(added)

    runner.create_run(
        db,
        actor_user_id=None,
        session_id="nap-fr-rail",
        depart_at=datetime(2026, 7, 1, 8, 0),
    )

    assert len(added) == 1
    row = added[0]
    assert row.window_start_local is None
    assert row.window_end_local is None
    assert row.window_timezone is None
    # The exact date, not merely "some date" — the old assertion
    # (`is not None`) is what let "tomorrow" slip through for months.
    assert row.reference_date == date(2026, 7, 1)


def test_create_run_persists_explicit_window_and_timezone(monkeypatch):
    """Operator picks a 06:00-12:00 Europe/Vienna window → all four PR-3
    fields land on the row verbatim. reference_date passed explicitly
    bypasses the _default_reference_date helper."""
    from datetime import time as dtime
    from unittest.mock import MagicMock

    _stub_active_hubs(monkeypatch)
    added: list = []
    db = MagicMock()
    db.add.side_effect = _add_capture_run(added)

    runner.create_run(
        db,
        actor_user_id=None,
        session_id="nap-fr-rail",
        depart_at=datetime(2026, 7, 1, 8, 0),
        window_start_local=dtime(6, 0),
        window_end_local=dtime(12, 0),
        window_timezone="Europe/Vienna",
        reference_date_value=date(2026, 7, 1),
    )

    row = added[0]
    assert row.window_start_local == dtime(6, 0)
    assert row.window_end_local == dtime(12, 0)
    assert row.window_timezone == "Europe/Vienna"
    assert row.reference_date == date(2026, 7, 1)


# ───────────── _anchor_depart_at_and_reference_date ─────────────
# One timezone per run: a naive `depart_at` (what the admin form's
# datetime-local posts) is a wall-clock in the run's own window_timezone,
# and `reference_date` is the day whose WINDOW contains that instant.


def _anchor(depart_at, ref=None, *, start=None, end=None, tz=None):
    return runner._anchor_depart_at_and_reference_date(
        depart_at, ref, start, end, tz, runner.CoverageConfig()
    )


def test_anchor_localises_naive_depart_at_to_the_window_timezone():
    """A naive depart_at is the operator's local wall-clock, NOT UTC.

    The old API layer did `depart_at.replace(tzinfo=UTC)`, so an operator
    on a Europe/Brussels run asking for 06:40 actually anchored at
    06:40Z = 08:40 local, silently clipping the early-morning trips off
    the front of every depart_at-anchored comparison."""
    depart_at, ref = _anchor(datetime(2026, 7, 20, 6, 40), tz="Europe/Brussels")
    # 06:40 Brussels in July (CEST, UTC+2) == 04:40Z -- the instant the
    # operator meant, not 06:40Z.
    assert depart_at.utcoffset() == timedelta(hours=2)
    assert depart_at.astimezone(UTC) == datetime(2026, 7, 20, 4, 40, tzinfo=UTC)
    assert ref == date(2026, 7, 20)


def test_anchor_respects_an_already_aware_depart_at():
    """A tz-aware depart_at already names an instant — keep it, and read
    reference_date off it in the run's zone."""
    aware = datetime(2026, 7, 20, 4, 40, tzinfo=UTC)
    depart_at, ref = _anchor(aware, tz="Europe/Brussels")
    assert depart_at == aware  # same instant
    assert ref == date(2026, 7, 20)  # 06:40 Brussels


def test_anchor_reads_reference_date_in_the_window_timezone_not_utc():
    """Date-boundary case: the instant's UTC day and its local day differ.
    reference_date must follow the run's zone, since that's the zone the
    K-slot window is composed in."""
    # 2026-07-20 13:00Z == 2026-07-21 01:00 NZST (UTC+12)
    _depart_at, ref = _anchor(datetime(2026, 7, 20, 13, 0, tzinfo=UTC), tz="Pacific/Auckland")
    assert ref == date(2026, 7, 21)


def test_anchor_unknown_timezone_falls_back_to_utc():
    """Operator picks a bogus IANA zone → fall back to UTC silently rather
    than raising. Without this the runner crashes before the row exists."""
    depart_at, ref = _anchor(datetime(2026, 7, 20, 6, 40), tz="Not/A/Zone")
    assert depart_at.utcoffset() == timedelta(0)
    assert ref == date(2026, 7, 20)


def test_anchor_accepts_an_explicit_reference_date_that_agrees():
    _depart_at, ref = _anchor(datetime(2026, 7, 20, 6, 40), date(2026, 7, 20), tz="Europe/Brussels")
    assert ref == date(2026, 7, 20)


def test_anchor_rejects_an_explicit_reference_date_that_disagrees():
    """Honouring a mismatched reference_date would recreate the very bug
    this whole change fixes, on a brand-new run: the grid searches one day
    while every depart_at-anchored comparison judges another, and the
    alignment sweep writes one_sided tiers across the whole matrix.
    `create_run`'s ValueError maps to HTTP 400 at the API layer."""
    with pytest.raises(ValueError, match="does not match the day-window"):
        _anchor(datetime(2026, 7, 20, 8, 0), date(2026, 7, 27), tz="Europe/Brussels")


# ── cross-midnight windows: the anchor day is the day the window OPENED ──


def test_anchor_for_cross_midnight_window_uses_the_previous_day():
    """Night-train run: window 18:00-06:00, depart 02:00. That 02:00 train
    belongs to the window that opened at 18:00 the PREVIOUS evening, so the
    grid must anchor on the 20th — not the 21st, which would compose a grid
    starting 16 hours after the train the operator asked about."""
    _depart_at, ref = _anchor(
        datetime(2026, 7, 21, 2, 0), start=dtime(18, 0), end=dtime(6, 0), tz="Europe/Vienna"
    )
    assert ref == date(2026, 7, 20)


def test_anchor_for_cross_midnight_window_evening_side_uses_the_same_day():
    _depart_at, ref = _anchor(
        datetime(2026, 7, 20, 20, 0), start=dtime(18, 0), end=dtime(6, 0), tz="Europe/Vienna"
    )
    assert ref == date(2026, 7, 20)


def test_anchor_for_narrowed_window_outside_it_degrades_to_the_local_date():
    """Operator narrows to 06:00-22:00 and departs 05:30 — outside their own
    window. Not a bug we can resolve for them; anchor on the local date and
    let the run search a coherent grid."""
    _depart_at, ref = _anchor(
        datetime(2026, 7, 20, 5, 30), start=dtime(6, 0), end=dtime(22, 0), tz="Europe/Vienna"
    )
    assert ref == date(2026, 7, 20)


# ───────────── reference_date_matches_depart_at ─────────────


def _run_row(*, depart_at, reference_date, tz=None, start=None, end=None):
    from types import SimpleNamespace

    return SimpleNamespace(
        depart_at=depart_at,
        reference_date=reference_date,
        window_timezone=tz,
        window_start_local=start,
        window_end_local=end,
    )


def test_reference_date_matches_for_a_correctly_anchored_run():
    cfg = runner.CoverageConfig()  # 00:00-24:00 UTC full-day default
    run = _run_row(
        depart_at=datetime(2026, 7, 20, 6, 40, tzinfo=UTC), reference_date=date(2026, 7, 20)
    )
    assert runner.reference_date_matches_depart_at(run, cfg) is True


def test_reference_date_mismatch_for_the_legacy_tomorrow_run():
    """The exact production shape that produced "26 itineraries" beside an
    empty trip list: the run searched 2026-07-09 (reference_date defaulted
    to tomorrow-at-create) while depart_at named 2026-07-20."""
    cfg = runner.CoverageConfig()
    run = _run_row(
        depart_at=datetime(2026, 7, 20, 6, 40, tzinfo=UTC), reference_date=date(2026, 7, 9)
    )
    assert runner.reference_date_matches_depart_at(run, cfg) is False


def test_narrowed_window_with_earlier_depart_at_is_not_flagged_as_wrong_day():
    """Regression guard: a DAY-level comparison, not window containment.
    depart_at 05:30 precedes a 06:00-22:00 window's first slot, but the run
    is correctly anchored — badging it would slander a valid run AND
    silently disable PR #221's trip filter for it."""
    cfg = runner.CoverageConfig()
    run = _run_row(
        depart_at=datetime(2026, 7, 20, 5, 30, tzinfo=UTC),
        reference_date=date(2026, 7, 20),
        start=dtime(6, 0),
        end=dtime(22, 0),
    )
    assert runner.reference_date_matches_depart_at(run, cfg) is True


def test_cross_midnight_run_anchored_on_the_previous_day_is_not_flagged():
    """A night-train run legitimately stores reference_date = depart_at's
    day minus one. Containment-based detection would flag it; day-level
    detection using the same anchor rule must not."""
    cfg = runner.CoverageConfig()
    run = _run_row(
        depart_at=datetime(2026, 7, 21, 0, 0, tzinfo=UTC),  # 02:00 Vienna
        reference_date=date(2026, 7, 20),
        tz="Europe/Vienna",
        start=dtime(18, 0),
        end=dtime(6, 0),
    )
    assert runner.reference_date_matches_depart_at(run, cfg) is True
