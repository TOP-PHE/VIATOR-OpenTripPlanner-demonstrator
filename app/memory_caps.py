"""Memory caps without swap for the containers VIATOR starts (issue #374).

Every container that gets a memory cap also gets a swap limit equal to it:
`memswap_limit` in compose, `--memory-swap` on `docker run`. Docker reads the
swap limit as memory plus swap, so "equal" means no swap at all. A container
that reaches its cap is then OOM-killed at once, which is visible
(`container_oom_events_total`, the ContainerOOMKilled alert, exit 137 in the
build log), instead of pushing its pages to the host swap and slowing the
whole server with iowait (the 2026-10-18 freeze).

Docker's per-container `mem_swappiness` was the other way to keep a resident
router out of swap. It is not used: on cgroup v2 hosts (Ubuntu 24.04) Docker
does not support it and ignores it.
"""

from __future__ import annotations

import re

# Same shape as a JVM -Xmx: integer + g or m. Docker accepts more units, but a
# narrow format keeps `.env` values readable and the tests exact.
_LIMIT_RE = re.compile(r"^(\d+)([gGmM])$")


def validate_mem_limit(value: str | None) -> str | None:
    """Return a normalised cap ('24g', '8192m') or None for "no cap".

    Empty or missing means no cap. A malformed value raises ValueError so the
    caller can log it; a zero value is refused too (Docker reads 0 as "no
    limit", which would silently undo the setting).
    """
    if value is None or not value.strip():
        return None
    m = _LIMIT_RE.match(value.strip())
    if not m:
        raise ValueError(
            f"memory limit {value!r} must be an integer + 'g' or 'm' (e.g. '24g'), or empty"
        )
    qty, unit = int(m.group(1)), m.group(2).lower()
    if qty == 0:
        raise ValueError("memory limit 0 means no limit in Docker; leave it empty instead")
    return f"{qty}{unit}"


def compose_memory_lines(limit: str | None, *, indent: str = "    ") -> str:
    """Compose service lines for a cap with no swap, or "" when there is no cap."""
    if not limit:
        return ""
    return f'{indent}mem_limit: "{limit}"\n{indent}memswap_limit: "{limit}"\n'


def docker_run_memory_args(limit: str | None) -> list[str]:
    """`docker run` flags for a cap with no swap, or [] when there is no cap."""
    if not limit:
        return []
    return ["--memory", limit, "--memory-swap", limit]
