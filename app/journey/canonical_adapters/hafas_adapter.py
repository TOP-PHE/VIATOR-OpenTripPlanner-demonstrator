"""ÖBB HAFAS `TripSearch` → CanonicalJourney: one adapter for `/journey` and for coverage.

Times are Europe/Vienna local (`date` + `HHMMSS`, with a day-roll prefix past midnight) and are
converted to UTC here, once; coverage's `VerifyLeg` kept them naive. A station's identity comes
from `extId`, then the lid's `L=` field read by name (`external_verify.oebb_stop_id`), never from
a scan of the lid, which returns a longitude (CLAUDE.md §3).

`prodL` gives the label HAFAS shows (`name`: `RJ 1141`, `S 7`), a line (`addName` / `nameS`),
the category (`prodCtx.catOut`) and the train number (`prodCtx.num`) when HAFAS sends it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

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
from ..hafas_client import _hafas_dt_to_utc_iso, _map_cat_to_mode, _section_duration_seconds
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
    departure = _hafas_dt_to_utc_iso(str(date), (connection.get("dep") or {}).get("dTimeS"))
    arrival = _hafas_dt_to_utc_iso(str(date), (connection.get("arr") or {}).get("aTimeS"))
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
    source_mode = _map_cat_to_mode(category)
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
    dep_iso = _hafas_dt_to_utc_iso(date, dep.get("dTimeS"))
    arr_iso = _hafas_dt_to_utc_iso(date, arr.get("aTimeS"))
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
        duration_seconds=_section_duration_seconds(dict(section), dep_iso, arr_iso),
        distance_meters=distance if distance is not None and distance >= 0 else None,
        service=service,
        continuous_mode=source_mode if service is None else None,
        source_mode=source_mode,
    )
