"""A default time zone for each NeTEx feed of a MOTIS build, and MOTIS's
import errors brought into the rebuild log.

MOTIS (nigiri's NeTEx loader, `get_intermediate`) takes a file's time zone
from `FrameDefaults/DefaultLocale/TimeZone`, else from the dataset's
`default_timezone` in config.yml. With neither it drops the whole file:
`ERROR:  not found in timezone database` in MOTIS 2.11.2 (the zone name is
empty), "missing timezone" in later nigiri. ÖBB's NAP file (MENTZ, EPIP 1.10,
432 files) declares no zone in any file: on 2026-10-28 eu19 held no ÖBB stop,
and its only ÖBB trains were those DB's German file carries.

So the build writes `default_timezone` for every NeTEx feed whose provider
declares a country (`country_iso`): that country's zone (`COUNTRY_TIMEZONES`).
A file that declares its own zone keeps it, since nigiri prefers the file's.
GTFS feeds are left alone: `agency_timezone` is mandatory there.

MOTIS writes those errors to `logs/tt.txt` in the build folder, never to its
console, so the rebuild log never showed them; `import_error_lines` copies a
summary of them to the end of the log.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any

from . import ingestion

# One zone per country: the mainland's where a country has several (ES, PT).
# Kosovo has no zone of its own in the IANA database and uses Belgrade's.
COUNTRY_TIMEZONES: dict[str, str] = {
    "AD": "Europe/Andorra",
    "AL": "Europe/Tirane",
    "AT": "Europe/Vienna",
    "BA": "Europe/Sarajevo",
    "BE": "Europe/Brussels",
    "BG": "Europe/Sofia",
    "BY": "Europe/Minsk",
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
    "IS": "Atlantic/Reykjavik",
    "IT": "Europe/Rome",
    "LI": "Europe/Vaduz",
    "LT": "Europe/Vilnius",
    "LU": "Europe/Luxembourg",
    "LV": "Europe/Riga",
    "MC": "Europe/Monaco",
    "MD": "Europe/Chisinau",
    "ME": "Europe/Podgorica",
    "MK": "Europe/Skopje",
    "MT": "Europe/Malta",
    "NL": "Europe/Amsterdam",
    "NO": "Europe/Oslo",
    "PL": "Europe/Warsaw",
    "PT": "Europe/Lisbon",
    "RO": "Europe/Bucharest",
    "RS": "Europe/Belgrade",
    "RU": "Europe/Moscow",
    "SE": "Europe/Stockholm",
    "SI": "Europe/Ljubljana",
    "SK": "Europe/Bratislava",
    "SM": "Europe/San_Marino",
    "TR": "Europe/Istanbul",
    "UA": "Europe/Kyiv",
    "VA": "Europe/Vatican",
    "XK": "Europe/Belgrade",
}

_NETEX_DIRS = frozenset({"netex", "_netex"})
# A dataset's `path:` line as `motis config` writes it (6 spaces, unquoted).
_PATH_LINE_RE = re.compile(r"""^(?P<indent> +)path: *["']?(?P<path>[^"'\s]+)["']? *$""")
_TZ_KEY = "default_timezone:"

# MOTIS's message for a file without a time zone (2.11.2, then later nigiri).
_NO_ZONE_MESSAGES = ("not found in timezone database", "missing timezone")
_MAX_MESSAGES = 10
_MAX_MESSAGE_CHARS = 160
_MAX_LOG_BYTES = 64 * 1024 * 1024
_UNPRINTABLE_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def timezone_for_country(country_iso: str | None) -> str | None:
    """The zone of a 2-letter country code, or None for an unknown one."""
    if not country_iso:
        return None
    return COUNTRY_TIMEZONES.get(country_iso.strip().upper())


def dataset_timezones(providers: list[dict[str, Any]]) -> dict[str, str]:
    """`{inbox file name: zone}` for every NeTEx provider with a known
    country. The file name is the provider's slot (`obb.zip`), which is also
    the name of a converted copy (`_netex/<name>`)."""
    zones: dict[str, str] = {}
    for provider in providers:
        try:
            subdir, filename = ingestion.provider_slot(provider)
        except (KeyError, ValueError):
            continue
        zone = timezone_for_country(provider.get("country_iso"))
        if subdir == "netex" and zone:
            zones[filename] = zone
    return zones


def _netex_file(path: str) -> str | None:
    """The file name of a NeTEx dataset path (`/inbox/<sid>/netex/x.zip`,
    `/graphs/motis/<sid>/_netex/x.zip`), else None."""
    parts = path.rstrip("/").split("/")
    if len(parts) >= 2 and parts[-2] in _NETEX_DIRS:
        return parts[-1]
    return None


def _block_line(line: str, block_indent: int | None) -> tuple[bool, int | None]:
    """Whether `line` is kept, and the indent of the dataset block it leaves
    open. `block_indent` is the keys' indent of the dataset whose zone was
    just written, None outside one: there an older `default_timezone:` line is
    dropped, and a line less indented ends the block."""
    if block_indent is None:
        return True, None
    body = line.lstrip(" ")
    indent = len(line) - len(body)
    if body.strip() and indent < block_indent:
        return True, None
    return not (indent == block_indent and body.startswith(_TZ_KEY)), block_indent


def _zone_note(name: str, zone: str | None) -> str:
    if zone is None:
        return (
            f"motis time zone: {name} gets no default (its provider declares no "
            "country): a file of it without a time zone of its own is dropped"
        )
    return (
        f"motis time zone: {name} → {zone}, for its files that declare none "
        "(from the provider's country)"
    )


def set_default_timezones(config_yml: Path, zones: dict[str, str]) -> list[str]:
    """Write `default_timezone` into the config.yml block of each NeTEx
    dataset, and return one log note per NeTEx dataset.

    The zone goes right after the dataset's `path:` line, at its indent; a
    `default_timezone:` line already in that block is replaced. Like
    `worker._strip_tiles_block`, a line state machine: MOTIS's YAML is plain
    (no anchors, no flow style)."""
    out: list[str] = []
    notes: list[str] = []
    block_indent: int | None = None
    for line in config_yml.read_text(encoding="utf-8").splitlines(keepends=True):
        keep, block_indent = _block_line(line, block_indent)
        if not keep:
            continue
        out.append(line)
        m = _PATH_LINE_RE.match(line.rstrip("\r\n"))
        name = _netex_file(m.group("path")) if m else None
        if m is None or name is None:
            continue
        zone = zones.get(name)
        notes.append(_zone_note(name, zone))
        if zone is not None:
            out.append(f"{m.group('indent')}{_TZ_KEY} {zone}\n")
            block_indent = len(m.group("indent"))
    # `config_yml` is the build's own staging file (see `_strip_tiles_block`):
    # a validated session id and a timestamp. pythonsecurity:S2083 suppressed.
    config_yml.write_text("".join(out), encoding="utf-8")  # NOSONAR
    return notes


def _clean(message: str) -> str:
    text = " ".join(_UNPRINTABLE_RE.sub("?", message).split())
    if len(text) > _MAX_MESSAGE_CHARS:
        return text[: _MAX_MESSAGE_CHARS - 1] + "…"
    return text


def import_error_lines(log_file: Path) -> list[str]:
    """A summary of the `ERROR:` messages of MOTIS's timetable import log
    (`<build>/logs/tt.txt`): the count, then the most frequent messages.
    Empty when the file is missing or holds none.

    Messages from parallel loaders can share a line ("…databaseERROR: …"), so
    each `ERROR:` starts a message. A trailing ` IN <path>` (later nigiri) is
    kept: it names the file. Only the first 64 MB are read."""
    try:
        with log_file.open("rb") as fh:
            raw = fh.read(_MAX_LOG_BYTES)
    except OSError:
        return []
    counts: Counter[str] = Counter()
    for line in raw.decode("utf-8", "replace").splitlines():
        for piece in line.split("ERROR:")[1:]:
            message = _clean(piece)
            if message:
                counts[message] += 1
    total = sum(counts.values())
    if not total:
        return []
    lines = [f"motis import errors (logs/tt.txt): {total}"]
    lines += [f"  {n:>7}  {message}" for message, n in counts.most_common(_MAX_MESSAGES)]
    if len(counts) > _MAX_MESSAGES:
        lines.append(f"  … and {len(counts) - _MAX_MESSAGES} other messages")
    if any(text in message for message in counts for text in _NO_ZONE_MESSAGES):
        lines.append(
            "  a NeTEx file declares no time zone and its feed has no default: "
            "give its provider a country so the build sets one"
        )
    return lines
