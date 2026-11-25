"""Two more ways the data disk filled, closed in configuration.

- A container's log: the json-file driver keeps all of it for as long as the
  container lives. Every container VIATOR runs is capped to the values of
  `app/disk_guard.py`: the services of docker/docker-compose.yml (one anchor),
  the per-session routers (the generated file), the build containers (flags
  of `docker run`, held in tests/unit/test_memory_caps.py).
- The deploy: it stopped half way at `git pull` on a full disk (2026-11-23),
  and every release left its images behind. bin/viator-deploy.sh now refuses
  up front and removes old unused images at the end.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app import disk_guard
from app.models.sessions import SessionState
from app.sessions_orchestrator import render_compose

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docker" / "docker-compose.yml"
DEPLOY = ROOT / "bin" / "viator-deploy.sh"

CAP = {
    "driver": "json-file",
    "options": {
        "max-size": disk_guard.CONTAINER_LOG_MAX_SIZE,
        "max-file": str(disk_guard.CONTAINER_LOG_MAX_FILES),
    },
}


@dataclass
class _StubSession:
    id: str
    engine: str | None = "otp"
    category: str = "MERITS"
    state: str = SessionState.SERVING.value
    config: dict[str, Any] = field(default_factory=dict)


def test_every_compose_service_caps_its_log() -> None:
    services = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]

    assert services
    for name, service in services.items():
        assert service.get("logging") == CAP, name


def test_every_session_router_caps_its_log() -> None:
    fragment = render_compose(
        [_StubSession(id="zz-otp"), _StubSession(id="zz-motis", engine="motis")]
    )
    services = yaml.safe_load(fragment)["services"]

    assert set(services) == {"otp-zz-otp", "motis-zz-motis"}
    for name, service in services.items():
        assert service["logging"] == CAP, name


def test_the_worker_receives_the_build_settings() -> None:
    environment = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]["worker"][
        "environment"
    ]

    assert environment["BUILD_MIN_FREE_GB"] == "${BUILD_MIN_FREE_GB:-30}"
    assert environment["BUILD_MAX_HOURS"] == "${BUILD_MAX_HOURS:-12}"
    assert environment["GRAPH_BUILDS_KEPT"] == "${GRAPH_BUILDS_KEPT:-2}"


def test_the_deploy_checks_the_free_disk_before_it_changes_anything() -> None:
    script = DEPLOY.read_text(encoding="utf-8")

    check = script.index("DEPLOY_MIN_FREE_GB=")
    assert check < script.index("git fetch")
    assert check < script.index("docker compose pull")
    assert "for path in /opt/viator /var/lib/docker; do" in script
    assert "exit 6" in script


def test_the_deploy_removes_old_unused_images_without_failing_on_it() -> None:
    script = DEPLOY.read_text(encoding="utf-8")

    prune = script.index("docker image prune --all --force")
    # After the deploy is verified, so that a failed deploy keeps every image.
    assert prune > script.index("/healthz/version")
    assert prune < script.index("DEPLOY OK")
    # Images of the last two weeks stay, for a rollback.
    assert '--filter "until=336h"' in script
    # Advisory: inside an `if`, so `set -e` does not end the script on a failure.
    assert re.search(r"if prune_out=\$\(docker image prune [^\n]*\); then", script)
