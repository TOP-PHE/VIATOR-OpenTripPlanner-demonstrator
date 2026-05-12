"""Validate the per-session `otp_serve_start_period` config field (PR #32).

The OTP serve container's healthcheck has a `start_period` grace window
during which `curl -fsS /otp/` failures don't mark the container unhealthy.
The pre-PR-#32 hardcoded value (`docker/otp/Dockerfile`: 120s) was correct
for France-wide single-feed graphs (~60-90s graph load + Raptor mapping)
but too tight for multi-network or multi-country sessions: nap-fr-rail
with FR+CH+SBB took 3-4 minutes for the Raptor mapping alone on 2026-05-11,
which tipped the container into a restart loop *despite* the heap being
fine.

Surfaced 2026-05-11 (audit-2026-05.md row #32): each restart kills the
in-progress Raptor mapping, the new JVM starts from scratch, and the cycle
never terminates. The orchestrator now writes a per-session `healthcheck.
start_period` into the generated compose fragment so operators can pick
a value matching their actual graph size.

The accepted format is plain integer seconds (no unit). Docker compose
accepts `<int>s`/`<int>m`/`<int>h` but normalising at the validator keeps
the UI dropdown values matchable.
"""

from __future__ import annotations

# Default for sessions that don't set the field. 300s (5 min) covers the
# vast majority of graph sizes we've seen — France-wide multi-NAP loads
# in ~90s, Europe-wide rail-focused in ~3 min. Stops short of the 10-min
# Docker-default for HEALTHCHECK start_period because the smaller window
# means a genuinely-stuck JVM is flagged quickly.
DEFAULT_START_PERIOD_SECONDS = 300

# Common values for the UI dropdown. Range covers from tiny IDF-scale
# sessions (30s graph load) up to all-Europe multi-modal (10 min):
#   120  — IDF / single-region (pre-PR-#32 baked default; kept for parity)
#   180  — France regional (1-2 NAPs)
#   300  — France-wide multi-NAP (recommended default)
#   480  — Europe-wide rail-focused (multi-country merge)
#   600  — Europe-wide multi-modal + cross-border constrained transfers
#   900  — last-resort cap for genuinely huge graphs / slow disk
COMMON_START_PERIODS: list[tuple[int, str]] = [
    (120, "2 min — IDF / single-region"),
    (180, "3 min — France regional (1-2 NAPs)"),
    (300, "5 min — France-wide multi-NAP (recommended)"),
    (480, "8 min — Europe-wide rail-focused"),
    (600, "10 min — Europe-wide multi-modal"),
    (900, "15 min — last-resort cap"),
]

# Hard bounds: anything below 30s is shorter than a JVM cold start on
# the smallest reasonable graph, anything above 30 min is almost
# certainly papering over a broken serve config. Both extremes get
# rejected at save time with a 400.
_MIN_SECONDS = 30
_MAX_SECONDS = 1800


def validate_start_period(
    value: int | str | None,
    *,
    default: int = DEFAULT_START_PERIOD_SECONDS,
) -> int:
    """Return a validated integer-seconds value, or raise ValueError.

    None / empty string → default. Accepts an int or a string-of-digits
    so the same validator works for form submissions (string) and DB
    reads (int).
    """
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        # Bools are ints in Python but never a sensible start_period.
        raise ValueError(f"otp_serve_start_period must be an integer, got bool {value!r}")
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return default
        try:
            value = int(stripped)
        except ValueError as exc:
            raise ValueError(
                f"otp_serve_start_period={value!r} must be an integer number of seconds"
            ) from exc
    if not isinstance(value, int):
        raise ValueError(f"otp_serve_start_period must be an int, got {type(value).__name__}")
    if value < _MIN_SECONDS:
        raise ValueError(
            f"otp_serve_start_period={value} below minimum {_MIN_SECONDS}s "
            "(JVM cold start needs at least 30s)"
        )
    if value > _MAX_SECONDS:
        raise ValueError(
            f"otp_serve_start_period={value} above maximum {_MAX_SECONDS}s "
            "(values above 30 min usually indicate a broken serve config)"
        )
    return value
