"""Stop-id routing helpers (v0.1.33).

Covers the two small pure helpers introduced when we moved the journey
search from lat/lon-only to "stop-id first, lat/lon fallback":

  - `app.api.journey._stop_id_for` — builds `<feedId>:<uic>` from a
    SessionRow and an optional UIC.
  - `app.journey.otp_client._location_not_found` — decides whether an
    OTP plan response justifies a retry with the alternate encoding.

The end-to-end behaviour (UI → API → OTP → fallback) is covered by the
integration suite; this file pins the decision logic that drives it.
"""

from __future__ import annotations

from types import SimpleNamespace


# ─────────────────── _stop_id_for ───────────────────


def _row(config: dict | None) -> SimpleNamespace:
    """Minimal SessionRow stand-in. The helper reads `.config` only."""
    return SimpleNamespace(config=config)


class TestStopIdFor:
    def test_returns_none_when_uic_missing(self):
        from app.api.journey import _stop_id_for

        sess = _row({"sources": {"providers": [{"id": "SBB"}]}})
        assert _stop_id_for(sess, None) is None
        assert _stop_id_for(sess, "") is None

    def test_returns_none_when_no_providers(self):
        from app.api.journey import _stop_id_for

        # Sessions in early lifecycle states may have an empty config
        # or no providers yet. Don't crash — just defer to lat/lon.
        assert _stop_id_for(_row(None), "8771500") is None
        assert _stop_id_for(_row({}), "8771500") is None
        assert _stop_id_for(_row({"sources": {}}), "8771500") is None
        assert _stop_id_for(_row({"sources": {"providers": []}}), "8771500") is None

    def test_builds_feed_prefix_from_first_provider(self):
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
        # First provider wins. Pontarlier (UIC 8771500) on the SBB feed:
        # the SBB GTFS uses UIC codes as stop_ids directly, so this
        # resolves cleanly inside OTP without per-feed mapping.
        assert _stop_id_for(sess, "8771500") == "SBB:8771500"

    def test_skips_malformed_provider_entries(self):
        from app.api.journey import _stop_id_for

        # An operator who saved a session via SQL might end up with
        # non-dict entries or missing ids. Don't blow up; just keep
        # walking until we find a usable id.
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


# ─────────────────── _location_not_found ───────────────────


class TestLocationNotFound:
    def test_empty_response(self):
        from app.journey.otp_client import _location_not_found

        # Defensive: completely empty payload (transport error mapped to
        # an empty dict by a defensive caller) shouldn't trigger a retry.
        assert _location_not_found({}) is False
        assert _location_not_found({"data": None}) is False
        assert _location_not_found({"data": {"plan": None}}) is False

    def test_itineraries_present_skips_retry(self):
        from app.journey.otp_client import _location_not_found

        # If OTP returned itineraries, the routingErrors list is
        # advisory only — we have a usable result, no retry needed.
        raw = {
            "data": {
                "plan": {
                    "itineraries": [{"duration": 1500, "legs": []}],
                    "routingErrors": [{"code": "LOCATION_NOT_FOUND"}],
                }
            }
        }
        assert _location_not_found(raw) is False

    def test_location_not_found_with_no_itineraries_triggers_retry(self):
        from app.journey.otp_client import _location_not_found

        raw = {
            "data": {
                "plan": {
                    "itineraries": [],
                    "routingErrors": [
                        {"code": "LOCATION_NOT_FOUND", "description": "Destination unknown."}
                    ],
                }
            }
        }
        assert _location_not_found(raw) is True

    def test_other_routing_errors_do_not_trigger_retry(self):
        from app.journey.otp_client import _location_not_found

        # NO_TRANSIT_CONNECTION etc. mean OTP routed but found nothing —
        # a lat/lon retry wouldn't change the answer. Don't waste an
        # extra round trip.
        for code in (
            "NO_TRANSIT_CONNECTION",
            "WALKING_BETTER_THAN_TRANSIT",
            "OUTSIDE_SERVICE_PERIOD",
            "SYSTEM_ERROR",
        ):
            raw = {
                "data": {
                    "plan": {
                        "itineraries": [],
                        "routingErrors": [{"code": code}],
                    }
                }
            }
            assert _location_not_found(raw) is False, f"{code} should not trigger retry"

    def test_mixed_errors_with_location_not_found_triggers_retry(self):
        from app.journey.otp_client import _location_not_found

        # If LOCATION_NOT_FOUND is in the list (regardless of order or
        # other co-occurring errors), retry is justified.
        raw = {
            "data": {
                "plan": {
                    "itineraries": [],
                    "routingErrors": [
                        {"code": "NO_TRANSIT_CONNECTION"},
                        {"code": "LOCATION_NOT_FOUND"},
                    ],
                }
            }
        }
        assert _location_not_found(raw) is True
