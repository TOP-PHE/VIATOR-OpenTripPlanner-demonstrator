"""The reason vocabulary of the matching engine (#304 design §9): each code, its severity and
the sentence a page prints.

A reason's code is stable: coverage counts them per cell, pages look sentences up by it. The
sentences live here, on the server, and a page writes them with `textContent`. A sentence is
built from the reason's values and the two sides' labels (`VIATOR`, `ÖBB`); no value is ever
read as markup.
"""

from __future__ import annotations

from ..canonical import MatchReason
from ..canonical.journey_similarity_result import ReasonSeverity

EXACT_FINGERPRINT = "exact_fingerprint"
LABEL_SPACING_ONLY = "label_spacing_only"
MODE_WORD_ONLY = "mode_word_only"
CATEGORY_DIFFERS = "category_differs"
LOCATION_ONLY = "location_only"
SAME_COMPLEX = "same_complex"
ENDPOINT_UNRESOLVED = "endpoint_unresolved"
LINE_LABEL_DIFFERS = "line_label_differs"
IDENTIFIER_KINDS_DIFFER = "identifier_kinds_differ"
IDENTIFIER_MISSING = "identifier_missing"
OPERATOR_DIFFERS = "operator_differs"
TRANSFER_COUNT_DIFFERS = "transfer_count_differs"
TRAIN_NUMBER_EQUAL = "train_number_equal"
TRAIN_NUMBER_DIFFERS = "train_number_differs"
TRAIN_NUMBER_DIFFERS_SAME_MINUTES = "train_number_differs_same_minutes"
STATIONS_BY_POSITION = "stations_by_position"
LOCAL_ACCESS_RIDE = "local_access_ride"
SPLITS_AFTER_SHARED_RIDE = "splits_after_shared_ride"
SHORTER_TRANSFER = "shorter_transfer"
REJOINS_AFTER_SPLIT = "rejoins_after_split"
OUTSIDE_REFERENCE_WINDOW = "outside_reference_window"
ENDPOINT_STATION_DIFFERS = "endpoint_station_differs"
DEPARTURE_OUTSIDE_TOLERANCE = "departure_outside_tolerance"
ARRIVAL_OUTSIDE_TOLERANCE = "arrival_outside_tolerance"
NO_TRANSIT_LEG = "no_transit_leg"
NO_CANDIDATE = "no_candidate"
SCORE_TOO_LOW = "score_too_low"
PAIRED_ELSEWHERE = "paired_elsewhere"

# The sentence of each code. `{left}` / `{right}`: the sides' labels; `{left_value}` /
# `{right_value}`: the reason's values; `{left_min}` / `{right_min}`: a value of seconds in
# whole minutes. An empty sentence: hidden by default.
SENTENCES: dict[str, str] = {
    EXACT_FINGERPRINT: "",
    LABEL_SPACING_ONLY: "",
    MODE_WORD_ONLY: "",
    CATEGORY_DIFFERS: "product names differ: {left_value} / {right_value}",
    LOCATION_ONLY: "",
    SAME_COMPLEX: "",
    ENDPOINT_UNRESOLVED: "stations could not be compared (no shared station code)",
    LINE_LABEL_DIFFERS: "line labels differ: {left_value} / {right_value}",
    IDENTIFIER_KINDS_DIFFER: "one source gives no train number",
    IDENTIFIER_MISSING: "one source gives no train number",
    OPERATOR_DIFFERS: "operators differ",
    TRANSFER_COUNT_DIFFERS: "{left_value} / {right_value} changes",
    TRAIN_NUMBER_EQUAL: "",
    TRAIN_NUMBER_DIFFERS: "different trains: {left_value} / {right_value}",
    TRAIN_NUMBER_DIFFERS_SAME_MINUTES: (
        "same times, the sources number the train differently: {left_value} / {right_value}"
    ),
    STATIONS_BY_POSITION: "stations compared by position (no shared station code)",
    # The side that takes the ride: the one whose value is set.
    LOCAL_ACCESS_RIDE: "{side} adds a local ride to the station",
    SPLITS_AFTER_SHARED_RIDE: "same train to {left_value}, then different trains",
    SHORTER_TRANSFER: "{left} changes in {left_min} min, {right} in {right_min} min",
    REJOINS_AFTER_SPLIT: "different trains, then the same again from {left_value}",
    OUTSIDE_REFERENCE_WINDOW: "leaves after {right}'s last connection",
    ENDPOINT_STATION_DIFFERS: "different first or last station",
    DEPARTURE_OUTSIDE_TOLERANCE: "departures {left_min} min apart",
    ARRIVAL_OUTSIDE_TOLERANCE: "arrivals {left_min} min apart",
    NO_TRANSIT_LEG: "walk only",
    NO_CANDIDATE: "no journey on the other side",
    SCORE_TOO_LOW: "too different to be the same journey",
    PAIRED_ELSEWHERE: "its closest match is paired with another journey",
}


def reason(
    code: str,
    severity: ReasonSeverity,
    *,
    leg: int | None = None,
    left_value: str | None = None,
    right_value: str | None = None,
) -> MatchReason:
    """A reason of the vocabulary; ValueError for a code it does not hold."""
    if code not in SENTENCES:
        raise ValueError(f"unknown reason code {code!r}")
    return MatchReason(
        code=code, severity=severity, leg=leg, left_value=left_value, right_value=right_value
    )


def _minutes(value: str | None) -> str:
    """A value of seconds as whole minutes, rounded; the value itself when it is not a number."""
    if value is None:
        return "?"
    try:
        return str(round(abs(int(value)) / 60))
    except ValueError:
        return value


def sentence(item: MatchReason, left: str, right: str) -> str:
    """The sentence of a reason, the sides named `left` and `right`; "" for a hidden one."""
    template = SENTENCES.get(item.code, "")
    return template.format(
        left=left,
        right=right,
        side=left if item.left_value is not None else right,
        left_value=item.left_value if item.left_value is not None else "?",
        right_value=item.right_value if item.right_value is not None else "?",
        left_min=_minutes(item.left_value),
        right_min=_minutes(item.right_value),
    )
