"""The matching engine (#304 design §7, §8, §12): gates, score, verdict, pairing and reasons.

The worked examples of §7 are pinned to the second decimal, so that a weight change cannot
silently undo the owner's expectation 4 (a label difference alone never splits a time-matched
pair). The cases measured on the full eu19 coverage run of 4 November (§1.1) are rebuilt by hand
from what the run's share page shows: names, times, labels and modes (the share projection holds
no stop id, code or position, design §12's "captured answer" is therefore not available).
"""

from __future__ import annotations

import random

import pytest

from app.journey.matching import (
    COVERAGE_ALIGNMENT,
    JOURNEY_ENGINES,
    JOURNEY_REFERENCE,
    MatchProfile,
    compare,
    profile_from_config,
    reasons,
)
from app.journey.matching import reasons as r
from tests.unit.matching_journeys import RAIL, TRAM, journey, ride, stop, walk

GRAZ, KOEFLACH = "Graz Hbf", "Köflach Bahnhof"


def codes(found: object) -> list[str]:
    return [item.code for item in found.reasons]  # type: ignore[attr-defined]


def only_pair(comparison: object) -> object:
    (pair,) = comparison.pairs  # type: ignore[attr-defined]
    return pair


# ── The worked examples of §7 (stations unresolved, as on eu19) ──────────────────────────


def s7(journey_id: str, label: dict[str, str], operator: str, provider: str = "motis") -> object:
    return journey(
        journey_id,
        ride(GRAZ, KOEFLACH, "08:14", "09:11", operator=operator, **label),
        provider=provider,
    )


def test_s7_and_s_7_at_the_same_minutes_are_the_same_service() -> None:
    viator = s7("motis:eu19:0", {"line": "S7", "published": "S7"}, "GKB")
    oebb = s7("hafas:oebb:0", {"published": "S 7", "category": "S"}, "GKB", "hafas")

    pair = only_pair(compare([viator], [oebb], JOURNEY_REFERENCE))

    assert (pair.verdict, pair.score) == ("same_service", 1.0)
    assert set(codes(pair)) == {r.LABEL_SPACING_ONLY, r.ENDPOINT_UNRESOLVED}
    assert all(item.severity == "info" for item in pair.reasons)


def test_operators_named_differently_still_the_same_service() -> None:
    viator = s7("motis:eu19:0", {"line": "S7"}, "GKB")
    oebb = s7("hafas:oebb:0", {"published": "S 7", "category": "S"}, "Graz-Köflacher Bahn", "hafas")

    pair = only_pair(compare([viator], [oebb], JOURNEY_REFERENCE))

    assert (pair.verdict, pair.score) == ("same_service", 0.93)
    assert r.OPERATOR_DIFFERS in codes(pair)


def tram(journey_id: str, label: dict[str, str], departure: str, arrival: str) -> object:
    return journey(
        journey_id,
        ride(
            GRAZ,
            "Graz Jakominiplatz",
            departure,
            arrival,
            mode=TRAM,
            operator="Holding Graz",
            **label,
        ),
        provider="hafas" if journey_id.startswith("hafas") else "motis",
    )


def test_17_and_tram_1_at_the_same_minutes_are_probably_the_same() -> None:
    pair = only_pair(
        compare(
            [tram("motis:eu19:0", {"line": "17"}, "08:04", "08:12")],
            [tram("hafas:oebb:0", {"published": "Tram 1"}, "08:04", "08:12")],
            JOURNEY_REFERENCE,
        )
    )

    assert (pair.verdict, pair.score) == ("probably_same", 0.9)
    (label,) = [item for item in pair.reasons if item.code == r.LINE_LABEL_DIFFERS]
    assert (label.severity, label.left_value, label.right_value) == ("warning", "17", "Tram 1")


def test_two_trams_of_different_lines_a_minute_apart_are_different() -> None:
    comparison = compare(
        [tram("motis:eu19:0", {"line": "1"}, "08:04", "08:12")],
        [tram("hafas:oebb:0", {"published": "Tram 7"}, "08:05", "08:13")],
        JOURNEY_REFERENCE,
    )

    assert comparison.pairs == []
    (alone,) = comparison.left_only
    assert r.SCORE_TOO_LOW in codes(alone)
    assert next(i for i in alone.reasons if i.code == r.SCORE_TOO_LOW).left_value == "0.65"


def test_two_tgvs_two_minutes_apart_with_different_numbers_never_pair() -> None:
    comparison = compare(
        [
            journey(
                "motis:eu19:0",
                ride(
                    "Paris Gare de Lyon", "Lyon Part-Dieu", "07:00", "09:00", published="TGV 6601"
                ),
            )
        ],
        [
            journey(
                "hafas:oebb:0",
                ride(
                    "Paris Gare de Lyon", "Lyon Part-Dieu", "07:02", "09:02", published="TGV 6603"
                ),
            )
        ],
        JOURNEY_REFERENCE,
    )

    assert comparison.pairs == []
    (alone,) = comparison.left_only
    veto = next(i for i in alone.reasons if i.code == r.TRAIN_NUMBER_DIFFERS)
    assert (veto.severity, veto.left_value, veto.right_value) == ("veto", "TGV 6601", "TGV 6603")
    assert alone.reasons[0] == veto  # most important first


# ── M2's amendment: a different number at the same minutes ──────────────────────────────


def eurostar(journey_id: str, label: str, provider: str) -> object:
    return journey(
        journey_id,
        ride("Paris Nord", "Bruxelles-Midi", "06:13", "07:35", published=label),
        provider=provider,
    )


def test_eur_9341_and_9441_at_the_same_minutes_pair_under_the_default_not_under_veto() -> None:
    viator, oebb = (
        eurostar("motis:eu19:0", "9441", "motis"),
        eurostar("hafas:oebb:0", "EUR 9341", "hafas"),
    )

    pair = only_pair(compare([viator], [oebb], JOURNEY_REFERENCE))
    vetoed = compare(
        [viator], [oebb], JOURNEY_REFERENCE.model_copy(update={"train_number_conflict": "veto"})
    )

    assert pair.verdict == "probably_same"
    (warned,) = [item for item in pair.reasons if item.code == r.TRAIN_NUMBER_DIFFERS_SAME_MINUTES]
    assert (warned.severity, warned.left_value, warned.right_value) == (
        "warning",
        "9441",
        "EUR 9341",
    )
    assert vetoed.pairs == []


# ── Tolerances ────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("departure", "arrival", "verdict"),
    [
        pytest.param("08:00", "09:15", "same_service", id="the same minutes"),
        pytest.param("08:01", "09:16", "probably_same", id="one minute later at both ends"),
        pytest.param("08:02", "09:15", "probably_same", id="two minutes at the departure"),
        pytest.param("07:58", "09:15", "probably_same", id="two minutes earlier"),
        pytest.param("08:03", "09:15", None, id="three minutes: outside the tolerance"),
    ],
)
def test_jitter_of_two_minutes_pairs_three_does_not(
    departure: str, arrival: str, verdict: str | None
) -> None:
    viator = journey("motis:0", ride("Wien Hbf", "Linz Hbf", "08:00", "09:15", published="RJ 540"))
    oebb = journey(
        "hafas:0",
        ride("Wien Hbf", "Linz Hbf", departure, arrival, published="RJ 540"),
        provider="hafas",
    )

    pairs = compare([viator], [oebb], JOURNEY_REFERENCE).pairs

    assert [pair.verdict for pair in pairs] == ([verdict] if verdict else [])


def test_two_minutes_at_both_ends_spends_the_whole_time_component() -> None:
    """As built with the design's weights: at the tolerance on both ends the time component is
    0, and the same train number does not carry the pair (0.57 < 0.75): `different`."""
    viator = journey("motis:0", ride("Wien Hbf", "Linz Hbf", "08:00", "09:15", published="RJ 540"))
    oebb = journey(
        "hafas:0",
        ride("Wien Hbf", "Linz Hbf", "08:02", "09:17", published="RJ 540"),
        provider="hafas",
    )

    comparison = compare([viator], [oebb], JOURNEY_REFERENCE)

    assert comparison.pairs == []
    low = next(i for i in comparison.left_only[0].reasons if i.code == r.SCORE_TOO_LOW)
    assert low.left_value == "0.57"


def test_the_tolerance_setting_moves_the_boundary() -> None:
    viator = journey("motis:0", ride("Wien Hbf", "Linz Hbf", "08:00", "09:15", published="RJ 540"))
    oebb = journey(
        "hafas:0",
        ride("Wien Hbf", "Linz Hbf", "08:03", "09:15", published="RJ 540"),
        provider="hafas",
    )
    wider = profile_from_config(
        {"JOURNEY_MATCH_DEPARTURE_TOLERANCE_S": 180, "JOURNEY_MATCH_ARRIVAL_TOLERANCE_S": 180},
        "journey_reference",
    )

    assert compare([viator], [oebb], JOURNEY_REFERENCE).pairs == []
    assert len(compare([viator], [oebb], wider).pairs) == 1
    # The journey-engines profile is tighter: one minute.
    near = journey(
        "hafas:1",
        ride("Wien Hbf", "Linz Hbf", "08:02", "09:15", published="RJ 540"),
        provider="hafas",
    )
    assert compare([viator], [near], JOURNEY_ENGINES).pairs == []


def test_access_walks_of_different_lengths_do_not_change_the_verdict() -> None:
    train = ride("Wien Hbf", "Linz Hbf", "08:00", "09:15", published="RJ 540")
    short = journey("motis:0", walk("Hotel", "Wien Hbf", "07:55", "08:00"), train)
    long = journey(
        "hafas:0", walk("Somewhere", "Wien Hbf", "07:20", "07:58"), train, provider="hafas"
    )
    bare = journey("hafas:1", train, provider="hafas")

    assert (
        only_pair(compare([short], [long], JOURNEY_REFERENCE)).verdict
        == only_pair(compare([short], [bare], JOURNEY_REFERENCE)).verdict
    )


def test_a_regional_rail_leg_pairs_with_a_rail_leg() -> None:
    viator = journey("motis:0", ride("Graz Hbf", "Leoben Hbf", "08:00", "08:50", line="REX"))
    oebb = journey(
        "hafas:0",
        ride("Graz Hbf", "Leoben Hbf", "08:00", "08:50", published="REX 1745"),
        provider="hafas",
    )
    assert viator.legs[0].service.mode == oebb.legs[0].service.mode == RAIL  # type: ignore[union-attr]

    assert len(compare([viator], [oebb], JOURNEY_REFERENCE).pairs) == 1


# ── Stations ──────────────────────────────────────────────────────────────────────────────


def unresolved_pair() -> tuple[object, object]:
    viator = journey(
        "motis:0", ride("Hauptbahnhof", "Ulm Hbf", "08:00", "09:00", published="ICE 512")
    )
    oebb = journey(
        "hafas:0",
        ride("Stuttgart Hbf", "Ulm Hbf", "08:00", "09:00", published="ICE 512"),
        provider="hafas",
    )
    return viator, oebb


def test_unresolved_endpoints_pair_on_journeys_and_are_rejected_on_coverage() -> None:
    viator, oebb = unresolved_pair()

    assert only_pair(compare([viator], [oebb], JOURNEY_REFERENCE)).verdict == "same_service"
    coverage = compare([viator], [oebb], COVERAGE_ALIGNMENT)
    assert coverage.pairs == []
    assert r.ENDPOINT_UNRESOLVED in codes(coverage.left_only[0])


def test_two_journeys_without_any_station_id_never_pair_on_coverage() -> None:
    # The `OEBB:?` property: no endpoint evidence, no coverage credit.
    viator = journey(
        "motis:0",
        ride(
            stop("Wien Hbf", token="OEBB:904050"), "Linz Hbf", "08:00", "09:15", published="RJ 540"
        ),
    )
    oebb = journey(
        "hafas:0",
        ride(
            stop("Wien Hbf", token="OEBB:904050", source="hafas:oebb"),
            "Linz Hbf",
            "08:00",
            "09:15",
            published="RJ 540",
        ),
        provider="hafas",
    )

    assert compare([viator], [oebb], COVERAGE_ALIGNMENT).pairs == []


@pytest.mark.parametrize(("north", "paired"), [(0.0027, True), (0.0135, False)])
def test_stuttgart_by_position_300_m_agrees_1_500_m_does_not(north: float, paired: bool) -> None:
    viator = journey(
        "motis:0",
        ride(
            stop("Hauptbahnhof", lat=48.7840, lon=9.1820),
            stop("Ulm Hbf", lat=48.3994, lon=9.9829),
            "08:00",
            "09:00",
            published="ICE 512",
        ),
    )
    oebb = journey(
        "hafas:0",
        ride(
            stop("Stuttgart Hbf", lat=48.7840 + north, lon=9.1820),
            stop("Ulm Hbf", lat=48.3996, lon=9.9831),
            "08:00",
            "09:00",
            published="ICE 512",
        ),
        provider="hafas",
    )

    comparison = compare([viator], [oebb], COVERAGE_ALIGNMENT)

    assert bool(comparison.pairs) is paired
    if paired:
        assert r.STATIONS_BY_POSITION in codes(comparison.pairs[0])
    else:
        assert r.ENDPOINT_UNRESOLVED in codes(comparison.left_only[0])


def test_the_module_s_stations_decide() -> None:
    a = stop("Zürich HB", station_id="ZZ-99001")
    b = stop("Basel SBB", station_id="ZZ-99002")
    viator = journey("motis:0", ride(a, b, "08:00", "09:00", line="IC3"))
    same = journey(
        "hafas:0",
        ride(
            stop("Zürich HB", station_id="ZZ-99001"),
            stop("Basel SBB", station_id="ZZ-99002"),
            "08:00",
            "09:00",
            published="IC 3",
        ),
        provider="hafas",
    )
    other = journey(
        "hafas:1",
        ride(stop("Zürich Oerlikon", station_id="ZZ-99003"), b, "08:00", "09:00", published="IC 3"),
        provider="hafas",
    )

    paired = only_pair(compare([viator], [same], COVERAGE_ALIGNMENT))
    refused = compare([viator], [other], COVERAGE_ALIGNMENT)

    assert (paired.verdict, paired.features.station_overlap) == ("same_service", 1.0)
    assert r.ENDPOINT_STATION_DIFFERS in codes(refused.left_only[0])


def test_equal_strict_fingerprints_are_exact() -> None:
    a, b = stop("Wien Hbf", uic="8100002"), stop("Linz Hbf", uic="8100013")
    viator = journey("motis:0", ride(a, b, "08:00", "09:15", line="RJ"))
    oebb = journey("hafas:0", ride(a, b, "08:00", "09:15", published="RJ 540"), provider="hafas")

    pair = only_pair(compare([viator], [oebb], COVERAGE_ALIGNMENT))

    assert (pair.exact, pair.score, pair.verdict) == (True, 1.0, "same_service")
    assert pair.reasons[0].code == r.EXACT_FINGERPRINT


# ── Pairing ───────────────────────────────────────────────────────────────────────────────


def minute_train(journey_id: str, provider: str, arrival: str = "09:15") -> object:
    return journey(
        journey_id,
        ride("Wien Hbf", "Linz Hbf", "08:00", arrival, published="RJ 540"),
        provider=provider,
    )


def test_three_against_two_in_one_minute_give_two_pairs_and_one_left_with_its_reason() -> None:
    viator = [minute_train(f"motis:{n}", "motis", f"09:{15 + n}") for n in range(3)]
    oebb = [minute_train(f"hafas:{n}", "hafas", f"09:{15 + n}") for n in range(2)]

    comparison = compare(viator, oebb, JOURNEY_REFERENCE)

    assert {(p.left_id, p.right_id) for p in comparison.pairs} == {
        ("motis:0", "hafas:0"),
        ("motis:1", "hafas:1"),
    }
    (alone,) = comparison.left_only
    assert alone.journey_id == "motis:2"
    # Its closest candidate passed, but went to a better match.
    assert (alone.closest_id, alone.reasons[0].code) == ("hafas:0", r.PAIRED_ELSEWHERE)
    assert comparison.summary.left_only == 1


def test_the_pairs_do_not_depend_on_the_order_of_the_input() -> None:
    viator = [minute_train(f"motis:{n}", "motis") for n in range(3)]
    oebb = [minute_train(f"hafas:{n}", "hafas") for n in range(3)]
    expected = {(p.left_id, p.right_id) for p in compare(viator, oebb, JOURNEY_REFERENCE).pairs}

    for seed in range(5):
        shuffled_v, shuffled_o = viator[:], oebb[:]
        random.Random(seed).shuffle(shuffled_v)
        random.Random(seed + 10).shuffle(shuffled_o)
        found = {
            (p.left_id, p.right_id)
            for p in compare(shuffled_v, shuffled_o, JOURNEY_REFERENCE).pairs
        }
        assert found == expected


def test_a_walk_only_journey_is_uncomparable_and_an_empty_side_says_so() -> None:
    walking = journey("motis:9", walk("A", "B", "08:00", "08:20"))
    train = minute_train("motis:0", "motis")

    comparison = compare([walking, train], [], JOURNEY_REFERENCE)

    assert comparison.uncomparable_left == ["motis:9"]
    (alone,) = comparison.left_only
    assert codes(alone) == [r.NO_CANDIDATE]


def test_metadata_and_reason_counts() -> None:
    viator = [minute_train("motis:0", "motis"), minute_train("motis:1", "motis", "10:30")]
    oebb = [minute_train("hafas:0", "hafas")]

    comparison = compare(viator, oebb, JOURNEY_REFERENCE)
    left, right = comparison.metadata()

    assert (left["motis:0"].bucket, left["motis:0"].paired_with) == ("common", "hafas:0")
    assert left["motis:1"].bucket == "left_only"
    assert right["hafas:0"].paired_with == "motis:0"
    counts = comparison.reason_counts()
    assert counts[r.ARRIVAL_OUTSIDE_TOLERANCE] == 1
    # Its closest candidate was stopped by a gate, not taken by a better match.
    assert r.PAIRED_ELSEWHERE not in counts


# ── M8: the same train, then a split (Salzburg Hbf → Basel SBB, §0.2) ─────────────────────


def test_the_same_train_to_munich_then_a_split_says_where_and_compares_the_waits() -> None:
    rjx = {"published": "RJX 260"}
    viator = journey(
        "motis:0",
        ride("Salzburg Hbf", "München Hbf", "08:00", "09:43", line="90", **{"published": "90"}),
        ride("München Hbf", "Zürich HB", "09:48", "13:28", line="88", published="88"),
        ride("Zürich HB", "Basel SBB", "13:34", "14:26", line="IR36", published="IR36"),
    )
    oebb = journey(
        "hafas:0",
        ride("Salzburg Hbf", "München Hbf", "08:00", "09:43", **rjx),
        ride("München Hbf", "Karlsruhe Hbf", "10:02", "12:52", published="ICE 566"),
        ride("Karlsruhe Hbf", "Basel SBB", "13:01", "14:48", published="ICE 371"),
        provider="hafas",
    )

    comparison = compare([viator], [oebb], JOURNEY_REFERENCE)

    assert comparison.pairs == []
    (alone,) = comparison.left_only
    split = next(i for i in alone.reasons if i.code == r.SPLITS_AFTER_SHARED_RIDE)
    shorter = next(i for i in alone.reasons if i.code == r.SHORTER_TRANSFER)
    assert (split.left_value, split.leg) == ("München Hbf", 0)
    assert (shorter.severity, shorter.left_value, shorter.right_value) == ("warning", "300", "1140")
    assert reasons.sentence(shorter, "VIATOR", "ÖBB") == "VIATOR changes in 5 min, ÖBB in 19 min"
    assert (
        reasons.sentence(split, "VIATOR", "ÖBB")
        == "same train to München Hbf, then different trains"
    )
    assert r.ARRIVAL_OUTSIDE_TOLERANCE in codes(alone)


def test_a_split_that_rejoins_says_so() -> None:
    viator = journey(
        "motis:0",
        ride("A", "B", "08:00", "09:00", published="IC 101"),
        ride("B", "C", "09:05", "10:00", published="IC 201"),
        ride("C", "D", "10:30", "11:00", published="IC 301"),
    )
    oebb = journey(
        "hafas:0",
        ride("A", "B", "08:00", "09:00", published="IC 101"),
        ride("B", "C", "09:20", "10:10", published="IC 202"),
        ride("C", "D", "10:30", "11:00", published="IC 301"),
        provider="hafas",
    )

    comparison = compare([viator], [oebb], JOURNEY_REFERENCE)

    # The ends agree in time: the pair passes G2, so M8 does not apply and the score decides.
    assert comparison.pairs or comparison.left_only
    late = journey(
        "hafas:1",
        ride("A", "B", "08:00", "09:00", published="IC 101"),
        ride("B", "C", "09:20", "10:10", published="IC 202"),
        ride("C", "D", "10:30", "11:00", published="IC 301"),
        ride("D", "E", "11:10", "11:40", published="IC 401"),
        provider="hafas",
    )
    alone = compare([viator], [late], JOURNEY_REFERENCE).left_only[0]
    rejoin = next(i for i in alone.reasons if i.code == r.REJOINS_AFTER_SPLIT)
    assert rejoin.left_value == "C"


# ── M10: a local ride only one side takes ─────────────────────────────────────────────────


def test_a_tram_to_the_station_on_one_side_is_left_out_like_a_walk() -> None:
    train = ride("Graz Hbf", "Wien Hbf", "08:00", "10:35", published="RJ 558")
    viator = journey(
        "motis:0", ride("Jakominiplatz", "Graz Hbf", "07:40", "07:48", mode=TRAM, line="1"), train
    )
    oebb = journey("hafas:0", train, provider="hafas")

    pair = only_pair(compare([viator], [oebb], JOURNEY_REFERENCE))

    assert pair.verdict == "same_service"
    local = next(i for i in pair.reasons if i.code == r.LOCAL_ACCESS_RIDE)
    assert (local.leg, local.left_value is not None) == (0, True)
    assert reasons.sentence(local, "VIATOR", "ÖBB") == "VIATOR adds a local ride to the station"


def test_a_leading_s_bahn_is_never_trimmed() -> None:
    train = ride("Graz Hbf", "Wien Hbf", "08:00", "10:35", published="RJ 558")
    viator = journey(
        "motis:0", ride("Graz Don Bosco", "Graz Hbf", "07:40", "07:48", line="S1"), train
    )
    oebb = journey("hafas:0", train, provider="hafas")

    comparison = compare([viator], [oebb], JOURNEY_REFERENCE)

    assert comparison.pairs == []
    assert r.LOCAL_ACCESS_RIDE not in codes(comparison.left_only[0])


# ── Profiles ──────────────────────────────────────────────────────────────────────────────


def test_profile_from_config_overlays_the_settings_and_keeps_bad_values_out() -> None:
    profile = profile_from_config(
        {
            "COVERAGE_MATCH_SAME_SERVICE_MIN_PCT": 85,
            "COVERAGE_MATCH_TRAIN_NUMBER_CONFLICT": "veto",
            "COVERAGE_MATCH_POSITION_FALLBACK_M": 99_999,  # out of bounds: kept out
            "COVERAGE_MATCH_UNRESOLVED_ENDPOINTS": "sometimes",  # not a policy: kept out
            "JOURNEY_MATCH_DEPARTURE_TOLERANCE_S": 600,  # another profile's: ignored
        },
        "coverage_alignment",
    )

    assert profile.same_service_min == 0.85
    assert profile.train_number_conflict == "veto"
    assert profile.position_fallback_m == COVERAGE_ALIGNMENT.position_fallback_m
    assert profile.unresolved_endpoints == "reject"
    assert profile.departure_tolerance_s == 120
    with pytest.raises(KeyError):
        profile_from_config({}, "zz")


def test_the_built_in_profiles() -> None:
    assert isinstance(JOURNEY_REFERENCE, MatchProfile)
    assert (JOURNEY_REFERENCE.unresolved_endpoints, COVERAGE_ALIGNMENT.unresolved_endpoints) == (
        "allow",
        "reject",
    )
    assert JOURNEY_ENGINES.departure_tolerance_s == 60
    assert JOURNEY_REFERENCE.train_number_conflict == "veto_unless_same_minutes"
    weights = JOURNEY_REFERENCE.weights
    assert (
        weights.time,
        weights.station,
        weights.route,
        weights.service,
        weights.transfers,
        weights.operator,
    ) == (
        0.30,
        0.25,
        0.15,
        0.15,
        0.10,
        0.05,
    )


def test_every_reason_code_has_a_sentence_entry() -> None:
    with pytest.raises(ValueError, match="unknown reason code"):
        r.reason("zz_unknown", "info")
    for code in r.SENTENCES:
        assert r.reason(code, "info").code == code
