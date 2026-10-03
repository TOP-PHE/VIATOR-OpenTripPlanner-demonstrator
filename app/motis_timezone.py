"""Give every MOTIS dataset a default time zone, from its provider's country.

nigiri's NeTEx loader picks each XML file's time zone from
`FrameDefaults/DefaultLocale`: CET when `SummerTimeZoneOffset` is 2, else the
`TimeZone` element, else the dataset's `default_timezone` from MOTIS's config
(nigiri load_timetable.cc 1203-1216). MOTIS passes an empty string when the
config has none (motis src/import.cc 366-367), and `locate_zone("")` throws,
so the whole file is dropped with "not found in timezone database". The error
goes to the build folder's `logs/tt.txt`, not to stderr.

Measured on the 2026-10-02 eu19 build: 2 154 of NMBS's 2 184 files and 412 of
ÖBB's 420 were lost this way. `motis config` never writes the key, so the
worker adds `default_timezone` to each dataset whose provider declares a
country. It is only a fallback: a file that declares its own zone keeps it.
The key exists in MOTIS 2.10.2 and 2.11.3 (include/motis/config.h, dataset
`default_timezone_`; the yaml reader drops the trailing underscore).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# One IANA zone per country: the mainland / capital zone. Feeds whose stops
# lie in another zone of the same country (Canary Islands, Azores) declare
# their own zone or keep their published times relative to this one.
COUNTRY_TIMEZONE: dict[str, str] = {
    "AL": "Europe/Tirane",
    "AT": "Europe/Vienna",
    "BA": "Europe/Sarajevo",
    "BE": "Europe/Brussels",
    "BG": "Europe/Sofia",
    "CH": "Europe/Zurich",
    "CY": "Asia/Nicosia",
    "CZ": "Europe/Prague",
    "DE": "Europe/Berlin",
    "DK": "Europe/Copenhagen",
    "EE": "Europe/Tallinn",
    "ES": "Europe/Madrid",
    "FI": "Europe/Helsinki",
    "FR": "Europe/Paris",
    "GB": "Europe/London",
    "GR": "Europe/Athens",
    "HR": "Europe/Zagreb",
    "HU": "Europe/Budapest",
    "IE": "Europe/Dublin",
    "IT": "Europe/Rome",
    "LI": "Europe/Vaduz",
    "LT": "Europe/Vilnius",
    "LU": "Europe/Luxembourg",
    "LV": "Europe/Riga",
    "ME": "Europe/Podgorica",
    "MK": "Europe/Skopje",
    "NL": "Europe/Amsterdam",
    "NO": "Europe/Oslo",
    "PL": "Europe/Warsaw",
    "PT": "Europe/Lisbon",
    "RO": "Europe/Bucharest",
    "RS": "Europe/Belgrade",
    "SE": "Europe/Stockholm",
    "SI": "Europe/Ljubljana",
    "SK": "Europe/Bratislava",
    "XK": "Europe/Belgrade",
}


def timezones_by_file(providers: list[dict[str, Any]], filename_for: Any) -> dict[str, str]:
    """{timetable file name in the inbox: IANA zone} for providers with a known country.

    `filename_for(provider_id, format)` is ingestion.staged_filename_for_format,
    passed in so this module stays free of the ingestion import graph."""
    out: dict[str, str] = {}
    for p in providers:
        tz = COUNTRY_TIMEZONE.get((p.get("country_iso") or "").upper())
        fmt = (p.get("timetable") or {}).get("format", "gtfs")
        if tz:
            out[filename_for(p["id"], fmt)] = tz
    return out


def set_dataset_timezones(config_yml: Path, tz_by_file: dict[str, str]) -> list[str]:
    """Add `default_timezone` after each dataset's `path:` line, matched on the
    file name; datasets that already have one are left alone. Returns one
    note per dataset changed. Same line-based editing as
    worker._strip_tiles_block: the YAML `motis config` writes is plain."""
    lines = config_yml.read_text(encoding="utf-8").splitlines(keepends=True)
    blocks = _dataset_blocks(lines)
    notes: list[str] = []
    insert_after: dict[int, str] = {}
    for key, (path_idx, has_tz) in blocks.items():
        if path_idx is None or has_tz:
            continue
        path_line = lines[path_idx]
        filename = path_line.split("path:", 1)[1].strip().rsplit("/", 1)[-1]
        tz = tz_by_file.get(filename)
        if tz:
            indent = path_line[: len(path_line) - len(path_line.lstrip(" "))]
            insert_after[path_idx] = f"{indent}default_timezone: {tz}\n"
            notes.append(f"{key}: default_timezone {tz}")
    out: list[str] = []
    for i, line in enumerate(lines):
        out.append(line)
        if i in insert_after:
            out.append(insert_after[i])
    config_yml.write_text(
        "".join(out), encoding="utf-8"
    )  # NOSONAR  same path as _strip_tiles_block
    return notes


def _dataset_blocks(lines: list[str]) -> dict[str, tuple[int | None, bool]]:
    """{dataset key: (index of its `path:` line, has default_timezone)} for the
    entries under `timetable:` → `datasets:`."""
    blocks: dict[str, tuple[int | None, bool]] = {}
    in_datasets = False
    datasets_indent = -1
    key: str | None = None
    key_indent = -1
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        indent = len(line) - len(line.lstrip(" "))
        if stripped == "datasets:":
            in_datasets, datasets_indent, key = True, indent, None
            continue
        if not in_datasets:
            continue
        if indent <= datasets_indent:
            in_datasets, key = False, None
            continue
        if stripped.endswith(":") and (key is None or indent <= key_indent):
            key, key_indent = stripped[:-1], indent
            blocks[key] = (None, False)
            continue
        if key is None or indent <= key_indent:
            continue
        path_idx, has_tz = blocks[key]
        if indent == key_indent + 2 and stripped.startswith("path:"):
            path_idx = i
        if indent == key_indent + 2 and stripped.startswith("default_timezone:"):
            has_tz = True
        blocks[key] = (path_idx, has_tz)
    return blocks
