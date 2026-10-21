"""The OTP entrypoint's java command lines (issue #384).

docker/otp/entrypoint.sh is run with bash and a stub `java` first on PATH
that records its arguments; serve mode `exec`s it, build mode runs it once
per phase. Checks:
- serve: the exact command of each OTP_SERVE_GC_MODE, the fallback of an
  unknown mode (logged once, the router still starts), and the soft max heap
  (ZGC only, not above the heap, malformed or too large values left out);
  the G1 periodic GC load threshold from the CPU count;
- build: the command lines before #384, byte for byte, whatever the serve
  knobs say.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ENTRYPOINT = Path(__file__).resolve().parents[2] / "docker" / "otp" / "entrypoint.sh"

# The flags every otp.jar run had before #384 (and build runs still have).
BASE = "-XX:+UseStringDeduplication -XX:MaxDirectMemorySize=2g -XX:+ExitOnOutOfMemoryError"
# The stub nproc below says 18 CPUs: the periodic GC's load threshold.
G1_UNCOMMIT = (
    "-XX:G1PeriodicGCInterval=300000 -XX:MinHeapFreeRatio=10 -XX:MaxHeapFreeRatio=50"
    " -XX:G1PeriodicGCSystemLoadThreshold=18"
)

_STUB_JAVA = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$JAVA_LOG"
# A build phase "saves" into its last argument; phase 2 / one-shot need graph.obj there.
for last in "$@"; do :; done
case " $* " in *" --save "*) touch "$last/graph.obj" ;; esac
"""


def _bash() -> str:
    bash = shutil.which("bash")
    if bash is None:  # pragma: no cover - every CI runner has bash
        pytest.skip("bash is not installed")
    return bash


@pytest.fixture
def stub_bin(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    java = bin_dir / "java"
    java.write_text(_STUB_JAVA, encoding="utf-8")
    java.chmod(0o755)
    _stub(bin_dir, "nproc", "echo 18")
    return bin_dir


def _stub(bin_dir: Path, name: str, body: str) -> None:
    tool = bin_dir / name
    tool.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
    tool.chmod(0o755)


def _run(tmp_path: Path, stub_bin: Path, env: dict[str, str]) -> tuple[list[str], str]:
    """Run the entrypoint; return the java argument lines and stderr."""
    log = tmp_path / "java.log"
    full_env = {
        "PATH": f"{stub_bin}{os.pathsep}{os.environ.get('PATH', '')}",
        "JAVA_LOG": str(log),
        **env,
    }
    done = subprocess.run(
        [_bash(), str(ENTRYPOINT)],
        env=full_env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return lines, done.stderr


# ── serve ───────────────────────────────────────────────────────────────────


@pytest.fixture
def graph(tmp_path: Path) -> Path:
    current = tmp_path / "graphs" / "zz-sid" / "current"
    current.mkdir(parents=True)
    (current / "graph.obj").write_bytes(b"")
    return current.parent


def _serve(
    tmp_path: Path, stub_bin: Path, graph: Path, heap: str = "8g", **knobs: str
) -> tuple[str, str]:
    env = {"OTP_MODE": "serve", "OTP_GRAPH_DIR": str(graph), "OTP_HEAP": heap, **knobs}
    lines, stderr = _run(tmp_path, stub_bin, env)
    assert len(lines) == 1
    return lines[0], stderr


def _serve_line(heap: str, flags: str, graph: Path) -> str:
    return f"-Xmx{heap} {flags} -jar /opt/otp/otp.jar --load --serve {graph}/current"


def test_serve_defaults_to_g1_giving_unused_heap_back(
    tmp_path: Path, stub_bin: Path, graph: Path
) -> None:
    line, stderr = _serve(tmp_path, stub_bin, graph)
    assert line.split() == _serve_line("8g", f"{BASE} {G1_UNCOMMIT}", graph).split()
    assert "WARNING" not in stderr


def test_serve_g1_uncommit_mode_named_explicitly(
    tmp_path: Path, stub_bin: Path, graph: Path
) -> None:
    line, _ = _serve(tmp_path, stub_bin, graph, OTP_SERVE_GC_MODE="g1-uncommit")
    assert line.split() == _serve_line("8g", f"{BASE} {G1_UNCOMMIT}", graph).split()


def test_serve_zgc_mode(tmp_path: Path, stub_bin: Path, graph: Path) -> None:
    line, stderr = _serve(tmp_path, stub_bin, graph, OTP_SERVE_GC_MODE="zgc")
    assert line.split() == _serve_line("8g", f"-XX:+UseZGC {BASE}", graph).split()
    assert "WARNING" not in stderr


def test_serve_legacy_mode_is_the_command_before_384(
    tmp_path: Path, stub_bin: Path, graph: Path
) -> None:
    line, _ = _serve(tmp_path, stub_bin, graph, OTP_SERVE_GC_MODE="legacy")
    assert line == _serve_line("8g", BASE, graph)


def test_unknown_mode_falls_back_to_the_default_and_says_so_once(
    tmp_path: Path, stub_bin: Path, graph: Path
) -> None:
    line, stderr = _serve(tmp_path, stub_bin, graph, OTP_SERVE_GC_MODE="zz-bogus")
    assert line.split() == _serve_line("8g", f"{BASE} {G1_UNCOMMIT}", graph).split()
    assert stderr.count("unknown OTP_SERVE_GC_MODE='zz-bogus'") == 1
    assert "using g1-uncommit" in stderr


@pytest.mark.parametrize(
    "heap,soft",
    [
        ("8g", "6g"),
        ("8192m", "6g"),
        ("9g", "08g"),
        ("8g", "7168M"),
        ("8G", "7g"),
        ("8g", "8g"),  # equal to -Xmx: the JVM accepts it
        ("8g", "8100m"),  # base 2: 8g is 8192m, not 8000m
        ("8192m", "8g"),
    ],
)
def test_zgc_takes_a_soft_max_heap_not_above_the_heap(
    tmp_path: Path, stub_bin: Path, graph: Path, heap: str, soft: str
) -> None:
    line, stderr = _serve(
        tmp_path, stub_bin, graph, heap=heap, OTP_SERVE_GC_MODE="zgc", OTP_SERVE_SOFT_MAX_HEAP=soft
    )
    expected = _serve_line(heap, f"-XX:+UseZGC {BASE} -XX:SoftMaxHeapSize={soft}", graph)
    assert line.split() == expected.split()
    assert "WARNING" not in stderr


@pytest.mark.parametrize(
    "heap,soft",
    [
        ("8g", "9g"),
        ("8g", "8193m"),
        ("8g", "0g"),
        ("8g", "6gb"),
        ("8g", "lots"),
        ("8gb", "6g"),
    ],
)
def test_zgc_leaves_out_a_soft_max_heap_above_the_heap_or_malformed(
    tmp_path: Path, stub_bin: Path, graph: Path, heap: str, soft: str
) -> None:
    line, stderr = _serve(
        tmp_path, stub_bin, graph, heap=heap, OTP_SERVE_GC_MODE="zgc", OTP_SERVE_SOFT_MAX_HEAP=soft
    )
    assert line.split() == _serve_line(heap, f"-XX:+UseZGC {BASE}", graph).split()
    assert stderr.count(f"OTP_SERVE_SOFT_MAX_HEAP='{soft}' ignored") == 1


@pytest.mark.parametrize("mode", ["g1-uncommit", "legacy"])
def test_soft_max_heap_is_ignored_outside_zgc(
    tmp_path: Path, stub_bin: Path, graph: Path, mode: str
) -> None:
    line, stderr = _serve(
        tmp_path, stub_bin, graph, OTP_SERVE_GC_MODE=mode, OTP_SERVE_SOFT_MAX_HEAP="6g"
    )
    assert "SoftMaxHeapSize" not in line
    assert f"OTP_SERVE_SOFT_MAX_HEAP ignored with OTP_SERVE_GC_MODE={mode}" in stderr


def test_serve_log_line_names_the_mode(tmp_path: Path, stub_bin: Path, graph: Path) -> None:
    env = {"OTP_MODE": "serve", "OTP_GRAPH_DIR": str(graph), "OTP_HEAP": "8g"}
    done = subprocess.run(
        [_bash(), str(ENTRYPOINT)],
        env={
            **env,
            "OTP_SERVE_GC_MODE": "zgc",
            "PATH": f"{stub_bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "JAVA_LOG": str(tmp_path / "java.log"),
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert "(heap=8g, gc=zgc)" in done.stdout


@pytest.mark.parametrize(
    "nproc_body", ["exit 1", "echo 0", "echo zz"], ids=["fails", "zero", "garbage"]
)
def test_load_threshold_is_left_out_without_a_usable_cpu_count(
    tmp_path: Path, stub_bin: Path, graph: Path, nproc_body: str
) -> None:
    _stub(stub_bin, "nproc", nproc_body)
    line, stderr = _serve(tmp_path, stub_bin, graph)
    flags = "-XX:G1PeriodicGCInterval=300000 -XX:MinHeapFreeRatio=10 -XX:MaxHeapFreeRatio=50"
    assert line.split() == _serve_line("8g", f"{BASE} {flags}", graph).split()
    assert "WARNING" not in stderr


@pytest.mark.parametrize("mode", ["zgc", "legacy"])
def test_load_threshold_is_g1_uncommit_only(
    tmp_path: Path, stub_bin: Path, graph: Path, mode: str
) -> None:
    line, _ = _serve(tmp_path, stub_bin, graph, OTP_SERVE_GC_MODE=mode)
    assert "G1Periodic" not in line


# ── build: unchanged ────────────────────────────────────────────────────────


@pytest.fixture
def inbox(tmp_path: Path) -> Path:
    root = tmp_path / "inbox" / "zz-sid"
    (root / "osm").mkdir(parents=True)
    (root / "gtfs").mkdir()
    (root / "osm" / "osm.pbf").write_bytes(b"zz invented pbf")
    (root / "gtfs" / "zz-feed.zip").write_bytes(b"zz invented zip")
    (root / "router-config.json").write_text("{}", encoding="utf-8")
    return root


@pytest.mark.parametrize(
    "phases,commands",
    [
        ("two_phase", ["--buildStreet --save", "--loadStreet --save"]),
        ("one_shot", ["--build --save"]),
    ],
)
@pytest.mark.parametrize(
    "knobs",
    [{}, {"OTP_SERVE_GC_MODE": "zgc", "OTP_SERVE_SOFT_MAX_HEAP": "6g"}],
    ids=["no-serve-knobs", "serve-knobs-set"],
)
def test_build_command_lines_are_unchanged(
    tmp_path: Path,
    stub_bin: Path,
    inbox: Path,
    phases: str,
    commands: list[str],
    knobs: dict[str, str],
) -> None:
    env = {
        "OTP_MODE": "build",
        "OTP_INBOX_DIR": str(inbox),
        "OTP_GRAPH_DIR": str(tmp_path / "graphs"),
        "OTP_HEAP": "8g",
        "OTP_OSM_SCOPE": "comprehensive",
        "OTP_TIMEZONE": "Europe/Paris",
        "OTP_BUILD_PHASES": phases,
        **knobs,
    }
    lines, stderr = _run(tmp_path, stub_bin, env)
    assert len(lines) == len(commands)
    for line, command in zip(lines, commands, strict=True):
        build_dir = line.rsplit(" ", 1)[1]
        assert line == f"-Xmx8g {BASE} -jar /opt/otp/otp.jar {command} {build_dir}"
    assert "WARNING" not in stderr
    assert (tmp_path / "graphs" / "graph.obj").exists()
