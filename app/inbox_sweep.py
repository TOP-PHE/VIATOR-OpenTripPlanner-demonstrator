"""Orphan inbox-file detection + quarantine (PR #33, issue #381).

When an operator removes a provider from `sessions.config.sources.providers`
(via the UI's "remove" button or a SQL UPDATE), the corresponding `<feed_id>.zip`
file lingers in `/data/inbox/<sid>/gtfs/`. The OTP entrypoint's
`gtfs/*.zip` glob picks the orphan up at build time and bakes its stale
data into the next graph — surfaced 2026-05-11 with BrittanyFerries
(operator removed via UI, file stayed, build failed on BrittanyFerries'
multi-line `stop_desc` CSV bug).

The same happens when a provider's timetable format changes (GTFS to NeTEx
or back): both formats use the name `<feed_id>.zip`, only the subdir
differs, so the file of the old format must be told apart by its subdir
(issue #381, seen in production on 2026-10-20: a MOTIS build read both).

The sweep runs at the end of `POST /sources/refresh`: list
`inbox/<sid>/gtfs/` + `inbox/<sid>/netex/`, rename any `*.zip` that is
not the slot (subdir + name) of a current provider to `*.zip.orphaned`.
A config save that switches a provider's format quarantines that
provider's old-format file at once (`quarantine_format_switch`). Idempotent
(already-`.orphaned` files are ignored) and lossless (renames rather
than deletes, so an operator who removed a provider by mistake can
recover by renaming back).

Lives in its own module so unit tests can import the helpers without
pulling in the FastAPI router stack (which transitively imports
`app.db` → psycopg native).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from . import ingestion

log = logging.getLogger(__name__)

# Suffix appended to orphaned filenames. Chosen so the OTP entrypoint's
# `gtfs/*.zip` glob doesn't pick the file up (it's now `*.zip.orphaned`,
# not `*.zip`). Matches the existing `*.zip.old` / `*.zip.broken`
# rotation conventions used elsewhere in the codebase.
ORPHAN_SUFFIX = ".orphaned"

# The timetable subdirs the sweep looks into (every build reads `*.zip` there).
_SWEPT_SUBDIRS = ("gtfs", "netex")


def expected_provider_filenames(config: dict[str, Any]) -> set[tuple[str, str]] | None:
    """Return the `(subdir, filename)` slots the current provider list
    expects to find, e.g. `("gtfs", "zz-a.zip")`.

    The subdir comes from `ingestion.staged_slot_for_format`, the mapping
    `dispatch` places a timetable with: a provider whose format moved from
    NeTEx to GTFS expects `gtfs/<id>.zip`, never `netex/<id>.zip`.

    Returns None on a malformed provider list — defensive choice that
    means the sweep won't rename anything (rather than aggressively
    quarantining everything because a single bad provider broke parsing).
    An empty set means no provider: every file is an orphan.
    """
    try:
        providers = ingestion.normalize_providers(config)
    except ValueError:
        return None
    expected: set[tuple[str, str]] = set()
    for p in providers:
        pid = p["id"]
        tt = p.get("timetable") or {}
        fmt = tt.get("format", "gtfs")
        try:
            expected.add(ingestion.staged_slot_for_format(pid, fmt))
        except ValueError:
            # Unknown format — skip rather than crash. Same defensive
            # posture as the try/except above.
            continue
    return expected


def sweep_orphaned_provider_files(
    session_inbox: Path,
    expected: set[tuple[str, str]] | None,
    *,
    only: set[tuple[str, str]] | None = None,
) -> list[str]:
    """Rename `<feed_id>.zip` files in gtfs/ and netex/ subdirs whose
    `(subdir, filename)` slot isn't in `expected` to `<feed_id>.zip.orphaned`.

    `expected` None (the provider list could not be read) renames nothing.
    `only`, when given, limits the sweep to those slots (the save-time
    quarantine of a format switch); None sweeps every file.

    Returns the list of human-readable rename events for the API
    response. Empty list means no orphans were found.

    Idempotent: files already ending in `.orphaned` (or anything other
    than exactly `.zip`) are ignored. Multiple consecutive runs produce
    the same end state.

    Best-effort on individual rename failures — logs and continues so
    one stuck file (e.g. EACCES, locked by another process) doesn't
    abort the whole sweep. Lossless: never deletes.
    """
    events: list[str] = []
    if expected is None:
        return events
    for subdir_name, entry in _timetable_zips(session_inbox):
        slot = (subdir_name, entry.name)
        if slot in expected or (only is not None and slot not in only):
            continue
        dst = entry.with_suffix(".zip" + ORPHAN_SUFFIX)
        try:
            entry.rename(dst)
            events.append(f"{subdir_name}/{entry.name} → {dst.name}")
        except OSError as exc:
            log.warning("could not rename orphan %s → %s: %s", entry, dst, exc)
    return events


def _timetable_zips(session_inbox: Path) -> Iterator[tuple[str, Path]]:
    """`(subdir name, file)` of every exact `<name>.zip` in gtfs/ and netex/.

    Both transit format subdirs use the same `<feed_id>.zip` convention;
    the subdir is part of the slot. The entrypoint scans them separately
    — leftover files in either would get picked up. Only exact `.zip`
    filenames count: `.zip.old`, `.zip.broken`, `.zip.orphaned`,
    README.md, etc. are skipped.
    """
    for subdir_name in _SWEPT_SUBDIRS:
        subdir = session_inbox / subdir_name
        if not subdir.is_dir():
            continue
        for entry in subdir.iterdir():
            if entry.is_file() and entry.suffix == ".zip":
                yield subdir_name, entry


def quarantine_format_switch(
    session_inbox: Path,
    old_config: dict[str, Any],
    new_config: dict[str, Any],
) -> list[str]:
    """At a config save: rename the old-format file of every provider whose
    timetable format moved to another subdir (`netex/<id>.zip` once the
    provider is GTFS, and the reverse), with the refresh's sweep.

    Only those slots are touched: a removed provider's file still waits for
    the next refresh, as before. Nothing is renamed when either config's
    provider list cannot be read. Returns the rename events.
    """
    # An unreadable list (None) has no slot, so it switches nothing.
    old = expected_provider_filenames(old_config) or set()
    new = expected_provider_filenames(new_config) or set()
    new_names = {name for _, name in new}
    switched = {slot for slot in old - new if slot[1] in new_names}
    if not switched:
        return []
    return sweep_orphaned_provider_files(session_inbox, new, only=switched)
