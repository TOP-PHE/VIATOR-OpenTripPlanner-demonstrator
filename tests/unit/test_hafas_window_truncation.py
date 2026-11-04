"""v0.1.45 — `_truncate_hafas_to_viator_window`, the post-fanout step
that clips ÖBB HAFAS's paginated results down to VIATOR's own actual
result span.

`hafas_client.fetch_plan_paginated` runs concurrently with VIATOR's own
session fanout (see `_query_hafas_reference`), so its pagination target
is a fixed config value chosen before VIATOR's results are known — not
VIATOR's actual last departure. If ÖBB's pagination reaches further
than VIATOR's fanout actually did (fewer/earlier VIATOR trips than the
configured window would suggest), the side-by-side comparison would
show ÖBB with MORE coverage in a time range VIATOR was never even
displayed for. This helper clips that overshoot after the fact, once
VIATOR's `merged_trips` is final.

Since #303 both sides are canonical journeys: the boundary is each journey's
`first_boarding`, else its door-to-door `departure`.

Pure function, importable without spinning up FastAPI/Postgres — same
pattern as test_fanout_engine_filter.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.api.journey import _truncate_hafas_to_viator_window
from app.journey.canonical import CanonicalJourney
from app.journey.canonical.canonical_journey import Provider
from tests.unit.canonical_fixtures import journey_from_legs


def _iso(value: datetime) -> str:
    return value.isoformat()


def _journey(provider: Provider, *, start: str | None, boarding: str | None) -> CanonicalJourney:
    """A journey leaving the door at `start` and boarding its train at `boarding` (an access
    walk between them when they differ); walk only when `boarding` is None; no time at all
    when both are None."""
    if start is None and boarding is None:
        return journey_from_legs(
            [{"mode": "RAIL", "from_stop_id": "SBB:8103000", "to_stop_id": "SBB:8100002"}],
            provider=provider,
        )
    begin = datetime.fromisoformat(start or boarding or "")
    if boarding is None:
        walk = {
            "mode": "WALK",
            "departure": _iso(begin),
            "arrival": _iso(begin + timedelta(minutes=10)),
        }
        return journey_from_legs([walk], provider=provider)
    board = datetime.fromisoformat(boarding)
    ride = {
        "mode": "RAIL",
        "from_stop_id": "SBB:8103000",
        "to_stop_id": "SBB:8100002",
        "departure": _iso(board),
        "arrival": _iso(board + timedelta(hours=1)),
    }
    legs = [ride]
    if board != begin:
        legs.insert(0, {"mode": "WALK", "departure": _iso(begin), "arrival": _iso(board)})
    return journey_from_legs(legs, provider=provider)


def _viator_trip(dep_iso: str | None, boarding: str | None = None) -> dict:
    return {"best": _journey("motis", start=dep_iso, boarding=boarding or dep_iso)}


def _hafas_trip(dep_iso: str | None, boarding: str | None = None) -> CanonicalJourney:
    return _journey("hafas", start=dep_iso, boarding=boarding or dep_iso)


def _departures(reference: dict) -> list[str]:
    return [j.departure.timetabled.isoformat() for j in reference["trips"]]


def test_drops_hafas_trips_departing_after_viators_last_trip():
    viator_trips = [
        _viator_trip("2026-07-20T06:19:00+00:00"),
        _viator_trip("2026-07-20T07:00:00+00:00"),
    ]
    hafas_reference = {
        "status": "ok",
        "trips": [
            _hafas_trip("2026-07-20T06:20:00+00:00"),
            _hafas_trip("2026-07-20T07:30:00+00:00"),  # after VIATOR's last (07:00) -> dropped
            _hafas_trip("2026-07-20T14:15:00+00:00"),  # well after -> dropped
        ],
    }

    _truncate_hafas_to_viator_window(hafas_reference, viator_trips)

    assert _departures(hafas_reference) == ["2026-07-20T06:20:00+00:00"]
    assert hafas_reference["trimmed_to_viator_window"] is True


def test_keeps_hafas_trip_departing_exactly_at_viators_last_trip():
    viator_trips = [_viator_trip("2026-07-20T07:00:00+00:00")]
    hafas_reference = {"status": "ok", "trips": [_hafas_trip("2026-07-20T07:00:00+00:00")]}

    _truncate_hafas_to_viator_window(hafas_reference, viator_trips)

    assert len(hafas_reference["trips"]) == 1
    assert "trimmed_to_viator_window" not in hafas_reference


def test_no_op_when_nothing_needs_dropping():
    viator_trips = [_viator_trip("2026-07-20T14:15:00+00:00")]
    hafas_reference = {"status": "ok", "trips": [_hafas_trip("2026-07-20T06:20:00+00:00")]}

    _truncate_hafas_to_viator_window(hafas_reference, viator_trips)

    assert len(hafas_reference["trips"]) == 1
    assert "trimmed_to_viator_window" not in hafas_reference


def test_no_op_when_hafas_reference_is_none():
    # Must not raise -- the comparison panel simply isn't present when
    # the operator didn't opt into ÖBB HAFAS.
    _truncate_hafas_to_viator_window(None, [_viator_trip("2026-07-20T07:00:00+00:00")])


def test_no_op_when_viator_found_nothing():
    # No VIATOR window to align to -- show whatever HAFAS found rather
    # than dropping everything (an empty VIATOR window is not "the
    # window is 0 seconds long").
    hafas_reference = {"status": "ok", "trips": [_hafas_trip("2026-07-20T14:15:00+00:00")]}

    _truncate_hafas_to_viator_window(hafas_reference, [])

    assert len(hafas_reference["trips"]) == 1
    assert "trimmed_to_viator_window" not in hafas_reference


def test_ignores_viator_trips_without_a_time():
    viator_trips = [_viator_trip(None), _viator_trip("2026-07-20T07:00:00+00:00")]
    hafas_reference = {
        "status": "ok",
        "trips": [
            _hafas_trip("2026-07-20T06:20:00+00:00"),
            _hafas_trip("2026-07-20T09:00:00+00:00"),  # after 07:00 -> dropped
        ],
    }

    _truncate_hafas_to_viator_window(hafas_reference, viator_trips)

    assert _departures(hafas_reference) == ["2026-07-20T06:20:00+00:00"]


def test_keeps_hafas_trips_without_a_time():
    # Defensive: an ÖBB journey without a time is kept rather than
    # dropped -- silently hiding a malformed-but-real result would be
    # a worse failure mode than showing one extra card.
    viator_trips = [_viator_trip("2026-07-20T07:00:00+00:00")]
    hafas_reference = {"status": "ok", "trips": [_hafas_trip(None)]}

    _truncate_hafas_to_viator_window(hafas_reference, viator_trips)

    assert len(hafas_reference["trips"]) == 1


# ─────────────── boarding-time (not walk-inclusive) boundary ───────────────
# The window boundary must compare the first boarding (the repo's canonical
# boarding instant) on BOTH sides, not the door-to-door departure, which is
# usually an access walk. The two engines produce different access walks
# for the same physical train, so a door-to-door boundary shifts by however
# long those walks differ, clipping ÖBB trips VIATOR actually covered.


def test_boundary_uses_first_boarding_not_walk_start():
    # VIATOR's last trip: 20 min access walk -> leaves at 06:40 but
    # BOARDS at 07:00. An ÖBB trip boarding the very same 07:00 train
    # (no walk) must be KEPT: a 06:40 boundary would have wrongly dropped it.
    viator_trips = [_viator_trip("2026-07-20T06:40:00+00:00", "2026-07-20T07:00:00+00:00")]
    hafas_reference = {"status": "ok", "trips": [_hafas_trip("2026-07-20T07:00:00+00:00")]}

    _truncate_hafas_to_viator_window(hafas_reference, viator_trips)

    assert len(hafas_reference["trips"]) == 1
    assert "trimmed_to_viator_window" not in hafas_reference


def test_boundary_still_drops_oebb_trip_boarding_after_viators_last_boarding():
    viator_trips = [_viator_trip("2026-07-20T06:40:00+00:00", "2026-07-20T07:00:00+00:00")]
    hafas_reference = {
        "status": "ok",
        "trips": [_hafas_trip("2026-07-20T07:05:00+00:00", "2026-07-20T07:30:00+00:00")],
    }

    _truncate_hafas_to_viator_window(hafas_reference, viator_trips)

    assert hafas_reference["trips"] == []
    assert hafas_reference["trimmed_to_viator_window"] is True


def test_falls_back_to_departure_for_walk_only_journeys():
    # A walk-only journey has no ride, so no first boarding -- fall back to
    # its departure rather than treating the journey as timeless.
    viator_trips = [{"best": _journey("motis", start="2026-07-20T07:00:00+00:00", boarding=None)}]
    hafas_reference = {
        "status": "ok",
        "trips": [
            _journey("hafas", start="2026-07-20T06:30:00+00:00", boarding=None),
            _journey("hafas", start="2026-07-20T09:00:00+00:00", boarding=None),
        ],
    }

    _truncate_hafas_to_viator_window(hafas_reference, viator_trips)

    assert _departures(hafas_reference) == ["2026-07-20T06:30:00+00:00"]
