"""The outcome of comparing two journeys: a verdict, a score and the reasons for it.

Structure only (#303); the vocabulary of reason codes and their sentences belongs to the
matching engine (#304, design §9).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .journey_match_features import JourneyMatchFeatures

MatchVerdict = Literal["same_service", "probably_same", "different"]
ReasonSeverity = Literal["info", "warning", "veto", "gate"]


class MatchReason(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(min_length=1)
    severity: ReasonSeverity
    # The timed-leg index when the reason concerns one leg.
    leg: int | None = Field(default=None, ge=0)
    left_value: str | None = None
    right_value: str | None = None


class JourneySimilarityResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    left_id: str
    right_id: str
    verdict: MatchVerdict
    score: float = Field(ge=0.0, le=1.0)
    # Strict fingerprints equal.
    exact: bool = False
    features: JourneyMatchFeatures
    # Most important first.
    reasons: list[MatchReason] = Field(default_factory=list)
