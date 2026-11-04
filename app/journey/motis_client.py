"""Thin async client for MOTIS — drop-in alternative to otp_client.

Phase-0 spike (see motis-spike/README.md). Mirrors `otp_client.fetch_journeys`'s
signature and return shape exactly, so once we validate the spike, the
session-level dispatcher in Phase 1 is a one-liner:

    client = motis_client if session.engine == "motis" else otp_client
    raw, journeys = await client.fetch_journeys(session_id=..., ...)

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

import asyncio
import logging
import weakref
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from .canonical import CanonicalJourney
from .canonical_adapters import motis_adapter

log = logging.getLogger(__name__)


_INTERNAL_SCHEME = "http"

# #397 — one httpx.AsyncClient per event loop, shared by every MOTIS call on
# that loop, instead of a new client (and its SSL context, built eagerly by
# httpx even for plain http) per call. Per loop because an AsyncClient belongs
# to the loop it first ran on: web's loop, the worker's coverage executor loop
# and each test's loop get their own. No connection pool to contend for: every
# request still says `Connection: close` (PR-188, passed with each request,
# not as a client default) and the client keeps no idle
# connection and sets no connection cap, as the per-call clients did (a capped
# pool would add a PoolTimeout under a large fan-out). The per-request
# timeouts are passed with each request, unchanged. Closed by
# `aclose_shared_client` (web's shutdown, the executor's loop end).
_HEADERS = {"Connection": "close"}
_LIMITS = httpx.Limits(max_connections=None, max_keepalive_connections=0)
_SHARED_CLIENTS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, httpx.AsyncClient] = (
    weakref.WeakKeyDictionary()
)


def _shared_client() -> httpx.AsyncClient:
    """The running loop's MOTIS client, created on first use (or again after
    it was closed)."""
    loop = asyncio.get_running_loop()
    client = _SHARED_CLIENTS.get(loop)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(limits=_LIMITS)
        _SHARED_CLIENTS[loop] = client
    return client


async def aclose_shared_client() -> None:
    """Close the running loop's MOTIS client, if it has one (shutdown)."""
    client = _SHARED_CLIENTS.pop(asyncio.get_running_loop(), None)
    if client is not None:
        await client.aclose()


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
    base_url: str | None = None,
) -> tuple[dict[str, Any], list[CanonicalJourney]]:
    """Call MOTIS's `/api/v6/plan`. Returns `(raw_response, trips_for_recorder)`.

    Mirrors `otp_client.fetch_journeys` argument-for-argument so the dispatcher
    (Phase 1) can pick the client by `session.engine` without touching the
    callsite. The `base_url` override is a spike-only convenience for pointing
    the comparison harness at `http://localhost:8081`; production sessions
    leave it unset and use the per-session DNS name.

    Raises `httpx.HTTPError` on transport / HTTP failure (caller maps to
    `'error'`, same contract as OTP). A date outside the loaded timetable is
    an HTTP 400 that `timetable_window.from_motis_refusal` recognises (#338).
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
    # `otp_client.fetch_journeys` but deliberately unused.
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
    # The exact anchor sent to MOTIS, to diff against external_verify's HAFAS
    # log for the same search (2026-07). DEBUG since #397: one line per call
    # was an INFO line per K-slot of every coverage pair.
    log.debug("motis.plan.request session_id=%s time=%s", session_id, when.isoformat())
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
    r = await _shared_client().get(url, params=params, timeout=timeout, headers=_HEADERS)
    r.raise_for_status()
    raw: dict[str, Any] = r.json()

    return raw, motis_adapter.journeys_from_plan(raw, session_id=session_id)
