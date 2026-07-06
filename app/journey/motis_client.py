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

from .trip_normalize import first_transit_leg_departure_utc as _first_transit_leg_departure_utc

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
    # MOTIS's `fromPlace`/`toPlace` accept either a coord string or a stop id,
    # BUT the stop id has to match MOTIS's own index format
    # (`<gtfs_feed_id>_<localId>`, e.g. `renfe_60000`) — NOT OTP's
    # `<provider_id>:<UIC>` (e.g. `RENFE-CERCA:7160000`). VIATOR's
    # `_stop_id_for` builds the OTP form because every existing session uses
    # OTP; passing it through to MOTIS produced `404 Not Found` for every
    # query (surfaced 2026-06-21 on sp-rail-motis).
    #
    # Since we can't reliably translate OTP-style ids → MOTIS-style ids at
    # call time (the GTFS `feed_id` MOTIS uses is determined at import time
    # and may not match the provider's session config id at all), we
    # *ignore* the stop-id kwargs and always use coordinates. MOTIS does a
    # short geo-walk from the coord to the nearest transit stop, which the
    # Phase-0.5 spike confirmed produces clean Madrid Atocha → Barcelona
    # Sants AVE itineraries.
    #
    # A Phase-2 follow-up could add a session-level feed_id map so we CAN
    # pass MOTIS-shaped stop ids when the caller knows them; for now
    # `from_stop_id` / `to_stop_id` are accepted for signature parity with
    # `otp_client.fetch_plan` but deliberately unused.
    _ = from_stop_id, to_stop_id  # signature-parity; see comment above
    from_place = f"{from_lat},{from_lon}"
    to_place = f"{to_lat},{to_lon}"
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
    # TEMP DEBUG (2026-07) — investigating a reported large VIATOR/ÖBB
    # time-window divergence in the journey-search side-by-side. Logs the
    # exact anchor sent to MOTIS so it can be diffed against
    # external_verify's equivalent HAFAS log for the same search. Safe to
    # remove once that investigation concludes.
    log.info("motis.plan.request session_id=%s time=%s", session_id, when.isoformat())
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

    # PR-188 — close TCP per-request + granular phase timeouts.
    #
    # When the coverage runner's outer `asyncio.wait_for` fires its slot
    # timeout, httpx closes the TCP socket client-side — but MOTIS keeps
    # crunching the RAPTOR result. When it finishes and tries to write back
    # it logs "Broken pipe" and discards, with the CPU already spent. Under
    # many concurrent coverage queries that orphan-compute pile-up pegged
    # MOTIS at 1798% CPU + 281 GB block I/O while serving ~0 useful results
    # (observed 2026-06-30).
    #
    # `Connection: close` signals MOTIS to release the per-request work the
    # instant the client disconnects, instead of finish-then-discover-dead-
    # socket. The trade-off is ~1ms TCP-handshake overhead per call (no
    # keep-alive); that's negligible compared to the orphan-compute cost we
    # avoid here.
    #
    # Separately, we now give httpx granular per-phase timeouts (connect /
    # read / write / pool) instead of a single overall deadline. The outer
    # `asyncio.wait_for(..., slot_timeout)` in the coverage runner stays as
    # the hard ceiling; these inner phase timeouts let httpx fail fast on
    # connect/write hangs without burning the whole slot budget.
    read_timeout_s = timeout_ms / 1000.0
    timeout = httpx.Timeout(connect=5.0, read=read_timeout_s, write=5.0, pool=5.0)
    headers = {"Connection": "close"}
    async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
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

    PR-3: each trip carries an extra `first_transit_leg_departure_utc`
    (UTC-ISO string or None when the itinerary is walk-only). The
    coverage runner uses this — NOT `departure_at` — to decide whether
    a trip's BOARDING falls inside the run's day window. `departure_at`
    is the itinerary START (which on a walk-then-train trip is the
    walking step) so it would silently let "leaves the door at 23:50,
    boards 00:15 train" trips slip into the previous day's window.
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
                # PR-3 — first transit-leg boarding time in UTC ISO. See
                # the module-level note in _first_transit_leg_departure_utc
                # for why this is separate from `departure_at`.
                "first_transit_leg_departure_utc": _first_transit_leg_departure_utc(legs_norm),
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
