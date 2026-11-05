"""Service identity: three answers, not two (#304 design §5).

Two timed legs' services are compared by what identifies them: a train number when both sides
give one, else a line label when both do. The answer is AGREE, CONFLICT or UNKNOWN; only a
CONFLICT between two identifiers *of the same kind* counts against a pair. A train number on one
side and only a line on the other (DB's `88` and ÖBB's `EC 194`) is UNKNOWN, never a conflict.

**Normalising a label** (`normalise_label`): Unicode NFKC, case-folded, trimmed; a leading mode
word that only repeats the leg's mode dropped (`Tram 1` → `1` on a tram; the `S` of `S7` is a
product letter and stays); spaces, `-`, `.` and `_` removed.

**Where the identifiers come from** (`identity_of`): the train number is the service's
`train_number`, else, on a rail leg only, the digits of a published name `<category> <3 to 6
digits>` (`RJ 1141`, `IC 801 MECSEK`); a label of one or two digits (`S 7`, `RE 5`) is a line, and
so is any label of a bus or a tram (`123`). The line is the service's `line`, else its published
name when that is no train number. A line also matches the other side's category and line
written together (ÖBB's category `S` and line `7` against MOTIS's `S7`).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum

from ..canonical import CanonicalService, MatchReason, TransportMode
from . import reasons as r


class Agreement(StrEnum):
    AGREE = "agree"
    CONFLICT = "conflict"
    UNKNOWN = "unknown"


# A leading word that only repeats the leg's mode (§5, step 2), after case-folding: `ß` reads
# as `ss`, so `Straßenbahn` is `strassenbahn`.
MODE_WORDS: dict[TransportMode, tuple[str, ...]] = {
    TransportMode.TRAM: ("strassenbahn", "tramway", "tram", "str"),
    TransportMode.BUS: ("bus",),
    TransportMode.COACH: ("coach",),
    TransportMode.FERRY: ("ferry", "schiff"),
    TransportMode.SUBWAY: ("metro",),
}
_SEPARATORS = re.compile(r"[\s\-._]+")
# `<category> <3 to 6 digits>[letter] [name]`: `RJ 1141`, `TGV INOUI 6107`, `IC 801 MECSEK`,
# `601A`. The category is what comes before the digits.
_NUMBERED = re.compile(
    r"^(?P<category>\D*?)\s*(?P<number>\d{3,6}[a-z]?)(?:\s+\D.*)?$", re.IGNORECASE
)


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold().strip()


def normalise_label(text: str | None, mode: TransportMode | None = None) -> tuple[str, bool]:
    """The label as compared, and whether a mode word was dropped; ("", False) for none."""
    if not text:
        return "", False
    folded = _fold(text)
    dropped = False
    for word in MODE_WORDS.get(mode, ()) if mode is not None else ():
        match = re.match(rf"{word}(?![a-z])[\s\-.]*", folded)
        if match is not None and match.end() < len(folded):
            folded = folded[match.end() :]
            dropped = True
            break
    return _SEPARATORS.sub("", folded), dropped


def _number(text: str) -> str:
    """A train number as compared: case-folded, without separators, leading zeros dropped from
    an all-digit one."""
    compact = _SEPARATORS.sub("", _fold(text))
    return compact.lstrip("0") or "0" if compact.isdigit() else compact


@dataclass(frozen=True)
class Identity:
    """What identifies a leg's service: its train number, its line, its category, and the raw
    labels shown in a reason."""

    number: str | None
    line: str | None
    category: str | None
    raw_number: str | None
    raw_line: str | None
    raw_category: str | None
    line_dropped_mode_word: bool


def _category_of(service: CanonicalService) -> str | None:
    code = service.product_category.code if service.product_category else None
    return _fold(code) if code else None


def _given_number(service: CanonicalService) -> tuple[str | None, str | None]:
    """The service's own train number, as compared and as given."""
    raw = (service.train_number or "").strip()
    return (_number(raw), raw) if raw else (None, None)


def _published_numbered(service: CanonicalService) -> re.Match[str] | None:
    """The published name read as `<category> <3 to 6 digits>`, on a rail leg only."""
    published = unicodedata.normalize("NFKC", service.published_name or "").strip()
    if service.mode != TransportMode.RAIL or not published:
        return None
    return _NUMBERED.match(published)


def identity_of(service: CanonicalService) -> Identity:
    """The identity of a timed leg's service (see the module docstring)."""
    number, raw_number = _given_number(service)
    category = _category_of(service)
    published = unicodedata.normalize("NFKC", service.published_name or "").strip()
    numbered = _published_numbered(service)
    raw_category = numbered.group("category").strip() or None if numbered is not None else None
    if numbered is not None and number is None:
        number, raw_number = _number(numbered.group("number")), published
    if category is None and raw_category is not None:
        category = _SEPARATORS.sub("", _fold(raw_category)) or None
    raw_line = (service.line or "").strip() or (published if numbered is None else "") or None
    line, dropped = normalise_label(raw_line, service.mode)
    if number is not None and line and _is_the_train_label(line, number, category):
        # HAFAS gives a long-distance train's label as its line (`RJ 1141`): a number, not a line.
        line, raw_line, dropped = "", None, False
    return Identity(
        number=number,
        line=line or None,
        category=category,
        raw_number=raw_number,
        raw_line=raw_line,
        raw_category=raw_category,
        line_dropped_mode_word=dropped,
    )


def _is_the_train_label(line: str, number: str, category: str | None) -> bool:
    """Whether a normalised line is only the train's number, with its category or not."""
    if not line.endswith(number):
        return False
    prefix = line[: len(line) - len(number)].rstrip("0")
    return prefix in ("", category or "")


def _line_forms(identity: Identity) -> set[str]:
    """The line alone, and the category and line written together (`s` + `7` = `s7`)."""
    forms = {identity.line} if identity.line else set()
    if identity.line and identity.category and not identity.line.startswith(identity.category):
        forms.add(identity.category + identity.line)
    return forms


@dataclass(frozen=True)
class ServiceComparison:
    agreement: Agreement
    # `number` or `line`: what was compared; None when nothing was.
    kind: str | None
    reasons: tuple[MatchReason, ...]


def compare_services(
    left: CanonicalService, right: CanonicalService, *, leg: int | None = None
) -> ServiceComparison:
    """AGREE, CONFLICT or UNKNOWN for two legs' services (the table of §5). The reasons say why;
    a train-number CONFLICT carries `train_number_differs` with a severity the caller sets from
    its policy (M2: `scoring.number_conflict_reason`)."""
    a, b = identity_of(left), identity_of(right)
    if a.number is not None and b.number is not None:
        if a.number != b.number:
            return ServiceComparison(
                Agreement.CONFLICT,
                "number",
                (
                    r.reason(
                        r.TRAIN_NUMBER_DIFFERS,
                        "veto",
                        leg=leg,
                        left_value=a.raw_number,
                        right_value=b.raw_number,
                    ),
                ),
            )
        found = [r.reason(r.TRAIN_NUMBER_EQUAL, "info", leg=leg)]
        if a.category and b.category and a.category != b.category:
            found.append(
                r.reason(
                    r.CATEGORY_DIFFERS,
                    "info",
                    leg=leg,
                    left_value=_category_label(left, a),
                    right_value=_category_label(right, b),
                )
            )
        return ServiceComparison(Agreement.AGREE, "number", tuple(found))
    if a.line is not None and b.line is not None:
        if _line_forms(a) & _line_forms(b):
            return ServiceComparison(Agreement.AGREE, "line", _label_reasons(a, b, leg))
        return ServiceComparison(
            Agreement.CONFLICT,
            "line",
            (
                r.reason(
                    r.LINE_LABEL_DIFFERS,
                    "warning",
                    leg=leg,
                    left_value=a.raw_line,
                    right_value=b.raw_line,
                ),
            ),
        )
    if (a.number is not None or a.line is not None) and (
        b.number is not None or b.line is not None
    ):
        return ServiceComparison(
            Agreement.UNKNOWN, None, (r.reason(r.IDENTIFIER_KINDS_DIFFER, "info", leg=leg),)
        )
    return ServiceComparison(
        Agreement.UNKNOWN, None, (r.reason(r.IDENTIFIER_MISSING, "info", leg=leg),)
    )


def _label_reasons(a: Identity, b: Identity, leg: int | None) -> tuple[MatchReason, ...]:
    """The info reasons of two lines that agree although written differently."""
    if a.raw_line == b.raw_line:
        return ()
    code = r.MODE_WORD_ONLY if a.line_dropped_mode_word or b.line_dropped_mode_word else None
    return (
        r.reason(
            code or r.LABEL_SPACING_ONLY,
            "info",
            leg=leg,
            left_value=a.raw_line,
            right_value=b.raw_line,
        ),
    )


def _category_label(service: CanonicalService, identity: Identity) -> str | None:
    """The category as the source wrote it: its product category, else the words before the
    number of its published name."""
    if service.product_category and service.product_category.code:
        return service.product_category.code
    return identity.raw_category or identity.category
