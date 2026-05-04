"""Unit tests for the v0.1.14 refresh split.

Two pure functions in app/api/admin/sessions.py make this testable without
a TestClient or Postgres:

  _build_refresh_tasks  — flag that decides whether OSM is in the work list
  _rotate_osm_pbf       — N-generation rotation of osm.pbf → osm.pbf.old.<N>

Together they're the entire "refreshing providers won't bust the streetGraph
cache" guarantee. If either regresses, France-wide rebuilds go from ~5 min
back to ~30 min.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# ─────────────────── _build_refresh_tasks: include_osm ───────────────────


def _config_with_provider_and_osm() -> dict:
    """Minimal v0.1.6-shape config covering both task families."""
    return {
        "sources": {
            "providers": [
                {
                    "id": "SNCF",
                    "label": "SNCF",
                    "country_iso": "FR",
                    "timetable": {"format": "gtfs", "url": "https://example/sncf.zip"},
                    "gtfs_rt": {},
                    "mct_url": None,
                    "stations_csv_url": None,
                }
            ],
            "osm_pbf": "https://example/france.osm.pbf",
        }
    }


def test_default_excludes_osm_from_provider_refresh():
    """The most important invariant of v0.1.14. If this regresses, every
    provider tweak re-fetches the OSM PBF and busts the streetGraph cache."""
    from app.api.admin.sessions import _build_refresh_tasks

    tasks = _build_refresh_tasks(_config_with_provider_and_osm())
    kinds = [t[1] for t in tasks]
    assert (
        "OSM-PBF" not in kinds
    ), "include_osm defaults to False — OSM must NOT be in the provider-refresh task list"
    # And the SNCF provider task is still present (otherwise we've over-corrected).
    assert "GTFS" in kinds


def test_include_osm_true_adds_osm_task():
    """When the dedicated OSM refresh endpoint asks for it, OSM is included."""
    from app.api.admin.sessions import _build_refresh_tasks

    tasks = _build_refresh_tasks(_config_with_provider_and_osm(), include_osm=True)
    kinds = [t[1] for t in tasks]
    assert kinds.count("OSM-PBF") == 1, "OSM should appear exactly once when opted in"


def test_per_provider_refresh_ignores_include_osm():
    """only_provider always wins — per-provider refresh never touches OSM,
    even if a misguided caller passes include_osm=True."""
    from app.api.admin.sessions import _build_refresh_tasks

    tasks = _build_refresh_tasks(
        _config_with_provider_and_osm(),
        only_provider="SNCF",
        include_osm=True,
    )
    kinds = [t[1] for t in tasks]
    assert (
        "OSM-PBF" not in kinds
    ), "per-provider refresh must never include OSM regardless of include_osm"
    assert "GTFS" in kinds


def test_no_osm_url_in_config_means_no_task_even_with_include_osm():
    """Defensive: if config.sources.osm_pbf is unset, include_osm=True is a no-op."""
    from app.api.admin.sessions import _build_refresh_tasks

    cfg = _config_with_provider_and_osm()
    del cfg["sources"]["osm_pbf"]
    tasks = _build_refresh_tasks(cfg, include_osm=True)
    assert all(t[1] != "OSM-PBF" for t in tasks)


# ─────────────────── _rotate_osm_pbf: N-generation rotation ───────────────────


@pytest.fixture
def osm_dir(tmp_path: Path) -> Path:
    """Per-test session-inbox skeleton with an osm/ subdir."""
    (tmp_path / "osm").mkdir(exist_ok=True)
    return tmp_path


def _write(p: Path, content: bytes) -> None:
    p.write_bytes(content)


def test_rotate_with_no_existing_files_is_noop(osm_dir: Path):
    from app.api.admin.sessions import _rotate_osm_pbf

    events = _rotate_osm_pbf(osm_dir)
    assert events == []
    assert list((osm_dir / "osm").iterdir()) == []


def test_rotate_shifts_current_to_old1(osm_dir: Path):
    from app.api.admin.sessions import _rotate_osm_pbf

    _write(osm_dir / "osm" / "osm.pbf", b"current")

    events = _rotate_osm_pbf(osm_dir)

    assert "osm.pbf → .old.1" in events
    assert not (osm_dir / "osm" / "osm.pbf").exists()
    assert (osm_dir / "osm" / "osm.pbf.old.1").read_bytes() == b"current"


def test_rotate_shifts_existing_old_generations_up(osm_dir: Path):
    from app.api.admin.sessions import _rotate_osm_pbf

    _write(osm_dir / "osm" / "osm.pbf", b"v3")
    _write(osm_dir / "osm" / "osm.pbf.old.1", b"v2")
    _write(osm_dir / "osm" / "osm.pbf.old.2", b"v1")

    events = _rotate_osm_pbf(osm_dir)

    # All three rotations should have happened.
    assert ".old.2 → .old.3" in events
    assert ".old.1 → .old.2" in events
    assert "osm.pbf → .old.1" in events
    # File contents should follow the rotation.
    assert (osm_dir / "osm" / "osm.pbf.old.1").read_bytes() == b"v3"
    assert (osm_dir / "osm" / "osm.pbf.old.2").read_bytes() == b"v2"
    assert (osm_dir / "osm" / "osm.pbf.old.3").read_bytes() == b"v1"


def test_rotate_drops_oldest_when_at_capacity(osm_dir: Path):
    """N=3 generations max. The 4th oldest (.old.3 before rotation, would
    become .old.4) gets deleted to free the slot."""
    from app.api.admin.sessions import _rotate_osm_pbf

    _write(osm_dir / "osm" / "osm.pbf", b"v4")
    _write(osm_dir / "osm" / "osm.pbf.old.1", b"v3")
    _write(osm_dir / "osm" / "osm.pbf.old.2", b"v2")
    _write(osm_dir / "osm" / "osm.pbf.old.3", b"v1-OLDEST-WILL-BE-DELETED")

    events = _rotate_osm_pbf(osm_dir)

    assert any("deleted oldest" in e for e in events)
    # We shouldn't have an .old.4 file.
    assert not (osm_dir / "osm" / "osm.pbf.old.4").exists()
    # Remaining 3 generations contain the most recent inputs.
    assert (osm_dir / "osm" / "osm.pbf.old.1").read_bytes() == b"v4"
    assert (osm_dir / "osm" / "osm.pbf.old.2").read_bytes() == b"v3"
    assert (osm_dir / "osm" / "osm.pbf.old.3").read_bytes() == b"v2"


def test_rotate_handles_missing_intermediate_generations(osm_dir: Path):
    """Operator manually deleted .old.2 — rotation shouldn't crash and should
    still shift everything that exists."""
    from app.api.admin.sessions import _rotate_osm_pbf

    _write(osm_dir / "osm" / "osm.pbf", b"current")
    _write(osm_dir / "osm" / "osm.pbf.old.1", b"prev")
    # No .old.2

    events = _rotate_osm_pbf(osm_dir)

    assert ".old.1 → .old.2" in events
    assert "osm.pbf → .old.1" in events
    assert (osm_dir / "osm" / "osm.pbf.old.1").read_bytes() == b"current"
    assert (osm_dir / "osm" / "osm.pbf.old.2").read_bytes() == b"prev"
