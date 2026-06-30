"""PR-196a — graduated alignment scorer for coverage matrix cells.

Reduces a (VIATOR trips, ÖBB itineraries) pair to a single (score, tier)
that drives the viridis heatmap in the matrix UI.

Scoring is hybrid by design:

  1. **Exact pass** — strip WALK / TRANSFER legs from both sides, hash
     the remaining transit-leg spine via
     :func:`app.journey.signature.transit_fingerprint` (the same
     UIC-normalised fingerprint cross-engine dedup already uses on the
     federated planner). Exact hash match → credit 1.0.

  2. **Fuzzy fallback** — for VIATOR trips that didn't exact-match,
     find an ÖBB itinerary with the same (first_UIC, last_UIC) endpoint
     pair AND the same first-transit-leg train-number (from HAFAS
     ``prodL.name``, the operator-readable "RJ 1141" style label) AND a
     departure time within ±5 minutes. Credit 0.7.

We deliberately do NOT have a tier weaker than the train-number-guarded
fuzzy match. The next plausible signal — same endpoints + same minute
without train-number agreement — produces a flood of false positives on
high-frequency corridors (Paris-Lyon TGV departing every 30 min would
auto-credit unrelated trains running in similar slots). On those
corridors the operator's signal is the train *identity*, not the
schedule overlap.

Score normalisation::

    score = sum_credits / max(min(len(viator), 3), min(len(oebb), 3))

The min(n, 3) caps reward at three matches per side. Rationale: ÖBB's
``numF=5`` and VIATOR's ``numItineraries`` typically yield 3-10
itineraries each, and a 3-of-3 match is qualitatively the same evidence
as a 10-of-10 match — it's a corridor we both serve. Without the cap,
a 3-out-of-3 alignment scores 1.00 while a 3-out-of-10 alignment scores
0.30, even though the operator's question ("does ÖBB confirm the
service exists?") is satisfied identically by both.

Tier mapping is intentionally coarse — operators need ≤8 buckets to
keep the matrix legend scannable, not a per-percentile gradient:

    both empty                          → 'no_service'      (no score)
    VIATOR empty + ÖBB nonzero           → 'one_sided_oebb'   (score=0.0)
    VIATOR nonzero + ÖBB empty           → 'one_sided_viator' (score=0.0)
    score == 1.00                       → 'agree'
    0.70 <= score < 1.00                → 'mostly_agree'
    0.40 <= score < 0.70                → 'partial'
    0.00 <  score < 0.40                → 'disagree'
    score == 0.0 + both nonzero         → 'no_overlap'
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from ..journey.signature import transit_fingerprint
from .external_verify import VerifyItinerary, extract_uic

# Threshold constants — kept at module scope so the test that exercises
# tier boundaries can import + assert against the same values, and a
# future operator-tunable knob (platform_config) replaces a single line
# rather than a literal scatter through the function body.
_TIER_AGREE_MIN = 1.00
_TIER_MOSTLY_MIN = 0.70
_TIER_PARTIAL_MIN = 0.40
_FUZZY_DEP_TOLERANCE_SECONDS = 5 * 60
_FUZZY_CREDIT = 0.70
_EXACT_CREDIT = 1.00
# Cap reward at 3 matches per side — see module docstring rationale.
_SCORE_CAP_PER_SIDE = 3


def _strip_walk_legs(legs: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return only the transit legs from `legs`.

    Same predicate :func:`transit_fingerprint` uses internally, lifted
    out here so the fuzzy-fallback path can also work on the
    transit-only spine (the endpoint UICs we compare are the FIRST and
    LAST *transit* leg endpoints, not the walk-leg origin/destination
    coordinates)."""
    return [leg for leg in legs if (leg.get("mode") or "").upper() not in ("", "WALK", "TRANSFER")]


def _verify_itinerary_to_legs(it: VerifyItinerary) -> list[dict[str, Any]]:
    """Translate a persisted VerifyItinerary into the leg-dict shape
    :func:`transit_fingerprint` expects.

    `transit_fingerprint` reads (mode, from_stop_id, to_stop_id, lat,
    lon, departure, arrival, route_short_name) per leg. Our
    VerifyItinerary stores UICs in `from_uic` / `to_uic` (already
    canonical), so we surface them as `from_stop_id` / `to_stop_id` and
    let the fingerprint extract the UIC out of the prefixed form (same
    regex it uses for VIATOR's `SBB:8501120:0:5`-style stop_ids).
    """
    return [
        {
            "mode": leg.mode,
            # `transit_fingerprint` parses the UIC out of the stop_id —
            # `UIC:8503000` matches its 7-digit regex on the trailing
            # number, so the cross-engine fingerprint agrees with a
            # VIATOR-side `SBB:8503000:0:5`.
            "from_stop_id": leg.from_uic,
            "to_stop_id": leg.to_uic,
            "departure": leg.dep_utc,
            "arrival": leg.arr_utc,
            "route_short_name": leg.route_name,
        }
        for leg in it.legs
    ]


def _endpoint_uics(transit_legs: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    """First/last UIC of the transit-leg spine, for the fuzzy fallback
    endpoint match. Returns (None, None) on an empty spine — caller
    treats that as unmatchable."""
    if not transit_legs:
        return None, None
    first = extract_uic(transit_legs[0].get("from_stop_id"))
    last = extract_uic(transit_legs[-1].get("to_stop_id"))
    return first, last


def _first_train_number(transit_legs: list[dict[str, Any]]) -> str | None:
    """Operator-readable train number of the first transit leg ("RJ 1141"
    on HAFAS, "TGV 9582" on SNCF). Returns None when missing — fuzzy
    fallback requires both sides to have one, so a missing number on
    either side blocks the match (deliberate: see module docstring)."""
    if not transit_legs:
        return None
    name = transit_legs[0].get("route_short_name") or transit_legs[0].get("route_long_name")
    if not name:
        return None
    return str(name).strip().upper() or None


def _parse_iso_minute(value: str | None) -> datetime | None:
    """Parse `YYYY-MM-DDTHH:MM[…]` to a naive datetime (minute resolution)
    for the ±5min fuzzy comparison. Returns None on garbage so the caller
    can short-circuit the fuzzy match rather than panic-fail."""
    if not value or len(value) < 16:
        return None
    try:
        return datetime.fromisoformat(value[:16])
    except ValueError:
        return None


def _first_transit_dep_dt(transit_legs: list[dict[str, Any]]) -> datetime | None:
    """First-transit-leg departure as a datetime, or None when absent."""
    if not transit_legs:
        return None
    return _parse_iso_minute(transit_legs[0].get("departure"))


def _is_fuzzy_candidate(
    o_legs: list[dict[str, Any]],
    v_first: str,
    v_last: str,
    v_train: str,
    v_dep: datetime,
) -> bool:
    """True iff `o_legs` matches the VIATOR side on (first_UIC, last_UIC)
    endpoints AND first-transit-leg train number AND first-leg departure
    within ±5 min. Extracted from `_fuzzy_match` so the outer function
    stays under Sonar's cognitive-complexity ceiling — the cascade of
    five `continue` checks was the bulk of its complexity."""
    o_first, o_last = _endpoint_uics(o_legs)
    if o_first != v_first or o_last != v_last:
        return False
    if _first_train_number(o_legs) != v_train:
        return False
    o_dep = _first_transit_dep_dt(o_legs)
    if o_dep is None:
        return False
    return abs((v_dep - o_dep).total_seconds()) <= _FUZZY_DEP_TOLERANCE_SECONDS


def _fuzzy_match(
    viator_legs: list[dict[str, Any]],
    oebb_candidates: list[tuple[int, list[dict[str, Any]]]],
    already_matched_oebb: set[int],
) -> int | None:
    """Find an ÖBB candidate that fuzzy-matches `viator_legs`. Returns the
    matched candidate's index (so the caller can mark it matched and
    avoid double-counting) or None.

    Fuzzy match = same (first_UIC, last_UIC) endpoint pair AND same
    first-transit-leg train number AND first-leg departure within
    ±5 min. Either side missing a train number → no match (intentional;
    see module docstring on the high-frequency-corridor false-positive
    risk).
    """
    v_first, v_last = _endpoint_uics(viator_legs)
    v_train = _first_train_number(viator_legs)
    v_dep = _first_transit_dep_dt(viator_legs)
    if v_first is None or v_last is None or v_train is None or v_dep is None:
        return None
    for idx, o_legs in oebb_candidates:
        if idx in already_matched_oebb:
            continue
        if _is_fuzzy_candidate(o_legs, v_first, v_last, v_train, v_dep):
            return idx
    return None


def _classify_score(score: float | None, v_n: int, o_n: int) -> str:
    """Score + (viator_count, oebb_count) → tier label. Split out so the
    8-branch bucket logic doesn't push compute_alignment over Sonar's
    cognitive-complexity ceiling."""
    if v_n == 0 and o_n == 0:
        return "no_service"
    if v_n == 0:
        return "one_sided_oebb"
    if o_n == 0:
        return "one_sided_viator"
    # Both sides have trips beyond this point.
    if score is None:  # pragma: no cover — score is float when both nonzero
        return "no_overlap"
    if score >= _TIER_AGREE_MIN:
        return "agree"
    if score >= _TIER_MOSTLY_MIN:
        return "mostly_agree"
    if score >= _TIER_PARTIAL_MIN:
        return "partial"
    if score > 0.0:
        return "disagree"
    return "no_overlap"


def _find_first_fp_match(v_fp: str, oebb_fps: list[str], already_matched: set[int]) -> int | None:
    """First unmatched ÖBB index whose fingerprint equals `v_fp`, or None.
    Empty (`""`) ÖBB fingerprints are never matchable — they mean "no
    comparable transit spine"."""
    for o_idx, o_fp in enumerate(oebb_fps):
        if o_idx in already_matched or not o_fp:
            continue
        if v_fp == o_fp:
            return o_idx
    return None


def _exact_pass(viator_fps: list[str], oebb_fps: list[str]) -> tuple[set[int], list[int], float]:
    """Pair each non-empty VIATOR fingerprint to the first unmatched ÖBB
    fingerprint that equals it. Returns
    ``(matched_oebb_indices, unmatched_viator_indices, total_credits)``.
    Extracted from `compute_alignment` so that function stays under
    Sonar's cognitive-complexity ceiling — the nested-loop + branch
    cluster here was the bulk of its complexity."""
    matched_oebb: set[int] = set()
    unmatched_viator: list[int] = []
    score_credits = 0.0
    for v_idx, v_fp in enumerate(viator_fps):
        if not v_fp:
            unmatched_viator.append(v_idx)
            continue
        hit = _find_first_fp_match(v_fp, oebb_fps, matched_oebb)
        if hit is None:
            unmatched_viator.append(v_idx)
            continue
        matched_oebb.add(hit)
        score_credits += _EXACT_CREDIT
    return matched_oebb, unmatched_viator, score_credits


def compute_alignment(
    viator_trips: list[dict[str, Any]],
    oebb_itineraries: list[VerifyItinerary],
) -> tuple[float | None, str]:
    """PR-196a — score + tier the agreement between VIATOR and ÖBB trips.

    `viator_trips` is the canonical trip-dict list (the shape
    :func:`_fetch_trips_by_search` emits per cell — legs[], duration,
    modes, etc.). `oebb_itineraries` is the persisted
    :class:`VerifyItinerary` list captured by the sweep.

    Returns ``(score, tier)``. ``score`` is None when both sides are
    empty (no_service) or when one side is empty (the tier carries the
    one-sided semantic instead). Otherwise score is in [0.0, 1.0].

    See module docstring for the full scoring + tier-mapping rules.
    """
    v_n = len(viator_trips)
    o_n = len(oebb_itineraries)
    if v_n == 0 and o_n == 0:
        return None, _classify_score(None, 0, 0)
    if v_n == 0 or o_n == 0:
        return 0.0, _classify_score(0.0, v_n, o_n)

    # Pre-compute transit-only spines + exact fingerprints for both sides
    # in one pass — same data feeds both the exact pass and the fuzzy
    # fallback so we don't strip + parse legs twice.
    viator_spines = [_strip_walk_legs(t.get("legs") or []) for t in viator_trips]
    oebb_legs_lists = [_strip_walk_legs(_verify_itinerary_to_legs(it)) for it in oebb_itineraries]
    viator_fps = [transit_fingerprint(legs) for legs in viator_spines]
    oebb_fps = [transit_fingerprint(legs) for legs in oebb_legs_lists]

    # Exact pass: every non-empty fingerprint match scores 1.0.
    matched_oebb, unmatched_viator, score_credits = _exact_pass(viator_fps, oebb_fps)

    # Fuzzy fallback: each VIATOR trip the exact pass missed gets one
    # shot at the ÖBB candidates that are still unmatched. Same-
    # train-number-guarded so high-frequency corridors don't auto-match
    # unrelated services that happen to share endpoints + slot.
    oebb_candidates = list(enumerate(oebb_legs_lists))
    for v_idx in unmatched_viator:
        matched_idx = _fuzzy_match(viator_spines[v_idx], oebb_candidates, matched_oebb)
        if matched_idx is not None:
            matched_oebb.add(matched_idx)
            score_credits += _FUZZY_CREDIT

    denominator = max(min(v_n, _SCORE_CAP_PER_SIDE), min(o_n, _SCORE_CAP_PER_SIDE))
    score = round(score_credits / denominator, 2) if denominator > 0 else 0.0
    # Cap at 1.0 — possible to overshoot when ÖBB returns 1 trip and
    # VIATOR returns 3 matches against it (each scored 1.0). The
    # operator question is "do we agree?", not "how many?", so saturate.
    score = min(score, 1.0)
    return score, _classify_score(score, v_n, o_n)
