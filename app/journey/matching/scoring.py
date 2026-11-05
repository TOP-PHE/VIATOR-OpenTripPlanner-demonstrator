"""Gates, score and verdict of one pair (#304 design §7), and M8's account of a split.

**Gates** (a failed one: no pair, its reason kept for the unpaired journey): G2 both first
boarding and last alighting within the tolerances; G3 no endpoint station CONFLICT, and an
unknown one only when the profile allows it (M4); G4 no train-number CONFLICT under the profile's
policy (M2). G1, a ride on both sides, is the engine's.

**Score**: the weighted mean of the components with evidence (time, station, route, service,
transfers, operator), minus `line_conflict_penalty` for each line-label CONFLICT (M3; and each
train-number one under the `penalty` policy). **Verdict**: an exact pair (strict fingerprints
equal) scores 1.0; `same_service` at `same_service_min` with no warning, `probably_same` at
`probable_min`, else `different`.

**M8** (`split_reasons`): when G2 fails but the two journeys take the same rides first, the
reasons say where they split (`splits_after_shared_ride`), compare the two waits there
(`shorter_transfer` when one is shorter by `transfer_difference_s` or more) and note a later
shared ride (`rejoins_after_split`). No pair and no score change: only the explanation.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..canonical import MatchReason, MatchVerdict
from . import reasons as r
from .features import AlignedRide, PairAnalysis
from .identifiers import Agreement
from .profiles import MatchProfile
from .spines import Ride, same_minutes, seconds_between

SEVERITY_RANK = {"veto": 0, "gate": 1, "warning": 2, "info": 3}


def ordered(found: list[MatchReason]) -> list[MatchReason]:
    """Most important first (veto, gate, warning, info), each once, otherwise in order."""
    unique = list(dict.fromkeys(found))
    return sorted(unique, key=lambda item: SEVERITY_RANK[item.severity])


@dataclass(frozen=True)
class Assessment:
    """A pair's outcome: whether it passed the gates, its score and verdict when it did, and
    its reasons."""

    passed: bool
    score: float
    verdict: MatchVerdict
    exact: bool
    reasons: list[MatchReason]


def _number_conflict(ride: AlignedRide, profile: MatchProfile) -> tuple[MatchReason, bool]:
    """The reason of a train-number CONFLICT under the profile's policy (M2), and whether it
    vetoes the pair."""
    found = ride.service.reasons[0]
    policy = profile.train_number_conflict
    if policy == "veto_unless_same_minutes" and same_minutes(ride.left, ride.right):
        code, severity, vetoes = r.TRAIN_NUMBER_DIFFERS_SAME_MINUTES, "warning", False
    elif policy == "penalty":
        code, severity, vetoes = r.TRAIN_NUMBER_DIFFERS, "warning", False
    else:
        code, severity, vetoes = r.TRAIN_NUMBER_DIFFERS, "veto", True
    item = r.reason(
        code,
        "veto" if severity == "veto" else "warning",
        leg=ride.left.index,
        left_value=found.left_value,
        right_value=found.right_value,
    )
    return item, vetoes


def _time_gate(analysis: PairAnalysis, profile: MatchProfile) -> list[MatchReason]:
    found = []
    dep, arr = analysis.features.departure_delta, analysis.features.arrival_delta
    if dep is None or abs(dep) > profile.departure_tolerance_s:
        found.append(
            r.reason(
                r.DEPARTURE_OUTSIDE_TOLERANCE,
                "gate",
                left_value=str(dep) if dep is not None else None,
            )
        )
    if arr is None or abs(arr) > profile.arrival_tolerance_s:
        found.append(
            r.reason(
                r.ARRIVAL_OUTSIDE_TOLERANCE,
                "gate",
                left_value=str(arr) if arr is not None else None,
            )
        )
    return found


def _station_gate(
    analysis: PairAnalysis, profile: MatchProfile
) -> tuple[list[MatchReason], list[MatchReason]]:
    """G3: the reasons that stop the pair, and the info reasons it keeps."""
    stops: list[MatchReason] = []
    notes: list[MatchReason] = []
    for leg, found in (
        (analysis.left[0].index, analysis.first_station),
        (analysis.left[-1].index, analysis.last_station),
    ):
        agreement = found.for_endpoint_gate()
        if agreement == Agreement.CONFLICT:
            stops.append(r.reason(r.ENDPOINT_STATION_DIFFERS, "veto", leg=leg))
        elif agreement == Agreement.UNKNOWN:
            if profile.unresolved_endpoints == "reject":
                stops.append(r.reason(r.ENDPOINT_UNRESOLVED, "gate", leg=leg))
            else:
                notes.append(r.reason(r.ENDPOINT_UNRESOLVED, "info", leg=leg))
    return stops, notes


def _components(analysis: PairAnalysis, profile: MatchProfile) -> dict[str, float | None]:
    features = analysis.features
    tolerance = profile.departure_tolerance_s + profile.arrival_tolerance_s
    dep, arr = abs(features.departure_delta or 0), abs(features.arrival_delta or 0)
    if tolerance > 0:
        time = max(0.0, 1.0 - (dep + arr) / tolerance)
    else:
        time = 1.0 if dep == arr == 0 else 0.0
    delta = abs(features.transfer_count_delta)
    return {
        "time": time,
        "station": features.station_overlap,
        "route": features.route_overlap,
        "service": features.service_overlap,
        "transfers": 1.0 if delta == 0 else 0.5 if delta == 1 else 0.0,
        "operator": features.operator_overlap,
    }


def score(analysis: PairAnalysis, profile: MatchProfile, penalties: int) -> float:
    """The weighted mean of the components with evidence, minus the penalties, in [0, 1]."""
    weights = profile.weights.model_dump()
    total = weighted = 0.0
    for name, value in _components(analysis, profile).items():
        if value is None:
            continue
        total += weights[name]
        weighted += weights[name] * value
    mean = weighted / total if total > 0 else 0.0
    return round(min(1.0, max(0.0, mean - profile.line_conflict_penalty * penalties)), 2)


def _service_reasons(
    analysis: PairAnalysis, profile: MatchProfile
) -> tuple[list[MatchReason], int, bool]:
    """The reasons of the aligned rides' services, the number of penalties, and whether a
    train number vetoes the pair (G4)."""
    found: list[MatchReason] = []
    penalties = 0
    vetoed = False
    for ride in analysis.rides:
        comparison = ride.service
        if comparison.agreement == Agreement.CONFLICT and comparison.kind == "number":
            item, veto = _number_conflict(ride, profile)
            found.append(item)
            vetoed = vetoed or veto
            if profile.train_number_conflict == "penalty":
                penalties += 1
            continue
        if comparison.agreement == Agreement.CONFLICT:
            penalties += 1
        found.extend(item for item in comparison.reasons if item.code != r.TRAIN_NUMBER_EQUAL)
    return found, penalties, vetoed


def _context_reasons(analysis: PairAnalysis) -> list[MatchReason]:
    """The info reasons of M10's trimmed rides, the stations' notes and the transfer counts."""
    found: list[MatchReason] = []
    for index in analysis.trimmed.left_out_left:
        found.append(r.reason(r.LOCAL_ACCESS_RIDE, "info", leg=index, left_value="local ride"))
    for index in analysis.trimmed.left_out_right:
        found.append(r.reason(r.LOCAL_ACCESS_RIDE, "info", leg=index, right_value="local ride"))
    for leg, note in analysis.station_notes:
        found.append(r.reason(note, "info", leg=leg))
    delta = analysis.features.transfer_count_delta
    if delta != 0:
        found.append(
            r.reason(
                r.TRANSFER_COUNT_DIFFERS,
                "info",
                left_value=str(len(analysis.left) - 1),
                right_value=str(len(analysis.right) - 1),
            )
        )
    if analysis.features.operator_overlap == 0.0:
        found.append(r.reason(r.OPERATOR_DIFFERS, "info"))
    return found


def assess(analysis: PairAnalysis, profile: MatchProfile, *, exact: bool) -> Assessment:
    """The gates, then the score and the verdict of a pair."""
    stops = _time_gate(analysis, profile)
    station_stops, station_notes = _station_gate(analysis, profile)
    stops += station_stops
    service, penalties, vetoed = _service_reasons(analysis, profile)
    context = [*station_notes, *service, *_context_reasons(analysis)]
    if stops or vetoed:
        if any(
            item.code == r.DEPARTURE_OUTSIDE_TOLERANCE or item.code == r.ARRIVAL_OUTSIDE_TOLERANCE
            for item in stops
        ):
            context += split_reasons(analysis, profile)
        return Assessment(False, 0.0, "different", False, ordered([*stops, *context]))
    warned = any(item.severity == "warning" for item in context)
    if exact:
        found = [r.reason(r.EXACT_FINGERPRINT, "info"), *context]
        return Assessment(
            True, 1.0, "probably_same" if warned else "same_service", True, ordered(found)
        )
    value = score(analysis, profile, penalties)
    if value >= profile.same_service_min and not warned:
        verdict: MatchVerdict = "same_service"
    elif value >= profile.probable_min:
        verdict = "probably_same"
    else:
        verdict = "different"
        context.append(r.reason(r.SCORE_TOO_LOW, "gate", left_value=f"{value:.2f}"))
    return Assessment(verdict != "different", value, verdict, False, ordered(context))


def _shared_prefix(analysis: PairAnalysis) -> int:
    """How many rides the two journeys share from their start, in order."""
    count = 0
    for k, ride in enumerate(analysis.rides):
        if k >= len(analysis.left) or k >= len(analysis.right):
            break
        if ride.left is not analysis.left[k] or ride.right is not analysis.right[k]:
            break
        count += 1
    return count


def _wait_after(rides: list[Ride], k: int) -> int | None:
    """The wait between ride `k - 1`'s arrival and ride `k`'s departure, in seconds."""
    if k >= len(rides):
        return None
    return seconds_between(rides[k - 1].arrival, rides[k].departure)


def split_reasons(analysis: PairAnalysis, profile: MatchProfile) -> list[MatchReason]:
    """M8's reasons for a pair that failed G2 but shares its first rides (see the module
    docstring); none when the first rides differ or every ride is shared."""
    shared = _shared_prefix(analysis)
    if shared == 0 or (shared == len(analysis.left) and shared == len(analysis.right)):
        return []
    last = analysis.rides[shared - 1]
    station_l = last.left.leg.destination.name
    station_r = last.right.leg.destination.name
    found = [
        r.reason(
            r.SPLITS_AFTER_SHARED_RIDE,
            "info",
            leg=last.left.index,
            left_value=station_l,
            right_value=station_r,
        )
    ]
    wait_l, wait_r = _wait_after(analysis.left, shared), _wait_after(analysis.right, shared)
    if (
        wait_l is not None
        and wait_r is not None
        and abs(wait_l - wait_r) >= profile.transfer_difference_s
    ):
        found.append(
            r.reason(
                r.SHORTER_TRANSFER,
                "warning",
                leg=last.left.index,
                left_value=str(wait_l),
                right_value=str(wait_r),
            )
        )
    later = analysis.rides[shared:]
    if later:
        found.append(
            r.reason(
                r.REJOINS_AFTER_SPLIT,
                "info",
                leg=later[0].left.index,
                left_value=later[0].left.leg.origin.name,
                right_value=later[0].right.leg.origin.name,
            )
        )
    return found
