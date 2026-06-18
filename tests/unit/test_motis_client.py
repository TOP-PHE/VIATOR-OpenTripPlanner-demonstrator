"""Smoke tests for the MOTIS Phase-0 client.

Covers the pure translator helpers (deterministic, easy to pin) plus the
async `fetch_plan` driven by an httpx `MockTransport` (no network) so we
exercise the URL/parameter shaping and the timezone-localisation branch
without standing up an actual MOTIS container.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from app.journey import motis_client
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


# ─────────────────────────── fetch_plan (mocked) ───────────────────────────


def _install_mock(monkeypatch, handler):
    """Wire an httpx.MockTransport into every httpx.AsyncClient `fetch_plan`
    constructs, so the test exercises the real URL/param shaping without
    requiring a live MOTIS container."""
    transport = httpx.MockTransport(handler)
    real_cls = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_cls(*args, **kwargs)

    monkeypatch.setattr(motis_client.httpx, "AsyncClient", factory)


async def test_fetch_plan_hits_motis_endpoint_with_canonical_params(monkeypatch):
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url).split("?", 1)[0]
        seen["params"] = dict(req.url.params)
        return httpx.Response(200, json={"itineraries": []})

    _install_mock(monkeypatch, handler)

    raw, trips = await motis_client.fetch_plan(
        session_id="x",
        from_lat=48.844,
        from_lon=2.374,
        to_lat=45.760,
        to_lon=4.860,
        when=datetime(2026, 6, 1, 8, 0, tzinfo=UTC),
        timeout_ms=5000,
        base_url="http://localhost:8081",
    )
    assert seen["url"] == "http://localhost:8081/api/v6/plan"
    # Coords serialise as "lat,lon" because no stop_id was supplied.
    assert seen["params"]["fromPlace"] == "48.844,2.374"
    assert seen["params"]["toPlace"] == "45.76,4.86"
    # `time` is the exact ISO instant we passed in (already tz-aware).
    assert seen["params"]["time"] == "2026-06-01T08:00:00+00:00"
    # Defaults flow through unchanged.
    assert seen["params"]["numItineraries"] == "12"
    assert seen["params"]["searchWindow"] == "21600"
    # Empty response round-trips to no trips.
    assert raw == {"itineraries": []}
    assert trips == []


async def test_fetch_plan_prefers_stop_id_over_coords(monkeypatch):
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["params"] = dict(req.url.params)
        return httpx.Response(200, json={"itineraries": []})

    _install_mock(monkeypatch, handler)
    await motis_client.fetch_plan(
        session_id="x",
        from_lat=0.0,
        from_lon=0.0,
        to_lat=0.0,
        to_lon=0.0,
        when=datetime(2026, 6, 1, 8, 0, tzinfo=UTC),
        timeout_ms=5000,
        from_stop_id="FR:StopPlace:8768600",
        to_stop_id="FR:StopPlace:8772319",
        base_url="http://localhost:8081",
    )
    # Stop ids win over the coordinate fallback (mirrors OTP's precedence).
    assert seen["params"]["fromPlace"] == "FR:StopPlace:8768600"
    assert seen["params"]["toPlace"] == "FR:StopPlace:8772319"


async def test_fetch_plan_localises_naive_when_with_session_timezone(monkeypatch):
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["time"] = req.url.params.get("time")
        return httpx.Response(200, json={"itineraries": []})

    _install_mock(monkeypatch, handler)
    await motis_client.fetch_plan(
        session_id="x",
        from_lat=0.0,
        from_lon=0.0,
        to_lat=0.0,
        to_lon=0.0,
        # Naive datetime — without session_timezone, MOTIS would see an
        # ambiguous instant. The localisation branch must attach the offset.
        when=datetime(2026, 6, 1, 8, 0),
        timeout_ms=5000,
        session_timezone="Europe/Paris",
        base_url="http://localhost:8081",
    )
    # Europe/Paris on 2026-06-01 is CEST (+02:00). The exact tz suffix
    # confirms we localised through session_timezone, not just slapped UTC on.
    assert seen["time"] is not None
    assert "+02:00" in seen["time"]


async def test_fetch_plan_unknown_timezone_falls_through_without_raising(monkeypatch, caplog):
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["time"] = req.url.params.get("time")
        return httpx.Response(200, json={"itineraries": []})

    _install_mock(monkeypatch, handler)
    await motis_client.fetch_plan(
        session_id="x",
        from_lat=0.0,
        from_lon=0.0,
        to_lat=0.0,
        to_lon=0.0,
        when=datetime(2026, 6, 1, 8, 0),
        timeout_ms=5000,
        session_timezone="Not/A_Real_Zone",
        base_url="http://localhost:8081",
    )
    # The unknown tz must NOT raise; it logs a warning and the wire `time`
    # ends up naive (no offset). MOTIS treats naive as the server's clock,
    # which is the documented fallback.
    assert seen["time"] is not None
    assert "+" not in seen["time"]  # no offset attached


async def test_fetch_plan_propagates_http_errors(monkeypatch):
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "kaboom"})

    _install_mock(monkeypatch, handler)
    try:
        await motis_client.fetch_plan(
            session_id="x",
            from_lat=0.0,
            from_lon=0.0,
            to_lat=0.0,
            to_lon=0.0,
            when=datetime(2026, 6, 1, 8, 0, tzinfo=UTC),
            timeout_ms=5000,
            base_url="http://localhost:8081",
        )
    except httpx.HTTPStatusError:
        return
    raise AssertionError("expected httpx.HTTPStatusError for a 500 response")
