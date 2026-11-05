"""Station agreement (#304 design §6): whether two leg ends are the same station.

The station module decides first (D8, M4): two ends it has placed agree when their VIATOR ids
are equal or share a parent (Bruxelles-Midi / Midi Eurostar), and conflict otherwise. For an end
it has not placed, the fingerprint's codes decide: two UIC codes, equal or not; two positions in
the same 3-decimal cell agree as `location_only` (OTP and MOTIS on the same feed). Anything else
is UNKNOWN: an ÖBB number outside the UIC namespace, a code against a position, nothing.

**M4's position fallback** (`with_position_fallback`): for a leg whose departure and arrival
agree within tolerance on both sides, an UNKNOWN end agrees, as `stations_by_position`, when each
stop lies within `position_fallback_m` of the other side's. It never turns a CONFLICT into an
agreement and is never station identity: nothing is kept, the decision is this leg's.

No distance threshold and no name ever decides that two stations are one (M5): St Pancras and
King's Cross (247 m) stay apart.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..canonical import CanonicalLocation, GeoPosition, station_key
from . import reasons as r
from .identifiers import Agreement

_EARTH_RADIUS_M = 6_371_008.8


@dataclass(frozen=True)
class StationAgreement:
    agreement: Agreement
    # The info reason that qualifies an agreement: `same_complex`, `location_only`,
    # `stations_by_position`; None otherwise.
    note: str | None = None

    @property
    def conflict(self) -> bool:
        return self.agreement == Agreement.CONFLICT

    def for_endpoint_gate(self) -> Agreement:
        """The agreement the endpoint gate reads (G3): a `location_only` agreement is no
        evidence there (§6: never used for the coverage endpoint gate)."""
        if self.agreement == Agreement.AGREE and self.note == r.LOCATION_ONLY:
            return Agreement.UNKNOWN
        return self.agreement


AGREE = StationAgreement(Agreement.AGREE)
CONFLICT = StationAgreement(Agreement.CONFLICT)
UNKNOWN = StationAgreement(Agreement.UNKNOWN)


def compare_stations(left: CanonicalLocation, right: CanonicalLocation) -> StationAgreement:
    """The agreement of two leg ends (the table of §6, first row that applies)."""
    a, b = left.stop_place, right.stop_place
    if a is not None and b is not None and a.station_id and b.station_id:
        if a.station_id == b.station_id:
            return AGREE
        parents_a = {a.station_id, a.station_parent_id} - {None}
        parents_b = {b.station_id, b.station_parent_id} - {None}
        if a.station_parent_id in parents_b or b.station_parent_id in parents_a:
            return StationAgreement(Agreement.AGREE, r.SAME_COMPLEX)
        return CONFLICT
    key_a, key_b = station_key(left), station_key(right)
    if key_a.startswith("UIC:") and key_b.startswith("UIC:"):
        return AGREE if key_a == key_b else CONFLICT
    if key_a.startswith("GEO:") and key_a == key_b:
        return StationAgreement(Agreement.AGREE, r.LOCATION_ONLY)
    return UNKNOWN


def position_of(location: CanonicalLocation) -> GeoPosition | None:
    if location.position is not None:
        return location.position
    return location.stop_place.position if location.stop_place is not None else None


def distance_m(a: GeoPosition, b: GeoPosition) -> float:
    """The great-circle distance in metres (haversine)."""
    lat_a, lat_b = math.radians(a.lat), math.radians(b.lat)
    d_lat = lat_b - lat_a
    d_lon = math.radians(b.lon - a.lon)
    h = math.sin(d_lat / 2) ** 2 + math.cos(lat_a) * math.cos(lat_b) * math.sin(d_lon / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def with_position_fallback(
    found: StationAgreement,
    left: CanonicalLocation,
    right: CanonicalLocation,
    fallback_m: int,
) -> StationAgreement:
    """M4's fallback, for an end of a leg whose times agree: an UNKNOWN end whose two stops lie
    within `fallback_m` of each other agrees, marked `stations_by_position`; anything else is
    returned as found. 0 turns it off."""
    if found.agreement != Agreement.UNKNOWN or fallback_m <= 0:
        return found
    a, b = position_of(left), position_of(right)
    if a is None or b is None or distance_m(a, b) > fallback_m:
        return found
    return StationAgreement(Agreement.AGREE, r.STATIONS_BY_POSITION)
