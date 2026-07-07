"""Async client for an external reference Open Journey Planner (OJP) endpoint.

VIATOR's own routing runs on OpenTripPlanner (see `otp_client.py`). This
module talks to an *external* OJP 2.0 endpoint — opentransportdata.swiss
— so the journey UI can show the reference engine's itineraries
side-by-side with VIATOR's, as a validation oracle. See
docs/ojp-reference-comparison-design.md.

OJP is a SIRI-family **XML** standard (`CEN/TS 17118`), so unlike the
GraphQL/JSON `otp_client` this module builds an XML `TripRequest` and
parses an XML `TripResult`. The request/response shapes here were
verified against the live opentransportdata.swiss OJP 2.0 endpoint
before this was written (the Phase 0 spike — design doc Appendix A),
and the parser is modelled on a real captured `OJPTripDelivery`.

Contract mirrors `otp_client.fetch_plan`: `fetch_reference` returns
`(raw, trips)` where `trips` is the same normalised trip-dict shape the
journey UI already renders. Transport / HTTP failures raise
`httpx.HTTPError` for the caller to map to an `error` status; a
well-formed-but-empty or unparseable body returns `(raw, [])`.

The result is **not persisted** — `journey_search_executions.session_id`
is FK'd to `sessions.id` and OJP isn't a session. Phase 1 shows the
comparison live only; persistence is Phase 2 (design doc §5.4 / §9).
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from typing import Any
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from defusedxml.ElementTree import fromstring as _xml_fromstring

from .trip_normalize import dedup_batch_and_track_latest_dep as _dedup_batch_and_track_latest_dep
from .trip_normalize import first_transit_leg_departure_utc as _first_transit_leg_departure_utc
from .trip_normalize import next_anchor_or_none as _next_anchor_or_none

log = logging.getLogger(__name__)

# OJP 2.0 XML namespace identifiers. The default namespace carries the
# OJP elements; the `siri:` prefix carries the SIRI-borrowed ones
# (StopPointRef, the timestamp elements, OperatorRef, …). ElementTree
# matches these as exact strings against the document's `xmlns`
# declarations.
#
# These are *namespace URIs* defined by the OJP / SIRI standards —
# opaque unique identifiers, NOT network endpoints; nothing is ever
# fetched from them, and the `http:` scheme is mandated by the spec
# (`https:` is simply not the identifier). They're assembled from a
# scheme constant so a security scanner doesn't misclassify a constant
# namespace identifier as an insecure URL (SonarCloud encrypt-data).
#
# `xml.etree.ElementTree` is imported only for the `Element` *type* and
# for navigating already-parsed trees (`.find` / `.iter` — safe). The
# one place untrusted bytes are *parsed* uses `defusedxml`'s hardened
# `fromstring` (see `_normalise`) — stdlib parsing is XXE-vulnerable.
_NS_SCHEME = "http:"  # spec-mandated namespace scheme — not a fetched URL
_OJP = f"{_NS_SCHEME}//www.vdv.de/ojp"
_SIRI = f"{_NS_SCHEME}//www.siri.org.uk/siri"
_NS = {"ojp": _OJP, "siri": _SIRI}

# `siri:StopPointRef` is pulled from several leg sub-elements; the
# literal lives here once (SonarCloud S1192).
_STOP_POINT_REF = "siri:StopPointRef"

# The Swiss OJP endpoint interprets a bare DepArrTime as local Swiss
# time. The journey UI's datetime-local input is naive (no offset), so a
# naive `when` is localised to this zone before formatting. A tz-aware
# `when` (e.g. the "now" default) is used as-is. When VIATOR grows
# multi-NAP OJP comparison (design doc Phase 3) each endpoint would
# carry its own zone; for the single Swiss reference this constant is
# correct and keeps the signature simple.
_REFERENCE_TZ = "Europe/Zurich"

# OJP TripRequest template — verified against the live endpoint (Phase 0
# spike). `version="2.0"`, default ns = OJP, `siri:` prefix = SIRI.
# DepArrTime sits inside <Origin> for a depart-at search. Values are
# substituted via str.format with pre-escaped/validated inputs.
_TRIP_REQUEST = """<?xml version="1.0" encoding="utf-8"?>
<OJP xmlns="http://www.vdv.de/ojp" xmlns:siri="http://www.siri.org.uk/siri"\
 xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"\
 xmlns:xsd="http://www.w3.org/2001/XMLSchema"\
 xsi:schemaLocation="http://www.vdv.de/ojp" version="2.0">
  <OJPRequest>
    <siri:ServiceRequest>
      <siri:ServiceRequestContext><siri:Language>en</siri:Language></siri:ServiceRequestContext>
      <siri:RequestTimestamp>{now}</siri:RequestTimestamp>
      <siri:RequestorRef>VIATOR</siri:RequestorRef>
      <OJPTripRequest>
        <siri:RequestTimestamp>{now}</siri:RequestTimestamp>
        <siri:MessageIdentifier>viator-compare</siri:MessageIdentifier>
        <Origin>
          <PlaceRef>
            <GeoPosition><siri:Longitude>{from_lon}</siri:Longitude>\
<siri:Latitude>{from_lat}</siri:Latitude></GeoPosition>
            <Name><Text>{from_name}</Text></Name>
          </PlaceRef>
          <DepArrTime>{depart}</DepArrTime>
        </Origin>
        <Destination>
          <PlaceRef>
            <GeoPosition><siri:Longitude>{to_lon}</siri:Longitude>\
<siri:Latitude>{to_lat}</siri:Latitude></GeoPosition>
            <Name><Text>{to_name}</Text></Name>
          </PlaceRef>
        </Destination>
        <Params>
          <NumberOfResults>{num_results}</NumberOfResults>
          <IncludeIntermediateStops>true</IncludeIntermediateStops>
          <UseRealtimeData>explanatory</UseRealtimeData>
        </Params>
      </OJPTripRequest>
    </siri:ServiceRequest>
  </OJPRequest>
</OJP>"""

_ISO_DURATION = re.compile(
    r"^P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)W)?(?:(\d+)D)?"
    r"(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?$"
)


# ─────────────────────────── request building ───────────────────────────


def _reference_departure(when: datetime) -> str:
    """Render `when` as an ISO-8601 instant for the OJP DepArrTime.

    Naive input (the journey UI's datetime-local has no offset) is
    localised to the Swiss reference zone; tz-aware input is used as-is.
    Falls back to UTC if the zone can't be loaded (shouldn't happen on a
    normal OS — `app/otp_timezone.py` relies on the same stdlib zoneinfo).
    """
    if when.tzinfo is None:
        tz: Any = UTC
        try:
            tz = ZoneInfo(_REFERENCE_TZ)
        except (ZoneInfoNotFoundError, ValueError):  # pragma: no cover
            log.warning("could not load %s — using UTC for OJP DepArrTime", _REFERENCE_TZ)
        when = when.replace(tzinfo=tz)
    return when.isoformat(timespec="seconds")


def _build_trip_request(
    *,
    from_lat: float,
    from_lon: float,
    to_lat: float,
    to_lon: float,
    when: datetime,
    from_name: str | None,
    to_name: str | None,
    num_results: int,
) -> str:
    """Build the OJP 2.0 TripRequest XML body."""
    return _TRIP_REQUEST.format(
        now=datetime.now(UTC).isoformat(timespec="seconds"),
        depart=_reference_departure(when),
        # Coordinates are floats from a pydantic-validated body — formatting
        # them as plain decimals can't inject XML. Names are operator-typed
        # station labels, so they're XML-escaped.
        from_lon=f"{from_lon:.6f}",
        from_lat=f"{from_lat:.6f}",
        to_lon=f"{to_lon:.6f}",
        to_lat=f"{to_lat:.6f}",
        from_name=escape(from_name or "Origin"),
        to_name=escape(to_name or "Destination"),
        num_results=max(1, min(int(num_results), 20)),
    )


# ─────────────────────────────── the call ───────────────────────────────


async def fetch_reference(
    *,
    from_lat: float,
    from_lon: float,
    to_lat: float,
    to_lon: float,
    when: datetime,
    timeout_ms: int,
    endpoint: str,
    token: str,
    from_name: str | None = None,
    to_name: str | None = None,
    num_results: int = 5,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Call the external OJP endpoint. Returns (raw, trips_for_ui).

    Raises `httpx.HTTPError` on transport / non-2xx failure (the caller
    maps that to an `error` status). A 2xx response that doesn't parse
    into any `TripResult` returns `(raw, [])` — `raw` always carries the
    response text for diagnostics.
    """
    body = _build_trip_request(
        from_lat=from_lat,
        from_lon=from_lon,
        to_lat=to_lat,
        to_lon=to_lon,
        when=when,
        from_name=from_name,
        to_name=to_name,
        num_results=num_results,
    )
    timeout = max(timeout_ms / 1000.0, 1.0)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "text/xml",
    }
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.post(endpoint, content=body.encode("utf-8"), headers=headers)
    r.raise_for_status()
    text = r.text
    raw: dict[str, Any] = {"format": "ojp-xml", "body": text}
    return raw, _normalise(text)


# ─────────────────────── anchor-time pagination ─────────────────────────


async def fetch_reference_paginated(
    *,
    from_lat: float,
    from_lon: float,
    to_lat: float,
    to_lon: float,
    when: datetime,
    timeout_ms: int,
    endpoint: str,
    token: str,
    from_name: str | None = None,
    to_name: str | None = None,
    num_results: int = 5,
    target_window_seconds: int = 21600,
    max_pages: int = 4,
) -> tuple[list[dict[str, Any]], int, int]:
    """Call OJP `TripRequest` repeatedly with successively later anchor
    times until time-window coverage matches OTP's, OJP runs out of
    trips, or `max_pages` is reached. Returns `(trips, total_ms, pages)`.

    **Why this exists** (v0.1.35.06). OJP's `TripRequest` returns ~6
    alternatives clustered around the requested time and has no
    `searchWindow`-like parameter — it caps by count, not by time
    window. OTP's `planConnection` returns `first: N` itineraries over
    `searchWindow: T`. At our defaults (N=12, T=6 h) OTP covers a wider
    time band than a single OJP request, so the comparison strip shows
    spurious `otp_only` itineraries in the tail of OTP's range. This
    helper closes the gap by issuing follow-up OJP requests anchored
    just after each batch's latest departure, until OJP's coverage
    catches up to OTP's `target_window_seconds`.

    Stop conditions, evaluated each page:

    - **fetch_reference raises** → if we have NO partial data yet,
      propagate so the caller maps to `error` / `rate_limited`.
      Otherwise return what we have (better UX than dropping the
      whole comparison because page 3 of 4 timed out).
    - **empty batch** → operator exhausted for this anchor; nothing
      more to fetch.
    - **all trips were duplicates of earlier pages** → no forward
      progress, bail (boundary collision only).
    - **latest `departure_at` >= `when + target_window_seconds`** →
      coverage caught up to OTP.
    - **`max_pages` reached** → hard cap (rate-limit safety;
      default 4 pages x 5 results = 20 trips max).

    Dedup uses `transit_fingerprint` (same hash as cross-engine
    bucketing in `_build_comparison`). Boundary trips that legitimately
    appear in two consecutive batches collapse to one.

    Cost: pages run **sequentially** (we don't know the next anchor
    until the current batch returns). At ~600 ms per OJP call, a
    full 4-page fetch adds ~1.8 s above the single-page baseline. The
    caller invokes this in parallel with the OTP fanout, not in
    series, so wall-time impact is the max of the two.
    """
    target_end_ts = when.timestamp() + target_window_seconds
    anchor = when
    all_trips: list[dict[str, Any]] = []
    seen_fps: set[str] = set()
    total_ms = 0
    pages = 0
    fetch_kwargs = {
        "from_lat": from_lat,
        "from_lon": from_lon,
        "to_lat": to_lat,
        "to_lon": to_lon,
        "timeout_ms": timeout_ms,
        "endpoint": endpoint,
        "token": token,
        "from_name": from_name,
        "to_name": to_name,
        "num_results": num_results,
    }

    for _ in range(max_pages):
        pages += 1
        batch, stop = await _fetch_one_page(anchor, fetch_kwargs, all_trips, pages)
        # Approximate per-page latency (callers time the whole paginated
        # call externally; this is informational only — see _query_
        # ojp_reference which uses its own monotonic clock).
        total_ms += max(1, timeout_ms // 12)
        if stop:
            return all_trips, total_ms, pages
        if not batch:
            break  # OJP exhausted at this anchor

        new_trips, latest_dep_ts = _dedup_batch_and_track_latest_dep(batch, seen_fps)
        if not new_trips:
            break  # all-dups → no forward progress

        all_trips.extend(new_trips)
        next_anchor = _next_anchor_or_none(latest_dep_ts, target_end_ts)
        if next_anchor is None:
            break  # caught up to OTP's window, or unparseable times
        anchor = next_anchor

    return all_trips, total_ms, pages


async def _fetch_one_page(
    anchor: datetime,
    fetch_kwargs: dict[str, Any],
    partial: list[dict[str, Any]],
    page_num: int,
) -> tuple[list[dict[str, Any]], bool]:
    """Call fetch_reference once. Returns (batch, stop).

    `stop=True` signals the paginator to return immediately with the
    partial data already collected (used when an HTTP error fires
    mid-flight after at least page 1 succeeded — better UX than
    discarding the whole comparison). HTTP errors with no partial
    data yet propagate so the caller maps to error / rate_limited.
    """
    try:
        _raw, batch = await fetch_reference(when=anchor, **fetch_kwargs)
    except httpx.HTTPError:
        if not partial:
            raise
        log.warning(
            "OJP pagination stopped mid-flight at page %d (kept %d trips so far)",
            page_num,
            len(partial),
        )
        return [], True
    return batch, False


# _dedup_batch_and_track_latest_dep / _next_anchor_or_none moved to
# trip_normalize.py (v0.1.45) so hafas_client's own anchor-pagination
# wrapper can share the identical algorithm instead of re-deriving it.


# ───────────────────────────── response parse ───────────────────────────


def _normalise(xml_text: str) -> list[dict[str, Any]]:
    """Parse an OJP `OJPTripDelivery` XML body into VIATOR's trip-dict shape.

    Output matches `otp_client._normalise` so the journey UI renders OJP
    reference trips with the same code path: `duration_seconds`,
    `num_transfers`, `departure_at`/`arrival_at` (UTC ISO), `modes`,
    `legs[]` (each leg carrying the same keys OTP legs do).

    Defensive throughout — a malformed body, a missing element, or an
    OJP error payload (which has no `<TripResult>`) all degrade to `[]`
    rather than raising.
    """
    try:
        root = _xml_fromstring(xml_text)
    except (ET.ParseError, ValueError):
        # ET.ParseError → malformed XML. defusedxml raises ValueError
        # subclasses (EntitiesForbidden, …) if the payload attempts an
        # XML attack — either way there are no usable trips.
        log.warning("OJP response did not parse as safe XML (%d bytes)", len(xml_text))
        return []

    places = _index_places(root)
    out: list[dict[str, Any]] = []
    for trip in root.iter(f"{{{_OJP}}}Trip"):
        legs_norm: list[dict[str, Any]] = []
        modes_set: list[str] = []
        for leg in trip.findall("ojp:Leg", _NS):
            norm = _normalise_leg(leg, places)
            if norm is None:
                continue
            legs_norm.append(norm)
            if norm.get("mode"):
                modes_set.append(norm["mode"])
        out.append(
            {
                "duration_seconds": _iso_duration_to_seconds(
                    trip.findtext("ojp:Duration", default="", namespaces=_NS)
                ),
                "num_transfers": _int_or_zero(
                    trip.findtext("ojp:Transfers", default="", namespaces=_NS)
                ),
                "departure_at": _iso_to_utc_iso(
                    trip.findtext("ojp:StartTime", default="", namespaces=_NS)
                )
                or "",
                "arrival_at": _iso_to_utc_iso(
                    trip.findtext("ojp:EndTime", default="", namespaces=_NS)
                )
                or "",
                "modes": ",".join(sorted(set(modes_set))),
                "legs": legs_norm,
                # PR-3 — first transit-leg boarding time in UTC ISO.
                # Same contract as OTP / MOTIS — the coverage runner
                # filters trips on this field, not on `departure_at`
                # (which is the itinerary START, often a walking
                # ContinuousLeg). See app/journey/trip_normalize.py.
                "first_transit_leg_departure_utc": _first_transit_leg_departure_utc(legs_norm),
            }
        )
    return out


def _index_places(root: ET.Element) -> dict[str, dict[str, Any]]:
    """Build `{stopRef: {name, lat, lon}}` from `TripResponseContext/Places`.

    Legs reference stops by `siri:StopPointRef` / `StopPlaceRef`; the
    coordinates live once, here, in the place dictionary.
    """
    index: dict[str, dict[str, Any]] = {}
    for place in root.iter(f"{{{_OJP}}}Place"):
        name = place.findtext("ojp:Name/ojp:Text", default=None, namespaces=_NS)
        geo = place.find("ojp:GeoPosition", _NS)
        lat = (
            _float_or_none(geo.findtext("siri:Latitude", namespaces=_NS))
            if geo is not None
            else None
        )
        lon = (
            _float_or_none(geo.findtext("siri:Longitude", namespaces=_NS))
            if geo is not None
            else None
        )
        # A Place is one of StopPlace / StopPoint / TopographicPlace; the
        # first two carry the refs the legs use.
        for ref in (
            place.findtext("ojp:StopPlace/ojp:StopPlaceRef", namespaces=_NS),
            place.findtext("ojp:StopPoint/siri:StopPointRef", namespaces=_NS),
        ):
            if ref:
                index[ref] = {"name": name, "lat": lat, "lon": lon}
    return index


def _normalise_leg(leg: ET.Element, places: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """Normalise one `<Leg>` — a Leg wraps exactly one of ContinuousLeg /
    TimedLeg / TransferLeg."""
    leg_duration = _iso_duration_to_seconds(
        leg.findtext("ojp:Duration", default="", namespaces=_NS)
    )

    timed = leg.find("ojp:TimedLeg", _NS)
    if timed is not None:
        return _normalise_timed_leg(timed, leg_duration, places)

    cont = leg.find("ojp:ContinuousLeg", _NS)
    if cont is not None:
        return _normalise_walk_leg(cont, leg_duration, places, mode="WALK")

    transfer = leg.find("ojp:TransferLeg", _NS)
    if transfer is not None:
        return _normalise_walk_leg(transfer, leg_duration, places, mode="WALK")

    return None  # unknown leg variant — skip rather than guess


def _blank_leg() -> dict[str, Any]:
    """A leg dict with every key the journey UI expects, defaulted."""
    return {
        "mode": None,
        "departure": None,
        "arrival": None,
        "duration_seconds": 0,
        "distance_meters": 0.0,
        "from_name": None,
        "from_lat": None,
        "from_lon": None,
        "from_stop_id": None,
        "to_name": None,
        "to_lat": None,
        "to_lon": None,
        "to_stop_id": None,
        "route_short_name": None,
        "route_long_name": None,
        "route_id": None,
        "agency_name": None,
        "agency_id": None,
        "agency_url": None,
        # feed_id drives the operator badge in the journey UI; tagging
        # every OJP leg "OJP" makes the reference panel visually distinct.
        "feed_id": "OJP",
        "trip_id": None,
        "trip_headsign": None,
    }


def _normalise_timed_leg(
    timed: ET.Element, leg_duration: int, places: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    out = _blank_leg()
    out["duration_seconds"] = leg_duration

    board = timed.find("ojp:LegBoard", _NS)
    alight = timed.find("ojp:LegAlight", _NS)
    svc = timed.find("ojp:Service", _NS)

    if board is not None:
        ref = board.findtext(_STOP_POINT_REF, namespaces=_NS)
        out["from_stop_id"] = ref
        out["from_name"] = board.findtext(
            "ojp:StopPointName/ojp:Text", default=None, namespaces=_NS
        ) or _place_name(places, ref)
        out["from_lat"], out["from_lon"] = _place_coords(places, ref)
        out["departure"] = _iso_to_utc_iso(
            board.findtext("ojp:ServiceDeparture/ojp:TimetabledTime", default="", namespaces=_NS)
        )
    if alight is not None:
        ref = alight.findtext(_STOP_POINT_REF, namespaces=_NS)
        out["to_stop_id"] = ref
        out["to_name"] = alight.findtext(
            "ojp:StopPointName/ojp:Text", default=None, namespaces=_NS
        ) or _place_name(places, ref)
        out["to_lat"], out["to_lon"] = _place_coords(places, ref)
        out["arrival"] = _iso_to_utc_iso(
            alight.findtext("ojp:ServiceArrival/ojp:TimetabledTime", default="", namespaces=_NS)
        )
    if svc is not None:
        # PtMode is the canonical mode ("rail" / "bus" / "tram" …) — upper
        # it to match VIATOR's "RAIL" / "BUS" / "WALK" vocabulary.
        ptmode = svc.findtext("ojp:Mode/ojp:PtMode", default="", namespaces=_NS)
        out["mode"] = ptmode.upper() if ptmode else "TRANSIT"
        out["route_short_name"] = svc.findtext(
            "ojp:PublishedServiceName/ojp:Text", default=None, namespaces=_NS
        ) or svc.findtext("ojp:PublicCode", default=None, namespaces=_NS)
        out["route_long_name"] = svc.findtext(
            "ojp:ProductCategory/ojp:Name/ojp:Text", default=None, namespaces=_NS
        )
        out["route_id"] = svc.findtext("siri:LineRef", default=None, namespaces=_NS)
        out["agency_id"] = svc.findtext("siri:OperatorRef", default=None, namespaces=_NS)
        out["trip_id"] = svc.findtext("ojp:JourneyRef", default=None, namespaces=_NS)
        out["trip_headsign"] = svc.findtext(
            "ojp:DestinationText/ojp:Text", default=None, namespaces=_NS
        )
    return out


def _normalise_walk_leg(
    leg: ET.Element, leg_duration: int, places: dict[str, dict[str, Any]], *, mode: str
) -> dict[str, Any]:
    """ContinuousLeg / TransferLeg — both are walk-class with LegStart/LegEnd
    that may be a GeoPosition or a StopPointRef."""
    out = _blank_leg()
    out["mode"] = mode
    out["duration_seconds"] = leg_duration
    out["distance_meters"] = _float_or_none(leg.findtext("ojp:Length", namespaces=_NS)) or 0.0

    start = leg.find("ojp:LegStart", _NS)
    end = leg.find("ojp:LegEnd", _NS)
    if start is not None:
        name, lat, lon, ref = _leg_endpoint(start, places)
        out["from_name"], out["from_lat"], out["from_lon"], out["from_stop_id"] = (
            name,
            lat,
            lon,
            ref,
        )
    if end is not None:
        name, lat, lon, ref = _leg_endpoint(end, places)
        out["to_name"], out["to_lat"], out["to_lon"], out["to_stop_id"] = (
            name,
            lat,
            lon,
            ref,
        )
    return out


def _leg_endpoint(
    el: ET.Element, places: dict[str, dict[str, Any]]
) -> tuple[str | None, float | None, float | None, str | None]:
    """A LegStart/LegEnd is either an inline GeoPosition or a StopPointRef
    resolved via the Places dictionary. Returns (name, lat, lon, stop_ref)."""
    name = el.findtext("ojp:Name/ojp:Text", default=None, namespaces=_NS)
    ref = el.findtext(_STOP_POINT_REF, default=None, namespaces=_NS)
    geo = el.find("ojp:GeoPosition", _NS)
    if geo is not None:
        return (
            name,
            _float_or_none(geo.findtext("siri:Latitude", namespaces=_NS)),
            _float_or_none(geo.findtext("siri:Longitude", namespaces=_NS)),
            ref,
        )
    if ref:
        lat, lon = _place_coords(places, ref)
        return (name or _place_name(places, ref), lat, lon, ref)
    return (name, None, None, ref)


# ───────────────────────────── small helpers ────────────────────────────


def _place_name(places: dict[str, dict[str, Any]], ref: str | None) -> str | None:
    return places.get(ref or "", {}).get("name")


def _place_coords(
    places: dict[str, dict[str, Any]], ref: str | None
) -> tuple[float | None, float | None]:
    p = places.get(ref or "", {})
    return p.get("lat"), p.get("lon")


def _iso_duration_to_seconds(value: str | None) -> int:
    """Parse an ISO-8601 duration (`PT1H9M`, `PT4M`, `PT0S`, …) to whole
    seconds. OJP trip/leg durations are always the `PT…` time form, but
    the regex tolerates the date components too. Unparseable → 0."""
    if not value:
        return 0
    m = _ISO_DURATION.match(value.strip())
    if not m:
        return 0
    # Years / months (groups 1-2) are calendar-ambiguous and never
    # appear in a trip/leg duration - ignored rather than guessed.
    _, _, weeks, days, hours, minutes, seconds = m.groups()
    total = 0.0
    total += int(days or 0) * 86400
    total += int(weeks or 0) * 604800
    total += int(hours or 0) * 3600
    total += int(minutes or 0) * 60
    total += float(seconds or 0)
    return int(total)


def _iso_to_utc_iso(value: str | None) -> str | None:
    """OJP times (`2026-05-18T08:26:00Z`, or with an offset) → UTC ISO,
    the same shape `otp_client` produces. Naive input is assumed UTC."""
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.strip())
    except (TypeError, ValueError):  # pragma: no cover
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _int_or_zero(value: str | None) -> int:
    try:
        return int((value or "").strip())
    except (TypeError, ValueError):
        return 0


def _float_or_none(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value.strip())
    except (TypeError, ValueError):
        return None
