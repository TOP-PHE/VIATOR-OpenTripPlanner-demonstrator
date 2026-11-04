"""#303 AC6 — `JourneyFingerprint`: equivalent journeys agree although their providers, ids,
train numbers and minutes (within ±2 min) differ; different journeys do not."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.journey.canonical import (
    CanonicalJourney,
    CanonicalLeg,
    CanonicalLocation,
    CanonicalService,
    CanonicalStopPlace,
    CanonicalTiming,
    GeoPosition,
    JourneySource,
    TransportMode,
    station_key,
)
from app.journey.canonical.canonical_journey import Provider
from app.journey.canonical.journey_fingerprint import NO_STATION
from app.journey.canonical_adapters import hafas_adapter, motis_adapter
from tests.unit import canonical_fixtures as fx

T0 = datetime(2026, 6, 28, 6, 0, tzinfo=UTC)


def _stop(uic: str | None, source: str, ref: str, name: str = "") -> CanonicalLocation:
    return CanonicalLocation(
        kind="stop", name=name, stop_place=CanonicalStopPlace(ref=ref, source=source, uic=uic)
    )


def _journey(
    provider: Provider,
    rides: list[tuple[str, str, int, int, str]],
    *,
    walk_first: bool = False,
    mode: TransportMode = TransportMode.RAIL,
) -> CanonicalJourney:
    """`rides`: (board UIC, alight UIC, board minute, alight minute, label) from T0."""
    source = f"{provider}:x"
    legs: list[CanonicalLeg] = []
    if walk_first:
        legs.append(
            CanonicalLeg(
                kind="continuous",
                origin=CanonicalLocation(
                    kind="coordinate", position=GeoPosition(lat=48.2, lon=16.37)
                ),
                destination=_stop(rides[0][0], source, f"{provider}-{rides[0][0]}"),
                departure=CanonicalTiming(timetabled=T0 + timedelta(minutes=rides[0][2] - 5)),
                arrival=CanonicalTiming(timetabled=T0 + timedelta(minutes=rides[0][2])),
                duration_seconds=300,
                continuous_mode="WALK",
            )
        )
    for board, alight, dep, arr, label in rides:
        legs.append(
            CanonicalLeg(
                kind="timed",
                origin=_stop(board, source, f"{provider}-{board}"),
                destination=_stop(alight, source, f"{provider}-{alight}"),
                departure=CanonicalTiming(timetabled=T0 + timedelta(minutes=dep)),
                arrival=CanonicalTiming(timetabled=T0 + timedelta(minutes=arr)),
                duration_seconds=(arr - dep) * 60,
                service=CanonicalService(mode=mode, published_name=label, train_number=label[-4:]),
            )
        )
    return CanonicalJourney(
        id=f"{provider}:x:0",
        source=JourneySource(provider=provider, role="viator", adapter_version=1),
        legs=legs,
        duration_seconds=sum(leg.duration_seconds for leg in legs),
    )


WIEN, SALZBURG, LINZ = "8103000", "8100002", "8100013"


def test_equivalent_despite_provider_ids_train_numbers_and_two_minutes() -> None:
    motis = _journey("motis", [(WIEN, SALZBURG, 0, 165, "RJ 1141")])
    hafas = _journey("hafas", [(WIEN, SALZBURG, 2, 163, "Railjet 9141")])
    assert motis.fingerprint().spine_key == hafas.fingerprint().spine_key
    assert motis.fingerprint().equivalent(hafas.fingerprint())
    assert motis.fingerprint().strict_key != hafas.fingerprint().strict_key


def test_three_minutes_apart_is_not_equivalent_unless_the_tolerance_says_so() -> None:
    a = _journey("motis", [(WIEN, SALZBURG, 0, 165, "RJ 1141")])
    b = _journey("hafas", [(WIEN, SALZBURG, 3, 165, "RJ 1141")])
    assert not a.fingerprint().equivalent(b.fingerprint())
    assert a.fingerprint().equivalent(b.fingerprint(), tolerance_seconds=180)


def test_access_walks_do_not_count() -> None:
    a = _journey("ojp", [(WIEN, SALZBURG, 0, 165, "RJ 1141")], walk_first=True)
    b = _journey("otp", [(WIEN, SALZBURG, 0, 165, "RJ 1141")])
    assert a.fingerprint() == b.fingerprint()


def test_different_stations_or_modes_or_leg_counts_are_not_equivalent() -> None:
    direct = _journey("motis", [(WIEN, SALZBURG, 0, 165, "RJ 1141")])
    via_linz = _journey(
        "hafas", [(WIEN, LINZ, 0, 75, "RJ 1141"), (LINZ, SALZBURG, 80, 165, "RJ 1141")]
    )
    bus = _journey("hafas", [(WIEN, SALZBURG, 0, 165, "Bus")], mode=TransportMode.BUS)
    for other in (via_linz, bus):
        assert not direct.fingerprint().equivalent(other.fingerprint())
        assert direct.fingerprint().spine_key != other.fingerprint().spine_key


def test_motis_regional_rail_and_hafas_rail_share_a_spine() -> None:
    """The fold the old `transit_fingerprint` lacked: MOTIS `REGIONAL_RAIL` vs ÖBB `RAIL`."""
    motis = motis_adapter.journeys_from_plan(fx.MOTIS_PLAN)[0]
    hafas = hafas_adapter.journeys_from_trip_search(fx.HAFAS_TRIP_SEARCH)[1]
    assert (
        motis.fingerprint().spine[0].mode is hafas.fingerprint().spine[0].mode is TransportMode.RAIL
    )


def test_similarity() -> None:
    a = _journey("motis", [(WIEN, LINZ, 0, 75, "x"), (LINZ, SALZBURG, 80, 165, "x")])
    same = _journey("hafas", [(WIEN, LINZ, 0, 75, "y"), (LINZ, SALZBURG, 80, 165, "y")])
    one_minute = _journey("hafas", [(WIEN, LINZ, 1, 76, "y"), (LINZ, SALZBURG, 81, 166, "y")])
    half = _journey("hafas", [(WIEN, LINZ, 0, 75, "y")])
    assert a.fingerprint().similarity(same.fingerprint()) == 1.0
    assert a.fingerprint().similarity(one_minute.fingerprint()) == 0.5
    assert a.fingerprint().similarity(half.fingerprint()) == 0.5
    assert a.fingerprint().similarity(one_minute.fingerprint(), tolerance_seconds=0) == 0.0


def test_station_keys() -> None:
    uic = _stop("8507000", "otp:SBB", "SBB:8507000:0:7")
    assert station_key(uic) == "UIC:8507000"
    geo = CanonicalLocation(
        kind="stop",
        stop_place=CanonicalStopPlace(ref="styria_at:46:4012:0:3", source="motis:styria"),
        position=GeoPosition(lat=47.07205, lon=15.41753),
    )
    assert station_key(geo) == "GEO:47.072,15.418"
    oebb = CanonicalLocation(
        kind="stop",
        stop_place=CanonicalStopPlace(
            ref="lid", source="hafas:oebb", unresolved_token="OEBB:904050"
        ),
        position=GeoPosition(lat=48.1846, lon=16.3761),
    )
    # An ÖBB station outside the UIC namespace never falls back to its coordinates.
    assert station_key(oebb) == "UNRESOLVED:hafas:oebb:OEBB:904050"
    bare = CanonicalLocation(kind="stop", stop_place=CanonicalStopPlace(ref="r", source="otp:X"))
    assert station_key(bare) == "UNRESOLVED:otp:X:r"
    assert station_key(CanonicalLocation(kind="coordinate")) == NO_STATION


def _no_station_journey(provider: Provider) -> CanonicalJourney:
    nowhere = CanonicalLocation(kind="coordinate")
    leg = CanonicalLeg(
        kind="timed",
        origin=nowhere,
        destination=nowhere,
        departure=CanonicalTiming(timetabled=T0),
        arrival=CanonicalTiming(timetabled=T0 + timedelta(minutes=10)),
        duration_seconds=600,
        service=CanonicalService(mode=TransportMode.RAIL),
    )
    return CanonicalJourney(
        id=f"{provider}:x:0",
        source=JourneySource(provider=provider, role="reference", adapter_version=1),
        legs=[leg],
        duration_seconds=600,
    )


def test_journeys_without_any_station_identity_never_agree() -> None:
    """The `OEBB:?` sentinel's property (`test_two_id_less_itineraries_do_not_exact_match`):
    no evidence on both sides is not a match."""
    a, b = _no_station_journey("hafas"), _no_station_journey("motis")
    assert not a.fingerprint().equivalent(b.fingerprint())
    assert a.fingerprint().similarity(b.fingerprint()) == 0.0


def test_a_walk_only_journey_has_an_empty_fingerprint() -> None:
    walk_only = motis_adapter.journeys_from_plan(fx.MOTIS_PLAN)[2]
    fingerprint = walk_only.fingerprint()
    assert (fingerprint.spine_key, fingerprint.strict_key) == ("", "")
    assert not fingerprint.equivalent(fingerprint)
    assert fingerprint.algorithm_version == 1


@pytest.mark.parametrize("tolerance", [0, 60, 120])
def test_a_journey_is_equivalent_to_itself(tolerance: int) -> None:
    journey = hafas_adapter.journeys_from_trip_search(fx.HAFAS_TRIP_SEARCH)[0]
    assert journey.fingerprint().equivalent(journey.fingerprint(), tolerance_seconds=tolerance)
