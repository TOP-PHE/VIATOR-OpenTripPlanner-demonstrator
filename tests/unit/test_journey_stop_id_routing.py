"""Stop-id routing — planConnection (v0.1.34).

Covers the pure helpers behind "route by transit stop, fall back to
coordinates":

  app.api.journey
    _primary_feed_id    — first provider id from session.config
    _stop_id_for        — builds `<feedId>:<uic>`
    _session_timezone   — session's OTP timezone, if any

  app.journey.otp_client
    _location_not_found — should a stop-id attempt retry with coords?
    _earliest_departure — naive datetime → ISO-8601 OffsetDateTime
    _plan_location      — PlanLabeledLocationInput (stop vs coordinate)
    _safe_log_token     — sanitise user-influenced stop_ids for logging
    _iso_to_utc_iso     — itinerary start/end ISO → UTC ISO
    _normalise          — planConnection edges[].node → recorder trips

The data feeding TestNormalisePlanConnection is the verbatim response
captured from the live OTP 2.9 nap-ch-rail session for the RE9
Pontarlier→Travers query — so this test pins `_normalise` against real
OTP output, not a guess at the shape.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

# ─────────────────── app.api.journey helpers ───────────────────


def _row(config: dict | None) -> SimpleNamespace:
    """Minimal SessionRow stand-in — the helpers read `.config` only."""
    return SimpleNamespace(config=config)


class TestPrimaryFeedIdAndStopIdFor:
    def test_stop_id_none_when_uic_missing(self):
        from app.api.journey import _stop_id_for

        sess = _row({"sources": {"providers": [{"id": "SBB"}]}})
        assert _stop_id_for(sess, None) is None
        assert _stop_id_for(sess, "") is None

    def test_stop_id_none_when_no_providers(self):
        from app.api.journey import _stop_id_for

        # Early-lifecycle sessions may have empty/missing config.
        assert _stop_id_for(_row(None), "8771500") is None
        assert _stop_id_for(_row({}), "8771500") is None
        assert _stop_id_for(_row({"sources": {}}), "8771500") is None
        assert _stop_id_for(_row({"sources": {"providers": []}}), "8771500") is None

    def test_stop_id_built_from_first_provider(self):
        from app.api.journey import _stop_id_for

        sess = _row(
            {
                "sources": {
                    "providers": [
                        {"id": "SBB", "label": "Swiss Federal Railways"},
                        {"id": "OEBB", "label": "Austrian Federal Railways"},
                    ]
                }
            }
        )
        # SBB GTFS keys stops by UIC, so SBB:8771500 resolves to
        # Pontarlier directly with no feed-specific mapping.
        assert _stop_id_for(sess, "8771500") == "SBB:8771500"

    def test_primary_feed_id_skips_malformed_entries(self):
        from app.api.journey import _stop_id_for

        # A session saved via SQL might carry non-dict entries or
        # missing ids — keep walking until a usable id is found.
        sess = _row(
            {
                "sources": {
                    "providers": [
                        "not-a-dict",
                        {"label": "no id"},
                        {"id": ""},
                        {"id": "SBB"},
                    ]
                }
            }
        )
        assert _stop_id_for(sess, "8771500") == "SBB:8771500"


class TestSessionTimezone:
    def test_returns_configured_tz(self):
        from app.api.journey import _session_timezone

        assert _session_timezone(_row({"otp_timezone": "Europe/Zurich"})) == "Europe/Zurich"

    def test_none_when_absent_or_blank(self):
        from app.api.journey import _session_timezone

        assert _session_timezone(_row(None)) is None
        assert _session_timezone(_row({})) is None
        assert _session_timezone(_row({"otp_timezone": ""})) is None
        assert _session_timezone(_row({"otp_timezone": 123})) is None  # type: ignore[dict-item]


# ─────────────────── _location_not_found ───────────────────


class TestLocationNotFound:
    def test_empty_response(self):
        from app.journey.otp_client import _location_not_found

        assert _location_not_found({}) is False
        assert _location_not_found({"data": None}) is False
        assert _location_not_found({"data": {"planConnection": None}}) is False

    def test_edges_present_skips_retry(self):
        from app.journey.otp_client import _location_not_found

        raw = {
            "data": {
                "planConnection": {
                    "edges": [{"node": {"start": "2026-05-18T11:03:00+02:00"}}],
                    "routingErrors": [{"code": "LOCATION_NOT_FOUND"}],
                }
            }
        }
        assert _location_not_found(raw) is False

    def test_location_not_found_with_no_edges_triggers_retry(self):
        from app.journey.otp_client import _location_not_found

        # The exact bad-stop-id shape verified against live OTP 2.9.
        raw = {
            "data": {
                "planConnection": {
                    "edges": [],
                    "routingErrors": [
                        {"code": "LOCATION_NOT_FOUND", "description": "Origin is unknown."}
                    ],
                }
            }
        }
        assert _location_not_found(raw) is True

    def test_other_routing_errors_do_not_trigger_retry(self):
        from app.journey.otp_client import _location_not_found

        # NO_TRANSIT_CONNECTION_IN_SEARCH_WINDOW etc. mean OTP located
        # both endpoints and routed — a coordinate retry of the same
        # endpoints wouldn't change the answer.
        for code in (
            "NO_TRANSIT_CONNECTION_IN_SEARCH_WINDOW",
            "WALKING_BETTER_THAN_TRANSIT",
            "OUTSIDE_SERVICE_PERIOD",
            "SYSTEM_ERROR",
        ):
            raw = {
                "data": {"planConnection": {"edges": [], "routingErrors": [{"code": code}]}}
            }
            assert _location_not_found(raw) is False, f"{code} should not retry"

    def test_mixed_errors_with_location_not_found_triggers_retry(self):
        from app.journey.otp_client import _location_not_found

        raw = {
            "data": {
                "planConnection": {
                    "edges": [],
                    "routingErrors": [
                        {"code": "NO_TRANSIT_CONNECTION_IN_SEARCH_WINDOW"},
                        {"code": "LOCATION_NOT_FOUND"},
                    ],
                }
            }
        }
        assert _location_not_found(raw) is True


# ─────────────────── _earliest_departure ───────────────────


class TestEarliestDeparture:
    def test_naive_localised_to_session_tz(self):
        from app.journey.otp_client import _earliest_departure

        # The UI's datetime-local input is naive. With the CH session's
        # timezone it should become a +02:00 (CEST) offset in May.
        naive = datetime(2026, 5, 18, 11, 3, 0)
        out = _earliest_departure(naive, "Europe/Zurich")
        assert out == "2026-05-18T11:03:00+02:00"

    def test_naive_without_tz_falls_back_to_utc(self):
        from app.journey.otp_client import _earliest_departure

        naive = datetime(2026, 5, 18, 11, 3, 0)
        assert _earliest_departure(naive, None) == "2026-05-18T11:03:00+00:00"

    def test_naive_with_invalid_tz_falls_back_to_utc(self):
        from app.journey.otp_client import _earliest_departure

        naive = datetime(2026, 5, 18, 11, 3, 0)
        assert _earliest_departure(naive, "Not/AZone") == "2026-05-18T11:03:00+00:00"

    def test_aware_datetime_used_as_is(self):
        from app.journey.otp_client import _earliest_departure

        aware = datetime(2026, 5, 18, 9, 3, 0, tzinfo=UTC)
        # Already has an offset — session tz must not override it.
        assert _earliest_departure(aware, "Europe/Zurich") == "2026-05-18T09:03:00+00:00"


# ─────────────────── _plan_location ───────────────────


class TestPlanLocation:
    def test_stop_id_builds_stop_location(self):
        from app.journey.otp_client import _plan_location

        assert _plan_location("SBB:8771500", 46.9, 6.35) == {
            "location": {"stopLocation": {"stopLocationId": "SBB:8771500"}}
        }

    def test_no_stop_id_builds_coordinate(self):
        from app.journey.otp_client import _plan_location

        assert _plan_location(None, 46.9, 6.35) == {
            "location": {"coordinate": {"latitude": 46.9, "longitude": 6.35}}
        }


# ─────────────────── _safe_log_token ───────────────────


class TestSafeLogToken:
    def test_empty_and_none(self):
        from app.journey.otp_client import _safe_log_token

        assert _safe_log_token(None) == "-"
        assert _safe_log_token("") == "-"

    def test_clean_stop_id_passes_through(self):
        from app.journey.otp_client import _safe_log_token

        assert _safe_log_token("SBB:8771500") == "SBB:8771500"
        assert _safe_log_token("SNCF:OCETrain-87271007") == "SNCF:OCETrain-87271007"

    def test_strips_newlines_and_control_chars(self):
        from app.journey.otp_client import _safe_log_token

        out = _safe_log_token("SBB:8771500\nINFO fake log line")
        assert "\n" not in out
        assert out == "SBB:8771500?INFO?fake?log?line"

    def test_truncates_long_values(self):
        from app.journey.otp_client import _safe_log_token

        assert len(_safe_log_token("A" * 500)) == 64


# ─────────────────── _iso_to_utc_iso ───────────────────


class TestIsoToUtcIso:
    def test_offset_converted_to_utc(self):
        from app.journey.otp_client import _iso_to_utc_iso

        assert _iso_to_utc_iso("2026-05-18T11:03:00+02:00") == "2026-05-18T09:03:00+00:00"

    def test_naive_assumed_utc(self):
        from app.journey.otp_client import _iso_to_utc_iso

        assert _iso_to_utc_iso("2026-05-18T09:03:00") == "2026-05-18T09:03:00+00:00"

    def test_none_and_garbage(self):
        from app.journey.otp_client import _iso_to_utc_iso

        assert _iso_to_utc_iso(None) is None
        assert _iso_to_utc_iso("") is None
        assert _iso_to_utc_iso("not-a-date") is None
        assert _iso_to_utc_iso(12345) is None  # type: ignore[arg-type]


# ─────────────────── _normalise (planConnection) ───────────────────


# Verbatim response captured from the live OTP 2.9 nap-ch-rail session
# for: planConnection Pontarlier(SBB:Parent8771500) → Travers
# (SBB:Parent8504215), 2026-05-18 — the RE9 11:03→11:28 service.
_VERIFIED_PLANCONNECTION = {
    "data": {
        "planConnection": {
            "edges": [
                {
                    "node": {
                        "start": "2026-05-18T11:03:00+02:00",
                        "end": "2026-05-18T11:28:00+02:00",
                        "duration": 1500,
                        "legs": [
                            {
                                "mode": "RAIL",
                                "startTime": 1779094980000,
                                "endTime": 1779096480000,
                                "duration": 1500.0,
                                "distance": 24873.63,
                                "from": {
                                    "name": "Pontarlier",
                                    "lat": 46.9005968,
                                    "lon": 6.3533169,
                                    "stop": {"gtfsId": "SBB:8771500"},
                                },
                                "to": {
                                    "name": "Travers",
                                    "lat": 46.9420299,
                                    "lon": 6.6751653,
                                    "stop": {"gtfsId": "SBB:8504215:0:2"},
                                },
                                "route": {
                                    "gtfsId": "SBB:91-9-N-j26-1",
                                    "shortName": "RE9",
                                    "longName": None,
                                    "agency": {
                                        "gtfsId": "SBB:11",
                                        "name": "Schweizerische Bundesbahnen SBB",
                                        "url": "https://sbb.ch",
                                    },
                                },
                                "trip": {
                                    "gtfsId": "SBB:.ojp-91-9-N.1.TA.17.j26",
                                    "tripHeadsign": "Neuchâtel",
                                },
                            }
                        ],
                    }
                }
            ],
            "routingErrors": [],
        }
    }
}


class TestNormalisePlanConnection:
    def test_verified_response_shape(self):
        from app.journey.otp_client import _normalise

        trips = _normalise(_VERIFIED_PLANCONNECTION)
        assert len(trips) == 1
        trip = trips[0]

        # Itinerary node: duration already in seconds; start/end are
        # ISO-8601 with offset, converted to UTC ISO here.
        assert trip["duration_seconds"] == 1500
        assert trip["departure_at"] == "2026-05-18T09:03:00+00:00"
        assert trip["arrival_at"] == "2026-05-18T09:28:00+00:00"
        assert trip["num_transfers"] == 0  # single RAIL leg
        assert trip["modes"] == "RAIL"
        assert trip["_raw_itinerary"] == _VERIFIED_PLANCONNECTION["data"][
            "planConnection"
        ]["edges"][0]["node"]

        # Single leg — leg times are epoch-ms, same as the legacy API.
        assert len(trip["legs"]) == 1
        leg = trip["legs"][0]
        assert leg["mode"] == "RAIL"
        assert leg["departure"] == "2026-05-18T09:03:00+00:00"
        assert leg["arrival"] == "2026-05-18T09:28:00+00:00"
        assert leg["duration_seconds"] == 1500
        assert leg["distance_meters"] == 24873.63
        assert leg["from_name"] == "Pontarlier"
        assert leg["from_stop_id"] == "SBB:8771500"
        assert leg["to_name"] == "Travers"
        assert leg["to_stop_id"] == "SBB:8504215:0:2"
        assert leg["route_short_name"] == "RE9"
        assert leg["route_id"] == "SBB:91-9-N-j26-1"
        assert leg["agency_name"] == "Schweizerische Bundesbahnen SBB"
        assert leg["agency_id"] == "SBB:11"
        # feed_id is derived from the trip.gtfsId prefix.
        assert leg["feed_id"] == "SBB"
        assert leg["trip_id"] == "SBB:.ojp-91-9-N.1.TA.17.j26"
        assert leg["trip_headsign"] == "Neuchâtel"

    def test_empty_edges(self):
        from app.journey.otp_client import _normalise

        raw = {"data": {"planConnection": {"edges": [], "routingErrors": []}}}
        assert _normalise(raw) == []

    def test_missing_data_is_safe(self):
        from app.journey.otp_client import _normalise

        assert _normalise({}) == []
        assert _normalise({"data": None}) == []
        assert _normalise({"data": {"planConnection": None}}) == []
        # A top-level GraphQL `errors` payload (no `data`) degrades to
        # an empty trip list rather than raising.
        assert _normalise({"errors": [{"message": "boom"}]}) == []

    def test_multi_leg_transfer_count(self):
        from app.journey.otp_client import _normalise

        raw = {
            "data": {
                "planConnection": {
                    "edges": [
                        {
                            "node": {
                                "start": "2026-05-18T08:00:00+02:00",
                                "end": "2026-05-18T10:00:00+02:00",
                                "duration": 7200,
                                "legs": [
                                    {"mode": "WALK", "startTime": 1, "endTime": 2},
                                    {"mode": "RAIL", "startTime": 3, "endTime": 4},
                                    {"mode": "RAIL", "startTime": 5, "endTime": 6},
                                    {"mode": "WALK", "startTime": 7, "endTime": 8},
                                ],
                            }
                        }
                    ],
                    "routingErrors": [],
                }
            }
        }
        trip = _normalise(raw)[0]
        # 2 non-walk legs → 1 transfer; WALK legs don't count.
        assert trip["num_transfers"] == 1
        assert trip["modes"] == "RAIL,WALK"
