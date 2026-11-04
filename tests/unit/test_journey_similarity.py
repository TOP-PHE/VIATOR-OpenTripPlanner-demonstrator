"""#303 PR 2 — the matching structures (behaviour is #304's) and station resolution."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import pytest
from pydantic import ValidationError

from app.journey.canonical import (
    CanonicalStopPlace,
    ComparisonMetadata,
    JourneyMatchFeatures,
    JourneySimilarityResult,
    MatchReason,
)
from app.journey.canonical.station_resolution import (
    NoStationResolver,
    ResolvedStation,
    StopKey,
    stop_places,
    with_resolved_stations,
)
from app.journey.canonical_adapters import hafas_adapter, motis_adapter
from tests.unit import canonical_fixtures as fx


def test_features_default_to_no_evidence_and_bound_their_shares() -> None:
    features = JourneyMatchFeatures()
    assert features.station_overlap is None and features.departure_delta is None
    for name in ("station_overlap", "operator_overlap", "service_overlap", "route_overlap"):
        with pytest.raises(ValidationError):
            JourneyMatchFeatures(**{name: 1.5})


def test_a_similarity_result_and_its_reasons() -> None:
    reason = MatchReason(
        code="line_label_differs", severity="warning", leg=0, left_value="17", right_value="1"
    )
    result = JourneySimilarityResult(
        left_id="motis:eu19:1",
        right_id="hafas:oebb:4",
        verdict="probably_same",
        score=0.9,
        features=JourneyMatchFeatures(departure_delta=0, fingerprint_similarity=1.0),
        reasons=[reason],
    )
    assert result.reasons[0].severity == "warning"
    with pytest.raises(ValidationError):
        JourneySimilarityResult(
            left_id="a",
            right_id="b",
            verdict="maybe",
            score=0.5,
            features=JourneyMatchFeatures(),  # type: ignore[arg-type]
        )
    with pytest.raises(ValidationError):
        MatchReason(code="", severity="info")


def test_comparison_metadata_buckets() -> None:
    assert ComparisonMetadata(bucket="left_only").paired_with is None
    with pytest.raises(ValidationError):
        ComparisonMetadata(bucket="otp_only")  # type: ignore[arg-type]


class _FakeModule:
    """Places the MOTIS Graz platform and the ÖBB Graz station on one MSMM station."""

    def __init__(self) -> None:
        self.asked: list[StopKey] = []

    async def resolve(
        self, stops: Sequence[CanonicalStopPlace]
    ) -> Mapping[StopKey, ResolvedStation]:
        self.asked = [(stop.source, stop.ref) for stop in stops]
        graz = ResolvedStation(station_id="AT-00173", parent_id=None)
        return {
            ("motis:styria", "styria_at:46:4012:0:3"): graz,
            ("hafas:oebb", "A=1@O=Graz Hbf@X=15417520@Y=47072054@U=81@L=8100173@"): graz,
        }


async def test_with_resolved_stations_fills_station_ids_and_only_those() -> None:
    journeys = [
        motis_adapter.journeys_from_plan(fx.MOTIS_PLAN)[0],
        hafas_adapter.journeys_from_trip_search(fx.HAFAS_TRIP_SEARCH)[1],
    ]
    module = _FakeModule()
    resolved = await with_resolved_stations(journeys, module)
    assert len(module.asked) == len(set(module.asked))  # each stop asked once
    motis_graz = resolved[0].legs[1].origin.stop_place
    hafas_graz = resolved[1].legs[1].origin.stop_place
    assert motis_graz.station_id == hafas_graz.station_id == "AT-00173"
    assert resolved[0].legs[1].destination.stop_place.station_id is None  # Köflach: not placed
    assert motis_graz.uic is None  # a station id never becomes a code
    assert journeys[0].legs[1].origin.stop_place.station_id is None  # the input is untouched
    # A station id is not a fingerprint input (design D8).
    assert resolved[0].fingerprint() == journeys[0].fingerprint()


async def test_no_station_resolver_changes_nothing() -> None:
    journeys = motis_adapter.journeys_from_plan(fx.MOTIS_PLAN)
    assert await with_resolved_stations(journeys, NoStationResolver()) == journeys


def test_stop_places_are_listed_once_in_order() -> None:
    eurostar = motis_adapter.journeys_from_plan(fx.MOTIS_PLAN)[1]
    refs = [stop.ref for stop in stop_places([eurostar, eurostar])]
    assert refs == [
        "eurostar_amsterdam_centraal",
        "eurostar_brussels_midi",
        "nmbs_8814001",
        "nmbs_8812005",
    ]


def test_a_resolved_station_is_a_station_module_id_never_a_code() -> None:
    with pytest.raises(ValidationError):
        ResolvedStation(station_id="8100173")
