"""Smoke tests for the MOTIS Phase-0 client — only the pure translator helpers.

The async `fetch_plan` is network/DB-bound and will be exercised live during
the spike measurement (see motis-spike/compare.py); here we only pin the
deterministic translation logic so future schema drift / refactors are caught.
"""

from __future__ import annotations

from app.journey.motis_client import (
    _base_url_for,
    _itineraries_to_trips,
    _leg_to_canonical,
)

# ──────────────────────────── _base_url_for ────────────────────────────


def test_base_url_default_mirrors_otp_per_session_convention():
    assert _base_url_for("nap-fr-rail", None) == "http://motis-nap-fr-rail:8080"


def test_base_url_override_wins_and_strips_trailing_slash():
    assert _base_url_for("anything", "http://localhost:8081/") == "http://localhost:8081"
    assert _base_url_for("anything", "http://localhost:8081") == "http://localhost:8081"


# ────────────────────────── _leg_to_canonical ──────────────────────────


def test_leg_canonical_maps_motis_fields_to_otp_shape():
    leg = {
        "mode": "RAIL",
        "startTime": "2026-06-01T08:00:00+00:00",
        "endTime": "2026-06-01T11:00:00+00:00",
        "duration": 10800,
        "from": {
            "name": "Paris Gare de Lyon",
            "latitude": 48.844,
            "longitude": 2.374,
            "stopId": "FR:StopPlace:8768600",
        },
        "to": {
            "name": "Lyon Part-Dieu",
            "latitude": 45.760,
            "longitude": 4.860,
            "stopId": "FR:StopPlace:8772319",
        },
        "routeShortName": "TGV",
        "agency": {"name": "SNCF Voyageurs", "url": "https://sncf.com"},
        "headsign": "Lyon Part-Dieu",
        "tripId": "TGV6603",
    }
    out = _leg_to_canonical(leg)
    # Time + space + transit identity make the round-trip into the canonical
    # leg dict the federated planner already consumes.
    assert out["mode"] == "RAIL"
    assert out["departure"] == "2026-06-01T08:00:00+00:00"
    assert out["arrival"] == "2026-06-01T11:00:00+00:00"
    assert out["duration_seconds"] == 10800
    assert out["from_name"] == "Paris Gare de Lyon"
    assert out["from_lat"] == 48.844
    assert out["from_lon"] == 2.374
    assert out["from_stop_id"] == "FR:StopPlace:8768600"
    assert out["to_stop_id"] == "FR:StopPlace:8772319"
    assert out["route_short_name"] == "TGV"
    assert out["agency_name"] == "SNCF Voyageurs"
    assert out["trip_id"] == "TGV6603"
    assert out["trip_headsign"] == "Lyon Part-Dieu"


def test_leg_canonical_tolerates_missing_optional_fields():
    out = _leg_to_canonical(
        {"mode": "WALK", "startTime": "2026-06-01T08:00:00Z", "endTime": "2026-06-01T08:05:00Z"}
    )
    assert out["mode"] == "WALK"
    assert out["from_name"] is None
    assert out["from_stop_id"] is None
    assert out["route_short_name"] is None
    assert out["trip_id"] is None
    # Duration absent -> 0 (consistent with the OTP path's int(... or 0)).
    assert out["duration_seconds"] == 0


# ─────────────────────── _itineraries_to_trips ────────────────────────


def test_itineraries_to_trips_empty_response():
    assert _itineraries_to_trips({}) == []
    assert _itineraries_to_trips({"itineraries": []}) == []


def test_itineraries_to_trips_maps_top_level_fields_and_mode_summary():
    raw = {
        "itineraries": [
            {
                "startTime": "2026-06-01T08:00:00+00:00",
                "endTime": "2026-06-01T13:00:00+00:00",
                "duration": 18000,
                "transfers": 1,
                "legs": [
                    {
                        "mode": "WALK",
                        "startTime": "2026-06-01T08:00:00Z",
                        "endTime": "2026-06-01T08:05:00Z",
                    },
                    {
                        "mode": "RAIL",
                        "startTime": "2026-06-01T08:05:00Z",
                        "endTime": "2026-06-01T11:00:00Z",
                        "routeShortName": "TGV",
                    },
                    {
                        "mode": "RAIL",
                        "startTime": "2026-06-01T11:30:00Z",
                        "endTime": "2026-06-01T13:00:00Z",
                        "routeShortName": "TER",
                    },
                ],
            }
        ]
    }
    trips = _itineraries_to_trips(raw)
    assert len(trips) == 1
    t = trips[0]
    assert t["departure_at"] == "2026-06-01T08:00:00+00:00"
    assert t["arrival_at"] == "2026-06-01T13:00:00+00:00"
    assert t["duration_seconds"] == 18000
    # MOTIS exposes `transfers` directly — use it verbatim.
    assert t["num_transfers"] == 1
    # Modes are sorted, deduped, WALK stripped (same convention as OTP).
    assert t["modes"] == "RAIL"
    assert len(t["legs"]) == 3
    # The raw itinerary is preserved under the underscore-prefixed key the UI
    # inspector + recorder.persist_trip contract already agrees on.
    assert t["_raw_itinerary"] is raw["itineraries"][0]


def test_itineraries_to_trips_derives_transfers_when_missing():
    # When MOTIS omits `transfers`, fall back to the OTP heuristic.
    raw = {
        "itineraries": [
            {
                "startTime": "2026-06-01T08:00:00Z",
                "endTime": "2026-06-01T13:00:00Z",
                "duration": 18000,
                # No `transfers` field.
                "legs": [
                    {
                        "mode": "RAIL",
                        "startTime": "2026-06-01T08:00:00Z",
                        "endTime": "2026-06-01T11:00:00Z",
                    },
                    {
                        "mode": "RAIL",
                        "startTime": "2026-06-01T11:30:00Z",
                        "endTime": "2026-06-01T13:00:00Z",
                    },
                ],
            }
        ]
    }
    assert _itineraries_to_trips(raw)[0]["num_transfers"] == 1
