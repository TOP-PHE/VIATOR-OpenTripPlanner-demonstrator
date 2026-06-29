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
the cap is implicit in how fast a human clicks.
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


def _build_trip_search_body(
    *,
    from_lat: float,
    from_lon: float,
    to_lat: float,
    to_lon: float,
    depart_at: datetime,
) -> dict[str, Any]:
    """Construct the JSON envelope HAFAS expects for a TripSearch.

    Coords are passed as `crd: {x: lon, y: lat}` (note the swap — HAFAS
    convention is x=longitude, y=latitude, not the lat/lon order most
    transport tools use). Date and time are local-tz strings (HAFAS
    interprets in the operator's TZ, which is Europe/Vienna for ÖBB)."""
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
                    "depLocL": [
                        {
                            "type": "C",
                            "crd": {
                                "x": _coord_to_micro(from_lon),
                                "y": _coord_to_micro(from_lat),
                            },
                        }
                    ],
                    "arrLocL": [
                        {
                            "type": "C",
                            "crd": {
                                "x": _coord_to_micro(to_lon),
                                "y": _coord_to_micro(to_lat),
                            },
                        }
                    ],
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

    `client` is injected for tests; production callers pass None and
    we manage a one-shot AsyncClient internally. Network / parse
    failures produce a VerifyResult with `ok=False` and `error` set —
    never raises to the caller, since the UI surface treats "unknown"
    as a distinct visual state from "external said no"."""
    body = _build_trip_search_body(
        from_lat=from_lat,
        from_lon=from_lon,
        to_lat=to_lat,
        to_lon=to_lon,
        depart_at=depart_at,
    )
    headers = {
        "User-Agent": _USER_AGENT,
        "Content-Type": "application/json;charset=UTF-8",
        "Accept": "application/json",
    }

    async def _do_request(c: httpx.AsyncClient) -> VerifyResult:
        try:
            response = await c.post(_OEBB_ENDPOINT, json=body, headers=headers)
        except httpx.HTTPError as e:
            log.warning("HAFAS request failed: %s", e)
            return VerifyResult(source=_SOURCE_OEBB_HAFAS, ok=False, error=f"http: {e}")
        if response.status_code != 200:
            return VerifyResult(
                source=_SOURCE_OEBB_HAFAS,
                ok=False,
                error=f"HTTP {response.status_code}",
            )
        try:
            payload = _decode_response_body(response.content)
        except (ValueError, json.JSONDecodeError) as e:
            return VerifyResult(source=_SOURCE_OEBB_HAFAS, ok=False, error=f"json: {e}")
        return _parse_hafas_response(payload)

    if client is not None:
        return await _do_request(client)
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as c:
        return await _do_request(c)


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
        return VerifyResult(source=_SOURCE_OEBB_HAFAS, ok=False, error=f"hafas svc: {svc['err']}")
    res = svc.get("res") or {}
    connections = res.get("outConL") or []
    return _summarise_connections(connections)
