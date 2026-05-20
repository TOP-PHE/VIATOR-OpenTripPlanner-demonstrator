"""Cross-engine fingerprinting for the OJP reference comparison (v0.1.36).

Covers:

  - `app.journey.signature.transit_fingerprint` — the DB-free helper
    that captures an itinerary's transit-leg spine in a stable 16-hex
    string. Walks/transfers stripped; coordinates rounded to ~11 m so
    OTP's `SBB:…` stop ids and OJP's `ch:1:sloid:…` stop ids match by
    location.
  - `app.api.journey._build_comparison` — bucketing of merged_trips +
    ojp_reference into common / OTP-only / OJP-only with both per-trip
    tags and a summary count.

The cross-engine match test is the centrepiece: an OJP-shape itinerary
with an Origin→Bern walk + Bern→Zürich transit must fingerprint
identically to an OTP-shape itinerary with *only* the transit leg
(stop-id routing has no end walks). That's the whole point of stripping
walks before hashing.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.api.journey import _build_comparison
from app.journey.signature import transit_fingerprint

# ─────────────────── transit_fingerprint ───────────────────


def _walk_leg(**kw: Any) -> dict[str, Any]:
    base = {
        "mode": "WALK",
        "from_lat": 46.948832,
        "from_lon": 7.439122,
        "to_lat": 46.948640,
        "to_lon": 7.436770,
        "departure": "2026-05-18T08:26:00+00:00",
        "arrival": "2026-05-18T08:31:00+00:00",
        "duration_seconds": 300,
    }
    base.update(kw)
    return base


def _rail_leg(**kw: Any) -> dict[str, Any]:
    """A Bern → Zürich HB IC1 leg — mirrors the verified OJP RE9 fixture's
    transit shape (coords + scheduled times rounded to the minute)."""
    base = {
        "mode": "RAIL",
        "from_lat": 46.948640,
        "from_lon": 7.436770,
        "to_lat": 47.378520,
        "to_lon": 8.536750,
        "departure": "2026-05-18T08:31:00+00:00",
        "arrival": "2026-05-18T09:28:00+00:00",
        "duration_seconds": 3420,
        "route_short_name": "IC1",
    }
    base.update(kw)
    return base


class TestTransitFingerprintBasics:
    def test_empty_legs_returns_empty_string(self):
        assert transit_fingerprint([]) == ""

    def test_all_walks_returns_empty_string(self):
        # No transit spine → no comparable fingerprint → caller treats
        # as 'uncomparable', avoiding false matches between walk-only
        # itineraries.
        assert transit_fingerprint([_walk_leg(), _walk_leg()]) == ""

    def test_transfer_leg_also_stripped(self):
        # OJP labels mid-trip transfers as a separate TransferLeg with
        # mode set to 'WALK' in our normalised shape, but some adapters
        # might emit literal 'TRANSFER' — both are stripped.
        only_rail = transit_fingerprint([_rail_leg()])
        with_transfer = transit_fingerprint(
            [_rail_leg(), {"mode": "TRANSFER", "duration_seconds": 240}]
        )
        assert with_transfer == only_rail

    def test_single_rail_produces_16hex(self):
        fp = transit_fingerprint([_rail_leg()])
        assert len(fp) == 16
        assert all(c in "0123456789abcdef" for c in fp)

    def test_mode_is_case_insensitive(self):
        upper = transit_fingerprint([_rail_leg(mode="RAIL")])
        lower = transit_fingerprint([_rail_leg(mode="rail")])
        mixed = transit_fingerprint([_rail_leg(mode="Rail")])
        assert upper == lower == mixed

    def test_route_name_whitespace_normalised(self):
        bare = transit_fingerprint([_rail_leg(route_short_name="IC1")])
        padded = transit_fingerprint([_rail_leg(route_short_name="  ic1 ")])
        assert bare == padded


class TestTransitFingerprintDiscrimination:
    def test_different_route_differs(self):
        ic1 = transit_fingerprint([_rail_leg(route_short_name="IC1")])
        re9 = transit_fingerprint([_rail_leg(route_short_name="RE9")])
        assert ic1 != re9

    def test_different_time_differs(self):
        eight = transit_fingerprint([_rail_leg(departure="2026-05-18T08:31:00+00:00")])
        nine = transit_fingerprint([_rail_leg(departure="2026-05-18T09:31:00+00:00")])
        assert eight != nine

    def test_coord_rounding_absorbs_sub_metre_noise(self):
        # 3-decimal rounding (~110 m) easily absorbs sub-metre wobble.
        # Real-world: OTP and OJP both report Bern station but may
        # differ in the 4th-5th decimal due to different source feeds
        # (DiDok vs operator GTFS), and both legitimately disagree on
        # which platform's centroid to publish.
        a = transit_fingerprint([_rail_leg(from_lat=46.948640, from_lon=7.436770)])
        b = transit_fingerprint([_rail_leg(from_lat=46.948643, from_lon=7.436771)])
        assert a == b

    def test_coord_rounding_absorbs_typical_cross_feed_offset(self):
        # ~70 m difference (4th-decimal jump) is well within the
        # cross-feed centroid variance we routinely see — must still
        # fingerprint identically at 3 dp. This is the regression test
        # for the v0.1.35.01 bug where 4-dp rounding made TGV→IC1
        # connections at Lausanne false-mismatch.
        a = transit_fingerprint([_rail_leg(from_lat=46.948640, from_lon=7.436770)])
        b = transit_fingerprint([_rail_leg(from_lat=46.948700, from_lon=7.436850)])
        assert a == b

    def test_coord_rounding_distinguishes_real_distance(self):
        # ~250 m apart (3rd-decimal jump) → different fingerprint.
        # Guards against the rounding being too aggressive — different
        # rail stations are always >>110 m apart, so this remains
        # discriminative for the real use case.
        a = transit_fingerprint([_rail_leg(from_lat=46.948640, from_lon=7.436770)])
        b = transit_fingerprint([_rail_leg(from_lat=46.951000, from_lon=7.439000)])
        assert a != b


class TestUicTokenisation:
    """v0.1.35.02 — UIC parsed from stop_id is the primary stop token.

    The 7-digit DiDok number is consistent across OTP and OJP for the
    same physical station; matching on UIC sidesteps every coordinate
    headache (platform precision in OTP, centroid disagreement between
    feeds, walk-graph snap offsets, etc.).
    """

    def test_otp_simple_stop_id_yields_uic_token(self):
        # OTP without stop-id routing emits clean SBB:NNNNNNN. The token
        # depends on the UIC, NOT on the lat/lon (which is the bug fix —
        # before, the lat/lon was always primary).
        a = transit_fingerprint([_rail_leg(from_stop_id="SBB:8501120", to_stop_id="SBB:8501008")])
        # Same UICs, totally different lat/lon, still match: the stop_id
        # wins.
        b = transit_fingerprint(
            [
                _rail_leg(
                    from_stop_id="SBB:8501120",
                    to_stop_id="SBB:8501008",
                    from_lat=0.0,
                    from_lon=0.0,
                    to_lat=0.0,
                    to_lon=0.0,
                )
            ]
        )
        assert a == b

    def test_otp_platform_suffix_does_not_affect_uic(self):
        # OTP with stop-id routing emits SBB:NNNNNNN:0:P where P is the
        # platform. The TGV arrives at platform 5; the IC1 departs from
        # platform 4. Both must yield the same UIC token (8501120) so
        # the within-itinerary connection is recognised as the same
        # station — and the cross-engine match against OJP works too.
        plat5 = transit_fingerprint([_rail_leg(to_stop_id="SBB:8501120:0:5")])
        plat4 = transit_fingerprint([_rail_leg(to_stop_id="SBB:8501120:0:4")])
        assert plat5 == plat4

    def test_ojp_sloid_with_swiss_dsn_reconstructs_full_uic(self):
        # v0.1.35.03 — opentransportdata.swiss SLOIDs use the 4-digit
        # Swiss DSN (DiDok-Nummer), NOT the full UIC. Bern's UIC is
        # 8507000; OJP emits "ch:1:sloid:7000:4:7" — just the trailing
        # 4 digits. The parser must prepend "850" to reconstruct
        # UIC:8507000, otherwise OTP's "SBB:8507000:0:7" and OJP's
        # "ch:1:sloid:7000:4:7" produce different tokens and the
        # fingerprint mismatches (the v0.1.35.02 regression Patrick
        # caught on the Bern→Geneva IR15).
        otp = transit_fingerprint([_rail_leg(from_stop_id="SBB:8507000:0:7")])
        ojp = transit_fingerprint([_rail_leg(from_stop_id="ch:1:sloid:7000:4:7")])
        assert otp == ojp

    def test_ojp_sloid_full_uic_still_parses(self):
        # Some OJP responses (non-Swiss stations, or older feeds) carry
        # the full 7-digit UIC in the SLOID. The 7-digit search runs
        # first and wins, so behaviour is identical to the OTP form.
        otp = transit_fingerprint([_rail_leg(from_stop_id="SBB:8501120:0:5")])
        ojp = transit_fingerprint([_rail_leg(from_stop_id="ch:1:sloid:8501120:0:5")])
        assert otp == ojp

    def test_dsn_prefix_only_applied_for_swiss_namespace(self):
        # A 4-digit chunk in a non-`ch:1:` id must NOT get the `850`
        # prefix — it's not a Swiss DSN, just an arbitrary id. Falls
        # through to lat/lon.
        from app.journey.signature import _uic_from_stop_id

        assert _uic_from_stop_id("ch:1:sloid:7000:4:7") == "8507000"
        # Non-Swiss namespace: 4-digit chunk is NOT treated as a DSN.
        assert _uic_from_stop_id("STIB:1234") is None
        assert _uic_from_stop_id("BVG:5678:0:1") is None

    def test_no_uic_falls_back_to_3dp_latlon(self):
        # Non-Swiss feed (no parseable UIC chunk) falls back to lat/lon.
        # Two synthetic ids with the same lat/lon must still match.
        a = transit_fingerprint(
            [
                _rail_leg(
                    from_stop_id="STIB:abcd",
                    to_stop_id="STIB:efgh",
                    from_lat=50.85,
                    from_lon=4.35,
                )
            ]
        )
        b = transit_fingerprint(
            [
                _rail_leg(
                    from_stop_id="MIVB:wxyz",
                    to_stop_id="MIVB:1q2w",
                    from_lat=50.85,
                    from_lon=4.35,
                )
            ]
        )
        assert a == b  # Different stop_ids, same coords → match via fallback

    def test_pontarlier_french_station_in_sbb_feed(self):
        # Pontarlier is a French SNCF station present in the cross-
        # border SBB feed (route 91-P38). OTP emits it as
        # "SBB:8771500"; OJP via opentransportdata.swiss may emit it
        # with the full UIC (it's not a Swiss DSN, so no prefix to
        # drop). Both yield UIC:8771500.
        otp = transit_fingerprint([_rail_leg(from_stop_id="SBB:8771500", route_short_name="P38")])
        ojp = transit_fingerprint(
            [_rail_leg(from_stop_id="ch:1:sloid:8771500:0:1", route_short_name="P38")]
        )
        assert otp == ojp


class TestUicCheckDigit:
    """v0.1.36 — cross-NAP UIC normalisation. SNCF publishes 8-digit
    station codes (7-digit UIC + a trailing check digit); SBB publishes
    7-digit UICs. The parser reduces both to the same 7-digit core so a
    SNCF leg and an SBB leg of the SAME cross-border train fingerprint
    identically. Numbers transcribed from the live SNCF GTFS (TGV Lyria
    9263, route 622E) compared against the SBB GTFS describing the same
    train (the cross-NAP federation spike)."""

    def test_sncf_eight_digit_reduces_to_seven(self):
        from app.journey.signature import _uic_from_stop_id

        # SNCF 8-digit "87686006" = UIC 8768600 + check digit 6.
        assert _uic_from_stop_id("StopPoint:OCELyria-87686006") == "8768600"
        # SBB 7-digit form of the same station.
        assert _uic_from_stop_id("8768600") == "8768600"

    def test_sncf_vs_sbb_same_station_match(self):
        from app.journey.signature import _uic_from_stop_id

        # The four Lyria 9263 stops, SNCF 8-digit vs SBB 7-digit.
        pairs = [
            ("87686006", "8768600"),  # Paris Gare de Lyon
            ("87713040", "8771304"),  # Dijon
            ("85011031", "8501103"),  # Vallorbe
            ("85010082", "8501008"),  # Genève
        ]
        for sncf8, sbb7 in pairs:
            assert _uic_from_stop_id(sncf8) == _uic_from_stop_id(sbb7) == sbb7

    def test_lyria_9263_fingerprints_identically_across_nap(self):
        # The dispositive cross-NAP case: TGV Lyria 9263 described by BOTH
        # SNCF (8-digit UICs, route_short_name "622E") and SBB (7-digit
        # UICs, route_short_name "622E"). Same times, same stops, same
        # route name — only the UIC encoding differs. Must fingerprint
        # identically once the check digit is normalised away.
        sncf_leg = _rail_leg(
            from_stop_id="StopPoint:OCELyria-87686006",  # Paris GdL, 8-digit
            to_stop_id="StopPoint:OCELyria-85010082",  # Genève, 8-digit
            route_short_name="622E",
            departure="2026-08-16T05:56:00+00:00",
            arrival="2026-08-16T10:25:00+00:00",
        )
        sbb_leg = _rail_leg(
            from_stop_id="8768600",  # Paris GdL, 7-digit
            to_stop_id="8501008",  # Genève, 7-digit
            route_short_name="622E",
            departure="2026-08-16T05:56:00+00:00",
            arrival="2026-08-16T10:25:00+00:00",
        )
        assert transit_fingerprint([sncf_leg]) == transit_fingerprint([sbb_leg])
        assert transit_fingerprint([sncf_leg]) != ""

    def test_nine_digit_blob_not_treated_as_uic(self):
        from app.journey.signature import _uic_from_stop_id

        # A 9+ digit run is not a UIC — must not yield a bogus prefix.
        assert _uic_from_stop_id("123456789") is None

    def test_existing_seven_digit_behaviour_unchanged(self):
        from app.journey.signature import _uic_from_stop_id

        # Regression guard: the 7-or-8-digit widening must not change
        # plain 7-digit parsing the OJP comparison relies on.
        assert _uic_from_stop_id("SBB:8507000:0:7") == "8507000"
        assert _uic_from_stop_id("ch:1:sloid:7000:4:7") == "8507000"
        assert _uic_from_stop_id("STIB:1234") is None


class TestCrossEngineMatching:
    """The centrepiece: OJP and OTP shapes of the *same train* must
    fingerprint identically, even though they differ in stop_id
    namespace and walk-leg presence."""

    def test_ojp_with_end_walks_matches_otp_without(self):
        # OJP shape: end-walks framing the transit leg, opaque SLOID
        # ids that use the 4-digit Swiss DSN (DiDok-Nummer, the trailing
        # 4 digits of the UIC). OTP-with-stop-id-routing shape: just
        # the transit leg, OTP-namespaced stop ids (SBB:NNNNNNN). Both
        # describe the same physical Bern→Zürich at 08:31→09:28: the
        # parser reconstructs OJP "ch:1:sloid:7000" → UIC 8507000 to
        # match OTP "SBB:8507000".
        ojp_itin = [
            _walk_leg(  # Origin → Bern (the access walk OJP renders)
                from_lat=46.94884,
                from_lon=7.43912,
                to_lat=46.94864,
                to_lon=7.43677,
            ),
            _rail_leg(
                from_stop_id="ch:1:sloid:7000:0:8",  # OJP DSN 7000 → UIC 8507000
                to_stop_id="ch:1:sloid:3000:0:33",  # OJP DSN 3000 → UIC 8503000
            ),
            _walk_leg(  # Zürich → destination (egress walk)
                from_lat=47.37852,
                from_lon=8.53675,
                to_lat=47.37818,
                to_lon=8.54018,
            ),
        ]
        otp_itin = [
            _rail_leg(
                from_stop_id="SBB:8507000",  # OTP stop id
                to_stop_id="SBB:8503000",
            ),
        ]
        assert transit_fingerprint(ojp_itin) == transit_fingerprint(otp_itin)
        # And not the empty fingerprint — both have a real transit spine.
        assert transit_fingerprint(ojp_itin) != ""

    def test_lausanne_tgv_to_ic1_connection_matches_cross_engine(self):
        # Regression test for the v0.1.35.01 bug observed live:
        # Pontarlier → Geneva via Frasne (TGV) + Lausanne (IC1).
        # OTP reports platform-precise lat/lon for each leg endpoint
        # (TGV arrives Lausanne platform 5, IC1 leaves platform 4 —
        # 130m apart, different at 4 dp). OJP reports station-centroid
        # coords. Before v0.1.35.02 the fingerprints differed; after,
        # both endpoints resolve to UIC:8501120 regardless of platform.
        otp_itin = [
            _rail_leg(  # TGV Frasne → Lausanne platform 5
                from_stop_id="SBB:8771513",
                to_stop_id="SBB:8501120:0:5",
                from_lat=46.8577495,
                from_lon=6.1578884,
                to_lat=46.5165829,
                to_lon=6.6290278,
                route_short_name="TGV",
                departure="2026-05-25T12:45:00+00:00",
                arrival="2026-05-25T13:39:00+00:00",
            ),
            _rail_leg(  # IC1 Lausanne platform 4 → Geneva platform 3
                from_stop_id="SBB:8501120:0:4",
                to_stop_id="SBB:8501008:0:3",
                from_lat=46.5166695,
                from_lon=6.6290548,
                to_lat=46.2105224,
                to_lon=6.1424194,
                route_short_name="IC1",
                departure="2026-05-25T13:46:00+00:00",
                arrival="2026-05-25T14:25:00+00:00",
            ),
        ]
        # OJP SLOIDs use the 4-digit Swiss DSN (the trailing 4 digits
        # of the UIC) — Lausanne UIC 8501120 becomes ch:1:sloid:1120;
        # Geneva UIC 8501008 becomes ch:1:sloid:1008. The parser
        # reconstructs the full UIC by prepending `850`.
        ojp_itin = [
            _walk_leg(),  # Origin → Frasne access walk
            _rail_leg(  # TGV with OJP-namespaced ids + station-centroid coords
                # Pontarlier is French (UIC starts with 87) — the test
                # fixture uses a synthetic 8771513 for Frasne; both
                # sides keep the full UIC since it's not a Swiss DSN.
                from_stop_id="ch:1:sloid:8771513:0:1",
                to_stop_id="ch:1:sloid:1120:0:5",  # Lausanne DSN
                from_lat=46.8569,  # different centroid — OJP source
                from_lon=6.1564,
                to_lat=46.5167,
                to_lon=6.6294,
                route_short_name="TGV",
                departure="2026-05-25T12:45:00+00:00",
                arrival="2026-05-25T13:39:00+00:00",
            ),
            _walk_leg(),  # Lausanne platform-transfer walk
            _rail_leg(  # IC1 from same Lausanne UIC, different platform suffix
                from_stop_id="ch:1:sloid:1120:0:4",  # Lausanne DSN, plat 4
                to_stop_id="ch:1:sloid:1008:0:3",  # Geneva DSN
                from_lat=46.5167,
                from_lon=6.6294,
                to_lat=46.2103,
                to_lon=6.1424,
                route_short_name="IC1",
                departure="2026-05-25T13:46:00+00:00",
                arrival="2026-05-25T14:25:00+00:00",
            ),
            _walk_leg(),  # Geneva → destination egress walk
        ]
        assert transit_fingerprint(otp_itin) == transit_fingerprint(ojp_itin)

    def test_bern_to_geneva_ir15_direct_matches_cross_engine(self):
        # Regression test for the v0.1.35.02 bug: direct IR15 Bern→Geneva.
        # OTP emits SBB:8507000:0:7 (Bern UIC 8507000 + platform 7) and
        # SBB:8501008:0:3 (Geneva UIC 8501008 + platform 3). OJP emits
        # ch:1:sloid:7000:4:7 and ch:1:sloid:1008:2:3 (Swiss DSNs).
        # v0.1.35.02 only parsed 7-digit chunks, so OJP fell through to
        # 3-dp lat/lon while OTP matched on UIC → different tokens. The
        # v0.1.35.03 fix reconstructs the UIC by prepending `850`.
        # Numbers transcribed from the live JSON Patrick captured via
        # the {} button shipped in v0.1.35.02.
        otp_itin = [
            _walk_leg(  # Origin → Bern
                from_lat=46.949113,
                from_lon=7.438483,
                to_lat=46.9485674,
                to_lon=7.4368288,
            ),
            _rail_leg(  # IR15 Bern → Geneva (direct, 2h)
                from_stop_id="SBB:8507000:0:7",
                to_stop_id="SBB:8501008:0:3",
                from_lat=46.9485674,
                from_lon=7.4368288,
                to_lat=46.2105224,
                to_lon=6.1424194,
                route_short_name="IR15",
                departure="2026-05-20T05:04:00+00:00",
                arrival="2026-05-20T07:05:00+00:00",
            ),
            _walk_leg(  # Geneva → Destination
                from_lat=46.2105224,
                from_lon=6.1424194,
                to_lat=46.208942,
                to_lon=6.145262,
            ),
        ]
        ojp_itin = [
            _walk_leg(  # OJP access walk Bern Hbf surface → platform
                from_lat=46.94911,
                from_lon=7.43848,
                to_lat=46.94857,
                to_lon=7.43683,
            ),
            _rail_leg(  # IR15 Bern → Geneva with OJP SLOID
                from_stop_id="ch:1:sloid:7000:4:7",  # → UIC 8507000
                to_stop_id="ch:1:sloid:1008:2:3",  # → UIC 8501008
                from_lat=46.94857,
                from_lon=7.43683,
                to_lat=46.21052,
                to_lon=6.14242,
                route_short_name="IR15",
                departure="2026-05-20T05:04:00+00:00",
                arrival="2026-05-20T07:05:00+00:00",
            ),
            _walk_leg(  # OJP egress walk Geneva platform → Genève surface
                from_lat=46.21052,
                from_lon=6.14242,
                to_lat=46.20894,
                to_lon=6.14526,
            ),
        ]
        assert transit_fingerprint(otp_itin) == transit_fingerprint(ojp_itin)
        assert transit_fingerprint(otp_itin) != ""


# ─────────────────── _build_comparison ───────────────────


def _merged_trip(legs: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the merged_trip shape `journey.fanout` produces — only
    `best.legs` is read by `_build_comparison`, the rest is irrelevant
    here."""
    return {"best": {"legs": legs}}


def _ojp_ref(*trips: list[dict[str, Any]]) -> dict[str, Any]:
    return {"status": "ok", "trips": [{"legs": legs} for legs in trips]}


class TestBuildComparison:
    def test_none_when_no_ojp_reference(self):
        assert _build_comparison([_merged_trip([_rail_leg()])], None) is None

    def test_none_when_ojp_status_not_ok(self):
        ref = {"status": "rate_limited", "trips": []}
        assert _build_comparison([_merged_trip([_rail_leg()])], ref) is None

    def test_all_three_buckets(self):
        # OTP found IC1 and IR99 (the latter not in OJP).
        # OJP found IC1 and R3 (the latter not in OTP).
        # → 1 common, 1 OTP-only, 1 OJP-only.
        otp = [
            _merged_trip([_rail_leg(route_short_name="IC1")]),
            _merged_trip([_rail_leg(route_short_name="IR99")]),
        ]
        ref = _ojp_ref(
            [_rail_leg(route_short_name="IC1")],
            [_rail_leg(route_short_name="R3")],
        )
        summary = _build_comparison(otp, ref)
        assert summary == {"common": 1, "otp_only": 1, "ojp_only": 1}
        # Per-trip tags attached for the UI badge.
        tags_otp = [t["comparison"] for t in otp]
        assert tags_otp == ["common", "otp_only"]
        tags_ojp = [t["comparison"] for t in ref["trips"]]
        assert tags_ojp == ["common", "ojp_only"]

    def test_walk_only_otp_trip_tagged_uncomparable(self):
        # Degenerate case: an OTP itinerary that's all walking (very
        # short stop-to-stop in walking distance). Its fingerprint is
        # the empty string, which would create false matches without
        # the special-case in _build_comparison.
        otp = [_merged_trip([_walk_leg()])]
        ref = _ojp_ref([_rail_leg()])
        summary = _build_comparison(otp, ref)
        # Walk-only OTP trip is uncomparable; not counted as common
        # despite both fingerprints being "" in the naive read.
        assert summary == {"common": 0, "otp_only": 0, "ojp_only": 1}
        assert otp[0]["comparison"] == "uncomparable"

    def test_cross_engine_match_via_uic(self):
        # The integration test: OJP renders end-walks and uses opaque
        # stop ids; OTP (stop-id routing) doesn't. _build_comparison
        # should still bucket them as common because the parser
        # reconstructs OJP's 4-digit Swiss DSN (DiDok-Nummer) into the
        # full 7-digit UIC by prepending `850`, so OJP "ch:1:sloid:7000"
        # matches OTP "SBB:8507000" — both yield UIC:8507000. This is
        # the v0.1.35.03 fix that closes the v0.1.35.02 regression.
        otp = [_merged_trip([_rail_leg(from_stop_id="SBB:8507000", to_stop_id="SBB:8503000")])]
        ref = _ojp_ref(
            [
                _walk_leg(),
                _rail_leg(
                    from_stop_id="ch:1:sloid:7000:0:8",  # Bern DSN → 8507000
                    to_stop_id="ch:1:sloid:3000:0:33",  # Zürich HB DSN → 8503000
                ),
                _walk_leg(),
            ]
        )
        summary = _build_comparison(otp, ref)
        assert summary == {"common": 1, "otp_only": 0, "ojp_only": 0}
        assert otp[0]["comparison"] == "common"
        assert ref["trips"][0]["comparison"] == "common"


# ─────────────────── module wiring sanity ───────────────────


def test_signature_module_exports_both_helpers():
    """Smoke test: the new helper is importable, the old one still is."""
    from app.journey import signature

    assert callable(signature.transit_fingerprint)
    assert callable(signature.trip_signature)


@pytest.mark.parametrize(
    "comparison_tag, css_class",
    [
        ("common", "common"),
        ("otp_only", "otp-only"),
        ("ojp_only", "ojp-only"),
        ("uncomparable", "uncomparable"),
    ],
)
def test_comparison_tag_kebab_case_mapping(comparison_tag: str, css_class: str) -> None:
    """The journey.html JS does `tag.replace('_', '-')` to derive the
    CSS class. This documents the contract — if a new bucket is added
    server-side, both sides must agree."""
    assert comparison_tag.replace("_", "-") == css_class
