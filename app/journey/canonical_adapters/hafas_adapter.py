"""ÖBB HAFAS `TripSearch` → CanonicalJourney: one adapter for `/journey` and for coverage.

Times are Europe/Vienna local (`date` + `HHMMSS`, with a day-roll prefix past midnight) and are
converted to UTC here, once (coverage's check kept them naive before #303 PR 4a). A station's identity comes
from `extId`, then the lid's `L=` field read by name (`external_verify.oebb_stop_id`), never from
a scan of the lid, which returns a longitude (CLAUDE.md §3).

`prodL` gives the label HAFAS shows (`name`: `RJ 1141`, `S 7`), a line (`addName` / `nameS`),
the category (`prodCtx.catOut`) and the train number (`prodCtx.num`) when HAFAS sends it.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ...network_coverage.external_verify import _parse_hafas_duration, oebb_stop_id
from ..canonical import (
    CanonicalJourney,
    CanonicalLeg,
    CanonicalLocation,
    CanonicalOperator,
    CanonicalService,
    CanonicalStopPlace,
    JourneySource,
    ProductCategory,
    mode_family,
)
from ._common import (
    ADAPTER_VERSION,
    location,
    mark_transfers,
    number_or_none,
    parse_instant,
    position,
    text_or_none,
    timing,
)

STOP_SOURCE = "hafas:oebb"
# HAFAS gives ÖBB times as Europe/Vienna wall clock.
REFERENCE_TZ = "Europe/Vienna"


def journeys_from_trip_search(payload: Mapping[str, Any]) -> list[CanonicalJourney]:
    """One journey per usable `outConL` connection; a connection without a `date` is
    dropped, like a section without both ends."""
    services = payload.get("svcResL") or []
    if not services:
        return []
    result = (services[0] or {}).get("res") or {}
    common = result.get("common") or {}
    locations = _index_locations(common.get("locL") or [])
    products = _index_products(common.get("prodL") or [])
    operators = _index_operators(common.get("opL") or [])
    source = JourneySource(provider="hafas", role="reference", adapter_version=ADAPTER_VERSION)
    out: list[CanonicalJourney] = []
    for rank, connection in enumerate(result.get("outConL") or []):
        journey = _journey(connection, rank, source, locations, products, operators)
        if journey is not None:
            out.append(journey)
    return out


def _index_locations(loc_l: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for index, loc in enumerate(loc_l):
        crd = loc.get("crd") or {}
        x, y = number_or_none(crd.get("x")), number_or_none(crd.get("y"))
        out[index] = {
            "name": loc.get("name"),
            "lid": loc.get("lid"),
            "ext_id": loc.get("extId"),
            "lat": y / 1_000_000.0 if y is not None else None,
            "lon": x / 1_000_000.0 if x is not None else None,
        }
    return out


def _index_products(prod_l: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for index, prod in enumerate(prod_l):
        context = prod.get("prodCtx") or {}
        out[index] = {
            "name": prod.get("name"),
            "line": prod.get("addName") or prod.get("nameS"),
            "cat": context.get("catOut") or prod.get("cls"),
            "num": context.get("num"),
            "oprX": prod.get("oprX"),
        }
    return out


def _index_operators(op_l: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    return {index: {"name": op.get("name"), "id": op.get("id")} for index, op in enumerate(op_l)}


def _indexed(table: Mapping[int, dict[str, Any]], key: Any) -> dict[str, Any]:
    return table.get(key, {}) if isinstance(key, int) else {}


def _journey(
    connection: Mapping[str, Any],
    rank: int,
    source: JourneySource,
    locations: Mapping[int, dict[str, Any]],
    products: Mapping[int, dict[str, Any]],
    operators: Mapping[int, dict[str, Any]],
) -> CanonicalJourney | None:
    date = connection.get("date")
    if not date:
        return None
    legs = [
        leg
        for section in connection.get("secL") or []
        if (leg := _leg(section, str(date), locations, products, operators)) is not None
    ]
    departure = hafas_time_to_utc_iso(str(date), (connection.get("dep") or {}).get("dTimeS"))
    arrival = hafas_time_to_utc_iso(str(date), (connection.get("arr") or {}).get("aTimeS"))
    return CanonicalJourney(
        id=f"hafas:oebb:{rank}",
        source=source,
        legs=mark_transfers(legs),
        departure=timing(parse_instant(departure)),
        arrival=timing(parse_instant(arrival)),
        duration_seconds=_parse_hafas_duration(connection.get("dur")) or 0,
        source_transfer_count=int(connection.get("chg") or 0),
    )


def _stop_place(loc: Mapping[str, Any]) -> CanonicalStopPlace | None:
    lid = loc.get("lid")
    if not lid:
        return None
    ext_id = loc.get("ext_id")
    identity = oebb_stop_id(ext_id, lid=str(lid))
    token = identity.token or ""
    return CanonicalStopPlace(
        ref=str(lid),
        source=STOP_SOURCE,
        name=text_or_none(loc.get("name")),
        uic=token[4:] if token.startswith("UIC:") else None,
        unresolved_token=token if token.startswith("OEBB:") else None,
        codes={"OEBB_EXT": str(ext_id)} if ext_id is not None else {},
        position=position(loc.get("lat"), loc.get("lon")),
    )


def _end(loc: Mapping[str, Any]) -> CanonicalLocation:
    name = text_or_none(loc.get("name"))
    return location(_stop_place(loc), name, position(loc.get("lat"), loc.get("lon")))


def _service(
    journey: Mapping[str, Any],
    products: Mapping[int, dict[str, Any]],
    operators: Mapping[int, dict[str, Any]],
) -> tuple[CanonicalService, str]:
    """The service of a `JNY` section, and the mode string the old dicts carried."""
    product = _indexed(products, journey.get("prodX"))
    category = str(product.get("cat") or "").strip()
    source_mode = map_category_to_mode(category)
    operator = _indexed(operators, product.get("oprX"))
    number = product.get("num")
    return (
        CanonicalService(
            mode=mode_family(source_mode),
            product_category=ProductCategory(code=category) if category else None,
            line=text_or_none(product.get("line")),
            train_number=str(number) if number not in (None, "") else None,
            published_name=text_or_none(product.get("name")),
            journey_ref=text_or_none(journey.get("jid")),
            destination_text=text_or_none(journey.get("dirTxt") or journey.get("dirFlg")),
            operator=CanonicalOperator(
                ref=text_or_none(operator.get("id")), name=text_or_none(operator.get("name"))
            )
            if operator
            else None,
        ),
        source_mode,
    )


def _leg(
    section: Mapping[str, Any],
    date: str,
    locations: Mapping[int, dict[str, Any]],
    products: Mapping[int, dict[str, Any]],
    operators: Mapping[int, dict[str, Any]],
) -> CanonicalLeg | None:
    kind = str(section.get("type") or "").upper()
    dep = section.get("dep") or {}
    arr = section.get("arr") or {}
    if not dep or not arr:
        return None
    dep_iso = hafas_time_to_utc_iso(date, dep.get("dTimeS"))
    arr_iso = hafas_time_to_utc_iso(date, arr.get("aTimeS"))
    service: CanonicalService | None = None
    source_mode: str | None = None
    if kind == "JNY":
        service, source_mode = _service(section.get("jny") or {}, products, operators)
    elif kind in ("WALK", "TRSF"):
        source_mode = "WALK"
    distance = number_or_none((section.get("gis") or {}).get("dist"))
    return CanonicalLeg(
        kind="timed" if service is not None else "continuous",
        origin=_end(_indexed(locations, dep.get("locX"))),
        destination=_end(_indexed(locations, arr.get("locX"))),
        departure=timing(parse_instant(dep_iso)),
        arrival=timing(parse_instant(arr_iso)),
        duration_seconds=section_duration_seconds(dict(section), dep_iso, arr_iso),
        distance_meters=distance if distance is not None and distance >= 0 else None,
        service=service,
        continuous_mode=source_mode if service is None else None,
        source_mode=source_mode,
    )


# ── HAFAS value helpers (moved from hafas_client, #303 PR 3a) ─────────────────────────


def map_category_to_mode(cat: str | int | None) -> str:
    """Map a HAFAS product category to VIATOR's mode vocabulary.

    HAFAS categories are operator-specific shorthand ("ICE", "EC",
    "RJ", "S", "Bus", "Tram", …) — we collapse them to OTP's broader
    families ("RAIL", "BUS", "TRAM", "SUBWAY", "FERRY"). Unrecognised
    categories pass through as upper-cased so they're still visible
    in diagnostics.
    """
    # HAFAS occasionally returns `cat` as an int (a numeric product code)
    # rather than a string abbreviation — str() first so `.upper()` never
    # raises AttributeError on those responses (#213; coverage's own mode rule,
    # `network_coverage.oebb_journeys.oebb_mode`, does the same).
    cat_upper = str(cat or "").upper()
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


def section_duration_seconds(
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
    parsed = _parse_hafas_duration(gis_dur)
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


def hafas_time_to_utc_iso(date: str, hhmmss: str | None) -> str | None:
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
        tz: Any = ZoneInfo(REFERENCE_TZ)
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
        local = local + timedelta(days=day_offset)
    return local.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")
