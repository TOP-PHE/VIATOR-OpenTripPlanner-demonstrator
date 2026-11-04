"""One end of a leg: a stop, or a place given by coordinates (an access walk from typed
coordinates). `position` is the leg's own coordinates as the source gave them (OTP gives a
platform's, OJP a station's centroid)."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator

from .canonical_quay import CanonicalQuay
from .canonical_stop_place import CanonicalStopPlace, GeoPosition

LocationKind = Literal["stop", "coordinate"]


class CanonicalLocation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: LocationKind
    name: str | None = None
    stop_place: CanonicalStopPlace | None = None
    quay: CanonicalQuay | None = None
    position: GeoPosition | None = None

    @model_validator(mode="after")
    def _stop_has_a_stop_place(self) -> Self:
        if (self.kind == "stop") != (self.stop_place is not None):
            raise ValueError("a 'stop' location has a stop_place, a 'coordinate' one has none")
        if self.quay is not None and self.stop_place is None:
            raise ValueError("a quay belongs to a stop place")
        return self
