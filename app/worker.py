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
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import asc

from .db import SessionLocal
from .models import RebuildJob
from .models import Session as SessionRow
from .models.sessions import SessionState
from .settings import settings

log = logging.getLogger("worker")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


# Same path written by app/api/admin/sessions.py::promote_session.
_RELOAD_TRIGGER = Path("/data/generated/.reload-trigger")


def main() -> None:
    log.info("worker starting; debounce=%ss", settings.debounce_seconds)
    while True:
        try:
            tick()
            handle_reload_trigger()
        except Exception:
            log.exception("worker tick failed")
        time.sleep(15)


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

        deadline = job.created_at + timedelta(seconds=settings.debounce_seconds)
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
        up = subprocess.run(  # noqa: S603, S607
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

    # nginx -s reload (target the compose-labeled container).
    reload = subprocess.run(  # noqa: S603
        ["docker", "exec", "viator-nginx-1", "nginx", "-s", "reload"],  # noqa: S607
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

    cmd = [
        "docker",
        "compose",
        "-p",
        "viator",  # must match `name:` in docker/docker-compose.yml
        "run",
        "--rm",
        "-e",
        f"OTP_HEAP={settings.otp_build_heap}",
        "-e",
        f"OTP_INBOX_DIR=/var/otp/inbox/{sid}",
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
