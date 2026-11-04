"""#303 — the four adapters fill the canonical fields right.

PR 2 proved, on these fixtures, that `to_trip_dict(adapter(raw))` equalled each client's own
normaliser key for key. Since PR 3a the clients' normalisers ARE the adapter plus that bridge,
so that comparison would only compare the bridge with itself: the clients' own tests
(`test_motis_client.py`, `test_hafas_client.py`, `test_ojp_client.py`,
`test_journey_stop_id_routing.py`) pin the dict values, and these tests pin the new fields.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from app.journey.canonical import CanonicalJourney, TransportMode, mode_family
from app.journey.canonical_adapters import hafas_adapter, motis_adapter, ojp_adapter, otp_adapter
from app.network_coverage import external_verify
from tests.unit import canonical_fixtures as fx

# ── MOTIS ──────────────────────────────────────────────────────────────────────────────


def _motis() -> list[CanonicalJourney]:
    return motis_adapter.journeys_from_plan(fx.MOTIS_PLAN, session_id="eu19")


def test_motis_sub_modes_fold_into_the_rail_family_and_keep_their_spelling() -> None:
    s7 = _motis()[0].legs[1]
    assert s7.service is not None
    assert s7.service.mode is TransportMode.RAIL
    assert s7.source_mode == "REGIONAL_RAIL"
    assert _motis()[1].legs[0].service.mode is TransportMode.RAIL  # HIGHSPEED_RAIL


def test_motis_stop_identity_platform_parent_and_uic() -> None:
    s7 = _motis()[0].legs[1]
    graz = s7.origin.stop_place
    assert graz is not None
    assert graz.ref == "styria_at:46:4012:0:3"
    assert graz.parent_ref == "styria_at:46:4012"
    assert graz.source == "motis:styria"
    assert graz.uic is None  # Styria's NeTEx carries no UIC: the case station resolution is for
    eurostar = _motis()[1].legs[0]
    assert eurostar.origin.stop_place.uic == "8400058"  # from stopCode
    assert eurostar.origin.stop_place.codes == {"GTFS_STOP_CODE": "8400058"}
    assert _motis()[1].legs[2].origin.stop_place.uic == "8814001"  # from the id


def test_motis_service_and_operator() -> None:
    s7 = _motis()[0].legs[1].service
    assert s7 is not None
    assert (s7.line, s7.published_name, s7.feed) == ("S7", "S7", "styria")
    assert s7.operator is not None
    assert s7.operator.name == "GKB"
    eurostar = _motis()[1].legs[0].service
    assert eurostar.operator.name == "EUROSTAR"  # the DELFI code prefix is removed
    assert eurostar.train_number is None  # MOTIS gives none the client reads


def test_motis_leg_kinds_and_the_walk_only_journey() -> None:
    first, eurostar, walk_only = _motis()
    assert [leg.kind for leg in first.legs] == ["continuous", "timed"]
    assert first.legs[0].origin.kind == "coordinate"
    assert [leg.kind for leg in eurostar.legs] == ["timed", "transfer", "timed"]
    assert eurostar.legs[1].continuous_mode == "WALK"
    assert eurostar.legs[1].source_mode == "TRANSFER"
    assert eurostar.legs[0].distance_meters is None  # MOTIS gives no distance
    assert walk_only.first_boarding is None
    assert walk_only.transit_duration_seconds is None
    assert walk_only.fingerprint().spine_key == ""


# ── OTP ────────────────────────────────────────────────────────────────────────────────


def test_otp_times_from_epoch_milliseconds_are_utc() -> None:
    journey = otp_adapter.journeys_from_plan(fx.OTP_PLAN)[0]
    assert journey.departure.timetabled == datetime(2026, 5, 18, 8, 31, tzinfo=UTC)
    assert journey.legs[1].departure.timetabled == datetime(2026, 5, 18, 8, 34, tzinfo=UTC)


def test_otp_stops_service_and_kinds() -> None:
    direct, change = otp_adapter.journeys_from_plan(fx.OTP_PLAN, session_id="nap-ch-rail")
    bern = direct.legs[1].origin.stop_place
    assert (bern.ref, bern.source, bern.uic) == ("SBB:8507000:0:7", "otp:SBB", "8507000")
    ir15 = direct.legs[1].service
    assert (ir15.line, ir15.feed, ir15.operator.ref) == ("IR15", "SBB", "SBB:11")
    assert direct.legs[0].kind == "continuous"
    assert direct.legs[0].distance_meters == pytest.approx(212.4)
    assert [leg.kind for leg in change.legs] == ["timed", "transfer", "timed"]
    assert change.source.session_id == "nap-ch-rail"
    assert change.transfer_count == 1


# ── HAFAS ──────────────────────────────────────────────────────────────────────────────


def _hafas() -> list[CanonicalJourney]:
    return hafas_adapter.journeys_from_trip_search(fx.HAFAS_TRIP_SEARCH)


def test_hafas_vienna_local_times_become_utc_once() -> None:
    rj, s7, night = _hafas()
    assert rj.departure.timetabled == datetime(2026, 6, 28, 6, 0, tzinfo=UTC)  # CEST
    assert s7.legs[1].departure.timetabled == datetime(2026, 11, 3, 8, 14, tzinfo=UTC)  # CET
    # Day-roll prefix: 02:00 Vienna on the 4th = 01:00 UTC.
    assert night.arrival.timetabled == datetime(2026, 11, 4, 1, 0, tzinfo=UTC)


def test_hafas_station_identity_from_ext_id_never_from_the_lid_coordinates() -> None:
    rj, s7, night = _hafas()
    assert rj.legs[0].origin.stop_place.uic == "8103000"
    graz = s7.legs[1].origin.stop_place
    assert (graz.uic, graz.codes) == ("8100173", {"OEBB_EXT": "8100173"})
    terminal = night.legs[0].origin.stop_place
    assert terminal.uic is None
    assert terminal.unresolved_token == "OEBB:904050"


def test_hafas_service_keeps_label_line_category_and_train_number_apart() -> None:
    rj = _hafas()[0].legs[0].service
    assert (rj.published_name, rj.line, rj.train_number) == ("RJ 1141", "RJ", "1141")
    assert rj.product_category.code == "RJ"
    assert rj.operator.name == "ÖBB-Personenverkehr AG"
    s7 = _hafas()[1].legs[1].service
    assert (s7.published_name, s7.line, s7.train_number) == ("S 7", "S 7", "4512")
    assert s7.mode is TransportMode.RAIL
    assert s7.destination_text == "Köflach Bahnhof"
    bus = _hafas()[2].legs[0].service
    assert bus.mode is TransportMode.BUS
    assert bus.train_number is None


def test_hafas_sections_walk_transfer_unknown_and_dropped_connections() -> None:
    journeys = _hafas()
    assert len(journeys) == 3  # the connection without a date is dropped
    s7 = journeys[1]
    assert [leg.kind for leg in s7.legs] == ["continuous", "timed", "continuous"]
    assert s7.legs[0].distance_meters == 410.0
    assert s7.legs[0].duration_seconds == 240
    unknown = s7.legs[2]
    assert (unknown.source_mode, unknown.continuous_mode) == (None, None)
    assert [leg.kind for leg in journeys[2].legs] == ["timed", "transfer", "timed"]
    assert journeys[2].source_transfer_count == 1
    assert all(j.source.role == "reference" for j in journeys)


def test_hafas_numeric_product_category_does_not_break_the_adapter() -> None:
    payload = copy.deepcopy(fx.HAFAS_TRIP_SEARCH)
    payload["svcResL"][0]["res"]["common"]["prodL"][0]["prodCtx"]["catOut"] = 7
    leg = hafas_adapter.journeys_from_trip_search(payload)[0].legs[0]
    assert leg.source_mode == "7"
    assert leg.service.mode is TransportMode.OTHER


def test_hafas_adapter_agrees_with_coverages_verify_itinerary() -> None:
    """One HAFAS adapter for /journey and coverage (design §5.3): what coverage's
    `VerifyLeg` says, the canonical leg says too, its naive Vienna time included."""
    vienna = ZoneInfo("Europe/Vienna")
    verified = external_verify._parse_hafas_response(fx.HAFAS_TRIP_SEARCH).itineraries
    # Coverage keeps a connection without a date as an empty itinerary; the adapter drops it,
    # like the /journey client. A difference PR 4 settles (HAFAS always sends a date).
    assert [len(it.legs) for it in verified] == [1, 3, 3, 0]
    for journey, itinerary in zip(_hafas(), verified[:3], strict=True):
        for leg, verify_leg in zip(journey.legs, itinerary.legs, strict=True):
            # Coverage calls an unknown section TRANSIT; the canonical leg has no mode.
            assert (leg.source_mode or "TRANSIT") == verify_leg.mode
            ends = ((leg.origin, verify_leg.from_uic), (leg.destination, verify_leg.to_uic))
            for end, token in ends:
                stop = end.stop_place
                assert (f"UIC:{stop.uic}" if stop.uic else stop.unresolved_token) == token
            local = leg.departure.timetabled.astimezone(vienna).replace(tzinfo=None)
            assert local == datetime.fromisoformat(verify_leg.dep_utc)
            if leg.service is not None:
                assert verify_leg.route_name == (leg.service.published_name or leg.service.line)


def test_hafas_empty_or_malformed_payloads_give_no_journey() -> None:
    assert hafas_adapter.journeys_from_trip_search({}) == []
    assert hafas_adapter.journeys_from_trip_search({"svcResL": [{"res": {}}]}) == []


# ── OJP ────────────────────────────────────────────────────────────────────────────────


def _ojp() -> list[CanonicalJourney]:
    return ojp_adapter.journeys_from_trip_delivery(fx.OJP_TRIP_DELIVERY)


def test_ojp_sloid_and_bare_numbers_give_uics() -> None:
    first, aarau, ir15 = _ojp()
    bern = first.legs[1].origin.stop_place
    assert (bern.uic, bern.codes) == ("8507000", {"SLOID": "ch:1:sloid:7000:4:8"})
    assert aarau.legs[0].origin.stop_place.uic == "8502113"
    assert ir15.legs[0].destination.stop_place.uic == "8501008"


def test_ojp_leg_kinds_follow_the_elements() -> None:
    first, aarau, _ = _ojp()
    assert [leg.kind for leg in first.legs] == ["continuous", "timed", "continuous"]
    assert first.legs[0].departure is None  # OJP gives no time on its walk legs
    assert first.legs[0].distance_meters == 221.0
    assert first.legs[0].origin.kind == "coordinate"
    assert aarau.legs[0].kind == "transfer"


def test_ojp_service_fields() -> None:
    ic1 = _ojp()[0].legs[1].service
    assert (ic1.line, ic1.published_name, ic1.train_number) == ("IC1", "IC1", None)
    assert ic1.product_category.name == "InterCity"
    assert ic1.product_category.code == "IC"  # from Mode/ShortName
    assert ic1.operator.ref == "11"
    ir15 = _ojp()[2].legs[0].service
    assert (ir15.line, ir15.published_name, ir15.train_number) == ("IR15", "IR 15", "2515")
    assert ir15.product_category.code == "IR"


def test_ojp_unsafe_or_broken_xml_gives_no_journey() -> None:
    assert ojp_adapter.journeys_from_trip_delivery("<not-xml") == []
    entity = '<!DOCTYPE x [<!ENTITY e "boom">]><OJP xmlns="http://www.vdv.de/ojp">&e;</OJP>'
    assert ojp_adapter.journeys_from_trip_delivery(entity) == []


# ── modes ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("source_mode", "family"),
    [
        ("RAIL", TransportMode.RAIL),
        ("HIGHSPEED_RAIL", TransportMode.RAIL),
        ("REGIONAL_FAST_RAIL", TransportMode.RAIL),
        ("LONG_DISTANCE", TransportMode.RAIL),
        ("SUBURBAN", TransportMode.RAIL),
        ("urbanRail", TransportMode.RAIL),
        ("TROLLEYBUS", TransportMode.BUS),
        ("coach", TransportMode.COACH),
        ("STREETCAR", TransportMode.TRAM),
        ("METRO", TransportMode.SUBWAY),
        ("UNDERGROUND", TransportMode.SUBWAY),
        ("WATER", TransportMode.FERRY),
        ("GONDOLA", TransportMode.CABLEWAY),
        ("AERIAL_LIFT", TransportMode.CABLEWAY),
        ("FUNICULAR", TransportMode.FUNICULAR),
        ("AIRPLANE", TransportMode.AIR),
        ("TRANSIT", TransportMode.OTHER),
        ("", TransportMode.OTHER),
        (None, TransportMode.OTHER),
    ],
)
def test_mode_family(source_mode: str | None, family: TransportMode) -> None:
    assert mode_family(source_mode) is family
