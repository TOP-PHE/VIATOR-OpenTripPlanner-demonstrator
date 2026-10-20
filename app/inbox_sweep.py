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

The sweep runs at the end of `POST /sources/refresh`, against the config
stored at that moment: list `inbox/<sid>/gtfs/` + `inbox/<sid>/netex/`,
rename any `*.zip` that is not the slot (subdir + name) of a current
provider to `*.zip.orphaned`. A config save renames, once its commit has
succeeded, every file that bears a current provider's file name in the
other format's subdir (`plan_save_quarantine` / `apply_save_quarantine`).
Idempotent (already-`.orphaned` files are ignored) and lossless (renames
rather than deletes; an `.orphaned` name already taken gets a time stamp,
`_orphan_target`), so an operator who removed a provider by mistake can
recover by renaming back.

Lives in its own module so unit tests can import the helpers without
pulling in the FastAPI router stack (which transitively imports
`app.db` → psycopg native).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
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

Slot = tuple[str, str]  # (subdir, filename), e.g. ("gtfs", "zz-a.zip")


def expected_provider_filenames(config: dict[str, Any]) -> set[Slot] | None:
    """Return the `(subdir, filename)` slots the current provider list
    expects to find, e.g. `("gtfs", "zz-a.zip")`.

    The slot comes from `ingestion.provider_slot`: the subdir is the one
    `dispatch` places the timetable in (a provider whose format moved from
    NeTEx to GTFS expects `gtfs/<id>.zip`, never `netex/<id>.zip`), and a
    derived provider's file is GTFS whatever format it declares.

    Returns None on a malformed provider list — defensive choice that
    means the sweep won't rename anything (rather than aggressively
    quarantining everything because a single bad provider broke parsing).
    An empty set means no provider: every file is an orphan.
    """
    try:
        providers = ingestion.normalize_providers(config)
    except ValueError:
        return None
    expected: set[Slot] = set()
    for p in providers:
        try:
            expected.add(ingestion.provider_slot(p))
        except ValueError:
            # Unknown format — skip rather than crash. Same defensive
            # posture as the try/except above.
            continue
    return expected


def sweep_orphaned_provider_files(
    session_inbox: Path,
    expected: set[Slot] | None,
    *,
    only: set[Slot] | None = None,
) -> list[str]:
    """Rename `<feed_id>.zip` files in gtfs/ and netex/ subdirs whose
    `(subdir, filename)` slot isn't in `expected` to `<feed_id>.zip.orphaned`.

    `expected` None (the provider list could not be read) renames nothing.
    `only`, when given, limits the sweep to those slots (the save-time
    quarantine); None sweeps every file.

    Returns the list of human-readable rename events for the API
    response. Empty list means no orphans were found.

    Idempotent: files already ending in `.orphaned` (or anything other
    than exactly `.zip`) are ignored. Multiple consecutive runs produce
    the same end state.

    Best-effort on individual rename failures — logs and continues so
    one stuck file (e.g. EACCES, locked by another process) doesn't
    abort the whole sweep. Lossless: never deletes, never overwrites.
    """
    events: list[str] = []
    if expected is None:
        return events
    for subdir_name, entry in _timetable_zips(session_inbox):
        slot = (subdir_name, entry.name)
        if slot in expected or (only is not None and slot not in only):
            continue
        dst = _orphan_target(entry)
        try:
            entry.rename(dst)
            events.append(f"{subdir_name}/{entry.name} → {dst.name}")
        except OSError as exc:
            log.warning("could not rename orphan %s → %s: %s", entry, dst, exc)
    return events


def _orphan_target(entry: Path) -> Path:
    """`<name>.zip.orphaned`, or, when an earlier orphan already holds that
    name, `<name>.zip.<UTC time>.orphaned` (`-2`, `-3`… on a clash), so a
    quarantine never overwrites one. Every form ends in `.orphaned`: no
    build glob reads it, the sweep skips it and Admin → Storage lists it."""
    dst = entry.with_name(entry.name + ORPHAN_SUFFIX)
    if not dst.exists():
        return dst
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    dst = entry.with_name(f"{entry.name}.{stamp}{ORPHAN_SUFFIX}")
    n = 2
    while dst.exists():
        dst = entry.with_name(f"{entry.name}.{stamp}-{n}{ORPHAN_SUFFIX}")
        n += 1
    return dst


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


@dataclass(frozen=True)
class SavePlan:
    """What a config save quarantines once committed (`plan_save_quarantine`).

    `expected`: the saved config's slots. `slots`: the other-format slots of
    their file names (`netex/<id>.zip` for a GTFS provider, and the reverse),
    the only files the save may rename. `switched`: the slots the save moved
    a provider to (its file name was in the stored config, in another
    subdir), checked for a quarantined copy after the renames.
    """

    expected: frozenset[Slot]
    slots: frozenset[Slot]
    switched: frozenset[Slot]


def plan_save_quarantine(old_config: dict[str, Any], new_config: dict[str, Any]) -> SavePlan:
    """The quarantine of a config save, computed before its commit; no file
    is touched. It does not rely on the stored config for the renames: a
    provider removed then added back in the other format, or renamed onto
    another's file name, leaves its stale file in the other subdir too. An
    unreadable saved list (None) plans nothing."""
    new = expected_provider_filenames(new_config) or set()
    old = expected_provider_filenames(old_config) or set()
    new_names = {name for _, name in new}
    old_names = {name for _, name in old}
    slots = {(subdir, name) for name in new_names for subdir in _SWEPT_SUBDIRS} - new
    switched = {slot for slot in new - old if slot[1] in old_names}
    return SavePlan(frozenset(new), frozenset(slots), frozenset(switched))


def apply_save_quarantine(session_inbox: Path, plan: SavePlan) -> list[str]:
    """Rename the planned other-format files (after the save's commit) and
    return the rename events. A switch whose new slot is empty while a
    quarantined copy of it exists (a stale page switching back) logs one
    WARNING: the copy is never restored automatically."""
    events: list[str] = []
    if plan.slots:
        events = sweep_orphaned_provider_files(
            session_inbox, set(plan.expected), only=set(plan.slots)
        )
    if plan.switched:
        _warn_quarantined_switch_target(session_inbox, plan.switched)
    return events


def _warn_quarantined_switch_target(session_inbox: Path, switched: frozenset[Slot]) -> None:
    for subdir_name in _SWEPT_SUBDIRS:
        subdir = session_inbox / subdir_name
        if not subdir.is_dir():
            continue
        for entry in subdir.iterdir():
            live = entry.name.removesuffix(ORPHAN_SUFFIX)
            if (
                live != entry.name
                and (subdir_name, live) in switched
                and not (subdir / live).exists()
            ):
                log.warning(
                    "inbox.switch_target_quarantined file=%s: a save switched a provider "
                    "back to this slot, which holds no live file; rename this copy back "
                    "(drop .orphaned) or refresh the provider",
                    entry,
                )
