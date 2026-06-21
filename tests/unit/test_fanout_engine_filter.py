"""P2 MOTIS — engine filter on /api/journey/fanout.

Pins the FanoutBody schema contract for the new `engine` field and the
default behaviour (omitted → no filter, fan out across every fanout-
enabled session regardless of engine). The endpoint-level validation
(unknown engine → 400) and SQL-level filter are exercised by the
existing integration tests once the field reaches them.
"""

from __future__ import annotations

from app.api.journey import FanoutBody


def _minimal_body(**overrides):
    """Smallest valid FanoutBody — every field beyond from/to is optional."""
    base = {
        "from": {"lat": 40.4060, "lon": -3.6905},
        "to": {"lat": 41.3791, "lon": 2.1402},
    }
    base.update(overrides)
    return FanoutBody.model_validate(base)


def test_engine_defaults_to_none_when_omitted():
    """Pre-Phase-2 callers (which don't send `engine`) keep the legacy
    'fan out across all serving fanout-enabled sessions' behaviour."""
    body = _minimal_body()
    assert body.engine is None


def test_engine_accepts_otp_explicitly():
    body = _minimal_body(engine="otp")
    assert body.engine == "otp"


def test_engine_accepts_motis_explicitly():
    body = _minimal_body(engine="motis")
    assert body.engine == "motis"


def test_engine_field_is_declared_on_model():
    """If a refactor accidentally dropped the field, Pydantic would
    silently ignore unknown keys with default config — explicit pin."""
    assert "engine" in FanoutBody.model_fields


def test_engine_field_is_optional_for_back_compat():
    """The legacy front-end + scripted clients send no `engine` at all.
    Pydantic must accept that without raising."""
    # No engine key at all (not just `engine=None`):
    body = FanoutBody.model_validate(
        {
            "from": {"lat": 0.0, "lon": 0.0},
            "to": {"lat": 0.0, "lon": 0.0},
        }
    )
    assert body.engine is None


def test_unknown_engine_string_is_accepted_at_schema_layer():
    """Schema accepts any string; endpoint-level validation rejects
    unknown engines with a 400 (see `fanout` in app/api/journey.py).
    This keeps Pydantic errors out of the user-facing flow."""
    body = _minimal_body(engine="ojp")  # not a real engine
    assert body.engine == "ojp"
