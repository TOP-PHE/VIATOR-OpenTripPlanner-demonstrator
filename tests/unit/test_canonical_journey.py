"""#303 PR 2 — the CanonicalJourney model: invariants, derived values, round trip, schema."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.journey.canonical import (
    CanonicalJourney,
    CanonicalLeg,
    CanonicalLocation,
    CanonicalService,
    CanonicalStopPlace,
    CanonicalTiming,
    JourneySource,
    TransportMode,
)
from app.journey.canonical.schema import schema_text
from app.journey.canonical_adapters import hafas_adapter, motis_adapter, ojp_adapter, otp_adapter
from tests.unit import canonical_fixtures as fx

SCHEMA_FILE = Path(__file__).parents[2] / "docs" / "schemas" / "canonical-journey-v1.schema.json"


def _all_fixture_journeys() -> list[CanonicalJourney]:
    return [
        *motis_adapter.journeys_from_plan(fx.MOTIS_PLAN, session_id="eu19"),
        *otp_adapter.journeys_from_plan(fx.OTP_PLAN, session_id="nap-ch-rail"),
        *hafas_adapter.journeys_from_trip_search(fx.HAFAS_TRIP_SEARCH),
        *ojp_adapter.journeys_from_trip_delivery(fx.OJP_TRIP_DELIVERY),
    ]


def _stop(ref: str = "SBB:8507000:0:7") -> CanonicalLocation:
    return CanonicalLocation(
        kind="stop", name="Bern", stop_place=CanonicalStopPlace(ref=ref, source="otp:SBB")
    )


def _at(hour: int, minute: int = 0) -> CanonicalTiming:
    return CanonicalTiming(timetabled=datetime(2026, 5, 18, hour, minute, tzinfo=UTC))


# ── invariants ─────────────────────────────────────────────────────────────────────────


def test_models_are_frozen_and_refuse_unknown_fields() -> None:
    journey = _all_fixture_journeys()[0]
    with pytest.raises(ValidationError):
        journey.duration_seconds = 1  # type: ignore[misc]
    with pytest.raises(ValidationError):
        CanonicalStopPlace(ref="x", source="otp:SBB", platform="7")  # type: ignore[call-arg]


def test_a_timed_leg_has_a_service_and_a_walk_has_none() -> None:
    service = CanonicalService(mode=TransportMode.RAIL)
    ends = {"origin": _stop(), "destination": _stop(), "duration_seconds": 60}
    with pytest.raises(ValidationError, match="timed leg has a service"):
        CanonicalLeg(kind="timed", **ends)
    with pytest.raises(ValidationError, match="timed leg has a service"):
        CanonicalLeg(kind="continuous", service=service, **ends)


def test_a_stop_location_has_a_stop_place_and_a_coordinate_one_has_none() -> None:
    with pytest.raises(ValidationError):
        CanonicalLocation(kind="stop", name="Bern")
    stop_place = CanonicalStopPlace(ref="x", source="otp:SBB")
    with pytest.raises(ValidationError):
        CanonicalLocation(kind="coordinate", stop_place=stop_place)


def test_times_are_aware_and_stored_in_utc() -> None:
    cest = timezone(timedelta(hours=2))
    timing = CanonicalTiming(timetabled=datetime(2026, 5, 18, 10, 31, tzinfo=cest))
    assert timing.timetabled == datetime(2026, 5, 18, 8, 31, tzinfo=UTC)
    assert timing.timetabled.utcoffset() == timedelta(0)
    naive = datetime(2026, 5, 18, 10, 31)
    with pytest.raises(ValidationError):
        CanonicalTiming(timetabled=naive)  # naive: refused


@pytest.mark.parametrize("uic", ["850700", "85070000", "UIC:8507000", "abcdefg"])
def test_a_uic_is_exactly_seven_digits(uic: str) -> None:
    with pytest.raises(ValidationError):
        CanonicalStopPlace(ref="x", source="otp:SBB", uic=uic)


@pytest.mark.parametrize("station_id", ["8507000", "AT-1234", "at-12345", "AT_12345"])
def test_a_station_id_has_the_station_modules_form(station_id: str) -> None:
    """A station module id is never a code, and a code never passes as an id (design D8)."""
    with pytest.raises(ValidationError):
        CanonicalStopPlace(ref="x", source="otp:SBB", station_id=station_id)
    assert CanonicalStopPlace(ref="x", source="otp:SBB", station_id="AT-01234").station_id


# ── derived values ─────────────────────────────────────────────────────────────────────


def test_derived_values_of_a_journey_with_a_transfer() -> None:
    eurostar = motis_adapter.journeys_from_plan(fx.MOTIS_PLAN)[1]
    assert eurostar.first_boarding == _utc(2026, 6, 20, 7, 0)
    assert eurostar.last_alighting == _utc(2026, 6, 20, 10, 5)
    assert eurostar.transit_duration_seconds == 11100 - 600  # minus the transfer walk
    assert eurostar.transfer_count == 1
    assert eurostar.source_transfer_count is None
    assert eurostar.modes == [TransportMode.RAIL]
    (transfer,) = eurostar.transfers
    assert (transfer.from_leg, transfer.to_leg) == (0, 2)
    assert transfer.walk_seconds == 600
    assert transfer.connection_seconds == 16 * 60  # 09:41 → 09:57
    assert transfer.alight_at.ref == "eurostar_brussels_midi"
    assert transfer.board_at.ref == "nmbs_8814001"


def _utc(*parts: int) -> CanonicalTiming:
    return CanonicalTiming(timetabled=datetime(*parts, tzinfo=UTC))


def test_first_boarding_skips_access_walks() -> None:
    s7 = motis_adapter.journeys_from_plan(fx.MOTIS_PLAN)[0]
    assert s7.departure == _utc(2026, 11, 3, 8, 10)  # leaving the door
    assert s7.first_boarding == _utc(2026, 11, 3, 8, 14)  # boarding the S7


def test_a_journey_needs_a_source_and_an_id() -> None:
    source = JourneySource(provider="otp", role="viator", adapter_version=1)
    with pytest.raises(ValidationError):
        CanonicalJourney(id="", source=source, legs=[], duration_seconds=0)
    with pytest.raises(ValidationError):
        JourneySource(provider="db", role="viator", adapter_version=1)  # type: ignore[arg-type]


# ── round trip and schema ──────────────────────────────────────────────────────────────


def test_every_fixture_journey_reads_back_from_its_json() -> None:
    """Storage is the canonical JSON (design D1): what is written reads back equal, and its
    derived values are computed again rather than stored."""
    for journey in _all_fixture_journeys():
        text = journey.model_dump_json()
        assert "first_boarding" not in text
        assert "fingerprint" not in text
        again = CanonicalJourney.model_validate_json(text)
        assert again == journey
        assert again.fingerprint() == journey.fingerprint()


def test_the_committed_json_schema_matches_the_models() -> None:
    """Regenerate with `python -m app.journey.canonical.schema > docs/schemas/...`."""
    committed = SCHEMA_FILE.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert committed == schema_text()
