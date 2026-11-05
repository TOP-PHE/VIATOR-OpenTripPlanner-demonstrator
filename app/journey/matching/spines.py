"""The spines the engine compares: each journey's rides, after M10, and their alignment.

A **ride** is a timed leg (walks and transfers never count: OJP adds them, stop-id routing has
none). Its `index` is its place among the journey's timed legs: the `leg` of a reason.

**M10** (`trim_local_access`): a leading or trailing tram, bus, metro, ferry, cableway or
funicular ride that only one side takes, while the other side rides nothing then, is left out
of both the gates and the score, and noted `local_access_ride`. MOTIS starts some journeys with a
tram from the hub's point to the station; ÖBB starts at the station. Rail (S-Bahn included) is
never trimmed, and a journey keeps at least one ride.

**Two alignments**, each a longest common subsequence in order:

  * `route_pairs`: same mode family and no station CONFLICT at either end, an unknown station
    matching any (the score's `route` component, §4);
  * `ride_pairs`: the same and both ends' times within the tolerance: the same vehicle. The
    service comparison (§5), the train-number gate (G4), M4's fallback and M8 read these.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from ..canonical import CanonicalJourney, CanonicalLeg, CanonicalService, TransportMode
from .profiles import MatchProfile
from .stations import compare_stations


@dataclass(frozen=True)
class Ride:
    index: int
    leg: CanonicalLeg

    @property
    def service(self) -> CanonicalService:
        assert self.leg.service is not None  # a timed leg has a service (CanonicalLeg)
        return self.leg.service

    @property
    def mode(self) -> TransportMode:
        return self.service.mode

    @property
    def departure(self) -> datetime | None:
        return self.leg.departure.timetabled if self.leg.departure is not None else None

    @property
    def arrival(self) -> datetime | None:
        return self.leg.arrival.timetabled if self.leg.arrival is not None else None


def rides_of(journey: CanonicalJourney) -> list[Ride]:
    timed = [leg for leg in journey.legs if leg.is_timed and leg.service is not None]
    return [Ride(index, leg) for index, leg in enumerate(timed)]


def seconds_between(a: datetime | None, b: datetime | None) -> int | None:
    """`b - a` in whole seconds; None when a time is missing."""
    if a is None or b is None:
        return None
    return int((b - a).total_seconds())


def _within(a: datetime | None, b: datetime | None, tolerance_s: int) -> bool:
    delta = seconds_between(a, b)
    return delta is not None and abs(delta) <= tolerance_s


def same_minutes(a: Ride, b: Ride) -> bool:
    """Both ends of the two rides in the same minute (M2's amendment)."""
    if None in (a.departure, b.departure, a.arrival, b.arrival):
        return False
    return _minute(a.departure) == _minute(b.departure) and _minute(a.arrival) == _minute(b.arrival)


def _minute(instant: datetime | None) -> int:
    assert instant is not None
    return int(instant.timestamp() // 60)


def times_agree(a: Ride, b: Ride, profile: MatchProfile) -> bool:
    """Both ends of the two rides within the profile's tolerances."""
    return _within(a.departure, b.departure, profile.departure_tolerance_s) and _within(
        a.arrival, b.arrival, profile.arrival_tolerance_s
    )


def route_match(a: Ride, b: Ride) -> bool:
    return (
        a.mode == b.mode
        and not compare_stations(a.leg.origin, b.leg.origin).conflict
        and not compare_stations(a.leg.destination, b.leg.destination).conflict
    )


def ride_match(profile: MatchProfile) -> Callable[[Ride, Ride], bool]:
    def match(a: Ride, b: Ride) -> bool:
        return route_match(a, b) and times_agree(a, b, profile)

    return match


def aligned(
    left: Sequence[Ride], right: Sequence[Ride], match: Callable[[Ride, Ride], bool]
) -> list[tuple[Ride, Ride]]:
    """The pairs of a longest common subsequence under `match`, in order; the earliest pairs
    first among equal lengths, so the result does not depend on anything but the order."""
    rows, cols = len(left), len(right)
    table = [[0] * (cols + 1) for _ in range(rows + 1)]
    for i in range(rows - 1, -1, -1):
        for j in range(cols - 1, -1, -1):
            # `match` is no equality: taking a matching pair is not always best, so the three
            # moves are weighed.
            best = max(table[i + 1][j], table[i][j + 1])
            if match(left[i], right[j]):
                best = max(best, table[i + 1][j + 1] + 1)
            table[i][j] = best
    pairs: list[tuple[Ride, Ride]] = []
    i = j = 0
    while i < rows and j < cols:
        if match(left[i], right[j]) and table[i][j] == table[i + 1][j + 1] + 1:
            pairs.append((left[i], right[j]))
            i += 1
            j += 1
        elif table[i + 1][j] >= table[i][j + 1]:
            i += 1
        else:
            j += 1
    return pairs


@dataclass(frozen=True)
class Trimmed:
    """The rides kept on each side, and the rides M10 left out (`left_out_left`,
    `left_out_right`: their ride indices)."""

    left: list[Ride]
    right: list[Ride]
    left_out_left: tuple[int, ...] = ()
    left_out_right: tuple[int, ...] = ()


def _local(ride: Ride, profile: MatchProfile) -> bool:
    return ride.mode in profile.local_access_modes


def _trim_one_end(
    rides: list[Ride], other: list[Ride], profile: MatchProfile, *, leading: bool
) -> tuple[list[Ride], int | None]:
    """`rides` without its first (or last) ride when that ride is local, the journey has
    another, and the other side rides nothing then: its first boarding is no earlier than the
    local ride's arrival (its last alighting no later than the local ride's departure)."""
    if len(rides) < 2 or not other:
        return rides, None
    ride = rides[0] if leading else rides[-1]
    if not _local(ride, profile):
        return rides, None
    edge = other[0] if leading else other[-1]
    if leading:
        gap = seconds_between(ride.arrival, edge.departure)
    else:
        gap = seconds_between(edge.arrival, ride.departure)
    if gap is None or gap < -profile.departure_tolerance_s:
        return rides, None
    if route_match(ride, edge) and times_agree(ride, edge, profile):
        return rides, None
    return (rides[1:] if leading else rides[:-1]), ride.index


def trim_local_access(left: list[Ride], right: list[Ride], profile: MatchProfile) -> Trimmed:
    """M10 at both ends of both sides (see the module docstring)."""
    out_left: list[int] = []
    out_right: list[int] = []
    for leading in (True, False):
        left, cut = _trim_one_end(left, right, profile, leading=leading)
        if cut is not None:
            out_left.append(cut)
        right, cut = _trim_one_end(right, left, profile, leading=leading)
        if cut is not None:
            out_right.append(cut)
    return Trimmed(left, right, tuple(sorted(out_left)), tuple(sorted(out_right)))
