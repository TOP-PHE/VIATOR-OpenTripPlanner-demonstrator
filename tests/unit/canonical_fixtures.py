"""Raw engine answers for the CanonicalJourney tests (#303), in the engines' real shapes.

Shapes come from the existing client tests (each pinned against a captured answer: Renfe AVE
spike for MOTIS, `fahrplan.oebb.at` TripSearch for HAFAS, opentransportdata.swiss OJP 2.0) and
from the cases measured on eu19 (#304 comment of 2026-11-03: Graz Hbf → Köflach on S7 / "S 7").
HAFAS lids carry their coordinates first, as live ones do: a stripped `A=1@L=…@` lid is what hid
the `extract_uic` bug.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

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
)
from app.journey.canonical.canonical_journey import Provider


def epoch_ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


_GRAZ = {
    "name": "Graz Hauptbahnhof",
    "lat": 47.07205,
    "lon": 15.41753,
    "stopId": "styria_at:46:4012:0:3",
    "parentId": "styria_at:46:4012",
}
_KOEFLACH = {
    "name": "Köflach Bahnhof",
    "lat": 47.06372,
    "lon": 15.08519,
    "stopId": "styria_at:46:2163:0:1",
    "parentId": "styria_at:46:2163",
}
_AMSTERDAM = {
    "name": "Amsterdam Centraal",
    "lat": 52.37888,
    "lon": 4.90054,
    "stopId": "eurostar_amsterdam_centraal",
    "stopCode": "8400058",
}
_MIDI_EUROSTAR = {
    "name": "Brussels-Midi",
    "lat": 50.83585,
    "lon": 4.33527,
    "stopId": "eurostar_brussels_midi",
    "stopCode": "8814001",
}
_MIDI_NMBS = {"name": "Bruxelles-Midi", "lat": 50.83565, "lon": 4.33600, "stopId": "nmbs_8814001"}
_NORD_NMBS = {"name": "Bruxelles-Nord", "lat": 50.86031, "lon": 4.36162, "stopId": "nmbs_8812005"}

MOTIS_PLAN: dict[str, Any] = {
    "itineraries": [
        {
            # eu19, Styria NeTEx: a walk from typed coordinates, then the S7.
            "startTime": "2026-11-03T08:10:00Z",
            "endTime": "2026-11-03T09:11:00Z",
            "duration": 3660,
            "transfers": 0,
            "legs": [
                {
                    "mode": "WALK",
                    "startTime": "2026-11-03T08:10:00Z",
                    "endTime": "2026-11-03T08:14:00Z",
                    "duration": 240,
                    "from": {"name": "START", "lat": 47.0725, "lon": 15.4162},
                    "to": _GRAZ,
                },
                {
                    "mode": "REGIONAL_RAIL",
                    "startTime": "2026-11-03T08:14:00Z",
                    "endTime": "2026-11-03T09:11:00Z",
                    "duration": 3420,
                    "from": _GRAZ,
                    "to": _KOEFLACH,
                    "routeShortName": "S7",
                    "routeLongName": "Graz Hbf - Köflach",
                    "routeId": "styria_at-46-S7",
                    "agencyId": "styria_gkb",
                    "agencyName": "GKB",
                    "headsign": "Köflach Bahnhof",
                    "tripId": "styria_4512",
                },
            ],
        },
        {
            # Eurostar (UIC in stopCode), a transfer, an NMBS train; no `transfers` given.
            "startTime": "2026-06-20T07:00:00Z",
            "endTime": "2026-06-20T10:05:00Z",
            "duration": 11100,
            "legs": [
                {
                    "mode": "HIGHSPEED_RAIL",
                    "startTime": "2026-06-20T07:00:00Z",
                    "endTime": "2026-06-20T09:41:00Z",
                    "duration": 9660,
                    "from": _AMSTERDAM,
                    "to": _MIDI_EUROSTAR,
                    "routeShortName": "EST",
                    "agencyId": "eurostar_es",
                    "agencyName": "3018___ EUROSTAR",
                    "agencyUrl": "https://www.eurostar.com",
                    "headsign": "Brussels-Midi",
                    "tripId": "eurostar_9322",
                },
                {
                    "mode": "TRANSFER",
                    "startTime": "2026-06-20T09:41:00Z",
                    "endTime": "2026-06-20T09:51:00Z",
                    "duration": 600,
                    "from": _MIDI_EUROSTAR,
                    "to": _MIDI_NMBS,
                },
                {
                    "mode": "REGIONAL_RAIL",
                    "startTime": "2026-06-20T09:57:00Z",
                    "endTime": "2026-06-20T10:05:00Z",
                    "duration": 480,
                    "from": _MIDI_NMBS,
                    "to": _NORD_NMBS,
                    "routeShortName": "IC",
                    "agencyName": "NMBS/SNCB",
                    "tripId": "nmbs_IC_2512",
                },
            ],
        },
        {
            # Walk only (short urban pair).
            "startTime": "2026-06-20T07:00:00Z",
            "endTime": "2026-06-20T07:12:00Z",
            "duration": 720,
            "transfers": 0,
            "legs": [
                {
                    "mode": "WALK",
                    "startTime": "2026-06-20T07:00:00Z",
                    "endTime": "2026-06-20T07:12:00Z",
                    "duration": 720,
                    "from": {"name": "START", "lat": 50.8466, "lon": 4.3528},
                    "to": {"name": "END", "lat": 50.8503, "lon": 4.3517},
                }
            ],
        },
    ]
}

_BERN = {"name": "Bern", "lat": 46.94883, "lon": 7.43913, "stop": {"gtfsId": "SBB:8507000:0:7"}}
_LAUSANNE = {
    "name": "Lausanne",
    "lat": 46.51679,
    "lon": 6.62909,
    "stop": {"gtfsId": "SBB:8501120:0:5"},
}
_LAUSANNE_4 = {
    "name": "Lausanne",
    "lat": 46.51706,
    "lon": 6.62960,
    "stop": {"gtfsId": "SBB:8501120:0:4"},
}
_GENEVE = {"name": "Genève", "lat": 46.21021, "lon": 6.14243, "stop": {"gtfsId": "SBB:8501008:0:3"}}


def _otp_leg(
    mode: str,
    start: str,
    end: str,
    origin: dict[str, Any],
    destination: dict[str, Any],
    **extra: Any,
) -> dict[str, Any]:
    leg = {
        "mode": mode,
        "startTime": epoch_ms(start),
        "endTime": epoch_ms(end),
        "duration": (epoch_ms(end) - epoch_ms(start)) / 1000,
        "from": origin,
        "to": destination,
    }
    leg.update(extra)
    return leg


_IR15 = {
    "route": {
        "shortName": "IR15",
        "longName": "Luzern - Genève-Aéroport",
        "gtfsId": "SBB:91-15-Y-j26-1",
        "agency": {
            "name": "Schweizerische Bundesbahnen SBB",
            "gtfsId": "SBB:11",
            "url": "https://www.sbb.ch",
        },
    },
    "trip": {"gtfsId": "SBB:2515.TA.91-15-Y-j26-1.1.H", "tripHeadsign": "Genève-Aéroport"},
}

OTP_PLAN: dict[str, Any] = {
    "data": {
        "planConnection": {
            "edges": [
                {
                    "node": {
                        # Bern → Genève, IR15 direct, after a walk from coordinates.
                        "start": "2026-05-18T10:31:00+02:00",
                        "end": "2026-05-18T12:20:00+02:00",
                        "duration": 6540,
                        "legs": [
                            _otp_leg(
                                "WALK",
                                "2026-05-18T08:31:00+00:00",
                                "2026-05-18T08:34:00+00:00",
                                {"name": "Origin", "lat": 46.9490, "lon": 7.4395, "stop": None},
                                _BERN,
                                distance=212.4,
                            ),
                            _otp_leg(
                                "RAIL",
                                "2026-05-18T08:34:00+00:00",
                                "2026-05-18T10:20:00+00:00",
                                _BERN,
                                _GENEVE,
                                distance=159000.0,
                                **_IR15,
                            ),
                        ],
                    }
                },
                {
                    "node": {
                        # Bern → Lausanne (IC1), platform change, Lausanne → Genève (IR90).
                        "start": "2026-05-18T10:04:00+02:00",
                        "end": "2026-05-18T11:50:00+02:00",
                        "duration": 6360,
                        "legs": [
                            _otp_leg(
                                "RAIL",
                                "2026-05-18T08:04:00+00:00",
                                "2026-05-18T09:10:00+00:00",
                                _BERN,
                                _LAUSANNE,
                                route={
                                    "shortName": "IC1",
                                    "gtfsId": "SBB:91-1-A",
                                    "agency": {"name": "SBB"},
                                },
                                trip={"gtfsId": "SBB:713.TA", "tripHeadsign": "Genève-Aéroport"},
                            ),
                            _otp_leg(
                                "WALK",
                                "2026-05-18T09:10:00+00:00",
                                "2026-05-18T09:14:00+00:00",
                                _LAUSANNE,
                                _LAUSANNE_4,
                                distance=60.0,
                            ),
                            _otp_leg(
                                "RAIL",
                                "2026-05-18T09:18:00+00:00",
                                "2026-05-18T09:50:00+00:00",
                                _LAUSANNE_4,
                                _GENEVE,
                                route={
                                    "shortName": "IR90",
                                    "gtfsId": "SBB:91-90-A",
                                    "agency": {"name": "SBB"},
                                },
                                trip={"gtfsId": "SBB:1717.TA", "tripHeadsign": "Genève"},
                            ),
                        ],
                    }
                },
            ]
        }
    }
}


def _hafas_payload(connections: list[dict[str, Any]], common: dict[str, Any]) -> dict[str, Any]:
    return {
        "err": "OK",
        "svcResL": [
            {"meth": "TripSearch", "err": "OK", "res": {"common": common, "outConL": connections}}
        ],
    }


HAFAS_COMMON: dict[str, Any] = {
    "locL": [
        {
            "name": "Wien Hbf",
            "lid": "A=1@O=Wien Hbf@X=16375400@Y=48185900@U=81@L=8103000@",
            "extId": "8103000",
            "crd": {"x": 16_375_400, "y": 48_185_900},
        },
        {
            "name": "Salzburg Hbf",
            "lid": "A=1@O=Salzburg Hbf@X=13046700@Y=47812700@U=81@L=8100002@",
            "extId": "8100002",
            "crd": {"x": 13_046_700, "y": 47_812_700},
        },
        {
            "name": "Graz Hbf",
            "lid": "A=1@O=Graz Hbf@X=15417520@Y=47072054@U=81@L=8100173@",
            "extId": "8100173",
            "crd": {"x": 15_417_520, "y": 47_072_054},
        },
        {
            "name": "Köflach Bahnhof",
            "lid": "A=1@O=Köflach Bahnhof@X=15085190@Y=47063720@U=81@L=8101134@",
            "extId": "8101134",
            "crd": {"x": 15_085_190, "y": 47_063_720},
        },
        {
            # Wien Hbf's coordinates snap to its bus terminal: a 6-digit ÖBB id, no UIC.
            "name": "Wien Hbf (Busbahnhof Regional)",
            "lid": "A=1@O=Wien Hbf (Busbahnhof Regional)@X=16376100@Y=48184600@U=81@L=904050@",
            "extId": "904050",
            "crd": {"x": 16_376_100, "y": 48_184_600},
        },
        {
            "name": "Graz Köflacherbahnhof",
            "lid": "A=1@O=Graz Köflacherbahnhof@X=15421180@Y=47067400@U=81@L=8100174@",
            "extId": "8100174",
            "crd": {"x": 15_421_180, "y": 47_067_400},
        },
    ],
    "prodL": [
        {"name": "RJ 1141", "addName": "RJ", "prodCtx": {"catOut": "RJ", "num": "1141"}, "oprX": 0},
        {"name": "S 7", "nameS": "S 7", "prodCtx": {"catOut": "S", "num": "4512"}, "oprX": 1},
        {"name": "Bus 34E", "prodCtx": {"catOut": "Bus"}},
    ],
    "opL": [
        {"name": "ÖBB-Personenverkehr AG", "id": "OBB"},
        {"name": "Graz-Köflacher Bahn und Busbetrieb GmbH", "id": "GKB"},
    ],
}

HAFAS_TRIP_SEARCH: dict[str, Any] = _hafas_payload(
    [
        {
            # Wien Hbf → Salzburg Hbf, RJ 1141 direct (CEST).
            "date": "20260628",
            "dur": "024500",
            "chg": 0,
            "dep": {"locX": 0, "dTimeS": "080000"},
            "arr": {"locX": 1, "aTimeS": "104500"},
            "secL": [
                {
                    "type": "JNY",
                    "dep": {"locX": 0, "dTimeS": "080000"},
                    "arr": {"locX": 1, "aTimeS": "104500"},
                    "jny": {"prodX": 0, "jid": "1|123|0|81|28062026", "dirTxt": "Innsbruck Hbf"},
                }
            ],
        },
        {
            # Graz Hbf → Köflach on the S 7 (CET), a walk first, an unknown section last.
            "date": "20261103",
            "dur": "010700",
            "chg": 0,
            "dep": {"locX": 5, "dTimeS": "091000"},
            "arr": {"locX": 3, "aTimeS": "101100"},
            "secL": [
                {
                    "type": "WALK",
                    "dep": {"locX": 5, "dTimeS": "091000"},
                    "arr": {"locX": 2, "dTimeS": "091400", "aTimeS": "091400"},
                    "gis": {"dist": 410, "dur": "000400"},
                },
                {
                    "type": "JNY",
                    "dep": {"locX": 2, "dTimeS": "091400"},
                    "arr": {"locX": 3, "aTimeS": "101100"},
                    "jny": {"prodX": 1, "jid": "1|4512|0|81|3112026", "dirFlg": "Köflach Bahnhof"},
                },
                {
                    "type": "KISS",
                    "dep": {"locX": 3, "dTimeS": "101100"},
                    "arr": {"locX": 3, "aTimeS": "101100"},
                },
            ],
        },
        {
            # Past midnight (day-roll prefix), a bus from the bus terminal, a transfer, the RJ.
            "date": "20261103",
            "dur": "023000",  # 23:30 → 02:00 the next day
            "chg": 1,
            "dep": {"locX": 4, "dTimeS": "233000"},
            "arr": {"locX": 1, "aTimeS": "01020000"},
            "secL": [
                {
                    "type": "JNY",
                    "dep": {"locX": 4, "dTimeS": "233000"},
                    "arr": {"locX": 0, "aTimeS": "234500"},
                    "jny": {"prodX": 2, "jid": "1|77|0|81|3112026"},
                },
                {
                    "type": "TRSF",
                    "dep": {"locX": 0, "dTimeS": "234500"},
                    "arr": {"locX": 0, "aTimeS": "235000"},
                },
                {
                    "type": "JNY",
                    "dep": {"locX": 0, "dTimeS": "235000"},
                    "arr": {"locX": 1, "aTimeS": "01020000"},
                    "jny": {"prodX": 0, "jid": "1|124|0|81|3112026"},
                },
            ],
        },
        {"dur": "010000", "secL": []},  # no date: dropped
    ],
    HAFAS_COMMON,
)

# Real OJP 2.0 OJPTripDelivery shape (from tests/unit/test_ojp_client.py), plus a third trip
# with a train number and a product category short name.
OJP_TRIP_DELIVERY = """<OJP xmlns="http://www.vdv.de/ojp" xmlns:siri="http://www.siri.org.uk/siri">
<OJPResponse><siri:ServiceDelivery><OJPTripDelivery>
<TripResponseContext><Places>
  <Place><StopPoint><siri:StopPointRef>ch:1:sloid:7000:4:8</siri:StopPointRef></StopPoint>
    <Name><Text>Bern</Text></Name>
    <GeoPosition><siri:Longitude>7.43677</siri:Longitude><siri:Latitude>46.94864</siri:Latitude></GeoPosition></Place>
  <Place><StopPoint><siri:StopPointRef>ch:1:sloid:3000:501:33</siri:StopPointRef></StopPoint>
    <Name><Text>Zürich HB</Text></Name>
    <GeoPosition><siri:Longitude>8.53675</siri:Longitude><siri:Latitude>47.37852</siri:Latitude></GeoPosition></Place>
  <Place><StopPlace><StopPlaceRef>8502113</StopPlaceRef></StopPlace>
    <Name><Text>Aarau</Text></Name>
    <GeoPosition><siri:Longitude>8.05144</siri:Longitude><siri:Latitude>47.39125</siri:Latitude></GeoPosition></Place>
  <Place><StopPoint><siri:StopPointRef>ch:1:sloid:1008:2:3</siri:StopPointRef></StopPoint>
    <Name><Text>Genève</Text></Name>
    <GeoPosition><siri:Longitude>6.14243</siri:Longitude><siri:Latitude>46.21021</siri:Latitude></GeoPosition></Place>
</Places></TripResponseContext>
<TripResult><Id>ID-1</Id><Trip><Id>ID-1</Id>
  <Duration>PT1H9M</Duration>
  <StartTime>2026-05-18T08:26:00Z</StartTime><EndTime>2026-05-18T09:35:00Z</EndTime>
  <Transfers>0</Transfers><Distance>118226</Distance>
  <Leg><Id>1</Id><Duration>PT5M</Duration><ContinuousLeg>
    <LegStart><GeoPosition><siri:Longitude>7.43912</siri:Longitude><siri:Latitude>46.94884</siri:Latitude></GeoPosition>
      <Name><Text>Bern</Text></Name></LegStart>
    <LegEnd><siri:StopPointRef>ch:1:sloid:7000:4:8</siri:StopPointRef><Name><Text>Bern</Text></Name></LegEnd>
    <Service><PersonalMode>foot</PersonalMode></Service><Duration>PT5M</Duration><Length>221</Length>
  </ContinuousLeg></Leg>
  <Leg><Id>2</Id><Duration>PT57M</Duration><TimedLeg>
    <LegBoard><siri:StopPointRef>ch:1:sloid:7000:4:8</siri:StopPointRef>
      <StopPointName><Text>Bern</Text></StopPointName>
      <ServiceDeparture><TimetabledTime>2026-05-18T08:31:00Z</TimetabledTime></ServiceDeparture><Order>1</Order></LegBoard>
    <LegAlight><siri:StopPointRef>ch:1:sloid:3000:501:33</siri:StopPointRef>
      <StopPointName><Text>Zürich HB</Text></StopPointName>
      <ServiceArrival><TimetabledTime>2026-05-18T09:28:00Z</TimetabledTime></ServiceArrival><Order>3</Order></LegAlight>
    <Service><JourneyRef>ch:1:sjyid:100001:713-001</JourneyRef><PublicCode>IC1</PublicCode>
      <siri:LineRef>ojp:91001:D</siri:LineRef>
      <Mode><PtMode>rail</PtMode><ShortName><Text>IC</Text></ShortName></Mode>
      <ProductCategory><Name><Text>InterCity</Text></Name></ProductCategory>
      <PublishedServiceName><Text>IC1</Text></PublishedServiceName>
      <siri:OperatorRef>11</siri:OperatorRef>
      <DestinationText><Text>St. Gallen</Text></DestinationText></Service>
  </TimedLeg></Leg>
  <Leg><Id>3</Id><Duration>PT7M</Duration><ContinuousLeg>
    <LegStart><siri:StopPointRef>ch:1:sloid:3000:501:33</siri:StopPointRef><Name><Text>Zürich HB</Text></Name></LegStart>
    <LegEnd><GeoPosition><siri:Longitude>8.54018</siri:Longitude><siri:Latitude>47.37818</siri:Latitude></GeoPosition>
      <Name><Text>Zürich HB</Text></Name></LegEnd>
    <Service><PersonalMode>foot</PersonalMode></Service><Duration>PT7M</Duration><Length>0</Length>
  </ContinuousLeg></Leg>
</Trip></TripResult>
<TripResult><Id>ID-2</Id><Trip><Id>ID-2</Id>
  <Duration>PT1H29M</Duration>
  <StartTime>2026-05-18T08:29:00Z</StartTime><EndTime>2026-05-18T09:58:00Z</EndTime>
  <Transfers>1</Transfers>
  <Leg><Id>1</Id><Duration>PT3M</Duration><TransferLeg><TransferType>walk</TransferType>
    <LegStart><siri:StopPointRef>8502113</siri:StopPointRef><Name><Text>Aarau</Text></Name></LegStart>
    <LegEnd><siri:StopPointRef>8502113</siri:StopPointRef><Name><Text>Aarau</Text></Name></LegEnd>
    <Duration>PT3M</Duration></TransferLeg></Leg>
</Trip></TripResult>
<TripResult><Id>ID-3</Id><Trip><Id>ID-3</Id>
  <Duration>PT1H46M</Duration>
  <StartTime>2026-05-18T08:34:00Z</StartTime><EndTime>2026-05-18T10:20:00Z</EndTime>
  <Transfers>0</Transfers>
  <Leg><Id>1</Id><Duration>PT1H46M</Duration><TimedLeg>
    <LegBoard><siri:StopPointRef>ch:1:sloid:7000:4:8</siri:StopPointRef>
      <ServiceDeparture><TimetabledTime>2026-05-18T08:34:00Z</TimetabledTime></ServiceDeparture></LegBoard>
    <LegAlight><siri:StopPointRef>ch:1:sloid:1008:2:3</siri:StopPointRef>
      <ServiceArrival><TimetabledTime>2026-05-18T10:20:00Z</TimetabledTime></ServiceArrival></LegAlight>
    <Service><JourneyRef>ch:1:sjyid:100001:2515-001</JourneyRef><PublicCode>IR15</PublicCode>
      <Mode><PtMode>rail</PtMode></Mode>
      <ProductCategory><Name><Text>InterRegio</Text></Name><ShortName><Text>IR</Text></ShortName></ProductCategory>
      <PublishedServiceName><Text>IR 15</Text></PublishedServiceName>
      <TrainNumber>2515</TrainNumber>
      <siri:OperatorRef>11</siri:OperatorRef></Service>
  </TimedLeg></Leg>
</Trip></TripResult>
</OJPTripDelivery></siri:ServiceDelivery></OJPResponse></OJP>"""


def reference_journey(
    provider: Provider, *, dep_iso: str, route: str, from_uic: str, to_uic: str
) -> CanonicalJourney:
    """A one-ride journey whose `transit_fingerprint` (on the bridge's legs) is set by
    (route, from_uic, to_uic, departure minute): for the reference pagination tests, which
    craft pages unique or duplicate by these inputs."""
    at = CanonicalTiming(timetabled=datetime.fromisoformat(dep_iso))

    def stop(uic: str) -> CanonicalLocation:
        place = CanonicalStopPlace(ref=f"SBB:{uic}", source=f"{provider}:test", uic=uic)
        return CanonicalLocation(
            kind="stop", stop_place=place, position=GeoPosition(lat=46.948, lon=7.437)
        )

    leg = CanonicalLeg(
        kind="timed",
        origin=stop(from_uic),
        destination=stop(to_uic),
        departure=at,
        arrival=at,
        duration_seconds=1800,
        service=CanonicalService(mode=TransportMode.RAIL, line=route, published_name=route),
        source_mode="RAIL",
    )
    return CanonicalJourney(
        id=f"{provider}:test:0",
        source=JourneySource(provider=provider, role="reference", adapter_version=1),
        legs=[leg],
        departure=at,
        arrival=at,
        duration_seconds=1800,
        source_transfer_count=0,
    )


_NOT_RIDES = ("", "WALK", "TRANSFER")


def journey_from_legs(
    legs: list[dict[str, Any]],
    *,
    provider: Provider = "otp",
    role: str = "viator",
    rank: int = 0,
    departure_at: str | None = None,
    arrival_at: str | None = None,
    duration_seconds: int | None = None,
    transfers: int | None = None,
) -> CanonicalJourney:
    """A canonical journey from the simple leg dicts older tests write (`mode`,
    `from_stop_id` / `to_stop_id`, `from_lat`…, `departure` / `arrival` ISO, `route_short_name`):
    the bridge gives those legs back, so `transit_fingerprint` and every rule written on the
    old dicts see the same values."""
    from app.journey.canonical import mode_family
    from app.journey.canonical_adapters._common import location, position, viator_stop_place

    def at(value: str | None) -> CanonicalTiming | None:
        return CanonicalTiming(timetabled=datetime.fromisoformat(value)) if value else None

    def end(leg: dict[str, Any], prefix: str) -> CanonicalLocation:
        place = position(leg.get(f"{prefix}_lat"), leg.get(f"{prefix}_lon"))
        ref = leg.get(f"{prefix}_stop_id")
        name = leg.get(f"{prefix}_name")
        if not ref:
            return location(None, name, place)
        stop = viator_stop_place(source=f"{provider}:test", ref=ref, name=name, at=place)
        return location(stop, name, place)

    canonical_legs = []
    for leg in legs:
        mode = leg.get("mode")
        ride = str(mode or "").upper() not in _NOT_RIDES
        route = leg.get("route_short_name")
        canonical_legs.append(
            CanonicalLeg(
                kind="timed" if ride else "continuous",
                origin=end(leg, "from"),
                destination=end(leg, "to"),
                departure=at(leg.get("departure")),
                arrival=at(leg.get("arrival")),
                duration_seconds=int(leg.get("duration_seconds") or 0),
                service=CanonicalService(mode=mode_family(mode), line=route, published_name=route)
                if ride
                else None,
                continuous_mode=None if ride else "WALK",
                source_mode=mode,
            )
        )
    first = departure_at or next((lg.get("departure") for lg in legs if lg.get("departure")), None)
    last = arrival_at or next(
        (lg.get("arrival") for lg in reversed(legs) if lg.get("arrival")), None
    )
    start, stop = at(first), at(last)
    if duration_seconds is None:
        duration_seconds = (
            int((stop.timetabled - start.timetabled).total_seconds()) if start and stop else 0
        )
    return CanonicalJourney(
        id=f"{provider}:test:{rank}",
        source=JourneySource(provider=provider, role=role, adapter_version=1),  # type: ignore[arg-type]
        legs=canonical_legs,
        departure=start,
        arrival=stop,
        duration_seconds=max(0, duration_seconds),
        source_transfer_count=transfers,
    )
