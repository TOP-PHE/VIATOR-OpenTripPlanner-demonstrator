"""Schema-level validation: types, bounds, choices, sensitivity."""

from __future__ import annotations

import pytest

from app.config_schema import (
    CONFIG_SCHEMA,
    MASK_SENTINEL,
    coerce,
    default_for,
    is_sensitive,
    serialize,
)


def test_every_key_has_a_default() -> None:
    for key, spec in CONFIG_SCHEMA.items():
        assert "default" in spec, f"{key}: missing default"
        assert spec["type"] in ("str", "int", "bool", "secret"), f"{key}: bad type {spec['type']}"


def test_smtp_pass_is_sensitive() -> None:
    assert is_sensitive("SMTP_PASS")
    assert not is_sensitive("SMTP_HOST")
    assert not is_sensitive("MAX_CONCURRENT_JOURNEYS")


@pytest.mark.parametrize("raw", ["true", "1", "yes", "on", True])
def test_bool_truthy(raw: object) -> None:
    assert coerce("REGISTRATION_OPEN", raw) is True


@pytest.mark.parametrize("raw", ["false", "0", "no", "off", False])
def test_bool_falsy(raw: object) -> None:
    assert coerce("REGISTRATION_OPEN", raw) is False


def test_bool_garbage_rejected() -> None:
    with pytest.raises(ValueError, match="expected bool"):
        coerce("REGISTRATION_OPEN", "maybe")


def test_int_within_bounds() -> None:
    assert coerce("MAX_CONCURRENT_JOURNEYS", "42") == 42


def test_int_below_min_rejected() -> None:
    with pytest.raises(ValueError, match="below minimum"):
        coerce("MAX_CONCURRENT_JOURNEYS", 0)


def test_int_above_max_rejected() -> None:
    with pytest.raises(ValueError, match="above maximum"):
        coerce("MAX_CONCURRENT_JOURNEYS", 1000)


def test_int_garbage_rejected() -> None:
    with pytest.raises(ValueError, match="expected int"):
        coerce("MAX_CONCURRENT_JOURNEYS", "lots")


def test_str_choices_enforced() -> None:
    assert coerce("SMTP_SECURE", "tls") == "tls"
    with pytest.raises(ValueError, match="must be one of"):
        coerce("SMTP_SECURE", "rot13")


def test_unknown_key_rejected() -> None:
    with pytest.raises(ValueError, match="unknown config key"):
        coerce("MADE_UP_KEY", "anything")


def test_serialize_round_trip() -> None:
    assert serialize("MAX_CONCURRENT_JOURNEYS", 30) == "30"
    assert serialize("REGISTRATION_OPEN", True) == "true"
    assert serialize("REGISTRATION_OPEN", False) == "false"
    assert serialize("SMTP_HOST", "smtp.example.com") == "smtp.example.com"
    assert serialize("SMTP_PORT", None) is None


def test_default_for() -> None:
    assert default_for("MAX_CONCURRENT_JOURNEYS") == 20
    assert default_for("REGISTRATION_OPEN") is True
    assert default_for("SMTP_FROM") == "no-reply@viator.local"


def test_mask_sentinel_constant() -> None:
    # Some routes round-trip this string; make sure it's stable.
    assert MASK_SENTINEL == "********"


def test_otp_query_depth_keys_present() -> None:
    """OTP_NUM_ITINERARIES + OTP_SEARCH_WINDOW_SECONDS expose the two
    knobs the fanout reads to size each engine's per-query search.
    If these get dropped, the journey API will KeyError at runtime."""
    assert "OTP_NUM_ITINERARIES" in CONFIG_SCHEMA
    assert "OTP_SEARCH_WINDOW_SECONDS" in CONFIG_SCHEMA


def test_otp_num_itineraries_bounds() -> None:
    assert default_for("OTP_NUM_ITINERARIES") == 12
    assert coerce("OTP_NUM_ITINERARIES", "20") == 20
    with pytest.raises(ValueError, match="below minimum"):
        coerce("OTP_NUM_ITINERARIES", 0)
    with pytest.raises(ValueError, match="above maximum"):
        coerce("OTP_NUM_ITINERARIES", 100)


def test_otp_search_window_seconds_bounds() -> None:
    # Default 21600 = 6 hours (the OTP live-UI historical value).
    assert default_for("OTP_SEARCH_WINDOW_SECONDS") == 21600
    assert coerce("OTP_SEARCH_WINDOW_SECONDS", "43200") == 43200
    # Below 600s (10 min) refuses — too tight for any useful fanout.
    with pytest.raises(ValueError, match="below minimum"):
        coerce("OTP_SEARCH_WINDOW_SECONDS", 60)
    # Above 86400s (24h) refuses — OTP RAPTOR's near-quadratic scaling
    # makes anything beyond a day unusable on dense national feeds.
    with pytest.raises(ValueError, match="above maximum"):
        coerce("OTP_SEARCH_WINDOW_SECONDS", 999999)


# ─────────────────────── coverage runner knobs (PR-2) ───────────────────────
# These seven keys are the operator-tunable equivalents of the hardcoded
# constants that used to live in `app/network_coverage/runner.py`. The
# runner's `CoverageConfig._load_coverage_config()` snapshots them at
# execute_run start; if any get renamed/dropped the runner will KeyError
# at runtime, so we pin the names + bounds here.


def test_coverage_runner_keys_present() -> None:
    """All seven COVERAGE_* keys must exist in the schema — runner's
    `_load_coverage_config` reads each one."""
    for key in (
        "COVERAGE_NUM_ITINERARIES",
        "COVERAGE_SEARCH_WINDOW_SECONDS",
        "COVERAGE_PAIR_TIMEOUT_MS",
        "COVERAGE_PAIR_PARALLELISM",
        "COVERAGE_VERIFY_PARALLELISM",
        "COVERAGE_VERIFY_TIMEOUT_S",
        "COVERAGE_VERIFY_SLEEP_MS",
    ):
        assert key in CONFIG_SCHEMA, f"{key} missing from CONFIG_SCHEMA"


def test_coverage_num_itineraries_bounds() -> None:
    # Default 50 — keeps the full-day-visibility behaviour the runner
    # had with the prior hardcoded value.
    assert default_for("COVERAGE_NUM_ITINERARIES") == 50
    assert coerce("COVERAGE_NUM_ITINERARIES", "100") == 100
    with pytest.raises(ValueError, match="below minimum"):
        coerce("COVERAGE_NUM_ITINERARIES", 0)
    with pytest.raises(ValueError, match="above maximum"):
        coerce("COVERAGE_NUM_ITINERARIES", 500)


def test_coverage_search_window_seconds_bounds() -> None:
    # Default 14400 = 4 hours (the v0.1.29.2 baseline that completed
    # cleanly on France-wide multi-NAP graphs).
    assert default_for("COVERAGE_SEARCH_WINDOW_SECONDS") == 14_400
    assert coerce("COVERAGE_SEARCH_WINDOW_SECONDS", "21600") == 21_600
    with pytest.raises(ValueError, match="below minimum"):
        coerce("COVERAGE_SEARCH_WINDOW_SECONDS", 60)
    with pytest.raises(ValueError, match="above maximum"):
        coerce("COVERAGE_SEARCH_WINDOW_SECONDS", 999_999)


def test_coverage_pair_timeout_ms_bounds() -> None:
    # Default 60_000ms = 60s — matches OTP's apiProcessingTimeout default.
    assert default_for("COVERAGE_PAIR_TIMEOUT_MS") == 60_000
    assert coerce("COVERAGE_PAIR_TIMEOUT_MS", "30000") == 30_000
    # Below 5s refuses — coverage queries are never going to finish that fast.
    with pytest.raises(ValueError, match="below minimum"):
        coerce("COVERAGE_PAIR_TIMEOUT_MS", 1_000)
    # Above 300s (5min) refuses — anything longer means OTP is hung.
    with pytest.raises(ValueError, match="above maximum"):
        coerce("COVERAGE_PAIR_TIMEOUT_MS", 600_000)


def test_coverage_pair_parallelism_bounds() -> None:
    # Default 5 — cuts wallclock ~5x vs sequential while staying well
    # below OTP's internal queue depth.
    assert default_for("COVERAGE_PAIR_PARALLELISM") == 5
    assert coerce("COVERAGE_PAIR_PARALLELISM", "10") == 10
    with pytest.raises(ValueError, match="below minimum"):
        coerce("COVERAGE_PAIR_PARALLELISM", 0)
    # Above 20 refuses — would saturate OTP and starve live journey searches.
    with pytest.raises(ValueError, match="above maximum"):
        coerce("COVERAGE_PAIR_PARALLELISM", 100)


def test_coverage_verify_parallelism_bounds() -> None:
    # Default 2 — keeps us under ÖBB HAFAS's ~1-2 req/s/IP soft cap.
    assert default_for("COVERAGE_VERIFY_PARALLELISM") == 2
    assert coerce("COVERAGE_VERIFY_PARALLELISM", "1") == 1
    with pytest.raises(ValueError, match="below minimum"):
        coerce("COVERAGE_VERIFY_PARALLELISM", 0)
    # Above 10 refuses — HAFAS would rate-limit us into oblivion.
    with pytest.raises(ValueError, match="above maximum"):
        coerce("COVERAGE_VERIFY_PARALLELISM", 50)


def test_coverage_verify_timeout_s_bounds() -> None:
    # Default 30s — generous headroom for the two-step LocGeoPos +
    # TripSearch chain.
    assert default_for("COVERAGE_VERIFY_TIMEOUT_S") == 30
    assert coerce("COVERAGE_VERIFY_TIMEOUT_S", "60") == 60
    with pytest.raises(ValueError, match="below minimum"):
        coerce("COVERAGE_VERIFY_TIMEOUT_S", 1)
    with pytest.raises(ValueError, match="above maximum"):
        coerce("COVERAGE_VERIFY_TIMEOUT_S", 999)


def test_coverage_verify_sleep_ms_bounds() -> None:
    # Default 500ms inter-call sleep inside each semaphore slot.
    assert default_for("COVERAGE_VERIFY_SLEEP_MS") == 500
    # Zero is valid — tests use it to suppress the sleep entirely.
    assert coerce("COVERAGE_VERIFY_SLEEP_MS", 0) == 0
    assert coerce("COVERAGE_VERIFY_SLEEP_MS", "1000") == 1_000
    with pytest.raises(ValueError, match="below minimum"):
        coerce("COVERAGE_VERIFY_SLEEP_MS", -1)
    with pytest.raises(ValueError, match="above maximum"):
        coerce("COVERAGE_VERIFY_SLEEP_MS", 60_000)
