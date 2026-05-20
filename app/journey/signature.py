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
import re
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


# Stop-id formats observed in the field:
#
#   SBB / OJP / OTP-Swiss — 7-digit UIC:
#     "SBB:8507000"          → UIC 8507000 (Bern)
#     "SBB:8507000:0:7"      → same UIC + platform suffix
#     "SBB:8501008:0:3"      → UIC 8501008 (Geneva)
#
#   SNCF — 8-digit form (7-digit UIC + a trailing check digit):
#     "StopPoint:OCELyria-87686006" → 87686006 = UIC 8768600 + check 6
#     "StopArea:OCE85010082"        → 85010082 = UIC 8501008 + check 2
#     (verified against the live SNCF GTFS — TGV Lyria 9263 — vs the SBB
#     GTFS describing the SAME train with 7-digit UICs. Both must reduce
#     to the same 7-digit core, else cross-NAP fingerprints mismatch.)
#
#   OJP — opentransportdata.swiss SLOID, drops the `850` Swiss
#   country/DiDok prefix and uses the 4-digit DSN (DiDok number):
#     "ch:1:sloid:7000:4:7"  → DSN 7000, reconstructed UIC = 850+7000 = 8507000
#     "ch:1:sloid:1008:2:3"  → DSN 1008, reconstructed UIC = 8501008
#     "ch:1:sloid:1120:0:5"  → DSN 1120, reconstructed UIC = 8501120 (Lausanne)
#
# Forms regex-anchored on `(?<!\d)…(?!\d)` so we don't pick a substring
# of a longer run of digits or a sub-stop index of the right length.
# The 7-or-8-digit pattern captures both the SBB 7-digit UIC and the
# SNCF 8-digit (UIC+check) form; `_uic_from_stop_id` keeps the first 7.
_UIC_RE = re.compile(r"(?<!\d)(\d{7,8})(?!\d)")
_SWISS_DSN_RE = re.compile(r"(?<!\d)(\d{4})(?!\d)")
_SWISS_SLOID_PREFIX = "ch:1:"  # opentransportdata.swiss authority namespace


def _uic_from_stop_id(stop_id: str | None) -> str | None:
    """Parse the canonical 7-digit UIC out of an OTP / OJP / SNCF stop_id.

    Strategy:
      1. If the id contains a 7- or 8-digit chunk, take its first 7 digits.
         Covers SBB/OTP 7-digit UICs (``SBB:8507000:0:7`` → 8507000) AND
         the SNCF 8-digit form (``…87686006`` → 8768600), which is the
         7-digit UIC plus a trailing check digit. Reducing both to 7
         digits is what lets a SNCF leg and an SBB leg of the same
         cross-border train (TGV Lyria) fingerprint identically.
      2. If the id is in the Swiss OJP authority namespace (``ch:1:…``)
         and contains a 4-digit chunk, treat that as a Swiss DSN
         (DiDok-Nummer) and prepend ``850`` to reconstruct the full UIC.
         ``ch:1:sloid:7000:4:7`` → ``8507000``.
      3. Otherwise return None — the caller falls back to a lat/lon token.

    v0.1.35.02 did step 1 (7-digit only). v0.1.35.03 added step 2 (Swiss
    DSN) after the OJP comparison data. The 8-digit handling here was
    added when the cross-NAP federation spike (SNCF vs SBB describing
    the SAME TGV Lyria) revealed SNCF publishes 8-digit codes while SBB
    publishes 7-digit — so an unmodified 7-digit matcher made the two
    NAPs' copies of one train mismatch.
    """
    if not stop_id:
        return None
    m = _UIC_RE.search(stop_id)
    if m:
        # 8-digit = 7-digit UIC + trailing check digit → keep first 7.
        return m.group(1)[:7]
    if stop_id.startswith(_SWISS_SLOID_PREFIX):
        m = _SWISS_DSN_RE.search(stop_id)
        if m:
            return "850" + m.group(1)
    return None


def _round_latlon_coarse(lat: float | None, lon: float | None) -> str:
    """Same shape as `_round_latlon` but rounded to 3 decimals (~110 m).

    Used by the cross-engine `transit_fingerprint` instead of the 4-dp
    rounding the within-feed `trip_signature` uses. Rationale: OTP emits
    *platform-precise* coordinates (e.g. Lausanne CFF platform 5 vs
    platform 4 are 130 m apart and round to different 4-dp tokens within
    the same itinerary); OJP typically emits a single station-centroid
    coordinate. At 4-dp neither engine matches itself reliably, let
    alone the other. 3-dp collapses both engines' platform/centroid
    differences while still distinguishing genuinely-different rail
    stations — Pontarlier and Frasne are 15 km apart, even Zürich HB and
    Zürich Stadelhofen are 700 m apart, both well above 110 m.
    """
    if lat is None or lon is None:
        return "?,?"
    return f"{round(lat, 3):.3f},{round(lon, 3):.3f}"


def _fingerprint_stop_token(stop_id: str | None, lat: float | None, lon: float | None) -> str:
    """Return the per-endpoint stop token used by `transit_fingerprint`.

    Strategy:
      1. If the stop_id contains a 7-digit UIC chunk, return `UIC:NNNNNNN`.
         This is the strongest cross-engine identifier — both OTP's
         ``SBB:8501120:0:5`` and OJP's ``ch:1:sloid:8501120:0:5`` produce
         ``UIC:8501120`` (and don't care about platform suffixes).
      2. Otherwise fall back to ``lat,lon`` rounded to 3 decimals (~110 m).
         Catches stations on non-Swiss feeds and the no-stop-id endpoints
         of access/egress walks. Walks are stripped before this is called,
         so the only callers are RAIL/BUS/TRANSIT legs which both engines
         resolve to a stop with an id.
    """
    uic = _uic_from_stop_id(stop_id)
    if uic:
        return f"UIC:{uic}"
    return _round_latlon_coarse(lat, lon)


def transit_fingerprint(legs: list[dict[str, Any]]) -> str:
    """16-hex stable fingerprint of an itinerary's *transit* leg spine.

    Designed for **cross-engine** comparison — VIATOR's OTP results vs
    the Swiss OJP reference. Walk and transfer legs are stripped, so an
    OJP itinerary with an explicit `Origin → Pontarlier` access walk
    still matches an OTP itinerary that started *at* the Pontarlier stop
    directly (which is what stop-id routing emits — no end walks).

    Per-transit-leg fragment::

        MODE:STOP-STOP@HH:MM-HH:MM#ROUTE

    where each STOP is either ``UIC:NNNNNNN`` (parsed from the stop_id)
    or ``lat,lon`` rounded to 3 decimals (~110 m) when no UIC is
    available. UIC matching is what makes a TGV→IC1 connection at
    Lausanne fingerprint identically across engines: OTP returns
    platform-precise coordinates (``SBB:8501120:0:5`` arr lat ≠
    ``SBB:8501120:0:4`` dep lat by ~30 m even within one itinerary),
    OJP returns the station centroid — but both stop_ids carry the same
    UIC ``8501120`` so the token agrees. The 3-dp lat/lon fallback
    handles non-Swiss feeds and absorbs typical cross-feed centroid
    variance (~20-100 m).

    Why 3-dp rather than 4-dp like the within-feed `trip_signature`:
    OTP's platform precision is finer than 11 m, so two consecutive
    legs at the same station get different 4-dp tokens even within one
    engine. 110 m precision collapses platforms while still
    distinguishing different stations.

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
        from_tok = _fingerprint_stop_token(
            leg.get("from_stop_id"), leg.get("from_lat"), leg.get("from_lon")
        )
        to_tok = _fingerprint_stop_token(
            leg.get("to_stop_id"), leg.get("to_lat"), leg.get("to_lon")
        )
        dep = _round_minute(leg.get("departure"))
        arr = _round_minute(leg.get("arrival"))
        route = (leg.get("route_short_name") or "").strip().upper()
        parts.append(f"{mode}:{from_tok}-{to_tok}@{dep}-{arr}#{route}")
    if not parts:
        return ""
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]
