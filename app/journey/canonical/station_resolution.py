"""Station resolution: giving each stop its station of the station module (MSMM).

The interface only (design D8). The MSMM-backed resolver is `app/journey/station_resolver.py`
(#303 PR 5): it asks the module's server-only bulk call (`POST /internal/v1/stops/stations`, MSMM
design 22.18) for up to 50 stops of one feed (a stop of a feed VIATOR sent it) or codes of one
series (an ÖBB number, series `DELFI_stop_key`) at once, and keeps the answers. Adapters never
resolve: they stay pure functions of a provider's answer.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from .canonical_journey import CanonicalJourney
from .canonical_leg import CanonicalLeg
from .canonical_location import CanonicalLocation
from .canonical_stop_place import VIATOR_ID_PATTERN, CanonicalStopPlace

StopKey = tuple[str, str]  # (stop_place.source, stop_place.ref)


class ResolvedStation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    station_id: str = Field(pattern=VIATOR_ID_PATTERN)
    parent_id: str | None = Field(default=None, pattern=VIATOR_ID_PATTERN)


class StationResolver(Protocol):
    async def resolve(
        self, stops: Sequence[CanonicalStopPlace]
    ) -> Mapping[StopKey, ResolvedStation]:
        """The stations of the stops it can place; a stop it cannot place is absent."""
        ...


def stop_places(journeys: Sequence[CanonicalJourney]) -> list[CanonicalStopPlace]:
    """Every distinct stop place of the journeys, in order of first appearance."""
    seen: dict[StopKey, CanonicalStopPlace] = {}
    for journey in journeys:
        for leg in journey.legs:
            for end in (leg.origin, leg.destination):
                stop = end.stop_place
                if stop is not None:
                    seen.setdefault((stop.source, stop.ref), stop)
    return list(seen.values())


def _with_station(
    location: CanonicalLocation, stations: Mapping[StopKey, ResolvedStation]
) -> CanonicalLocation:
    stop = location.stop_place
    if stop is None:
        return location
    found = stations.get((stop.source, stop.ref))
    if found is None:
        return location
    resolved = stop.model_copy(
        update={"station_id": found.station_id, "station_parent_id": found.parent_id}
    )
    return location.model_copy(update={"stop_place": resolved})


def _leg_with_stations(
    leg: CanonicalLeg, stations: Mapping[StopKey, ResolvedStation]
) -> CanonicalLeg:
    return leg.model_copy(
        update={
            "origin": _with_station(leg.origin, stations),
            "destination": _with_station(leg.destination, stations),
        }
    )


async def with_resolved_stations(
    journeys: Sequence[CanonicalJourney], resolver: StationResolver | None
) -> list[CanonicalJourney]:
    """The journeys with `station_id` / `station_parent_id` filled where the resolver can.
    Without a resolver, nothing is placed: every `station_id` stays None and the matching
    engine falls back to codes (design #304 §6)."""
    if resolver is None:
        return list(journeys)
    stations = await resolver.resolve(stop_places(journeys))
    if not stations:
        return list(journeys)
    return [
        journey.model_copy(
            update={"legs": [_leg_with_stations(leg, stations) for leg in journey.legs]}
        )
        for journey in journeys
    ]
