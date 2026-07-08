"""PR-196a — graduated alignment scorer for coverage matrix cells.

Reduces a (VIATOR trips, ÖBB itineraries) pair to a single (score, tier)
that drives the viridis heatmap in the matrix UI.

Scoring is hybrid by design:

  1. **Exact pass** — strip WALK / TRANSFER legs from both sides, hash
     the remaining transit-leg spine via
     :func:`app.journey.signature.transit_fingerprint` (the same
     UIC-normalised fingerprint cross-engine dedup already uses on the
     federated planner). Exact hash match → credit 1.0.

  2. **Fuzzy fallback** — for VIATOR trips that didn't exact-match, find
     an ÖBB itinerary whose first transit leg departs within ±5 minutes
     and which is the same service by one of two rules:

       * both sides report a **numeric train number** (HAFAS ``prodL.name``
         "RJ 1141" → ``1141``; ÖBB's "EUR 9322" → ``9322``) → the numbers
         must agree; or
       * at least one side reports **no** number (VIATOR's GTFS feed says
         "Eurostar", a brand shared by every service on the corridor) →
         the **transit-only spans** must agree within ±5 minutes.

     Credit 0.7 either way.

The train-number rule still blocks the high-frequency-corridor false
positive: two Paris-Lyon TGVs departing 5 minutes apart carry different
numbers, so they never auto-credit. The span rule only engages when no
number is available on at least one side, and then requires agreement on
*both* ends of the train's own journey — a much stronger claim than
"same departure minute".

Two things are deliberately NOT part of the match:

  * **Endpoint identity.** Both engines were asked for the same
    origin→destination hub pair by construction of the coverage cell, so
    within a cell it discriminates nothing. Across engines it is actively
    wrong: ``extract_uic`` scrapes the digits out of a HAFAS lid and calls
    them a UIC, but ÖBB's ``L=4899427`` for Amsterdam Centraal is an
    ÖBB-internal id, not that station's UIC (``8400058``). The namespaces
    coincide only inside DACH — which is why the old endpoint guard looked
    correct and silently rejected every cross-border pair.

  * **Walk-inclusive durations.** ÖBB appends a 42-minute egress walk on
    the Bruxelles-Midi hub (LocGeoPos snaps the coords to a stop away from
    the platforms), so door-to-door times compare the engines' station
    geocoding rather than their trains. Every comparison here runs on the
    walk-stripped transit spine.

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

import re
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from ..journey.signature import transit_fingerprint
from .external_verify import VerifyItinerary

# `external_verify._hafas_time_to_utc_iso` persists VerifyLeg/VerifyItinerary
# dep_utc/arr_utc as NAIVE Europe/Vienna wall-clock strings (the "_utc" name
# is aspirational — see that function's own docstring). This is the zone
# `_verify_itinerary_to_legs` localizes them from before comparing against
# VIATOR's genuinely UTC-instant leg times.
_OEBB_REFERENCE_TZ = ZoneInfo("Europe/Vienna")

# Threshold constants — kept at module scope so the test that exercises
# tier boundaries can import + assert against the same values, and a
# future operator-tunable knob (platform_config) replaces a single line
# rather than a literal scatter through the function body.
_TIER_AGREE_MIN = 1.00
_TIER_MOSTLY_MIN = 0.70
_TIER_PARTIAL_MIN = 0.40
_FUZZY_DEP_TOLERANCE_SECONDS = 5 * 60
# Transit-only span tolerance, used ONLY when at least one side reports no
# numeric train number. Same width as the departure tolerance: two services
# that leave within 5 min AND arrive within 5 min of each other, on the
# same hub pair, answer the operator's question identically.
_FUZZY_SPAN_TOLERANCE_SECONDS = 5 * 60
_FUZZY_CREDIT = 0.70
_EXACT_CREDIT = 1.00
# First digit-run of a route label: "EUR 9322" → 9322, "RJ 1141" → 1141.
# A label with no digits ("Eurostar", "Sprinter") is a brand, not a number.
_TRAIN_NUMBER_RE = re.compile(r"\d+")
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


def filter_trips_from_depart_at(
    trips: list[dict[str, Any]], depart_at: datetime
) -> list[dict[str, Any]]:
    """Keep only trips departing at/after `depart_at`, sorted
    chronologically (earliest first).

    ÖBB HAFAS's TripSearch is a forward "depart after" query — `numF=5`
    (see external_verify._build_trip_search_body) returns its next 5
    connections from `depart_at`, not 5 spread across the whole day.
    VIATOR's own per-cell trip list, by contrast, aggregates K time
    slots spanning the ENTIRE requested day window (runner.py's K-slot
    time-slicing), so without this filter a cell's VIATOR side could
    show itineraries from hours before OR after `depart_at` that ÖBB
    was never asked about — comparing a whole-day aggregate against a
    single-instant snapshot. That scope mismatch is independent of (and
    was found to be larger in practice than) the clock-basis mismatch
    `_oebb_naive_to_utc_iso` fixes.

    Used both by the alignment scorer (so a persisted score reflects a
    comparable window — see `runner._fetch_viator_trips_for_search`)
    and by the cell-detail display (so the VIATOR and ÖBB columns shown
    side by side represent the same search scope — see
    `network_coverage._fetch_trips_by_search`), rather than duplicating
    this filter in each caller.

    Trips with a missing/unparseable `departure_at` are dropped — with
    no placement in the window, there's no basis to call them
    comparable to ÖBB's answer either way.
    """
    dated: list[tuple[datetime, dict[str, Any]]] = []
    for t in trips:
        dep_str = t.get("departure_at")
        if not dep_str:
            continue
        try:
            dep = datetime.fromisoformat(dep_str)
        except ValueError:
            continue
        if dep >= depart_at:
            dated.append((dep, t))
    dated.sort(key=lambda pair: pair[0])
    return [t for _, t in dated]


def _oebb_naive_to_utc_iso(value: str | None) -> str | None:
    """Convert a persisted VerifyLeg/VerifyItinerary naive Vienna-local
    timestamp to a genuine UTC-instant ISO string — the same basis
    VIATOR's own leg `departure`/`arrival` values are already on.

    Without this, `transit_fingerprint`'s minute-rounding (exact-match
    tier) and `_first_transit_dep_dt`'s ±5min fuzzy comparison both
    compared VIATOR's true-UTC digits against ÖBB's local-clock digits
    verbatim — a systematic 1-2h (CET/CEST) skew that made a genuine
    train-for-train match nearly impossible to detect on either tier,
    silently pushing real matches toward 'no_overlap'/'disagree'. This
    went uncaught because the existing tests built BOTH sides' fixture
    legs with the same naive-looking timestamp, which isn't
    representative of production data (only the ÖBB side is naive).

    Returns None on unparseable/missing input so the caller treats the
    leg as unmatchable rather than guessing at a time.
    """
    if not value:
        return None
    try:
        naive = datetime.fromisoformat(value)
    except ValueError:
        return None
    return naive.replace(tzinfo=_OEBB_REFERENCE_TZ).astimezone(UTC).isoformat()


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
            "departure": _oebb_naive_to_utc_iso(leg.dep_utc),
            "arrival": _oebb_naive_to_utc_iso(leg.arr_utc),
            "route_short_name": leg.route_name,
        }
        for leg in it.legs
    ]


def _first_train_number(transit_legs: list[dict[str, Any]]) -> str | None:
    """The *numeric* train number of the first transit leg, or None.

    "RJ 1141" → "1141", "EUR 9322" → "9322", "TGV 9582" → "9582".
    A label carrying no digits at all ("Eurostar", "Nahreisezug",
    "Sprinter") is NOT a train number — it's a brand or a product class
    shared by every service on the corridor — so it returns None and the
    caller falls back to a schedule-shaped comparison.

    Previously this returned the whole label upper-cased, which turned the
    fuzzy guard into a string-equality test between two different naming
    systems: VIATOR's GTFS feed says "Eurostar" where ÖBB's HAFAS says
    "EUR 9322". They never matched, so every cross-border service scored
    `no_overlap` however well the two engines actually agreed.
    """
    if not transit_legs:
        return None
    name = transit_legs[0].get("route_short_name") or transit_legs[0].get("route_long_name")
    if not name:
        return None
    m = _TRAIN_NUMBER_RE.search(str(name))
    return m.group(0) if m else None


def _transit_span_seconds(transit_legs: list[dict[str, Any]]) -> float | None:
    """Seconds from the first transit leg's departure to the last transit
    leg's arrival — the journey as the *train* experiences it.

    Deliberately excludes the access/egress walks that bracket a trip: the
    two engines resolve the same hub to different stops and can produce
    very different walks (ÖBB appends a 42-minute walk on the
    Bruxelles-Midi hub, where LocGeoPos snaps the coords to a stop away
    from the platforms). Comparing walk-inclusive door-to-door times would
    judge the engines on their station geocoding, not on whether they
    found the same train.
    """
    if not transit_legs:
        return None
    dep = _parse_iso_minute(transit_legs[0].get("departure"))
    arr = _parse_iso_minute(transit_legs[-1].get("arrival"))
    if dep is None or arr is None:
        return None
    return (arr - dep).total_seconds()


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
    v_train: str | None,
    v_dep: datetime,
    v_span: float | None,
) -> bool:
    """True iff `o_legs` is the same physical service as the VIATOR spine.

    Departure of the first transit leg must agree within ±5 min. Then:

    * **both sides carry a numeric train number** → require agreement.
      This is the strong signal, and it still blocks the high-frequency-
      corridor false positive the module docstring warns about (two
      Paris-Lyon TGVs 5 min apart have different numbers).
    * **either side has no number** ("Eurostar" is a brand, not a train
      number) → require the transit-only spans to agree within ±5 min.
      Two *different* services leaving the same station within 5 minutes
      of each other and arriving within 5 minutes of each other are, for
      the operator's question ("does ÖBB confirm this service?"), the
      same answer.

    Endpoint identity is deliberately NOT checked. Both engines were asked
    for the same origin→destination hub pair by construction of the
    coverage cell, so within a cell it discriminates nothing — while
    across engines it is actively wrong: `extract_uic` scrapes the digits
    out of a HAFAS lid and calls them a UIC, but ÖBB's `L=4899427` for
    Amsterdam Centraal is an ÖBB-internal id, not that station's UIC
    (`8400058`). The two namespaces coincide only inside DACH, which is
    why this guard appeared to work and silently rejected every
    cross-border pair.
    """
    o_dep = _first_transit_dep_dt(o_legs)
    if o_dep is None:
        return False
    if abs((v_dep - o_dep).total_seconds()) > _FUZZY_DEP_TOLERANCE_SECONDS:
        return False
    o_train = _first_train_number(o_legs)
    if v_train is not None and o_train is not None:
        return v_train == o_train
    o_span = _transit_span_seconds(o_legs)
    if v_span is None or o_span is None:
        return False
    return abs(v_span - o_span) <= _FUZZY_SPAN_TOLERANCE_SECONDS


def _fuzzy_match(
    viator_legs: list[dict[str, Any]],
    oebb_candidates: list[tuple[int, list[dict[str, Any]]]],
    already_matched_oebb: set[int],
) -> int | None:
    """Find an ÖBB candidate that fuzzy-matches `viator_legs`. Returns the
    matched candidate's index (so the caller can mark it matched and
    avoid double-counting) or None.

    See `_is_fuzzy_candidate` for the rule. A VIATOR spine with no
    parseable first-transit departure is unmatchable.
    """
    v_train = _first_train_number(viator_legs)
    v_dep = _first_transit_dep_dt(viator_legs)
    v_span = _transit_span_seconds(viator_legs)
    if v_dep is None:
        return None
    for idx, o_legs in oebb_candidates:
        if idx in already_matched_oebb:
            continue
        if _is_fuzzy_candidate(o_legs, v_train, v_dep, v_span):
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
