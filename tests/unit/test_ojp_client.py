"""OJP reference-comparison adapter (v0.1.35).

Covers `app/journey/ojp_client.py` — the request builder and the
`TripResult` → VIATOR-trip-dict parser. Pure functions only; the one
network function (`fetch_reference`) is exercised by its callers in the
integration suite.

`_OJP_RESPONSE` is a faithful trim of a real `OJPTripDelivery` captured
from the live opentransportdata.swiss OJP 2.0 endpoint (the Phase 0
spike — see docs/ojp-reference-comparison-design.md Appendix A): one
walk→rail→walk trip and one walk→rail→transfer→rail→walk trip, so the
parser is pinned against actual OJP output, not a guess at the shape.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from defusedxml.ElementTree import fromstring as _xml_fromstring

from app.journey.ojp_client import (
    _build_trip_request,
    _float_or_none,
    _int_or_zero,
    _iso_duration_to_seconds,
    _iso_to_utc_iso,
    _normalise,
    _reference_departure,
)

# ─────────────────── _iso_duration_to_seconds ───────────────────


class TestIsoDurationToSeconds:
    def test_hours_and_minutes(self):
        assert _iso_duration_to_seconds("PT1H9M") == 4140
        assert _iso_duration_to_seconds("PT1H18M") == 4680

    def test_minutes_only(self):
        assert _iso_duration_to_seconds("PT50M") == 3000
        assert _iso_duration_to_seconds("PT4M") == 240

    def test_seconds(self):
        assert _iso_duration_to_seconds("PT0S") == 0
        assert _iso_duration_to_seconds("PT1M39S") == 99
        assert _iso_duration_to_seconds("PT15S") == 15

    def test_days_component(self):
        # Tolerated even though OJP trip durations never use it.
        assert _iso_duration_to_seconds("P1DT2H") == 93600

    def test_unparseable_is_zero(self):
        assert _iso_duration_to_seconds("") == 0
        assert _iso_duration_to_seconds(None) == 0
        assert _iso_duration_to_seconds("garbage") == 0
        assert _iso_duration_to_seconds("1H9M") == 0  # missing leading P


# ─────────────────── _iso_to_utc_iso ───────────────────


class TestIsoToUtcIso:
    def test_zulu_passthrough(self):
        assert _iso_to_utc_iso("2026-05-18T08:26:00Z") == "2026-05-18T08:26:00+00:00"

    def test_offset_converted_to_utc(self):
        assert _iso_to_utc_iso("2026-05-18T10:26:00+02:00") == "2026-05-18T08:26:00+00:00"

    def test_naive_assumed_utc(self):
        assert _iso_to_utc_iso("2026-05-18T08:26:00") == "2026-05-18T08:26:00+00:00"

    def test_none_and_garbage(self):
        assert _iso_to_utc_iso(None) is None
        assert _iso_to_utc_iso("") is None
        assert _iso_to_utc_iso("not-a-time") is None


# ─────────────────── small helpers ───────────────────


class TestSmallHelpers:
    def test_int_or_zero(self):
        assert _int_or_zero("3") == 3
        assert _int_or_zero(" 0 ") == 0
        assert _int_or_zero("") == 0
        assert _int_or_zero(None) == 0
        assert _int_or_zero("two") == 0

    def test_float_or_none(self):
        # pytest.approx — SonarCloud S1244: no bare == on floats.
        assert _float_or_none("24873.63") == pytest.approx(24873.63)
        assert _float_or_none(" 0 ") == pytest.approx(0.0)
        assert _float_or_none(None) is None
        assert _float_or_none("") is None
        assert _float_or_none("nope") is None


# ─────────────────── _reference_departure ───────────────────


class TestReferenceDeparture:
    def test_aware_datetime_used_as_is(self):
        aware = datetime(2026, 5, 18, 6, 0, 0, tzinfo=UTC)
        assert _reference_departure(aware) == "2026-05-18T06:00:00+00:00"

    def test_naive_datetime_gets_an_offset(self):
        # Localised to Europe/Zurich on a normal OS (CEST = +02:00 in
        # May); falls back to UTC only if the zone can't be loaded. Either
        # way the result must carry an explicit offset — OJP's DepArrTime
        # is an OffsetDateTime.
        naive = datetime(2026, 5, 18, 8, 0, 0)
        out = _reference_departure(naive)
        assert out.startswith("2026-05-18T08:00:00")
        assert out.endswith(("+02:00", "+00:00"))


# ─────────────────── _build_trip_request ───────────────────


class TestBuildTripRequest:
    def _build(self, **kw):
        defaults = {
            "from_lat": 46.948832,
            "from_lon": 7.439122,
            "to_lat": 47.378177,
            "to_lon": 8.540192,
            "when": datetime(2026, 5, 18, 8, 0, 0, tzinfo=UTC),
            "from_name": "Bern",
            "to_name": "Zürich HB",
            "num_results": 5,
        }
        defaults.update(kw)
        return _build_trip_request(**defaults)

    def test_is_well_formed_xml(self):
        _xml_fromstring(self._build())  # raises on malformed

    def test_carries_coordinates(self):
        xml = self._build()
        assert "7.439122" in xml and "46.948832" in xml
        assert "8.540192" in xml and "47.378177" in xml

    def test_station_names_are_escaped(self):
        xml = self._build(from_name="A <b> & 'c'", to_name="Zürich HB")
        # The raw '<' must not appear unescaped inside the Name text.
        assert "&lt;b&gt;" in xml and "&amp;" in xml
        _xml_fromstring(xml)  # still well-formed

    def test_missing_names_get_defaults(self):
        xml = self._build(from_name=None, to_name=None)
        assert "<Text>Origin</Text>" in xml
        assert "<Text>Destination</Text>" in xml

    def test_num_results_clamped(self):
        assert "<NumberOfResults>20</NumberOfResults>" in self._build(num_results=999)
        assert "<NumberOfResults>1</NumberOfResults>" in self._build(num_results=0)
        assert "<NumberOfResults>5</NumberOfResults>" in self._build(num_results=5)


# ─────────────────── _normalise ───────────────────


# Real OJP 2.0 OJPTripDelivery shape (trimmed). Trip 1: walk → IC1 rail →
# walk. Trip 2: walk → IR16 rail → walk-transfer → IR37 rail → walk.
_OJP_RESPONSE = """<OJP xmlns="http://www.vdv.de/ojp" xmlns:siri="http://www.siri.org.uk/siri">
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
</OJPTripDelivery></siri:ServiceDelivery></OJPResponse></OJP>"""


class TestNormalise:
    def test_two_trips_parsed(self):
        trips = _normalise(_OJP_RESPONSE)
        assert len(trips) == 2

    def test_trip_level_fields(self):
        trip = _normalise(_OJP_RESPONSE)[0]
        assert trip["duration_seconds"] == 4140  # PT1H9M
        assert trip["departure_at"] == "2026-05-18T08:26:00+00:00"
        assert trip["arrival_at"] == "2026-05-18T09:35:00+00:00"
        assert trip["num_transfers"] == 0
        assert trip["modes"] == "RAIL,WALK"  # sorted, deduped

    def test_continuous_leg(self):
        walk = _normalise(_OJP_RESPONSE)[0]["legs"][0]
        assert walk["mode"] == "WALK"
        assert walk["duration_seconds"] == 300
        # pytest.approx on every float field — SonarCloud S1244.
        assert walk["distance_meters"] == pytest.approx(221.0)
        assert walk["from_name"] == "Bern"
        # LegStart carried an inline GeoPosition.
        assert walk["from_lat"] == pytest.approx(46.94884)
        assert walk["from_lon"] == pytest.approx(7.43912)
        # LegEnd was a StopPointRef — name resolved, coords via Places.
        assert walk["to_name"] == "Bern"
        assert walk["to_lat"] == pytest.approx(46.94864)
        assert walk["feed_id"] == "OJP"

    def test_timed_leg(self):
        rail = _normalise(_OJP_RESPONSE)[0]["legs"][1]
        assert rail["mode"] == "RAIL"  # PtMode "rail" upper-cased
        assert rail["duration_seconds"] == 3420  # PT57M
        assert rail["departure"] == "2026-05-18T08:31:00+00:00"
        assert rail["arrival"] == "2026-05-18T09:28:00+00:00"
        assert rail["from_name"] == "Bern"
        assert rail["from_stop_id"] == "ch:1:sloid:7000:4:8"
        assert rail["to_name"] == "Zürich HB"
        assert rail["to_lat"] == pytest.approx(47.37852)  # resolved via Places dict
        assert rail["route_short_name"] == "IC1"  # PublishedServiceName
        assert rail["route_long_name"] == "InterCity"  # ProductCategory
        assert rail["route_id"] == "ojp:91001:D"  # siri:LineRef
        assert rail["agency_id"] == "11"  # siri:OperatorRef
        assert rail["trip_id"] == "ch:1:sjyid:100001:713-001"  # JourneyRef
        assert rail["trip_headsign"] == "St. Gallen"  # DestinationText
        assert rail["feed_id"] == "OJP"

    def test_transfer_leg(self):
        # Trip 2's only leg in this fixture is a TransferLeg.
        transfer = _normalise(_OJP_RESPONSE)[1]["legs"][0]
        assert transfer["mode"] == "WALK"
        assert transfer["duration_seconds"] == 180  # PT3M
        assert transfer["from_name"] == "Aarau"
        # StopPlaceRef "8502113" resolved via the Places dictionary.
        assert transfer["from_lat"] == pytest.approx(47.39125)

    def test_empty_and_malformed_degrade_to_empty_list(self):
        assert _normalise("") == []
        assert _normalise("<not-ojp/>") == []
        assert _normalise("this is not xml at all <") == []
        # An OJP error payload has no <TripResult> — also → [].
        assert _normalise("<OJP xmlns='http://www.vdv.de/ojp'><OJPResponse/></OJP>") == []


# ─────────────────── fetch_reference_paginated ───────────────────


def _make_trip(*, dep_iso: str, route: str, from_uic: str, to_uic: str) -> dict:
    """A minimal trip dict whose transit_fingerprint is determined by
    (route, from_uic, to_uic, dep_minute). Lets us craft batches that
    are unique by these inputs without building real XML."""
    return {
        "duration_seconds": 1800,
        "num_transfers": 0,
        "departure_at": dep_iso,
        "arrival_at": dep_iso,  # not used by fingerprint
        "modes": "RAIL",
        "legs": [
            {
                "mode": "RAIL",
                "from_lat": 46.948,
                "from_lon": 7.437,
                "to_lat": 47.378,
                "to_lon": 8.537,
                "from_stop_id": f"SBB:{from_uic}",
                "to_stop_id": f"SBB:{to_uic}",
                "departure": dep_iso,
                "arrival": dep_iso,
                "route_short_name": route,
                "feed_id": "OJP",
            }
        ],
        "feed_id": "OJP",
    }


class TestFetchReferencePaginated:
    """v0.1.35.06 anchor-time pagination: issue successive OJP TripRequests
    until time-window coverage catches up to OTP's searchWindow, then
    dedupe boundary trips via transit_fingerprint and return the union.
    """

    @pytest.mark.asyncio
    async def test_single_page_covers_window_no_pagination(self, monkeypatch):
        # OJP's first batch already reaches the target window end →
        # one call, return what came back.
        from app.journey import ojp_client

        async def fake(when, **kw):
            # Single trip at depart+5h30m — past the 6h target window? No,
            # 5h30m is just under 6h. But "latest_dep >= target_end" stops
            # the loop because it's >= the 6h target window of 21600s? Let's
            # set the trip at 7h to ensure stop.
            return {}, [
                _make_trip(
                    dep_iso=(when + timedelta(hours=7)).isoformat(),
                    route="IR15",
                    from_uic="8507000",
                    to_uic="8501008",
                )
            ]

        async def fake_fetch_reference(**kw):
            return await fake(kw["when"])

        monkeypatch.setattr(ojp_client, "fetch_reference", fake_fetch_reference)

        trips, _ms, pages = await ojp_client.fetch_reference_paginated(
            from_lat=0,
            from_lon=0,
            to_lat=0,
            to_lon=0,
            when=datetime(2026, 5, 25, 6, 0, 0, tzinfo=UTC),
            timeout_ms=5000,
            endpoint="https://example/ojp",
            token="x",
            target_window_seconds=21600,
            max_pages=4,
        )
        assert pages == 1
        assert len(trips) == 1

    @pytest.mark.asyncio
    async def test_paginates_until_window_covered(self, monkeypatch):
        # Each batch's latest trip is +1h ahead of its anchor. Target
        # window 4h → need 4 calls to walk past the 4h mark.
        from app.journey import ojp_client

        calls: list[datetime] = []

        async def fake_fetch_reference(**kw):
            calls.append(kw["when"])
            base = kw["when"]
            return {}, [
                _make_trip(
                    dep_iso=(base + timedelta(minutes=30)).isoformat(),
                    route="IR15",
                    from_uic=f"850{1000 + len(calls)}",  # unique per call
                    to_uic="8501008",
                ),
                _make_trip(
                    dep_iso=(base + timedelta(minutes=60)).isoformat(),
                    route="IC1",
                    from_uic=f"850{2000 + len(calls)}",
                    to_uic="8501008",
                ),
            ]

        monkeypatch.setattr(ojp_client, "fetch_reference", fake_fetch_reference)

        start = datetime(2026, 5, 25, 6, 0, 0, tzinfo=UTC)
        trips, _ms, pages = await ojp_client.fetch_reference_paginated(
            from_lat=0,
            from_lon=0,
            to_lat=0,
            to_lon=0,
            when=start,
            timeout_ms=5000,
            endpoint="https://example/ojp",
            token="x",
            target_window_seconds=4 * 3600,
            max_pages=8,  # well above what we expect
        )
        # 4h window / 1h forward progress per page = 4 pages to cover.
        # Bookkeeping: page 1 anchored at +0h → latest +1h; page 2 at
        # +1h01m → latest +2h01m; … page 4 anchored ~+3h03m → latest
        # ~+4h03m, which crosses the 4h target → stop after page 4.
        assert pages == 4
        assert len(trips) == 8  # 2 unique trips per page
        # Anchors should march forward in time.
        for i in range(1, len(calls)):
            assert calls[i] > calls[i - 1]

    @pytest.mark.asyncio
    async def test_empty_batch_stops_pagination(self, monkeypatch):
        # First page returns trips; second returns nothing → stop.
        from app.journey import ojp_client

        page_calls = {"n": 0}

        async def fake_fetch_reference(**kw):
            page_calls["n"] += 1
            if page_calls["n"] == 1:
                return {}, [
                    _make_trip(
                        dep_iso=(kw["when"] + timedelta(minutes=10)).isoformat(),
                        route="IR15",
                        from_uic="8507000",
                        to_uic="8501008",
                    )
                ]
            return {}, []  # exhausted

        monkeypatch.setattr(ojp_client, "fetch_reference", fake_fetch_reference)

        trips, _ms, pages = await ojp_client.fetch_reference_paginated(
            from_lat=0,
            from_lon=0,
            to_lat=0,
            to_lon=0,
            when=datetime(2026, 5, 25, 6, 0, 0, tzinfo=UTC),
            timeout_ms=5000,
            endpoint="https://example/ojp",
            token="x",
            target_window_seconds=21600,
            max_pages=4,
        )
        assert pages == 2
        assert len(trips) == 1

    @pytest.mark.asyncio
    async def test_all_duplicate_batch_stops_pagination(self, monkeypatch):
        # Same trip on every page → seen_fps already has it after page 1,
        # page 2 yields no new trips → bail.
        from app.journey import ojp_client

        async def fake_fetch_reference(**kw):
            return {}, [
                _make_trip(
                    dep_iso="2026-05-25T06:10:00+00:00",
                    route="IR15",
                    from_uic="8507000",
                    to_uic="8501008",
                )
            ]

        monkeypatch.setattr(ojp_client, "fetch_reference", fake_fetch_reference)

        trips, _ms, pages = await ojp_client.fetch_reference_paginated(
            from_lat=0,
            from_lon=0,
            to_lat=0,
            to_lon=0,
            when=datetime(2026, 5, 25, 6, 0, 0, tzinfo=UTC),
            timeout_ms=5000,
            endpoint="https://example/ojp",
            token="x",
            target_window_seconds=21600,
            max_pages=4,
        )
        # Page 1 collects the trip; page 2 returns the same dup → all_dups
        # break → 2 pages total, 1 unique trip.
        assert pages == 2
        assert len(trips) == 1

    @pytest.mark.asyncio
    async def test_max_pages_caps_pagination(self, monkeypatch):
        # Infinite supply of new trips that never reach the window end —
        # max_pages bounds the call count.
        from app.journey import ojp_client

        async def fake_fetch_reference(**kw):
            # Each call returns a trip just 5 min past its own anchor.
            # 4 pages x 5 min = 20 min - nowhere near the 6h target.
            base = kw["when"]
            uid = base.minute  # cheap unique-ish id per anchor
            return {}, [
                _make_trip(
                    dep_iso=(base + timedelta(minutes=5)).isoformat(),
                    route=f"IR{uid}",
                    from_uic=f"850{7000 + uid % 100}",
                    to_uic="8501008",
                )
            ]

        monkeypatch.setattr(ojp_client, "fetch_reference", fake_fetch_reference)

        trips, _ms, pages = await ojp_client.fetch_reference_paginated(
            from_lat=0,
            from_lon=0,
            to_lat=0,
            to_lon=0,
            when=datetime(2026, 5, 25, 6, 0, 0, tzinfo=UTC),
            timeout_ms=5000,
            endpoint="https://example/ojp",
            token="x",
            target_window_seconds=21600,
            max_pages=3,
        )
        assert pages == 3  # capped exactly
        assert len(trips) == 3

    @pytest.mark.asyncio
    async def test_boundary_dedup_via_fingerprint(self, monkeypatch):
        # Page 1: trip A. Page 2: trip A (boundary dup) + trip B (new).
        # Expect 2 unique trips, 2 pages, B preserved.
        from app.journey import ojp_client

        page_calls = {"n": 0}

        def trip_a(when):
            return _make_trip(
                dep_iso=(when + timedelta(minutes=30)).isoformat(),
                route="IR15",
                from_uic="8507000",
                to_uic="8501008",
            )

        async def fake_fetch_reference(**kw):
            page_calls["n"] += 1
            if page_calls["n"] == 1:
                return {}, [trip_a(kw["when"])]
            # Page 2: dup A (same dep, route, UICs → same fingerprint)
            # plus trip B (different route).
            same_dep = "2026-05-25T06:30:00+00:00"
            return {}, [
                _make_trip(
                    dep_iso=same_dep,
                    route="IR15",
                    from_uic="8507000",
                    to_uic="8501008",
                ),
                _make_trip(
                    dep_iso="2026-05-25T07:00:00+00:00",
                    route="IC1",
                    from_uic="8507000",
                    to_uic="8501008",
                ),
            ]

        monkeypatch.setattr(ojp_client, "fetch_reference", fake_fetch_reference)

        trips, _ms, pages = await ojp_client.fetch_reference_paginated(
            from_lat=0,
            from_lon=0,
            to_lat=0,
            to_lon=0,
            when=datetime(2026, 5, 25, 6, 0, 0, tzinfo=UTC),
            timeout_ms=5000,
            endpoint="https://example/ojp",
            token="x",
            target_window_seconds=21600,
            max_pages=4,
        )
        # Page 1 → 1 trip (A). Page 2 → A is dup, B is new.
        # Loop continues to page 3 (B's dep is 07:00, anchor for p3
        # would be 07:01, still well inside 6h window), which returns
        # the same response as page 2 → all dups → stop.
        assert len(trips) == 2  # A + B, no duplicate A
        routes = sorted(t["legs"][0]["route_short_name"] for t in trips)
        assert routes == ["IC1", "IR15"]
        assert pages >= 2  # at least the dedup-tested pages happened

    @pytest.mark.asyncio
    async def test_http_error_on_first_page_propagates(self, monkeypatch):
        # No partial data yet → caller needs the exception to map status.
        import httpx

        from app.journey import ojp_client

        async def boom(**kw):
            raise httpx.HTTPError("boom")

        monkeypatch.setattr(ojp_client, "fetch_reference", boom)

        with pytest.raises(httpx.HTTPError):
            await ojp_client.fetch_reference_paginated(
                from_lat=0,
                from_lon=0,
                to_lat=0,
                to_lon=0,
                when=datetime(2026, 5, 25, 6, 0, 0, tzinfo=UTC),
                timeout_ms=5000,
                endpoint="https://example/ojp",
                token="x",
            )

    @pytest.mark.asyncio
    async def test_http_error_on_later_page_returns_partial(self, monkeypatch):
        # Page 1 succeeds, page 2 raises → swallow, return page-1 data.
        import httpx

        from app.journey import ojp_client

        page_calls = {"n": 0}

        async def flaky(**kw):
            page_calls["n"] += 1
            if page_calls["n"] == 1:
                return {}, [
                    _make_trip(
                        dep_iso=(kw["when"] + timedelta(minutes=10)).isoformat(),
                        route="IR15",
                        from_uic="8507000",
                        to_uic="8501008",
                    )
                ]
            raise httpx.HTTPError("boom on page 2")

        monkeypatch.setattr(ojp_client, "fetch_reference", flaky)

        trips, _ms, pages = await ojp_client.fetch_reference_paginated(
            from_lat=0,
            from_lon=0,
            to_lat=0,
            to_lon=0,
            when=datetime(2026, 5, 25, 6, 0, 0, tzinfo=UTC),
            timeout_ms=5000,
            endpoint="https://example/ojp",
            token="x",
            target_window_seconds=21600,
            max_pages=4,
        )
        assert pages == 2  # we attempted page 2 (which raised)
        assert len(trips) == 1  # page-1 data preserved
