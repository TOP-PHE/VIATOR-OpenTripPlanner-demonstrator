"""#303 AC5 — CanonicalJourney → OJP 2.0 (XML, JSON) and → OSDM `Trip` (proof of concept).

The OJP XSD and the OSDM JSON Schema are not in this repository, so these tests pin the shape
and check the round trip through VIATOR's own OJP adapter instead (#306 / #307 validate).
"""

from __future__ import annotations

import pytest
from defusedxml.ElementTree import fromstring

from app.journey.canonical.mappers import iso_duration
from app.journey.canonical.mappers.ojp import (
    OJP_NS,
    SIRI_NS,
    CanonicalJourneyToOjpMapper,
    to_ojp_json,
    to_ojp_xml,
)
from app.journey.canonical.mappers.osdm import (
    CanonicalJourneyToOsdmMapper,
    OsdmMappingError,
    to_osdm_trip,
)
from app.journey.canonical_adapters import hafas_adapter, ojp_adapter
from tests.unit import canonical_fixtures as fx

NS = {"ojp": OJP_NS, "siri": SIRI_NS}


def _ojp():
    return ojp_adapter.journeys_from_trip_delivery(fx.OJP_TRIP_DELIVERY)


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(0, "PT0S"), (59, "PT59S"), (300, "PT5M"), (4140, "PT1H9M"), (3601, "PT1H1S"), (-5, "PT0S")],
)
def test_iso_duration(seconds: int, text: str) -> None:
    assert iso_duration(seconds) == text


def test_ojp_json_tree_of_a_trip() -> None:
    tree = to_ojp_json(_ojp()[0])
    trip = tree["Trip"]
    assert (trip["Duration"], trip["StartTime"], trip["Transfers"]) == (
        "PT1H9M",
        "2026-05-18T08:26:00Z",
        0,
    )
    kinds = [next(key for key in leg if key.endswith("Leg")) for leg in trip["Leg"]]
    assert kinds == ["ContinuousLeg", "TimedLeg", "ContinuousLeg"]
    timed = trip["Leg"][1]["TimedLeg"]
    assert timed["LegBoard"]["siri:StopPointRef"] == "8507000"  # the UIC, not the SLOID
    assert timed["LegBoard"]["ServiceDeparture"]["TimetabledTime"] == "2026-05-18T08:31:00Z"
    assert timed["Service"]["Mode"] == {"PtMode": "rail"}
    assert timed["Service"]["PublicCode"] == "IC1"
    assert timed["Service"]["ProductCategory"]["Name"] == {"Text": "InterCity"}


def test_ojp_xml_is_well_formed_and_namespaced() -> None:
    root = fromstring(to_ojp_xml(_ojp()))
    assert root.tag == f"{{{OJP_NS}}}OJPTripDelivery"
    results = root.findall("ojp:TripResult", NS)
    assert len(results) == 3
    service = results[2].find("ojp:Trip/ojp:Leg/ojp:TimedLeg/ojp:Service", NS)
    assert service.findtext("ojp:TrainNumber", namespaces=NS) == "2515"
    assert service.findtext("siri:OperatorRef", namespaces=NS) == "11"
    assert service.findtext("ojp:PublishedServiceName/ojp:Text", namespaces=NS) == "IR 15"


def test_ojp_round_trip_through_viators_own_ojp_adapter() -> None:
    """canonical → OJP XML → ojp_adapter → canonical keeps the journey's identity."""
    journeys = _ojp()
    again = ojp_adapter.journeys_from_trip_delivery(CanonicalJourneyToOjpMapper().to_xml(journeys))
    assert len(again) == len(journeys)
    for before, after in zip(journeys, again, strict=True):
        assert after.fingerprint().equivalent(before.fingerprint(), tolerance_seconds=0) or (
            not before.timed_legs
        )
        assert [leg.kind for leg in after.legs] == [leg.kind for leg in before.legs]
        assert after.duration_seconds == before.duration_seconds
        for a, b in zip(after.timed_legs, before.timed_legs, strict=True):
            assert a.service.line == b.service.line
            assert a.service.train_number == b.service.train_number


def test_osdm_trip_of_a_journey_whose_stations_have_uics() -> None:
    trip = CanonicalJourneyToOsdmMapper().to_trip(_ojp()[2])  # Bern → Genève, IR 15
    assert (trip["startTime"], trip["endTime"], trip["duration"]) == (
        "2026-05-18T08:34:00Z",
        "2026-05-18T10:20:00Z",
        "PT1H46M",
    )
    (leg,) = trip["legs"]
    timed = leg["timedLeg"]
    assert timed["start"]["stopPlaceRef"]["stopPlaceRef"] == "urn:uic:stn:8507000"
    assert timed["end"]["stopPlaceRef"]["stopPlaceRef"] == "urn:uic:stn:8501008"
    assert timed["start"]["serviceDeparture"] == {"timetabledTime": "2026-05-18T08:34:00Z"}
    assert timed["service"]["mode"] == {"ptMode": "RAIL"}
    assert timed["service"]["vehicleNumbers"] == ["2515"]
    assert timed["service"]["productCategory"] == {"shortName": "IR", "name": "InterRegio"}


def test_osdm_walk_legs_may_lack_a_station_but_rides_may_not() -> None:
    with_walks = to_osdm_trip(_ojp()[0])  # access walk from coordinates
    assert "continuousLeg" in with_walks["legs"][0]
    assert "start" not in with_walks["legs"][0]["continuousLeg"]
    night = hafas_adapter.journeys_from_trip_search(fx.HAFAS_TRIP_SEARCH)[2]  # from a bus terminal
    with pytest.raises(OsdmMappingError, match="no UIC"):
        to_osdm_trip(night)
