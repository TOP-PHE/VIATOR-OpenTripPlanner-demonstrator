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


# ─────────────────── anchor-time pagination helpers ───────────────────
#
# v0.1.35.06 introduced this exact algorithm inside ojp_client.py to
# close the gap between OJP's ~6-alternative TripRequest and OTP's
# wider searchWindow. v0.1.45 needs the identical algorithm for HAFAS's
# numF=5 TripSearch (same problem — cf. external_verify's
# _build_trip_search_body). Moved here (rather than duplicated a second
# time) so both clients share one implementation of "dedup by
# fingerprint, track the latest departure, compute the next anchor" —
# the parts that are genuinely identical regardless of which HTTP API
# is behind them. Each client keeps its own thin per-page fetch loop
# (error-handling semantics differ: ojp_client's fetch_reference can
# raise; hafas_client's fetch_plan never does).


def max_dep_ts(current: float | None, dep_str: str | None) -> float | None:
    """Return max(current, parse(dep_str)) ignoring unparseable entries."""
    if not dep_str:
        return current
    try:
        dep_ts = datetime.fromisoformat(dep_str).timestamp()
    except ValueError:
        return current
    return dep_ts if current is None else max(current, dep_ts)


def dedup_batch_and_track_latest_dep(
    batch: list[dict[str, Any]], seen_fps: set[str]
) -> tuple[list[dict[str, Any]], float | None]:
    """One page's bookkeeping: dedupe by transit_fingerprint, track the
    latest departure across ALL trips (duplicates included — a boundary
    dup still proves the engine advanced to that anchor).

    Walk-only trips (empty fingerprint) are never deduplicated; they're
    rare for any OD pair where pagination would even fire.
    """
    # Lazy import: signature.py pulls SQLAlchemy for trip_signature even
    # though transit_fingerprint itself is DB-free. Importing at call
    # time keeps this module (and its callers) importable in lightweight
    # environments that don't have SQLAlchemy on the path.
    from .signature import transit_fingerprint

    new_trips: list[dict[str, Any]] = []
    latest_dep_ts: float | None = None
    for t in batch:
        fp = transit_fingerprint(t.get("legs") or [])
        if not (fp and fp in seen_fps):
            if fp:
                seen_fps.add(fp)
            new_trips.append(t)
        latest_dep_ts = max_dep_ts(latest_dep_ts, t.get("departure_at"))
    return new_trips, latest_dep_ts


def next_anchor_or_none(latest_dep_ts: float | None, target_end_ts: float) -> datetime | None:
    """Compute the next page's anchor, or None to stop.

    Returns None when:
    - latest_dep_ts is None (no parseable departures, can't advance), OR
    - latest_dep_ts >= target window end (the engine caught up to the
      comparison target).

    Otherwise returns `latest_dep_ts + 1 min` as a UTC datetime — the
    +60 s nudge avoids the next page returning the same train as the
    leading edge of the previous batch.
    """
    if latest_dep_ts is None:
        return None
    if latest_dep_ts >= target_end_ts:
        return None
    return datetime.fromtimestamp(latest_dep_ts + 60.0, tz=UTC)
