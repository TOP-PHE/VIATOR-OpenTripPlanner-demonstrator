r"""ÖBB endpoint identity — how a HAFAS location becomes a comparable token.

**Why this file exists.** `extract_uic` never worked on a real HAFAS lid. Its
regex `(?<!\d)(\d{7,8})(?!\d)` is used with `.search()` over the *whole* lid,
and a real lid orders its fields ``A= @ O= @ X= @ Y= @ U= @ L=`` — so the first
standalone 7-8 digit run it finds is the **X longitude in micro-degrees**,
never the `L=` station id. Every pre-existing fixture in this repo uses a
*stripped* lid (`A=1@L=8100002@`) with no coordinate fields, which is precisely
why the suite stayed green on a function that never worked in production.

The lids and extIds below are **verbatim from a live read-only ÖBB probe**
(``LocGeoPos`` + ``TripSearch`` against ``fahrplan.oebb.at``, 2026-09-07), not
invented. Amsterdam Centraal's real UIC ``8400058`` sits in ``L=`` with a clean
``extId`` beside it, while the old code returned ``UIC:4899427`` = 4.899427° E.

Two tests carry the history and should be read together:

- `test_leg_endpoints_are_station_ids_not_longitudes` is the **flipped**
  characterization test. It landed one commit earlier asserting the wrong
  values, so the fix shows up as a one-line diff proving behaviour changed.
- `test_extract_uic_must_never_be_called_on_a_hafas_lid` is **permanent** and
  does NOT flip: `extract_uic` is deliberately unchanged and still correct for
  VIATOR-side ids. It is the trip-wire against a new call site on a lid.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from app.journey import signature
from app.network_coverage import alignment, external_verify

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


def test_leg_endpoints_are_station_ids_not_longitudes() -> None:
    """The flipped characterization test — this is the fix, in one assertion.

    Before: `UIC:4899427` / `UIC:4335695`, the X longitudes (4.899427° E and
    4.335695° E). After: the real station ids, which were sitting in `extId`
    and in `L=` the whole time.

    The parametrized test below still pins the OLD behaviour of `extract_uic`
    itself, because that function is deliberately unchanged — what changed is
    that nothing calls it on a lid any more.
    """
    result = external_verify._parse_hafas_response(_hafas_payload())

    assert result.ok is True
    leg = result.itineraries[0].legs[0]

    assert leg.from_uic == "UIC:8400058", "Amsterdam Centraal, from extId"
    assert leg.to_uic == "UIC:8800004", "Bruxelles Midi, from extId"
    # Provenance survives to the persisted row.
    assert leg.from_id_raw == "8400058"
    assert leg.from_name == "Amsterdam Centraal"
    assert leg.from_id_source == "extid"


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


# ─────────────────────── oebb_stop_id: the token table ───────────────────────


@pytest.mark.parametrize(
    ("ext_id", "expected"),
    [
        # Live-probe values — all seven TripSearch locL entries.
        ("8400058", "UIC:8400058"),
        ("8800004", "UIC:8800004"),
        # Midi Eurostar is a genuinely DISTINCT UIC and must NOT be folded
        # into 8800004; parent/child merging is master_stations.parent_uic's
        # job, offline and recorded, never a runtime heuristic.
        ("8898004", "UIC:8898004"),
        ("8400561", "UIC:8400561"),
        ("8400530", "UIC:8400530"),
        ("8400413", "UIC:8400413"),
        ("8800007", "UIC:8800007"),
        # Zero-padded (BE/AT MOTIS shape) — padding absorbed.
        ("008100002", "UIC:8100002"),
        ("008811007", "UIC:8811007"),
        # SNCF 8-digit = UIC + check digit → keep first 7. Parity with
        # extract_uic and signature._uic_from_stop_id is mandatory.
        ("87686006", "UIC:8768600"),
        # Wien Hbf's regional BUS terminal: 6 digits, not a UIC in any scheme.
        ("904050", "OEBB:904050"),
        # 9 significant digits — no known scheme; must not be truncated into a
        # plausible-looking UIC.
        ("123456789", "OEBB:123456789"),
        # Non-numeric shapes stay opaque rather than being guessed at.
        ("at:43:300", "OEBB:at:43:300"),
        ("Wien Hbf", "OEBB:Wien Hbf"),
        # No id at all.
        ("", None),
        (None, None),
        ("   ", None),
    ],
)
def test_oebb_stop_id_extid_table(ext_id: object, expected: str | None) -> None:
    """Every verified input, and the three-state contract it produces."""
    assert external_verify.oebb_stop_id(ext_id).token == expected


def test_oebb_stop_id_non_uic_is_labelled_not_dropped() -> None:
    """The design decision, stated as an assertion.

    `904050` must become `OEBB:904050` — explicitly NOT None and NOT
    `UIC:904050`.

    - `None` is unsafe: VerifyLeg has no coordinates, so a None endpoint
      degenerates to the shared `"?,?"` token and opens an exact-tier
      collision channel. It also collapses "no id" and "foreign id" into one
      bucket, destroying the signal §7 item 2's measurement step needs.
    - `UIC:904050` is a lie in persisted JSONB and on the cell modal, and it
      invites a later "just widen the regex to 6 digits" patch — which is #226
      wearing a different mask.
    """
    got = external_verify.oebb_stop_id("904050")
    assert got.token == "OEBB:904050"
    assert got.token is not None
    assert not got.token.startswith("UIC:")
    assert got.raw == "904050", "raw is retained for a later master_stations join"


@pytest.mark.parametrize(
    "ext_id",
    ["00904050", "00000000", "000123456789", "at:43:8100002", "Wien Hbf 1234567", "904050"],
)
def test_oebb_token_can_never_reparse_as_a_uic(ext_id: str) -> None:
    """The load-bearing invariant, and the most valuable assertion here.

    An `OEBB:` token must survive both downstream parsers unchanged. This
    catches two distinct regressions: emitting RAW digits (``00904050`` is an
    8-digit run that would re-parse as the fabricated ``UIC:0090405``), and a
    future reviewer "tidying" the step-0 passthrough to run after
    `_uic_from_stop_id`.
    """
    token = external_verify.oebb_stop_id(ext_id).token
    assert token is not None
    assert token.startswith("OEBB:"), f"{ext_id!r} should not normalise into the UIC namespace"
    assert signature._fingerprint_stop_token(token, None, None) == token
    assert alignment._endpoint_token(token) == token


def test_oebb_stop_id_zero_padded_degenerate_does_not_become_a_uic() -> None:
    """`00000000` is 8 digits — the width alone would make it `UIC:0000000`.
    The `[1-9]` anchor in `_OEBB_UIC_RE` blocks it, and the payload collapses
    to `0` so it cannot re-parse either."""
    assert external_verify.oebb_stop_id("00000000").token == "OEBB:0"
    assert external_verify.oebb_stop_id("00904050").token == "OEBB:904050"


# ─────────────────────── lid fallback (no extId) ───────────────────────


def test_oebb_stop_id_lid_fallback_reads_the_L_field_not_the_coordinates() -> None:
    """The negative assertion is the point: it fails if anyone reintroduces a
    `.search()` over the whole lid."""
    got = external_verify.oebb_stop_id(None, lid=AMS_LID)
    assert got.token == "UIC:8400058"
    assert got.token != "UIC:4899427", "must not read the X longitude"
    assert got.source == "lid_L"


def test_oebb_stop_id_lid_fallback_handles_padded_and_trailing_fields() -> None:
    """Zero-padded `L=` plus a trailing `@B=1@` after it, in one case."""
    got = external_verify.oebb_stop_id(None, lid=WIEN_PADDED_LID)
    assert got.token == "UIC:8100002"
    assert got.token != "UIC:1637552", "must not read the truncated X longitude"
    assert got.raw == "008100002"


def test_oebb_stop_id_lid_without_L_field_yields_no_token() -> None:
    """No `L=` means no id — not the longitude, not the latitude."""
    assert (
        external_verify.oebb_stop_id(None, lid="A=1@O=Nowhere@X=4899427@Y=52379191@").token is None
    )


def test_oebb_stop_id_ignores_the_U_field() -> None:
    """`U=` is not a country code — the live probe shows `U=81` on both NL and
    BE stations, while docs/architecture.md:726 records `U=181` on an Austrian
    one. Only `L=` is read."""
    assert external_verify.oebb_stop_id(None, lid=AMS_LID).token == "UIC:8400058"
    assert external_verify.oebb_stop_id(None, lid=WIEN_PADDED_LID).token == "UIC:8100002"


def test_oebb_stop_id_prefers_extid_over_lid() -> None:
    """Precedence is a decision, not an accident."""
    got = external_verify.oebb_stop_id("8400058", lid="A=1@O=X@L=9999999@")
    assert got.token == "UIC:8400058"
    assert got.source == "extid"


def test_oebb_stop_id_coerces_integer_extid() -> None:
    """HAFAS field types are untrusted. #213 was exactly this shape: a numeric
    `cat` raised AttributeError and was swallowed per-cell as
    `external_error='sweep_exception'`."""
    assert external_verify.oebb_stop_id(8400058).token == "UIC:8400058"


def test_oebb_stop_id_does_not_merge_brussels_midi_variants() -> None:
    """Pins that parent/child folding is master_stations.parent_uic's job, and
    pre-empts a "helpful" proximity merge (the two are 43 m apart)."""
    assert (
        external_verify.oebb_stop_id("8800004").token
        != external_verify.oebb_stop_id("8898004").token
    )


# ─────────────────────── plumbing ───────────────────────


def test_index_hafas_locations_keeps_ext_id() -> None:
    """The single-line omission that was the proximate cause. Nothing pinned
    it before."""
    idx = external_verify._index_hafas_locations(
        [{"lid": AMS_LID, "extId": AMS_EXTID, "name": "Amsterdam Centraal"}]
    )
    assert idx[0]["ext_id"] == "8400058"
    assert idx[0]["lid"] == AMS_LID
    assert idx[0]["name"] == "Amsterdam Centraal"


def test_parse_hafas_response_emits_no_token_derivable_from_a_coordinate() -> None:
    """Generalisable trip-wire: no emitted token's digits may equal any `X=` or
    `Y=` value appearing anywhere in the payload."""
    payload = _hafas_payload()
    coords = set(re.findall(r"[XY]=(\d+)", json.dumps(payload)))
    assert coords, "fixture must actually contain coordinate fields"

    result = external_verify._parse_hafas_response(payload)
    for it in result.itineraries:
        for leg in it.legs:
            for token in (leg.from_uic, leg.to_uic):
                if not token:
                    continue
                digits = token.split(":", 1)[1]
                assert digits not in coords, f"{token} is a coordinate, not a station id"
                assert not any(c.startswith(digits) for c in coords), (
                    f"{token} looks like a truncated coordinate"
                )


def test_canonical_token_vocabulary_agrees_across_modules() -> None:
    """`external_verify` and `signature` describe the same token set. They are
    deliberate twins (external_verify imports nothing from app.journey), so
    nothing but a test keeps them honest."""
    assert signature._CANONICAL_TOKEN_RE.fullmatch(external_verify._SCHEME_UIC + "8400058")
    assert signature._CANONICAL_TOKEN_RE.fullmatch(external_verify._SCHEME_OEBB + "904050")
    assert not signature._CANONICAL_TOKEN_RE.fullmatch("SBB:8507000:0:5")
    # 8 digits after UIC: is not a shape any producer emits — both apply [:7].
    assert not signature._CANONICAL_TOKEN_RE.fullmatch("UIC:84000581")
