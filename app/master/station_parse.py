"""Parsing of the five offline station files. Pure: no database, no settings.

Each `parse_*` function turns the rows of one file into plain values shaped
for the tables they feed (docs/station-offline-file-shapes.md gives the
mapping). `app/master/station_import.py` does the writing; nothing here does.

Cell conventions, from the shapes document:

    |   several values of the same series      nap_* columns, eva_all, alt names
    ;   several tokens                         flags, op_type_all, iso2_all
    +   several source labels                  uic_merits_sources
    :   token, then payload                    inside flags
    #   feed label, then feed-local stop key   inside nap_*_regional values

An empty cell is "no value", never zero. Anything not recognised is kept as
opaque text: an unknown tier, flag token, match method or `nat_code_series`
is stored, never refused. Cells are not stripped: a PLC can contain a space,
and its seven characters are its identity.

What IS refused: a header that is not the declared shape, a master in which a
`(plc, era_uopid)` pair repeats, a PLC that is not seven characters, and a
CRD-derived row without its licence tag.
"""

from __future__ import annotations

import csv
import math
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from . import station_files as sf
from .station_files import StationFileError

Row = Mapping[str, str]
Key = tuple[str, str]  # (plc, era_uopid)

PLC_LENGTH = 7
_EXAMPLES = 10  # how many offending rows an error message names
_DATE_FORMATS = ("%Y-%m-%d", "%Y%m%d", "%d/%m/%Y", "%d.%m.%Y")
_TRUE = frozenset({"yes", "1", "true"})
_FALSE = frozenset({"no", "0", "false"})

# Origins given to the MERITS candidates that were not chosen. `Calculated` is
# the offline chain's own word; `Conflict value` names a value listed in
# `uic_merits_conflict_values`.
ORIGIN_CALCULATED = "Calculated"
ORIGIN_CONFLICT = "Conflict value"

# Spine sources whose rows are CRD-derived beyond doubt, and so must carry the
# licence tag. `ERA_only` rows carry none; other values are not asserted on.
_CRD_DERIVED = frozenset({"CRD_and_ERA", "CRD_only"})

# The CRD subsidiary codes, flattened into columns by the offline extractor.
SUBSIDIARY_COLUMNS = (
    "crd_rl100",
    "crd_sncf_codes",
    "crd_sncf_site_codes",
    "crd_ns_abbrev",
    "crd_sncb_telegraph",
    "crd_sbb_enee",
    "crd_dium_codes",
)

# station_ref columns filled straight from a master column of the same name.
_MASTER_TEXT_FIELDS = (
    "previous_plc",
    "name_src",
    "pos_src",
    "link_pos_src",
    "position_flag",
    "iso2",
    "op_type_src",
    "plc_kind",
    "spine_source",
    "crd_source_tag",
    "uic_merits",
    "uic_merits_origin",
    "uic_merits_rule",
    "uic_merits_confidence",
    "rl100",
    "nat_code",
    "nat_code_series",
    "nat_code_src",
    "ifopt_dhid",
    "ifopt_dhid_src",
    "eva",
    "eva_src",
    "warning_level",
    "best_tier",
)


# ───────────────────────────── cells ─────────────────────────────


def text_or_none(value: str | None) -> str | None:
    """An empty cell is no value."""
    return value or None


def split_cell(value: str | None, separator: str) -> list[str]:
    """The values of a multi-value cell, in order, without blanks or repeats."""
    if not value:
        return []
    return list(dict.fromkeys(part for part in value.split(separator) if part))


def parse_float(value: str | None) -> float | None:
    try:
        number = float(value) if value else None
    except ValueError:
        return None
    if number is None or not math.isfinite(number):
        return None
    return number


def parse_int(value: str | None) -> int | None:
    """`3` and `3.0` are 3; anything else, and an empty cell, is no value."""
    number = parse_float(value)
    if number is None or not number.is_integer():
        return None
    return int(number)


def parse_flag(value: str | None) -> bool | None:
    """yes/no (also 1/0, true/false). An unknown word is no value, not False."""
    word = (value or "").strip().lower()
    if word in _TRUE:
        return True
    if word in _FALSE:
        return False
    return None


def parse_date(value: str | None) -> date | None:
    """A date in any of the forms an extract plausibly writes, or None.

    `2024-12-15`, `2024-12-15T00:00:00`, `20241215`, `15/12/2024`, `15.12.2024`.
    """
    raw = (value or "").strip()
    if not raw:
        return None
    for candidate in dict.fromkeys((raw, raw[:10], raw[:8])):
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(candidate, fmt).date()
            except ValueError:
                continue
    return None


def split_flag(raw: str) -> tuple[str, str]:
    """`candidate_displaced_to:ZZ00002` -> (token, payload), split on the first colon."""
    token, _, payload = raw.partition(":")
    if not token:
        return raw, ""
    return token, payload


# ───────────────────────────── reading ─────────────────────────────


def read_rows(path: Path, fmt: str) -> Iterator[dict[str, str]]:
    """The data rows of a station file, every cell as text.

    Refuses the file unless its header is the shape of `fmt`, and refuses a
    row with more cells than columns (a quoting error would otherwise shift
    every value one column to the right, silently).
    """
    sf.require_shape(fmt, path)
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        reader.fieldnames = [name.strip() for name in reader.fieldnames or []]
        try:
            for row in reader:
                if None in row:
                    raise StationFileError(f"line {reader.line_num}: more cells than columns")
                yield {name: value or "" for name, value in row.items()}
        except (csv.Error, UnicodeDecodeError) as exc:
            raise StationFileError(f"line {reader.line_num}: unreadable ({exc})") from exc


# ───────────────────────────── the master ─────────────────────────────


@dataclass(frozen=True)
class MeritsCandidate:
    code: str
    origin: str | None
    rule: str | None
    confidence: str | None
    sources: tuple[str, ...]
    check_digit: str | None
    is_chosen: bool


@dataclass(frozen=True)
class CodeValue:
    source_key: str
    code: str
    # Listed first in its cell by the offline builder.
    is_primary: bool


@dataclass
class MasterRow:
    plc: str
    era_uopid: str
    fields: dict[str, Any]
    merits: list[MeritsCandidate]
    codes: list[CodeValue]
    flags: list[tuple[str, str]]

    @property
    def key(self) -> Key:
        return (self.plc, self.era_uopid)


def merits_candidates(row: Row) -> list[MeritsCandidate]:
    """Every MERITS code one master row carries, one candidate per distinct code.

    `uic_merits` is the chosen one. `uic_merits_candidate` is the calculated
    one; it usually equals the chosen code and is then the same candidate, and
    where it differs it stays as a second, not chosen, candidate, so a
    calculated code is never withdrawn. Each value of
    `uic_merits_conflict_values` is a further candidate. The sources and the
    check digit belong to the calculation.
    """
    chosen = row["uic_merits"]
    calculated = row["uic_merits_candidate"]
    confidence = text_or_none(row["uic_merits_confidence"])
    sources = tuple(split_cell(row["uic_merits_sources"], "+"))
    check_digit = text_or_none(row["uic_merits_check_digit"])

    out: dict[str, MeritsCandidate] = {}
    if chosen:
        same = chosen == calculated
        out[chosen] = MeritsCandidate(
            code=chosen,
            origin=text_or_none(row["uic_merits_origin"]),
            rule=text_or_none(row["uic_merits_rule"]),
            confidence=confidence,
            sources=sources if same else (),
            check_digit=check_digit if same else None,
            is_chosen=True,
        )
    if calculated and calculated not in out:
        out[calculated] = MeritsCandidate(
            code=calculated,
            origin=ORIGIN_CALCULATED,
            rule=None,
            # With nothing chosen, the row's confidence describes the calculation.
            confidence=None if chosen else confidence,
            sources=sources,
            check_digit=check_digit,
            is_chosen=False,
        )
    for value in split_cell(row["uic_merits_conflict_values"], "|"):
        if value not in out:
            out[value] = MeritsCandidate(value, ORIGIN_CONFLICT, None, None, (), None, False)
    return list(out.values())


def provider_codes(row: Row, columns: Iterable[str]) -> list[CodeValue]:
    """One code per `|`-separated value of each provider column, verbatim.

    A `feed#key` value of a regional aggregate is kept whole: splitting it
    would make two feeds' identical local keys collide on one station.
    """
    return [
        CodeValue(source_key=column, code=code, is_primary=index == 0)
        for column in columns
        for index, code in enumerate(split_cell(row[column], "|"))
    ]


def row_flags(row: Row) -> list[tuple[str, str]]:
    """The `;`-separated flags of a master row as (token, payload) pairs."""
    return list(dict.fromkeys(split_flag(raw) for raw in split_cell(row["flags"], ";")))


def master_fields(row: Row) -> dict[str, Any]:
    """The station_ref columns one master row fills."""
    alt_name = split_cell(row["era_alt_name"], "|")
    fields: dict[str, Any] = {name: text_or_none(row[name]) for name in _MASTER_TEXT_FIELDS}
    fields.update(
        name=text_or_none(row["era_name"]),
        alt_name=alt_name or None,
        alt_name_text=" | ".join(alt_name) or None,
        lat=parse_float(row["lat"]),
        lon=parse_float(row["lon"]),
        iso2_all=split_cell(row["iso2_all"], ";") or None,
        op_type_all=split_cell(row["op_type_all"], ";") or None,
        is_passenger=parse_flag(row["is_passenger"]),
        n_op_with_plc=parse_int(row["n_op_with_plc"]),
        eva_all=split_cell(row["eva_all"], "|") or None,
        n_nap_feeds=parse_int(row["n_nap_feeds"]),
    )
    return fields


def parse_master_row(row: Row, columns: Iterable[str]) -> MasterRow:
    plc = row["plc"]
    return MasterRow(
        plc=plc,
        # Where a source has no operational-point id the PLC is the sentinel.
        era_uopid=row["era_uopid"] or plc,
        fields=master_fields(row),
        merits=merits_candidates(row),
        codes=provider_codes(row, columns),
        flags=row_flags(row),
    )


def _refuse(problem: str, lines: list[str]) -> None:
    if lines:
        shown = "; ".join(lines[:_EXAMPLES])
        more = f" (and {len(lines) - _EXAMPLES} more)" if len(lines) > _EXAMPLES else ""
        raise StationFileError(f"{problem}: {shown}{more}")


def parse_master(rows: Iterable[Row]) -> list[MasterRow]:
    """Parse the master, refusing what would corrupt the reference.

    `(plc, era_uopid)` is the grain: a PLC can carry several operational
    points, and a pair that repeats cannot be told apart from its twin.
    """
    columns = sf.provider_columns()
    out: list[MasterRow] = []
    seen: set[Key] = set()
    repeated: list[str] = []
    bad_plc: list[str] = []
    untagged: list[str] = []
    for line, row in enumerate(rows, start=2):  # line 1 is the header
        parsed = parse_master_row(row, columns)
        where = f"line {line} ({parsed.plc!r}, {parsed.era_uopid!r})"
        if len(parsed.plc) != PLC_LENGTH:
            bad_plc.append(where)
        if parsed.key in seen:
            repeated.append(where)
        if parsed.fields["spine_source"] in _CRD_DERIVED and not parsed.fields["crd_source_tag"]:
            untagged.append(where)
        seen.add(parsed.key)
        out.append(parsed)
    _refuse("a PLC must be 7 characters", bad_plc)
    _refuse("the pair (plc, era_uopid) repeats", repeated)
    _refuse("a CRD-derived row carries no licence tag (crd_source_tag)", untagged)
    return out


# ───────────────────────────── links ─────────────────────────────


@dataclass
class LinkRow:
    """One row of station_ref_link. `key` is None for a stop nothing matched."""

    key: Key | None
    fields: dict[str, Any]


def parse_link_row(row: Row) -> LinkRow:
    plc = row["plc"]
    return LinkRow(
        key=(plc, row["era_uopid"] or plc),
        fields={
            # The offline NAP station id (`NAPST...`), not a database id.
            "offline_station_id": text_or_none(row["station_id"]),
            "feed_key": text_or_none(row["feed"]),
            "stop_key": text_or_none(row["stop_key"]),
            "stop_name": text_or_none(row["stop_name"]),
            "label": text_or_none(row["station_label"]),
            "code_value": text_or_none(row["code_value"]),
            "code_series": text_or_none(row["code_series"]),
            "match_method": text_or_none(row["match_method"]),
            "tier": text_or_none(row["tier"]),
            "asserted": parse_flag(row["asserted"]) is True,
            "distance_m": parse_float(row["distance_m"]),
            "name_sim": parse_float(row["name_sim"]),
            "note": text_or_none(row["note"]),
        },
    )


def parse_unmapped_row(row: Row) -> LinkRow:
    return LinkRow(
        key=None,
        fields={
            "offline_station_id": text_or_none(row["station_id"]),
            "feed_key": text_or_none(row["feeds"]),
            "stop_name": text_or_none(row["name"]),
            "iso2": text_or_none(row["iso2"]),
            "lat": parse_float(row["lat"]),
            "lon": parse_float(row["lon"]),
            "label": text_or_none(row["label"]),
            "asserted": False,
            "reason": text_or_none(row["reason"]),
            # The hint an operator works from.
            "nearest_plc": text_or_none(row["nearest_era_plc"]),
            "nearest_distance_m": parse_float(row["nearest_era_distance_m"]),
        },
    )


def link_series_index(links: Iterable[LinkRow]) -> dict[Key, dict[str, set[str]]]:
    """Per station, the series the links file names for each code value."""
    index: dict[Key, dict[str, set[str]]] = {}
    for link in links:
        code, series = link.fields.get("code_value"), link.fields.get("code_series")
        if link.key is not None and code and series:
            index.setdefault(link.key, {}).setdefault(code, set()).add(series)
    return index


def series_for(code: str, by_code: Mapping[str, set[str]] | None) -> str | None:
    """The series of a master code, when the links file states exactly one for
    the same station and the same code value. Otherwise unknown: never guessed."""
    found = (by_code or {}).get(code)
    if found is None or len(found) != 1:
        return None
    return next(iter(found))


# ───────────────────────────── registers ─────────────────────────────


@dataclass
class CrdParse:
    """What the CRD locations file yields."""

    # Per (plc, uopid): the station_ref columns that are not in the master.
    extras: dict[Key, dict[str, Any]] = field(default_factory=dict)
    locations: list[dict[str, Any]] = field(default_factory=list)
    subsidiaries: list[dict[str, Any]] = field(default_factory=list)
    rows: int = 0
    duplicates_dropped: int = 0
    dates_unparsed: int = 0


def _spine_extras(row: Row, result: CrdParse) -> dict[str, Any]:
    extras: dict[str, Any] = {
        "plc_op_max_sep_m": parse_int(row["plc_op_max_sep_m"]),
        "is_passenger_src": text_or_none(row["is_passenger_src"]),
        "n_op_with_plc": parse_int(row["n_op_with_plc"]),
    }
    for column in ("crd_start", "crd_end"):
        parsed = parse_date(row[column])
        if row[column] and parsed is None:
            result.dates_unparsed += 1
        extras[column] = parsed
    return extras


def _crd_location(row: Row) -> dict[str, Any]:
    return {
        "country": text_or_none(row["crd_country"]),
        "location_code": text_or_none(row["crd_location_code"]),
        "plc": row["plc"],
        # Kept as published; station_ref carries the parsed dates.
        "start_validity": text_or_none(row["crd_start"]),
        "end_validity": text_or_none(row["crd_end"]),
        "name": text_or_none(row["name"]),
        "lat": parse_float(row["lat"]),
        "lon": parse_float(row["lon"]),
        "passenger_flag": text_or_none(row["crd_passenger_flag"]),
        "freight_flag": text_or_none(row["crd_freight_flag"]),
        "responsible_im": text_or_none(row["crd_responsible_im"]),
        "nuts": text_or_none(row["crd_nuts"]),
    }


def parse_crd_locations(rows: Iterable[Row]) -> CrdParse:
    """The spine as the offline extractor writes it: CRD already joined with ERA.

    The file's grain is (plc, operational point), so one CRD location repeats
    on every operational point of its PLC: it is kept once, on CRD's own key
    (country, code, start of validity). A row with no CRD country and no CRD
    code is an ERA-only row and yields no CRD location.
    """
    result = CrdParse()
    seen_locations: set[tuple[str | None, str | None, str | None]] = set()
    seen_codes: set[tuple[str, str, str]] = set()
    for row in rows:
        result.rows += 1
        plc = row["plc"]
        result.extras.setdefault((plc, row["uopid"] or plc), _spine_extras(row, result))
        if row["crd_country"] or row["crd_location_code"]:
            _add_location(row, seen_locations, result)
            _add_subsidiaries(row, seen_codes, result)
    return result


def _add_location(
    row: Row, seen: set[tuple[str | None, str | None, str | None]], result: CrdParse
) -> None:
    location = _crd_location(row)
    location_key = (location["country"], location["location_code"], location["start_validity"])
    if location_key in seen:
        result.duplicates_dropped += 1
        return
    seen.add(location_key)
    result.locations.append(location)


def _add_subsidiaries(row: Row, seen: set[tuple[str, str, str]], result: CrdParse) -> None:
    """One row per non-empty value, the column name as the subsidiary type."""
    plc = row["plc"]
    for column in SUBSIDIARY_COLUMNS:
        for code in split_cell(row[column], "|"):
            if (plc, column, code) not in seen:
                seen.add((plc, column, code))
                result.subsidiaries.append({"plc": plc, "subsidiary_type": column, "code": code})


@dataclass
class TelrefParse:
    points: list[dict[str, Any]] = field(default_factory=list)
    rows: int = 0
    duplicates_dropped: int = 0
    rows_without_plc: int = 0


def parse_telref(rows: Iterable[Row]) -> TelrefParse:
    """ERA operational points, one per (plc, uopid)."""
    result = TelrefParse()
    seen: set[Key] = set()
    for row in rows:
        result.rows += 1
        plc = row["plc"]
        if not plc:
            result.rows_without_plc += 1
            continue
        key = (plc, row["uopid"] or plc)
        if key in seen:
            result.duplicates_dropped += 1
            continue
        seen.add(key)
        result.points.append(
            {
                "plc": plc,
                "uopid": key[1],
                "name": text_or_none(row["name"]),
                "op_type": text_or_none(row["op_type"]),
                "iso2": text_or_none(row["iso2"]),
                "lat": parse_float(row["lat"]),
                "lon": parse_float(row["lon"]),
            }
        )
    return result
