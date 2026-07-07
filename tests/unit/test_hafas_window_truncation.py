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

Pure function, importable without spinning up FastAPI/Postgres — same
pattern as test_fanout_engine_filter.py.
"""

from __future__ import annotations

from app.api.journey import _truncate_hafas_to_viator_window


def _viator_trip(dep_iso: str) -> dict:
    return {"best": {"departure_at": dep_iso}}


def _hafas_trip(dep_iso: str) -> dict:
    return {"departure_at": dep_iso}


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

    assert [t["departure_at"] for t in hafas_reference["trips"]] == ["2026-07-20T06:20:00+00:00"]
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


def test_ignores_viator_trips_with_unparseable_departure():
    viator_trips = [{"best": {"departure_at": None}}, _viator_trip("2026-07-20T07:00:00+00:00")]
    hafas_reference = {
        "status": "ok",
        "trips": [
            _hafas_trip("2026-07-20T06:20:00+00:00"),
            _hafas_trip("2026-07-20T09:00:00+00:00"),  # after 07:00 -> dropped
        ],
    }

    _truncate_hafas_to_viator_window(hafas_reference, viator_trips)

    assert [t["departure_at"] for t in hafas_reference["trips"]] == ["2026-07-20T06:20:00+00:00"]


def test_keeps_hafas_trips_with_unparseable_departure():
    # Defensive: an unparseable ÖBB departure_at is kept rather than
    # dropped -- silently hiding a malformed-but-real result would be
    # a worse failure mode than showing one extra card.
    viator_trips = [_viator_trip("2026-07-20T07:00:00+00:00")]
    hafas_reference = {"status": "ok", "trips": [_hafas_trip(None)]}

    _truncate_hafas_to_viator_window(hafas_reference, viator_trips)

    assert len(hafas_reference["trips"]) == 1
