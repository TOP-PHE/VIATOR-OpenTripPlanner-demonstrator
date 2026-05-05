"""Validate the per-session `otp_api_timeout` config field (v0.1.24).

OTP's `server.apiProcessingTimeout` caps how long a single journey-search
request can run. Pre-v0.1.24 we hardcoded "10s", which works for small
single-feed sessions but **silently times out** larger graphs:

  Paris GdL → Marseille on a 13-provider France-wide graph
  → "0 trips in 10036ms (timeout)"

The graph isn't broken; OTP just hasn't finished exploring all the
TGV/TER/Trenitalia/Eurostar candidate paths in 10s.

v0.1.24 makes this a per-session knob (UI dropdown, same pattern as
osm_scope/otp_timezone/otp_build_heap) and bumps the default to 30s,
which is plenty for France-wide multi-NAP graphs while still being short
enough to fail-fast on misrouted searches.

Accepts the human-readable duration syntax OTP itself parses
(`<int><unit>`, units `s` / `m`). ISO-8601 (`PT30S`) is rejected here
to keep the dropdown simple — operators with exotic needs can always
override via the API.

Caveat documented in OTP 2.9 docs and worth knowing: when the
`ParallelRouting` feature flag is on, OTP **bypasses this timeout
entirely**. We don't enable that flag; if a future operator does, the
knob becomes a no-op and they'll see neither timeout nor enforcement.
"""

from __future__ import annotations

import re

# Default for sessions that don't set the field. 30s is the comfortable
# upper bound for a France-wide multi-NAP graph (43k stops, 13 providers,
# 1.1M walk transfers) doing a Paris → Marseille search; smaller graphs
# return well inside this. 10s — the previous hardcoded value — was too
# tight for the multi-NAP shape this demo has grown into.
DEFAULT_TIMEOUT = "30s"

# Curated dropdown values. Top of the range is operator's last resort
# before they need to think about graph pruning or a tighter
# `numItineraries` setting.
COMMON_TIMEOUTS: list[tuple[str, str]] = [
    ("10s", "10 s — small single-feed sessions"),
    ("20s", "20 s"),
    ("30s", "30 s — standard multi-NAP graphs (recommended)"),
    ("60s", "60 s"),
    ("120s", "120 s — last-resort cap for cross-border experiments"),
]

# Strict pattern: integer + 's' (seconds) or 'm' (minutes). Lowercase
# unit only — OTP itself accepts both cases but normalising keeps the
# stored value predictable for audit-log searches.
_TIMEOUT_RE = re.compile(r"^(\d+)([sm])$")


def validate_timeout(
    value: str | None,
    *,
    default: str = DEFAULT_TIMEOUT,
) -> str:
    """Return a validated timeout string (e.g. '30s'), or raise ValueError.

    None / empty → default (caller can pass an env-var fallback so legacy
    sessions inherit whatever the deployment is using). Whitespace
    trimmed but bare-whitespace fails the regex and raises — same shape
    as `app.otp_timezone` / `app.otp_heap`.
    """
    if value is None or value == "":
        return default
    if not isinstance(value, str):
        raise ValueError(
            f"otp_api_timeout must be a string, got {type(value).__name__}"
        )
    stripped = value.strip().lower()
    m = _TIMEOUT_RE.match(stripped)
    if not m:
        raise ValueError(
            f"otp_api_timeout={value!r} doesn't match the expected pattern "
            "(integer + 's' or 'm', e.g. '30s', '120s', '2m')"
        )
    return stripped
