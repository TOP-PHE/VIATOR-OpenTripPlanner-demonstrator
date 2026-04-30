"""Thin async client for OTP's GraphQL endpoint.

Each session has its own OTP container reachable at `http://otp-<sid>:8080/`
via the internal docker network. We post a small GraphQL document and shape
the response into the canonical 'trip' dicts our recorder expects.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx

log = logging.getLogger(__name__)


# Minimal GraphQL query — the real query at step 14 will be richer.
#
# OTP 2.9 dropped Itinerary.walkTime / transitTime / waitingTime: those
# values are trivially computable from legs[].duration grouped by mode,
# so OTP simplified the schema. Asking for them returns
# "Validation error (FieldUndefined@[plan/itineraries/transitTime])"
# and no itineraries — so the journey UI shows "0 trips (error)" even
# when OTP would otherwise have returned valid TGV/RER results.
#
# `routingErrors` is included explicitly so we can surface OTP's own
# diagnostics (LOCATION_NOT_FOUND, WALKING_BETTER_THAN_TRANSIT, etc.)
# back to the operator instead of treating them as silent empty results.
#
# numItineraries: 8 — operators usually want to see "next several trains"
# not just the first match, especially for hourly TGV service.
# searchWindow: 14400 (4 hours, in seconds) — OTP's default search window
# adapts but tends to be ~1h on long-distance routes; explicitly widening
# pulls more departures into the result set. Trade-off: slightly slower
# queries (typically still <1 s for inter-city), but the demonstrator
# value of seeing 6-8 alternatives outweighs it.
_QUERY = """
query Plan($from: InputCoordinates!, $to: InputCoordinates!, $date: String, $time: String) {
  plan(from: $from, to: $to, date: $date, time: $time, numItineraries: 8, searchWindow: 14400) {
    itineraries {
      duration
      startTime
      endTime
      legs {
        mode
        startTime
        endTime
        from { name lat lon stop { gtfsId } }
        to   { name lat lon stop { gtfsId } }
        route { shortName longName }
        duration
        distance
      }
    }
    routingErrors { code description }
  }
}
""".strip()


def _otp_base(session_id: str) -> str:
    """Resolve the internal hostname for a session's OTP container."""
    return f"http://otp-{session_id}:8080"


async def fetch_plan(
    *,
    session_id: str,
    from_lat: float,
    from_lon: float,
    to_lat: float,
    to_lon: float,
    when: datetime,
    timeout_ms: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Call OTP. Returns (raw_response, trips_for_recorder).

    Raises httpx.HTTPError on transport / HTTP failure (caller maps to 'error').
    """
    payload = {
        "query": _QUERY,
        "variables": {
            "from": {"lat": from_lat, "lon": from_lon},
            "to": {"lat": to_lat, "lon": to_lon},
            "date": when.strftime("%Y-%m-%d"),
            "time": when.strftime("%H:%M"),
        },
    }
    # OTP 2.9 GTFS GraphQL endpoint. Note: it is `/otp/gtfs/v1`, NOT
    # `/otp/gtfs/v1/index/graphql` — the `/index/graphql` form was the
    # legacy (Entur/HSL) path served at `/otp/routers/default/index/graphql`
    # which OTP 2.x dropped. Mismatching this returns 404.
    url = f"{_otp_base(session_id)}/otp/gtfs/v1"
    timeout = max(timeout_ms / 1000.0, 1.0)
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.post(url, json=payload)
    r.raise_for_status()
    raw: dict[str, Any] = r.json()
    return raw, _normalise(raw)


def _normalise(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Translate OTP's response into the recorder's `trips` format.

    Per-leg fields captured (all optional — defensively coerced):

      mode               WALK / RAIL / SUBWAY / BUS / etc.
      departure / arrival  ISO 8601 UTC; OTP gives epoch ms, we convert
      duration_seconds   leg duration; useful for expandable detail UI
      distance_meters    leg distance (walking only meaningful for WALK)
      from_name/lat/lon/stop_id  origin endpoint
      to_name/lat/lon/stop_id    destination endpoint
      route_short_name / route_long_name  e.g. "601A" / "Paris - Lyon TGV"

    The journey UI's expandable detail (v0.1.7.x) reads these directly.
    Keep the field shapes stable — they're stored verbatim in
    `journey_trips.legs` (JSONB) for replay / audit.
    """
    its = (((raw or {}).get("data") or {}).get("plan") or {}).get("itineraries") or []
    out: list[dict[str, Any]] = []
    for it in its:
        legs_norm = []
        modes_set = []
        for leg in it.get("legs", []):
            f = leg.get("from") or {}
            t = leg.get("to") or {}
            route = leg.get("route") or {}
            legs_norm.append(
                {
                    "mode": leg.get("mode"),
                    "departure": _ms_to_iso(leg.get("startTime")),
                    "arrival": _ms_to_iso(leg.get("endTime")),
                    "duration_seconds": int(leg.get("duration") or 0),
                    "distance_meters": float(leg.get("distance") or 0.0),
                    "from_name": f.get("name"),
                    "from_lat": f.get("lat"),
                    "from_lon": f.get("lon"),
                    "from_stop_id": ((f.get("stop") or {}).get("gtfsId")),
                    "to_name": t.get("name"),
                    "to_lat": t.get("lat"),
                    "to_lon": t.get("lon"),
                    "to_stop_id": ((t.get("stop") or {}).get("gtfsId")),
                    "route_short_name": route.get("shortName"),
                    "route_long_name": route.get("longName"),
                }
            )
            if leg.get("mode"):
                modes_set.append(leg["mode"])
        out.append(
            {
                "duration_seconds": int(it.get("duration") or 0),
                "num_transfers": max(
                    0, len([lg for lg in legs_norm if lg.get("mode") not in (None, "WALK")]) - 1
                ),
                "departure_at": _ms_to_iso(it.get("startTime")) or "",
                "arrival_at": _ms_to_iso(it.get("endTime")) or "",
                "modes": ",".join(sorted(set(modes_set))),
                "legs": legs_norm,
            }
        )
    return out


def _ms_to_iso(ms: Any) -> str | None:
    """OTP returns epoch millis; render as ISO so DB can store it as TIMESTAMPTZ."""
    if ms is None:
        return None
    try:
        return datetime.utcfromtimestamp(int(ms) / 1000).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    except (TypeError, ValueError):  # pragma: no cover
        return None
