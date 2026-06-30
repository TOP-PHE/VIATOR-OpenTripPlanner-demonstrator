"""Async journey-level client for ÖBB's public HAFAS endpoint.

VIATOR's own routing runs on OpenTripPlanner (see `otp_client.py`); this
module talks to an *external* HAFAS backend (`fahrplan.oebb.at/bin/
mgate.exe`) so the journey UI can show ÖBB's reference engine side-by-
side with VIATOR's own results, as a validation oracle on the broader
DACH + cross-border + Eurostar/TGV/AVE/Iberian + Nordic-cross-border
network footprint Swiss OJP doesn't cover. See the module docstring on
`app/network_coverage/external_verify.py` for the 43-pair empirical
coverage validation that motivated picking ÖBB HAFAS as the reference.

This module is the **journey-level** counterpart of the coverage-cell
verify path. The HTTP plumbing (LocGeoPos → TripSearch two-step,
header set, error-code translation) lives in `external_verify` and is
shared verbatim — this file is the trip-dict normaliser layer that
turns HAFAS's `outConL[].secL[]` shape into VIATOR's canonical trip
dict (the same shape `otp_client._normalise` / `motis_client.
_itineraries_to_trips` / `ojp_client._normalise` emit). Once a HAFAS
connection has been normalised this way, the comparison panel renders
it through the same code path as the other engines.

Why a journey-level adapter on top of a coverage adapter
--------------------------------------------------------
`external_verify.verify_via_oebb_hafas` was built for the click-to-
verify modal on `no_route` cells: it asks "does ÖBB find anything?"
and returns a single yes/no `VerifyResult`. Journey-search reuses the
same backend but needs the full itinerary list (durations, transfers,
legs with operator badges, intermediate-station detail). PR refactor
extracted `external_verify.fetch_oebb_two_step` which returns both the
verdict *and* the parsed payload; this module consumes that payload.

Contract mirrors the other planner clients
------------------------------------------
`fetch_plan(...)` returns `(raw, trips)` where `trips` is the list of
canonical trip dicts. Transport / HTTP errors are mapped to an empty
trip list (HAFAS errors are returned via the `verdict.error` field
inside the raw payload, never raised) — the caller decides whether to
surface them as "unavailable" or "no_route" by checking `raw.status`.
PR-3's `first_transit_leg_departure_utc` is included so cross-engine
dedup / day-window filtering treats HAFAS itineraries identically to
the others.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from ..network_coverage import external_verify
from .trip_normalize import first_transit_leg_departure_utc as _first_transit_leg_departure_utc

log = logging.getLogger(__name__)


# HAFAS times are expressed in the operator's local timezone (ÖBB =
# Europe/Vienna). The journey UI's `datetime-local` input is naive, so a
# naive `when` is localised against this zone for the TripSearch's
# `outDate`/`outTime`. Trip-leg `departure` / `arrival` ISO strings
# coming back out of the normaliser are converted to UTC for consistency
# with the other clients.
_REFERENCE_TZ = "Europe/Vienna"

# Source label propagated on every trip dict (via `feed_id` on each leg
# and the response's source attribution). Matches the constant used by
# the coverage-cell verify path so the journey UI's per-engine badge
# logic doesn't need a separate branch.
_SOURCE_OEBB_HAFAS = "fahrplan.oebb.at"


# ─────────────────────── public API ───────────────────────


async def fetch_plan(
    *,
    from_lat: float,
    from_lon: float,
    to_lat: float,
    to_lon: float,
    when: datetime,
    timeout_ms: int = 10_000,
    num_itineraries: int = 6,
    from_name: str | None = None,
    to_name: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Call ÖBB HAFAS and return `(raw, trips)`.

    Mirrors `ojp_client.fetch_reference`'s signature — the journey API
    layer uses both interchangeably under different operator toggles.

    `raw` carries a small diagnostic dict (`format`, `status`,
    `from_lid`, `to_lid`, `error`) so the comparison panel can show
    "Reference (ÖBB HAFAS) · N itineraries · Mms" with proper status
    differentiation. The full HAFAS payload is intentionally NOT
    persisted — like OJP, this is a live-display-only comparison.

    Never raises: transport / parse failures land in `raw.status` and
    `raw.error`; `trips` comes back as `[]` in that case.
    """
    start = time.monotonic()
    # `num_itineraries` and `from_name` / `to_name` are accepted for
    # signature parity with the other journey clients but not threaded
    # all the way through yet — HAFAS's TripSearch returns ~5 by default
    # and naming is informational on the request only. They could be
    # plumbed in a future PR; for now Sonar's S1172 is suppressed by
    # explicit `_ = …` to keep the silence intentional.
    _ = num_itineraries, from_name, to_name

    depart_at = _localise_when(when)
    # Honour the operator-provided timeout (config_schema HAFAS_TIMEOUT_MS,
    # default 10s) — construct a client with that ceiling and hand it to the
    # two-step adapter. Without this the adapter falls back to its module-
    # local 30s constant, which is fine for coverage verification but too
    # generous for live-UI journey comparison.
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_ms / 1000.0)) as client:
        out = await external_verify.fetch_oebb_two_step(
            from_lat=from_lat,
            from_lon=from_lon,
            to_lat=to_lat,
            to_lon=to_lon,
            depart_at=depart_at,
            client=client,
        )

    elapsed_ms = int((time.monotonic() - start) * 1000)
    raw: dict[str, Any] = {
        "format": "hafas-mgate",
        "from_lid": out.from_lid,
        "to_lid": out.to_lid,
        "response_ms": elapsed_ms,
    }
    verdict = out.verdict
    if verdict.error:
        # Network / parse / envelope failure — surface the error and
        # return no trips. The journey API maps `status='error'` and
        # the comparison panel renders the friendly message.
        raw["status"] = "error"
        raw["error"] = verdict.error
        return raw, []
    if not verdict.ok or out.payload is None:
        # Clean "no connections" answer (HAFAS H890 or empty outConL).
        raw["status"] = "no_route"
        return raw, []

    trips = _normalise_payload(out.payload)
    raw["status"] = "ok" if trips else "no_route"
    return raw, trips


# ─────────────────────── request-side helpers ───────────────────────


def _localise_when(when: datetime) -> datetime:
    """Render `when` as a tz-aware datetime for HAFAS's TripSearch.

    Naive input (the journey UI's `datetime-local` is naive) is
    localised to Europe/Vienna; tz-aware input is used as-is.
    `external_verify._build_trip_search_body` formats `outDate` /
    `outTime` off the resulting datetime's HHMMSS / YYYYMMDD — both
    naive-friendly methods, so a tz attached here is informational at
    the wire level but matters when comparing against UTC trip-leg
    timestamps. Falls back to UTC if the zone fails to load (shouldn't
    happen on a normal OS — see ojp_client for the matching pattern).
    """
    if when.tzinfo is not None:
        return when
    tz: Any = UTC
    try:
        tz = ZoneInfo(_REFERENCE_TZ)
    except (ZoneInfoNotFoundError, ValueError):  # pragma: no cover
        log.warning("could not load %s — using UTC for HAFAS TripSearch", _REFERENCE_TZ)
    return when.replace(tzinfo=tz)


# ─────────────────────── response normaliser ───────────────────────


def _normalise_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Translate a HAFAS TripSearch `outConL` into canonical trip dicts.

    Defensive throughout — a malformed response, a missing field, or
    an empty common list degrades to `[]` rather than raising. The
    public locations dictionary (`common.locL`) is indexed by `lid`
    so per-leg endpoints can resolve to a name + coords with one
    lookup; products (`common.prodL`) supply train numbers + operator
    labels.
    """
    svc_res_l = payload.get("svcResL") or []
    if not svc_res_l:
        return []
    res = (svc_res_l[0] or {}).get("res") or {}
    common = res.get("common") or {}
    locations = _index_locations(common.get("locL") or [])
    products = _index_products(common.get("prodL") or [])
    operators = _index_operators(common.get("opL") or [])

    connections = res.get("outConL") or []
    out: list[dict[str, Any]] = []
    for conn in connections:
        trip = _normalise_connection(conn, locations, products, operators)
        if trip is not None:
            out.append(trip)
    return out


def _index_locations(loc_l: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Build `{idx: {name, lid, lat, lon}}` from `common.locL`.

    HAFAS leg endpoints reference locations by integer index into this
    list (`locX`). Coords come back as integer micro-degrees (`crd.x` =
    lon * 1e6, `crd.y` = lat * 1e6) — same convention as the LocGeoPos
    request; we invert it here."""
    out: dict[int, dict[str, Any]] = {}
    for i, loc in enumerate(loc_l):
        crd = loc.get("crd") or {}
        x = crd.get("x")
        y = crd.get("y")
        out[i] = {
            "name": loc.get("name"),
            "lid": loc.get("lid"),
            "lat": (y / 1_000_000.0) if isinstance(y, (int, float)) else None,
            "lon": (x / 1_000_000.0) if isinstance(x, (int, float)) else None,
        }
    return out


def _index_products(prod_l: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Build `{idx: {name, line, cat, oprX}}` from `common.prodL`.

    Each `prodL` entry describes a transit product (= an OTP "route" +
    "trip" combined). `name` is the train name ("RJ 1141"); `addName`
    is sometimes the line short-name ("RJ"); `cat` carries categories
    like "RAIL" or "BUS"; `oprX` indexes into `common.opL` for the
    operator."""
    out: dict[int, dict[str, Any]] = {}
    for i, prod in enumerate(prod_l):
        out[i] = {
            "name": prod.get("name"),
            "line": prod.get("addName") or prod.get("nameS"),
            "cat": (prod.get("prodCtx") or {}).get("catOut") or prod.get("cls"),
            "oprX": prod.get("oprX"),
        }
    return out


def _index_operators(op_l: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Build `{idx: {name, id}}` from `common.opL` — agency lookup."""
    out: dict[int, dict[str, Any]] = {}
    for i, op in enumerate(op_l):
        out[i] = {"name": op.get("name"), "id": op.get("id")}
    return out


def _normalise_connection(
    conn: dict[str, Any],
    locations: dict[int, dict[str, Any]],
    products: dict[int, dict[str, Any]],
    operators: dict[int, dict[str, Any]],
) -> dict[str, Any] | None:
    """One HAFAS `outConL` entry → one canonical trip dict.

    HAFAS connection fields used here:
      - `date`: YYYYMMDD service-date anchor for HHMM time fields
      - `dur`: total duration HHMMSS / DDHHMMSS
      - `chg`: number of changes (transfers)
      - `dep` / `arr`: connection endpoints with their `dTimeS`/`aTimeS`
        (scheduled HHMM strings) and `locX` index
      - `secL`: list of sections (= our legs); each is `type` in
        ('JNY', 'WALK', 'TRSF', …) plus `dep`/`arr` and `jny` (for JNY).

    Returns None when the entry is malformed enough that we can't
    extract a sensible (departure, arrival, duration) triple — better
    to drop one bad row than to ship a card with missing fields.
    """
    date = conn.get("date")
    if not date:
        return None
    legs_norm: list[dict[str, Any]] = []
    modes_set: list[str] = []
    for sec in conn.get("secL") or []:
        leg = _normalise_section(sec, date, locations, products, operators)
        if leg is None:
            continue
        legs_norm.append(leg)
        if leg.get("mode") and leg["mode"] != "WALK":
            modes_set.append(leg["mode"])

    dep_node = conn.get("dep") or {}
    arr_node = conn.get("arr") or {}
    dep_iso = _hafas_dt_to_utc_iso(date, dep_node.get("dTimeS"))
    arr_iso = _hafas_dt_to_utc_iso(date, arr_node.get("aTimeS"))
    duration_s = external_verify._parse_hafas_duration(conn.get("dur")) or 0

    return {
        "duration_seconds": duration_s,
        "num_transfers": int(conn.get("chg") or 0),
        "departure_at": dep_iso or "",
        "arrival_at": arr_iso or "",
        "modes": ",".join(sorted(set(modes_set))),
        "legs": legs_norm,
        # PR-3 — same field every other client emits. The coverage runner
        # and the cross-engine fingerprint dedup both read this; missing
        # it would silently exclude HAFAS trips from the comparison.
        "first_transit_leg_departure_utc": _first_transit_leg_departure_utc(legs_norm),
    }


def _normalise_section(
    sec: dict[str, Any],
    date: str,
    locations: dict[int, dict[str, Any]],
    products: dict[int, dict[str, Any]],
    operators: dict[int, dict[str, Any]],
) -> dict[str, Any] | None:
    """Normalise one HAFAS section into a canonical leg dict.

    HAFAS section types we handle:
      - 'JNY' (journey)   → transit leg, mode derived from product cat
      - 'WALK' / 'TRSF'   → walking / transfer leg, mode='WALK'
      - everything else   → returned as a generic leg with the raw type

    Returns None when the section's dep/arr nodes are missing — same
    "drop bad data" stance as `_normalise_connection`.
    """
    sec_type = (sec.get("type") or "").upper()
    dep = sec.get("dep") or {}
    arr = sec.get("arr") or {}
    if not dep or not arr:
        return None

    # Extract locX once so mypy can narrow through isinstance — calling
    # dep.get("locX") twice returns Any|None each time, defeating the narrow.
    dep_locx = dep.get("locX")
    arr_locx = arr.get("locX")
    dep_loc = locations.get(dep_locx) if isinstance(dep_locx, int) else None
    arr_loc = locations.get(arr_locx) if isinstance(arr_locx, int) else None
    dep_loc = dep_loc or {}
    arr_loc = arr_loc or {}

    dep_iso = _hafas_dt_to_utc_iso(date, dep.get("dTimeS"))
    arr_iso = _hafas_dt_to_utc_iso(date, arr.get("aTimeS"))

    leg: dict[str, Any] = {
        "mode": "WALK" if sec_type in ("WALK", "TRSF") else None,
        "departure": dep_iso,
        "arrival": arr_iso,
        "duration_seconds": _section_duration_seconds(sec, dep_iso, arr_iso),
        "distance_meters": _section_distance_meters(sec),
        "from_name": dep_loc.get("name"),
        "from_lat": dep_loc.get("lat"),
        "from_lon": dep_loc.get("lon"),
        "from_stop_id": dep_loc.get("lid"),
        "to_name": arr_loc.get("name"),
        "to_lat": arr_loc.get("lat"),
        "to_lon": arr_loc.get("lon"),
        "to_stop_id": arr_loc.get("lid"),
        "route_short_name": None,
        "route_long_name": None,
        "route_id": None,
        "agency_name": None,
        "agency_id": None,
        "agency_url": None,
        # feed_id drives the operator badge in the journey UI; tagging
        # every HAFAS leg with the source string keeps the reference
        # panel visually distinct from native VIATOR results (same
        # convention OJP uses).
        "feed_id": _SOURCE_OEBB_HAFAS,
        "trip_id": None,
        "trip_headsign": None,
    }

    if sec_type == "JNY":
        _enrich_transit_leg(leg, sec, products, operators)
    return leg


def _enrich_transit_leg(
    leg: dict[str, Any],
    sec: dict[str, Any],
    products: dict[int, dict[str, Any]],
    operators: dict[int, dict[str, Any]],
) -> None:
    """Populate route + operator fields on a JNY leg from `jny.prodX`."""
    jny = sec.get("jny") or {}
    prod_x = jny.get("prodX")
    prod = products.get(prod_x) if isinstance(prod_x, int) else None
    prod = prod or {}
    cat = (prod.get("cat") or "").strip()
    leg["mode"] = _map_cat_to_mode(cat)
    leg["route_short_name"] = prod.get("line") or prod.get("name")
    leg["route_long_name"] = prod.get("name") if prod.get("line") else None
    leg["route_id"] = jny.get("jid")
    leg["trip_id"] = jny.get("jid")
    leg["trip_headsign"] = jny.get("dirTxt") or jny.get("dirFlg")
    opr_x = prod.get("oprX")
    op = operators.get(opr_x) if isinstance(opr_x, int) else None
    if op:
        leg["agency_name"] = op.get("name")
        leg["agency_id"] = op.get("id")


def _map_cat_to_mode(cat: str) -> str:
    """Map a HAFAS product category to VIATOR's mode vocabulary.

    HAFAS categories are operator-specific shorthand ("ICE", "EC",
    "RJ", "S", "Bus", "Tram", …) — we collapse them to OTP's broader
    families ("RAIL", "BUS", "TRAM", "SUBWAY", "FERRY"). Unrecognised
    categories pass through as upper-cased so they're still visible
    in diagnostics.
    """
    cat_upper = (cat or "").upper()
    if not cat_upper:
        return "TRANSIT"
    # Order matters — check the most specific buckets first.
    if cat_upper in ("BUS",) or "BUS" in cat_upper:
        return "BUS"
    if cat_upper in ("TRAM", "STR") or "TRAM" in cat_upper:
        return "TRAM"
    if cat_upper in ("U", "U-BAHN", "METRO") or "METRO" in cat_upper or "SUBWAY" in cat_upper:
        return "SUBWAY"
    if cat_upper in ("S", "S-BAHN") or "S-BAHN" in cat_upper:
        return "RAIL"
    if cat_upper in ("SHIP", "FERRY", "BOAT") or "FERRY" in cat_upper:
        return "FERRY"
    # The big rail bucket — anything that looks like a train.
    if cat_upper in (
        "ICE",
        "IC",
        "EC",
        "RJ",
        "RJX",
        "NJ",
        "EN",
        "TGV",
        "AVE",
        "AVLO",
        "EUROSTAR",
        "RE",
        "R",
        "REX",
        "D",
        "EUR",
        "WB",
        "IR",
        "FB",
    ):
        return "RAIL"
    if "RAIL" in cat_upper or "TRAIN" in cat_upper:
        return "RAIL"
    return cat_upper


def _section_duration_seconds(
    sec: dict[str, Any],
    dep_iso: str | None,
    arr_iso: str | None,
) -> int:
    """Derive a leg duration in seconds.

    Prefers HAFAS's `gisDur` (walk/transfer duration) when present;
    otherwise computes from the parsed dep/arr ISO times. Returns 0
    when neither source is usable rather than raising — a stray "0s"
    leg is recoverable; a crash on a degenerate response is not.
    """
    gis = sec.get("gis") or {}
    gis_dur = gis.get("dur")
    parsed = external_verify._parse_hafas_duration(gis_dur)
    if parsed is not None:
        return parsed
    if dep_iso and arr_iso:
        try:
            dep_dt = datetime.fromisoformat(dep_iso)
            arr_dt = datetime.fromisoformat(arr_iso)
            return max(0, int((arr_dt - dep_dt).total_seconds()))
        except ValueError:  # pragma: no cover — defensive
            return 0
    return 0


def _section_distance_meters(sec: dict[str, Any]) -> float:
    """Pull `gis.dist` (metres) off a walk/transfer section if present.

    Transit legs don't carry a distance field; coverage / federated
    code that depends on this expects 0.0 for those rather than None.
    """
    gis = sec.get("gis") or {}
    dist = gis.get("dist")
    if isinstance(dist, (int, float)):
        return float(dist)
    return 0.0


def _hafas_dt_to_utc_iso(date: str, hhmmss: str | None) -> str | None:
    """Convert a HAFAS (date, HHMMSS) pair to UTC ISO.

    HAFAS expresses times in Europe/Vienna for ÖBB. Date is YYYYMMDD;
    time is HHMMSS (occasionally with a day-roll prefix like "01024500"
    for "next-day 02:45:00"). Returns None on unparseable input.
    """
    if not date or not hhmmss:
        return None
    s = str(hhmmss)
    # Day-roll prefix: leading two digits are a day offset (HAFAS
    # convention for connections that cross midnight). Pad to a known
    # width then slice.
    s = s.zfill(6)
    day_offset = 0
    hms = s
    if len(s) > 6:
        try:
            day_offset = int(s[:-6])
            hms = s[-6:]
        except ValueError:
            return None
    try:
        year = int(date[0:4])
        month = int(date[4:6])
        day = int(date[6:8])
        hours = int(hms[0:2])
        minutes = int(hms[2:4])
        seconds = int(hms[4:6])
    except (ValueError, IndexError):
        return None
    try:
        tz: Any = ZoneInfo(_REFERENCE_TZ)
    except (ZoneInfoNotFoundError, ValueError):  # pragma: no cover
        tz = UTC
    try:
        local = datetime(year, month, day, hours, minutes, seconds, tzinfo=tz)
    except ValueError:
        return None
    if day_offset:
        # `datetime(..., day=day+offset)` would mis-handle month
        # boundaries; use timedelta semantics via the `replace` round-
        # trip. Simpler: add seconds.
        from datetime import timedelta

        local = local + timedelta(days=day_offset)
    return local.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")
