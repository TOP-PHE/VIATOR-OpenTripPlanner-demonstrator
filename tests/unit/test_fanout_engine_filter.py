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


# ────────────── feat/hafas-journey-comparison — toggle wiring ─────────────
# Pin the FanoutBody contract for the second reference engine. Same
# discipline as the OJP `compare_ojp` field above.


def test_compare_hafas_defaults_false_for_back_compat():
    """Pre-PR callers don't send `compare_hafas`. Default False keeps
    the standard search fast (no surprise extra HTTP round-trip)."""
    body = _minimal_body()
    assert body.compare_hafas is False


def test_compare_hafas_accepts_true():
    body = _minimal_body(compare_hafas=True)
    assert body.compare_hafas is True


def test_compare_hafas_field_is_declared_on_model():
    """A future refactor that drops the field would silently make the
    operator's checkbox a no-op (Pydantic ignores unknown keys with
    default config). Explicit pin."""
    assert "compare_hafas" in FanoutBody.model_fields


def test_compare_ojp_and_compare_hafas_are_independent():
    """Operators can opt into either reference engine on its own, or
    both at once — they're not mutually exclusive at the schema level."""
    both = _minimal_body(compare_ojp=True, compare_hafas=True)
    assert both.compare_ojp is True
    assert both.compare_hafas is True

    neither = _minimal_body()
    assert neither.compare_ojp is False
    assert neither.compare_hafas is False


# ────────────── feat/hafas-journey-comparison — _query_hafas_reference ─────────────
# The integration helper that runs HAFAS in parallel with the OTP fanout.
# Mirrors the existing _query_ojp_reference pattern.


@pytest.mark.asyncio
async def test_query_hafas_reference_returns_ok_with_trips_on_success():
    """Happy path: HAFAS adapter returns trips, the helper packages
    them into the `{status, trips, response_ms}` shape the journey API
    response payload uses."""
    from unittest.mock import AsyncMock, patch

    from app.api.journey import _query_hafas_reference

    body = _minimal_body(compare_hafas=True)
    fake_trips = [
        {
            "duration_seconds": 9900,
            "num_transfers": 0,
            "departure_at": "2026-06-28T08:00:00+00:00",
            "arrival_at": "2026-06-28T10:45:00+00:00",
            "modes": "RAIL",
            "legs": [],
            "first_transit_leg_departure_utc": "2026-06-28T08:00:00+00:00",
        }
    ]
    fake_raw = {"status": "ok", "format": "hafas-mgate", "response_ms": 123}
    fake_fetch = AsyncMock(return_value=(fake_raw, fake_trips))

    cfg = {"HAFAS_TIMEOUT_MS": 10_000}
    from datetime import UTC, datetime

    when = datetime(2026, 6, 28, 8, 0, 0, tzinfo=UTC)
    with patch("app.api.journey.hafas_client.fetch_plan", fake_fetch):
        result = await _query_hafas_reference(cfg, body, when)
    assert result["status"] == "ok"
    assert result["trips"] == fake_trips
    assert isinstance(result["response_ms"], int)
    assert "error" not in result


@pytest.mark.asyncio
async def test_query_hafas_reference_surfaces_no_route_status():
    """HAFAS adapter returns `status='no_route'` cleanly — the helper
    passes it through so the panel renders the distinct "no itinerary
    found" state."""
    from unittest.mock import AsyncMock, patch

    from app.api.journey import _query_hafas_reference

    body = _minimal_body(compare_hafas=True)
    fake_fetch = AsyncMock(return_value=({"status": "no_route"}, []))
    from datetime import UTC, datetime

    when = datetime(2026, 6, 28, 8, 0, 0, tzinfo=UTC)
    with patch("app.api.journey.hafas_client.fetch_plan", fake_fetch):
        result = await _query_hafas_reference({"HAFAS_TIMEOUT_MS": 10_000}, body, when)
    assert result["status"] == "no_route"
    assert result["trips"] == []


@pytest.mark.asyncio
async def test_query_hafas_reference_surfaces_error_with_message():
    """HAFAS adapter returns `status='error'` + an `error` field — the
    helper must lift both into the response so the panel renders the
    yellow "unavailable" state with the diagnostic."""
    from unittest.mock import AsyncMock, patch

    from app.api.journey import _query_hafas_reference

    body = _minimal_body(compare_hafas=True)
    fake_fetch = AsyncMock(return_value=({"status": "error", "error": "backend down"}, []))
    from datetime import UTC, datetime

    when = datetime(2026, 6, 28, 8, 0, 0, tzinfo=UTC)
    with patch("app.api.journey.hafas_client.fetch_plan", fake_fetch):
        result = await _query_hafas_reference({"HAFAS_TIMEOUT_MS": 10_000}, body, when)
    assert result["status"] == "error"
    assert result["error"] == "backend down"


@pytest.mark.asyncio
async def test_query_hafas_reference_never_raises_on_httpx_error():
    """A failing reference call must NOT affect VIATOR's own results —
    the journey API wires this with `await ... if hafas_task ...` so
    a raise here would bubble out as a 500. Defence-in-depth: even if
    the adapter regresses and lets an httpx.HTTPError escape, the
    helper converts to status='error'."""
    from unittest.mock import AsyncMock, patch

    import httpx

    from app.api.journey import _query_hafas_reference

    body = _minimal_body(compare_hafas=True)
    fake_fetch = AsyncMock(side_effect=httpx.ConnectError("simulated"))
    from datetime import UTC, datetime

    when = datetime(2026, 6, 28, 8, 0, 0, tzinfo=UTC)
    with patch("app.api.journey.hafas_client.fetch_plan", fake_fetch):
        result = await _query_hafas_reference({"HAFAS_TIMEOUT_MS": 10_000}, body, when)
    assert result["status"] == "error"
    assert "HAFAS request failed" in result["error"]


@pytest.mark.asyncio
async def test_query_hafas_reference_never_raises_on_timeout():
    """Timeout exception → status='timeout' (matches OJP path's
    treatment so the journey UI can render the same colour-coded
    "timed out" state on either engine)."""
    from unittest.mock import AsyncMock, patch

    import httpx

    from app.api.journey import _query_hafas_reference

    body = _minimal_body(compare_hafas=True)
    fake_fetch = AsyncMock(side_effect=httpx.TimeoutException("slow"))
    from datetime import UTC, datetime

    when = datetime(2026, 6, 28, 8, 0, 0, tzinfo=UTC)
    with patch("app.api.journey.hafas_client.fetch_plan", fake_fetch):
        result = await _query_hafas_reference({"HAFAS_TIMEOUT_MS": 10_000}, body, when)
    assert result["status"] == "timeout"
    assert result["trips"] == []
