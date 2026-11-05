"""The configurable fuzzy journey matching engine (#304).

Design: docs/journey-matching-engine-design.md. It compares two sets of canonical journeys
(#303) and says which are the same service, with a score and the reasons for it. It reads
`CanonicalJourney` only (never a provider's format) and does no I/O: station ids come filled by
the station resolver (#303 PR 5) before a comparison, when the caller has one.

    compare(left, right, profile) -> JourneyComparison
    JOURNEY_REFERENCE, JOURNEY_ENGINES, COVERAGE_ALIGNMENT, profile_from_config(values, name)
    reasons.sentence(reason, left_label, right_label)
"""

from . import reasons
from .engine import (
    ENGINE_VERSION,
    ComparisonSummary,
    JourneyComparison,
    UnmatchedJourney,
    compare,
)
from .profiles import (
    BUILT_IN,
    COVERAGE_ALIGNMENT,
    JOURNEY_ENGINES,
    JOURNEY_REFERENCE,
    MatchProfile,
    Weights,
    profile_from_config,
)

__all__ = [
    "BUILT_IN",
    "COVERAGE_ALIGNMENT",
    "ENGINE_VERSION",
    "JOURNEY_ENGINES",
    "JOURNEY_REFERENCE",
    "ComparisonSummary",
    "JourneyComparison",
    "MatchProfile",
    "UnmatchedJourney",
    "Weights",
    "compare",
    "profile_from_config",
    "reasons",
]
