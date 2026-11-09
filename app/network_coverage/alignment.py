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
departure tolerance. One ÖBB page is its next 5 connections from the run's departure time (a
median of 3.8 h), VIATOR's journeys cover the day: the check asks ÖBB's later pages until its
span reaches VIATOR's last first boarding (`last_boarding_to_cover`), at most
`COVERAGE_VERIFY_MAX_PAGES` (`oebb_journeys.fetch_cell_answer`), and the span runs over every page
asked. A VIATOR journey still after ÖBB's last connection says nothing either way. It is counted
(`outside_reference_window`), never `left_only` and never in the score. The lower bound is still
`journeys_from_depart_at` (the run's departure time).

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
{code: n}}`: why the cell scored so, which nothing recorded before #304. The runner adds
`oebb_pages: {asked, stopped, error?}` when it considered paging (`CellAnswer.pages_detail`).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from ..journey.canonical import CanonicalJourney
from ..journey.matching import COVERAGE_ALIGNMENT, ENGINE_VERSION, MatchProfile, compare
from ..journey.matching import reasons as match_reasons
from .oebb_journeys import STOP_MAX_PAGES

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


def last_boarding_to_cover(viator: Sequence[CanonicalJourney], tolerance_s: int) -> datetime | None:
    """The first boarding ÖBB's latest connection must reach for every VIATOR journey to lie
    within ÖBB's span (`reference_window`): VIATOR's last first boarding, less the tolerance the
    span is widened by. None when no VIATOR journey has a ride: nothing for a later page to
    cover. `viator` are the journeys the scorer compares (from the run's departure time on)."""
    boardings = [time for time in (_boarding(j) for j in viator) if time is not None]
    if not boardings:
        return None
    return max(boardings) - timedelta(seconds=tolerance_s)


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


# The order a cell's reasons are listed in: what stops a pair first, then what qualifies one.
_EXPLAINED_FIRST = (
    match_reasons.OUTSIDE_REFERENCE_WINDOW,
    match_reasons.ENDPOINT_STATION_DIFFERS,
    match_reasons.TRAIN_NUMBER_DIFFERS,
    match_reasons.DEPARTURE_OUTSIDE_TOLERANCE,
    match_reasons.ARRIVAL_OUTSIDE_TOLERANCE,
    match_reasons.ENDPOINT_UNRESOLVED,
    match_reasons.SPLITS_AFTER_SHARED_RIDE,
    match_reasons.SHORTER_TRANSFER,
)


def explain_cell(
    detail: dict[str, Any] | None, left: str = "VIATOR", right: str = "ÖBB"
) -> list[str]:
    """The lines a page prints under a cell's tier (M6): the pairs by verdict and the journeys
    on one side only, then each reason with the number of pairs or journeys it was met for.
    Plain text, no id or code of a stop: the share page prints them as they are. None for a
    cell scored before #304 (no detail)."""
    if not isinstance(detail, dict):
        return []
    raw_counts, raw_reasons = detail.get("counts"), detail.get("reasons")
    counts: dict[str, Any] = raw_counts if isinstance(raw_counts, dict) else {}
    reasons: dict[str, Any] = raw_reasons if isinstance(raw_reasons, dict) else {}

    def count(key: str) -> int:
        value = counts.get(key)
        return value if isinstance(value, int) and value > 0 else 0

    parts = [
        f"{count('same_service')} same service",
        f"{count('probably_same')} probably the same",
        f"{count('viator_only')} {left} only",
        f"{count('oebb_only')} {right} only",
    ]
    lines = [f"{left} vs {right}: " + ", ".join(parts)]
    pages_line = _pages_line(detail.get("oebb_pages"), right)
    if pages_line:
        lines.append(pages_line)
    order = [*_EXPLAINED_FIRST, *sorted(set(reasons) - set(_EXPLAINED_FIRST))]
    for code in order:
        value = reasons.get(code)
        if isinstance(value, int):
            line = match_reasons.summary_sentence(code, value, left, right)
            if line:
                lines.append(line)
    return lines


def _pages_line(pages: Any, right: str) -> str:
    """The line for a check that asked ÖBB more than one page, or whose later page failed; "" for
    one page (as before paging). The error itself stays in the detail and the worker log: these
    lines reach the share page."""
    if not isinstance(pages, dict):
        return ""
    asked = pages.get("asked")
    if not isinstance(asked, int) or asked < 2:
        return ""
    if pages.get("error"):
        kept = asked - 1
        noun = "page" if kept == 1 else "pages"
        return f"{right}'s page {asked} could not be read: its first {kept} {noun} are compared"
    if pages.get("stopped") == STOP_MAX_PAGES:
        return f"{right} answered over {asked} pages, the most a cell asks for"
    return f"{right} answered over {asked} pages"
