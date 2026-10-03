"""The five offline station files: their shapes, and nothing that touches a database.

The files are produced by the offline mapping chain and are never in this
repository; only their shape is, in docs/station-offline-file-shapes.md. The
headers below are that document's, column for column, and a unit test reads
the document and fails if the two drift apart.

All five are UTF-8 with a BOM, RFC 4180, comma-separated, header on the first
line. Every cell is read as text: several columns look numeric and are not (a
leading zero is significant in a code).

A `station_source` declares in its `format` which of these shapes it accepts,
so the same check serves the upload route (refuse early, with the list of
missing and unexpected columns) and the importer (refuse again before a build).
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

# The `format` values of the five sources the step 1 importer needs.
MASTER = "station_master_csv"
LINKS = "station_links_csv"
UNMAPPED = "station_unmapped_csv"
CRD_LOCATIONS = "crd_locations_csv"
ERA_TELREF = "era_telref_csv"

# The 36 columns of the ERA telref extract. The CRD locations file starts with
# the same 36, by design of the offline chain.
_TELREF_HEADER = (
    "plc,iso2,uopid,era_uri,name,op_type,is_passenger,lat,lon,n_plc_on_op,n_op_with_plc,"
    "plc_prefix,plc_kind,iso3,era_iso2_table,iso2_all,n_countries,op_type_all,n_op_types,"
    "is_passenger_src,alt_name,n_names,position_flag,lat_dp,lon_dp,location_uri,n_locations,"
    "plc_op_max_sep_m,plc_op_names_agree,net_element,n_line_refs,line_ids,line_ids_src,"
    "n_tracks,n_sidings,local_rules_or_restrictions"
)

_HEADERS: dict[str, str] = {
    MASTER: (
        "plc,era_uopid,era_name,era_alt_name,op_type_all,is_passenger,plc_kind,iso2,iso2_all,"
        "lat,lon,position_flag,n_op_with_plc,rl100,nat_code,nat_code_series,nat_code_src,"
        "ifopt_dhid,ifopt_dhid_src,eva,eva_src,eva_all,uic_merits_candidate,"
        "uic_merits_check_digit,uic_merits_sources,uic_merits_n_sources,uic_merits_confidence,"
        "uic_merits_conflict_values,uic_merits_collision,uic_merits,uic_merits_origin,"
        "uic_merits_rule,nap_AT_OEBB,nap_BE_SNCB,nap_CH_SBB,nap_CH_SBB_non_rail_members,"
        "nap_CZ_CZPTT,nap_DE_DELFI,nap_ES_OUIGO,nap_ES_RENFE,nap_ES_regional,nap_EUROSTAR,"
        "nap_FR_SNCF,nap_FR_TRENITALIA_FR,nap_FR_regional,nap_IT_TRENITALIA,nap_LU,nap_NL_IFF,"
        "n_nap_feeds,nap_station_ids,best_match_method,best_tier,review_links,flags,"
        "warning_level,warnings,n_issues,n_warnings,spine_source,previous_plc,crd_rl100,"
        "crd_sncf_codes,crd_ns_abbrev,crd_sbb_enee,crd_dium_codes,era_name_2022,era_crd_dist_m,"
        "name_src,pos_src,op_type_src,crd_source_tag,link_pos_src,crd_key_check"
    ),
    LINKS: (
        "plc,era_uopid,station_id,feed,railway_label,stop_key,stop_name,code_value,code_series,"
        "match_method,tier,asserted,distance_m,name_sim,rail_served,modes,station_distance_m,"
        "station_name_sim,station_label,note,spine_source,link_pos_src,"
        "station_distance_spine_m,crd_source_tag"
    ),
    UNMAPPED: (
        "station_id,iso2,name,lat,lon,rail_served,rail_repl,label,modes,feeds,uic_intl_all,"
        "uic_intl_nl_chb,eva_delfi,reason,review_links,nearest_era_plc,nearest_era_name,"
        "nearest_era_op_type_all,nearest_era_distance_m,nearest_spine_source,nearest_name_src,"
        "nearest_pos_src,crd_source_tag"
    ),
    CRD_LOCATIONS: (
        _TELREF_HEADER + ",spine_source,crd_country,crd_location_code,crd_start,crd_end,"
        "crd_passenger_flag,crd_freight_flag,crd_responsible_im,crd_nuts,crd_rl100,"
        "crd_sncf_codes,crd_sncf_site_codes,crd_ns_abbrev,crd_sncb_telegraph,crd_sbb_enee,"
        "crd_dium_codes,previous_plc,era_name,era_lat,era_lon,era_crd_dist_m,name_src,pos_src,"
        "op_type_src,era_crd_passenger_disagree,crd_source_tag,crd_position_issue,"
        "era_position_alternative,crd_key_check"
    ),
    ERA_TELREF: _TELREF_HEADER,
}

FILE_SHAPES: dict[str, tuple[str, ...]] = {
    fmt: tuple(header.split(",")) for fmt, header in _HEADERS.items()
}

# A build needs all five: a partial import leaves screens silently empty.
REQUIRED_FORMATS: tuple[str, ...] = tuple(FILE_SHAPES)

FORMAT_LABELS: dict[str, str] = {
    MASTER: "station master (station_master_crd_*.csv)",
    LINKS: "station links (station_links_crd_*.csv)",
    UNMAPPED: "unmatched stops (nap_rail_stations_unmapped_crd_*.csv)",
    CRD_LOCATIONS: "CRD locations (crd_locations_*.csv)",
    ERA_TELREF: "ERA telref extract (telref_locations_v3.csv)",
}

# In the master, every `nap_*` column is a provider column except this one,
# which lists the offline station ids the row is linked to.
_NOT_A_PROVIDER_COLUMN = "nap_station_ids"


class StationFileError(ValueError):
    """A station file the importer refuses. The message is for the operator."""


@dataclass(frozen=True)
class HeaderCheck:
    """How a file's header differs from the shape its source declares."""

    missing: tuple[str, ...]
    unexpected: tuple[str, ...]
    duplicated: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not (self.missing or self.unexpected or self.duplicated)

    def describe(self) -> str:
        """Which columns are missing, unexpected or repeated. Never a guess."""
        parts = [
            f"{label}: {', '.join(columns)}"
            for label, columns in (
                ("missing columns", self.missing),
                ("unexpected columns", self.unexpected),
                ("repeated columns", self.duplicated),
            )
            if columns
        ]
        return "; ".join(parts) or "header matches"


def provider_columns(header: tuple[str, ...] | None = None) -> tuple[str, ...]:
    """The provider columns of a master header, in file order."""
    columns = FILE_SHAPES[MASTER] if header is None else header
    return tuple(c for c in columns if c.startswith("nap_") and c != _NOT_A_PROVIDER_COLUMN)


def check_header(fmt: str, header: list[str]) -> HeaderCheck:
    """Compare a header with the shape of `fmt`. Column order is not significant."""
    expected = FILE_SHAPES.get(fmt)
    if expected is None:
        raise StationFileError(f"{fmt!r} is not one of the station file shapes")
    seen: set[str] = set()
    duplicated: list[str] = []
    for column in header:
        if column in seen and column not in duplicated:
            duplicated.append(column)
        seen.add(column)
    return HeaderCheck(
        missing=tuple(c for c in expected if c not in seen),
        unexpected=tuple(dict.fromkeys(c for c in header if c not in expected)),
        duplicated=tuple(duplicated),
    )


def read_header(path: Path) -> list[str]:
    """The first line of a station file, as column names. `utf-8-sig` drops the BOM."""
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            header = next(csv.reader(handle), None)
    except UnicodeDecodeError as exc:
        raise StationFileError("the file is not UTF-8 text") from exc
    if not header:
        raise StationFileError("the file is empty")
    return [column.strip() for column in header]


def require_shape(fmt: str, path: Path) -> list[str]:
    """Read a file's header and refuse the file unless it has the shape of `fmt`."""
    header = read_header(path)
    check = check_header(fmt, header)
    if not check.ok:
        raise StationFileError(
            f"the header is not that of the {FORMAT_LABELS[fmt]}: {check.describe()}"
        )
    return header
