"""A change between two consecutive timed legs, derived from the legs (never given by a
source as such). `min_connection_seconds` is the minimum connection time when known: the
federated planner uses `DEFAULT_MCT_SECONDS`; `mct_overrides` has no writer yet."""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from .canonical_leg import CanonicalLeg
from .canonical_stop_place import CanonicalStopPlace


class CanonicalTransfer(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    from_leg: int = Field(ge=0)
    to_leg: int = Field(ge=1)
    alight_at: CanonicalStopPlace | None = None
    # May differ from `alight_at`: a walk between two stations.
    board_at: CanonicalStopPlace | None = None
    # Arrival of `from_leg` to departure of `to_leg`; None when a time is missing.
    connection_seconds: int | None = None
    walk_seconds: int = Field(default=0, ge=0)
    min_connection_seconds: int | None = Field(default=None, ge=0)


def derive_transfers(legs: Sequence[CanonicalLeg]) -> list[CanonicalTransfer]:
    """The transfers between consecutive timed legs, with the walking time between them."""
    out: list[CanonicalTransfer] = []
    previous: int | None = None
    walk = 0
    for index, leg in enumerate(legs):
        if not leg.is_timed:
            if previous is not None:
                walk += leg.duration_seconds
            continue
        if previous is not None:
            before = legs[previous]
            connection = None
            if before.arrival is not None and leg.departure is not None:
                delta = leg.departure.timetabled - before.arrival.timetabled
                connection = int(delta.total_seconds())
            out.append(
                CanonicalTransfer(
                    from_leg=previous,
                    to_leg=index,
                    alight_at=before.destination.stop_place,
                    board_at=leg.origin.stop_place,
                    connection_seconds=connection,
                    walk_seconds=walk,
                )
            )
        previous = index
        walk = 0
    return out


def mark_transfers(legs: Sequence[CanonicalLeg]) -> list[CanonicalLeg]:
    """A non-timed leg between two timed legs is a `transfer`, any other one `continuous`
    (OJP's `TransferLeg` vs `ContinuousLeg`), for sources that do not say which, and for a
    federated stitch, whose legs come from several journeys."""
    timed = [index for index, leg in enumerate(legs) if leg.is_timed]
    if len(timed) < 2:
        return list(legs)
    first, last = timed[0], timed[-1]
    return [
        leg.model_copy(update={"kind": "transfer"})
        if not leg.is_timed and first < index < last
        else leg
        for index, leg in enumerate(legs)
    ]
