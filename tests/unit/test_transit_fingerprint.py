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

    def test_ojp_sloid_yields_same_uic_as_otp(self):
        # The whole point of v0.1.35.02: OTP's "SBB:8501120:0:5" and
        # OJP's "ch:1:sloid:8501120:0:5" describe the same physical
        # Lausanne CFF platform; both must produce the UIC token
        # "UIC:8501120" so the fingerprint matches across engines.
        otp = transit_fingerprint([_rail_leg(from_stop_id="SBB:8501120:0:5")])
        ojp = transit_fingerprint([_rail_leg(from_stop_id="ch:1:sloid:8501120:0:5")])
        assert otp == ojp

    def test_no_uic_falls_back_to_3dp_latlon(self):
        # Non-Swiss feed (no 7-digit UIC chunk in the stop_id) falls
        # back to lat/lon. Two synthetic ids with the same lat/lon must
        # still match.
        a = transit_fingerprint(
            [
                _rail_leg(
                    from_stop_id="STIB:1234",
                    to_stop_id="STIB:5678",
                    from_lat=50.85,
                    from_lon=4.35,
                )
            ]
        )
        b = transit_fingerprint(
            [
                _rail_leg(
                    from_stop_id="MIVB:abc",
                    to_stop_id="MIVB:def",
                    from_lat=50.85,
                    from_lon=4.35,
                )
            ]
        )
        assert a == b  # Different stop_ids, same coords → match via fallback

    def test_pontarlier_french_station_in_sbb_feed(self):
        # Pontarlier is a French SNCF station present in the cross-
        # border SBB feed (route 91-P38). OTP emits it as
        # "SBB:8771500"; OJP via opentransportdata.swiss emits it as
        # "ch:1:sloid:8771500" or similar. Both yield UIC:8771500.
        otp = transit_fingerprint([_rail_leg(from_stop_id="SBB:8771500", route_short_name="P38")])
        ojp = transit_fingerprint(
            [_rail_leg(from_stop_id="ch:1:sloid:8771500:0:1", route_short_name="P38")]
        )
        assert otp == ojp


class TestCrossEngineMatching:
    """The centrepiece: OJP and OTP shapes of the *same train* must
    fingerprint identically, even though they differ in stop_id
    namespace and walk-leg presence."""

    def test_ojp_with_end_walks_matches_otp_without(self):
        # OJP shape: end-walks framing the transit leg, opaque stop ids
        # (ch:1:sloid:NNNNNNN:…). OTP-with-stop-id-routing shape: just
        # the transit leg, OTP-namespaced stop ids (SBB:NNNNNNN). Both
        # report the same physical Bern→Zürich at 08:31→09:28, and both
        # ids carry the same UIC chunk (8507000 / 8503000) so the
        # fingerprint matches via the UIC token.
        ojp_itin = [
            _walk_leg(  # Origin → Bern (the access walk OJP renders)
                from_lat=46.94884,
                from_lon=7.43912,
                to_lat=46.94864,
                to_lon=7.43677,
            ),
            _rail_leg(
                from_stop_id="ch:1:sloid:8507000:0:8",  # OJP stop reference
                to_stop_id="ch:1:sloid:8503000:0:33",
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
        ojp_itin = [
            _walk_leg(),  # Origin → Frasne access walk
            _rail_leg(  # TGV with OJP-namespaced ids + station-centroid coords
                from_stop_id="ch:1:sloid:8771513:0:1",
                to_stop_id="ch:1:sloid:8501120:0:5",
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
                from_stop_id="ch:1:sloid:8501120:0:4",
                to_stop_id="ch:1:sloid:8501008:0:3",
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
        # should still bucket them as common because both stop ids
        # carry the same 7-digit UIC chunk (8507000 / 8503000) — the
        # cross-engine matching mechanism v0.1.35.02 ships.
        otp = [_merged_trip([_rail_leg(from_stop_id="SBB:8507000", to_stop_id="SBB:8503000")])]
        ref = _ojp_ref(
            [
                _walk_leg(),
                _rail_leg(
                    from_stop_id="ch:1:sloid:8507000:0:8",
                    to_stop_id="ch:1:sloid:8503000:0:33",
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
