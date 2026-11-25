"""Keep a rebuild from filling the data disk.

On a full disk postgres can no longer write and web stops answering, so the
whole site goes down, not only the build. A build is what can fill the disk: a
Europe-wide MOTIS import writes far more than the graph it leaves behind, and
one that hangs keeps what it wrote. The worker therefore asks this module
twice: before a build starts (`start_refusal`), and every few seconds while it
runs (`stop_reason`), which also stops a rebuild that has run for longer than
any rebuild should.

The free space is read where the graphs live (`settings.graph_dir`). The
inbox, the database and the images share that disk on the server; where they
do not, the graphs are still the part a build fills.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .storage import path_size

GB = 1024**3

# The json-file log driver keeps all of a container's output for as long as the
# container lives. Every container VIATOR runs is capped to these: the services
# of docker/docker-compose.yml, the per-session routers, the build containers.
CONTAINER_LOG_MAX_SIZE = "50m"
CONTAINER_LOG_MAX_FILES = 3
DOCKER_RUN_LOG_ARGS: tuple[str, ...] = (
    "--log-opt",
    f"max-size={CONTAINER_LOG_MAX_SIZE}",
    "--log-opt",
    f"max-file={CONTAINER_LOG_MAX_FILES}",
)


def free_bytes(path: Path) -> int | None:
    """Free bytes on the disk holding `path`; None when it cannot be read."""
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def current_build_bytes(session_root: Path) -> int:
    """Size of the build `session_root/current` points to; 0 without one."""
    link = session_root / "current"
    try:
        if not link.is_symlink():
            return 0
        target = link.resolve(strict=True)
    except (OSError, RuntimeError):
        return 0
    return path_size(target)


def _gb(n: int) -> str:
    return f"{n / GB:.1f} GB"


def start_refusal(graph_dir: Path, session_root: Path, min_free_gb: int) -> str | None:
    """Why a build of the session in `session_root` must not start; None when it may.

    A new build takes at least the room of the one it replaces, and the disk
    must keep `min_free_gb` on top of that. A disk that cannot be read lets the
    build start: the watch during the build is the guard that matters.
    """
    free = free_bytes(graph_dir)
    if free is None:
        return None
    last = current_build_bytes(session_root)
    needed = min_free_gb * GB + last
    if free >= needed:
        return None
    return (
        f"not enough free disk: {_gb(free)} free, {_gb(needed)} needed (the session's "
        f"current build, {_gb(last)}, plus BUILD_MIN_FREE_GB={min_free_gb}). "
        "Free space first: Admin → Storage."
    )


def stop_reason(
    graph_dir: Path, min_free_gb: int, elapsed_seconds: float, max_hours: int
) -> str | None:
    """Why a running rebuild must be stopped now; None while it may go on.

    `max_hours` 0 sets no time limit. A disk that cannot be read stops nothing.
    """
    if max_hours > 0 and elapsed_seconds > max_hours * 3600:
        return f"the rebuild ran for more than BUILD_MAX_HOURS={max_hours} h"
    free = free_bytes(graph_dir)
    if free is not None and free < min_free_gb * GB:
        return (
            f"the free disk space fell to {_gb(free)}, under BUILD_MIN_FREE_GB={min_free_gb}: "
            "stopped before the build filled the disk"
        )
    return None
