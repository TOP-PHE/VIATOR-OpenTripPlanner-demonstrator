"""PR-196a — unit tests for the ÖBB alignment scorer + the UIC helper.

Three concern groups:

  1. `extract_uic` — canonical UIC extraction from every observed
     stop_id shape (MOTIS ScheduledStopPoint, HAFAS lid, SNCF
     8-digit, junk, None).
  2. `compute_alignment` — the (VIATOR, ÖBB) → (score, tier)
     pipeline: exact-fingerprint match, train-number-guarded fuzzy
     fallback, the high-frequency-corridor-false-positive guard,
     one-sided / no-service edge cases.
  3. Tier mapping — boundary checks (1.00, 0.99, 0.70, 0.69, 0.40,
     0.39, 0.0) so a future refactor that nudges a threshold breaks
     a single test rather than silently shifting cell colours.

Out of scope (covered elsewhere):
  - HAFAS payload parsing → tests/unit/test_external_verify.py
  - Sweep dispatch + persistence → tests/unit/test_coverage_external_verify_sweep.py
"""

from __future__ import annotations

import pytest

from app.network_coverage.alignment import compute_alignment
from app.network_coverage.external_verify import VerifyItinerary, VerifyLeg, extract_uic

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


def _viator_trip(legs: list[dict]) -> dict:
    """Minimal VIATOR trip dict — only `legs` matters to the scorer."""
    return {"legs": legs}


def _v_leg(
    *,
    mode: str = "RAIL",
    from_uic: str = "8503000",
    to_uic: str = "8100002",
    dep: str = "2026-06-30T08:00",
    arr: str = "2026-06-30T13:30",
    route: str = "RJ 1141",
) -> dict:
    """Build a VIATOR-shaped leg with stop_ids that carry the UIC."""
    return {
        "mode": mode,
        "from_stop_id": f"SBB:{from_uic}:0:5",
        "to_stop_id": f"SBB:{to_uic}:0:5",
        "departure": dep,
        "arrival": arr,
        "route_short_name": route,
    }


def _o_itin(*, legs: list[VerifyLeg]) -> VerifyItinerary:
    """Build an ÖBB-side VerifyItinerary from raw VerifyLeg list."""
    return VerifyItinerary(
        legs=legs,
        departure_at=legs[0].dep_utc if legs else None,
        arrival_at=legs[-1].arr_utc if legs else None,
        duration_seconds=0,
        num_transfers=max(len(legs) - 1, 0),
    )


def _o_leg(
    *,
    mode: str = "RAIL",
    from_uic: str = "UIC:8503000",
    to_uic: str = "UIC:8100002",
    dep: str = "2026-06-30T08:00",
    arr: str = "2026-06-30T13:30",
    route: str = "RJ 1141",
) -> VerifyLeg:
    """Build an ÖBB-side leg already in canonical-UIC form (the
    captured shape extract_uic emits). Default mode is RAIL — mirrors
    what the sweep persists for a JNY section with a typical
    long-distance rail product cat (ICE/RJ/EC/...)."""
    return VerifyLeg(
        mode=mode, from_uic=from_uic, to_uic=to_uic, dep_utc=dep, arr_utc=arr, route_name=route
    )


def test_compute_alignment_both_empty_is_no_service() -> None:
    """Neither planner found anything → 'no_service' with NULL score.
    The matrix UI renders this as informational grey, not red — a real
    'no Sunday service' answer isn't a data gap."""
    score, tier = compute_alignment([], [])
    assert score is None
    assert tier == "no_service"


def test_compute_alignment_viator_only_is_one_sided_viator() -> None:
    """VIATOR returned trips, ÖBB returned nothing → 'one_sided_viator'
    with score=0.0. Operator now knows VIATOR's confident, ÖBB doesn't
    even have the corridor."""
    v = [_viator_trip([_v_leg()])]
    score, tier = compute_alignment(v, [])
    assert score == 0.0
    assert tier == "one_sided_viator"


def test_compute_alignment_oebb_only_is_one_sided_oebb() -> None:
    """ÖBB has the route, VIATOR doesn't — the classic 'likely VIATOR
    data gap' case the operator wants surfaced."""
    o = [_o_itin(legs=[_o_leg()])]
    score, tier = compute_alignment([], o)
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
    score, tier = compute_alignment(v, o)
    assert score == 1.0
    assert tier == "agree"


def test_compute_alignment_no_train_and_different_minute_is_no_overlap() -> None:
    """Same endpoints + DIFFERENT minute + no train number on either
    side → exact fingerprint differs (timestamps differ) AND the fuzzy
    fallback can't fire (no train identity to confirm) → no_overlap.

    This is the path the high-frequency-corridor guard relies on once
    train numbers are missing — without train identity, schedule skew
    is meaningless evidence in either direction."""
    v = [_viator_trip([_v_leg(route="", dep="2026-06-30T08:00", arr="2026-06-30T13:30")])]
    o = [_o_itin(legs=[_o_leg(route="", dep="2026-06-30T08:30", arr="2026-06-30T13:00")])]
    score, tier = compute_alignment(v, o)
    assert score == 0.0
    assert tier == "no_overlap"


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
    score, tier = compute_alignment(v, o)
    assert score == 1.0
    assert tier == "agree"


def test_compute_alignment_fuzzy_match_with_train_scores_partial() -> None:
    """Same endpoints + same train number + ±5min departure → fuzzy
    match → 0.70 credit → 'mostly_agree' (≥ 0.70 threshold). Operator
    sees this as 'almost certainly the same train, schedule jitter'
    rather than 'no overlap'."""
    v = [_viator_trip([_v_leg(dep="2026-06-30T08:00", arr="2026-06-30T13:30")])]
    # Same train number ("RJ 1141"), 3 min later departure — within ±5min.
    o = [_o_itin(legs=[_o_leg(dep="2026-06-30T08:03", arr="2026-06-30T13:33")])]
    score, tier = compute_alignment(v, o)
    assert score == 0.7
    assert tier == "mostly_agree"


def test_compute_alignment_fuzzy_blocked_by_train_number_mismatch() -> None:
    """Same endpoints + same departure but different train numbers →
    fuzzy match DECLINES. This is the high-frequency-corridor guard:
    'TGV 9582' and 'TGV 9586' run the same corridor 30 min apart but
    are unrelated services."""
    v = [_viator_trip([_v_leg(route="TGV 9582", dep="2026-06-30T08:00")])]
    # Same endpoints + minute, different train.
    o = [_o_itin(legs=[_o_leg(route="TGV 9586", dep="2026-06-30T08:00")])]
    score, tier = compute_alignment(v, o)
    assert score == 0.0
    assert tier == "no_overlap"


def test_compute_alignment_partial_score_below_mostly_threshold() -> None:
    """3 VIATOR trips, 1 ÖBB exact match → 1/3 = 0.33 → below the
    0.40 'partial' threshold → 'disagree'. Pinned so a denominator
    refactor doesn't shift this cell into a more positive tier."""
    v = [
        _viator_trip([_v_leg(route="RJ 1141", dep="2026-06-30T08:00")]),
        _viator_trip([_v_leg(route="RJ 1143", dep="2026-06-30T09:00")]),
        _viator_trip([_v_leg(route="RJ 1145", dep="2026-06-30T10:00")]),
    ]
    o = [_o_itin(legs=[_o_leg(route="RJ 1141", dep="2026-06-30T08:00")])]
    score, tier = compute_alignment(v, o)
    # denominator = max(min(3,3), min(1,3)) = max(3,1) = 3 → 1/3 = 0.33
    assert score == 0.33
    assert tier == "disagree"


def test_compute_alignment_partial_tier_at_two_of_three() -> None:
    """2 of 3 exact matches → 2/3 = 0.67 → 'partial' (≥ 0.40 and < 0.70).
    Boundary check on the partial bucket."""
    v = [
        _viator_trip([_v_leg(route="RJ 1141", dep="2026-06-30T08:00")]),
        _viator_trip([_v_leg(route="RJ 1143", dep="2026-06-30T09:00")]),
        _viator_trip([_v_leg(route="RJ 1145", dep="2026-06-30T10:00")]),
    ]
    o = [
        _o_itin(legs=[_o_leg(route="RJ 1141", dep="2026-06-30T08:00")]),
        _o_itin(legs=[_o_leg(route="RJ 1143", dep="2026-06-30T09:00")]),
    ]
    score, tier = compute_alignment(v, o)
    assert score == 0.67
    assert tier == "partial"


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
    score, tier = compute_alignment(v, o)
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
    score, tier = compute_alignment(v, o)
    # Exact match consumes the single ÖBB row once; remaining 2 VIATOR
    # trips fuzzy-match against an already-matched ÖBB row → no further
    # credit. denominator = max(min(3,3), min(1,3)) = 3 → 1/3 = 0.33.
    # This validates the algorithm doesn't double-credit a single ÖBB
    # row to multiple VIATOR trips.
    assert score == 0.33
    assert tier == "disagree"
