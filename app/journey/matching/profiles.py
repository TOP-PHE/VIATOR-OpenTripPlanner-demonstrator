"""Match profiles: the settings one comparison runs under (#304 design §10).

A profile is a frozen value: tolerances, verdict thresholds, the three policies of the owner's
decisions (M2 train numbers, M3 line labels, M4 unresolved stations and its position fallback),
M8's transfer difference, M10's local access modes, and the score's weights.

Three built-in profiles; `profile_from_config` overlays the platform settings
(`JOURNEY_MATCH_*` for the two journey profiles, `COVERAGE_MATCH_*` for coverage) on one of them.
The settings themselves are declared where they are read (the wiring of #304); a missing or
unreadable value keeps the built-in default.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..canonical import TransportMode

TrainNumberConflict = Literal["veto", "veto_unless_same_minutes", "penalty"]
UnresolvedEndpoints = Literal["allow", "reject"]

# M10: the modes of a local ride only one side takes at the start or the end (a tram from the
# hub's point to the station). Rail, S-Bahn included, is never trimmed.
LOCAL_ACCESS_MODES: tuple[TransportMode, ...] = (
    TransportMode.TRAM,
    TransportMode.BUS,
    TransportMode.SUBWAY,
    TransportMode.FERRY,
    TransportMode.CABLEWAY,
    TransportMode.FUNICULAR,
)


class Weights(BaseModel):
    """The weights of the score's components (§7); a component without evidence is left out
    of the mean, its weight with it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    time: float = Field(default=0.30, ge=0.0)
    station: float = Field(default=0.25, ge=0.0)
    route: float = Field(default=0.15, ge=0.0)
    service: float = Field(default=0.15, ge=0.0)
    transfers: float = Field(default=0.10, ge=0.0)
    operator: float = Field(default=0.05, ge=0.0)


class MatchProfile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    # M1: first boarding and last alighting, seconds.
    departure_tolerance_s: int = Field(default=120, ge=0, le=1800)
    arrival_tolerance_s: int = Field(default=120, ge=0, le=1800)
    same_service_min: float = Field(default=0.90, ge=0.0, le=1.0)
    probable_min: float = Field(default=0.75, ge=0.0, le=1.0)
    # M2, as amended and accepted on 2026-11-05.
    train_number_conflict: TrainNumberConflict = "veto_unless_same_minutes"
    # M3: a line-label conflict lowers the score, never splits a pair.
    line_conflict_penalty: float = Field(default=0.10, ge=0.0, le=1.0)
    # M4: an endpoint the station data cannot compare.
    unresolved_endpoints: UnresolvedEndpoints = "allow"
    # M4's fallback: the stations of a leg whose times agree agree when each stop lies within
    # this distance of the other side's; 0 turns it off.
    position_fallback_m: int = Field(default=1000, ge=0, le=5000)
    # M8: one side's change is shorter by at least this.
    transfer_difference_s: int = Field(default=300, ge=60, le=3600)
    # M10.
    local_access_modes: tuple[TransportMode, ...] = LOCAL_ACCESS_MODES
    use_route_aliases: bool = False
    weights: Weights = Weights()


JOURNEY_REFERENCE = MatchProfile(name="journey_reference", unresolved_endpoints="allow")
JOURNEY_ENGINES = MatchProfile(
    name="journey_engines",
    departure_tolerance_s=60,
    arrival_tolerance_s=60,
    unresolved_endpoints="allow",
)
COVERAGE_ALIGNMENT = MatchProfile(name="coverage_alignment", unresolved_endpoints="reject")

BUILT_IN: dict[str, MatchProfile] = {
    profile.name: profile for profile in (JOURNEY_REFERENCE, JOURNEY_ENGINES, COVERAGE_ALIGNMENT)
}

# The platform settings of §10, without their prefix, and the profile field each one sets.
SETTING_FIELDS: dict[str, str] = {
    "DEPARTURE_TOLERANCE_S": "departure_tolerance_s",
    "ARRIVAL_TOLERANCE_S": "arrival_tolerance_s",
    "SAME_SERVICE_MIN_PCT": "same_service_min",
    "PROBABLE_MIN_PCT": "probable_min",
    "TRAIN_NUMBER_CONFLICT": "train_number_conflict",
    "UNRESOLVED_ENDPOINTS": "unresolved_endpoints",
    "POSITION_FALLBACK_M": "position_fallback_m",
    "TRANSFER_DIFFERENCE_S": "transfer_difference_s",
}
_PERCENT_FIELDS = frozenset({"same_service_min", "probable_min"})


def settings_prefix(name: str) -> str:
    """`COVERAGE_MATCH_` for the coverage profile, `JOURNEY_MATCH_` for the others."""
    return "COVERAGE_MATCH_" if name == COVERAGE_ALIGNMENT.name else "JOURNEY_MATCH_"


def profile_from_config(values: Mapping[str, Any], name: str) -> MatchProfile:
    """The built-in profile `name` with the platform settings of `values` laid over it, each
    one checked by the profile's own bounds: a value out of them, or of the wrong type, keeps
    that field's built-in value. KeyError for an unknown profile name."""
    profile = BUILT_IN[name]
    prefix = settings_prefix(name)
    for suffix, field in SETTING_FIELDS.items():
        key = prefix + suffix
        if key not in values or values[key] is None:
            continue
        value = values[key]
        if (
            field in _PERCENT_FIELDS
            and isinstance(value, int | float)
            and not isinstance(value, bool)
        ):
            value = value / 100
        try:
            profile = MatchProfile.model_validate({**profile.model_dump(), field: value})
        except ValidationError:
            continue
    return profile
