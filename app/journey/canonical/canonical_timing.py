"""A departure or an arrival: OJP `ServiceDeparture` / `ServiceArrival`.

Every instant is aware and in UTC. The adapters do the conversions once: OTP epoch
milliseconds, MOTIS ISO strings, OJP ISO with an offset, HAFAS local `date` + `HHMMSS` in
Europe/Vienna. No consumer converts a time again.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import AwareDatetime, BaseModel, ConfigDict, field_validator


class CanonicalTiming(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    timetabled: AwareDatetime
    estimated: AwareDatetime | None = None
    # IANA zone of the stop when the source says so, for display and OJP export.
    local_timezone: str | None = None

    @field_validator("timetabled", "estimated")
    @classmethod
    def _in_utc(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(UTC) if value is not None else None
