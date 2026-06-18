"""P1 MOTIS — worker.run_build_motis preflight + dispatch behaviour.

Full end-to-end builds need a docker socket + the MOTIS image + real GTFS
data, so these tests scope to:
  * Preflight failures (missing PBF / missing GTFS) — must fail clean
    with an operator-actionable message instead of a docker-side error
    that's harder to read in the rebuild log.
  * `tick()`'s engine dispatch: a session with engine='motis' must
    invoke `run_build_motis`, not the OTP path.

Subprocess invocations are intercepted with `monkeypatch.setattr` so the
docker socket is never touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app import worker


def test_run_build_motis_rejects_missing_session_id():
    log, ok, path = worker.run_build_motis(session_id=None)
    assert ok is False
    assert path == ""
    assert "requires a session_id" in log


def test_run_build_motis_fails_when_osm_pbf_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A MOTIS build with no PBF should fail at the preflight check,
    NOT halfway through `motis config` with an opaque docker error."""
    inbox = tmp_path / "inbox"
    graphs = tmp_path / "graphs"
    inbox.mkdir()
    graphs.mkdir()
    from app.settings import settings

    monkeypatch.setattr(settings, "inbox_dir", inbox)
    monkeypatch.setattr(settings, "graph_dir", graphs)

    # Create the session directory with a (populated) gtfs but no osm.
    sid = "nap-de-rail"
    gtfs_dir = inbox / sid / "gtfs"
    gtfs_dir.mkdir(parents=True)
    (gtfs_dir / "renfe.zip").write_bytes(b"dummy")

    log, ok, path = worker.run_build_motis(session_id=sid)
    assert ok is False
    assert path == ""
    assert "osm.pbf" in log


def test_run_build_motis_fails_when_no_gtfs_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A MOTIS build with PBF but no GTFS feeds is meaningless — fail
    fast with a clear message."""
    inbox = tmp_path / "inbox"
    graphs = tmp_path / "graphs"
    inbox.mkdir()
    graphs.mkdir()
    from app.settings import settings

    monkeypatch.setattr(settings, "inbox_dir", inbox)
    monkeypatch.setattr(settings, "graph_dir", graphs)

    sid = "nap-de-rail"
    osm_dir = inbox / sid / "osm"
    osm_dir.mkdir(parents=True)
    (osm_dir / "osm.pbf").write_bytes(b"dummy")

    log, ok, path = worker.run_build_motis(session_id=sid)
    assert ok is False
    assert path == ""
    assert "GTFS" in log


def test_run_build_motis_invokes_config_then_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The two-phase MOTIS lifecycle (config writes config.yml; import
    materialises the data dir) is the whole point of run_build_motis —
    if a refactor combined them or skipped one, this would catch it."""
    inbox = tmp_path / "inbox"
    graphs = tmp_path / "graphs"
    inbox.mkdir()
    graphs.mkdir()
    from app.settings import settings

    monkeypatch.setattr(settings, "inbox_dir", inbox)
    monkeypatch.setattr(settings, "graph_dir", graphs)

    sid = "nap-de-rail"
    (inbox / sid / "osm").mkdir(parents=True)
    (inbox / sid / "osm" / "osm.pbf").write_bytes(b"dummy")
    (inbox / sid / "gtfs").mkdir(parents=True)
    (inbox / sid / "gtfs" / "renfe.zip").write_bytes(b"dummy")

    seen_cmds: list[list[str]] = []

    class _FakeProc:
        stdout = "ok"
        stderr = ""
        returncode = 0

    def _fake_run(cmd, **_kwargs):
        seen_cmds.append(list(cmd))
        # Pretend the import step wrote a config.yml.
        if "import" in cmd:
            # The cwd-equivalent path: look up the staging dir from the
            # `-v <host>:/data` mount the caller passed.
            for i, arg in enumerate(cmd):
                if arg == "-v" and i + 1 < len(cmd):
                    host_path = cmd[i + 1].split(":", 1)[0]
                    if host_path.startswith(str(graphs)):
                        Path(host_path).joinpath("config.yml").write_text("dummy")
                        break
        return _FakeProc()

    monkeypatch.setattr(worker.subprocess, "run", _fake_run)

    log, ok, path = worker.run_build_motis(session_id=sid)
    assert ok, log
    assert path.startswith(str(graphs / "motis" / sid))

    # Exactly two docker invocations: config first, then import.
    assert len(seen_cmds) == 2
    assert "config" in seen_cmds[0]
    assert "import" in seen_cmds[1]
    # The PBF and GTFS feed are passed to `config`, mounted under /inbox.
    config_cmd = seen_cmds[0]
    assert any("/inbox/osm/osm.pbf" in a for a in config_cmd)
    assert any("/inbox/gtfs/renfe.zip" in a for a in config_cmd)


def test_run_build_motis_surfaces_subprocess_failure_in_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """If `motis import` exits non-zero, the failure must be visible in
    the rebuild log (operator-facing) — not silently swallowed."""
    inbox = tmp_path / "inbox"
    graphs = tmp_path / "graphs"
    inbox.mkdir()
    graphs.mkdir()
    from app.settings import settings

    monkeypatch.setattr(settings, "inbox_dir", inbox)
    monkeypatch.setattr(settings, "graph_dir", graphs)

    sid = "nap-de-rail"
    (inbox / sid / "osm").mkdir(parents=True)
    (inbox / sid / "osm" / "osm.pbf").write_bytes(b"dummy")
    (inbox / sid / "gtfs").mkdir(parents=True)
    (inbox / sid / "gtfs" / "renfe.zip").write_bytes(b"dummy")

    class _OkProc:
        stdout = "config-ok"
        stderr = ""
        returncode = 0

    class _FailProc:
        stdout = ""
        stderr = "schema mismatch"
        returncode = 2

    call_count = {"n": 0}

    def _fake_run(cmd, **_kwargs):
        call_count["n"] += 1
        return _OkProc() if "config" in cmd else _FailProc()

    monkeypatch.setattr(worker.subprocess, "run", _fake_run)

    log, ok, path = worker.run_build_motis(session_id=sid)
    assert ok is False
    assert path == ""
    assert "schema mismatch" in log
    assert call_count["n"] == 2  # config (ok) + import (fail)
