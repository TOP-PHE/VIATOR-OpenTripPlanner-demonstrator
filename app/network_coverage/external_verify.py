"""External-planner verification for coverage cells.

Lets an operator click a `no_route` cell in the matrix and ask "does
ÖBB's own planner also fail this pair, or does it return a route we're
missing in our NAP feeds?" If ÖBB finds a route too, our data is the
gap; if ÖBB also fails, the gap is real (no scheduled service at that
depart-time).

We talk to ÖBB's HAFAS endpoint directly — the same `mgate.exe` JSON
service that powers the ÖBB Scotty mobile app. This is what the
`hafas-client` JS library (https://github.com/public-transport/hafas-
client) has been doing for ~10 years; the protocol is proprietary but
well-understood and the credentials below are the publicly-published
Scotty app id used by every hafas-client install. We pass a polite
identifying User-Agent rather than masquerading as the mobile app,
since the goal is comparison data and we're not trying to evade
detection.

Why ÖBB and not DB: DB's `reiseauskunft.bahn.de/bin/mgate.exe` was
silently retired in mid-2026. ÖBB's instance is alive, uses the same
HAFAS protocol family, and (verified empirically on 43 EU rail
corridor pairs) covers DACH + cross-border partners + Eurostar/TGV/
AVE/Iberian and Nordic-cross-border services — broader than DB ever
did. The one confirmed gap is Norwegian domestic (Vy/NSB Bergensbanen)
which isn't in ÖBB's data pool.

Two-step lookup: ÖBB rejects coord-only `TripSearch` (type:"C") with
H9220 ("no stop near coords") even for canonical hubs like Köln Hbf
and Frankfurt (Main) Hbf — its coord-snap is stricter than DB's was.
Hubs in our DB carry good coords (within ~25m of the real station),
but ÖBB still won't snap. So we resolve in two steps: first a
`LocGeoPos` to convert coords to station lids, then a `TripSearch`
with `type:"S"` station-based lookup. This is the path hafas-client
uses universally.

What this is NOT:
  - A scraper of oebb.at's HTML — ÖBB's ToS prohibits automated access
    to the website. The HAFAS backend path used here is the legitimate
    alternative.
  - A replacement for HACON's paid partner API. For high-volume use
    (millions/day) the partner API is the right answer; for operator-
    driven verification of a handful of coverage gaps, the public
    endpoint is fine.

Rate limit: HAFAS doesn't publish one, but the practical safe ceiling
is ~1 request/second per origin IP. We don't enforce that here because
this surface is operator-driven (click-to-verify on individual cells);
the cap is implicit in how fast a human clicks. Note that one verify
now costs 2 HTTP round-trips (LocGeoPos + TripSearch) — still well
under any sane rate cap.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

import httpx
from pydantic import BaseModel

log = logging.getLogger(__name__)


# ─────────────────────── HAFAS profile ───────────────────────
#
# ÖBB Scotty mobile-app credentials. Public — used by every hafas-
# client install on the planet. ÖBB hasn't rotated them since at least
# 2019.

_OEBB_ENDPOINT = "https://fahrplan.oebb.at/bin/mgate.exe"
_OEBB_AID = "OWDL4fE4ixNiPBBm"
# Source label propagated on every VerifyResult. Single constant so the
# UI verdict-colour logic can equality-check it (and Sonar S1192 is happy
# with the literal not duplicated 9 times across the error branches).
_SOURCE_OEBB_HAFAS = "fahrplan.oebb.at"
_OEBB_CLIENT = {
    "id": "OEBB",
    "v": "6030600",
    "type": "AND",
    "name": "oebb",
}
_OEBB_VER = "1.42"

# Identify ourselves rather than masquerading — ÖBB tolerates known
# clients, and "VIATOR-coverage-verify" is honest about why we're here.
_USER_AGENT = (
    "VIATOR-coverage-verify/1.0 (+https://github.com/TOP-PHE/VIATOR-OpenTripPlanner-demonstrator)"
)

# HAFAS can be slow under load — give it room. Operator is waiting on
# the modal, so cap at 30s to fail fast on a hung backend.
_HTTP_TIMEOUT_SECONDS = 30.0

# LocGeoPos search radius for coord→station resolution. 5 km is
# generous: our hubs are typically within 25 m of the real station,
# but very-rural origins (e.g. a future hub at an obscure stop)
# might be hundreds of meters off. 5 km still safely picks the
# *intended* station rather than a wrong one in metropolitan areas
# (no two mainline stations are this close).
_OEBB_RESOLVE_RADIUS_METERS = 5000

# Friendly translations for the HAFAS error codes operators are
# most likely to see. Anything not in this table falls through to
# the raw "hafas svc: <code>" form — an escape hatch for new codes
# we haven't catalogued yet. H890 is special-cased earlier as the
# "real no-route" answer (ok=False, error=None).
_HAFAS_ERROR_MESSAGES: dict[str, str] = {
    "H9220": "no station found near the supplied coordinates",
    "H9230": "ÖBB backend internal error",
    "H9240": "ÖBB backend search timeout",
    "H9250": "the combination of train products is not allowed",
    "H9300": "internal error during address search",
}


class VerifyResult(BaseModel):
    """Outcome of one external-planner check for one coverage cell.

    `ok=True` means the external planner returned at least one
    connection (so VIATOR's `no_route` likely indicates missing data,
    not a real gap). `ok=False` with `error=None` means the external
    cleanly returned zero connections (a real "no service" answer).
    `ok=False` with `error` set means we couldn't reach the external
    backend; the verdict is "unknown" not "no route".
    """

    source: str
    ok: bool
    num_connections: int = 0
    best_duration_seconds: int | None = None
    best_transfers: int | None = None
    # When set, the verdict is "we couldn't get an answer" rather than
    # "external said no". UI renders this as a yellow warning, not a
    # red/green verdict.
    error: str | None = None


# ─────────────────────── HAFAS protocol bits ───────────────────────


def _coord_to_micro(value: float) -> int:
    """HAFAS coordinates are integer micro-degrees (lat * 1e6, lon * 1e6).
    Float input is rounded to the nearest integer — sub-metre precision
    is meaningless for trip planning anyway."""
    return round(value * 1_000_000)


def _translate_hafas_error(code: str) -> str:
    """Translate a HAFAS service-level error code into a friendly
    message. Falls back to the raw code so new ones still surface."""
    friendly = _HAFAS_ERROR_MESSAGES.get(code)
    if friendly:
        return friendly
    return f"hafas svc: {code}"


def _build_locgeopos_body(coord_pairs: list[tuple[float, float]]) -> dict[str, Any]:
    """Build a HAFAS `LocGeoPos` envelope that resolves N coordinate
    pairs to their nearest stations in one POST.

    Each coord is converted to integer micro-degrees and wrapped in a
    `ring` with `maxDist=5000` metres. `getStops=true, getPOIs=false`
    constrains the response to railway stops only (not addresses or
    POIs that share the area). `maxLoc=1` says "give me the single
    closest stop" — anything beyond that we'd just ignore."""
    return {
        "auth": {"type": "AID", "aid": _OEBB_AID},
        "client": _OEBB_CLIENT,
        "ver": _OEBB_VER,
        "lang": "eng",
        "formatted": False,
        "svcReqL": [
            {
                "meth": "LocGeoPos",
                "req": {
                    "ring": {
                        "cCrd": {
                            "x": _coord_to_micro(lon),
                            "y": _coord_to_micro(lat),
                        },
                        "maxDist": _OEBB_RESOLVE_RADIUS_METERS,
                        "minDist": 0,
                    },
                    "maxLoc": 1,
                    "getStops": True,
                    "getPOIs": False,
                },
            }
            for (lat, lon) in coord_pairs
        ],
    }


def _extract_lids_from_locgeopos(payload: dict[str, Any], count: int) -> list[str | None]:
    """Extract the lid (HAFAS location identifier) of the nearest stop
    from each `LocGeoPos` result in the payload.

    Returns one entry per expected svcResL slot. A slot's value is None
    if that LocGeoPos either failed at the service level (svc.err != OK)
    or returned an empty locL (no stop within radius). The caller decides
    how to handle a None — typically by returning a friendly "no station
    found" VerifyResult."""
    out: list[str | None] = []
    svc_res = payload.get("svcResL") or []
    for i in range(count):
        if i >= len(svc_res):
            out.append(None)
            continue
        svc = svc_res[i]
        if svc.get("err") and svc["err"] != "OK":
            out.append(None)
            continue
        loc_l = (svc.get("res") or {}).get("locL") or []
        if not loc_l:
            out.append(None)
            continue
        lid = loc_l[0].get("lid")
        out.append(lid if isinstance(lid, str) and lid else None)
    return out


def _build_trip_search_body(
    *,
    from_lid: str,
    to_lid: str,
    depart_at: datetime,
) -> dict[str, Any]:
    """Construct the JSON envelope HAFAS expects for a TripSearch.

    Uses `type:"S"` (station) with the pre-resolved lids from
    LocGeoPos. Date and time are local-tz strings (HAFAS interprets in
    the operator's TZ, which is Europe/Vienna for ÖBB)."""
    return {
        "auth": {"type": "AID", "aid": _OEBB_AID},
        "client": _OEBB_CLIENT,
        "ver": _OEBB_VER,
        "lang": "eng",
        "formatted": False,
        "svcReqL": [
            {
                "meth": "TripSearch",
                "req": {
                    "depLocL": [{"type": "S", "lid": from_lid}],
                    "arrLocL": [{"type": "S", "lid": to_lid}],
                    "outDate": depart_at.strftime("%Y%m%d"),
                    "outTime": depart_at.strftime("%H%M%S"),
                    "numF": 5,
                    "getPolyline": False,
                    "getPasslist": False,
                },
            }
        ],
    }


def _parse_hafas_duration(value: str | None) -> int | None:
    """HAFAS durations are strings like `040000` for 4h00m00s (or the
    longer `0102030000` form when crossing midnight: DDHHMMSS-ish).
    Returns total seconds, or None if the string doesn't parse."""
    if not value:
        return None
    s = value.zfill(6)
    try:
        # Last 6 digits = HHMMSS; anything before that = days (rare).
        days_part = s[:-6] or "0"
        hhmmss = s[-6:]
        days = int(days_part)
        hours = int(hhmmss[0:2])
        mins = int(hhmmss[2:4])
        secs = int(hhmmss[4:6])
        return days * 86400 + hours * 3600 + mins * 60 + secs
    except ValueError:
        return None


def _summarise_connections(connections: list[dict[str, Any]]) -> VerifyResult:
    """Reduce a HAFAS `outConL` list to a single VerifyResult.

    `best_duration_seconds` is the minimum of the returned connections
    (HAFAS doesn't guarantee they're sorted shortest-first; ranking
    differs between profiles)."""
    if not connections:
        return VerifyResult(source=_SOURCE_OEBB_HAFAS, ok=False, num_connections=0)
    parsed_durations: list[int] = []
    parsed_transfers: list[int] = []
    for c in connections:
        d = _parse_hafas_duration(c.get("dur"))
        if d is not None:
            parsed_durations.append(d)
        chg = c.get("chg")
        if isinstance(chg, int):
            parsed_transfers.append(chg)
    best_idx = (
        min(range(len(parsed_durations)), key=lambda i: parsed_durations[i])
        if parsed_durations
        else None
    )
    return VerifyResult(
        source=_SOURCE_OEBB_HAFAS,
        ok=True,
        num_connections=len(connections),
        best_duration_seconds=parsed_durations[best_idx] if best_idx is not None else None,
        best_transfers=(
            parsed_transfers[best_idx]
            if best_idx is not None and best_idx < len(parsed_transfers)
            else None
        ),
    )


def _decode_response_body(raw: bytes) -> Any:
    """Decode a HAFAS response body to a parsed JSON object.

    ÖBB's mgate returns UTF-8 in practice, but the sibling ajax-getstop
    endpoint returns Latin-1, and we've seen field-level Latin-1 leak
    into mgate during outages. Try UTF-8 first; fall back to Latin-1
    (which never raises on any byte sequence) so a transient encoding
    quirk doesn't trip an `error` verdict on otherwise-valid responses.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    return json.loads(text)


# ─────────────────────── public API ───────────────────────


_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Content-Type": "application/json;charset=UTF-8",
    "Accept": "application/json",
}


async def _post_hafas(
    client: httpx.AsyncClient, body: dict[str, Any]
) -> tuple[dict[str, Any] | None, VerifyResult | None]:
    """POST a HAFAS request envelope. Returns `(payload, None)` on
    success or `(None, error-VerifyResult)` on transport / parse
    failure — never raises to the caller."""
    try:
        response = await client.post(_OEBB_ENDPOINT, json=body, headers=_HEADERS)
    except httpx.HTTPError as e:
        log.warning("HAFAS request failed: %s", e)
        return None, VerifyResult(source=_SOURCE_OEBB_HAFAS, ok=False, error=f"http: {e}")
    if response.status_code != 200:
        return None, VerifyResult(
            source=_SOURCE_OEBB_HAFAS,
            ok=False,
            error=f"HTTP {response.status_code}",
        )
    try:
        payload = _decode_response_body(response.content)
    except ValueError as e:
        # json.JSONDecodeError is a subclass of ValueError, so this
        # catches both the parse failure and any future ValueError
        # raised by _decode_response_body.
        return None, VerifyResult(source=_SOURCE_OEBB_HAFAS, ok=False, error=f"json: {e}")
    return payload, None


async def verify_via_oebb_hafas(
    *,
    from_lat: float,
    from_lon: float,
    to_lat: float,
    to_lon: float,
    depart_at: datetime,
    client: httpx.AsyncClient | None = None,
) -> VerifyResult:
    """Ask ÖBB's HAFAS backend whether it can route this pair.

    Two-step internally: first resolve both coords to station lids via
    LocGeoPos, then run TripSearch with `type:"S"`. Returns a single
    VerifyResult either way — the two-POST shape is invisible to the
    caller.

    `client` is injected for tests; production callers pass None and
    we manage a one-shot AsyncClient internally. Network / parse
    failures produce a VerifyResult with `ok=False` and `error` set —
    never raises to the caller, since the UI surface treats "unknown"
    as a distinct visual state from "external said no"."""
    resolve_body = _build_locgeopos_body([(from_lat, from_lon), (to_lat, to_lon)])

    async def _run(c: httpx.AsyncClient) -> VerifyResult:
        # Step 1: coord → station lid resolution.
        resolve_payload, err = await _post_hafas(c, resolve_body)
        if err is not None:
            return err
        if resolve_payload is None:  # pragma: no cover — defensive
            return VerifyResult(source=_SOURCE_OEBB_HAFAS, ok=False, error="no resolve payload")
        if resolve_payload.get("err") and resolve_payload["err"] != "OK":
            return VerifyResult(
                source=_SOURCE_OEBB_HAFAS,
                ok=False,
                error=f"hafas envelope: {resolve_payload['err']}",
            )
        lids = _extract_lids_from_locgeopos(resolve_payload, count=2)
        from_lid, to_lid = lids[0], lids[1]
        if not from_lid or not to_lid:
            # One or both endpoints don't snap to an ÖBB station. This
            # is informative ("ÖBB doesn't have this stop in its
            # catalogue") rather than a true backend failure — but
            # we can't route without IDs, so surface as yellow with
            # the friendly H9220-equivalent message.
            return VerifyResult(
                source=_SOURCE_OEBB_HAFAS,
                ok=False,
                error=_HAFAS_ERROR_MESSAGES["H9220"],
            )

        # Step 2: trip search using the resolved station lids.
        trip_body = _build_trip_search_body(from_lid=from_lid, to_lid=to_lid, depart_at=depart_at)
        trip_payload, err2 = await _post_hafas(c, trip_body)
        if err2 is not None:
            return err2
        if trip_payload is None:  # pragma: no cover — defensive
            return VerifyResult(source=_SOURCE_OEBB_HAFAS, ok=False, error="no trip payload")
        return _parse_hafas_response(trip_payload)

    if client is not None:
        return await _run(client)
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as c:
        return await _run(c)


def _parse_hafas_response(payload: dict[str, Any]) -> VerifyResult:
    """HAFAS error reporting is two-layered: the envelope `err` for
    transport errors (`"OK"` on success), and the per-service `err`
    inside `svcResL[i]`. Anything other than `"OK"` at either level
    means no usable connections returned."""
    if payload.get("err") and payload["err"] != "OK":
        return VerifyResult(
            source=_SOURCE_OEBB_HAFAS, ok=False, error=f"hafas envelope: {payload['err']}"
        )
    svc_res = payload.get("svcResL") or []
    if not svc_res:
        return VerifyResult(source=_SOURCE_OEBB_HAFAS, ok=False, error="no svcResL")
    svc = svc_res[0]
    if svc.get("err") and svc["err"] != "OK":
        # `H890` is HAFAS's "no connections found" code — meaningful
        # negative answer, not a transport error.
        if svc["err"] == "H890":
            return VerifyResult(source=_SOURCE_OEBB_HAFAS, ok=False, num_connections=0)
        return VerifyResult(
            source=_SOURCE_OEBB_HAFAS, ok=False, error=_translate_hafas_error(svc["err"])
        )
    res = svc.get("res") or {}
    connections = res.get("outConL") or []
    return _summarise_connections(connections)
