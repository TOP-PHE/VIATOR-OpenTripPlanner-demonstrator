"""The service of a timed leg: OJP `Service` (`DatedJourney`), Transmodel `ServiceJourney`.

The fields keep apart what the engines' dicts mixed in one `route_short_name`: the line
(`S7`, `IC5`), the train number (`1141`), the product category (`RJ`, `ICE`) and the label the
source shows (`RJ 1141`, `S 7`). Adapters copy the source's strings untouched; normalising them
for a comparison is the matching engine's job (#304).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from .canonical_operator import CanonicalOperator


class TransportMode(StrEnum):
    """Mode families, OJP `PtMode` granularity. Engine detail (MOTIS `REGIONAL_RAIL`) is kept
    in `CanonicalLeg.source_mode`, never compared by default."""

    RAIL = "RAIL"
    BUS = "BUS"
    COACH = "COACH"
    TRAM = "TRAM"
    SUBWAY = "SUBWAY"
    FERRY = "FERRY"
    CABLEWAY = "CABLEWAY"
    FUNICULAR = "FUNICULAR"
    AIR = "AIR"
    TAXI = "TAXI"
    OTHER = "OTHER"


# Exact spellings of the four sources, upper-cased (OJP's `PtMode` is camelCase: `urbanRail`).
_EXACT: dict[str, TransportMode] = {
    "RAIL": TransportMode.RAIL,
    "TRAIN": TransportMode.RAIL,
    "LONG_DISTANCE": TransportMode.RAIL,  # MOTIS
    "SUBURBAN": TransportMode.RAIL,  # MOTIS S-Bahn
    "URBANRAIL": TransportMode.RAIL,  # OJP
    "MONORAIL": TransportMode.RAIL,  # OTP
    "BUS": TransportMode.BUS,
    "TROLLEYBUS": TransportMode.BUS,
    "COACH": TransportMode.COACH,
    "TRAM": TransportMode.TRAM,
    "STREETCAR": TransportMode.TRAM,
    "SUBWAY": TransportMode.SUBWAY,
    "METRO": TransportMode.SUBWAY,
    "UNDERGROUND": TransportMode.SUBWAY,  # OJP
    "FERRY": TransportMode.FERRY,
    "WATER": TransportMode.FERRY,  # OJP
    "WATERWAY": TransportMode.FERRY,
    "BOAT": TransportMode.FERRY,
    "SHIP": TransportMode.FERRY,
    "GONDOLA": TransportMode.CABLEWAY,  # OTP
    "CABLE_CAR": TransportMode.CABLEWAY,
    "AERIAL_LIFT": TransportMode.CABLEWAY,  # MOTIS
    "TELECABIN": TransportMode.CABLEWAY,  # OJP
    "CABLEWAY": TransportMode.CABLEWAY,
    "FUNICULAR": TransportMode.FUNICULAR,
    "AIRPLANE": TransportMode.AIR,
    "AIR": TransportMode.AIR,
    "TAXI": TransportMode.TAXI,
}

# A suffix names the family when the source adds a sub-type (MOTIS `HIGHSPEED_RAIL`,
# `REGIONAL_RAIL`, `NIGHT_RAIL`; GTFS extended types). Same rule as the journey page's
# `_normalizeMode`, which until now was the only place that folded them.
_SUFFIXES: tuple[tuple[str, TransportMode], ...] = (
    ("_RAIL", TransportMode.RAIL),
    ("_BUS", TransportMode.BUS),
    ("_TRAM", TransportMode.TRAM),
    ("_SUBWAY", TransportMode.SUBWAY),
    ("_FERRY", TransportMode.FERRY),
)


def mode_family(source_mode: str | None) -> TransportMode:
    """The family of a source's mode string; `OTHER` for anything unknown or empty
    (HAFAS's `TRANSIT` fallback, MOTIS `OTHER`, an unmapped HAFAS category)."""
    mode = (source_mode or "").strip().upper().replace("-", "_")
    if mode in _EXACT:
        return _EXACT[mode]
    for suffix, family in _SUFFIXES:
        if mode.endswith(suffix):
            return family
    return TransportMode.OTHER


class ProductCategory(BaseModel):
    """OJP `ProductCategory`: `code` (`RJ`, `ICE`, `S`) and its name (`Railjet`)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str | None = None
    name: str | None = None


class CanonicalService(BaseModel):
    """What runs on a timed leg."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: TransportMode
    product_category: ProductCategory | None = None
    # OJP `PublicCode`: the line as published (`S7`, `17`, `IC5`).
    line: str | None = None
    # OJP `TrainNumber`: only when the source gives it as a field of its own.
    train_number: str | None = None
    # OJP `PublishedServiceName`: the label the source shows (`RJ 1141`, `S 7`).
    published_name: str | None = None
    route_long_name: str | None = None
    # Provenance: the source's route and trip ids (OJP `LineRef`, `JourneyRef`).
    line_ref: str | None = None
    journey_ref: str | None = None
    # OJP `DestinationText`, the headsign.
    destination_text: str | None = None
    operator: CanonicalOperator | None = None
    # The VIATOR feed the leg came from (`renfe-ld`, `SBB`); None for a reference engine.
    feed: str | None = None
