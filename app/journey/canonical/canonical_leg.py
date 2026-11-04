"""One leg: OJP `Leg` with exactly one of `TimedLeg`, `TransferLeg`, `ContinuousLeg`.

`timed` is a ride in a vehicle. The old non-transit predicate `{WALK, TRANSFER, ''}` becomes
`not leg.is_timed`: one definition for every consumer.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .canonical_location import CanonicalLocation
from .canonical_service import CanonicalService
from .canonical_timing import CanonicalTiming

LegKind = Literal["timed", "transfer", "continuous"]


class CanonicalLeg(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: LegKind
    origin: CanonicalLocation
    destination: CanonicalLocation
    # A source can omit a time (OJP gives none on its walk legs).
    departure: CanonicalTiming | None = None
    arrival: CanonicalTiming | None = None
    duration_seconds: int = Field(ge=0)
    # None when the source gives no distance (MOTIS), never a made-up 0.0.
    distance_meters: float | None = Field(default=None, ge=0.0)
    service: CanonicalService | None = None
    # How a transfer or continuous leg is travelled: `WALK`, `BIKE`, `CAR`; None when unknown.
    continuous_mode: str | None = None
    # The mode string as the source path gave it (MOTIS `REGIONAL_RAIL`, HAFAS's mapped
    # category, OJP's upper-cased `PtMode`). Provenance and display; compare `service.mode`.
    source_mode: str | None = None

    @model_validator(mode="after")
    def _service_on_timed_legs_only(self) -> Self:
        if (self.kind == "timed") != (self.service is not None):
            raise ValueError("a timed leg has a service, a transfer or continuous leg has none")
        return self

    @property
    def is_timed(self) -> bool:
        return self.kind == "timed"
