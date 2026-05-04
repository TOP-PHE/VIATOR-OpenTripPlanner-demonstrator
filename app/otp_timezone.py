"""Validate the per-session `otp_timezone` config field (v0.1.21).

OTP 2.9 refuses to build a graph when its agencies declare different
timezones — e.g. SNCF says `Europe/Paris`, Eurostar says `Europe/Brussels`,
SBB says `Europe/Zurich`. The operator must pick the canonical tz for the
graph via `transitModelTimeZone` in `build-config.json`.

This module is the validation gate. The session config carries
`otp_timezone: str` (default `Europe/Paris`); the worker passes it as
`OTP_TIMEZONE` to the otp-build container; the entrypoint injects it into
the generated `build-config.json`.

We use stdlib `zoneinfo` (Python 3.9+) so no extra dep, and we get the
authoritative IANA tz database that OTP itself uses (both java.time.ZoneId
and Python's zoneinfo wrap the same tzdata files in the OS).
"""

from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Default for sessions that don't set the field. Most VIATOR demos to date
# have been French rail, so this is the lowest-friction default. Operators
# building German / Italian / Nordic sessions can override via the UI.
DEFAULT_TIMEZONE = "Europe/Paris"

# Curated list shown in the UI dropdown — covers every European country
# whose rail data has appeared in a session so far (or is likely to). The
# input field is free-text on top of the dropdown so operators can type any
# valid IANA name (e.g. "America/New_York" for an Amtrak demo) and we'll
# accept it via the live `zoneinfo` lookup below.
COMMON_TIMEZONES: list[tuple[str, str]] = [
    ("Europe/Paris", "France"),
    ("Europe/London", "United Kingdom"),
    ("Europe/Brussels", "Belgium / Eurostar admin"),
    ("Europe/Amsterdam", "Netherlands"),
    ("Europe/Berlin", "Germany / Austria-DE"),
    ("Europe/Madrid", "Spain"),
    ("Europe/Rome", "Italy"),
    ("Europe/Zurich", "Switzerland"),
    ("Europe/Vienna", "Austria"),
    ("Europe/Stockholm", "Sweden"),
    ("Europe/Oslo", "Norway"),
    ("Europe/Copenhagen", "Denmark"),
    ("Europe/Helsinki", "Finland"),
    ("Europe/Warsaw", "Poland"),
    ("Europe/Prague", "Czech Republic"),
    ("Europe/Lisbon", "Portugal"),
    ("UTC", "UTC (fallback)"),
]


def validate_timezone(value: str | None) -> str:
    """Return a validated IANA timezone string, or raise ValueError.

    Accepts None / empty → DEFAULT_TIMEZONE so legacy sessions keep working.
    Any other value must resolve via stdlib zoneinfo; the error message
    points the operator at the dropdown so they don't get stuck on typos.
    """
    if value is None or value == "":
        return DEFAULT_TIMEZONE
    if not isinstance(value, str):
        raise ValueError(
            f"otp_timezone must be a string, got {type(value).__name__}"
        )
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        # Don't dump the whole COMMON_TIMEZONES list into the error — the UI
        # already shows the dropdown. Just nudge the operator to pick from
        # there if they didn't recognise the typo.
        raise ValueError(
            f"otp_timezone={value!r} is not a valid IANA timezone "
            "(see /admin/sessions Timezone dropdown for the curated list)"
        ) from exc
    return value
