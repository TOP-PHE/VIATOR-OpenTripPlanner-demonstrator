"""What a page needs about one journey of a comparison: which bucket it is in, what it is
paired with, and why. Derived from the matching engine's comparison (#304)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .journey_similarity_result import MatchReason, MatchVerdict

ComparisonBucket = Literal["common", "left_only", "right_only", "uncomparable"]


class ComparisonMetadata(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    bucket: ComparisonBucket
    paired_with: str | None = None
    verdict: MatchVerdict | None = None
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    reasons: list[MatchReason] = Field(default_factory=list)
