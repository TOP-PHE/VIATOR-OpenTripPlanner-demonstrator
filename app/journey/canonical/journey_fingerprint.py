"""`JourneyFingerprint` v1: what `trip_signature` meant to be, on canonical journeys.

The spine is the timed legs only (walks and transfers stripped). Per leg: the mode family, the
station keys of boarding and alighting, and the boarding/alighting minutes in UTC. No route name,
no train number, no provider id (#303).

A hash cannot be tolerant: two times a minute apart can fall either side of any rounding
boundary. So `spine_key` hashes what must be equal (modes, stations), and `equivalent` /
`similarity` then compare the minutes with a tolerance.

Station key, in order: `UIC:<7 digits>` from `stop_place.uic`; `UNRESOLVED:<source>:<token>` for
an ÖBB station outside the UIC namespace (it agrees with nothing from another source, the
`OEBB:?` sentinel's property); `GEO:<lat>,<lon>` at 3 decimals (~110 m, the fallback
`transit_fingerprint` uses); `UNRESOLVED:<source>:<ref>`; `NONE` when there is nothing, which
never agrees with anything. The station module's `station_id` is not used: it is an identity
for the matching engine, never a hash input (design D8).
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

from .canonical_location import CanonicalLocation
from .canonical_service import TransportMode

if TYPE_CHECKING:
    from .canonical_journey import CanonicalJourney

ALGORITHM_VERSION: Literal[1] = 1
DEFAULT_TOLERANCE_SECONDS = 120
NO_STATION = "NONE"


def station_key(location: CanonicalLocation) -> str:
    """The fingerprint key of one leg end (see the module docstring for the order)."""
    stop = location.stop_place
    if stop is not None and stop.uic:
        return f"UIC:{stop.uic}"
    if stop is not None and stop.unresolved_token:
        return f"UNRESOLVED:{stop.source}:{stop.unresolved_token}"
    position = location.position or (stop.position if stop is not None else None)
    if position is not None:
        return f"GEO:{round(position.lat, 3):.3f},{round(position.lon, 3):.3f}"
    if stop is not None:
        return f"UNRESOLVED:{stop.source}:{stop.ref}"
    return NO_STATION


class SpineLeg(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: TransportMode
    board: str
    alight: str
    # Minutes since the epoch, UTC; None when the source gave no time.
    board_minute: int | None
    alight_minute: int | None

    def same_place(self, other: SpineLeg) -> bool:
        """Same mode and same stations, a `NONE` key agreeing with nothing."""
        return (
            self.mode == other.mode
            and _keys_agree(self.board, other.board)
            and _keys_agree(self.alight, other.alight)
        )


def _keys_agree(a: str, b: str) -> bool:
    return a == b and a != NO_STATION


def _minute(seconds: float) -> int:
    return int(seconds // 60)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class JourneyFingerprint(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    algorithm_version: Literal[1] = ALGORITHM_VERSION
    spine: tuple[SpineLeg, ...]

    @property
    def spine_key(self) -> str:
        """16 hex of the modes and stations, no times; "" for a journey without a timed leg
        (never equal to anything, like `transit_fingerprint`'s empty string)."""
        if not self.spine:
            return ""
        return _digest("|".join(f"{leg.mode}:{leg.board}-{leg.alight}" for leg in self.spine))

    @property
    def strict_key(self) -> str:
        """16 hex of the spine and its minutes: identity within one source (dedup)."""
        if not self.spine:
            return ""
        minutes = "|".join(f"{leg.board_minute}-{leg.alight_minute}" for leg in self.spine)
        return _digest(f"{self.spine_key}|{minutes}")

    def equivalent(
        self, other: JourneyFingerprint, tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS
    ) -> bool:
        """Same spine (modes, stations) and every boarding/alighting minute within the
        tolerance. Two journeys with a `NONE` station are never equivalent."""
        if not self.spine or len(self.spine) != len(other.spine):
            return False
        return all(
            a.same_place(b) and _within(a, b, tolerance_seconds)
            for a, b in zip(self.spine, other.spine, strict=True)
        )

    def similarity(
        self, other: JourneyFingerprint, tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS
    ) -> float:
        """In [0, 1]: the share of legs that line up (longest common subsequence of
        mode + stations, over the longer spine), times how close their minutes are."""
        if not self.spine or not other.spine:
            return 0.0
        pairs = _aligned_pairs(self.spine, other.spine)
        if not pairs:
            return 0.0
        timing = sum(_time_agreement(a, b, tolerance_seconds) for a, b in pairs) / len(pairs)
        return round(len(pairs) / max(len(self.spine), len(other.spine)) * timing, 4)


def _delta_seconds(a: int | None, b: int | None) -> int | None:
    if a is None or b is None:
        return None
    return abs(a - b) * 60


def _within(a: SpineLeg, b: SpineLeg, tolerance_seconds: int) -> bool:
    board = _delta_seconds(a.board_minute, b.board_minute)
    alight = _delta_seconds(a.alight_minute, b.alight_minute)
    return (
        board is not None
        and alight is not None
        and board <= tolerance_seconds
        and alight <= tolerance_seconds
    )


def _time_agreement(a: SpineLeg, b: SpineLeg, tolerance_seconds: int) -> float:
    board = _delta_seconds(a.board_minute, b.board_minute)
    alight = _delta_seconds(a.alight_minute, b.alight_minute)
    if board is None or alight is None:
        return 0.0
    if tolerance_seconds <= 0:
        return 1.0 if board == 0 and alight == 0 else 0.0
    return max(0.0, 1.0 - (board + alight) / (2 * tolerance_seconds))


def _aligned_pairs(
    left: tuple[SpineLeg, ...], right: tuple[SpineLeg, ...]
) -> list[tuple[SpineLeg, SpineLeg]]:
    """The legs of a longest common subsequence under `same_place`, in order."""
    rows, cols = len(left), len(right)
    table = [[0] * (cols + 1) for _ in range(rows + 1)]
    for i in range(rows - 1, -1, -1):
        for j in range(cols - 1, -1, -1):
            if left[i].same_place(right[j]):
                table[i][j] = table[i + 1][j + 1] + 1
            else:
                table[i][j] = max(table[i + 1][j], table[i][j + 1])
    pairs: list[tuple[SpineLeg, SpineLeg]] = []
    i = j = 0
    while i < rows and j < cols:
        if left[i].same_place(right[j]):
            pairs.append((left[i], right[j]))
            i += 1
            j += 1
        elif table[i + 1][j] >= table[i][j + 1]:
            i += 1
        else:
            j += 1
    return pairs


def fingerprint_of(journey: CanonicalJourney) -> JourneyFingerprint:
    spine: list[SpineLeg] = []
    for leg in journey.legs:
        if not leg.is_timed or leg.service is None:
            continue
        spine.append(
            SpineLeg(
                mode=leg.service.mode,
                board=station_key(leg.origin),
                alight=station_key(leg.destination),
                board_minute=_minute(leg.departure.timetabled.timestamp())
                if leg.departure is not None
                else None,
                alight_minute=_minute(leg.arrival.timetabled.timestamp())
                if leg.arrival is not None
                else None,
            )
        )
    return JourneyFingerprint(spine=tuple(spine))
