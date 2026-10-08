"""Disk usage of the data volumes, and what can be deleted to free space.

Why this exists: on 2026-10-01 about fifteen failed Europe-wide MOTIS imports
left their partial output behind, filled the 678 GB disk, and took `web` and
`postgres` down. Nothing showed the disk filling up, and cleaning it meant
`sudo du` on the host. This module backs the admin Storage page: one scan of
the two data volumes (`settings.inbox_dir`, `settings.graph_dir`) that reports
usage per session and a list of clean-up *candidates*, each decided by a fixed
rule and never something in use.

What is never a candidate:
  * the build a session's `current` symlink points to (what is served);
  * anything of a session whose rebuild is running;
  * a live feed or `osm.pbf` (only `.old`, `.old.N` and `.orphaned` copies);
  * a `_staging/` file younger than a day (a refresh may be writing it);
  * the top-level inbox folder `_staging`, which is not a session folder
    (`INBOX_ROOT_RESERVED`).

Deletion re-runs the scan and deletes only ids present in the fresh result,
so the endpoint can never be pointed at an arbitrary path, and a candidate that
stopped qualifying since the page was loaded (a rebuild started, `current`
moved) is skipped rather than deleted.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .logging_config import one_line

log = logging.getLogger(__name__)

# Builds are written to `<YYYYMMDD-HHMMSS>` folders (worker.run_build / run_build_motis).
_BUILD_DIR_RE = re.compile(r"^\d{8}-\d{6}$")
# Rotated feed copies: `sncf.zip.old`, `sncf.zip.old.2`, `osm.pbf.old.1`; removed providers: `.orphaned`.
_STALE_FILE_RE = re.compile(r"\.(?:old(?:\.\d+)?|orphaned)$")
_STAGING_MIN_AGE_S = 24 * 3600
_OTP_GRAPH = "graph.obj"
_OTP_ROUTER_CONFIG = "router-config.json"
# Top-level graph-volume names that are not session folders.
_GRAPH_ROOT_RESERVED = {"motis", _OTP_GRAPH, _OTP_ROUTER_CONFIG, "current", "lost+found"}
# Top-level inbox names that are not session folders either, and so must never
# be offered as "the folder of a session that no longer exists":
#   `_staging`   where the legacy `/upload` route streams a file before it is
#                dispatched. Deleting it mid-upload loses the upload.
INBOX_ROOT_RESERVED = frozenset({"_staging"})

WARN_PERCENT = 85.0


@dataclass
class Candidate:
    id: str  # "graphs/<rel>" or "inbox/<rel>" — the only thing the delete call accepts
    category: str
    session_id: str | None
    size_bytes: int
    modified: float  # epoch seconds
    reason: str


@dataclass
class SessionUsage:
    session_id: str
    known: bool  # False: a folder with no matching session row
    graphs_bytes: int = 0
    inbox_bytes: int = 0
    current_build: str | None = None


@dataclass
class Report:
    total_bytes: int
    used_bytes: int
    free_bytes: int
    used_percent: float
    warn: bool
    sessions: list[SessionUsage] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ──────────────────────────── sizes ────────────────────────────


def path_size(path: Path) -> int:
    """Bytes on disk under `path`, not following symlinks (a `current`
    symlink must not count its build twice)."""
    try:
        if path.is_symlink():
            return 0
        if path.is_file():
            return path.stat().st_size
    except OSError:
        return 0
    total = 0
    stack = [path]
    while stack:
        total += _dir_files_size(stack.pop(), stack)
    return total


def _dir_files_size(here: Path, subdirs: list[Path]) -> int:
    """Size of the files directly in `here`; its subfolders go onto `subdirs`."""
    try:
        with os.scandir(here) as it:
            entries = list(it)
    except OSError:
        return 0
    return sum(_entry_size(entry, subdirs) for entry in entries)


def _entry_size(entry: os.DirEntry[str], subdirs: list[Path]) -> int:
    try:
        if entry.is_symlink():
            return 0
        if entry.is_dir(follow_symlinks=False):
            subdirs.append(Path(entry.path))
            return 0
        return entry.stat(follow_symlinks=False).st_size
    except OSError:
        return 0


def _mtime(path: Path) -> float:
    try:
        return path.lstat().st_mtime
    except OSError:
        return 0.0


def _current_target(session_dir: Path) -> str | None:
    link = session_dir / "current"
    if not link.is_symlink():
        return None
    try:
        return link.readlink().name
    except OSError:
        return None


def _is_complete_build(build_dir: Path) -> bool:
    """A MOTIS import writes `tt.bin`, an OTP build `graph.obj`."""
    return (build_dir / "tt.bin").exists() or (build_dir / _OTP_GRAPH).exists()


# ──────────────────────────── scan ────────────────────────────


def _build_candidates(
    root: Path, id_prefix: str, sid: str, busy: bool, usage: SessionUsage
) -> list[Candidate]:
    out: list[Candidate] = []
    current = _current_target(root)
    usage.current_build = current
    usage.graphs_bytes += path_size(root)
    if busy or not root.is_dir():
        return out
    for child in sorted(root.iterdir()):
        if child.name == "current" or child.is_symlink() or not child.is_dir():
            continue
        if child.name == current or not _BUILD_DIR_RE.match(child.name):
            continue
        complete = _is_complete_build(child)
        out.append(
            Candidate(
                id=f"{id_prefix}/{child.name}",
                category="previous_build" if complete else "failed_build",
                session_id=sid,
                size_bytes=path_size(child),
                modified=_mtime(child),
                reason=(
                    "Earlier complete build, not the one being served (kept for rollback)"
                    if complete
                    else "Incomplete build: an import or build that failed or was interrupted"
                ),
            )
        )
    return out


def _classify_inbox_file(name: str, in_staging: bool, age_s: float) -> tuple[str, str] | None:
    """(category, reason) for an inbox file that can go, or None for one in use."""
    if in_staging:
        if age_s < _STAGING_MIN_AGE_S:
            return None
        return "staging_leftover", "Left in _staging by an interrupted refresh"
    if not _STALE_FILE_RE.search(name):
        return None
    if name.endswith(".orphaned"):
        return "orphaned_feed", "Feed file of a provider removed from the session"
    return "old_copy", "Previous version kept after a refresh (rollback copy)"


def _inbox_candidates(root: Path, sid: str, busy: bool, now: float) -> list[Candidate]:
    out: list[Candidate] = []
    if busy or not root.is_dir():
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        dirnames[:] = [d for d in dirnames if not (here / d).is_symlink()]
        in_staging = "_staging" in here.relative_to(root).parts
        for path in (here / name for name in filenames):
            kind = (
                None
                if path.is_symlink()
                else _classify_inbox_file(path.name, in_staging, now - _mtime(path))
            )
            if kind is None:
                continue
            out.append(
                Candidate(
                    id=f"inbox/{path.relative_to(root.parent).as_posix()}",
                    category=kind[0],
                    session_id=sid,
                    size_bytes=path_size(path),
                    modified=_mtime(path),
                    reason=kind[1],
                )
            )
    return out


def _disk_report(graph_dir: Path) -> Report:
    try:
        du = shutil.disk_usage(graph_dir)
    except OSError:
        return Report(0, 0, 0, 0.0, False)
    pct = round(100.0 * du.used / du.total, 1) if du.total else 0.0
    return Report(du.total, du.used, du.free, pct, pct >= WARN_PERCENT)


def _subdirs(base: Path) -> list[Path]:
    if not base.is_dir():
        return []
    return [c for c in sorted(base.iterdir()) if c.is_dir() and not c.is_symlink()]


def _graph_roots(graph_dir: Path) -> list[tuple[Path, str]]:
    """(session folder, candidate id prefix): OTP at graphs/<sid>/, MOTIS at graphs/motis/<sid>/."""
    otp = [
        (c, f"graphs/{c.name}") for c in _subdirs(graph_dir) if c.name not in _GRAPH_ROOT_RESERVED
    ]
    motis = [(c, f"graphs/motis/{c.name}") for c in _subdirs(graph_dir / "motis")]
    return otp + motis


def _root_leftovers(graph_dir: Path) -> list[Candidate]:
    """OTP build output written at the volume root and never moved (failed build)."""
    out = []
    for name in (_OTP_GRAPH, _OTP_ROUTER_CONFIG):
        leftover = graph_dir / name
        if leftover.is_file() and not leftover.is_symlink():
            out.append(
                Candidate(
                    id=f"graphs/{name}",
                    category="failed_build",
                    session_id=None,
                    size_bytes=path_size(leftover),
                    modified=_mtime(leftover),
                    reason="OTP build output never moved into a session (failed build)",
                )
            )
    return out


def scan(
    inbox_dir: Path,
    graph_dir: Path,
    session_ids: set[str],
    busy_session_ids: set[str],
    *,
    now: float | None = None,
) -> Report:
    """`session_ids`: every session that exists. `busy_session_ids`: those
    with a rebuild running, whose files are left alone."""
    now = time.time() if now is None else now
    report = _disk_report(graph_dir)
    usages: dict[str, SessionUsage] = {}

    def usage_for(sid: str) -> SessionUsage:
        # A reserved folder is reported (its size matters) but is not "a
        # deleted session": `known` is what the page badges as deleted.
        known = sid in session_ids or sid in INBOX_ROOT_RESERVED
        return usages.setdefault(sid, SessionUsage(sid, known))

    for root, prefix in _graph_roots(graph_dir):
        sid = root.name
        if sid in session_ids:
            busy = sid in busy_session_ids
            report.candidates += _build_candidates(root, prefix, sid, busy, usage_for(sid))
        else:
            usage_for(sid).graphs_bytes += path_size(root)
            report.candidates.append(_orphan(root, prefix, sid))

    if not busy_session_ids:
        report.candidates += _root_leftovers(graph_dir)

    for root in _subdirs(inbox_dir):
        sid = root.name
        usage_for(sid).inbox_bytes += path_size(root)
        if sid in INBOX_ROOT_RESERVED:
            continue
        if sid in session_ids:
            report.candidates += _inbox_candidates(root, sid, sid in busy_session_ids, now)
        else:
            report.candidates.append(_orphan(root, f"inbox/{sid}", sid))

    report.sessions = sorted(
        usages.values(), key=lambda u: u.graphs_bytes + u.inbox_bytes, reverse=True
    )
    report.candidates.sort(key=lambda c: c.size_bytes, reverse=True)
    return report


def _orphan(path: Path, cid: str, sid: str) -> Candidate:
    return Candidate(
        id=cid,
        category="orphan_session",
        session_id=sid,
        size_bytes=path_size(path),
        modified=_mtime(path),
        reason="Folder of a session that no longer exists",
    )


# ──────────────────────────── delete ────────────────────────────


def resolve_candidate(candidate_id: str, inbox_dir: Path, graph_dir: Path) -> Path:
    """Map a candidate id back to its path, refusing anything outside the volume."""
    volume, _, rel = candidate_id.partition("/")
    base = {"inbox": inbox_dir, "graphs": graph_dir}.get(volume)
    if base is None or not rel:
        raise ValueError(f"not a storage candidate id: {candidate_id!r}")
    root = os.path.realpath(base)
    target = os.path.realpath(Path(root) / rel)
    if not target.startswith(root + os.sep):
        raise ValueError(f"candidate escapes its volume: {candidate_id!r}")
    return Path(target)


def delete(
    candidate_ids: list[str],
    report: Report,
    inbox_dir: Path,
    graph_dir: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Delete the ids that are candidates in `report` (a fresh scan).
    Returns (deleted, skipped). Ids not in the report are skipped, never deleted."""
    by_id = {c.id: c for c in report.candidates}
    deleted: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for cid in dict.fromkeys(candidate_ids):
        cand = by_id.get(cid)
        if cand is None:
            skipped.append({"id": cid, "reason": "no longer a clean-up candidate"})
            continue
        try:
            path = resolve_candidate(cid, inbox_dir, graph_dir)
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
        except (OSError, ValueError) as exc:
            # The error text names paths and OS details: keep it in the server log,
            # never in the response (CodeQL py/stack-trace-exposure).
            log.warning("storage clean-up could not delete %s: %s", one_line(cid), one_line(exc))
            skipped.append({"id": cid, "reason": "could not delete (see the web server log)"})
            continue
        deleted.append({"id": cid, "category": cand.category, "size_bytes": cand.size_bytes})
    return deleted, skipped
