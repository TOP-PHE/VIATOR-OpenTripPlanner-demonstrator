"""Memory caps with no swap — issue #374.

  * the OTP serve cap derived from the serve heap, never below heap + 3g;
  * the cap format check and the compose / `docker run` renderings;
  * the per-session compose file: OTP serve capped by default with
    memswap = mem, the off switch and the headroom knob, MOTIS opt-in;
  * the MOTIS build `docker run` argv carries `--memory-swap` equal to
    `--memory` when a cap is set, and nothing when it is not;
  * a killed OTP build says why in its log.

Every value is invented.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml

from app import memory_caps, worker
from app.models.sessions import SessionState
from app.otp_heap import MIN_SERVE_HEADROOM_GB, heap_to_gb, serve_mem_limit_for_heap
from app.sessions_orchestrator import render_compose
from app.settings import settings

# ── serve cap derivation ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "heap,cap",
    [
        ("4g", "10g"),  # default serve heap: 6g headroom
        ("12g", "18g"),
        ("16g", "22g"),
        ("18g", "24g"),  # heap // 3 == 6 == the default headroom
        ("21g", "28g"),  # heap // 3 == 7 wins over 6
        ("24g", "32g"),
        ("48g", "64g"),
        ("8192m", "14g"),  # megabytes round up to whole GB
        ("12G", "18g"),
    ],
)
def test_serve_cap_follows_the_heap(heap: str, cap: str) -> None:
    assert serve_mem_limit_for_heap(heap) == cap


@pytest.mark.parametrize(
    "heap,headroom,cap",
    [
        # heap 12g: heap // 3 = 4 → the setting only matters above 4
        ("12g", 0, "16g"),
        ("12g", 4, "16g"),
        ("12g", 9, "21g"),
        # heap 2g: heap // 3 = 0 → the 3g floor holds whatever the setting
        ("2g", 0, "5g"),
        ("2g", 2, "5g"),
        ("2g", 5, "7g"),
    ],
)
def test_serve_headroom_setting_only_widens_the_cap(heap: str, headroom: int, cap: str) -> None:
    assert serve_mem_limit_for_heap(heap, headroom_gb=headroom) == cap


@pytest.mark.parametrize("heap", ["1g", "2g", "4g", "5g", "9g", "31g", "64g", "1024m"])
@pytest.mark.parametrize("headroom", [-5, 0, 1, 2, 6])
def test_serve_cap_is_never_below_heap_plus_direct_buffers(heap: str, headroom: int) -> None:
    cap = serve_mem_limit_for_heap(heap, headroom_gb=headroom)
    assert cap is not None
    assert heap_to_gb(cap) - heap_to_gb(heap) >= MIN_SERVE_HEADROOM_GB == 3


@pytest.mark.parametrize("heap", [None, "", "lots", "12", "12k", "1.5g"])
def test_malformed_serve_heap_gets_no_cap(heap: str | None) -> None:
    assert serve_mem_limit_for_heap(heap) is None


# ── format and renderings ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value,expected",
    [(None, None), ("", None), ("  ", None), ("32g", "32g"), (" 32G ", "32g"), ("9900m", "9900m")],
)
def test_validate_mem_limit(value: str | None, expected: str | None) -> None:
    assert memory_caps.validate_mem_limit(value) == expected


@pytest.mark.parametrize("value", ["0g", "32", "32gb", "-1g", "1.5g", "lots"])
def test_validate_mem_limit_refuses(value: str) -> None:
    with pytest.raises(ValueError, match="memory limit"):
        memory_caps.validate_mem_limit(value)


def test_compose_lines_set_swap_equal_to_memory() -> None:
    lines = memory_caps.compose_memory_lines("18g")
    assert lines == '    mem_limit: "18g"\n    memswap_limit: "18g"\n'
    assert memory_caps.compose_memory_lines(None) == ""


def test_docker_run_args_set_swap_equal_to_memory() -> None:
    assert memory_caps.docker_run_memory_args("9g") == ["--memory", "9g", "--memory-swap", "9g"]
    assert memory_caps.docker_run_memory_args(None) == []


# ── per-session compose file ─────────────────────────────────────────────────


@dataclass
class _StubSession:
    id: str
    engine: str | None = "otp"
    category: str = "MERITS"
    state: str = SessionState.SERVING.value
    config: dict[str, Any] = field(default_factory=dict)


def _services(sessions: list[_StubSession]) -> dict[str, Any]:
    services: dict[str, Any] = yaml.safe_load(render_compose(sessions))["services"]  # type: ignore[arg-type]
    return services


def test_otp_serve_is_capped_with_no_swap_by_default() -> None:
    services = _services([_StubSession(id="zz-otp", config={"otp_heap": "12g"})])
    svc = services["otp-zz-otp"]
    assert svc["environment"]["OTP_HEAP"] == "12g"
    assert svc["mem_limit"] == "18g"
    assert svc["memswap_limit"] == svc["mem_limit"]


def test_otp_serve_cap_follows_the_heap_derived_from_the_build_heap() -> None:
    # build 36g → serve heap 12g (app/otp_heap.default_serve_heap_for_build)
    services = _services([_StubSession(id="zz-otp", config={"otp_build_heap": "36g"})])
    assert services["otp-zz-otp"]["environment"]["OTP_HEAP"] == "12g"
    assert services["otp-zz-otp"]["mem_limit"] == "18g"


def test_otp_serve_cap_can_be_switched_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "otp_serve_mem_cap", False)
    svc = _services([_StubSession(id="zz-otp", config={"otp_heap": "12g"})])["otp-zz-otp"]
    assert "mem_limit" not in svc
    assert "memswap_limit" not in svc


def test_otp_serve_headroom_setting_widens_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "otp_serve_mem_headroom_gb", 10)
    svc = _services([_StubSession(id="zz-otp", config={"otp_heap": "12g"})])["otp-zz-otp"]
    assert (svc["mem_limit"], svc["memswap_limit"]) == ("22g", "22g")


def test_otp_serve_with_a_malformed_heap_gets_no_cap() -> None:
    svc = _services([_StubSession(id="zz-otp", config={"otp_heap": "lots"})])["otp-zz-otp"]
    assert "mem_limit" not in svc
    assert "memswap_limit" not in svc


def test_motis_serve_has_no_cap_by_default() -> None:
    svc = _services([_StubSession(id="zz-motis", engine="motis")])["motis-zz-motis"]
    assert "mem_limit" not in svc
    assert "memswap_limit" not in svc


def test_motis_serve_cap_is_opt_in_with_no_swap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "motis_serve_mem_limit", "99g")
    svc = _services([_StubSession(id="zz-motis", engine="motis")])["motis-zz-motis"]
    assert (svc["mem_limit"], svc["memswap_limit"]) == ("99g", "99g")


def test_malformed_motis_serve_cap_is_ignored_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "motis_serve_mem_limit", "lots")
    services = _services([_StubSession(id="zz-motis", engine="motis"), _StubSession(id="zz-otp")])
    assert "mem_limit" not in services["motis-zz-motis"]
    assert services["otp-zz-otp"]["mem_limit"] == "10g"


def test_every_capped_session_service_has_no_swap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "motis_serve_mem_limit", "9900m")
    services = _services(
        [
            _StubSession(id="zz-a", config={"otp_heap": "4g"}),
            _StubSession(id="zz-b", config={"otp_build_heap": "72g"}),
            _StubSession(id="zz-c", engine="motis"),
        ]
    )
    assert len(services) == 3
    for svc in services.values():
        assert svc["mem_limit"]
        assert svc["memswap_limit"] == svc["mem_limit"]


# ── MOTIS build `docker run` argv ────────────────────────────────────────────


def _run_motis_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, list[list[str]]]:
    inbox, graphs = tmp_path / "inbox", tmp_path / "graphs"
    monkeypatch.setattr(settings, "inbox_dir", inbox)
    monkeypatch.setattr(settings, "graph_dir", graphs)
    sid = "zz-motis"
    (inbox / sid / "osm").mkdir(parents=True)
    (inbox / sid / "osm" / "osm.pbf").write_bytes(b"zz")
    (inbox / sid / "gtfs").mkdir(parents=True)
    (inbox / sid / "gtfs" / "zz.zip").write_bytes(b"zz")
    seen: list[list[str]] = []

    class _Proc:
        stdout = "ok"
        stderr = ""
        returncode = 0

    def _fake_run(cmd: list[str], **_kw: Any) -> _Proc:
        seen.append(list(cmd))
        staging = graphs / cmd[cmd.index("-w") + 1].partition("/graphs/")[2]
        if "config" in cmd:
            (staging / "config.yml").write_text("osm: zz\n")
        if "import" in cmd:
            (staging / "tt.bin").write_bytes(b"zz")
        return _Proc()

    monkeypatch.setattr(worker.subprocess, "run", _fake_run)
    log, ok, _path = worker.run_build_motis(session_id=sid)
    assert ok, log
    return log, seen


def test_motis_build_argv_caps_memory_with_no_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "motis_build_mem_limit", "99g")
    log, seen = _run_motis_build(tmp_path, monkeypatch)
    assert len(seen) == 2
    for cmd in seen:
        assert cmd[cmd.index("--memory") + 1] == "99g"
        assert cmd[cmd.index("--memory-swap") + 1] == "99g"
        # flags of `docker run`, so before the image name
        assert cmd.index("--memory-swap") < cmd.index(worker._MOTIS_IMAGE)
    assert "[viator] build memory cap: 99g (no swap)" in log


@pytest.mark.parametrize("value", ["", "lots"])
def test_motis_build_argv_without_a_valid_cap_has_no_memory_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setattr(settings, "motis_build_mem_limit", value)
    log, seen = _run_motis_build(tmp_path, monkeypatch)
    for cmd in seen:
        assert "--memory" not in cmd
        assert "--memory-swap" not in cmd
    assert "build memory cap" not in log


# ── a killed OTP build says why ──────────────────────────────────────────────


def test_killed_otp_build_names_the_signal_and_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "graph_dir", tmp_path / "graphs")
    monkeypatch.setattr(settings, "otp_build_heap", "8g")
    monkeypatch.setattr(worker, "_host_total_gb", lambda: None)
    seen_env: list[dict[str, str]] = []

    class _Proc:
        stdout = "zz build output"
        stderr = ""
        returncode = 137

    def _fake_run(_cmd: list[str], **kw: Any) -> _Proc:
        seen_env.append(kw["env"])
        return _Proc()

    monkeypatch.setattr(worker.subprocess, "run", _fake_run)
    log, ok, path = worker.run_build(session_id=None)
    assert (ok, path) == (False, "")
    assert seen_env[0]["OTP_BUILD_MEM_LIMIT"] == "12g"
    assert log.rstrip().endswith(
        "[viator] otp build failed: exit 137 - killed (SIGKILL) - usually out of memory, "
        "or the container was stopped (memory cap 12g, no swap)"
    )
