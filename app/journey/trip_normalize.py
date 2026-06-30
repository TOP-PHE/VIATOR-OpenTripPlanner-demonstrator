"""Tiny shared helpers for normalising trip dicts across planner clients.

PR-3 introduces `first_transit_leg_departure_utc` — a UTC-ISO string
on every trip dict that identifies WHEN the first transit leg
(non-WALK / non-TRANSFER) boards. Coverage runner uses this — NOT the
itinerary-level `departure_at` — to decide whether a trip's BOARDING
falls inside a configured day-window. The itinerary-level
`departure_at` is the START of the entire trip (often a walk leg),
so it would silently let "leaves the door at 23:50, boards 00:15
train" trips slip into the previous day's window.

Lives here (not in motis_client / otp_client / ojp_client) so the
three clients can share the implementation without circular imports.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

# Modes treated as non-transit. The canonical mode vocabulary across
# OTP / MOTIS / OJP is upper-case strings; `_first_transit_leg_*` is
# tolerant of case to handle the rare OJP `pt_mode` that comes back
# mixed-case from older endpoints.
_NON_TRANSIT_MODES: frozenset[str] = frozenset({"WALK", "TRANSFER", ""})


def first_transit_leg_departure_utc(legs: list[dict[str, Any]] | None) -> str | None:
    """Return the first transit leg's scheduled departure as a UTC-ISO
    string (matching the recorder's `+00:00` suffix convention), or None
    when the itinerary has no transit leg.

    Walk-only itineraries (door-to-door walking, common on short urban
    OD pairs) deliberately return None — the coverage runner's day-D
    filter is about "did the operator board a train/bus/tram in the
    window", and a walk has no boarding event.

    Tolerant of:
      - missing/None `legs`
      - missing/None `mode` on a leg (treated as non-transit)
      - missing/None `departure` on the first transit leg (returns None
        rather than guessing — the itinerary is unusable for the
        day-window filter anyway)
      - mixed-case mode strings
    """
    if not legs:
        return None
    for leg in legs:
        mode = (leg.get("mode") or "").upper()
        if mode in _NON_TRANSIT_MODES:
            continue
        # First transit leg found. The clients all emit `departure` as
        # a UTC-ISO string already (see `otp_client._ms_to_iso`,
        # `motis_client._leg_to_canonical`, `ojp_client._iso_to_utc_iso`),
        # but we re-normalise defensively so a future client that
        # forgets the UTC conversion doesn't poison the day-window
        # comparison.
        dep = leg.get("departure")
        return _coerce_utc_iso(dep)
    return None


def _coerce_utc_iso(value: Any) -> str | None:
    """Re-serialise an ISO-ish string into the canonical UTC ISO shape
    (`YYYY-MM-DDTHH:MM:SS+00:00`). Naive input is assumed UTC; tz-aware
    input is converted to UTC. Returns None on unparseable / falsy
    input."""
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.strip())
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")
