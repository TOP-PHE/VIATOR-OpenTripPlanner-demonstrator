"""A platform: OJP `PlannedQuay`, NeTEx `Quay`.

No V1 adapter fills it: OTP and MOTIS return platform-level stops but no platform field, and
parsing a platform out of an id (`SBB:8507000:0:7`) is provider-specific guessing. The class is
here so an adapter that does get a platform has somewhere to put it.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .canonical_stop_place import GeoPosition


class CanonicalQuay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ref: str = Field(min_length=1)
    name: str | None = None
    public_code: str | None = None
    position: GeoPosition | None = None
