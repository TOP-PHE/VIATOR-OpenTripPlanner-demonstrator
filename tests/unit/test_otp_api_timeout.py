"""Unit tests for v0.1.24's `otp_api_timeout` validation.

The validator is the fail-fast gate for the per-session timeout dropdown.
A bad value at save time raises a 400 instead of silently falling back
to the default at the worker — operators picking '60s' in the UI for a
multi-NAP graph that times out at 10s would otherwise have no way to
tell their choice was ignored.
"""

from __future__ import annotations

import pytest

from app.otp_api_timeout import COMMON_TIMEOUTS, DEFAULT_TIMEOUT, validate_timeout

# ──────────────────────── happy path ────────────────────────


def test_default_is_30s():
    """30s is the chosen default for v0.1.24, bumped from pre-v0.1.24's
    hardcoded 10s. If this drifts, multi-NAP sessions go back to seeing
    'timeout' on Paris → Marseille searches."""
    assert DEFAULT_TIMEOUT == "30s"


def test_none_returns_default():
    """Legacy sessions (pre-v0.1.24) won't have `otp_api_timeout` set."""
    assert validate_timeout(None) == "30s"


def test_empty_string_returns_default():
    assert validate_timeout("") == "30s"


def test_explicit_default_arg_honoured():
    """Caller can override the fallback (mirrors how otp_heap works)."""
    assert validate_timeout(None, default="60s") == "60s"


@pytest.mark.parametrize(
    "value,expected",
    [
        ("10s", "10s"),
        ("30s", "30s"),
        ("60s", "60s"),
        ("120s", "120s"),
        ("2m", "2m"),
        # Case insensitivity at the unit position.
        ("30S", "30s"),
        ("2M", "2m"),
        # Whitespace gets trimmed.
        ("  30s  ", "30s"),
        ("60s\n", "60s"),
    ],
)
def test_valid_values_normalize(value, expected):
    assert validate_timeout(value) == expected


def test_all_curated_dropdown_values_validate():
    """Every entry in the UI dropdown must validate cleanly."""
    for timeout_value, _label in COMMON_TIMEOUTS:
        assert validate_timeout(timeout_value) == timeout_value


# ──────────────────────── failure modes ────────────────────────


@pytest.mark.parametrize(
    "bad_value",
    [
        "30 s",  # space between qty and unit
        "30sec",  # 'sec' suffix
        "30 seconds",  # spelled out
        "PT30S",  # ISO-8601 (deliberately unsupported — keep dropdown simple)
        "30",  # missing unit
        "s",  # missing qty
        "30.5s",  # OTP doesn't accept fractional in this form
        "30000ms",  # 'ms' deliberately not in our regex
        "-30s",  # negative
        "thirty_s",  # alphabet
    ],
)
def test_malformed_strings_rejected(bad_value):
    with pytest.raises(ValueError, match="doesn't match the expected pattern"):
        validate_timeout(bad_value)


def test_non_string_rejected():
    with pytest.raises(ValueError, match="must be a string"):
        validate_timeout(30)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="must be a string"):
        validate_timeout(True)  # type: ignore[arg-type]


def test_whitespace_only_rejected():
    with pytest.raises(ValueError):
        validate_timeout("   ")


# ─────────────────── COMMON_TIMEOUTS structure ─────────────────


def test_common_timeouts_unique():
    seen: set[str] = set()
    for value, _label in COMMON_TIMEOUTS:
        assert value not in seen, f"duplicate timeout value: {value}"
        seen.add(value)


def test_default_appears_in_common_timeouts():
    """The default must appear in the dropdown so the operator can
    discover what value they're inheriting."""
    assert any(v == DEFAULT_TIMEOUT for v, _ in COMMON_TIMEOUTS)


def test_recommended_marked_for_default():
    """Sanity: the default's label includes the word 'recommended' so
    the dropdown obviously points operators at it."""
    default_label = next(label for v, label in COMMON_TIMEOUTS if v == DEFAULT_TIMEOUT)
    assert "recommended" in default_label.lower()
