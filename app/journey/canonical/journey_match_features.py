"""The features of one (left, right) journey pair. The structure is part of #303; computing it
is the matching engine's (#304).

`None` means "no evidence", never "disagree": a score ignores a missing feature instead of
counting it as 0. Deltas are right minus left, in seconds or counts.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class JourneyMatchFeatures(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    departure_delta: int | None = None
    arrival_delta: int | None = None
    duration_delta: int | None = None
    transfer_count_delta: int = 0
    station_overlap: float | None = Field(default=None, ge=0.0, le=1.0)
    operator_overlap: float | None = Field(default=None, ge=0.0, le=1.0)
    service_overlap: float | None = Field(default=None, ge=0.0, le=1.0)
    route_overlap: float | None = Field(default=None, ge=0.0, le=1.0)
    fingerprint_similarity: float = Field(default=0.0, ge=0.0, le=1.0)
