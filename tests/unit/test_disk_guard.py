"""A rebuild never fills the data disk (`app/disk_guard.py`).

On a full disk postgres and web stop, and the site with them: a hung MOTIS
import did so on 2026-11-23. Before a build the worker asks `start_refusal`;
while it runs, `stop_reason`, every few seconds. The free space is a stand-in
here (`shutil.disk_usage`); the build folders are real, under `tmp_path`.
"""

from __future__ import annotations

from collections import namedtuple
from pathlib import Path

import pytest

from app import disk_guard
from app.settings import Settings, whole_number_or_default

GB = disk_guard.GB
_Usage = namedtuple("_Usage", "total used free")


def _free(monkeypatch: pytest.MonkeyPatch, free: int) -> None:
    monkeypatch.setattr(disk_guard.shutil, "disk_usage", lambda _p: _Usage(0, 0, free))


def _unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(_p: object) -> object:
        raise FileNotFoundError("no such disk")

    monkeypatch.setattr(disk_guard.shutil, "disk_usage", refuse)


def _session_with_build(root: Path, size: int) -> Path:
    """A session folder whose `current` points at a build of `size` bytes."""
    build = root / "20261105-223104"
    build.mkdir(parents=True)
    (build / "tt.bin").write_bytes(b"x" * size)
    try:
        (root / "current").symlink_to(build.name, target_is_directory=True)
    except OSError:
        pytest.skip("this system does not let the tests create a symbolic link")
    return root


# ── before a build ─────────────────────────────────────────────────────


def test_a_build_starts_when_the_disk_keeps_the_floor_beyond_the_last_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _session_with_build(tmp_path / "motis" / "zz", 1000)
    _free(monkeypatch, 30 * GB + 1000)

    assert disk_guard.start_refusal(tmp_path, root, 30) is None


def test_a_build_does_not_start_without_room_for_the_last_build_and_the_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _session_with_build(tmp_path / "motis" / "zz", 1000)
    _free(monkeypatch, 30 * GB + 999)

    refusal = disk_guard.start_refusal(tmp_path, root, 30)

    assert refusal is not None
    assert "BUILD_MIN_FREE_GB=30" in refusal
    assert "Admin → Storage" in refusal


def test_a_first_build_needs_the_floor_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _free(monkeypatch, 30 * GB)

    assert disk_guard.current_build_bytes(tmp_path / "never-built") == 0
    assert disk_guard.start_refusal(tmp_path, tmp_path / "never-built", 30) is None
    _free(monkeypatch, 30 * GB - 1)
    assert disk_guard.start_refusal(tmp_path, tmp_path / "never-built", 30) is not None


def test_a_disk_that_cannot_be_read_lets_the_build_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _unreadable(monkeypatch)

    assert disk_guard.start_refusal(tmp_path, tmp_path, 30) is None


def test_the_last_build_is_measured_through_current(tmp_path: Path) -> None:
    root = _session_with_build(tmp_path / "zz", 4096)
    (root / "20261102-161444").mkdir()  # an older build: not the one replaced
    (root / "20261102-161444" / "tt.bin").write_bytes(b"x" * 10_000)

    assert disk_guard.current_build_bytes(root) == 4096


# ── during a build ─────────────────────────────────────────────────────


def test_a_build_goes_on_while_the_disk_keeps_the_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _free(monkeypatch, 30 * GB)

    assert disk_guard.stop_reason(tmp_path, 30, 3600.0, 12) is None


def test_a_build_is_stopped_when_the_free_space_falls_under_the_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _free(monkeypatch, 30 * GB - 1)

    reason = disk_guard.stop_reason(tmp_path, 30, 60.0, 12)

    assert reason is not None
    assert "BUILD_MIN_FREE_GB=30" in reason
    assert "before the build filled the disk" in reason


def test_a_build_is_stopped_after_the_longest_time_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _free(monkeypatch, 500 * GB)

    assert disk_guard.stop_reason(tmp_path, 30, 12 * 3600.0, 12) is None
    reason = disk_guard.stop_reason(tmp_path, 30, 12 * 3600.0 + 1, 12)
    assert reason == "the rebuild ran for more than BUILD_MAX_HOURS=12 h"


def test_zero_turns_each_check_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _free(monkeypatch, 0)

    assert disk_guard.stop_reason(tmp_path, 0, 100 * 3600.0, 0) is None
    assert disk_guard.start_refusal(tmp_path, tmp_path / "never-built", 0) is None


def test_a_disk_that_cannot_be_read_stops_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _unreadable(monkeypatch)

    assert disk_guard.stop_reason(tmp_path, 30, 60.0, 12) is None


def test_the_build_log_cap_is_a_flag_of_docker_run() -> None:
    assert disk_guard.DOCKER_RUN_LOG_ARGS == (
        "--log-opt",
        "max-size=50m",
        "--log-opt",
        "max-file=3",
    )


# ── the settings ───────────────────────────────────────────────────────


def test_the_build_settings_default_to_a_floor_a_limit_and_one_rollback() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.build_min_free_gb == 30
    assert settings.build_max_hours == 12
    assert settings.graph_builds_kept == 2


def test_the_build_settings_read_whole_numbers() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, build_min_free_gb="50g", build_max_hours="0", graph_builds_kept=3
    )

    assert (settings.build_min_free_gb, settings.build_max_hours, settings.graph_builds_kept) == (
        50,
        0,
        3,
    )


@pytest.mark.parametrize(
    ("field", "value", "default"),
    [
        ("build_min_free_gb", "lots", 30),
        ("build_max_hours", "-1", 12),
        ("graph_builds_kept", "0", 2),
        ("graph_builds_kept", "two", 2),
    ],
)
def test_a_typo_in_a_build_setting_falls_back_to_its_default(
    field: str, value: str, default: int
) -> None:
    settings = Settings(_env_file=None, **{field: value})  # type: ignore[call-arg]

    assert getattr(settings, field) == default


def test_whole_number_or_default_keeps_its_minimum() -> None:
    assert whole_number_or_default(1, 2, 1, "graph_builds_kept") == 1
    assert whole_number_or_default(0, 2, 1, "graph_builds_kept") == 2
