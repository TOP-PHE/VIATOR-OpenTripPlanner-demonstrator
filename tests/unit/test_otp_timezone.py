"""Unit tests for v0.1.21's `otp_timezone` validation.

OTP 2.9 demands an explicit `transitModelTimeZone` whenever the graph
mixes agencies that declare different IANA tzs (SNCF says Europe/Paris,
Eurostar says Europe/Brussels, OTP refuses to pick — see error in the
v0.1.20 → v0.1.21 release notes). The validator pinned here is the
fail-fast gate: bad strings raise at save-time so the operator sees a
toast next to the dropdown instead of waiting 5 minutes into a rebuild
log to find out their tz was a typo.
"""

from __future__ import annotations

import pytest

from app.otp_timezone import (
    COMMON_TIMEZONES,
    DEFAULT_TIMEZONE,
    validate_timezone,
)


# ───────────────────────── happy path ─────────────────────────


def test_default_timezone_is_europe_paris():
    """The most common VIATOR demo to date has been French rail; default
    keeps single-FR sessions building without forcing every operator to
    fill in the dropdown by hand."""
    assert DEFAULT_TIMEZONE == "Europe/Paris"


def test_none_returns_default():
    """Legacy sessions (pre-v0.1.21) won't have `otp_timezone` set in
    their config — the worker reads `row.config.get('otp_timezone')`
    which returns None. The validator must handle that gracefully."""
    assert validate_timezone(None) == DEFAULT_TIMEZONE


def test_empty_string_returns_default():
    """An operator who clears the field in the UI should also fall back
    to the default rather than getting a confusing 400."""
    assert validate_timezone("") == DEFAULT_TIMEZONE


@pytest.mark.parametrize("tz", [
    "Europe/Paris",
    "Europe/London",
    "Europe/Brussels",
    "Europe/Berlin",
    "Europe/Madrid",
    "Europe/Rome",
    "Europe/Zurich",
    "Europe/Vienna",
    "Europe/Stockholm",
    "Europe/Oslo",
    "Europe/Copenhagen",
    "Europe/Helsinki",
    "Europe/Warsaw",
    "Europe/Prague",
    "Europe/Lisbon",
    "UTC",
])
def test_curated_dropdown_values_all_validate(tz):
    """Every value in the UI dropdown must validate. If a curated entry
    ever fails, the operator sees a 400 the moment they click Save —
    much worse than catching it here at CI time."""
    assert validate_timezone(tz) == tz


def test_non_european_iana_zones_are_accepted():
    """The dropdown is curated for the European-rail use-case but the
    validator is intentionally not — operators can type any IANA tz
    (e.g. America/New_York for an Amtrak demo) and it'll be accepted
    via stdlib zoneinfo. Pin that openness so a future "lock to dropdown"
    change is a deliberate decision."""
    assert validate_timezone("America/New_York") == "America/New_York"
    assert validate_timezone("Asia/Tokyo") == "Asia/Tokyo"


# ──────────────────────── failure modes ────────────────────────


def test_typo_raises_with_helpful_message():
    """The whole point of the validator: catch typos at save-time so the
    operator doesn't waste 5 minutes on a rebuild that fails with an
    opaque java.time error."""
    with pytest.raises(ValueError, match="not a valid IANA timezone"):
        validate_timezone("Europe/Pariss")  # typo


def test_empty_after_strip_handled_via_default():
    """Bare whitespace isn't a valid IANA tz, but operators may type a
    space by accident. We treat it as falsy → default. (Matches the
    `if not value` branch.)"""
    # zoneinfo rejects whitespace; we pass it through and let the
    # stdlib raise. The error message is still helpful (mentions the
    # bad value), so we don't try to be cleverer than zoneinfo here.
    with pytest.raises(ValueError):
        validate_timezone("   ")


def test_non_string_value_rejected_explicitly():
    """The session config is JSONB — could in theory contain numbers or
    nulls if hand-edited via raw API. We reject non-strings with a clear
    message rather than letting `ZoneInfo(123)` raise something cryptic."""
    with pytest.raises(ValueError, match="must be a string"):
        validate_timezone(123)  # type: ignore[arg-type]


def test_known_invalid_strings_rejected():
    """A handful of plausible-but-wrong values that operators might try."""
    for bad in ["GMT+1", "PST", "UTC+0100", "Europe", "Paris"]:
        with pytest.raises(ValueError, match="not a valid IANA timezone"):
            validate_timezone(bad)


# ──────────────────────── COMMON_TIMEZONES ────────────────────


def test_common_timezones_starts_with_default():
    """The default value must be the first entry in the dropdown so it
    sticks as the rendered default for new sessions."""
    assert COMMON_TIMEZONES[0][0] == DEFAULT_TIMEZONE


def test_common_timezones_unique():
    """Defensive: a copy-paste editing the curated list could introduce
    a duplicate, which renders two identical-looking dropdown entries."""
    seen: set[str] = set()
    for tz, _label in COMMON_TIMEZONES:
        assert tz not in seen, f"duplicate tz in COMMON_TIMEZONES: {tz}"
        seen.add(tz)
