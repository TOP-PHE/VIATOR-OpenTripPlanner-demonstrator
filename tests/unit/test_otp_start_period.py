"""Unit tests for PR #32's `otp_serve_start_period` validator.

The validator gates the per-session healthcheck `start_period` dropdown.
A bad value at save time raises a 400 instead of silently writing junk
that confuses the orchestrator when render_compose interpolates it.

Surfaced 2026-05-11: nap-fr-rail with SBB included needed ~4 min for
Raptor mapping but the hardcoded 120s start_period in the Dockerfile
flagged it unhealthy after ~3 min, triggering a restart loop. This
validator + the per-session UI dropdown are the structural fix.
"""

from __future__ import annotations

import pytest

from app.otp_start_period import (
    COMMON_START_PERIODS,
    DEFAULT_START_PERIOD_SECONDS,
    validate_start_period,
)

# ──────────────────────── happy path ────────────────────────


def test_default_is_300_seconds():
    """5 min covers most multi-NAP sessions without flagging genuinely-stuck
    JVMs slowly. If this changes, update the UI dropdown's 'recommended'
    annotation in app/templates/admin/sessions.html too."""
    assert DEFAULT_START_PERIOD_SECONDS == 300


def test_none_returns_default():
    """Legacy sessions (pre-PR-#32) won't have the field set."""
    assert validate_start_period(None) == 300


def test_empty_string_returns_default():
    """The UI's 'Default' option submits an empty string — the orchestrator
    should fall back to the default rather than 400."""
    assert validate_start_period("") == 300


def test_whitespace_only_string_returns_default():
    """Belt-and-braces: whitespace-only input is also empty for our purposes."""
    assert validate_start_period("   ") == 300


def test_explicit_default_arg_is_honoured():
    """Caller can override the fallback — useful for unit tests and for
    operators who want to bake a longer default into a deployment."""
    assert validate_start_period(None, default=600) == 600
    assert validate_start_period("", default=480) == 480


@pytest.mark.parametrize(
    "value,expected",
    [
        # Plain int input (typical DB read).
        (120, 120),
        (300, 300),
        (900, 900),
        # String input (typical form submission).
        ("120", 120),
        ("300", 300),
        ("900", 900),
        # Whitespace gets trimmed.
        ("  180  ", 180),
        ("180\n", 180),
    ],
)
def test_valid_values_pass_through(value, expected):
    assert validate_start_period(value) == expected


def test_every_ui_dropdown_value_validates():
    """Every value in the UI dropdown should round-trip through the validator
    unchanged — otherwise the operator's choice gets silently rewritten."""
    for seconds, _label in COMMON_START_PERIODS:
        assert validate_start_period(seconds) == seconds
        assert validate_start_period(str(seconds)) == seconds


# ──────────────────────── rejection ────────────────────────


def test_below_minimum_rejected():
    """30s floor: anything shorter is less than a JVM cold start on the
    smallest reasonable graph. Allowing it just guarantees instant
    unhealthy flagging."""
    with pytest.raises(ValueError, match="below minimum"):
        validate_start_period(29)
    with pytest.raises(ValueError, match="below minimum"):
        validate_start_period(0)
    with pytest.raises(ValueError, match="below minimum"):
        validate_start_period(-100)


def test_above_maximum_rejected():
    """30 min cap: anything longer almost certainly indicates a broken
    serve config rather than a genuinely slow graph. The user gets a
    400 with a hint instead of waiting half an hour for diagnosis."""
    with pytest.raises(ValueError, match="above maximum"):
        validate_start_period(1801)
    with pytest.raises(ValueError, match="above maximum"):
        validate_start_period(3600)


def test_non_integer_string_rejected():
    """A string that doesn't parse to int gets a clear error rather than
    silently coercing to 0 or default."""
    with pytest.raises(ValueError, match="must be an integer"):
        validate_start_period("five minutes")
    with pytest.raises(ValueError, match="must be an integer"):
        validate_start_period("300s")  # we accept ints; suffix isn't part of the format
    with pytest.raises(ValueError, match="must be an integer"):
        validate_start_period("5m")


def test_bool_rejected():
    """Bools are ints in Python (True == 1, False == 0) but never a sensible
    start_period — reject explicitly with a clear message rather than letting
    True silently become 1 (and then below-minimum)."""
    with pytest.raises(ValueError, match="got bool"):
        validate_start_period(True)
    with pytest.raises(ValueError, match="got bool"):
        validate_start_period(False)


def test_unsupported_type_rejected():
    """Defensive: a caller passing the wrong type (e.g. a float, dict, list)
    should fail fast with a clear error rather than producing junk output."""
    with pytest.raises(ValueError, match="must be an int"):
        validate_start_period(300.5)  # type: ignore[arg-type]
