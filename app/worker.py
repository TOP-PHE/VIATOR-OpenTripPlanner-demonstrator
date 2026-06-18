"""Background worker: polls per-session rebuild_jobs, debounces, runs the
session's engine-specific build (OTP or MOTIS).

Per session, at most one rebuild runs at a time. The MAX_CONCURRENT_REBUILDS
config knob caps total simultaneous rebuilds across all sessions.

The worker also watches for the reload-trigger file written by
`POST /api/sessions/<sid>/promote` — when present, runs `docker compose up`
and `nginx -s reload` so per-session planner containers (otp-<sid> /
motis-<sid>) come online without the operator having to shell in.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import asc

from . import graph_snapshots
from .db import SessionLocal
from .models import RebuildJob
from .models import Session as SessionRow
from .models.sessions import SessionState
from .settings import settings

log = logging.getLogger("worker")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


# Same path written by app/api/admin/sessions.py::promote_session.
_RELOAD_TRIGGER = Path("/data/generated/.reload-trigger")

# Absolute path so Bandit (S607) doesn't flag our subprocess.run calls.
# We copy the docker CLI binary here in docker/web/Dockerfile stage 2 — this
# is the *only* docker we'll ever invoke from inside the worker container.
# Hard-coding it (rather than `shutil.which`) also makes the security review
# trivial: every subprocess invocation passes through this constant.
_DOCKER = "/usr/local/bin/docker"


def _host_total_gb() -> int | None:
    """Best-effort host RAM in whole GB, read from /proc/meminfo.

    `MemTotal` reflects the *host's* physical memory even from inside a
    container (it's not cgroup-scoped), so it's a sound basis for warning
    when a requested build heap simply won't fit the box. Returns None if
    the file is unreadable (non-Linux dev box, locked-down sandbox).
    """
    try:
        with Path("/proc/meminfo").open(encoding="ascii") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // (1024 * 1024)
    except (OSError, ValueError, IndexError):
        return None
    return None


# ───────────────────── Max-memory rebuild (v0.1.38) ─────────────────────────
# Observability containers stopped during a max-memory rebuild and restarted
# afterward. Service names come from docker/docker-compose.yml. The core stack
# (postgres / web / worker / nginx) is never touched — the build itself needs
# the worker + DB, and the operator needs the UI. Serving OTP session
# containers (otp-<sid>) are added dynamically from the DB.
_OBSERVABILITY_SERVICES: tuple[str, ...] = (
    "grafana",
    "loki",
    "promtail",
    "prometheus",
    "cadvisor",
    "node-exporter",
    "tempo",
)

# Records the services stopped for an in-flight max-memory rebuild so a worker
# that dies mid-build can restart them on next boot (the normal restart runs in
# run_build's `finally`; this is the crash-safety net).
_MAXMEM_MARKER = Path("/data/generated/.max-mem-stopped")


def _max_memory_stop_targets(serving_sids: list[str]) -> list[str]:
    """Compose service names to stop for a max-memory rebuild: every serving
    OTP session plus the observability stack. Pure — unit-tested."""
    return [f"otp-{sid}" for sid in serving_sids] + list(_OBSERVABILITY_SERVICES)


def _compose(*args: str) -> subprocess.CompletedProcess[str]:
    """Run `docker compose -p viator <args>` from the compose dir."""
    return subprocess.run(  # noqa: S603
        [_DOCKER, "compose", "-p", "viator", *args],
        cwd="/srv/docker",
        capture_output=True,
        text=True,
        check=False,
    )


def _log_safe(values: list[str]) -> str:
    """Join service names with CR/LF stripped before logging.

    These names trace back to the DB (session ids) and the recovery marker
    file, so Sonar treats them as user-controlled; stripping line breaks
    defeats log-forging via a crafted value (python S5145)."""
    cleaned = [v.replace("\r\n", "").replace("\n", "").replace("\r", "") for v in values]
    return " ".join(cleaned)


def _stop_services(services: list[str]) -> None:
    if not services:
        return
    log.info("max-memory rebuild: stopping %d containers: %s", len(services), _log_safe(services))
    r = _compose("stop", *services)
    if r.returncode != 0:
        log.warning("max-memory stop exit=%s: %s", r.returncode, r.stderr.strip())


def _start_services(services: list[str]) -> None:
    if not services:
        return
    log.info("max-memory rebuild: restarting %d containers: %s", len(services), _log_safe(services))
    r = _compose("start", *services)
    if r.returncode != 0:
        # `start` only revives existing stopped containers; if any were pruned,
        # recreate them from config. `up -d` is the robust fallback.
        log.warning(
            "max-memory restart exit=%s (%s) — falling back to `up -d`",
            r.returncode,
            r.stderr.strip(),
        )
        _compose("up", "-d", "--no-deps", *services)


def _recover_max_memory_stopped() -> None:
    """Restart anything an interrupted max-memory rebuild left stopped.

    The happy path restarts in run_build's `finally`; this covers the worker
    being killed mid-build (after stopping containers, before the finally ran).
    Best-effort: a failure here must never keep the worker from booting."""
    try:
        if not _MAXMEM_MARKER.exists():
            return
        services = [s for s in _MAXMEM_MARKER.read_text(encoding="utf-8").split() if s]
        if services:
            log.warning(
                "recovering %d containers left stopped by an interrupted max-memory rebuild",
                len(services),
            )
            _start_services(services)
        _MAXMEM_MARKER.unlink(missing_ok=True)
    except Exception:
        log.exception("max-memory recovery failed at startup (non-fatal)")


def _mark_orphaned_rebuild_jobs() -> int:
    """v0.1.32 — terminate rebuild_jobs left mid-flight by a worker restart.

    Same shape as the v0.1.29.3 fix for network_coverage_runs. The worker
    runs `docker compose run otp-build` synchronously per job and updates
    rebuild_jobs.status as it progresses (pending → running → done|failed).
    If the worker container is killed mid-build (deploy, OOM, manual
    restart), the otp-build container also dies but the rebuild_jobs row
    stays in `running` forever — operators see ghost "still building"
    entries that never resolve, and the same session may try to queue
    another rebuild but get blocked by the one-rebuild-per-session lock.

    By the time the worker reaches its main loop, no `docker compose run`
    invocation from a prior worker process can still be alive (subprocess
    handles don't survive container restart), so anything in `running`
    status at startup is by definition orphaned. Mark them `failed` with
    a log line annotating the cleanup so post-hoc analysis is clear about
    what happened.

    Returns the number of rows marked. Caller is responsible for the
    surrounding error handling — we don't want a startup hiccup here to
    crash the worker.
    """
    from sqlalchemy import select

    with SessionLocal() as db:
        orphans = list(
            db.execute(select(RebuildJob).where(RebuildJob.status == "running")).scalars().all()
        )
        if not orphans:
            return 0
        now = datetime.now(UTC)
        for job in orphans:
            existing_log = job.log or ""
            note = (
                f"\n--- [v0.1.32] worker startup: marked failed (orphaned by "
                f"worker restart at {now.isoformat()}) ---\n"
            )
            job.log = existing_log + note
            job.status = "failed"
            job.finished_at = now
        db.commit()
        return len(orphans)


def main() -> None:
    log.info("worker starting; debounce + tick are live-read from platform_config")

    # v0.1.32 — clean up any rebuild_jobs left in `running` status by a
    # previous worker process that died mid-build. Wrapped in try/except
    # so a malformed row can't keep the worker from booting.
    try:
        n = _mark_orphaned_rebuild_jobs()
        if n:
            log.warning("marked %d orphaned rebuild_jobs as failed at startup", n)
    except Exception:
        log.exception("orphan rebuild_jobs cleanup failed at startup (non-fatal)")

    # v0.1.38 — restart any containers a max-memory rebuild left stopped if the
    # previous worker died mid-build before its `finally` could run.
    _recover_max_memory_stopped()

    while True:
        try:
            tick()
            handle_reload_trigger()
        except Exception:
            log.exception("worker tick failed")
        # Tick interval is admin-editable in Admin -> Configuration -> Worker
        # timing (v0.1.11). config_service caches for 30 s so a save in the
        # UI takes up to ~30 s + the previous tick's interval to take effect.
        time.sleep(_tick_seconds())


def _tick_seconds() -> int:
    """Read WORKER_TICK_SECONDS from platform_config, fall back to 15 if the DB
    is unreachable (so the worker keeps ticking even during a Postgres blip)."""
    from . import config_service

    try:
        with SessionLocal() as db:
            return int(config_service.get(db, "WORKER_TICK_SECONDS"))
    except Exception:
        return 15


def _debounce_seconds() -> int:
    """Read REBUILD_DEBOUNCE_SECONDS from platform_config (admin-editable
    since v0.1.11). Falls back to settings.debounce_seconds (the .env
    legacy value) if the DB is unreachable."""
    from . import config_service

    try:
        with SessionLocal() as db:
            return int(config_service.get(db, "REBUILD_DEBOUNCE_SECONDS"))
    except Exception:
        return settings.debounce_seconds


def tick() -> None:
    with SessionLocal() as db:
        job = (
            db.query(RebuildJob)
            .filter(RebuildJob.status == "pending")
            .order_by(asc(RebuildJob.created_at))
            .first()
        )
        if job is None:
            return

        deadline = job.created_at + timedelta(seconds=_debounce_seconds())
        if datetime.now(UTC) < deadline:
            return

        job.status = "running"
        job.started_at = datetime.now(UTC)
        db.commit()
        job_id = job.id
        sid = job.session_id
        max_memory = bool(job.max_memory)

    log.info("running rebuild job %s (session=%s max_memory=%s)", job_id, sid, max_memory)
    # P1 MOTIS — dispatch to the engine-appropriate builder. We resolve
    # engine in its own short txn so the row's session field is fresh
    # (operator may have edited it between job-enqueue and tick).
    engine = "otp"
    if sid is not None:
        with SessionLocal() as db:
            row = db.get(SessionRow, sid)
            if row is not None:
                engine = getattr(row, "engine", "otp") or "otp"
    if engine == "motis":
        output, success, graph_path = run_build_motis(session_id=sid, max_memory=max_memory)
    else:
        output, success, graph_path = run_build(session_id=sid, max_memory=max_memory)

    with SessionLocal() as db:
        job = db.get(RebuildJob, job_id)
        if job is None:  # pragma: no cover  defensive
            return
        job.finished_at = datetime.now(UTC)
        job.status = "done" if success else "failed"
        job.log = (job.log or "") + output[-32_000:]
        job.graph_path = graph_path

        # Auto-advance the session's state on a successful build so the
        # operator only has to click 'promote' to reach 'serving'.
        if success and sid is not None:
            s = db.get(SessionRow, sid)
            if s is not None and s.state in (
                SessionState.POPULATED.value,
                SessionState.CONFIGURED.value,
            ):
                s.state = SessionState.GRAPH_BUILT.value

        # v0.1.20 — record a graph_snapshots row so the admin UI can show
        # what was rebuilt, when, what's in it, and which build is current.
        # Without this row the rebuild table can only show timestamps + log
        # tail (the pre-v0.1.20 state of affairs). The schema's been ready
        # since spec §6.6 — this is just wiring it up.
        #
        # Best-effort: a snapshot write failure must never flip a successful
        # build to "failed", because the graph file on disk is real and
        # already wired into the symlink at this point. We log loudly so the
        # operator can still find the build via job log even if the snapshot
        # never materialised.
        #
        # v0.1.23 — `enumerate_session_inputs` walks the inbox directly and
        # captures every file OTP actually consumed (gtfs/, netex/, osm/),
        # not just the Upload-table subset. Refresh-from-URL doesn't write
        # Upload rows yet (separate v0.1.24+ work), so the v0.1.20 logic
        # surfaced an empty inputs list on NAP-imported sessions. The inbox
        # scan closes that gap by computing sha256 directly from disk; the
        # `Upload` table is still consulted (for manually-uploaded files
        # we record their upload_id and source: "uploaded") but no longer
        # the sole source of truth.
        if success and sid is not None and graph_path:
            try:
                inbox_root = settings.inbox_dir / sid
                inputs = graph_snapshots.enumerate_session_inputs(db, sid, inbox_root)
                graph_snapshots.record_snapshot(
                    db,
                    session_id=sid,
                    rebuild_job_id=job_id,
                    graph_path=Path(graph_path),
                    source_inputs=inputs,
                )
            except Exception:
                log.exception(
                    "snapshot recording failed for job %s — build itself "
                    "succeeded, graph is still on disk and symlinked",
                    job_id,
                )

        db.commit()


def _list_serving_sessions() -> list[str]:
    """Return the IDs of every session currently in 'serving' state."""
    with SessionLocal() as db:
        rows = db.query(SessionRow).filter(SessionRow.state == SessionState.SERVING.value).all()
        return [r.id for r in rows]


def _parse_otp_service_names(ps_output: str) -> set[str]:
    """Extract per-session OTP compose service names from `docker ps` output.

    Input is a `docker ps -a --format {{.Names}}` listing filtered to
    `name=^viator-otp-`. We turn each container name back into its
    compose service name so it can be compared against the set of
    services the orchestrator currently wants to be running.

    Container naming convention (compose project=`viator`, replica index
    appended by compose):

      viator-otp-<sid>-1                 → `otp-<sid>` (per-session serve)
      viator-otp-build-run-<random hex>  → ephemeral build container,
                                           skipped (always orphan-shaped
                                           but never something we should
                                           tear down — `docker compose
                                           run --rm` cleans these itself)

    Audit-2026-05 #25 surfaced the prior `docker ps` (running-only) form
    of this code missed Exited (143) orphans. Tested via
    tests/unit/test_worker_orphan_parse.py.
    """
    services: set[str] = set()
    for line in ps_output.splitlines():
        name = line.strip()
        if not name.startswith("viator-"):
            continue
        inner = name[len("viator-") :]
        # Defensive: only process per-session OTP containers. The
        # `name=^viator-otp-` docker filter upstream should already
        # exclude `viator-web-1`, `viator-postgres-1`, etc., but this
        # guard keeps the helper correct if a future caller drops or
        # changes the filter — tested in
        # test_unrelated_viator_containers_are_ignored.
        if not inner.startswith("otp-"):
            continue
        # Skip ephemeral `docker compose run --rm` build containers.
        # They match `viator-otp-*` but aren't compose services we
        # manage; trying to `compose rm` them produces noisy "no such
        # service" errors.
        if inner.startswith("otp-build-"):
            continue
        # `viator-<service>-<replica>`: replica is the numeric compose
        # index. Strip it to recover the service name.
        stem, _, last = inner.rpartition("-")
        if stem and last.isdigit():
            services.add(stem)
        else:
            # Unexpected name shape — keep the whole inner so the orphan
            # cleanup sees it (and the eventual `compose rm` will surface
            # the anomaly via a logged warning).
            services.add(inner)
    return services


# Audit-2026-05 #27 — non-compose OTP containers.
# Anything running our viator-otp image but NOT under the compose project
# (e.g. an operator's `docker run ghcr.io/top-phe/viator-otp:vX cat …` debug
# container that lingered for 45h on 2026-05-07 as `wizardly_pasteur`).
# We don't auto-remove (could be intentional) — just log a warning.
_OTP_IMAGE_PREFIX = "ghcr.io/top-phe/viator-otp"


def _find_non_compose_otp_containers(ps_output: str) -> list[tuple[str, str, str]]:
    """Parse `docker ps -a --format '{{.Names}}\\t{{.Image}}\\t{{.Status}}'`
    output and return rows for containers that:

      - run our OTP image (`ghcr.io/top-phe/viator-otp:*`)
      - are NOT under the compose project (name doesn't start with `viator-`)

    Returns a list of `(name, image, status)` tuples for the warning logger.
    Returns an empty list when input is empty / malformed / no matches —
    callers should treat that as the success case.

    Audit-2026-05 #27 — surfaced when `wizardly_pasteur` (image
    `ghcr.io/top-phe/viator-otp:v0.1.30`) lingered 45h after a manual
    `docker run … cat /opt/otp/entrypoint.sh` debug invocation. Tested
    in `tests/unit/test_worker_orphan_parse.py`.
    """
    found: list[tuple[str, str, str]] = []
    for line in ps_output.splitlines():
        parts = line.strip().split("\t")
        if len(parts) < 3:
            continue
        name, image, status = parts[0], parts[1], parts[2]
        if not name or not image:
            continue
        if not image.startswith(_OTP_IMAGE_PREFIX):
            continue
        if name.startswith("viator-"):
            # Compose-managed — handled by the existing orphan-cleanup
            # path in handle_reload_trigger() (audit #25).
            continue
        found.append((name, image, status))
    return found


def handle_reload_trigger() -> None:
    """If `/data/generated/.reload-trigger` exists, apply the current set of
    per-session compose + nginx fragments to the running stack.

    Steps:
      1. `docker compose -p viator up -d` — picks up any new `otp-<sid>`
         services from the regenerated `docker-compose.sessions.yml`.
      2. `docker exec viator-nginx-1 nginx -s reload` — picks up any new
         `location /otp/<sid>/ → otp-<sid>:8080` blocks.
      3. Delete the trigger file.

    Idempotent: running with no diffs is a no-op.
    """
    if not _RELOAD_TRIGGER.exists():
        return
    log.info("reload trigger seen at %s; applying compose + nginx reload", _RELOAD_TRIGGER)

    # Find which per-session OTP services we need to bring up. Targeting the
    # specific service names keeps a broken include from triggering rebuilds
    # of long-running services (web, nginx, postgres) on every retry.
    serving_sids = _list_serving_sessions()
    if not serving_sids:
        log.info("reload trigger seen but no sessions in serving state; skipping compose up")
    else:
        otp_services = [f"otp-{sid}" for sid in serving_sids]
        # docker compose up -d --no-deps <otp-services>:
        # --no-deps prevents touching web/nginx/postgres even if the
        #   generated fragment somehow references them
        # explicit service names: only these services are created/updated
        # cwd=/srv/docker so docker compose finds docker-compose.yml + the
        #   generated/docker-compose.sessions.yml fragment via include.
        up_cmd = [
            "docker",
            "compose",
            "-p",
            "viator",
            "up",
            "-d",
            "--no-deps",
            *otp_services,
        ]
        up = subprocess.run(  # noqa: S603
            up_cmd,
            cwd="/srv/docker",
            capture_output=True,
            text=True,
            check=False,
        )
        if up.returncode != 0:
            log.error(
                "compose up -d for %s failed (exit %s):\nstdout: %s\nstderr: %s",
                otp_services,
                up.returncode,
                up.stdout,
                up.stderr,
            )
            # Don't delete the trigger — we'll try again on the next tick.
            return

    # ── Orphan cleanup (v0.1.7, fixed audit-2026-05 #25) ─────────
    # When a session was deleted (or archived out of `serving`), its
    # `otp-<sid>` service is no longer in the regenerated compose
    # fragment. `docker compose up -d --no-deps` leaves the orphaned
    # container alive — it keeps running and consuming RAM despite
    # nginx no longer routing to it. Detect and remove them.
    #
    # Audit-2026-05 #25 — pre-this-fix this used `docker ps` (running
    # only). Containers SIGTERMed during a previous deploy and left in
    # `Exited (143)` state slipped through and accumulated indefinitely.
    # The 2026-05-07 incident triage confirmed this: two stopped OTP
    # containers from deleted sessions were lingering 21-25 hours after
    # the deploy that stopped them. Use `ps -a` so stopped containers
    # also count as orphans.
    expected_otp_services = {f"otp-{sid}" for sid in serving_sids}
    ps = subprocess.run(  # noqa: S603
        [_DOCKER, "ps", "-a", "--format", "{{.Names}}", "--filter", "name=^viator-otp-"],
        capture_output=True,
        text=True,
        check=False,
    )
    if ps.returncode == 0:
        existing_otp_services = _parse_otp_service_names(ps.stdout)
        orphans = existing_otp_services - expected_otp_services
        for orphan in sorted(orphans):
            log.info("removing orphan compose service %s", orphan)
            rm = subprocess.run(  # noqa: S603
                [_DOCKER, "compose", "-p", "viator", "rm", "-f", "-s", "-v", orphan],
                cwd="/srv/docker",
                capture_output=True,
                text=True,
                check=False,
            )
            if rm.returncode != 0:
                log.warning(
                    "rm of orphan %s failed (exit %s); next tick will retry. stderr: %s",
                    orphan,
                    rm.returncode,
                    rm.stderr,
                )

    # ── Non-compose OTP containers (audit-2026-05 #27) ─────────────
    # Catch operator-spawned `docker run` containers using our OTP
    # image (e.g. `wizardly_pasteur` from a manual debug session).
    # These slip past audit #25's `name=^viator-otp-` filter. Don't
    # auto-remove (could be intentional debug); just log a clear
    # warning so the operator sees it during routine log review.
    ps_all = subprocess.run(  # noqa: S603
        [_DOCKER, "ps", "-a", "--format", "{{.Names}}\t{{.Image}}\t{{.Status}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if ps_all.returncode == 0:
        non_compose = _find_non_compose_otp_containers(ps_all.stdout)
        for name, image, status in non_compose:
            log.warning(
                "non-compose OTP container detected: %s (image=%s status=%s) "
                "— not managed by sessions_orchestrator. If intentional "
                "(operator debug), ignore. Otherwise: docker rm -f %s",
                name,
                image,
                status,
                name,
            )

    # nginx -s reload (target the compose-labeled container).
    reload = subprocess.run(  # noqa: S603
        [_DOCKER, "exec", "viator-nginx-1", "nginx", "-s", "reload"],
        capture_output=True,
        text=True,
        check=False,
    )
    if reload.returncode != 0:
        log.error(
            "nginx reload failed (exit %s):\nstdout: %s\nstderr: %s",
            reload.returncode,
            reload.stdout,
            reload.stderr,
        )
        return

    _RELOAD_TRIGGER.unlink(missing_ok=True)
    log.info("reload completed; trigger deleted")


def run_build(*, session_id: str | None, max_memory: bool = False) -> tuple[str, bool, str]:
    """Invoke OTP build via the docker socket. Returns (log, success, graph_path).

    When `max_memory` is set, the worker first stops the serving sessions +
    observability stack to free the box, sizes the build heap to host RAM, runs
    the build, then restarts everything (in a `finally`, so a build failure
    still revives them). For the worst-case all-Europe build on a single VPS.
    """
    sid = session_id or "_phase1"
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    graph_target = Path(str(settings.graph_dir)) / sid / timestamp
    graph_target.mkdir(parents=True, exist_ok=True)

    # Resolve the operator's OSM-scope choice for this session and pass it
    # through to the build container. Default (transit-focused) wins for
    # legacy sessions whose config never set `osm_scope`. Validation is
    # defensive — bad strings raise here rather than at build time, so the
    # job's log shows a clear "unknown osm_scope" instead of a shell error
    # from the entrypoint.
    from . import (
        ingestion,
        osm_filter,
        osm_geo,
        otp_api_timeout,
        otp_heap,
        otp_timezone,
        router_config,
    )

    osm_scope = osm_filter.DEFAULT_SCOPE
    # v0.1.40 — geographic OSM scope: crop the street graph to these served
    # countries (orthogonal to osm_scope's tag filter). Empty ⇒ no crop
    # (legacy sessions build unchanged). See docs/osm-geographic-scope-design.md.
    osm_countries: list[str] = []
    # v0.1.21 — explicit transitModelTimeZone, required by OTP 2.9 when
    # the graph mixes agencies declaring different timezones (SNCF says
    # Europe/Paris, Eurostar says Europe/Brussels, etc). Default keeps
    # single-FR sessions working unchanged; UI lets operators override.
    otp_tz = otp_timezone.DEFAULT_TIMEZONE
    # v0.1.23 — per-session JVM heap. Default is the env-var-driven
    # `settings.otp_build_heap` (12g unless overridden in .env), so
    # legacy sessions keep building unchanged. Operators bumping a
    # session past the heap ceiling on a NAP-bulk-import (12+ providers,
    # France-wide) now do it via the UI dropdown instead of SSH-and-
    # restart-worker.
    otp_heap_value = settings.otp_build_heap
    # v0.1.24 — per-session OTP API processing timeout. Default 30s
    # (bumped from the pre-v0.1.24 hardcoded 10s); operator can dial up
    # to 60s/120s for cross-border/multi-NAP graphs that explore many
    # candidate paths before returning. Same read-from-config pattern.
    api_timeout = otp_api_timeout.DEFAULT_TIMEOUT
    providers: list[dict[str, Any]] = []
    # Map of credential_id → (auth_type, plaintext, param_name) for any
    # credentials referenced by GTFS-RT URLs in this session's providers.
    # Resolved here (with DB session in scope) so router_config.py stays pure.
    # Value tuple matches `app.router_config.ResolvedCredentials`'s schema —
    # auth_type is one of the AuthType literals; we cast at insert time.
    from .credentials import AuthType as _AuthType

    rt_credentials: dict[str, tuple[_AuthType, str, str | None]] = {}

    if session_id:
        with SessionLocal() as db:
            row = db.get(SessionRow, session_id)
            if row is not None and row.config:
                try:
                    osm_scope = osm_filter.validate_scope(row.config.get("osm_scope"))
                except ValueError as exc:
                    log.warning("session %s has bad osm_scope: %s — using default", sid, exc)
                try:
                    osm_countries = osm_geo.validate_countries(row.config.get("osm_countries"))
                except ValueError as exc:
                    log.warning(
                        "session %s has bad osm_countries: %s — skipping geo-crop", sid, exc
                    )
                try:
                    otp_tz = otp_timezone.validate_timezone(row.config.get("otp_timezone"))
                except ValueError as exc:
                    log.warning(
                        "session %s has bad otp_timezone: %s — using default %s",
                        sid,
                        exc,
                        otp_timezone.DEFAULT_TIMEZONE,
                    )
                try:
                    otp_heap_value = otp_heap.validate_heap(
                        row.config.get("otp_build_heap"),
                        default=settings.otp_build_heap,
                    )
                except ValueError as exc:
                    log.warning(
                        "session %s has bad otp_build_heap: %s — using default %s",
                        sid,
                        exc,
                        settings.otp_build_heap,
                    )
                try:
                    api_timeout = otp_api_timeout.validate_timeout(
                        row.config.get("otp_api_timeout")
                    )
                except ValueError as exc:
                    log.warning(
                        "session %s has bad otp_api_timeout: %s — using default %s",
                        sid,
                        exc,
                        otp_api_timeout.DEFAULT_TIMEOUT,
                    )
                try:
                    providers = ingestion.normalize_providers(row.config)
                except ValueError as exc:
                    log.warning(
                        "session %s has bad provider config: %s — using empty list", sid, exc
                    )

                # Materialise credentials for GTFS-RT URLs (v0.1.10). We
                # only resolve `gtfs_rt_credential_id` here because OTP
                # is the only consumer of this map; timetable/mct/stations
                # credentials apply at refresh time (handled by
                # `_refresh_one_task` in app/api/admin/sessions.py).
                from . import credentials as crypto_module
                from .models import UserCredential

                # Set comprehension produces set[Any | None] under --strict;
                # we narrow to set[str] explicitly so the loop variable is str
                # (and the dict key type matches `rt_credentials`'s annotation).
                referenced_ids: set[str] = {
                    p["gtfs_rt_credential_id"]
                    for p in providers
                    if isinstance(p.get("gtfs_rt_credential_id"), str)
                    and p["gtfs_rt_credential_id"]
                }
                for cid in referenced_ids:
                    try:
                        cred = db.get(UserCredential, uuid.UUID(cid))
                    except (ValueError, TypeError):
                        log.warning(
                            "session %s references malformed credential id %r — skipping",
                            sid,
                            cid,
                        )
                        continue
                    if cred is None:
                        log.warning(
                            "session %s references credential %s — not found, "
                            "GTFS-RT for the affected provider will be unauthenticated",
                            sid,
                            cid,
                        )
                        continue
                    try:
                        plaintext = crypto_module.decrypt(
                            cred.ciphertext, cred.nonce, settings.jwt_secret
                        )
                    except crypto_module.CredentialDecryptError as exc:
                        log.error(
                            "session %s credential %r cannot be decrypted: %s — "
                            "GTFS-RT for the affected provider will be unauthenticated",
                            sid,
                            cred.name,
                            exc,
                        )
                        continue
                    # cred.auth_type is `str` in the ORM but the value is
                    # constrained by the DB CHECK + app-side validate to one
                    # of the AuthType literals. The cast is the cheapest way
                    # to satisfy --strict mypy without runtime overhead.
                    from typing import cast as _cast

                    rt_credentials[cid] = (
                        _cast("_AuthType", cred.auth_type),
                        plaintext,
                        cred.param_name,
                    )

    # Generate per-session router-config.json. The entrypoint copies it
    # into BUILD_DIR (overriding the baked image default) before launching
    # OTP, so each provider's GTFS-RT URLs become real-time updaters at
    # graph load time.
    if session_id:
        try:
            session_inbox = ingestion.session_inbox(session_id)
            session_inbox.mkdir(parents=True, exist_ok=True)
            (session_inbox / "router-config.json").write_text(
                router_config.render_router_config(
                    providers,
                    credentials=rt_credentials or None,
                    api_timeout=api_timeout,
                ),
                encoding="utf-8",
            )
        except Exception as exc:
            log.warning("session %s router-config.json write failed: %s", sid, exc)

    # v0.1.40 — geographic OSM crop polygon. When the session selects
    # `osm_countries`, write the merged country MultiPolygon to the inbox; the
    # otp-build entrypoint runs `osmium extract --polygon` on it before the tag
    # filter, so the street graph covers only the served countries. Written
    # only when countries are set (else the entrypoint skips the crop). The
    # inbox is mounted read-only into otp-build, so it lands here in the worker.
    if session_id and osm_countries:
        try:
            session_inbox = ingestion.session_inbox(session_id)
            session_inbox.mkdir(parents=True, exist_ok=True)
            (session_inbox / "osm-crop.geojson").write_text(
                json.dumps(osm_geo.crop_geojson(osm_countries)),
                encoding="utf-8",
            )
        except Exception as exc:
            log.warning("session %s osm-crop.geojson write failed: %s — no geo-crop", sid, exc)
    elif session_id:
        # No countries selected → ensure a stale crop file from a previous
        # build doesn't silently keep cropping. Remove it if present.
        stale = ingestion.session_inbox(session_id) / "osm-crop.geojson"
        stale.unlink(missing_ok=True)

    # v0.1.38 — max-memory rebuild: free the box and size the heap to host RAM
    # before deriving the cap. The operator accepted stopping the journey-
    # planner sessions for this build; the `finally` at the end restarts them
    # (+ observability) so a build failure still revives the stack.
    host_gb = _host_total_gb()
    stopped_services: list[str] = []
    if max_memory:
        auto = otp_heap.auto_build_heap(host_gb) if host_gb is not None else None
        if auto is not None:
            log.info(
                "max-memory rebuild: host=%dGB → auto build heap %s (was %s)",
                host_gb,
                auto,
                otp_heap_value,
            )
            otp_heap_value = auto
        else:
            log.warning(
                "max-memory rebuild requested but host RAM is unknown or too "
                "small to auto-size — keeping configured heap %s",
                otp_heap_value,
            )
        stopped_services = _max_memory_stop_targets(_list_serving_sessions())
        try:
            _MAXMEM_MARKER.parent.mkdir(parents=True, exist_ok=True)
            _MAXMEM_MARKER.write_text("\n".join(stopped_services), encoding="utf-8")
        except OSError as exc:
            log.warning("max-memory marker write failed (%s) — crash recovery off", exc)
        _stop_services(stopped_services)

    # Derive the build container's cgroup cap from the (possibly auto-sized)
    # heap so a per-session heap bump can't silently exceed a stale
    # OTP_BUILD_MEM_LIMIT and get OOM-killed (signal 9 `Killed`) mid-OSM-parse —
    # the JVM never even reaches its own -Xmx. Injected via the subprocess
    # environment so docker-compose's `mem_limit: ${OTP_BUILD_MEM_LIMIT:-12g}`
    # interpolation picks it up (a process env var beats the project's .env).
    mem_limit_value = otp_heap.mem_limit_for_heap(otp_heap_value)
    needed_gb = otp_heap.heap_to_gb(mem_limit_value)
    if host_gb is not None and needed_gb > host_gb:
        log.warning(
            "session %s build needs ~%dGB (heap=%s + native headroom) but host "
            "has only %dGB RAM — build will likely OOM-kill. Lower the session "
            "heap, crop the OSM to the corridor, or use max-memory rebuild.",
            sid,
            needed_gb,
            otp_heap_value,
            host_gb,
        )
    build_env = {**os.environ, "OTP_BUILD_MEM_LIMIT": mem_limit_value}

    cmd = [
        _DOCKER,
        "compose",
        "-p",
        "viator",  # must match `name:` in docker/docker-compose.yml
        "run",
        "--rm",
        "-e",
        # v0.1.23 — heap now resolved from session config (with the env-var
        # default as fallback), not always from settings. Lets operators
        # size memory per session via the Configure form.
        f"OTP_HEAP={otp_heap_value}",
        "-e",
        f"OTP_INBOX_DIR=/var/otp/inbox/{sid}",
        "-e",
        f"OTP_OSM_SCOPE={osm_scope}",
        # v0.1.40 — geographic crop scope (CSV of ISO codes). The entrypoint
        # geo-crops via osm-crop.geojson (written above) when this is non-empty;
        # it's also part of the streetGraph cache key so toggling countries
        # invalidates the cache.
        "-e",
        f"OTP_OSM_COUNTRIES={','.join(osm_countries)}",
        # v0.1.21 — required by OTP 2.9 when the graph mixes agency tzs.
        "-e",
        f"OTP_TIMEZONE={otp_tz}",
        "otp-build",
    ]
    # `cmd` is built from constants + the configured session_id slug only;
    # nothing user-supplied. Bandit S603 does not apply.
    # cwd=/srv/docker so `docker compose` finds docker-compose.yml + the
    # generated/ include directory (mounted from the host's /opt/viator/docker/).
    log.info(
        "session %s build: heap=%s mem_limit=%s (derived)",
        sid,
        otp_heap_value,
        mem_limit_value,
    )
    try:
        proc = subprocess.run(  # noqa: S603
            cmd,
            cwd="/srv/docker",
            capture_output=True,
            text=True,
            check=False,
            env=build_env,
        )
        output = (
            f"[viator] build resources: OTP_HEAP={otp_heap_value} "
            f"OTP_BUILD_MEM_LIMIT={mem_limit_value} (cgroup cap derived from heap)\n"
            + proc.stdout
            + _STDERR_SEP
            + proc.stderr
        )

        if proc.returncode != 0:
            return output, False, ""

        built = Path(str(settings.graph_dir)) / "graph.obj"
        if not built.exists():
            return output + "\nERROR: graph.obj not found after build", False, ""

        shutil.move(str(built), str(graph_target / "graph.obj"))

        # If the entrypoint emitted router-config.json alongside the graph
        # (v0.1.7 — generated from session.config.sources.providers[*].gtfs_rt),
        # move it next to graph.obj so the serving otp-<sid> container picks
        # up the GTFS-RT updaters at load time.
        built_router_cfg = Path(str(settings.graph_dir)) / "router-config.json"
        if built_router_cfg.exists():
            shutil.move(str(built_router_cfg), str(graph_target / "router-config.json"))

        current = Path(str(settings.graph_dir)) / sid / "current"
        if current.exists() or current.is_symlink():
            current.unlink()
        # IMPORTANT: relative target. The symlink lives inside a Docker volume
        # that's mounted at different paths in different containers (worker:
        # /data/graphs/<sid>/, otp-<sid>: /var/otp/graph/<sid>/). An absolute
        # target would only resolve in the worker's namespace; the otp serving
        # container would fail at startup with "graph.obj: No such file or
        # directory". A relative target (`current -> 20260429-042955`) works in
        # any container that mounts the volume.
        current.symlink_to(graph_target.name, target_is_directory=True)

        _prune_old_graphs(sid, keep=3)

        return output, True, str(graph_target)
    finally:
        # Always revive what a max-memory rebuild stopped — even on build
        # failure or an exception promoting the graph. The serving containers
        # re-read their `current` symlink, so a rebuilt session comes back on
        # the fresh graph.
        if max_memory and stopped_services:
            _start_services(stopped_services)
            _MAXMEM_MARKER.unlink(missing_ok=True)


_MOTIS_IMAGE = "ghcr.io/motis-project/motis:latest"

# Separator the rebuild-log emits between captured stdout and stderr from
# any docker subprocess. Module-level constant so Sonar's S1192 isn't
# tripped by the OTP + MOTIS builders both reaching for the same string.
_STDERR_SEP = "\n--- stderr ---\n"


def run_build_motis(*, session_id: str | None, max_memory: bool = False) -> tuple[str, bool, str]:
    """P1 MOTIS — invoke `motis config` + `motis import` via the docker socket.

    Returns the same `(log, success, data_path)` triple as the OTP builder
    so the `tick()` loop can stay engine-agnostic past the dispatch point.

    Lifecycle (mirrors motis-spike/README.md):
      1. Read inbox/<sid>/osm/osm.pbf and inbox/<sid>/gtfs/*.zip — same
         inputs operators already prepare for OTP.
      2. `motis config <pbf> <gtfs...>` writes `config.yml` into a fresh
         per-session staging dir under graphs/motis/<sid>/<timestamp>/.
      3. `motis import` (run with cwd=staging) reads the generated
         config.yml and produces the imported data dir alongside it.
      4. Promote the staging dir to `graphs/motis/<sid>/current` (relative
         symlink) so the serving `motis-<sid>` container's mount picks
         it up at /var/motis-graphs/motis/<sid>/current/.

    `max_memory` is wired through for parity with the OTP builder but
    has no MOTIS-specific tuning yet — MOTIS's RAM footprint is dominated
    by data mmap, not heap. Operators who need to stop sibling services
    for big imports can flip it; the same `_stop_services` / `_start_services`
    plumbing runs.
    """
    if session_id is None:
        return "ERROR: MOTIS build requires a session_id (no phase1 path)", False, ""
    sid = session_id

    inbox_root = settings.inbox_dir / sid
    pbf = inbox_root / "osm" / "osm.pbf"
    gtfs_dir = inbox_root / "gtfs"
    gtfs_files = sorted(gtfs_dir.glob("*.zip")) if gtfs_dir.is_dir() else []

    if not pbf.exists():
        return f"ERROR: no osm.pbf at {pbf} — upload or refresh first", False, ""
    if not gtfs_files:
        return f"ERROR: no GTFS files under {gtfs_dir} — upload or refresh first", False, ""

    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    # MOTIS data lives under graphs/motis/<sid>/<timestamp>/ so the OTP
    # session subtree (graphs/<sid>/...) is never mixed with MOTIS data —
    # makes the orchestrator's per-engine mount path unambiguous.
    motis_root = Path(str(settings.graph_dir)) / "motis" / sid
    staging = motis_root / timestamp
    staging.mkdir(parents=True, exist_ok=True)

    stopped_services: list[str] = []
    if max_memory:
        # Mirrors the OTP path's recovery contract: the marker holds the
        # service names (one per line) so a worker crash mid-build lets the
        # next start-up revive what got stopped. A bare timestamp would
        # leave the recovery path with nothing to restart.
        stopped_services = _max_memory_stop_targets(_list_serving_sessions())
        try:
            _MAXMEM_MARKER.parent.mkdir(parents=True, exist_ok=True)
            _MAXMEM_MARKER.write_text("\n".join(stopped_services), encoding="utf-8")
        except OSError as exc:
            log.warning("max-memory marker write failed (%s) — crash recovery off", exc)
        _stop_services(stopped_services)

    try:
        # MOTIS reads/writes everything under /data inside the container; the
        # staging dir is mounted there. Inbox is mounted read-only — the
        # config + import commands only need to enumerate inputs from it.
        common_cmd = [
            "docker",
            "run",
            "--rm",
            "--network",
            "viator_default",
            "-v",
            f"{staging}:/data",
            "-v",
            f"{inbox_root}:/inbox:ro",
            "-w",
            "/data",
        ]

        # `motis config <pbf> <gtfs...>` writes /data/config.yml. The
        # container-side paths are /inbox/... because we mounted the
        # session's inbox there read-only.
        in_container_pbf = f"/inbox/osm/{pbf.name}"
        in_container_gtfs = [f"/inbox/gtfs/{g.name}" for g in gtfs_files]
        config_cmd = [*common_cmd, _MOTIS_IMAGE, "config", in_container_pbf, *in_container_gtfs]

        log.info("session %s MOTIS build: motis config (gtfs=%d feeds)", sid, len(gtfs_files))
        # `config_cmd` is built from constants + filesystem-derived names; nothing
        # operator-supplied flows untrimmed into the argv. Bandit S603 doesn't apply.
        config_proc = subprocess.run(  # noqa: S603
            config_cmd,
            capture_output=True,
            text=True,
            check=False,
        )
        if config_proc.returncode != 0:
            return (
                (
                    f"[viator] motis config failed (exit {config_proc.returncode})\n"
                    + (config_proc.stdout or "")
                    + _STDERR_SEP
                    + (config_proc.stderr or "")
                ),
                False,
                "",
            )

        # `motis import` reads /data/config.yml and writes the rest of /data.
        import_cmd = [*common_cmd, _MOTIS_IMAGE, "import"]
        log.info("session %s MOTIS build: motis import", sid)
        import_proc = subprocess.run(  # noqa: S603
            import_cmd,
            capture_output=True,
            text=True,
            check=False,
        )
        output = (
            "[viator] motis config OK\n"
            + (config_proc.stdout or "")
            + "\n--- motis import ---\n"
            + (import_proc.stdout or "")
            + _STDERR_SEP
            + (import_proc.stderr or "")
        )

        if import_proc.returncode != 0:
            return output, False, ""

        # Sanity check: config.yml must exist post-import (it's the file the
        # serve container points its --config flag at).
        if not (staging / "config.yml").exists():
            return output + "\nERROR: config.yml not found in data dir after import", False, ""

        current = motis_root / "current"
        if current.exists() or current.is_symlink():
            current.unlink()
        # Relative target — see the rationale in run_build's symlink section
        # (volume mount paths differ between worker and serve containers).
        current.symlink_to(staging.name, target_is_directory=True)

        _prune_old_motis_imports(sid, keep=3)

        return output, True, str(staging)
    finally:
        if max_memory and stopped_services:
            _start_services(stopped_services)
            _MAXMEM_MARKER.unlink(missing_ok=True)


def _prune_old_motis_imports(sid: str, keep: int) -> None:
    base = Path(str(settings.graph_dir)) / "motis" / sid
    if not base.is_dir():
        return
    snapshots = sorted(
        (p for p in base.iterdir() if p.is_dir() and p.name != "current"),
        reverse=True,
    )
    for old in snapshots[keep:]:
        shutil.rmtree(old, ignore_errors=True)


def _prune_old_graphs(sid: str, keep: int) -> None:
    base = Path(str(settings.graph_dir)) / sid
    if not base.is_dir():
        return
    snapshots = sorted(
        (p for p in base.iterdir() if p.is_dir() and p.name != "current"),
        reverse=True,
    )
    for old in snapshots[keep:]:
        shutil.rmtree(old, ignore_errors=True)


if __name__ == "__main__":
    main()
