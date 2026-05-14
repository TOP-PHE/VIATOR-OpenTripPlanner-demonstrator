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
# The variables $from and $to are typed as `InputCoordinates!` which OTP
# 2.9 accepts with EITHER `{lat, lon}` OR `{stopId}` (or both, in which
# case stopId wins and the lat/lon is ignored). We choose between the
# two encodings per-call in `fetch_plan` below — defaults to lat/lon for
# back-compat with callers that don't pass stop_ids.
#
# Stop-id routing was added in v0.1.33 to bypass walk-graph snap-failures
# for small/border stations whose walking neighbourhood was stripped by
# rail-focused OSM filtering (e.g. Travers and Pontarlier in the CH
# session — see docs/nap-ch-rail.md §9).
_QUERY = """
query Plan($from: InputCoordinates!, $to: InputCoordinates!, $date: String, $time: String) {
  plan(from: $from, to: $to, date: $date, time: $time,
       numItineraries: __NUM_ITINERARIES__, searchWindow: __SEARCH_WINDOW__) {
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
        route {
          gtfsId
          shortName
          longName
          # v0.1.26: surface the operator on each leg so the journey UI
          # can show "SNCF" / "Trenitalia" / "Eurostar" instead of just
          # the route number. agency.gtfsId comes back as "<feedId>:<id>"
          # so we can also derive which session-level feed a leg came
          # from (useful for "why isn't Trenitalia showing" diagnosis).
          agency { gtfsId name url }
        }
        # trip.gtfsId is "<feedId>:<trip_id>" — the feedId prefix tells
        # us which provider the trip came from regardless of whether
        # agency.gtfsId is set (some feeds don't populate agency).
        trip { gtfsId tripHeadsign }
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


def _location_not_found(raw: dict[str, Any]) -> bool:
    """True iff OTP returned an empty itinerary list AND at least one
    `LOCATION_NOT_FOUND` routing error.

    Used to decide whether a first-attempt stop-id plan should be retried
    with lat/lon. Other routingErrors (NO_TRANSIT_CONNECTION,
    WALKING_BETTER_THAN_TRANSIT, etc.) are NOT triggers for retry — those
    mean OTP routed but found no acceptable result, which a lat/lon
    fallback wouldn't fix.
    """
    plan = ((raw or {}).get("data") or {}).get("plan") or {}
    if plan.get("itineraries"):
        return False
    for err in plan.get("routingErrors") or []:
        if (err or {}).get("code") == "LOCATION_NOT_FOUND":
            return True
    return False


async def fetch_plan(
    *,
    session_id: str,
    from_lat: float,
    from_lon: float,
    to_lat: float,
    to_lon: float,
    when: datetime,
    timeout_ms: int,
    num_itineraries: int = 8,
    search_window_seconds: int = 14400,
    from_stop_id: str | None = None,
    to_stop_id: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Call OTP. Returns (raw_response, trips_for_recorder).

    Raises httpx.HTTPError on transport / HTTP failure (caller maps to 'error').

    `num_itineraries` and `search_window_seconds` (v0.1.29) let the caller
    widen OTP's search beyond the live-journey defaults — used by the
    network-coverage runner to fetch the full day's worth of trains for
    each pair in one call (50 itineraries / 24h window) instead of the
    "next ~hour, top 8" the live UI wants.

    v0.1.29.1: substituted into the query string at call time rather than
    passed as GraphQL variables. The first attempt declared
    `$searchWindow: Int` but OTP's GTFS schema has `searchWindow: Long` —
    when fed via a typed Int variable, the field was silently dropped and
    OTP fell back to a tiny search window, returning 0 itineraries on
    every pair (cured by going back to inline literals which OTP coerces
    correctly into the Long field).

    v0.1.33 — `from_stop_id` / `to_stop_id` (optional): when set, OTP's
    plan is called with `{stopId: ...}` instead of `{lat, lon}` for that
    endpoint, bypassing the walk-graph snap entirely. If the resulting
    plan comes back empty with `LOCATION_NOT_FOUND` (= the stop_id isn't
    in this session's graph, e.g. the caller guessed the feed prefix
    wrong) we transparently retry once with lat/lon. The caller gets
    whichever attempt produced results — or the LOCATION_NOT_FOUND
    response from the lat/lon attempt if both fail.

    Why retry inside the client and not in the API layer: the retry
    decision is purely about OTP's response shape (matching
    `LOCATION_NOT_FOUND` vs other errors), so it belongs next to the OTP
    call. The API layer doesn't need to learn OTP's error codes.
    """
    query = _QUERY.replace("__NUM_ITINERARIES__", str(int(num_itineraries))).replace(
        "__SEARCH_WINDOW__", str(int(search_window_seconds))
    )

    def _build_endpoint(
        stop_id: str | None, lat: float, lon: float
    ) -> dict[str, Any]:
        # OTP 2.9 InputCoordinates: stopId wins when both are present;
        # we omit lat/lon when sending stopId to keep the variable shape
        # minimal and unambiguous.
        if stop_id:
            return {"stopId": stop_id}
        return {"lat": lat, "lon": lon}

    def _payload(use_stop_ids: bool) -> dict[str, Any]:
        return {
            "query": query,
            "variables": {
                "from": _build_endpoint(
                    from_stop_id if use_stop_ids else None, from_lat, from_lon
                ),
                "to": _build_endpoint(
                    to_stop_id if use_stop_ids else None, to_lat, to_lon
                ),
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
    has_stop_ids = bool(from_stop_id or to_stop_id)

    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.post(url, json=_payload(use_stop_ids=has_stop_ids))
        r.raise_for_status()
        raw: dict[str, Any] = r.json()
        # Fallback: stop-id attempt rejected by OTP — caller probably
        # supplied a feedId/UIC pairing that doesn't exist in this
        # session's graph. Retry with lat/lon if we have it.
        if has_stop_ids and _location_not_found(raw):
            log.info(
                "session=%s stop-id plan returned LOCATION_NOT_FOUND "
                "(from_stop_id=%s to_stop_id=%s) — retrying with lat/lon",
                session_id,
                from_stop_id,
                to_stop_id,
            )
            r = await c.post(url, json=_payload(use_stop_ids=False))
            r.raise_for_status()
            raw = r.json()

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
      route_id           "<feed>:<route>" — full namespaced GTFS id (v0.1.26)
      agency_name        operator-facing name e.g. "SNCF", "Trenitalia" (v0.1.26)
      agency_id          "<feed>:<agency_id>" full GTFS id (v0.1.26)
      feed_id            extracted from trip.gtfsId prefix — answers
                         "which session-level feed did this leg come from"
                         even when agency.name is null (v0.1.26)
      trip_headsign      e.g. "Marseille via Lyon" (v0.1.26)
      trip_id            "<feed>:<trip>" — full namespaced GTFS trip id (v0.1.26)

    The journey UI's expandable detail (v0.1.7.x) reads these directly.
    Keep the field shapes stable — they're stored verbatim in
    `journey_trips.legs` (JSONB) for replay / audit.

    v0.1.26 also attaches the raw OTP itinerary slice as `_raw_itinerary`
    on each trip so the UI's JSON inspector can show OTP's exact reply
    without an extra round-trip. The `_` prefix marks it as
    presentation-layer-only — the recorder ignores fields starting with
    underscore when persisting trips to journey_trips.legs.
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
            agency = route.get("agency") or {}
            trip_obj = leg.get("trip") or {}
            # Derive feed_id from trip.gtfsId. Format is "<feedId>:<localId>"
            # — same convention OTP uses for stop_id, route_id, agency_id.
            # When the feed itself doesn't populate agency, this gives us
            # a reliable fallback indicator of which provider ingested
            # this leg.
            trip_gtfs_id = trip_obj.get("gtfsId") or ""
            feed_id_from_trip = trip_gtfs_id.split(":", 1)[0] if ":" in trip_gtfs_id else None
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
                    "route_id": route.get("gtfsId"),
                    # v0.1.26 — operator visibility on each leg.
                    "agency_name": agency.get("name"),
                    "agency_id": agency.get("gtfsId"),
                    "agency_url": agency.get("url"),
                    "feed_id": feed_id_from_trip,
                    "trip_id": trip_gtfs_id or None,
                    "trip_headsign": trip_obj.get("tripHeadsign"),
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
                # v0.1.26 — raw OTP itinerary slice for the JSON inspector
                # in the journey UI. Underscore prefix marks it as
                # presentation-layer-only; recorder.persist_trip() drops
                # any keys starting with underscore before INSERTing into
                # journey_trips so the JSONB column stays stable.
                "_raw_itinerary": it,
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
