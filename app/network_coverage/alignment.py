"""A coverage cell's alignment: VIATOR's journeys against ÖBB's, scored by the matching engine.

Since #304 the pairs come from the journey matching engine (`app/journey/matching/`, design
docs/journey-matching-engine-design.md), profile `coverage_alignment`: first boarding and last
alighting within the tolerance (M1), no endpoint station that conflicts or that the station data
cannot compare (M4, its position fallback for a time-matched leg included), no train-number
conflict (M2, a same-minute one is a warning), a line label that differs only lowering the score
(M3). The exact fingerprint and the train-number-guarded ±5 min fuzzy tiers of before (PR-196a)
are gone: every pair the engine makes is credited by its verdict.

**Which VIATOR journeys a cell compares** (M9, `reference_window`): those whose first boarding
lies within ÖBB's span, from ÖBB's earliest to its latest first boarding, widened by the
departure tolerance. ÖBB answers its next 5 connections from the run's departure time (about
4 h), VIATOR's journeys cover the whole day: a VIATOR journey after ÖBB's last connection says
nothing either way. It is counted (`outside_reference_window`), never `left_only` and never in
the score. The lower bound is still `journeys_from_depart_at` (the run's departure time).

**Score** (unchanged)::

    score = credits / max(min(len(viator), 3), min(len(oebb), 3))

with a credit of 1.0 per `same_service` pair and 0.7 per `probably_same` pair (open question 2 of
the design: 0.7 keeps the fuzzy credit of before), `viator` the journeys within ÖBB's span. The
cap of three a side: a 3-of-3 alignment is the same evidence as 10-of-10. **Tiers** (unchanged):

    both empty                          → 'no_service'      (no score)
    VIATOR empty + ÖBB nonzero          → 'one_sided_oebb'  (score=0.0)
    VIATOR nonzero + ÖBB empty          → 'one_sided_viator'(score=0.0)
    score == 1.00                       → 'agree'
    0.70 <= score < 1.00                → 'mostly_agree'
    0.40 <= score < 0.70                → 'partial'
    0.00 <  score < 0.40                → 'disagree'
    score == 0.0 + both nonzero         → 'no_overlap'

**Detail** (M6, `external_alignment_detail`): `{engine_version, profile, counts: {same_service,
probably_same, viator_only, oebb_only, uncomparable, outside_reference_window}, reasons:
{code: n}}`: why the cell scored so, which nothing recorded before #304.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from ..journey.canonical import CanonicalJourney
from ..journey.matching import COVERAGE_ALIGNMENT, ENGINE_VERSION, MatchProfile, compare
from ..journey.matching import reasons as match_reasons

# Threshold constants — kept at module scope so the test that exercises tier boundaries can
# import and assert against the same values.
_TIER_AGREE_MIN = 1.00
_TIER_MOSTLY_MIN = 0.70
_TIER_PARTIAL_MIN = 0.40
SAME_SERVICE_CREDIT = 1.00
PROBABLY_SAME_CREDIT = 0.70
# Cap reward at 3 matches per side — see the module docstring.
_SCORE_CAP_PER_SIDE = 3


def journeys_from_depart_at(
    journeys: Sequence[CanonicalJourney], depart_at: datetime
) -> list[CanonicalJourney]:
    """Keep only journeys departing at/after `depart_at`, sorted chronologically (earliest
    first).

    ÖBB HAFAS's TripSearch is a forward "depart after" query (`numF=5`), while VIATOR's per-cell
    trip list aggregates K time slots spanning the whole requested day (runner.py's K-slot
    time-slicing): without this filter a cell's VIATOR side shows itineraries from hours before
    the run's departure time that ÖBB was never asked about. Used by the scorer and by the cell
    detail (`network_coverage._fetch_trips_by_search`), so both show the same scope.

    A journey's departure is its door-to-door start (`CanonicalJourney.departure`). A journey
    without one is dropped: with no placement in the window there is no basis to compare it."""
    dated = [
        (journey.departure.timetabled, journey)
        for journey in journeys
        if journey.departure is not None and journey.departure.timetabled >= depart_at
    ]
    dated.sort(key=lambda pair: pair[0])
    return [journey for _, journey in dated]


def _boarding(journey: CanonicalJourney) -> datetime | None:
    first = journey.first_boarding
    return first.timetabled if first is not None else None


def reference_window(
    viator: Sequence[CanonicalJourney],
    oebb: Sequence[CanonicalJourney],
    tolerance_s: int,
) -> tuple[list[CanonicalJourney], list[CanonicalJourney]]:
    """M9: the VIATOR journeys within ÖBB's span and those outside it, each in its order.

    ÖBB's span runs from its earliest to its latest first boarding, widened by `tolerance_s` at
    both ends. With no ÖBB journey that has a ride, there is no span: every VIATOR journey is
    within (the cell is then one-sided). A VIATOR journey without a ride (walk only) stays
    within: the engine counts it as uncomparable."""
    boardings = [time for time in (_boarding(j) for j in oebb) if time is not None]
    if not boardings:
        return list(viator), []
    margin = timedelta(seconds=tolerance_s)
    start, end = min(boardings) - margin, max(boardings) + margin
    inside: list[CanonicalJourney] = []
    outside: list[CanonicalJourney] = []
    for journey in viator:
        boarding = _boarding(journey)
        (inside if boarding is None or start <= boarding <= end else outside).append(journey)
    return inside, outside


def _classify_score(score: float | None, v_n: int, o_n: int) -> str:
    """Score + (viator_count, oebb_count) → tier label."""
    if v_n == 0 and o_n == 0:
        return "no_service"
    if v_n == 0:
        return "one_sided_oebb"
    if o_n == 0:
        return "one_sided_viator"
    # Both sides have trips beyond this point.
    if score is None:  # pragma: no cover — score is float when both nonzero
        return "no_overlap"
    if score >= _TIER_AGREE_MIN:
        return "agree"
    if score >= _TIER_MOSTLY_MIN:
        return "mostly_agree"
    if score >= _TIER_PARTIAL_MIN:
        return "partial"
    if score > 0.0:
        return "disagree"
    return "no_overlap"


@dataclass(frozen=True)
class CellAlignment:
    """A cell's score (None when both sides are empty), its tier, and why (M6)."""

    score: float | None
    tier: str
    detail: dict[str, Any] = field(default_factory=dict)


def _detail(
    profile: MatchProfile,
    counts: dict[str, int],
    reasons: dict[str, int],
) -> dict[str, Any]:
    return {
        "engine_version": ENGINE_VERSION,
        "profile": profile.name,
        "counts": counts,
        "reasons": dict(sorted(reasons.items())),
    }


def compute_alignment(
    viator_journeys: Sequence[CanonicalJourney],
    oebb_journeys: Sequence[CanonicalJourney],
    profile: MatchProfile = COVERAGE_ALIGNMENT,
) -> CellAlignment:
    """Score and tier the agreement between a cell's VIATOR journeys (already from the run's
    departure time on, `journeys_from_depart_at`) and ÖBB's answer, under `profile` (see the
    module docstring). Station ids, when the caller resolved them, are on the stops."""
    inside, outside = reference_window(
        viator_journeys, oebb_journeys, profile.departure_tolerance_s
    )
    v_n, o_n = len(inside), len(oebb_journeys)
    counts = {
        "same_service": 0,
        "probably_same": 0,
        "viator_only": 0,
        "oebb_only": 0,
        "uncomparable": 0,
        "outside_reference_window": len(outside),
    }
    reasons: dict[str, int] = {}
    if outside:
        reasons[match_reasons.OUTSIDE_REFERENCE_WINDOW] = len(outside)
    if v_n == 0 or o_n == 0:
        counts["viator_only"], counts["oebb_only"] = v_n, o_n
        score = None if v_n == 0 and o_n == 0 else 0.0
        return CellAlignment(
            score, _classify_score(score, v_n, o_n), _detail(profile, counts, reasons)
        )
    comparison = compare(inside, oebb_journeys, profile)
    summary = comparison.summary
    counts.update(
        same_service=summary.same_service,
        probably_same=summary.probably_same,
        viator_only=summary.left_only,
        oebb_only=summary.right_only,
        uncomparable=summary.uncomparable_left + summary.uncomparable_right,
    )
    for code, count in comparison.reason_counts().items():
        reasons[code] = reasons.get(code, 0) + count
    credit = (
        summary.same_service * SAME_SERVICE_CREDIT + summary.probably_same * PROBABLY_SAME_CREDIT
    )
    denominator = max(min(v_n, _SCORE_CAP_PER_SIDE), min(o_n, _SCORE_CAP_PER_SIDE))
    # Saturate at 1.0: the question is "do we agree?", not "how many?".
    score = min(round(credit / denominator, 2), 1.0)
    return CellAlignment(score, _classify_score(score, v_n, o_n), _detail(profile, counts, reasons))
