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


def test_strip_tiles_block_removes_top_level_section(tmp_path: Path):
    """Phase-0.5 fix: MOTIS-generated config always carries a tiles: block
    referencing tiles-profiles/full.lua (a built-in container asset). The
    strip must delete the block + every indented child line, leaving sibling
    top-level keys intact."""
    cfg = tmp_path / "config.yml"
    cfg.write_text(
        "osm: /inbox/osm/osm.pbf\n"
        "tiles:\n"
        "  profile: tiles-profiles/full.lua\n"
        "  db_size: 274877906944\n"
        "  flush_threshold: 100000\n"
        "timetable:\n"
        "  first_day: TODAY\n"
        "  num_days: 365\n"
    )
    worker._strip_tiles_block(cfg)
    out = cfg.read_text()
    assert "tiles:" not in out
    assert "tiles-profiles" not in out
    assert "db_size" not in out
    # Sibling top-level keys are intact and in original order.
    assert out.startswith("osm: /inbox/osm/osm.pbf\n")
    assert "timetable:\n  first_day: TODAY\n  num_days: 365\n" in out


def test_strip_tiles_block_is_noop_when_no_tiles_section(tmp_path: Path):
    """Defensive: a config without a tiles: block (e.g. a future MOTIS
    version that gates tiles behind a flag) must round-trip unchanged."""
    cfg = tmp_path / "config.yml"
    original = "osm: /inbox/osm/osm.pbf\ntimetable:\n  first_day: TODAY\n"
    cfg.write_text(original)
    worker._strip_tiles_block(cfg)
    assert cfg.read_text() == original


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
    inbox.mkdir(exist_ok=True)
    graphs.mkdir(exist_ok=True)
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
    inbox.mkdir(exist_ok=True)
    graphs.mkdir(exist_ok=True)
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
    if a refactor combined them or skipped one, this would catch it.

    Phase-0.5 fixes pinned here: `/motis` is the explicit binary path,
    `--user 0:0` overrides the image's default uid, and `import` runs
    with `--data /data` to flatten the output. Also: the `config` step
    must produce a config.yml AND we must produce a tt.bin during import
    (the real timetable artifact that proves a successful build)."""
    inbox = tmp_path / "inbox"
    graphs = tmp_path / "graphs"
    inbox.mkdir(exist_ok=True)
    graphs.mkdir(exist_ok=True)
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
        # Resolve the staging dir from the `-v <host>:/data` mount the
        # caller passed — used by BOTH the config step (writes config.yml
        # with a tiles block) and the import step (writes tt.bin).
        staging: Path | None = None
        for i, arg in enumerate(cmd):
            if arg == "-v" and i + 1 < len(cmd):
                host_path = cmd[i + 1].split(":", 1)[0]
                if host_path.startswith(str(graphs)):
                    staging = Path(host_path)
                    break
        if staging is None:
            return _FakeProc()
        if "config" in cmd:
            # Mimic MOTIS — config always writes a tiles: block referencing
            # the in-container Lua profile. _strip_tiles_block must clean
            # it before import sees it.
            staging.joinpath("config.yml").write_text(
                "osm: /inbox/osm/osm.pbf\n"
                "tiles:\n"
                "  profile: tiles-profiles/full.lua\n"
                "  db_size: 274877906944\n"
                "timetable:\n"
                "  first_day: TODAY\n"
            )
        if "import" in cmd:
            # Successful import lands tt.bin alongside config.yml.
            staging.joinpath("tt.bin").write_bytes(b"dummy")
        return _FakeProc()

    monkeypatch.setattr(worker.subprocess, "run", _fake_run)

    log, ok, path = worker.run_build_motis(session_id=sid)
    assert ok, log
    assert path.startswith(str(graphs / "motis" / sid))

    # Exactly two docker invocations: config first, then import.
    assert len(seen_cmds) == 2
    assert "config" in seen_cmds[0]
    assert "import" in seen_cmds[1]
    # Both runs must pass `--user 0:0` (Phase-0.5 silent-write-failure fix).
    for c in seen_cmds:
        assert "--user" in c
        assert c[c.index("--user") + 1] == "0:0"
    # Both runs invoke the binary by absolute path (no ENTRYPOINT in the image).
    assert "/motis" in seen_cmds[0]
    assert "/motis" in seen_cmds[1]
    # Import must pin --data /data so preprocessed output lands flat next
    # to config.yml (without --data, MOTIS writes to /data/data/).
    assert "--data" in seen_cmds[1]
    assert seen_cmds[1][seen_cmds[1].index("--data") + 1] == "/data"
    # The PBF and GTFS feed are passed to `config`, mounted under /inbox.
    config_cmd = seen_cmds[0]
    assert any("/inbox/osm/osm.pbf" in a for a in config_cmd)
    assert any("/inbox/gtfs/renfe.zip" in a for a in config_cmd)
    # The tiles: block MUST have been stripped from the staged config.yml
    # before import ran — its presence at import time aborts with a
    # `[VERIFY FAIL] tiles profile ... does not exist` error.
    staging_dir = Path(path)
    final_config = staging_dir.joinpath("config.yml").read_text()
    assert "tiles:" not in final_config
    assert "tiles-profiles/full.lua" not in final_config
    # And the rest of the config survived the strip (osm: + timetable:).
    assert "osm:" in final_config
    assert "timetable:" in final_config


def test_run_build_motis_requires_tt_bin_for_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Phase-0.5: tt.bin is the real proof of successful timetable indexing
    (Nigiri uses flat files, not a subdirectory). Import returning exit 0
    but failing to produce tt.bin = silent failure; we must surface it."""
    inbox = tmp_path / "inbox"
    graphs = tmp_path / "graphs"
    inbox.mkdir(exist_ok=True)
    graphs.mkdir(exist_ok=True)
    from app.settings import settings

    monkeypatch.setattr(settings, "inbox_dir", inbox)
    monkeypatch.setattr(settings, "graph_dir", graphs)

    sid = "nap-de-rail"
    (inbox / sid / "osm").mkdir(parents=True)
    (inbox / sid / "osm" / "osm.pbf").write_bytes(b"dummy")
    (inbox / sid / "gtfs").mkdir(parents=True)
    (inbox / sid / "gtfs" / "renfe.zip").write_bytes(b"dummy")

    class _FakeProc:
        stdout = ""
        stderr = ""
        returncode = 0

    def _fake_run(cmd, **_kwargs):
        # Discover the staging dir.
        for i, arg in enumerate(cmd):
            if arg == "-v" and i + 1 < len(cmd):
                host_path = cmd[i + 1].split(":", 1)[0]
                if host_path.startswith(str(graphs)):
                    if "config" in cmd:
                        # Minimal valid post-strip config.
                        Path(host_path).joinpath("config.yml").write_text(
                            "osm: /inbox/osm/osm.pbf\n"
                        )
                    # Critically: import does NOT write tt.bin (simulates the
                    # silent-no-output failure mode).
                    break
        return _FakeProc()

    monkeypatch.setattr(worker.subprocess, "run", _fake_run)

    log, ok, path = worker.run_build_motis(session_id=sid)
    assert ok is False
    assert path == ""
    assert "tt.bin" in log


def test_run_build_motis_surfaces_subprocess_failure_in_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """If `motis import` exits non-zero, the failure must be visible in
    the rebuild log (operator-facing) — not silently swallowed."""
    inbox = tmp_path / "inbox"
    graphs = tmp_path / "graphs"
    inbox.mkdir(exist_ok=True)
    graphs.mkdir(exist_ok=True)
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
        # Phase-0.5: the worker checks `config.yml` exists post-config so it
        # can strip the `tiles:` block before invoking import. The mock must
        # write the file when config runs, otherwise the import-failure path
        # we're testing is never reached.
        if "config" in cmd:
            for i, arg in enumerate(cmd):
                if arg == "-v" and i + 1 < len(cmd):
                    host_path = cmd[i + 1].split(":", 1)[0]
                    if host_path.startswith(str(graphs)):
                        Path(host_path).joinpath("config.yml").write_text(
                            "osm: /inbox/osm/osm.pbf\n"
                        )
                        break
            return _OkProc()
        return _FailProc()

    monkeypatch.setattr(worker.subprocess, "run", _fake_run)

    log, ok, path = worker.run_build_motis(session_id=sid)
    assert ok is False
    assert path == ""
    assert "schema mismatch" in log
    assert call_count["n"] == 2  # config (ok) + import (fail)
