"""A station: OJP / NeTEx `StopPlace`.

`uic` is set only by the rules the fingerprint already used (`signature.stop_token` for VIATOR
ids, `external_verify.oebb_stop_id` for HAFAS); `station_id` only by a `StationResolver`, from
the station module (MSMM). A VIATOR id of the module is an identity key, never a code: it never
goes into `uic`, `codes` or a fingerprint hash (design D8).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# The station module's id form (`ZZ-99999`), the hub table's CHECK
# (`20261015_1200_hub_station_id`).
VIATOR_ID_PATTERN = r"^[A-Z]{2}-[0-9]{5}$"


class GeoPosition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    lat: float = Field(ge=-90.0, le=90.0)
    lon: float = Field(ge=-180.0, le=180.0)


class CanonicalStopPlace(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # The id as the source gave it (`nl-nap_2992170`, `SBB:8507000:0:7`, a HAFAS lid,
    # `ch:1:sloid:7000:4:8`): provenance, and the key a resolver looks the stop up by.
    ref: str = Field(min_length=1)
    # The namespace of `ref`: `motis:<feed>`, `otp:<feed>`, `hafas:oebb`, `ojp:ch`.
    source: str = Field(min_length=1)
    name: str | None = None
    uic: str | None = Field(default=None, pattern=r"^[0-9]{7}$")
    # Codes by series (`GTFS_STOP_CODE`, `OEBB_EXT`, `SLOID`): the evidence a resolution needs.
    codes: dict[str, str] = Field(default_factory=dict)
    parent_ref: str | None = None
    # `OEBB:<payload>`: an ÖBB station we could not place in the UIC namespace. Kept apart so
    # it can never re-parse as a UIC (`test_oebb_token_can_never_reparse_as_a_uic`).
    unresolved_token: str | None = None
    position: GeoPosition | None = None
    country: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    # The station module's station (`station_module.current_id`) and its parent (a complex).
    station_id: str | None = Field(default=None, pattern=VIATOR_ID_PATTERN)
    station_parent_id: str | None = Field(default=None, pattern=VIATOR_ID_PATTERN)
