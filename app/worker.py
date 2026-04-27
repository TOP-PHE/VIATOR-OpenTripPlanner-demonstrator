"""Background worker: polls rebuild_jobs, debounces, and runs OTP build.

Runs OTP build by invoking `docker compose run --rm otp-build` over the host
docker socket. This keeps OTP in its own image and lets the worker stay slim.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import asc

from .db import RebuildJob, SessionLocal, init_db
from .settings import settings


log = logging.getLogger("worker")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def main() -> None:
    init_db()
    log.info("worker starting; debounce=%ss", settings.debounce_seconds)
    while True:
        try:
            tick()
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

        # Debounce: wait for the configured quiet period after the job was queued.
        deadline = job.created_at + timedelta(seconds=settings.debounce_seconds)
        if datetime.utcnow() < deadline:
            return

        job.status = "running"
        job.started_at = datetime.utcnow()
        db.commit()
        job_id = job.id

    log.info("running rebuild job %s", job_id)
    output, success, graph_path = run_build()

    with SessionLocal() as db:
        job = db.get(RebuildJob, job_id)
        job.finished_at = datetime.utcnow()
        job.status = "done" if success else "failed"
        job.log = (job.log or "") + output[-32_000:]
        job.graph_path = graph_path
        db.commit()


def run_build() -> tuple[str, bool, str]:
    """Invoke OTP build via the docker socket. Returns (log, success, graph_path)."""
    timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    graph_target = settings.graph_dir / timestamp
    graph_target.mkdir(parents=True, exist_ok=True)

    cmd = [
        "docker", "compose", "-p", "otp-merits",
        "run", "--rm",
        "-e", f"OTP_HEAP={settings.otp_build_heap}",
        "otp-build",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    output = proc.stdout + "\n--- stderr ---\n" + proc.stderr

    if proc.returncode != 0:
        return output, False, ""

    # The build container writes graph.obj to /var/otp/graph (the `graphs` volume).
    # Promote it to a timestamped subdir + update the `current` symlink.
    built = settings.graph_dir / "graph.obj"
    if not built.exists():
        return output + "\nERROR: graph.obj not found after build", False, ""

    shutil.move(str(built), str(graph_target / "graph.obj"))

    current = settings.graph_dir / "current"
    if current.exists() or current.is_symlink():
        current.unlink()
    current.symlink_to(graph_target, target_is_directory=True)

    _prune_old_graphs(keep=3)

    return output, True, str(graph_target)


def _prune_old_graphs(keep: int) -> None:
    snapshots = sorted(
        (p for p in settings.graph_dir.iterdir() if p.is_dir() and p.name != "current"),
        reverse=True,
    )
    for old in snapshots[keep:]:
        shutil.rmtree(old, ignore_errors=True)


if __name__ == "__main__":
    main()
