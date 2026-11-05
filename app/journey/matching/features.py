"""The features of one (left, right) pair (#304 design §4), on the spines after M10.

`analyse` computes everything the gates and the score read: the deltas (right minus left), the
two endpoints' station agreement (M4's fallback applied when the end rides' times agree), the
station, route, service and operator overlaps, and each aligned ride's service comparison.
`None` is "no evidence", never "disagree".
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

from ..canonical import CanonicalJourney, CanonicalLocation, JourneyMatchFeatures
from .identifiers import Agreement, ServiceComparison, compare_services
from .profiles import MatchProfile
from .spines import (
    Ride,
    Trimmed,
    aligned,
    ride_match,
    rides_of,
    route_match,
    seconds_between,
    times_agree,
    trim_local_access,
)
from .stations import StationAgreement, compare_stations, with_position_fallback

# Legal forms and DELFI's operator-number prefix, left out of an operator's name (§4).
_LEGAL_FORMS = re.compile(r"\b(?:ag|gmbh|sa|s\.p\.a\.|spa|se|ltd|plc|bv|nv|as|ab|oy)\b\.?")
_DELFI_PREFIX = re.compile(r"^\d+_+\s*")
_NOT_A_WORD = re.compile(r"[^\w]+")


@dataclass(frozen=True)
class AlignedRide:
    left: Ride
    right: Ride
    service: ServiceComparison


@dataclass(frozen=True)
class PairAnalysis:
    trimmed: Trimmed
    rides: tuple[AlignedRide, ...]
    route_pairs: int
    first_station: StationAgreement
    last_station: StationAgreement
    station_notes: tuple[tuple[int, str], ...]
    features: JourneyMatchFeatures

    @property
    def left(self) -> list[Ride]:
        return self.trimmed.left

    @property
    def right(self) -> list[Ride]:
        return self.trimmed.right


def _end_agreement(
    a: CanonicalLocation, b: CanonicalLocation, times_ok: bool, profile: MatchProfile
) -> StationAgreement:
    found = compare_stations(a, b)
    if times_ok:
        found = with_position_fallback(found, a, b, profile.position_fallback_m)
    return found


def operator_name(name: str | None) -> str | None:
    """An operator's name as compared: case-folded, without legal forms or DELFI's number
    prefix, words joined by one space; None when nothing is left."""
    if not name:
        return None
    folded = unicodedata.normalize("NFKC", name).casefold().strip()
    folded = _DELFI_PREFIX.sub("", folded)
    folded = _LEGAL_FORMS.sub(" ", folded)
    words = _NOT_A_WORD.sub(" ", folded).split()
    return " ".join(words) or None


def _operators(rides: Sequence[Ride]) -> set[str]:
    names = set()
    for ride in rides:
        operator = ride.service.operator
        name = operator_name(operator.name if operator is not None else None)
        if name:
            names.add(name)
    return names


def _overlap(agreements: Sequence[StationAgreement]) -> float | None:
    comparable = [item for item in agreements if item.agreement != Agreement.UNKNOWN]
    if not comparable:
        return None
    agreeing = sum(1 for item in comparable if item.agreement == Agreement.AGREE)
    return round(agreeing / len(comparable), 4)


def _service_overlap(rides: Sequence[AlignedRide]) -> float | None:
    """AGREE 1, a train-number CONFLICT 0; a line CONFLICT and UNKNOWN are no evidence (§7)."""
    values = []
    for ride in rides:
        found = ride.service
        if found.agreement == Agreement.AGREE:
            values.append(1.0)
        elif found.agreement == Agreement.CONFLICT and found.kind == "number":
            values.append(0.0)
    return round(sum(values) / len(values), 4) if values else None


def _end_rides_agree(a: Ride, b: Ride, profile: MatchProfile) -> bool:
    """Whether M4's fallback may apply to the two journeys' end rides: same mode, times agreeing."""
    return a.mode == b.mode and times_agree(a, b, profile)


def _inner_agreements(
    rides: Sequence[AlignedRide], trimmed: Trimmed, profile: MatchProfile
) -> tuple[list[StationAgreement], list[tuple[int, str]]]:
    """The station agreements of the aligned rides' ends, the journeys' two endpoints left out
    (the caller has them), and the notes they carry, each with its ride's index."""
    first = (trimmed.left[0], trimmed.right[0])
    last = (trimmed.left[-1], trimmed.right[-1])
    agreements: list[StationAgreement] = []
    notes: list[tuple[int, str]] = []
    for ride in rides:
        ends = []
        if (ride.left, ride.right) != first:
            ends.append((ride.left.leg.origin, ride.right.leg.origin))
        if (ride.left, ride.right) != last:
            ends.append((ride.left.leg.destination, ride.right.leg.destination))
        for end_l, end_r in ends:
            found = _end_agreement(end_l, end_r, True, profile)
            agreements.append(found)
            if found.note is not None:
                notes.append((ride.left.index, found.note))
    return agreements, notes


def analyse(left: CanonicalJourney, right: CanonicalJourney, profile: MatchProfile) -> PairAnalysis:
    """The analysis of a pair whose two journeys both have a ride (G1 is the caller's)."""
    trimmed = trim_local_access(rides_of(left), rides_of(right), profile)
    l_rides, r_rides = trimmed.left, trimmed.right
    pairs = aligned(l_rides, r_rides, ride_match(profile))
    rides = tuple(
        AlignedRide(a, b, compare_services(a.service, b.service, leg=a.index)) for a, b in pairs
    )
    first_l, first_r, last_l, last_r = l_rides[0], r_rides[0], l_rides[-1], r_rides[-1]
    first_station = _end_agreement(
        first_l.leg.origin, first_r.leg.origin, _end_rides_agree(first_l, first_r, profile), profile
    )
    last_station = _end_agreement(
        last_l.leg.destination,
        last_r.leg.destination,
        _end_rides_agree(last_l, last_r, profile),
        profile,
    )
    agreements, notes = _inner_agreements(rides, trimmed, profile)
    agreements = [first_station, last_station, *agreements]
    notes = [
        *(
            (leg, found.note)
            for leg, found in ((first_l.index, first_station), (last_l.index, last_station))
            if found.note
        ),
        *notes,
    ]
    route_pairs = len(aligned(l_rides, r_rides, route_match))
    l_ops, r_ops = _operators(l_rides), _operators(r_rides)
    tolerance = max(profile.departure_tolerance_s, profile.arrival_tolerance_s)
    l_span = seconds_between(first_l.departure, last_l.arrival)
    r_span = seconds_between(first_r.departure, last_r.arrival)
    features = JourneyMatchFeatures(
        departure_delta=seconds_between(first_l.departure, first_r.departure),
        arrival_delta=seconds_between(last_l.arrival, last_r.arrival),
        duration_delta=r_span - l_span if l_span is not None and r_span is not None else None,
        transfer_count_delta=(len(r_rides) - 1) - (len(l_rides) - 1),
        station_overlap=_overlap(agreements),
        operator_overlap=(
            round(len(l_ops & r_ops) / len(l_ops | r_ops), 4) if l_ops and r_ops else None
        ),
        service_overlap=_service_overlap(rides),
        route_overlap=round(route_pairs / max(len(l_rides), len(r_rides)), 4),
        fingerprint_similarity=left.fingerprint().similarity(right.fingerprint(), tolerance),
    )
    return PairAnalysis(
        trimmed=trimmed,
        rides=rides,
        route_pairs=route_pairs,
        first_station=first_station,
        last_station=last_station,
        station_notes=tuple(dict.fromkeys(notes)),
        features=features,
    )
