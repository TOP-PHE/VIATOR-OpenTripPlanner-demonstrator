"""Thin async client for OTP's GraphQL endpoint.

Each session has its own OTP container reachable at `http://otp-<sid>:8080/`
via the internal docker network. We post a small GraphQL document and shape
the response into the canonical 'trip' dicts our recorder expects.

Routing query — `planConnection` (v0.1.34)
------------------------------------------
We use OTP 2.9's `planConnection` query rather than the legacy `plan`
query. `planConnection`'s location input (`PlanLocationInput`) accepts
EITHER a coordinate OR a transit `stopLocation` — and the stop form
bypasses the lat/lon → walk-graph snap entirely. That snap is what fails
for small / border stations whose walking neighbourhood gets stripped by
`rail-focused` OSM filtering (Travers, Pontarlier, Les Verrières in the
CH session — see docs/nap-ch-rail.md §9.1).

The earlier v0.1.33 attempt (reverted in #76) tried to pass `{stopId}`
to the legacy `plan` query — OTP rejects that, `plan`'s `InputCoordinates`
requires `lat`/`lon`. `planConnection` is the correct API; verified
against the live OTP 2.9 schema before this was written.

Response-shape notes (verified against OTP 2.9, nap-ch-rail):
  - success: `data.planConnection.edges[].node` — node has `start`/`end`
    (ISO-8601 OffsetDateTime strings) and `duration` (seconds, Int).
  - legs: `startTime`/`endTime` are still epoch-ms, and `duration` /
    `distance` / `from` / `to` / `route` / `trip` are byte-identical to
    the legacy `plan` query — so the per-leg parsing below is unchanged.
  - bad stop id: HTTP 200, empty `edges`, and a `routingErrors` entry
    with `code == "LOCATION_NOT_FOUND"`.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from .canonical import CanonicalJourney
from .canonical_adapters import otp_adapter
from .canonical_adapters._common import source_rank
from .canonical_adapters.trip_dict import to_trip_dict

log = logging.getLogger(__name__)


# Stop ids are `<feedId>:<localId>` — feedId matches OTP's
# ^[A-Z][A-Z0-9_-]{1,15}$ and localId is GTFS-ish (alphanumerics plus
# :_.- ). Anything outside that set in a value we're about to log is
# stripped: the `uic` half originates from the journey request body, so
# logging it raw would let a caller forge log lines via newline
# injection (SonarCloud pythonsecurity:S5145).
_LOG_TOKEN_DISALLOWED = re.compile(r"[^A-Za-z0-9:_.\-]")


def _safe_log_token(value: str | None) -> str:
    """Sanitise a user-influenced stop_id for safe logging.

    Returns '-' for empty/None, otherwise the value with any character
    outside the stop_id charset replaced by '?' and truncated to 64
    chars. Neutralises log-forging without losing the diagnostic value
    of seeing which feed/UIC pairing OTP rejected.
    """
    if not value:
        return "-"
    return _LOG_TOKEN_DISALLOWED.sub("?", value)[:64]


# `planConnection` query. $origin / $destination / $dateTime are passed
# as GraphQL variables (input objects — the same variable pattern the
# legacy `plan` query used reliably). `first` and `searchWindow` are
# substituted as inline literals: `searchWindow` is the `Duration`
# scalar (an ISO-8601 string literal, needs quotes) and inlining both
# matches exactly what was verified against the live OTP schema.
#
# `routingErrors` is selected explicitly so we can detect
# LOCATION_NOT_FOUND and retry a stop-id attempt with coordinates (see
# fetch_plan). `node.start` / `node.end` are ISO-8601 OffsetDateTime;
# leg `startTime` / `endTime` remain epoch-ms (OTP keeps both shapes).
_QUERY = """
query Plan($origin: PlanLabeledLocationInput!, $destination: PlanLabeledLocationInput!,
           $dateTime: PlanDateTimeInput) {
  planConnection(origin: $origin, destination: $destination, dateTime: $dateTime,
                 searchWindow: __SEARCH_WINDOW__, first: __NUM_ITINERARIES__) {
    edges {
      node {
        start
        end
        duration
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
    }
    routingErrors { code description }
  }
}
""".strip()


def _otp_base(session_id: str) -> str:
    """Resolve the internal hostname for a session's OTP container."""
    return f"http://otp-{session_id}:8080"


def _location_not_found(raw: dict[str, Any]) -> bool:
    """True iff OTP returned no itineraries AND a `LOCATION_NOT_FOUND`
    routing error.

    Used to decide whether a first-attempt stop-id plan should be retried
    with coordinates. Other routingErrors (NO_TRANSIT_CONNECTION_IN_
    SEARCH_WINDOW, WALKING_BETTER_THAN_TRANSIT, etc.) are NOT triggers —
    those mean OTP located both endpoints and routed, but found no
    acceptable result, which a coordinate retry of the same endpoints
    wouldn't change.
    """
    pc = ((raw or {}).get("data") or {}).get("planConnection") or {}
    if pc.get("edges"):
        return False
    for err in pc.get("routingErrors") or []:
        if (err or {}).get("code") == "LOCATION_NOT_FOUND":
            return True
    return False


def _earliest_departure(when: datetime, session_timezone: str | None) -> str:
    """Render `when` as an ISO-8601 OffsetDateTime for PlanDateTimeInput.

    The journey UI's `datetime-local` input yields a *naive* datetime
    (no offset). The legacy `plan` query sent bare date+time strings,
    which OTP interpreted in the graph's own timezone. `planConnection`'s
    `earliestDeparture` is an `OffsetDateTime` and needs an explicit
    offset — so we localise a naive `when` to the session's configured
    timezone, preserving the old "operator picks 12:51 → OTP searches
    12:51 graph-local" semantics. A `when` that's already tz-aware
    (e.g. `datetime.now(UTC)` when no depart time was given) is used
    as-is. Falls back to UTC if the session has no / an invalid tz.
    """
    if when.tzinfo is None:
        tz: Any = UTC
        if session_timezone:
            try:
                tz = ZoneInfo(session_timezone)
            except (ZoneInfoNotFoundError, ValueError):
                log.warning(
                    "unknown session timezone %r — falling back to UTC for earliestDeparture",
                    session_timezone,
                )
        when = when.replace(tzinfo=tz)
    return when.isoformat()


def _plan_location(stop_id: str | None, lat: float, lon: float) -> dict[str, Any]:
    """Build a PlanLabeledLocationInput.

    When `stop_id` is supplied, route by transit stop (bypasses the
    walk-graph snap); otherwise route by coordinate (the legacy
    behaviour, and the fallback when a stop-id attempt fails).
    """
    if stop_id:
        return {"location": {"stopLocation": {"stopLocationId": stop_id}}}
    return {"location": {"coordinate": {"latitude": lat, "longitude": lon}}}


async def fetch_journeys(
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
) -> tuple[dict[str, Any], list[CanonicalJourney]]:
    """Call OTP's `planConnection`. Returns (raw_response, trips_for_recorder).

    Raises httpx.HTTPError on transport / HTTP failure (caller maps to 'error').

    `num_itineraries` and `search_window_seconds` (v0.1.29) let the caller
    widen OTP's search beyond the live-journey defaults — used by the
    network-coverage runner to fetch the full day's worth of trains for
    each pair in one call (50 itineraries / 24h window) instead of the
    "next 6 h, top 12" the live UI wants. `num_itineraries` maps to the
    connection's `first`; `search_window_seconds` is rendered as an
    ISO-8601 `Duration` literal (`PT<n>S`).

    v0.1.35.04 — bumped live-UI defaults from 8/4 h to **12/6 h**. The
    cross-engine OJP comparison was surfacing legitimate alternatives
    (e.g. Bern → Geneva via Neuchâtel + Renens VD, IR66 → IC5 → IR90)
    that OJP returned but OTP clipped because the 2-transfer route
    ranked outside OTP's Pareto-optimal top 8. 12 itineraries widens
    the slate enough to include 1-2-transfer alternatives in busy
    corridors; 6 h widens the time band so the operator's chosen
    departure time doesn't sit at the edge of the window. Cost: OTP
    RAPTOR's near-quadratic scaling on `searchWindow` means each
    fanout query takes ~1.5x the wall-time of the v0.1.29 defaults —
    on SBB rail-only the difference is ~50 ms, well below the 5 s
    `timeout_ms` ceiling. For dense national feeds (e.g. SNCF-FR) a
    future operator-tunable override would be the right move.

    v0.1.34 — `from_stop_id` / `to_stop_id` (optional): when set, the
    corresponding endpoint is sent as a `stopLocation` instead of a
    coordinate, bypassing the walk-graph snap. If the resulting plan
    comes back empty with `LOCATION_NOT_FOUND` (= the stop_id isn't in
    this session's graph — e.g. the caller built `<feedId>:<uic>` for a
    feed whose stop_ids aren't UIC-based) we transparently retry once
    with coordinates. The caller gets whichever attempt produced results.

    `session_timezone` (optional): the session's configured OTP timezone,
    used to localise a naive `when` for `earliestDeparture` — see
    `_earliest_departure`.

    Why the retry lives in the client and not the API layer: the retry
    decision is purely about OTP's response shape (matching
    `LOCATION_NOT_FOUND` vs other routingErrors), so it belongs next to
    the OTP call.
    """
    # `searchWindow` is the Duration scalar — an ISO-8601 string literal,
    # so the inlined value must carry its own quotes. `first` is a plain
    # Int literal.
    query = _QUERY.replace("__SEARCH_WINDOW__", f'"PT{int(search_window_seconds)}S"').replace(
        "__NUM_ITINERARIES__", str(int(num_itineraries))
    )

    dt_iso = _earliest_departure(when, session_timezone)

    def _payload(use_stop_ids: bool) -> dict[str, Any]:
        return {
            "query": query,
            "variables": {
                "origin": _plan_location(
                    from_stop_id if use_stop_ids else None, from_lat, from_lon
                ),
                "destination": _plan_location(to_stop_id if use_stop_ids else None, to_lat, to_lon),
                "dateTime": {"earliestDeparture": dt_iso},
            },
        }

    # OTP 2.9 GTFS GraphQL endpoint. Note: it is `/otp/gtfs/v1`, NOT
    # `/otp/gtfs/v1/index/graphql` — the `/index/graphql` form was the
    # legacy (Entur/HSL) path served at `/otp/routers/default/index/graphql`
    # which OTP 2.x dropped. Mismatching this returns 404.
    url = f"{_otp_base(session_id)}/otp/gtfs/v1"
    timeout = _plan_timeout(timeout_ms)
    has_stop_ids = bool(from_stop_id or to_stop_id)

    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.post(url, json=_payload(use_stop_ids=has_stop_ids))
        r.raise_for_status()
        raw: dict[str, Any] = r.json()
        # Fallback: stop-id attempt rejected by OTP — the caller supplied
        # a feedId/UIC pairing that doesn't exist in this session's
        # graph. Retry with coordinates.
        if has_stop_ids and _location_not_found(raw):
            log.info(
                "session=%s stop-id planConnection returned LOCATION_NOT_FOUND "
                "(from_stop_id=%s to_stop_id=%s) — retrying with coordinates",
                session_id,
                _safe_log_token(from_stop_id),
                _safe_log_token(to_stop_id),
            )
            r = await c.post(url, json=_payload(use_stop_ids=False))
            r.raise_for_status()
            raw = r.json()

    return raw, otp_adapter.journeys_from_plan(raw, session_id=session_id)


async def fetch_plan(**kwargs: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """`fetch_journeys`, as today's trip dicts with OTP's own itinerary under
    `_raw_itinerary`: for the callers not on canonical journeys yet (coverage and replay,
    #303 PR 4). Same arguments as `fetch_journeys`."""
    raw, journeys = await fetch_journeys(**kwargs)
    return raw, _with_raw_itineraries(raw, journeys)


# Phase budgets other than the read (#388 review), as in `motis_client`.
# OTP is a session-internal hop: a connection it does not accept within
# 5 s is not coming, and before this the connect budget was the whole
# read budget, so the coverage runner's outer `wait_for` (same length)
# fired first and the connect-timeout retry never got a chance. The
# request body is a small GraphQL query and every call opens its own
# client, so 5 s also bounds write and pool. Never longer than before.
_CONNECT_WRITE_POOL_TIMEOUT_S = 5.0


def _plan_timeout(timeout_ms: int) -> httpx.Timeout:
    """httpx timeouts for one plan call: read as before (`timeout_ms`, at
    least 1 s), connect / write / pool at most 5 s."""
    read_s = max(timeout_ms / 1000.0, 1.0)
    short_s = min(_CONNECT_WRITE_POOL_TIMEOUT_S, read_s)
    return httpx.Timeout(connect=short_s, read=read_s, write=short_s, pool=short_s)


def _with_raw_itineraries(
    raw: dict[str, Any], journeys: list[CanonicalJourney]
) -> list[dict[str, Any]]:
    """Today's trip dicts of `journeys` (the temporary `trip_dict` bridge, #303), each with
    OTP's own itinerary node under `_raw_itinerary` for the journey page's JSON inspector; the
    recorder stores only the named keys."""
    edges = (((raw or {}).get("data") or {}).get("planConnection") or {}).get("edges") or []
    return [
        {
            **to_trip_dict(journey),
            "_raw_itinerary": (edges[source_rank(journey)] or {}).get("node") or {},
        }
        for journey in journeys
    ]


def _normalise(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Today's trip dicts for a `planConnection` answer (field mapping in
    `canonical_adapters.otp_adapter`); stored verbatim in `journey_trips.legs`."""
    return _with_raw_itineraries(raw, otp_adapter.journeys_from_plan(raw))
