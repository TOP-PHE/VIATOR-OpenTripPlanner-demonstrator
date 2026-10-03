"""Synthetic station files for the station panel tests.

The five offline files are never in this repository (it is public, and they
carry RNE-licensed rows). Everything built here is invented: the PLC prefix
`ZZ` does not exist, the names are made up, the codes use a `99` prefix.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from starlette.requests import Request

STATION_TABS = (
    "/admin/stations/nap",
    "/admin/stations/registers",
    "/admin/stations/trainline",
    "/admin/stations/reference",
    "/admin/stations/sources",
)


def page_request(path: str, role: str | None) -> Request:
    """A browser request for a page, as `role` (None: not logged in).

    The page guard reads the JWT only, so a page can be rendered in a unit
    test without a database or a running app.
    """
    headers: list[tuple[bytes, bytes]] = []
    if role is not None:
        from app.auth import tokens

        jwt = tokens.issue_jwt(uuid.uuid4(), f"{role}@viator.example", role)
        headers.append((b"authorization", f"Bearer {jwt}".encode()))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "query_string": b"",
            "headers": headers,
        }
    )


_SCRIPT_RE = re.compile(
    r"<script(?P<attrs>[^>]*)>(?P<body>.*?)</script\s*>", re.DOTALL | re.IGNORECASE
)


def inline_scripts(html: str) -> list[tuple[bool, str]]:
    """The inline scripts of a rendered page: (is an ES module, source)."""
    out = []
    for match in _SCRIPT_RE.finditer(html):
        attrs = match.group("attrs")
        if "src=" in attrs or not match.group("body").strip():
            continue
        if "application/json" in attrs:  # a data block, not a script
            continue
        out.append(('type="module"' in attrs, match.group("body")))
    return out


def node_or_skip() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed; skipping the JavaScript checks")
    return node


def assert_scripts_parse(html: str, tmp_path: Path) -> int:
    """Run `node --check` on every inline script of a rendered page.

    The templates' JavaScript is never executed by the Python tests, so a
    syntax error would only show in a browser. Returns how many were checked.
    """
    node = node_or_skip()
    scripts = inline_scripts(html)
    for index, (is_module, source) in enumerate(scripts):
        path = tmp_path / f"inline_{index}.{'mjs' if is_module else 'cjs'}"
        path.write_text(source, encoding="utf-8")
        result = subprocess.run(
            [node, "--check", str(path)], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, f"script {index} does not parse:\n{result.stderr}"
    return len(scripts)


def run_in_node(script: str, expression: str) -> Any:
    """Evaluate `expression` after running a classic inline script, in Node.

    Enough for helpers that do not touch the DOM until they are called.
    """
    node = node_or_skip()
    program = script + "\nprocess.stdout.write(JSON.stringify(" + expression + "));\n"
    result = subprocess.run(
        [node, "-e", program], capture_output=True, text=True, check=False, encoding="utf-8"
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def csv_bytes(header: Sequence[str], *rows: dict[str, str]) -> bytes:
    """A station file as the offline chain writes it: UTF-8 with BOM, CRLF,
    RFC 4180 quoting. Columns a row does not name are empty."""
    lines = [",".join(header)]
    lines += [",".join(_cell(row.get(column, "")) for column in header) for row in rows]
    return ("\r\n".join(lines) + "\r\n").encode("utf-8-sig")


def _cell(value: str) -> str:
    if any(ch in value for ch in ',"\r\n'):
        return '"' + value.replace('"', '""') + '"'
    return value


# ─────────────────── a complete, invented set of the five files ───────────────────
#
# Five reference rows over four PLCs:
#
#   ZZ00001 / ZZ00001   Exampleville Central   passenger, MERITS chosen = calculated
#   ZZ00002 / ZZ00002   Sampleton              passenger, MERITS chosen differs from the
#                                              calculated one, two conflict values,
#                                              renumbered from ZZ00009
#   ZZ00003 / ZZOP03A   Testbury Yard A        two operational points on one PLC,
#   ZZ00003 / ZZOP03B   Testbury Yard B        retired in CRD (crd_end is past)
#   ZZ00004 / ZZ00004   Mockford Halt          ERA-only: no CRD row, no licence tag,
#                                              no position, a calculated MERITS code
#                                              that was not chosen
#
# `LICENCE_TAG` stands for the constant the offline chain writes on CRD-derived rows.

LICENCE_TAG = "CRD-derived (synthetic test tag)"


def master_rows() -> list[dict[str, str]]:
    return [
        {
            "plc": "ZZ00001",
            "era_uopid": "ZZ00001",
            "era_name": "Exampleville Central",
            "era_alt_name": "Exampleville|Exampleville Hbf",
            "op_type_all": "station;junction",
            "is_passenger": "yes",
            "plc_kind": "national",
            "iso2": "ZZ",
            "iso2_all": "ZZ",
            "lat": "50.000000",
            "lon": "4.000000",
            "position_flag": "ok",
            "n_op_with_plc": "1",
            "eva": "9900001",
            "eva_src": "DELFI+CRD",
            "eva_all": "9900001|9900002",
            "uic_merits_candidate": "9900001",
            "uic_merits_check_digit": "5",
            "uic_merits_sources": "TRAINLINE+CALC",
            "uic_merits_n_sources": "2",
            "uic_merits_confidence": "high",
            "uic_merits": "9900001",
            "uic_merits_origin": "Trainline = calculated",
            "uic_merits_rule": "Trainline and the calculation agree",
            "nap_CH_SBB": "9900001|9900001:0:1",
            "nap_ES_regional": "ZZ_FEED#MC",
            "n_nap_feeds": "2",
            "nap_station_ids": "NAPST0001",
            "best_tier": "T0_code_exact",
            "flags": "candidate_displaced_to:ZZ00002;plc_kind_national",
            "warning_level": "INFO",
            "spine_source": "CRD_and_ERA",
            "name_src": "CRD",
            "pos_src": "CRD",
            "op_type_src": "ERA",
            "crd_source_tag": LICENCE_TAG,
        },
        {
            "plc": "ZZ00002",
            "era_uopid": "ZZ00002",
            "era_name": "Sampleton",
            "is_passenger": "yes",
            "plc_kind": "national",
            "iso2": "ZZ",
            "iso2_all": "ZZ;YY",
            "lat": "50.100000",
            "lon": "4.100000",
            "position_flag": "ok",
            "n_op_with_plc": "1",
            "uic_merits_candidate": "9900012",
            "uic_merits_check_digit": "7",
            "uic_merits_sources": "CALC",
            "uic_merits_confidence": "conflict",
            "uic_merits_conflict_values": "9900022|9900032",
            "uic_merits": "9900002",
            "uic_merits_origin": "Trainline (calculated differs)",
            "uic_merits_rule": "Trainline preferred over the calculation",
            "nap_DE_DELFI": "de:99:2",
            "n_nap_feeds": "1",
            "best_tier": "T2_name_distance",
            "flags": "swap_partner:ZZ00001",
            "warning_level": "WARNING",
            "spine_source": "CRD_and_ERA",
            "previous_plc": "ZZ00009",
            "name_src": "ERA",
            "pos_src": "ERA",
            "op_type_src": "ERA",
            "crd_source_tag": LICENCE_TAG,
        },
        {
            "plc": "ZZ00003",
            "era_uopid": "ZZOP03A",
            "era_name": "Testbury Yard A",
            "is_passenger": "no",
            "plc_kind": "national",
            "iso2": "ZZ",
            "iso2_all": "ZZ",
            "lat": "50.200000",
            "lon": "4.200000",
            "position_flag": "ok",
            "n_op_with_plc": "2",
            "n_nap_feeds": "0",
            "best_tier": "none",
            "flags": "shares_plc_with:ZZ00003",
            "warning_level": "OK",
            "spine_source": "CRD_only",
            "name_src": "CRD",
            "pos_src": "CRD",
            "op_type_src": "CRD_derived",
            "crd_source_tag": LICENCE_TAG,
        },
        {
            "plc": "ZZ00003",
            "era_uopid": "ZZOP03B",
            "era_name": "Testbury Yard B",
            "is_passenger": "no",
            "plc_kind": "national",
            "iso2": "ZZ",
            "iso2_all": "ZZ",
            "lat": "50.200000",
            "lon": "4.200000",
            "position_flag": "ok",
            "n_op_with_plc": "2",
            "n_nap_feeds": "0",
            "best_tier": "none",
            "warning_level": "OK",
            "spine_source": "CRD_only",
            "name_src": "CRD",
            "pos_src": "CRD",
            "op_type_src": "CRD_derived",
            "crd_source_tag": LICENCE_TAG,
        },
        {
            "plc": "ZZ00004",
            "era_uopid": "ZZ00004",
            "era_name": "Mockford Halt",
            "is_passenger": "yes",
            "plc_kind": "national",
            "iso2": "ZZ",
            "iso2_all": "ZZ",
            "position_flag": "location_resource_empty",
            "n_op_with_plc": "1",
            # Values the importer has never seen: stored as text, never refused.
            "nat_code": "ZZ-77",
            "nat_code_series": "ZZ_series_of_tomorrow",
            "uic_merits_candidate": "9900004",
            "uic_merits_check_digit": "1",
            "uic_merits_sources": "CALC",
            "uic_merits_confidence": "low",
            "n_nap_feeds": "0",
            "best_tier": "T9_tier_of_tomorrow",
            "flags": "token_of_tomorrow:with:colons;bare_token",
            "warning_level": "ISSUE",
            "spine_source": "ERA_only",
            "name_src": "ERA",
            "pos_src": "none",
            "op_type_src": "ERA",
        },
    ]


def crd_location_rows() -> list[dict[str, str]]:
    def row(plc: str, uopid: str, name: str, **extra: str) -> dict[str, str]:
        base = {
            "plc": plc,
            "iso2": "ZZ",
            "uopid": uopid,
            "name": name,
            "lat": "50.0",
            "lon": "4.0",
            "n_op_with_plc": "1",
            "plc_op_max_sep_m": "0",
            "is_passenger_src": "ERA",
            "spine_source": "CRD_and_ERA",
            "crd_country": "ZZ",
            "crd_location_code": plc[2:],
            "crd_start": "2019-12-15",
            "crd_passenger_flag": "true",
            "crd_freight_flag": "false",
            "crd_responsible_im": "ZZ Infra",
            "crd_nuts": "ZZ001",
            "crd_source_tag": LICENCE_TAG,
        }
        base.update(extra)
        return base

    yard = {
        "n_op_with_plc": "2",
        "plc_op_max_sep_m": "140",
        "is_passenger_src": "CRD_derived",
        "spine_source": "CRD_only",
        "crd_end": "2021-06-30",
        "crd_passenger_flag": "false",
        "crd_freight_flag": "true",
    }
    return [
        row(
            "ZZ00001",
            "ZZ00001",
            "Exampleville Central",
            crd_rl100="ZEXC",
            crd_sncf_codes="99001|99002",
            crd_dium_codes="990001",
        ),
        row("ZZ00002", "ZZ00002", "Sampleton", lat="50.1", lon="4.1", previous_plc="ZZ00009"),
        # One CRD location, repeated on each operational point of its PLC.
        row("ZZ00003", "ZZOP03A", "Testbury Yard", lat="50.2", lon="4.2", **yard),
        row("ZZ00003", "ZZOP03B", "Testbury Yard", lat="50.2", lon="4.2", **yard),
        {
            "plc": "ZZ00004",
            "iso2": "ZZ",
            "uopid": "ZZ00004",
            "name": "Mockford Halt",
            "n_op_with_plc": "1",
            "is_passenger_src": "ERA",
            "spine_source": "ERA_only",
        },
    ]


def telref_rows() -> list[dict[str, str]]:
    return [
        {"plc": "ZZ00001", "uopid": "ZZ00001", "name": "Exampleville Central", "iso2": "ZZ",
         "op_type": "station", "lat": "50.0", "lon": "4.0"},
        {"plc": "ZZ00002", "uopid": "ZZ00002", "name": "Sampleton", "iso2": "ZZ",
         "op_type": "station", "lat": "50.1", "lon": "4.1"},
        {"plc": "ZZ00003", "uopid": "ZZOP03A", "name": "Testbury Yard A", "iso2": "ZZ",
         "op_type": "yard", "lat": "50.2", "lon": "4.2"},
        {"plc": "ZZ00003", "uopid": "ZZOP03B", "name": "Testbury Yard B", "iso2": "ZZ",
         "op_type": "yard", "lat": "50.2", "lon": "4.2"},
        {"plc": "ZZ00004", "uopid": "ZZ00004", "name": "Mockford Halt", "iso2": "ZZ",
         "op_type": "halt"},
    ]  # fmt: skip


def link_rows() -> list[dict[str, str]]:
    return [
        # Asserted, by code: the series of the master's code 9900001 comes from here.
        {"plc": "ZZ00001", "era_uopid": "ZZ00001", "station_id": "NAPST0001", "feed": "CH_SBB",
         "stop_key": "9900001", "stop_name": "Exampleville Central", "code_value": "9900001",
         "code_series": "CH_service_point_number", "match_method": "code then name",
         "tier": "T0_code_exact", "asserted": "yes", "distance_m": "12.5", "name_sim": "0.98",
         "station_label": "Rail", "crd_source_tag": LICENCE_TAG},
        # A series the vocabulary has never seen, on a code the master carries.
        {"plc": "ZZ00001", "era_uopid": "ZZ00001", "station_id": "NAPST0005", "feed": "ZZ_FEED",
         "stop_key": "MC", "stop_name": "Exampleville", "code_value": "ZZ_FEED#MC",
         "code_series": "ZZ_series_from_links", "match_method": "feed key",
         "tier": "T1_feed_key", "asserted": "yes", "distance_m": "30", "name_sim": "0.9",
         "station_label": "Rail"},
        # Not asserted although the stop carries a code: the code says "here",
        # the distance and the name do not.
        {"plc": "ZZ00002", "era_uopid": "ZZ00002", "station_id": "NAPST0002", "feed": "DE_DELFI",
         "stop_key": "de:99:2", "stop_name": "Sampleton Nord", "code_value": "9900002",
         "code_series": "DELFI_stop_key", "match_method": "code, refused on distance",
         "tier": "T2_name_distance", "asserted": "no", "distance_m": "480", "name_sim": "0.61",
         "station_label": "Rail", "note": "code points here, name does not"},
        # Not asserted and no code: a name-only candidate, not a contradiction.
        {"plc": "ZZ00003", "era_uopid": "ZZOP03A", "station_id": "NAPST0003", "feed": "ZZ_FEED",
         "stop_key": "TY", "stop_name": "Testbury", "match_method": "name only",
         "tier": "T4_name_only", "asserted": "no", "distance_m": "95", "name_sim": "0.7",
         "station_label": "Multimodal"},
        # Names a reference row the master does not have.
        {"plc": "ZZ99999", "era_uopid": "ZZ99999", "station_id": "NAPST0099", "feed": "ZZ_FEED",
         "stop_key": "XX", "stop_name": "Nowhere", "tier": "T5_distance_only", "asserted": "no"},
    ]  # fmt: skip


def unmapped_rows() -> list[dict[str, str]]:
    return [
        {"station_id": "NAPST0101", "iso2": "ZZ", "name": "Exampleville Tram Depot",
         "lat": "50.01", "lon": "4.01", "label": "Urban", "feeds": "ZZ_FEED",
         "reason": "no_reference_within_300m", "nearest_era_plc": "ZZ00001",
         "nearest_era_distance_m": "812.4"},
        {"station_id": "NAPST0102", "iso2": "ZZ", "name": "Sampleton West", "lat": "50.11",
         "lon": "4.11", "label": "Rail", "feeds": "DE_DELFI|ZZ_FEED",
         "reason": "name_mismatch", "nearest_era_plc": "ZZ00002",
         "nearest_era_distance_m": "150"},
        {"station_id": "NAPST0103", "iso2": "YY", "name": "Mockford Interchange",
         "label": "Multimodal", "feeds": "ZZ_FEED", "reason": "no_reference_within_300m"},
        {"station_id": "NAPST0104", "iso2": "ZZ", "name": "Unlabelled Stop", "label": "unknown",
         "feeds": "ZZ_FEED"},
    ]  # fmt: skip


# What each seeded source accepts, and the file name the offline chain would give it.
STATION_FILE_SET = {
    "OFFLINE_MASTER": ("station_master_csv", "station_master_crd_2026-09.csv", master_rows),
    "OFFLINE_LINKS": ("station_links_csv", "station_links_crd_2026-09.csv", link_rows),
    "OFFLINE_UNMAPPED": (
        "station_unmapped_csv",
        "nap_rail_stations_unmapped_crd_2026-09.csv",
        unmapped_rows,
    ),
    "CRD": ("crd_locations_csv", "crd_locations_2026-09.csv", crd_location_rows),
    "ERA_TELREF": ("era_telref_csv", "telref_locations_v3.csv", telref_rows),
}


def station_file(source_key: str, rows: list[dict[str, str]] | None = None) -> tuple[str, bytes]:
    """(file name, content) of one synthetic file; `rows` replaces the default set."""
    from app.master import station_files

    fmt, filename, default_rows = STATION_FILE_SET[source_key]
    content = csv_bytes(station_files.FILE_SHAPES[fmt], *(default_rows() if rows is None else rows))
    return filename, content


def write_station_files(folder: Path) -> dict[str, Path]:
    """Write the five synthetic files; returns their paths by format."""
    out = {}
    for source_key, (fmt, _, _) in STATION_FILE_SET.items():
        filename, content = station_file(source_key)
        path = folder / filename
        path.write_bytes(content)
        out[fmt] = path
    return out
