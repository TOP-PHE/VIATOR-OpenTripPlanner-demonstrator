"""Background worker: polls per-session rebuild_jobs, debounces, runs OTP build.

Per session, at most one rebuild runs at a time. The MAX_CONCURRENT_REBUILDS
config knob caps total simultaneous rebuilds across all sessions.

The worker also watches for the reload-trigger file written by
`POST /api/sessions/<sid>/promote` — when present, runs `docker compose up`
and `nginx -s reload` so per-session OTP containers come online without
the operator having to shell in.
"""

from __future__ import annotations

import logging
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
from .models import RebuildJob, Upload
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


def main() -> None:
    log.info("worker starting; debounce + tick are live-read from platform_config")
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

    log.info("running rebuild job %s (session=%s)", job_id, sid)
    output, success, graph_path = run_build(session_id=sid)

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
        # Today the `Upload` table only carries manually-uploaded files —
        # refresh-from-URL doesn't write rows there. So `source_uploads`
        # here will be the manual-upload subset of the inputs OTP actually
        # used; the operator still sees the rebuild + graph_path + version
        # on the card, just with a possibly-incomplete inputs list. v0.1.21+
        # may extend the refresh path to record Upload rows so this list is
        # complete. Tracked in the v0.1.20 changelog.
        if success and sid is not None and graph_path:
            try:
                uploads = (
                    db.query(Upload).filter(Upload.session_id == sid).all()
                )
                graph_snapshots.record_snapshot(
                    db,
                    session_id=sid,
                    rebuild_job_id=job_id,
                    graph_path=Path(graph_path),
                    source_uploads=uploads,
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

    # ── Orphan cleanup (v0.1.7) ─────────────────────────────────
    # When a session was deleted (or archived out of `serving`), its
    # `otp-<sid>` service is no longer in the regenerated compose
    # fragment. `docker compose up -d --no-deps` leaves the orphaned
    # container alive — it keeps running and consuming RAM despite
    # nginx no longer routing to it. Detect and remove them.
    expected_otp_services = {f"otp-{sid}" for sid in serving_sids}
    ps = subprocess.run(  # noqa: S603
        [_DOCKER, "ps", "--format", "{{.Names}}", "--filter", "name=^viator-otp-"],
        capture_output=True,
        text=True,
        check=False,
    )
    if ps.returncode == 0:
        # Container names are like `viator-otp-nap-fr-sncf-2026-q2-1` — strip
        # the project prefix and the compose-replica suffix to recover the
        # service name `otp-nap-fr-sncf-2026-q2`.
        running_otp_services: set[str] = set()
        for line in ps.stdout.splitlines():
            name = line.strip()
            if not name.startswith("viator-"):
                continue
            # `viator-<service>-<replica>` — replica is a numeric suffix
            inner = name[len("viator-") :]
            # Drop trailing `-N` if numeric; otherwise leave whole.
            stem, _, last = inner.rpartition("-")
            if stem and last.isdigit():
                running_otp_services.add(stem)
            else:
                running_otp_services.add(inner)
        orphans = running_otp_services - expected_otp_services
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


def run_build(*, session_id: str | None) -> tuple[str, bool, str]:
    """Invoke OTP build via the docker socket. Returns (log, success, graph_path)."""
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
    from . import ingestion, osm_filter, otp_timezone, router_config

    osm_scope = osm_filter.DEFAULT_SCOPE
    # v0.1.21 — explicit transitModelTimeZone, required by OTP 2.9 when
    # the graph mixes agencies declaring different timezones (SNCF says
    # Europe/Paris, Eurostar says Europe/Brussels, etc). Default keeps
    # single-FR sessions working unchanged; UI lets operators override.
    otp_tz = otp_timezone.DEFAULT_TIMEZONE
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
                    otp_tz = otp_timezone.validate_timezone(row.config.get("otp_timezone"))
                except ValueError as exc:
                    log.warning(
                        "session %s has bad otp_timezone: %s — using default %s",
                        sid,
                        exc,
                        otp_timezone.DEFAULT_TIMEZONE,
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
                ),
                encoding="utf-8",
            )
        except Exception as exc:
            log.warning("session %s router-config.json write failed: %s", sid, exc)

    cmd = [
        _DOCKER,
        "compose",
        "-p",
        "viator",  # must match `name:` in docker/docker-compose.yml
        "run",
        "--rm",
        "-e",
        f"OTP_HEAP={settings.otp_build_heap}",
        "-e",
        f"OTP_INBOX_DIR=/var/otp/inbox/{sid}",
        "-e",
        f"OTP_OSM_SCOPE={osm_scope}",
        # v0.1.21 — required by OTP 2.9 when the graph mixes agency tzs.
        "-e",
        f"OTP_TIMEZONE={otp_tz}",
        "otp-build",
    ]
    # `cmd` is built from constants + the configured session_id slug only;
    # nothing user-supplied. Bandit S603 does not apply.
    # cwd=/srv/docker so `docker compose` finds docker-compose.yml + the
    # generated/ include directory (mounted from the host's /opt/viator/docker/).
    proc = subprocess.run(  # noqa: S603
        cmd,
        cwd="/srv/docker",
        capture_output=True,
        text=True,
        check=False,
    )
    output = proc.stdout + "\n--- stderr ---\n" + proc.stderr

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
