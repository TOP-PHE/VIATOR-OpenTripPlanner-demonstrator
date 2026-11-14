"""PR-196a — unit tests for the ÖBB alignment scorer + the UIC helper.

Three concern groups:

  1. `extract_uic` — canonical UIC extraction from every observed
     stop_id shape (MOTIS ScheduledStopPoint, HAFAS lid, SNCF
     8-digit, junk, None).
  2. `compute_alignment` — the (VIATOR, ÖBB) → (score, tier, detail)
     pipeline, scored by the matching engine since #304: pairs by verdict,
     the high-frequency-corridor guard (M2), ÖBB's window (M9),
     one-sided / no-service edge cases. The cases whose result #304
     changed say so, with the decision behind it.
  3. Tier mapping — boundary checks (1.00, 0.99, 0.70, 0.69, 0.40,
     0.39, 0.0) so a future refactor that nudges a threshold breaks
     a single test rather than silently shifting cell colours.

Since #303 PR 4a both sides are canonical journeys: VIATOR's built from the old leg dicts
(`canonical_fixtures.journey_from_legs`, whose bridge gives those legs back), ÖBB's from a HAFAS
answer through the real adapter (`canonical_fixtures.oebb_journeys`). Every score and tier below
is the one these tests pinned on the old dicts and `VerifyItinerary` (design D5): this file is the
characterisation of the port.

Out of scope (covered elsewhere):
  - ÖBB's answer → journeys → the scorer's legs → tests/unit/test_oebb_journeys.py
  - Sweep dispatch + persistence → tests/unit/test_coverage_external_verify_sweep.py
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app.journey.canonical import CanonicalJourney
from app.journey.canonical_adapters import hafas_adapter
from app.network_coverage import oebb_journeys
from app.network_coverage.alignment import compute_alignment, journeys_from_depart_at
from app.network_coverage.external_verify import extract_uic
from tests.unit import canonical_fixtures as fx

_VIENNA = ZoneInfo("Europe/Vienna")


def _vienna_local_to_utc(value: str) -> str:
    """A Vienna wall-clock `YYYY-MM-DDTHH:MM` as the UTC instant VIATOR's legs carry."""
    return datetime.fromisoformat(value).replace(tzinfo=_VIENNA).astimezone(UTC).isoformat()


# ─────────────────────── extract_uic ───────────────────────


@pytest.mark.parametrize(
    ("stop_id", "expected"),
    [
        # MOTIS / GTFS-flavoured form — straight 7-digit UIC.
        ("ScheduledStopPoint:8503000", "UIC:8503000"),
        # HAFAS simple lid form (the common shape in `common.locL`
        # entries that don't carry an explicit X/Y coord).
        ("A=1@L=8507000@", "UIC:8507000"),
        # SBB OTP form with platform suffix.
        ("SBB:8507000:0:7", "UIC:8507000"),
        # SNCF 8-digit (UIC + check digit) → keep first 7.
        ("StopPoint:OCELyria-87686006", "UIC:8768600"),
        # ÖBB lid form.
        ("A=1@L=8100002@", "UIC:8100002"),
    ],
)
def test_extract_uic_recovers_canonical_token(stop_id: str, expected: str) -> None:
    """Every shape a leg endpoint might carry must reduce to the same
    canonical UIC token so the alignment scorer's exact-match path sees
    the SAME train described by VIATOR and ÖBB as identical."""
    assert extract_uic(stop_id) == expected


@pytest.mark.parametrize(
    "stop_id",
    [
        None,
        "",
        "no-digits-here",
        "shortid:123",  # 3 digits — below UIC width
        "verylong:123456789",  # 9 digits — above the regex anchor
    ],
)
def test_extract_uic_returns_none_on_unparseable(stop_id: str | None) -> None:
    """No-UIC inputs return None — the caller falls back to
    coordinate-based matching rather than panic-attributing the leg
    to a random stop."""
    assert extract_uic(stop_id) is None


# ─────────────────────── compute_alignment — edges ───────────────────────


def _viator_trip(legs: list[dict[str, Any]]) -> CanonicalJourney:
    """A VIATOR journey whose bridge legs are `legs` (only the legs matter to the scorer)."""
    return fx.journey_from_legs(legs)


def _v_leg(
    *,
    mode: str = "RAIL",
    from_uic: str = "8503000",
    to_uic: str = "8100002",
    dep: str = "2026-06-30T08:00",
    arr: str = "2026-06-30T13:30",
    route: str = "RJ 1141",
) -> dict:
    """Build a VIATOR-shaped leg with stop_ids that carry the UIC.

    `dep`/`arr` are given as Vienna-local wall-clock strings for
    readability — same literal format `_o_leg` takes — and converted to
    genuine UTC-instant ISO strings here, the shape real VIATOR leg
    timestamps actually carry in production (both OTP and MOTIS return
    offset-aware times; see `hafas_adapter.hafas_time_to_utc_iso` for the
    equivalent real conversion on the ÖBB side of the LIVE journey
    comparison, as opposed to the coverage sweep's intentionally-naive
    persistence this module reads). This means `_v_leg(dep=X)` and
    `_o_leg(dep=X)` called with the same literal X describe the SAME
    real train on the two DIFFERENT wire-format bases each production
    consumer actually sees — reusing the identical naive string for
    both (the pre-fix version of this fixture) was the reason the
    cross-engine timezone mismatch went undetected by this test file."""
    return {
        "mode": mode,
        "from_stop_id": f"SBB:{from_uic}:0:5",
        "to_stop_id": f"SBB:{to_uic}:0:5",
        "departure": _vienna_local_to_utc(dep),
        "arrival": _vienna_local_to_utc(arr),
        "route_short_name": route,
    }


def _o_itin(*, legs: list[dict[str, Any]]) -> CanonicalJourney:
    """An ÖBB journey from `_o_leg` specs, through a HAFAS answer and the real adapter."""
    (journey,) = fx.oebb_journeys(legs)
    return journey


def _o_leg(
    *,
    mode: str = "RAIL",
    from_uic: str | None = "UIC:8503000",
    to_uic: str | None = "UIC:8100002",
    dep: str = "2026-06-30T08:00",
    arr: str = "2026-06-30T13:30",
    route: str = "RJ 1141",
) -> dict[str, Any]:
    """An ÖBB leg: the stop tokens its ids give, Vienna wall-clock times (ÖBB's form), the label
    HAFAS shows. Default mode RAIL is a `RJ` product, a typical long-distance category."""
    return {
        "mode": mode,
        "from_uic": from_uic,
        "to_uic": to_uic,
        "dep": dep,
        "arr": arr,
        "route": route,
    }


def _scored(
    viator: list[CanonicalJourney], oebb: list[CanonicalJourney]
) -> tuple[float | None, str]:
    """The cell's score and tier (`compute_alignment`'s detail read where a test needs it)."""
    found = compute_alignment(viator, oebb)
    return found.score, found.tier


# ───────────────── endpoint identity: tokens, not coordinates ─────────────────
#
# These four are the assertions that separate this design from PR #226. #226
# made matching MORE permissive by deleting the endpoint guard, and was
# rejected because it traded a VISIBLE false negative for INVISIBLE false
# positives. Nothing here deletes a discriminator; the guard is fed the input
# it was always meant to read.


def test_two_id_less_itineraries_do_not_exact_match() -> None:
    """**The #226 test — read this one first.**

    A VIATOR leg with no stop_id AND no coordinates fingerprints its endpoints
    as the literal `"?,?"`. The ÖBB scorer legs carry no lat/lon at all, so if the
    ÖBB side passed a bare None it would produce the SAME `"?,?"` — and with equal
    mode, equal rounded minutes and equal route name the two would hash
    identically and score a 1.00 `agree` on zero endpoint evidence.

    `oebb_journeys.UNKNOWN_ENDPOINT` is what makes that impossible. Note
    `test_compute_alignment_empty_route_same_minute_still_exact_matches` proves
    an empty route name does NOT save us here — only the sentinel does.
    """
    v_leg = {
        "mode": "RAIL",
        "from_stop_id": None,
        "to_stop_id": None,
        "from_lat": None,
        "from_lon": None,
        "to_lat": None,
        "to_lon": None,
        "departure": _vienna_local_to_utc("2026-06-30T08:00"),
        "arrival": _vienna_local_to_utc("2026-06-30T13:30"),
        "route_short_name": "RJ 1141",
    }
    o_it = _o_itin(legs=[_o_leg(from_uic=None, to_uic=None)])

    score, tier = _scored([_viator_trip([v_leg])], [o_it])

    assert score != 1.0, "two id-less itineraries must not be credited as identical"
    assert tier != "agree"


def test_guard_rejects_cross_namespace_endpoints() -> None:
    """Matchable on every axis except identity — same train number, same
    minute, same mode — and still refused, because ÖBB named a station we
    could not place in the shared namespace (Wien Hbf's bus terminal)."""
    v = [_viator_trip([_v_leg(from_uic="8100002", to_uic="8103000")])]
    o = [_o_itin(legs=[_o_leg(from_uic="OEBB:904050", to_uic="OEBB:904062")])]

    score, tier = _scored(v, o)

    assert score == 0.0
    assert tier == "no_overlap"


def test_guard_accepts_when_both_sides_reduce_to_the_same_uic() -> None:
    """The one case the fix is allowed to NEWLY accept — without it the fix is
    unfalsifiable. The ÖBB side is built through the real adapter from a
    zero-padded extId, so this exercises the whole path, not just the scorer.
    Both stops are ones whose number ÖBB states as their UIC (`gidL` `U\u00d7…`, measured
    2026-11-12): only those reach the UIC namespace (`test_oebb_stop_numbers.py`)."""
    section = {
        "type": "JNY",
        "dep": {"locX": 0, "dTimeS": "080000"},
        "arr": {"locX": 1, "aTimeS": "133000"},
        "jny": {"prodX": 0},
    }
    connection = {"date": "20260630", "dur": "053000", "chg": 0, "secL": [section]}
    connection.update(dep=section["dep"], arr=section["arr"])
    payload = fx._hafas_payload(
        [connection],
        {
            "locL": [
                {
                    "lid": "A=1@O=Basel SBB@X=7589563@L=008500010@",
                    "extId": "008500010",
                    "gidL": ["U\u00d78500010"],
                },
                {
                    "lid": "A=1@O=Zuerich HB@X=8540192@L=8503000@",
                    "extId": "8503000",
                    "gidL": ["U\u00d78503000"],
                },
            ],
            "prodL": [{"name": "RJ 1141", "prodCtx": {"catOut": "RJ"}}],
        },
    )
    (o_journey,) = hafas_adapter.journeys_from_trip_search(payload)
    (o_leg,) = oebb_journeys.scoring_legs(o_journey)
    assert o_leg["from_stop_id"] == "UIC:8500010", "zero padding absorbed"

    v = [_viator_trip([_v_leg(from_uic="8500010", to_uic="8503000")])]
    _score, tier = _scored(v, [o_journey])

    assert tier == "agree"


def test_an_oebb_token_compares_as_an_unknown_station() -> None:
    """Since #304 the engine compares the stops: an `OEBB:` token is UNKNOWN (design §6), and
    the coverage profile refuses an unknown endpoint (M4), so it can never stand for a UIC."""
    v = [_viator_trip([_v_leg(from_uic="8100002", to_uic="8503000")])]
    o = [_o_itin(legs=[_o_leg(from_uic="OEBB:904050", to_uic="UIC:8503000")])]

    found = compute_alignment(v, o)

    assert (found.score, found.tier) == (0.0, "no_overlap")
    assert found.detail["reasons"].get("endpoint_unresolved") == 1


def test_compute_alignment_both_empty_is_no_service() -> None:
    """Neither planner found anything → 'no_service' with NULL score.
    The matrix UI renders this as informational grey, not red — a real
    'no Sunday service' answer isn't a data gap."""
    score, tier = _scored([], [])
    assert score is None
    assert tier == "no_service"


def test_compute_alignment_viator_only_is_one_sided_viator() -> None:
    """VIATOR returned trips, ÖBB returned nothing → 'one_sided_viator'
    with score=0.0. Operator now knows VIATOR's confident, ÖBB doesn't
    even have the corridor."""
    v = [_viator_trip([_v_leg()])]
    score, tier = _scored(v, [])
    assert score == 0.0
    assert tier == "one_sided_viator"


def test_compute_alignment_oebb_only_is_one_sided_oebb() -> None:
    """ÖBB has the route, VIATOR doesn't — the classic 'likely VIATOR
    data gap' case the operator wants surfaced."""
    o = [_o_itin(legs=[_o_leg()])]
    score, tier = _scored([], o)
    assert score == 0.0
    assert tier == "one_sided_oebb"


# ─────────────────────── compute_alignment — scoring ───────────────────────


def test_compute_alignment_exact_match_scores_one() -> None:
    """Same UIC endpoints + same train + same minute → exact
    fingerprint match → 1.00 → 'agree'. Pinned so a fingerprint refactor
    that drops the route_short_name input doesn't silently degrade
    every same-train comparison to a fuzzy 0.7."""
    v = [_viator_trip([_v_leg()])]
    o = [_o_itin(legs=[_o_leg()])]
    score, tier = _scored(v, o)
    assert score == 1.0
    assert tier == "agree"


def test_a_viator_journey_half_an_hour_before_oebb_s_is_outside_its_window() -> None:
    """M9 (#304): ÖBB's span is its first boardings, ±2 min. A VIATOR journey half an hour
    before is outside it: counted, never compared, so the cell is ÖBB's alone. (Before #304:
    `no_overlap`, the two journeys compared and found different.)"""
    v = [_viator_trip([_v_leg(route="", dep="2026-06-30T08:00", arr="2026-06-30T13:30")])]
    o = [_o_itin(legs=[_o_leg(route="", dep="2026-06-30T08:30", arr="2026-06-30T13:00")])]

    found = compute_alignment(v, o)

    assert (found.score, found.tier) == (0.0, "one_sided_oebb")
    assert found.detail["counts"]["outside_reference_window"] == 1
    assert found.detail["reasons"] == {"outside_reference_window": 1}


def test_compute_alignment_empty_route_same_minute_still_exact_matches() -> None:
    """Documents a real edge of the scorer: empty `route_short_name` on
    both sides DOES exact-fingerprint-match if endpoints + timestamps
    align — empty string is a stable hash key, not a wildcard.

    The high-frequency-corridor false-positive (e.g. TGV every 30 min)
    is therefore NOT guarded by this path. The guard lives on the
    FUZZY path, which requires a matching non-empty train number — see
    test_compute_alignment_fuzzy_blocked_by_train_number_mismatch.

    Pinning so a future fingerprint refactor that drops empty strings
    or treats them as wildcards doesn't silently re-introduce the
    guard at the wrong layer."""
    v = [_viator_trip([_v_leg(route="")])]
    o = [_o_itin(legs=[_o_leg(route="")])]
    score, tier = _scored(v, o)
    assert score == 1.0
    assert tier == "agree"


def test_a_minute_of_jitter_with_the_same_train_is_probably_the_same() -> None:
    """One minute later at both ends, the same train number: `probably_same`, credit 0.7 →
    'mostly_agree'. (The ±5 min fuzzy tier of before is gone, M1: ±2 min.)"""
    v = [_viator_trip([_v_leg(dep="2026-06-30T08:00", arr="2026-06-30T13:30")])]
    o = [_o_itin(legs=[_o_leg(dep="2026-06-30T08:01", arr="2026-06-30T13:31")])]

    found = compute_alignment(v, o)

    assert (found.score, found.tier) == (0.7, "mostly_agree")
    assert found.detail["counts"]["probably_same"] == 1


def test_three_minutes_of_jitter_is_outside_the_tolerance() -> None:
    """M1 (#304): ±2 min. Three minutes later is outside ÖBB's window (M9) too: the cell is
    ÖBB's alone. (Before #304 the ±5 min fuzzy tier credited it 0.7.)"""
    v = [_viator_trip([_v_leg(dep="2026-06-30T08:00", arr="2026-06-30T13:30")])]
    o = [_o_itin(legs=[_o_leg(dep="2026-06-30T08:03", arr="2026-06-30T13:33")])]

    assert _scored(v, o) == (0.0, "one_sided_oebb")


def test_different_numbers_two_minutes_apart_never_pair() -> None:
    """The high-frequency corridor guard (M2): two trains with different numbers that are not
    the same minute at both ends are vetoed."""
    v = [_viator_trip([_v_leg(route="TGV 9582", dep="2026-06-30T08:00", arr="2026-06-30T13:30")])]
    o = [_o_itin(legs=[_o_leg(route="TGV 9586", dep="2026-06-30T08:02", arr="2026-06-30T13:32")])]

    found = compute_alignment(v, o)

    assert (found.score, found.tier) == (0.0, "no_overlap")
    assert found.detail["reasons"].get("train_number_differs") == 1


def test_different_numbers_at_the_same_minutes_are_probably_the_same() -> None:
    """M2 as amended and accepted (owner, 2026-11-05): a different number on a leg that is the
    same minute at both ends on both sides is a warning, not a veto (Eurostar's 94xx / 93xx,
    renumbered TGVs). (Before #304: `no_overlap`.)"""
    v = [_viator_trip([_v_leg(route="TGV 9582", dep="2026-06-30T08:00")])]
    o = [_o_itin(legs=[_o_leg(route="TGV 9586", dep="2026-06-30T08:00")])]

    found = compute_alignment(v, o)

    assert (found.score, found.tier) == (0.7, "mostly_agree")
    assert found.detail["reasons"].get("train_number_differs_same_minutes") == 1


def _elsewhere(dep: str, arr: str) -> CanonicalJourney:
    """An ÖBB connection between two other stations: it widens ÖBB's span, pairs with nothing."""
    return _o_itin(
        legs=[
            _o_leg(route="RJ 1199", from_uic="UIC:8100013", to_uic="UIC:8100173", dep=dep, arr=arr)
        ]
    )


def test_compute_alignment_partial_score_below_mostly_threshold() -> None:
    """3 VIATOR journeys within ÖBB's span, 1 pair → 1/3 = 0.33 → 'disagree'."""
    v = [
        _viator_trip([_v_leg(route="RJ 1141", dep="2026-06-30T08:00", arr="2026-06-30T13:30")]),
        _viator_trip([_v_leg(route="RJ 1143", dep="2026-06-30T08:30", arr="2026-06-30T14:00")]),
        _viator_trip([_v_leg(route="RJ 1145", dep="2026-06-30T09:00", arr="2026-06-30T14:30")]),
    ]
    o = [
        _o_itin(legs=[_o_leg(route="RJ 1141", dep="2026-06-30T08:00", arr="2026-06-30T13:30")]),
        _elsewhere("2026-06-30T09:00", "2026-06-30T11:00"),
    ]

    assert _scored(v, o) == (0.33, "disagree")


def test_compute_alignment_partial_tier_at_two_of_three() -> None:
    """2 pairs over three journeys a side → 2/3 = 0.67 → 'partial'."""
    v = [
        _viator_trip([_v_leg(route="RJ 1141", dep="2026-06-30T08:00", arr="2026-06-30T13:30")]),
        _viator_trip([_v_leg(route="RJ 1143", dep="2026-06-30T08:30", arr="2026-06-30T14:00")]),
        _viator_trip([_v_leg(route="RJ 1145", dep="2026-06-30T09:00", arr="2026-06-30T14:30")]),
    ]
    o = [
        _o_itin(legs=[_o_leg(route="RJ 1141", dep="2026-06-30T08:00", arr="2026-06-30T13:30")]),
        _o_itin(legs=[_o_leg(route="RJ 1143", dep="2026-06-30T08:30", arr="2026-06-30T14:00")]),
        _elsewhere("2026-06-30T09:00", "2026-06-30T11:00"),
    ]

    assert _scored(v, o) == (0.67, "partial")


def test_viator_journeys_after_oebb_s_last_connection_are_not_counted() -> None:
    """M9 (#304): ÖBB answers 5 connections from the run's departure time, VIATOR covers the
    day. The two VIATOR journeys after ÖBB's last one say nothing either way: the cell is the
    one pair, 'agree', and the detail counts the two. (Before #304: 1/3 = 0.33, 'disagree'.)"""
    v = [
        _viator_trip([_v_leg(route="RJ 1141", dep="2026-06-30T08:00")]),
        _viator_trip([_v_leg(route="RJ 1143", dep="2026-06-30T09:00")]),
        _viator_trip([_v_leg(route="RJ 1145", dep="2026-06-30T10:00")]),
    ]
    o = [_o_itin(legs=[_o_leg(route="RJ 1141", dep="2026-06-30T08:00")])]

    found = compute_alignment(v, o)

    assert (found.score, found.tier) == (1.0, "agree")
    assert found.detail["counts"] == {
        "same_service": 1,
        "probably_same": 0,
        "viator_only": 0,
        "oebb_only": 0,
        "uncomparable": 0,
        "outside_reference_window": 2,
    }
    assert found.detail["engine_version"] == 1
    assert found.detail["profile"] == "coverage_alignment"


def test_compute_alignment_strips_walk_legs_before_match() -> None:
    """An ÖBB itinerary with an explicit WALK leg wrapping the transfer
    must STILL exact-match a VIATOR trip that doesn't model the walk.
    Without the strip both sides' fingerprints diverge and the
    alignment goes to 0.0 — a class of false negatives PR-196a
    is meant to eliminate."""
    v = [
        _viator_trip(
            [
                _v_leg(
                    route="RJ 1141",
                    dep="2026-06-30T08:00",
                    arr="2026-06-30T10:00",
                    from_uic="8503000",
                    to_uic="8100100",
                ),
                _v_leg(
                    route="RJ 1143",
                    dep="2026-06-30T10:15",
                    arr="2026-06-30T13:30",
                    from_uic="8100100",
                    to_uic="8100002",
                ),
            ]
        )
    ]
    # ÖBB: same two transit legs PLUS a WALK between them.
    o = [
        _o_itin(
            legs=[
                _o_leg(
                    mode="RAIL",
                    route="RJ 1141",
                    from_uic="UIC:8503000",
                    to_uic="UIC:8100100",
                    dep="2026-06-30T08:00",
                    arr="2026-06-30T10:00",
                ),
                _o_leg(
                    mode="WALK",
                    from_uic="UIC:8100100",
                    to_uic="UIC:8100100",
                    dep="2026-06-30T10:00",
                    arr="2026-06-30T10:15",
                    route="",
                ),
                _o_leg(
                    mode="RAIL",
                    route="RJ 1143",
                    from_uic="UIC:8100100",
                    to_uic="UIC:8100002",
                    dep="2026-06-30T10:15",
                    arr="2026-06-30T13:30",
                ),
            ]
        )
    ]
    score, tier = _scored(v, o)
    assert score == 1.0
    assert tier == "agree"


def test_compute_alignment_caps_score_at_one_when_overcounting() -> None:
    """3 VIATOR trips, 1 ÖBB trip, all matching same fingerprint —
    without the per-side cap one ÖBB row could absorb only one match.
    With the score=min(score, 1.0) clamp, the result saturates at
    'agree' rather than overshooting. Defensive guard."""
    leg = _v_leg(route="RJ 1141", dep="2026-06-30T08:00")
    v = [_viator_trip([leg]), _viator_trip([leg]), _viator_trip([leg])]
    o = [_o_itin(legs=[_o_leg(route="RJ 1141", dep="2026-06-30T08:00")])]
    score, tier = _scored(v, o)
    # Exact match consumes the single ÖBB row once; remaining 2 VIATOR
    # trips fuzzy-match against an already-matched ÖBB row → no further
    # credit. denominator = max(min(3,3), min(1,3)) = 3 → 1/3 = 0.33.
    # This validates the algorithm doesn't double-credit a single ÖBB
    # row to multiple VIATOR trips.
    assert score == 0.33
    assert tier == "disagree"


# ─────────────── ÖBB's Vienna wall clock (cross-engine timezone fix, #220) ───────────────


def test_oebb_times_reach_the_scorer_in_utc_in_summer() -> None:
    """June is CEST (UTC+2) — 08:00 Vienna-local is 06:00 UTC."""
    (leg,) = oebb_journeys.scoring_legs(_o_itin(legs=[_o_leg(dep="2026-06-30T08:00")]))
    assert leg["departure"] == "2026-06-30T06:00:00+00:00"


def test_oebb_times_reach_the_scorer_in_utc_in_winter() -> None:
    """January is CET (UTC+1), not CEST — the conversion must be
    DST-aware (via zoneinfo), not a hardcoded +2h offset."""
    (leg,) = oebb_journeys.scoring_legs(
        _o_itin(legs=[_o_leg(dep="2026-01-15T08:00", arr="2026-01-15T13:30")])
    )
    assert leg["departure"] == "2026-01-15T07:00:00+00:00"


def test_compute_alignment_exact_match_across_summer_dst_boundary() -> None:
    """Regression lock for the cross-engine timezone bug: `_o_leg`'s
    ÖBB answer carries Vienna wall clock (as ÖBB sends it), while `_v_leg`
    carries genuine UTC. Before #220 the exact fingerprint compared
    VIATOR's raw UTC digits against ÖBB's raw local digits verbatim — a
    same-train, same-minute pair like this one would NOT have exact-matched
    (fingerprints differ whenever the CEST offset is nonzero), silently
    scoring 'no_overlap' instead of 'agree'. The HAFAS adapter now makes
    ÖBB's times UTC once."""
    v = [_viator_trip([_v_leg(dep="2026-06-30T08:00", arr="2026-06-30T13:30")])]
    o = [_o_itin(legs=[_o_leg(dep="2026-06-30T08:00", arr="2026-06-30T13:30")])]
    score, tier = _scored(v, o)
    assert score == 1.0
    assert tier == "agree"


def test_a_minute_of_jitter_survives_the_cest_offset() -> None:
    """Same regression, with jitter: a minute later on the SAME real train still pairs once both
    sides are on the same clock basis; before #220 the ~2 h CEST skew put it far outside any
    tolerance."""
    v = [_viator_trip([_v_leg(dep="2026-06-30T08:00", arr="2026-06-30T13:30")])]
    o = [_o_itin(legs=[_o_leg(dep="2026-06-30T08:01", arr="2026-06-30T13:31")])]

    assert _scored(v, o) == (0.7, "mostly_agree")


# ─────────── journeys_from_depart_at (VIATOR/ÖBB search-scope fix) ───────────


def _trip(dep_iso: str | None) -> CanonicalJourney:
    """A journey departing (door to door) at `dep_iso`; no departure at all when None."""
    leg = {"mode": "RAIL", "from_stop_id": "SBB:8503000:0:5", "to_stop_id": "SBB:8100002:0:5"}
    if dep_iso is not None:
        leg.update(departure=dep_iso, arrival=dep_iso)
    return fx.journey_from_legs([leg])


def _departures(journeys: list[CanonicalJourney]) -> list[str]:
    return [j.departure.timetabled.isoformat() for j in journeys if j.departure is not None]


def test_journeys_from_depart_at_drops_earlier_journeys() -> None:
    """The bug this fixes: a cell's VIATOR trips span the WHOLE K-slot
    day window, while ÖBB's verify-sweep call is one forward-looking
    search anchored at depart_at — so trips departing before that
    anchor were never comparable to what ÖBB was asked and must be
    excluded, not just deprioritised."""
    depart_at = datetime(2026, 7, 13, 6, 0, tzinfo=UTC)
    journeys = [
        _trip("2026-07-13T02:34:00+00:00"),
        _trip("2026-07-13T05:59:00+00:00"),
        _trip("2026-07-13T06:00:00+00:00"),
        _trip("2026-07-13T07:20:00+00:00"),
    ]
    out = journeys_from_depart_at(journeys, depart_at)
    assert _departures(out) == ["2026-07-13T06:00:00+00:00", "2026-07-13T07:20:00+00:00"]


def test_journeys_from_depart_at_sorts_chronologically() -> None:
    """Kept journeys come back earliest-first regardless of input order —
    matching HAFAS's own next-N-departures-from-depart_at ordering, not
    VIATOR's internal quality/duration rank_in_response order."""
    depart_at = datetime(2026, 7, 13, 6, 0, tzinfo=UTC)
    journeys = [
        _trip("2026-07-13T09:00:00+00:00"),
        _trip("2026-07-13T06:05:00+00:00"),
        _trip("2026-07-13T07:30:00+00:00"),
    ]
    out = journeys_from_depart_at(journeys, depart_at)
    assert _departures(out) == [
        "2026-07-13T06:05:00+00:00",
        "2026-07-13T07:30:00+00:00",
        "2026-07-13T09:00:00+00:00",
    ]


def test_journeys_from_depart_at_drops_a_journey_without_departure() -> None:
    depart_at = datetime(2026, 7, 13, 6, 0, tzinfo=UTC)
    assert journeys_from_depart_at([_trip(None)], depart_at) == []


def test_journeys_from_depart_at_empty_input() -> None:
    assert journeys_from_depart_at([], datetime(2026, 7, 13, 6, 0, tzinfo=UTC)) == []
