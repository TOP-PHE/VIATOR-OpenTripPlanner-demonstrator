r"""ÖBB endpoint identity — how a HAFAS location becomes a comparable token.

**Why this file exists.** `extract_uic` has never worked on a real HAFAS lid.
Its regex `(?<!\d)(\d{7,8})(?!\d)` is used with `.search()` over the *whole*
lid, and a real lid orders its fields ``A= @ O= @ X= @ Y= @ U= @ L=`` — so the
first standalone 7-8 digit run it finds is the **X longitude in micro-degrees**,
never the `L=` station id. Every pre-existing fixture in this repo uses a
*stripped* lid (`A=1@L=8100002@`) with no coordinate fields, which is precisely
why the suite has been green on a function that never worked in production.

The lids and extIds below are **verbatim from a live read-only ÖBB probe**
(``LocGeoPos`` + ``TripSearch`` against ``fahrplan.oebb.at``, 2026-09-07), not
invented. Amsterdam Centraal's real UIC ``8400058`` sits in ``L=`` with a clean
``extId`` beside it; the code returns ``UIC:4899427`` = 4.899427° E.

Two tests here are *characterization* tests — they assert the CURRENT, WRONG
behaviour so the fix produces a one-line reviewable diff proving the change.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.network_coverage import external_verify

# ─────────────────────── live-probe fixtures ───────────────────────
#
# Captured 2026-09-07 from TripSearch `res.common.locL`. extId is present on
# 7/7 entries and every `L=` value is UNPADDED 7 digits.

AMS_LID = "A=1@O=Amsterdam Centraal@X=4899427@Y=52379191@U=81@L=8400058@"
AMS_EXTID = "8400058"
BXL_LID = "A=1@O=Bruxelles Midi@X=4335695@Y=50835375@U=81@L=8800004@"
BXL_EXTID = "8800004"

# From docs/architecture.md:726 — the only in-tree record of a ZERO-PADDED `L=`
# carrying a trailing `B=` field after it.
WIEN_PADDED_LID = "A=1@O=Wien Hbf@X=16375526@Y=48185507@U=181@L=008100002@B=1@"

# A |lon| < 1 station (Greenwich meridian): the X run is only 6 digits, so it
# does not match the 7-8 width and `.search()` falls through to the LATITUDE.
LOW_LON_LID = "A=1@O=Test Halt@X=125730@Y=51531800@U=81@L=7015400@"


def _hafas_payload() -> dict[str, Any]:
    """A minimal but production-SHAPED TripSearch response: full lids with
    `X=`/`Y=` *and* `extId`, one JNY section Amsterdam → Bruxelles."""
    return {
        "err": "OK",
        "svcResL": [
            {
                "err": "OK",
                "res": {
                    "common": {
                        "locL": [
                            {"lid": AMS_LID, "extId": AMS_EXTID, "name": "Amsterdam Centraal"},
                            {"lid": BXL_LID, "extId": BXL_EXTID, "name": "Bruxelles Midi"},
                        ],
                        "prodL": [{"name": "EUR 9322", "prodCtx": {"catOut": "EUR"}}],
                    },
                    "outConL": [
                        {
                            "date": "20260908",
                            "dur": "015600",
                            "secL": [
                                {
                                    "type": "JNY",
                                    "dep": {"locX": 0, "dTimeS": "081000"},
                                    "arr": {"locX": 1, "aTimeS": "100600"},
                                    "jny": {"prodX": 0},
                                }
                            ],
                        }
                    ],
                },
            }
        ],
    }


# ─────────────────── characterization: the bug, pinned ───────────────────


def test_characterization_leg_endpoints_are_longitudes_not_uics() -> None:
    """CHARACTERIZATION — asserts the CURRENT, WRONG behaviour.

    `4899427` is 4.899427° E, the longitude of Amsterdam Centraal, and
    `4335695` is 4.335695° E for Bruxelles Midi. The correct answers —
    `8400058` and `8800004` — are sitting in `L=` and in `extId`, untouched.

    The fix flips these two assertions and nothing else in this test, which is
    what makes the behaviour change reviewable in one line.
    """
    result = external_verify._parse_hafas_response(_hafas_payload())

    assert result.ok is True
    leg = result.itineraries[0].legs[0]

    assert leg.from_uic == "UIC:4899427", "today: the X longitude, not UIC:8400058"
    assert leg.to_uic == "UIC:4335695", "today: the X longitude, not UIC:8800004"


@pytest.mark.parametrize(
    ("lid", "expected", "why"),
    [
        (
            AMS_LID,
            "UIC:4899427",
            "|lon| in [1,100): the 7-digit X run is returned verbatim",
        ),
        (
            WIEN_PADDED_LID,
            "UIC:1637552",
            "|lon| >= 10: X is 8 digits, so [:7] truncates it — neither a coordinate nor an id",
        ),
        (
            LOW_LON_LID,
            "UIC:5153180",
            "|lon| < 1: the X run is too short to match, so it falls through "
            "to the LATITUDE (51.531800)",
        ),
    ],
)
def test_extract_uic_must_never_be_called_on_a_hafas_lid(lid: str, expected: str, why: str) -> None:
    """PERMANENT trip-wire — this one does NOT flip.

    `extract_uic` is deliberately left unchanged by the fix: it remains the
    correct parser for VIATOR-side ids (`SBB:8507000:0:5`, SNCF 8-digit) and
    stays in mirror agreement with `signature._uic_from_stop_id`. What changes
    is that nothing calls it on a *lid* any more.

    Keeping these three regimes pinned means re-introducing such a call site is
    a visible test change, not a silent regression. All three values were
    executed against the live regex, not reasoned about.
    """
    assert external_verify.extract_uic(lid) == expected, why
