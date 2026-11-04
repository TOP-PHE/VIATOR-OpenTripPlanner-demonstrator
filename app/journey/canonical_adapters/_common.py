"""Helpers the four adapters share: instants, positions, stop places, leg kinds."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..canonical import (
    CanonicalJourney,
    CanonicalLocation,
    CanonicalStopPlace,
    CanonicalTiming,
    GeoPosition,
)
from ..canonical.canonical_transfer import mark_transfers as mark_transfers
from ..signature import stop_token

ADAPTER_VERSION = 1

# Leg modes that are not a ride in a vehicle. Wider than the old `{WALK, TRANSFER, ''}`
# predicate: bike and car legs are not timed legs either (VIATOR asks the engines for
# TRANSIT + WALK only, so they do not occur in practice).
_CONTINUOUS_MODES: dict[str, str] = {
    "WALK": "WALK",
    "TRANSFER": "WALK",
    "BIKE": "BIKE",
    "BICYCLE": "BIKE",
    "BICYCLE_RENTAL": "BIKE",
    "RENTAL": "BIKE",
    "SCOOTER": "BIKE",
    "SCOOTER_RENTAL": "BIKE",
    "CAR": "CAR",
    "CAR_PARKING": "CAR",
    "CAR_DROPOFF": "CAR",
    "CAR_PICKUP": "CAR",
    "CAR_HAIL": "CAR",
    "CAR_RENTAL": "CAR",
}


def is_timed_mode(source_mode: Any) -> bool:
    """A leg with this mode is a ride: anything but a walk, a transfer, a bike or a car, and
    not an empty mode."""
    mode = str(source_mode or "").strip().upper()
    return bool(mode) and mode not in _CONTINUOUS_MODES


def continuous_mode(source_mode: Any) -> str | None:
    mode = str(source_mode or "").strip().upper()
    if not mode:
        return None
    return _CONTINUOUS_MODES.get(mode, mode)


def parse_instant(value: Any) -> datetime | None:
    """An ISO-8601 string (`Z` or an offset) as an aware UTC datetime; naive input is UTC,
    anything unparseable None."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def timing(instant: datetime | None) -> CanonicalTiming | None:
    return CanonicalTiming(timetabled=instant) if instant is not None else None


def number_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def position(lat: Any, lon: Any) -> GeoPosition | None:
    """A position when both coordinates are numbers in range, else None."""
    la, lo = number_or_none(lat), number_or_none(lon)
    if la is None or lo is None or not (-90.0 <= la <= 90.0 and -180.0 <= lo <= 180.0):
        return None
    return GeoPosition(lat=la, lon=lo)


def text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def viator_stop_place(
    *,
    source: str,
    ref: str,
    name: str | None,
    at: GeoPosition | None,
    stop_code: Any = None,
    parent_ref: str | None = None,
    codes: dict[str, str] | None = None,
) -> CanonicalStopPlace:
    """A stop of OTP, MOTIS or OJP. `uic` follows the fingerprint's own token rules
    (`signature.stop_token`): a canonical token, a UIC-shaped `stop_code`, a UIC inside the id
    (SBB 7 digits, SNCF 8, a Swiss SLOID + 850), else nothing."""
    all_codes = dict(codes or {})
    if stop_code is not None:
        all_codes["GTFS_STOP_CODE"] = str(stop_code)
    token = stop_token(
        ref,
        at.lat if at is not None else None,
        at.lon if at is not None else None,
        str(stop_code) if stop_code is not None else None,
    )
    return CanonicalStopPlace(
        ref=ref,
        source=source,
        name=name,
        uic=token[4:] if token.startswith("UIC:") else None,
        unresolved_token=token if token.startswith("OEBB:") else None,
        codes=all_codes,
        parent_ref=parent_ref,
        position=at,
    )


def location(
    stop: CanonicalStopPlace | None, name: str | None, at: GeoPosition | None
) -> CanonicalLocation:
    if stop is None:
        return CanonicalLocation(kind="coordinate", name=name, position=at)
    return CanonicalLocation(kind="stop", name=name, stop_place=stop, position=at)


def source_rank(journey: CanonicalJourney) -> int:
    """The journey's position in its source's answer: the last part of the id every adapter
    gives (`<provider>:<scope>:<rank>`)."""
    return int(journey.id.rsplit(":", 1)[1])
