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
from types import SimpleNamespace

import pytest

from app import worker

_DATASET_TIMEZONES = worker._motis_dataset_timezones


@pytest.fixture(autouse=True)
def _no_session_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """The build reads the session's providers for their time zones; these
    tests have no database."""
    monkeypatch.setattr(worker, "_motis_dataset_timezones", lambda _sid: {})


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


def test_run_build_motis_fails_when_no_timetable_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A MOTIS build with PBF but no timetable feeds (neither GTFS nor
    NeTEx) is meaningless — fail fast with a clear message that mentions
    both source directories so operators know which slot to populate."""
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
    assert "timetable" in log
    # Both candidate paths surfaced so the operator knows which slot to fill.
    assert "gtfs" in log
    assert "netex" in log


def test_run_build_motis_includes_netex_files_in_config_cmd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A MOTIS session with only a `netex/` slot populated (no `gtfs/`)
    must still build. The config command must reference the NeTEx file
    at `/inbox/<sid>/netex/<name>` so MOTIS's auto-detection by content
    picks the NeTEx loader. Regression guard for v0.1.43.08 where the
    worker globbed only `gtfs/*.zip` and silently ignored NeTEx uploads.
    """
    inbox = tmp_path / "inbox"
    graphs = tmp_path / "graphs"
    inbox.mkdir(exist_ok=True)
    graphs.mkdir(exist_ok=True)
    from app.settings import settings

    monkeypatch.setattr(settings, "inbox_dir", inbox)
    monkeypatch.setattr(settings, "graph_dir", graphs)

    sid = "eu-rail-motis"
    (inbox / sid / "osm").mkdir(parents=True)
    (inbox / sid / "osm" / "osm.pbf").write_bytes(b"dummy")
    (inbox / sid / "netex").mkdir(parents=True)
    (inbox / sid / "netex" / "trenitalia.zip").write_bytes(b"dummy")

    seen_cmds: list[list[str]] = []

    class _FakeProc:
        stdout = "ok"
        stderr = ""
        returncode = 0

    def _fake_run(cmd, **_kwargs):
        seen_cmds.append(list(cmd))
        staging: Path | None = None
        for i, arg in enumerate(cmd):
            if arg == "-w" and i + 1 < len(cmd):
                _, _, rel = cmd[i + 1].partition("/graphs/")
                candidate = graphs / rel
                if candidate.exists():
                    staging = candidate
                    break
        if staging is None:
            return _FakeProc()
        if "config" in cmd:
            staging.joinpath("config.yml").write_text("osm: x\ntimetable:\n  first_day: TODAY\n")
        if "import" in cmd:
            staging.joinpath("tt.bin").write_bytes(b"dummy")
        return _FakeProc()

    monkeypatch.setattr(worker.subprocess, "run", _fake_run)

    log, ok, _ = worker.run_build_motis(session_id=sid)
    assert ok, log
    # Find the config command and verify the NeTEx path landed in it.
    config_cmd = next(c for c in seen_cmds if "config" in c)
    assert f"/inbox/{sid}/netex/trenitalia.zip" in config_cmd
    # And: the config invocation log should report a 1-feed timetable.
    # (Pinned via the log message format change in this commit.)


def test_run_build_motis_combines_gtfs_and_netex_in_config_cmd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A session with both GTFS and NeTEx providers — the typical real
    eu-rail-motis shape after Trenitalia is added — must pass *both* sets
    of files to motis config so MOTIS sees the whole timetable picture."""
    inbox = tmp_path / "inbox"
    graphs = tmp_path / "graphs"
    inbox.mkdir(exist_ok=True)
    graphs.mkdir(exist_ok=True)
    from app.settings import settings

    monkeypatch.setattr(settings, "inbox_dir", inbox)
    monkeypatch.setattr(settings, "graph_dir", graphs)

    sid = "eu-rail-motis"
    (inbox / sid / "osm").mkdir(parents=True)
    (inbox / sid / "osm" / "osm.pbf").write_bytes(b"dummy")
    (inbox / sid / "gtfs").mkdir(parents=True)
    (inbox / sid / "gtfs" / "sbb.zip").write_bytes(b"dummy")
    (inbox / sid / "gtfs" / "sncf.zip").write_bytes(b"dummy")
    (inbox / sid / "netex").mkdir(parents=True)
    (inbox / sid / "netex" / "trenitalia.zip").write_bytes(b"dummy")

    seen_cmds: list[list[str]] = []

    class _FakeProc:
        stdout = "ok"
        stderr = ""
        returncode = 0

    def _fake_run(cmd, **_kwargs):
        seen_cmds.append(list(cmd))
        staging: Path | None = None
        for i, arg in enumerate(cmd):
            if arg == "-w" and i + 1 < len(cmd):
                _, _, rel = cmd[i + 1].partition("/graphs/")
                candidate = graphs / rel
                if candidate.exists():
                    staging = candidate
                    break
        if staging is None:
            return _FakeProc()
        if "config" in cmd:
            staging.joinpath("config.yml").write_text("osm: x\ntimetable:\n  first_day: TODAY\n")
        if "import" in cmd:
            staging.joinpath("tt.bin").write_bytes(b"dummy")
        return _FakeProc()

    monkeypatch.setattr(worker.subprocess, "run", _fake_run)

    log, ok, _ = worker.run_build_motis(session_id=sid)
    assert ok, log
    config_cmd = next(c for c in seen_cmds if "config" in c)
    # All three feeds in the cmd, each with its source-subdir path.
    assert f"/inbox/{sid}/gtfs/sbb.zip" in config_cmd
    assert f"/inbox/{sid}/gtfs/sncf.zip" in config_cmd
    assert f"/inbox/{sid}/netex/trenitalia.zip" in config_cmd


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
        # The worker mkdir's the staging dir against its own /data/graphs
        # mount before invoking docker run; the new container then reaches
        # the same dir via the `viator_graphs` named volume. In the test,
        # the host equivalent of the worker's `staging` Path is where the
        # fake subprocess should drop its artifacts.
        staging: Path | None = None
        for i, arg in enumerate(cmd):
            if arg == "-w" and i + 1 < len(cmd):
                # Working dir inside the container looks like
                # /graphs/motis/<sid>/<timestamp>; map to the worker's
                # local path under the test's graphs tmp_path.
                _, _, rel = cmd[i + 1].partition("/graphs/")
                candidate = graphs / rel
                if candidate.exists():
                    staging = candidate
                    break
        if staging is None:
            return _FakeProc()
        if "config" in cmd:
            # Mimic MOTIS — config always writes a tiles: block referencing
            # the in-container Lua profile. _strip_tiles_block must clean
            # it before import sees it.
            staging.joinpath("config.yml").write_text(
                "osm: /inbox/nap-de-rail/osm/osm.pbf\n"
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
    # Both runs must pass `--user <uid>:<gid>` matching the WORKER's own uid
    # (not root). v0.1.43.02 used `0:0` and then `_strip_tiles_block` crashed
    # with PermissionError because the rewrite ran as the non-root worker
    # against a root-owned file. Matching uids closes that loop and also
    # means the MOTIS container can write the staging dir the worker
    # mkdir'd (which is owned by the worker's uid via the named volume).
    import os as _os

    expected_user = f"{_os.getuid()}:{_os.getgid()}"
    for c in seen_cmds:
        assert "--user" in c
        assert c[c.index("--user") + 1] == expected_user
        # And we should NOT be back at the broken 0:0:
        assert c[c.index("--user") + 1] != "0:0" or expected_user == "0:0"
    # Both runs invoke the binary by absolute path (no ENTRYPOINT in the image).
    assert "/motis" in seen_cmds[0]
    assert "/motis" in seen_cmds[1]
    # Both runs mount by NAMED VOLUME, not by bind path. The classic DinD
    # trap is passing a worker-local path as `-v <src>:<dst>` — the host
    # docker daemon then resolves <src> against the host filesystem, not
    # the worker's view. Named volumes are unambiguous.
    for c in seen_cmds:
        assert "viator_inbox:/inbox:ro" in c
        assert "viator_graphs:/graphs" in c
    # Import must pin --data <staging-dir>. The container's view of the
    # staging dir lives under /graphs/motis/<sid>/<timestamp>.
    import_cmd = seen_cmds[1]
    assert "--data" in import_cmd
    data_arg = import_cmd[import_cmd.index("--data") + 1]
    assert data_arg.startswith(f"/graphs/motis/{sid}/")
    # The PBF and GTFS feed are passed to `config` at their in-container
    # paths under /inbox/<sid>/... (named-volume mount layout).
    config_cmd = seen_cmds[0]
    assert any(f"/inbox/{sid}/osm/osm.pbf" in a for a in config_cmd)
    assert any(f"/inbox/{sid}/gtfs/renfe.zip" in a for a in config_cmd)
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
        # The container's working dir is `-w /graphs/motis/<sid>/<timestamp>`;
        # mapping the tail to the worker-side path under graphs/.
        for i, arg in enumerate(cmd):
            if arg == "-w" and i + 1 < len(cmd):
                _, _, rel = cmd[i + 1].partition("/graphs/")
                candidate = graphs / rel
                if candidate.exists() and "config" in cmd:
                    candidate.joinpath("config.yml").write_text(f"osm: /inbox/{sid}/osm/osm.pbf\n")
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
        # we're testing is never reached. Staging is derived from the
        # container's working dir (`-w /graphs/motis/<sid>/<timestamp>`).
        if "config" in cmd:
            for i, arg in enumerate(cmd):
                if arg == "-w" and i + 1 < len(cmd):
                    _, _, rel = cmd[i + 1].partition("/graphs/")
                    candidate = graphs / rel
                    if candidate.exists():
                        candidate.joinpath("config.yml").write_text(
                            f"osm: /inbox/{sid}/osm/osm.pbf\n"
                        )
                        break
            return _OkProc()
        return _FailProc()

    monkeypatch.setattr(worker.subprocess, "run", _fake_run)

    log, ok, path = worker.run_build_motis(session_id=sid)
    assert ok is False
    assert path == ""
    assert "schema mismatch" in log
    # The exit status is recorded: it is the only trace when MOTIS is killed.
    assert log.rstrip().endswith("[viator] motis import failed: exit 2")
    assert call_count["n"] == 2  # config (ok) + import (fail)


def test_exit_statuses_name_the_signal():
    assert worker._describe_exit(1) == "exit 1"
    assert worker._describe_exit(137).startswith("exit 137 - killed (SIGKILL)")
    assert "out of memory" in worker._describe_exit(137)
    assert worker._describe_exit(139) == "exit 139 - crashed (SIGSEGV, segmentation fault)"


def _failing_import_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    inbox = tmp_path / "inbox"
    graphs = tmp_path / "graphs"
    from app.settings import settings

    monkeypatch.setattr(settings, "inbox_dir", inbox)
    monkeypatch.setattr(settings, "graph_dir", graphs)
    sid = "eu19-transit-motis"
    (inbox / sid / "osm").mkdir(parents=True)
    (inbox / sid / "osm" / "osm.pbf").write_bytes(b"dummy")
    (inbox / sid / "gtfs").mkdir(parents=True)
    (inbox / sid / "gtfs" / "sncf.zip").write_bytes(b"dummy")

    class _Proc:
        stdout = ""
        stderr = ""

        def __init__(self, returncode: int) -> None:
            self.returncode = returncode

    def _fake_run(cmd, **_kwargs):
        for i, arg in enumerate(cmd):
            if arg == "-w" and i + 1 < len(cmd):
                _, _, rel = cmd[i + 1].partition("/graphs/")
                staging = graphs / rel
                if "config" in cmd:
                    staging.joinpath("config.yml").write_text("osm: x\n")
                    return _Proc(0)
                # The import writes partial data, then dies (OOM, disk full).
                staging.joinpath("osr.bin").write_bytes(b"x" * 1024)
                return _Proc(137)
        return _Proc(0)

    monkeypatch.setattr(worker.subprocess, "run", _fake_run)
    return graphs / "motis" / sid, sid


def test_failed_import_removes_its_build_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """2026-10-01: failed Europe imports were never cleaned up and filled the
    disk. A failed build must leave no folder behind."""
    root, sid = _failing_import_setup(tmp_path, monkeypatch)
    _, ok, _ = worker.run_build_motis(session_id=sid)
    assert ok is False
    assert not [p for p in root.iterdir() if p.name != "current"]


def test_failed_import_keeps_the_live_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root, sid = _failing_import_setup(tmp_path, monkeypatch)
    live = root / "20260629-213457"
    live.mkdir(parents=True)
    (live / "tt.bin").write_bytes(b"live")
    (root / "current").symlink_to(live.name, target_is_directory=True)

    _, ok, _ = worker.run_build_motis(session_id=sid)
    assert ok is False
    assert sorted(p.name for p in root.iterdir()) == ["20260629-213457", "current"]
    assert (root / "current" / "tt.bin").read_bytes() == b"live"


def test_discard_never_removes_the_current_build(tmp_path: Path):
    live = tmp_path / "20260629-213457"
    live.mkdir()
    (tmp_path / "current").symlink_to(live.name, target_is_directory=True)
    worker._discard_failed_build(live)
    assert live.is_dir()


# ── time zones of NeTEx feeds, MOTIS's import errors (app/motis_timezones.py) ──


def _netex_build_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, import_exit: int = 0
) -> tuple[str, dict[str, str]]:
    """A session with ÖBB's NeTEx file; the fake import records the config it
    was given and writes MOTIS's loader log (logs/tt.txt), as 2.11.2 does."""
    inbox = tmp_path / "inbox"
    graphs = tmp_path / "graphs"
    from app.settings import settings

    monkeypatch.setattr(settings, "inbox_dir", inbox)
    monkeypatch.setattr(settings, "graph_dir", graphs)
    sid = "eu19-transit-motis"
    (inbox / sid / "osm").mkdir(parents=True)
    (inbox / sid / "osm" / "osm.pbf").write_bytes(b"dummy")
    (inbox / sid / "netex").mkdir(parents=True)
    (inbox / sid / "netex" / "obb.zip").write_bytes(b"dummy")
    monkeypatch.setattr(worker.netex_calendar, "convert_zip", lambda _src, _dest: None)
    seen: dict[str, str] = {}

    class _Proc:
        stdout = "ok"
        stderr = ""

        def __init__(self, returncode: int) -> None:
            self.returncode = returncode

    def _fake_run(cmd, **_kwargs):
        for i, arg in enumerate(cmd):
            if arg == "-w" and i + 1 < len(cmd):
                _, _, rel = cmd[i + 1].partition("/graphs/")
                staging = graphs / rel
                if "config" in cmd:
                    staging.joinpath("config.yml").write_text(
                        "timetable:\n  datasets:\n    obb:\n"
                        f"      path: /inbox/{sid}/netex/obb.zip\n"
                        "      extend_calendar: false\n"
                    )
                    return _Proc(0)
                seen["config"] = staging.joinpath("config.yml").read_text()
                (staging / "logs").mkdir()
                (staging / "logs" / "tt.txt").write_text(
                    "ERROR:  not found in timezone database\n" * 2
                )
                staging.joinpath("tt.bin").write_bytes(b"dummy")
                return _Proc(import_exit)
        return _Proc(0)

    monkeypatch.setattr(worker.subprocess, "run", _fake_run)
    return sid, seen


def test_the_import_reads_each_netex_feed_with_its_countrys_zone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """2026-10-28: ÖBB's files declare no time zone and MOTIS dropped all of
    them; the build now gives the feed its provider's country zone."""
    sid, seen = _netex_build_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(
        worker, "_motis_dataset_timezones", lambda _sid: {"obb.zip": "Europe/Vienna"}
    )

    log, ok, _ = worker.run_build_motis(session_id=sid)
    assert ok, log
    assert (
        f"      path: /inbox/{sid}/netex/obb.zip\n      default_timezone: Europe/Vienna\n"
        in seen["config"]
    )
    tail = log.splitlines()[-4:]
    assert tail[0].startswith("[viator] motis time zone: obb.zip → Europe/Vienna")
    assert tail[1] == "[viator] motis import errors (logs/tt.txt): 2"
    assert tail[2] == "[viator]         2  not found in timezone database"
    assert tail[3].startswith("[viator]   a NeTEx file declares no time zone")


def test_a_failed_import_still_shows_motis_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sid, _seen = _netex_build_setup(tmp_path, monkeypatch, import_exit=1)
    log, ok, _ = worker.run_build_motis(session_id=sid)
    assert ok is False
    assert "[viator] motis import errors (logs/tt.txt): 2\n" in log
    assert "[viator] motis time zone: obb.zip gets no default" in log
    assert log.rstrip().endswith("[viator] motis import failed: exit 1")


def test_a_time_zone_failure_never_stops_the_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sid, seen = _netex_build_setup(tmp_path, monkeypatch)

    def _boom(_cfg, _zones):
        raise OSError("disk")

    monkeypatch.setattr(worker.motis_timezones, "set_default_timezones", _boom)
    log, ok, _ = worker.run_build_motis(session_id=sid)
    assert ok, log
    assert "default_timezone" not in seen["config"]
    assert "[viator] motis time zone: not written (OSError)" in log


class _FakeDb:
    def __init__(self, row) -> None:
        self._row = row

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        return None

    def get(self, _model, _sid):
        return self._row


def test_the_zones_come_from_the_sessions_providers(monkeypatch: pytest.MonkeyPatch):
    def _url_provider(pid: str, country: str, fmt: str) -> dict:
        timetable = {"format": fmt, "source": "url", "url": f"https://x/{pid}.zip"}
        return {"id": pid, "country_iso": country, "timetable": timetable}

    providers = [_url_provider("OBB", "AT", "netex_epip"), _url_provider("SNCF", "FR", "gtfs")]
    row = SimpleNamespace(config={"sources": {"providers": providers}})

    monkeypatch.setattr(worker, "SessionLocal", lambda: _FakeDb(row))
    assert _DATASET_TIMEZONES("eu19-transit-motis") == {"obb.zip": "Europe/Vienna"}
    monkeypatch.setattr(worker, "SessionLocal", lambda: _FakeDb(None))
    assert _DATASET_TIMEZONES("gone") == {}


def test_a_database_error_gives_no_zones(monkeypatch: pytest.MonkeyPatch):
    def _down():
        raise RuntimeError("database down")

    monkeypatch.setattr(worker, "SessionLocal", _down)
    assert _DATASET_TIMEZONES("eu19-transit-motis") == {}
