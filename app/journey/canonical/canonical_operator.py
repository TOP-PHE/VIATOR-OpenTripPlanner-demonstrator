"""The operator of a service: OJP `OperatorRef`, Transmodel `Operator`."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class CanonicalOperator(BaseModel):
    """`ref` is the source's id (GTFS agency id, HAFAS operator id, OJP `OperatorRef`); `name`
    is for display; `code` is a RICS code once `master_carriers` has a loader."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ref: str | None = None
    name: str | None = None
    code: str | None = None
    url: str | None = None
