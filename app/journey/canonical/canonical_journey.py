"""A journey: OJP `Trip`. The single representation every layer above the adapters reads.

Derived values (first boarding, transit duration, transfers, fingerprint) are properties of
the model, computed in one place, and never serialised: a stored journey reads back with
`CanonicalJourney.model_validate_json` and computes them again.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from .canonical_leg import CanonicalLeg
from .canonical_service import TransportMode
from .canonical_timing import CanonicalTiming
from .canonical_transfer import CanonicalTransfer, derive_transfers

if TYPE_CHECKING:
    from .journey_fingerprint import JourneyFingerprint

SCHEMA_VERSION: Literal["1"] = "1"

Provider = Literal["motis", "otp", "hafas", "ojp"]
JourneyRole = Literal["viator", "reference"]


class JourneySource(BaseModel):
    """Where a journey comes from. For labels and provenance only: no business code branches
    on `provider` (design §2, rule 4)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: Provider
    role: JourneyRole
    session_id: str | None = None
    engine: str | None = None
    adapter_version: int = Field(ge=1)
    # A federated stitch (hub-and-spoke): the sessions and hubs it was built from.
    stitched_from_sessions: list[str] = Field(default_factory=list)
    via_hubs: list[str] = Field(default_factory=list)


class CanonicalJourney(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"] = SCHEMA_VERSION
    # Stable within one answer: `<provider>:<session or source>:<rank>`.
    id: str = Field(min_length=1)
    source: JourneySource
    legs: list[CanonicalLeg]
    # Door to door, as the source gives them; None when the source gave no usable time.
    departure: CanonicalTiming | None = None
    arrival: CanonicalTiming | None = None
    duration_seconds: int = Field(ge=0)
    # The source's own count of changes when it gives one (MOTIS `transfers`, HAFAS `chg`,
    # OJP `Transfers`); `transfer_count` is the one derived from the legs.
    source_transfer_count: int | None = Field(default=None, ge=0)

    @property
    def timed_legs(self) -> list[CanonicalLeg]:
        return [leg for leg in self.legs if leg.is_timed]

    @property
    def first_boarding(self) -> CanonicalTiming | None:
        """The first timed leg's departure: what `first_transit_leg_departure_utc` was. None
        for a journey without a timed leg (walk only)."""
        for leg in self.legs:
            if leg.is_timed:
                return leg.departure
        return None

    @property
    def last_alighting(self) -> CanonicalTiming | None:
        for leg in reversed(self.legs):
            if leg.is_timed:
                return leg.arrival
        return None

    @property
    def transit_duration_seconds(self) -> int | None:
        """Door-to-door duration minus every transfer and continuous leg, in seconds (the
        arithmetic the #227 review asked for: immune to time zones and day rolls). None for a
        journey without a timed leg."""
        if not any(leg.is_timed for leg in self.legs):
            return None
        walking = sum(leg.duration_seconds for leg in self.legs if not leg.is_timed)
        return max(0, self.duration_seconds - walking)

    @property
    def transfer_count(self) -> int:
        return max(0, len(self.timed_legs) - 1)

    @property
    def modes(self) -> list[TransportMode]:
        """The mode families of the timed legs, in order of first appearance."""
        seen: list[TransportMode] = []
        for leg in self.timed_legs:
            if leg.service is not None and leg.service.mode not in seen:
                seen.append(leg.service.mode)
        return seen

    @property
    def transfers(self) -> list[CanonicalTransfer]:
        return derive_transfers(self.legs)

    def fingerprint(self) -> JourneyFingerprint:
        from .journey_fingerprint import fingerprint_of

        return fingerprint_of(self)
