"""P2 MOTIS — engine filter on /api/journey/fanout.

Two layers pinned here:
  * `FanoutBody` schema contract — optional field, default None, accepts
    both engine values; back-compat for callers that omit it entirely.
  * `_validate_engine_filter` / `_no_serving_sessions_message` /
    `_select_fanout_sessions` — pure helpers extracted from the route so
    the validation + SQL-builder shape are testable without spinning up
    FastAPI + Postgres + auth. The route's actual `db.execute` and
    `recorder.begin_search` stay covered by the existing integration
    tests.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api.journey import (
    FanoutBody,
    _no_serving_sessions_message,
    _select_fanout_sessions,
    _validate_engine_filter,
)


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
    unknown engines with a 400 (see `_validate_engine_filter` below).
    This keeps Pydantic errors out of the user-facing flow."""
    body = _minimal_body(engine="ojp")  # not a real engine
    assert body.engine == "ojp"


# ─────────────────────── _validate_engine_filter ───────────────────────


def test_validate_engine_filter_passes_none():
    """None = no filter; never raises."""
    _validate_engine_filter(None)


def test_validate_engine_filter_passes_otp_and_motis():
    """Each value in the SessionEngine enum is accepted verbatim."""
    _validate_engine_filter("otp")
    _validate_engine_filter("motis")


def test_validate_engine_filter_rejects_unknown_with_400():
    """Unknown engine → HTTPException(400) with a list of valid values
    in the detail so operators can self-correct."""
    with pytest.raises(HTTPException) as exc:
        _validate_engine_filter("ojp")
    assert exc.value.status_code == 400
    assert "Invalid engine" in str(exc.value.detail)
    # Detail mentions both valid values so the operator doesn't guess:
    assert "otp" in str(exc.value.detail)
    assert "motis" in str(exc.value.detail)


def test_validate_engine_filter_is_case_sensitive():
    """'OTP' / 'Motis' / etc. are NOT accepted — the SessionEngine enum
    values are lower-case by design. Catches a class of typo issues."""
    with pytest.raises(HTTPException) as exc:
        _validate_engine_filter("OTP")
    assert exc.value.status_code == 400


# ────────────────────── _no_serving_sessions_message ──────────────────────


def test_no_serving_message_without_engine_filter():
    """Pre-Phase-2 message — surfaces 'no fanout sessions at all'."""
    msg = _no_serving_sessions_message(None)
    assert "fanout" in msg
    assert "engine" not in msg  # don't mention filter when none was applied


def test_no_serving_message_with_engine_filter_names_the_engine():
    """With a filter, the message must explicitly name which engine
    matched zero — so the operator knows whether to relax the filter
    or onboard a session for that engine."""
    msg = _no_serving_sessions_message("motis")
    assert "motis" in msg
    assert "engine" in msg.lower()


# ────────────────────── _select_fanout_sessions ──────────────────────


def test_select_fanout_sessions_passes_through_engine_filter():
    """Builds a SELECT with the engine WHERE clause when engine is set,
    omits it when None. Uses a fake `db.execute` so we capture the
    compiled SQL string without needing Postgres."""

    captured: dict = {}

    class _FakeScalars:
        def all(self):
            return []

    class _FakeResult:
        def scalars(self):
            return _FakeScalars()

    class _FakeDb:
        def execute(self, stmt):
            captured["sql"] = str(stmt.compile(compile_kwargs={"literal_binds": True}))
            return _FakeResult()

    # No engine filter -> SQL must NOT mention "engine =":
    _select_fanout_sessions(_FakeDb(), None)
    assert "engine" not in captured["sql"].lower().split("where")[1]

    # With engine filter -> SQL DOES carry the engine clause:
    _select_fanout_sessions(_FakeDb(), "motis")
    where_clause = captured["sql"].lower().split("where")[1]
    assert "engine" in where_clause
    assert "motis" in where_clause
