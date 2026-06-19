"""Thin async client for MOTIS — drop-in alternative to otp_client.

Phase-0 spike (see motis-spike/README.md). Mirrors `otp_client.fetch_plan`'s
signature and return shape exactly, so once we validate the spike, the
session-level dispatcher in Phase 1 is a one-liner:

    client = motis_client if session.engine == "motis" else otp_client
    raw, trips = await client.fetch_plan(session_id=..., ...)

Routing query
-------------
`GET /api/v6/plan` (https://github.com/motis-project/motis/blob/master/openapi.yaml).
Unlike OTP's GraphQL `planConnection`, MOTIS exposes a flat REST endpoint that
accepts the same primitives we need: a `fromPlace` / `toPlace` as `lat,lon`
strings, an ISO `time`, optional `transitModes`, `numItineraries`, and
`searchWindow` (in seconds). MOTIS also supports a stop id in the `fromPlace`
slot (same way OTP's `planConnection.origin.stopLocation` does), so the
`from_stop_id` / `to_stop_id` kwargs the federated planner passes carry over.

Response shape (verified against the OpenAPI spec)
--------------------------------------------------
Top-level: `{ from, to, direct[], itineraries[], previousPageCursor, nextPageCursor }`.
Each `Itinerary`: `startTime`, `endTime` (ISO date-time), `duration` (seconds),
`transfers`, `legs[]`. Each `Leg`: `startTime`, `endTime` (ISO), `mode`,
`from`, `to` (`Place` = `{name, latitude, longitude, stopId?}`), and optional
`route`, `routeShortName`, `agency`, `headsign`, `tripId`,
`intermediateStops[]`. So compared to OTP every time field is already ISO and
every place coordinate is already a float — no epoch-ms or coordinate
unpacking gymnastics here.

Translator gaps / TODOs are marked inline; resolve them once we have a
captured live response to compare against.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

log = logging.getLogger(__name__)


_INTERNAL_SCHEME = "http"


def _base_url_for(session_id: str, base_url: str | None) -> str:
    """`base_url` overrides; default mirrors OTP's per-session DNS convention.

    The default uses plain HTTP because the MOTIS container is only ever
    reachable from inside the docker network (same as every other VIATOR
    session-internal hop — see `otp_client._otp_base`). There is no public TLS
    surface here.
    """
    if base_url is not None:
        return base_url.rstrip("/")
    return f"{_INTERNAL_SCHEME}://motis-{session_id}:8080"


async def fetch_plan(
    *,
    session_id: str,
    from_lat: float,
    from_lon: float,
    to_lat: float,
    to_lon: float,
    when: datetime,
    timeout_ms: int,
    num_itineraries: int = 12,
    search_window_seconds: int = 21600,
    from_stop_id: str | None = None,
    to_stop_id: str | None = None,
    session_timezone: str | None = None,
    base_url: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Call MOTIS's `/api/v6/plan`. Returns `(raw_response, trips_for_recorder)`.

    Mirrors `otp_client.fetch_plan` argument-for-argument so the dispatcher
    (Phase 1) can pick the client by `session.engine` without touching the
    callsite. The `base_url` override is a spike-only convenience for pointing
    the comparison harness at `http://localhost:8081`; production sessions
    leave it unset and use the per-session DNS name.

    Raises `httpx.HTTPError` on transport / HTTP failure (caller maps to
    `'error'`, same contract as OTP).
    """
    url = f"{_base_url_for(session_id, base_url)}/api/v6/plan"
    # MOTIS accepts a stop id in the same slot as the coord ("lat,lon" or a
    # stop id), so when the caller hands us one we prefer it — same precedence
    # the federated planner relies on for OTP's stop-id-first attempt.
    from_place = from_stop_id or f"{from_lat},{from_lon}"
    to_place = to_stop_id or f"{to_lat},{to_lon}"
    # Localise a naive `when` against the session's configured timezone, same
    # pre-processing the OTP path does in `_earliest_departure`. MOTIS accepts
    # any ISO-8601 instant, but a naive string is ambiguous on the wire — so
    # if we know the session's tz, attach it now. Unknown tz falls back to UTC.
    if when.tzinfo is None and session_timezone:
        try:
            when = when.replace(tzinfo=ZoneInfo(session_timezone))
        except ZoneInfoNotFoundError:
            log.warning(
                "unknown session_timezone for session_id=%s; treating naive `when` as UTC",
                session_id,
            )
    params: dict[str, Any] = {
        "fromPlace": from_place,
        "toPlace": to_place,
        "time": when.isoformat(),
        "numItineraries": num_itineraries,
        "searchWindow": search_window_seconds,
        # Default `TRANSIT` is what we want everywhere today. Expose modes via
        # a kwarg once Phase 1 needs it.
        "transitModes": ["TRANSIT"],
    }

    async with httpx.AsyncClient(timeout=timeout_ms / 1000.0) as client:
        r = await client.get(url, params=params)
    r.raise_for_status()
    raw: dict[str, Any] = r.json()

    return raw, _itineraries_to_trips(raw)


def _itineraries_to_trips(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Translate MOTIS's `/api/v6/plan` response into the canonical trip dicts
    the recorder + federated planner consume.

    Schema mapping:
        MOTIS Itinerary.startTime  ->  trip['departure_at']
        MOTIS Itinerary.endTime    ->  trip['arrival_at']
        MOTIS Itinerary.duration   ->  trip['duration_seconds']
        MOTIS Itinerary.transfers  ->  trip['num_transfers']
        legs[].mode (filtered)     ->  trip['modes']  (comma-joined, no WALK)
        legs[]                     ->  trip['legs']   (one canonical leg per)

    Best-effort fields (None until we capture a live response to confirm
    MOTIS's exact field names — see the inline notes in `_leg_to_canonical`):
        leg distance, route_id, agency_id, feed_id.
    """
    itineraries = raw.get("itineraries") or []
    out: list[dict[str, Any]] = []
    for it in itineraries:
        legs_norm = [_leg_to_canonical(leg) for leg in (it.get("legs") or [])]
        # Same convention as OTP: only non-WALK modes count toward the
        # itinerary's mode summary.
        modes = sorted({lg["mode"] for lg in legs_norm if lg.get("mode") and lg["mode"] != "WALK"})
        out.append(
            {
                "duration_seconds": int(it.get("duration") or 0),
                # MOTIS gives transfers explicitly; fall back to the OTP-style
                # count if the field is missing on some response variant.
                "num_transfers": int(
                    it.get("transfers")
                    if it.get("transfers") is not None
                    else max(0, sum(1 for lg in legs_norm if lg.get("mode") != "WALK") - 1)
                ),
                "departure_at": str(it.get("startTime") or ""),
                "arrival_at": str(it.get("endTime") or ""),
                "modes": ",".join(modes),
                "legs": legs_norm,
                # Same convention as OTP — underscore-prefixed keys are
                # presentation-layer-only and stripped by recorder.persist_trip.
                "_raw_itinerary": it,
            }
        )
    return out


def _feed_id_from_motis_id(motis_id: str | None) -> str | None:
    """Extract the feed id from a MOTIS-formatted stop or trip id.

    MOTIS encodes both feed and local id into one string with `_` as the
    separator (e.g. `renfe-ld_60000`). OTP uses `:` (e.g. `renfe-ld:60000`).
    Phase-1 federated planner dedup keys on feed_id, so we surface it here
    rather than make the dispatcher engine-aware.

    Returns None on inputs that don't carry the `<feed>_<local>` shape —
    the federated planner already tolerates missing feed_id by falling
    back to coordinate-based stitching.
    """
    if not motis_id or "_" not in motis_id:
        return None
    return motis_id.rsplit("_", 1)[0] or None


def _leg_to_canonical(leg: dict[str, Any]) -> dict[str, Any]:
    """One MOTIS leg -> one canonical leg dict (matching otp_client's shape).

    Field names verified against a real Renfe AVE response in the Phase-0.5
    spike (Madrid Atocha → Barcelona Sants, 2026-06-19). Key MOTIS-isms vs
    OTP that surfaced there:
      * Place coords are `lat` / `lon`, not `latitude` / `longitude`.
      * Route + agency fields are TOP-LEVEL on the leg (e.g. `routeShortName`,
        `agencyName`) — there is no nested `route: {}` / `agency: {}` object.
      * `routeId` carries the GTFS route id; we surface it verbatim.
      * No `distance` field; if downstream consumers care about leg distance
        they'll need to derive it from `legGeometry` (out of scope for P1).
      * `stopId` uses underscore: `<feed>_<local>` not OTP's `<feed>:<local>`.
        Feed id is extracted via _feed_id_from_motis_id so the federated
        planner's dedup keys still work.
    """
    f = leg.get("from") or {}
    t = leg.get("to") or {}
    duration = leg.get("duration")
    from_stop_id = f.get("stopId")
    to_stop_id = t.get("stopId")
    return {
        "mode": leg.get("mode"),
        "departure": str(leg.get("startTime") or ""),
        "arrival": str(leg.get("endTime") or ""),
        "duration_seconds": int(duration) if duration is not None else 0,
        # MOTIS doesn't expose a leg distance field. Recorded as 0.0 to keep
        # the canonical shape stable; consumers that need distance can
        # derive it from `legGeometry` on _raw_itinerary.
        "distance_meters": 0.0,
        "from_name": f.get("name"),
        "from_lat": f.get("lat"),
        "from_lon": f.get("lon"),
        "from_stop_id": from_stop_id,
        "to_name": t.get("name"),
        "to_lat": t.get("lat"),
        "to_lon": t.get("lon"),
        "to_stop_id": to_stop_id,
        "route_short_name": leg.get("routeShortName"),
        "route_long_name": leg.get("routeLongName"),
        "route_id": leg.get("routeId"),
        "agency_name": leg.get("agencyName"),
        "agency_id": leg.get("agencyId"),
        "agency_url": leg.get("agencyUrl"),
        # Both stops should share a feed id (transit legs don't cross feeds);
        # prefer the origin's so the value is stable when MOTIS adds the
        # trailing-stop entry to multi-stop intermediate hops.
        "feed_id": _feed_id_from_motis_id(from_stop_id),
        "trip_id": leg.get("tripId"),
        "trip_headsign": leg.get("headsign"),
    }
