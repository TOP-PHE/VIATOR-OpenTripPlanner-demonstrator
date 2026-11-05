"""Service identity (#304 design §5): every row of the examples table, and the label rules."""

from __future__ import annotations

import pytest

from app.journey.canonical import TransportMode
from app.journey.matching import reasons as r
from app.journey.matching.identifiers import (
    Agreement,
    compare_services,
    identity_of,
    normalise_label,
)
from tests.unit.matching_journeys import BUS, RAIL, TRAM, service


def codes(found: object) -> list[str]:
    return [item.code for item in found.reasons]  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("left", "right", "agreement", "expected"),
    [
        pytest.param(
            service(line="S7", published="S7"),
            service(published="S 7", category="S"),
            Agreement.AGREE,
            [r.LABEL_SPACING_ONLY],
            id="S7 / S 7",
        ),
        pytest.param(
            service(line="S7", published="S7"),
            service(line="7", category="S", number="4512"),
            Agreement.AGREE,
            [r.LABEL_SPACING_ONLY],
            id="S7 / category S and line 7 (HAFAS's course number on one side only)",
        ),
        pytest.param(
            service(mode=TRAM, line="17", published="17"),
            service(mode=TRAM, published="Tram 1"),
            Agreement.CONFLICT,
            [r.LINE_LABEL_DIFFERS],
            id="17 / Tram 1",
        ),
        pytest.param(
            service(published="RJ 1141"),
            service(published="RJ 1141"),
            Agreement.AGREE,
            [],
            id="RJ 1141 / RJ 1141",
        ),
        pytest.param(
            service(published="RJ 1141"),
            service(published="RJ 1143"),
            Agreement.CONFLICT,
            [r.TRAIN_NUMBER_DIFFERS],
            id="RJ 1141 / RJ 1143",
        ),
        pytest.param(
            service(line="IC5"),
            service(line="IC 5"),
            Agreement.AGREE,
            [r.LABEL_SPACING_ONLY],
            id="IC5 / IC 5",
        ),
        pytest.param(
            service(published="TGV 6107"),
            service(published="TGV INOUI 6107"),
            Agreement.AGREE,
            [r.CATEGORY_DIFFERS],
            id="TGV 6107 / TGV INOUI 6107",
        ),
        pytest.param(
            service(published="EUR 9322"),
            service(published="Eurostar 9322"),
            Agreement.AGREE,
            [r.CATEGORY_DIFFERS],
            id="EUR 9322 / Eurostar 9322",
        ),
        pytest.param(
            service(number="601"),
            service(number="601A"),
            Agreement.CONFLICT,
            [r.TRAIN_NUMBER_DIFFERS],
            id="601 / 601A",
        ),
        pytest.param(
            service(line="RE5"),
            service(),
            Agreement.UNKNOWN,
            [r.IDENTIFIER_MISSING],
            id="RE5 / nothing",
        ),
        pytest.param(
            service(line="88", published="88"),
            service(line="EC 194", number="194", published="EC 194", category="EC"),
            Agreement.UNKNOWN,
            [r.IDENTIFIER_KINDS_DIFFER],
            id="DB's line 88 / ÖBB's EC 194",
        ),
        pytest.param(
            service(line="RJ", published="RJ"),
            service(line="RJ 1141", number="1141", published="RJ 1141", category="RJ"),
            Agreement.UNKNOWN,
            [r.IDENTIFIER_KINDS_DIFFER],
            id="HAFAS's line that is the train's label is a number, not a line",
        ),
        pytest.param(
            service(mode=BUS, line="123", published="123"),
            service(mode=BUS, published="Bus 123"),
            Agreement.AGREE,
            [r.MODE_WORD_ONLY],
            id="a bus's three digits are a line, its mode word dropped",
        ),
        pytest.param(
            service(published="IC 801 MECSEK"),
            service(published="IC 801", category="IC"),
            Agreement.AGREE,
            [],
            id="a train name after the number",
        ),
        pytest.param(
            service(number="09730"),
            service(published="AVE 9730"),
            Agreement.AGREE,
            [],
            id="leading zeros of a number",
        ),
    ],
)
def test_the_examples_of_section_5(
    left: object, right: object, agreement: Agreement, expected: list[str]
) -> None:
    found = compare_services(left, right, leg=0)  # type: ignore[arg-type]

    assert found.agreement == agreement
    # `train_number_equal` is the agreement itself: never shown, so left out here.
    assert [code for code in codes(found) if code != r.TRAIN_NUMBER_EQUAL] == expected


def test_a_line_conflict_names_both_labels() -> None:
    found = compare_services(
        service(mode=TRAM, line="17"), service(mode=TRAM, published="Tram 1"), leg=2
    )

    (reason,) = found.reasons
    assert (reason.severity, reason.leg, reason.left_value, reason.right_value) == (
        "warning",
        2,
        "17",
        "Tram 1",
    )


def test_a_category_reason_keeps_the_sources_spelling() -> None:
    found = compare_services(service(published="TGV 6107"), service(published="TGV INOUI 6107"))

    (category,) = [item for item in found.reasons if item.code == r.CATEGORY_DIFFERS]
    assert (category.left_value, category.right_value) == ("TGV", "TGV INOUI")


@pytest.mark.parametrize(
    ("text", "mode", "expected"),
    [
        ("S 7", RAIL, ("s7", False)),
        ("Straßenbahn 5", TRAM, ("5", True)),
        ("STR 5", TRAM, ("5", True)),
        ("Tram", TRAM, ("tram", False)),
        ("Strasbourg", TRAM, ("strasbourg", False)),
        ("Tram 1", RAIL, ("tram1", False)),
        ("Metro M2", TransportMode.SUBWAY, ("m2", True)),
        ("RE-5", RAIL, ("re5", False)),
        ("", RAIL, ("", False)),
        (None, RAIL, ("", False)),
    ],
)
def test_normalise_label(text: str | None, mode: TransportMode, expected: tuple[str, bool]) -> None:
    assert normalise_label(text, mode) == expected


def test_no_number_is_read_from_a_bus_or_a_short_label() -> None:
    assert identity_of(service(mode=BUS, published="250")).number is None
    assert identity_of(service(published="RE 5")).number is None
    assert identity_of(service(published="RE 5")).line == "re5"
    assert identity_of(service(published="ICE 566")).number == "566"
