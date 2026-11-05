"""`compare(left, right, profile)`: the matching engine's one entry point (#304 design §3, §8).

Every journey with a ride on one side is weighed against every one on the other (`analyse`,
`assess`); the pairs that pass the gates with a verdict better than `different` are accepted
one to one, best first (verdict, score, smallest departure difference, then the journeys' ids),
each journey at most once. Deterministic: the same journeys give the same pairs whatever the
order they come in (ties are broken by id, never by position).

A journey left unpaired keeps why: the reasons of its closest candidate on the other side (the
smallest departure difference), the gate or verdict that stopped it, or `paired_elsewhere` when
that candidate went to a better match. A journey without a ride is uncomparable (G1).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..canonical import (
    CanonicalJourney,
    ComparisonMetadata,
    JourneySimilarityResult,
    MatchReason,
    MatchVerdict,
)
from . import reasons as r
from .features import PairAnalysis, analyse
from .profiles import MatchProfile
from .scoring import Assessment, assess

ENGINE_VERSION: Literal[1] = 1
_VERDICT_RANK: dict[MatchVerdict, int] = {"same_service": 0, "probably_same": 1, "different": 2}


class UnmatchedJourney(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    journey_id: str
    # The closest journey on the other side, None when it has none.
    closest_id: str | None = None
    reasons: list[MatchReason] = Field(default_factory=list)


class ComparisonSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    same_service: int = 0
    probably_same: int = 0
    left_only: int = 0
    right_only: int = 0
    uncomparable_left: int = 0
    uncomparable_right: int = 0


class JourneyComparison(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    engine_version: Literal[1] = ENGINE_VERSION
    profile: str
    pairs: list[JourneySimilarityResult] = Field(default_factory=list)
    left_only: list[UnmatchedJourney] = Field(default_factory=list)
    right_only: list[UnmatchedJourney] = Field(default_factory=list)
    uncomparable_left: list[str] = Field(default_factory=list)
    uncomparable_right: list[str] = Field(default_factory=list)
    summary: ComparisonSummary = ComparisonSummary()

    def reason_counts(self) -> dict[str, int]:
        """How many times each reason code occurs, over the pairs and the unpaired journeys
        (one count per journey or pair for a code): what a coverage cell records (M6)."""
        counts: dict[str, int] = {}
        groups = [pair.reasons for pair in self.pairs]
        groups += [item.reasons for item in (*self.left_only, *self.right_only)]
        for found in groups:
            for code in dict.fromkeys(item.code for item in found):
                counts[code] = counts.get(code, 0) + 1
        return dict(sorted(counts.items()))

    def metadata(self) -> tuple[dict[str, ComparisonMetadata], dict[str, ComparisonMetadata]]:
        """What a page needs per journey card, for the left side and for the right side."""
        left: dict[str, ComparisonMetadata] = {}
        right: dict[str, ComparisonMetadata] = {}
        for pair in self.pairs:
            common = {"verdict": pair.verdict, "score": pair.score, "reasons": pair.reasons}
            left[pair.left_id] = ComparisonMetadata(
                bucket="common", paired_with=pair.right_id, **common
            )
            right[pair.right_id] = ComparisonMetadata(
                bucket="common", paired_with=pair.left_id, **common
            )
        for item in self.left_only:
            left[item.journey_id] = ComparisonMetadata(bucket="left_only", reasons=item.reasons)
        for item in self.right_only:
            right[item.journey_id] = ComparisonMetadata(bucket="right_only", reasons=item.reasons)
        for journey_id in self.uncomparable_left:
            left[journey_id] = ComparisonMetadata(bucket="uncomparable")
        for journey_id in self.uncomparable_right:
            right[journey_id] = ComparisonMetadata(bucket="uncomparable")
        return left, right


@dataclass(frozen=True)
class _Candidate:
    left: int
    right: int
    left_id: str
    right_id: str
    analysis: PairAnalysis
    assessment: Assessment

    @property
    def departure_gap(self) -> int:
        delta = self.analysis.features.departure_delta
        return abs(delta) if delta is not None else 1 << 62


def _exact(left: CanonicalJourney, right: CanonicalJourney) -> bool:
    key = left.fingerprint().strict_key
    return bool(key) and key == right.fingerprint().strict_key


def _result(
    left: CanonicalJourney, right: CanonicalJourney, candidate: _Candidate
) -> JourneySimilarityResult:
    assessment = candidate.assessment
    return JourneySimilarityResult(
        left_id=left.id,
        right_id=right.id,
        verdict=assessment.verdict,
        score=assessment.score,
        exact=assessment.exact,
        features=candidate.analysis.features,
        reasons=assessment.reasons,
    )


def _closest(candidates: Sequence[_Candidate]) -> _Candidate | None:
    if not candidates:
        return None
    return min(candidates, key=lambda c: (c.departure_gap, c.left_id, c.right_id))


def _unmatched(
    journey: CanonicalJourney,
    candidates: Sequence[_Candidate],
    others: Sequence[CanonicalJourney],
    *,
    left_side: bool,
) -> UnmatchedJourney:
    closest = _closest(candidates)
    if closest is None:
        return UnmatchedJourney(journey_id=journey.id, reasons=[r.reason(r.NO_CANDIDATE, "info")])
    other = others[closest.right if left_side else closest.left]
    found = list(closest.assessment.reasons)
    if closest.assessment.passed:
        # Its closest candidate passed, but went to a better match.
        found = [r.reason(r.PAIRED_ELSEWHERE, "info"), *found]
    return UnmatchedJourney(journey_id=journey.id, closest_id=other.id, reasons=found)


def compare(
    left: Sequence[CanonicalJourney], right: Sequence[CanonicalJourney], profile: MatchProfile
) -> JourneyComparison:
    """The comparison of two sets of journeys under `profile` (see the module docstring)."""
    comparable_left = [i for i, journey in enumerate(left) if journey.timed_legs]
    comparable_right = [j for j, journey in enumerate(right) if journey.timed_legs]
    candidates: list[_Candidate] = []
    for i in comparable_left:
        for j in comparable_right:
            analysis = analyse(left[i], right[j], profile)
            assessment = assess(analysis, profile, exact=_exact(left[i], right[j]))
            candidates.append(_Candidate(i, j, left[i].id, right[j].id, analysis, assessment))
    passing = sorted(
        (c for c in candidates if c.assessment.passed),
        key=lambda c: (
            _VERDICT_RANK[c.assessment.verdict],
            -c.assessment.score,
            c.departure_gap,
            c.left_id,
            c.right_id,
        ),
    )
    used_left: set[int] = set()
    used_right: set[int] = set()
    accepted: list[_Candidate] = []
    for candidate in passing:
        if candidate.left in used_left or candidate.right in used_right:
            continue
        used_left.add(candidate.left)
        used_right.add(candidate.right)
        accepted.append(candidate)
    accepted.sort(key=lambda c: (c.left, c.right))
    pairs = [_result(left[c.left], right[c.right], c) for c in accepted]
    left_only = [
        _unmatched(left[i], [c for c in candidates if c.left == i], right, left_side=True)
        for i in comparable_left
        if i not in used_left
    ]
    right_only = [
        _unmatched(right[j], [c for c in candidates if c.right == j], left, left_side=False)
        for j in comparable_right
        if j not in used_right
    ]
    uncomparable_left = [left[i].id for i in range(len(left)) if i not in comparable_left]
    uncomparable_right = [right[j].id for j in range(len(right)) if j not in comparable_right]
    return JourneyComparison(
        profile=profile.name,
        pairs=pairs,
        left_only=left_only,
        right_only=right_only,
        uncomparable_left=uncomparable_left,
        uncomparable_right=uncomparable_right,
        summary=ComparisonSummary(
            same_service=sum(1 for p in pairs if p.verdict == "same_service"),
            probably_same=sum(1 for p in pairs if p.verdict == "probably_same"),
            left_only=len(left_only),
            right_only=len(right_only),
            uncomparable_left=len(uncomparable_left),
            uncomparable_right=len(uncomparable_right),
        ),
    )
