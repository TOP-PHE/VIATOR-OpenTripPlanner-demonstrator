"""Hand corrections of the station reference, and how a rebuild re-applies them.

A correction is one row of `station_ref_override`: one field of one station,
with a reason. `station_ref` holds the *effective* value, so a search or a
filter sees the correction. The rule a rebuild follows is `apply_overrides`:

  * the build computes every field from its inputs, as if nothing was corrected;
  * for each active correction, the corrected value replaces the computed one;
  * the computed value is remembered (`computed_value_latest`), so releasing
    the correction restores what the build says now, not what it said then;
  * a correction is per field: when a rebuild improves a *different* field of
    a corrected row, that improvement goes through untouched.

Pure: no database. Values travel as text (the override table is one text
column for every field) and are cast to the column's type here.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .station_parse import ORIGIN_CALCULATED

MERITS_FIELD = "uic_merits"
_ISO2_LENGTH = 2


def _text(value: str) -> str:
    return value


def _iso2(value: str) -> str:
    if len(value) != _ISO2_LENGTH or not value.isalpha():
        raise ValueError("a country is two letters")
    return value.upper()


def _coordinate(limit: float) -> Callable[[str], float]:
    def cast(value: str) -> float:
        number = float(value)
        if not -limit <= number <= limit:
            raise ValueError(f"must be between {-limit:g} and {limit:g}")
        return number

    return cast


def _bool(value: str) -> bool:
    word = value.strip().lower()
    if word in ("true", "yes", "1"):
        return True
    if word in ("false", "no", "0"):
        return False
    raise ValueError("must be true or false")


# The fields a content manager may correct, and how their text form is cast.
OVERRIDABLE_FIELDS: dict[str, Callable[[str], Any]] = {
    "name": _text,
    "lat": _coordinate(90.0),
    "lon": _coordinate(180.0),
    "iso2": _iso2,
    "is_passenger": _bool,
    MERITS_FIELD: _text,
    "rl100": _text,
    "nat_code": _text,
    "ifopt_dhid": _text,
    "eva": _text,
}


def to_text(value: Any) -> str | None:
    """The text form a value is stored under in the override table."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return repr(value)
    return str(value)


def from_text(field_name: str, value: str | None) -> Any:
    """Cast an override's text to the column's type. None or '' means "no value".

    Raises ValueError for a field that cannot be corrected or a value that
    does not fit it.
    """
    cast = OVERRIDABLE_FIELDS.get(field_name)
    if cast is None:
        raise ValueError(
            f"{field_name!r} cannot be corrected by hand; fields that can: "
            f"{', '.join(sorted(OVERRIDABLE_FIELDS))}"
        )
    if value is None or value == "":
        return None
    try:
        return cast(value)
    except ValueError as exc:
        raise ValueError(f"{field_name}: {exc}") from exc


@dataclass(frozen=True)
class ActiveOverride:
    field_name: str
    value: str | None
    computed_value_at_set: str | None


@dataclass(frozen=True)
class OverrideOutcome:
    field_name: str
    # False when the correction could not be cast any more (its field is no
    # longer correctable, or its value no longer fits): the build must not
    # replace anything while a correction is being dropped.
    applied: bool
    # What the build computed underneath the correction, as text.
    computed: str | None
    # The computed value moved since the correction was made: worth a review.
    drifted: bool
    # The build now computes the corrected value itself: the correction is moot.
    redundant: bool


def apply_overrides(
    computed: Mapping[str, Any], overrides: Iterable[ActiveOverride]
) -> tuple[dict[str, Any], list[OverrideOutcome]]:
    """Re-apply a station's active corrections on top of freshly computed fields.

    Returns the effective fields and one outcome per correction. Fields without
    a correction come out exactly as computed.
    """
    effective = dict(computed)
    outcomes: list[OverrideOutcome] = []
    for override in overrides:
        name = override.field_name
        computed_text = to_text(computed.get(name))
        try:
            effective[name] = from_text(name, override.value)
        except ValueError:
            outcomes.append(OverrideOutcome(name, False, computed_text, False, False))
            continue
        outcomes.append(
            OverrideOutcome(
                field_name=name,
                applied=True,
                computed=computed_text,
                drifted=computed_text != override.computed_value_at_set,
                redundant=computed_text == to_text(effective[name]),
            )
        )
    return effective, outcomes


# ───────────────────────── the MERITS candidates ─────────────────────────

ORIGIN_MANUAL = "Manual"
# The station_ref columns that mirror the chosen MERITS candidate.
#
# Not `uic_merits_rule`: that column is the build's own text, and a correction
# never writes it. Where the master gives a station no code, it carries a
# sentence saying why and no candidate, so the sentence is on the row and
# nowhere else. Were the mirror to write the rule, the correction's reason
# would replace it and a release, with nothing chosen, could only blank it.
# Left alone, it is also what the latest build computed whenever the
# correction is released. The reason of a correction is on its override row,
# and on the `Manual` candidate when the correction adds one.
MERITS_MIRROR = ("uic_merits", "uic_merits_origin", "uic_merits_confidence")


def merits_with_override(
    candidates: Iterable[Mapping[str, Any]], value: str | None, reason: str | None
) -> list[dict[str, Any]]:
    """The candidates of a station once its MERITS code is corrected to `value`.

    Nothing computed is withdrawn: every candidate stays, none of them chosen,
    except the one carrying `value`. If no candidate carries it, a `Manual`
    one is added. A `value` of None means "this station has no MERITS code".
    """
    # A `Manual` candidate left by an earlier correction goes: one at most.
    out = [
        {**candidate, "is_chosen": False}
        for candidate in candidates
        if candidate.get("origin") != ORIGIN_MANUAL
    ]
    if value is None:
        return out
    for candidate in out:
        if candidate["code"] == value:
            candidate["is_chosen"] = True
            return out
    out.append(
        {
            "code": value,
            "origin": ORIGIN_MANUAL,
            "rule": reason,
            "confidence": None,
            "sources": None,
            "check_digit": None,
            "is_chosen": True,
        }
    )
    return out


def merits_without_override(
    candidates: Iterable[Mapping[str, Any]], computed: str | None
) -> list[dict[str, Any]]:
    """The candidates once a MERITS correction is released: the `Manual` one
    goes, and the code the build computes is the chosen one again."""
    return [
        {**candidate, "is_chosen": computed is not None and candidate["code"] == computed}
        for candidate in candidates
        if candidate.get("origin") != ORIGIN_MANUAL
    ]


def merits_mirror(candidates: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """The station_ref columns that mirror the chosen candidate (`MERITS_MIRROR`).

    With nothing chosen there is no code and no origin, and the confidence is
    the calculated candidate's. Where the master chooses no code, its
    confidence describes the calculation: the build puts it on the row and on
    that candidate (`station_parse.merits_candidates`). Blanked instead, a
    released correction would leave the row without it until the next build
    wrote it back, as a change that never happened.
    """
    candidates = list(candidates)
    chosen = next((c for c in candidates if c["is_chosen"]), None)
    if chosen is None:
        confidence = next(
            (c.get("confidence") for c in candidates if c.get("origin") == ORIGIN_CALCULATED), None
        )
        return {**dict.fromkeys(MERITS_MIRROR), "uic_merits_confidence": confidence}
    return {
        "uic_merits": chosen["code"],
        "uic_merits_origin": chosen.get("origin"),
        "uic_merits_confidence": chosen.get("confidence"),
    }
