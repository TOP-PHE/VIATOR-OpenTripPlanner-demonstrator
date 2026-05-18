"""trip_signature canonicaliser — see spec §6.4.

Rules of the canonical form:
  - Stops resolved to UIC via stations_xref → master_stations.uic; missing UIC
    falls back to (lat,lon) rounded to 4 decimals (~11 m).
  - Route names canonicalised via route_aliases (alias → canonical_name).
  - Times rounded to the minute.
  - Hash = sha256[:16] of '|'.join(per-leg fragments).
"""

from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy.orm import Session as DbSession

from ..models import RouteAlias, StationXref


def _round_latlon(lat: float | None, lon: float | None) -> str:
    if lat is None or lon is None:
        return "?,?"
    return f"{round(lat, 4):.4f},{round(lon, 4):.4f}"


def _stop_token(
    db: DbSession, *, session_id: str, stop_id: str | None, lat: float | None, lon: float | None
) -> str:
    if not stop_id:
        return "@" + _round_latlon(lat, lon)
    xref = db.get(StationXref, (session_id, stop_id))
    if xref and xref.uic:
        return f"UIC:{xref.uic}"
    return "@" + _round_latlon(lat, lon)


def _route_canonical(db: DbSession, name: str | None) -> str:
    if not name:
        return ""
    n = name.strip()
    # Look up an alias for `n` → use canonical_name if found.
    row = db.query(RouteAlias).filter(RouteAlias.alias == n).first()
    return row.canonical_name if row else n


def _round_minute(iso: str | None) -> str:
    if not iso:
        return "?"
    # Accept "YYYY-MM-DDTHH:MM:SS+TZ" or epoch-ish; just trim seconds.
    if "T" in iso:
        # take HH:MM
        try:
            return iso.split("T", 1)[1][:5]
        except IndexError:  # pragma: no cover
            return "?"
    return iso[:5]


def trip_signature(db: DbSession, *, session_id: str, legs: list[dict[str, Any]]) -> str:
    """Return a stable 16-hex-char signature for the given leg list."""
    parts: list[str] = []
    for leg in legs:
        mode = (leg.get("mode") or "").upper()
        from_tok = _stop_token(
            db,
            session_id=session_id,
            stop_id=leg.get("from_stop_id"),
            lat=leg.get("from_lat"),
            lon=leg.get("from_lon"),
        )
        to_tok = _stop_token(
            db,
            session_id=session_id,
            stop_id=leg.get("to_stop_id"),
            lat=leg.get("to_lat"),
            lon=leg.get("to_lon"),
        )
        dep = _round_minute(leg.get("departure"))
        arr = _round_minute(leg.get("arrival"))
        route = _route_canonical(db, leg.get("route_short_name"))
        parts.append(f"{mode}:{from_tok}-{to_tok}@{dep}-{arr}#{route}")
    canonical = "|".join(parts)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def transit_fingerprint(legs: list[dict[str, Any]]) -> str:
    """16-hex stable fingerprint of an itinerary's *transit* leg spine.

    Designed for **cross-engine** comparison — VIATOR's OTP results vs
    the Swiss OJP reference. Walk and transfer legs are stripped, so an
    OJP itinerary with an explicit `Origin → Pontarlier` access walk
    still matches an OTP itinerary that started *at* the Pontarlier stop
    directly (which is what stop-id routing emits — no end walks).

    Per-transit-leg fragment::

        MODE:lat,lon-lat,lon@HH:MM-HH:MM#ROUTE

    where coordinates are rounded to 4 decimals (~11 m). The rounding
    is what makes cross-feed matching work: OTP emits a Bern stop as
    ``SBB:8507000`` while OJP emits the same stop as
    ``ch:1:sloid:7000:4:8`` — opaque, non-equal strings — but both
    report the same physical lat/lon to within metres. The
    `trip_signature` function above uses stations_xref + UIC for
    *within-feed* matching; this function deliberately does not, because
    `stations_xref` has no entries for the synthetic ``OJP`` feed.

    Times are rounded to the minute and the route name is uppercased /
    stripped. The fingerprint of an itinerary with no transit legs (all
    WALK / TRANSFER, or empty) is the empty string — callers treat that
    as "no comparable spine" rather than letting empty-string-equals-
    empty-string create false matches between two walk-only routes.

    DB-free by design — same input data the journey UI consumes from
    `/api/journey/fanout`, so it can be called both server-side
    (`app/api/journey.py` Phase 2 bucketing) and in unit tests against
    captured fixtures without setting up a database.
    """
    parts: list[str] = []
    for leg in legs:
        mode = (leg.get("mode") or "").upper()
        if mode in ("", "WALK", "TRANSFER"):
            continue
        from_tok = _round_latlon(leg.get("from_lat"), leg.get("from_lon"))
        to_tok = _round_latlon(leg.get("to_lat"), leg.get("to_lon"))
        dep = _round_minute(leg.get("departure"))
        arr = _round_minute(leg.get("arrival"))
        route = (leg.get("route_short_name") or "").strip().upper()
        parts.append(f"{mode}:{from_tok}-{to_tok}@{dep}-{arr}#{route}")
    if not parts:
        return ""
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]
