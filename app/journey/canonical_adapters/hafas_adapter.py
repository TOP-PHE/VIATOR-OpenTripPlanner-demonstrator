"""ÖBB HAFAS `TripSearch` → CanonicalJourney: one adapter for `/journey` and for coverage.

Times are Europe/Vienna local (`date` + `HHMMSS`, with a day-roll prefix past midnight) and are
converted to UTC here, once (coverage's check kept them naive before #303 PR 4a). A stop's number comes
from `extId`, then the lid's `L=` field read by name (`external_verify.oebb_stop_id`), never from
a scan of the lid, which returns a longitude (CLAUDE.md §3).

**That number is ÖBB's own (an EVA number), not the station's UIC code.** It is kept as the stop's
`uic` only where ÖBB's `gidL` states the same number as the UIC (`U\u00d78503000` for Zürich HB). Measured
2026-11-12 on ÖBB's TripSearch: Paris Gare de Lyon is 8700012 with no `U\u00d7` (SNCF's UIC: 8768600),
Milano Centrale 8300046 with `U\u00d78301700`, Mulhouse Ville 8700031 with `U\u00d78718200`. Kept as a UIC,
such a number met the true one of VIATOR's French or Italian feed and the matching engine read two
different stations: the same train at the same minute was vetoed ("different first or last
station"), and coverage cells touching France scored 1 % agreement (run f7165bd7). A stated `U\u00d7`
that differs from the number is not used either: it can be off by a digit (Graz Hbf `U\u00d78103172`,
MERITS 8103171), and a wrong UIC vetoes again. Such a stop keeps its number in `codes` (the station
module's key) and is compared by the station module, else by position on a time-matched leg (M4).

`prodL` gives the label HAFAS shows (`name`: `RJ 1141`, `S 7`), a line (`addName` / `nameS`),
the category (`prodCtx.catOut`) and the train number (`prodCtx.num`) when HAFAS sends it.
"""

from __future__ import annotations

import re
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
    TransportMode,
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
# A `gidL` entry stating the stop's UIC code: `U\u00d78503000` (U, MULTIPLICATION SIGN, the digits; an
# 8-digit form carries a check digit, dropped as `oebb_stop_id` drops it).
_STATED_UIC = re.compile("\\AU\u00d70*([1-9]\\d{6,7})\\Z")
# ÖBB's product classes (`prodL[].cls`) of trains, measured 2026-11-12 on TripSearch: 1 high speed
# (TGV, OGV, FR, AVE, IRY, EUR, ICE), 4 IC, 16 regional (RB, RER), 32 S-Bahn (catOut `DB`); 2 and 8
# are rail in ÖBB's class table too. A category the map below does not know (FR, OGV, IRY, RB, RER,
# DB) used to become OTHER, so ÖBB's train never aligned with VIATOR's RAIL leg of the same train. A
# bus keeps its category (a rail replacement bus is `Bus` with class 2).
_RAIL_CLASSES = frozenset({1, 2, 4, 8, 16, 32})


def _is_rail_class(cls: object) -> bool:
    """Whether HAFAS's product class is one of ÖBB's train classes (untrusted type: an int, or
    digits in a string)."""
    try:
        return int(str(cls).strip()) in _RAIL_CLASSES
    except ValueError:
        return False


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
            "stated_uic": stated_uic(loc.get("gidL")),
            "lat": y / 1_000_000.0 if y is not None else None,
            "lon": x / 1_000_000.0 if x is not None else None,
        }
    return out


def stated_uic(gid_l: object) -> str | None:
    """The UIC code ÖBB states for a stop in its `gidL` (`U\u00d78503000` → `8503000`), None when it
    states none. HAFAS field types are untrusted: anything but a list of strings gives None."""
    if not isinstance(gid_l, list):
        return None
    for gid in gid_l:
        match = _STATED_UIC.fullmatch(str(gid))
        if match:
            return match.group(1)[:7]
    return None


def _index_products(prod_l: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for index, prod in enumerate(prod_l):
        context = prod.get("prodCtx") or {}
        out[index] = {
            "name": prod.get("name"),
            "line": prod.get("addName") or prod.get("nameS"),
            "cat": context.get("catOut") or prod.get("cls"),
            "cls": prod.get("cls"),
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
    number = token[4:] if token.startswith("UIC:") else None
    # ÖBB's number is a UIC only where ÖBB states that same number as one (module docstring). An
    # unconfirmed 7-digit number gets no `OEBB:` token either: a payload of 7 or 8 digits would break
    # `_oebb_opaque_payload`'s invariant. The stop keeps it in `codes` and is placed by the station
    # module or by its position.
    uic = number if number is not None and number == loc.get("stated_uic") else None
    own_number = str(ext_id) if ext_id is not None else identity.raw
    return CanonicalStopPlace(
        ref=str(lid),
        source=STOP_SOURCE,
        name=text_or_none(loc.get("name")),
        uic=uic,
        unresolved_token=token if token.startswith("OEBB:") else None,
        codes={"OEBB_EXT": own_number} if own_number else {},
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
    if mode_family(source_mode) == TransportMode.OTHER and _is_rail_class(product.get("cls")):
        source_mode = "RAIL"
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
