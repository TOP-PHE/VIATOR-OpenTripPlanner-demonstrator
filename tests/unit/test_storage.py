"""Storage scan and clean-up rules (app/storage.py).

The guarantees that matter: what is served or in use is never a candidate,
and delete only removes ids that a fresh scan still lists.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from app import storage

NOW = time.time()
OLD = NOW - 3 * 24 * 3600


def _write(path: Path, size: int = 10, mtime: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _motis_session(graphs: Path, sid: str) -> Path:
    root = graphs / "motis" / sid
    _write(root / "20260629-213457" / "tt.bin", 100)  # live
    _write(root / "20260628-100000" / "tt.bin", 80)  # previous, complete
    _write(root / "20261001-172636" / "osr.bin", 500)  # failed import, no tt.bin
    (root / "current").symlink_to("20260629-213457", target_is_directory=True)
    return root


@pytest.fixture
def volumes(tmp_path: Path) -> tuple[Path, Path]:
    inbox, graphs = tmp_path / "vol" / "inbox", tmp_path / "vol" / "graphs"
    _motis_session(graphs, "eu19")
    otp = graphs / "nap-fr-rail"
    _write(otp / "20260901-000000" / "graph.obj", 40)
    _write(otp / "20260801-000000" / "graph.obj", 30)
    (otp / "current").symlink_to("20260901-000000", target_is_directory=True)

    _write(inbox / "eu19" / "osm" / "osm.pbf", 50)
    _write(inbox / "eu19" / "osm" / "osm.pbf.old.1", 50)
    _write(inbox / "eu19" / "gtfs" / "sncf.zip", 20)
    _write(inbox / "eu19" / "gtfs" / "sncf.zip.old", 20)
    _write(inbox / "eu19" / "gtfs" / "ora.zip.orphaned", 20)
    _write(inbox / "eu19" / "_staging" / "old.download", 5, mtime=OLD)
    _write(inbox / "eu19" / "_staging" / "fresh.download", 5)
    _write(inbox / "eu19" / "_fetch_state" / "provider_SNCF_.json", 2)
    _write(inbox / "gone-session" / "gtfs" / "x.zip", 7)
    return inbox, graphs


def _ids(report: storage.Report) -> dict[str, str]:
    return {c.id: c.category for c in report.candidates}


def test_scan_lists_only_what_is_not_in_use(volumes: tuple[Path, Path]) -> None:
    inbox, graphs = volumes
    report = storage.scan(inbox, graphs, {"eu19", "nap-fr-rail"}, set(), now=NOW)
    assert _ids(report) == {
        "graphs/motis/eu19/20261001-172636": "failed_build",
        "graphs/motis/eu19/20260628-100000": "previous_build",
        "graphs/nap-fr-rail/20260801-000000": "previous_build",
        "inbox/eu19/osm/osm.pbf.old.1": "old_copy",
        "inbox/eu19/gtfs/sncf.zip.old": "old_copy",
        "inbox/eu19/gtfs/ora.zip.orphaned": "orphaned_feed",
        "inbox/eu19/_staging/old.download": "staging_leftover",
        "inbox/gone-session": "orphan_session",
    }
    # Largest first, so the page leads with what frees the most.
    assert report.candidates[0].id == "graphs/motis/eu19/20261001-172636"


def test_served_builds_and_live_files_are_never_candidates(volumes: tuple[Path, Path]) -> None:
    inbox, graphs = volumes
    ids = _ids(storage.scan(inbox, graphs, {"eu19", "nap-fr-rail"}, set(), now=NOW))
    for live in (
        "graphs/motis/eu19/20260629-213457",
        "graphs/motis/eu19/current",
        "graphs/nap-fr-rail/20260901-000000",
        "inbox/eu19/osm/osm.pbf",
        "inbox/eu19/gtfs/sncf.zip",
        "inbox/eu19/_staging/fresh.download",
        "inbox/eu19/_fetch_state/provider_SNCF_.json",
    ):
        assert live not in ids


def test_the_top_level_staging_folder_is_never_a_clean_up_candidate(tmp_path: Path) -> None:
    # `inbox/_staging` is where the legacy `/upload` route streams a file
    # before dispatching it. It is no session's folder, and it used to be
    # offered for deletion as "a session that no longer exists".
    inbox, graphs = tmp_path / "vol" / "inbox", tmp_path / "vol" / "graphs"
    graphs.mkdir(parents=True)
    _write(inbox / "_staging" / "20261003-120000-abcd1234" / "feed.zip", 10)
    _write(inbox / "gone-session" / "gtfs" / "x.zip", 10)

    report = storage.scan(inbox, graphs, set(), set())
    assert _ids(report) == {"inbox/gone-session": "orphan_session"}

    usage = {u.session_id: u for u in report.sessions}
    # Reported, since its size matters, but not badged as a deleted session.
    assert usage["_staging"].inbox_bytes == 10
    assert usage["_staging"].known is True
    assert usage["gone-session"].known is False

    # And delete refuses it even when asked by id.
    deleted, skipped = storage.delete(["inbox/_staging"], report, inbox, graphs)
    assert deleted == []
    assert skipped == [{"id": "inbox/_staging", "reason": "no longer a clean-up candidate"}]
    assert (inbox / "_staging" / "20261003-120000-abcd1234" / "feed.zip").is_file()


def test_a_reserved_name_cannot_be_a_session_id() -> None:
    # A session id is a slug starting with a letter, so no session can ever be
    # shadowed by a reserved folder.
    assert sorted(storage.INBOX_ROOT_RESERVED) == ["_staging"]
    assert all(name.startswith("_") for name in storage.INBOX_ROOT_RESERVED)


def test_a_session_with_a_running_rebuild_is_left_alone(volumes: tuple[Path, Path]) -> None:
    inbox, graphs = volumes
    ids = _ids(storage.scan(inbox, graphs, {"eu19", "nap-fr-rail"}, {"eu19"}, now=NOW))
    assert not any(i.startswith(("graphs/motis/eu19/", "inbox/eu19/")) for i in ids)
    assert "graphs/nap-fr-rail/20260801-000000" in ids


def test_usage_and_disk_figures(volumes: tuple[Path, Path]) -> None:
    inbox, graphs = volumes
    report = storage.scan(inbox, graphs, {"eu19", "nap-fr-rail"}, set(), now=NOW)
    eu19 = next(s for s in report.sessions if s.session_id == "eu19")
    assert eu19.graphs_bytes == 680  # symlink not counted twice
    assert eu19.current_build == "20260629-213457"
    gone = next(s for s in report.sessions if s.session_id == "gone-session")
    assert gone.known is False
    assert report.total_bytes > 0
    assert report.warn == (report.used_percent >= storage.WARN_PERCENT)


def test_delete_removes_selected_candidates(volumes: tuple[Path, Path]) -> None:
    inbox, graphs = volumes
    report = storage.scan(inbox, graphs, {"eu19", "nap-fr-rail"}, set(), now=NOW)
    deleted, skipped = storage.delete(
        ["graphs/motis/eu19/20261001-172636", "inbox/eu19/gtfs/ora.zip.orphaned"],
        report,
        inbox,
        graphs,
    )
    assert [d["id"] for d in deleted] == [
        "graphs/motis/eu19/20261001-172636",
        "inbox/eu19/gtfs/ora.zip.orphaned",
    ]
    assert skipped == []
    assert not (graphs / "motis" / "eu19" / "20261001-172636").exists()
    assert not (inbox / "eu19" / "gtfs" / "ora.zip.orphaned").exists()
    assert (graphs / "motis" / "eu19" / "current" / "tt.bin").exists()


def test_delete_refuses_anything_not_in_the_fresh_scan(volumes: tuple[Path, Path]) -> None:
    inbox, graphs = volumes
    report = storage.scan(inbox, graphs, {"eu19", "nap-fr-rail"}, set(), now=NOW)
    deleted, skipped = storage.delete(
        [
            "graphs/motis/eu19/20260629-213457",  # the served build
            "inbox/eu19/osm/osm.pbf",  # a live file
            "inbox/../../etc",  # traversal
            "graphs/motis/eu19/current",
        ],
        report,
        inbox,
        graphs,
    )
    assert deleted == []
    assert len(skipped) == 4
    assert (graphs / "motis" / "eu19" / "20260629-213457" / "tt.bin").exists()
    assert (inbox / "eu19" / "osm" / "osm.pbf").exists()


def test_resolve_candidate_stays_inside_the_volume(tmp_path: Path) -> None:
    inbox, graphs = tmp_path / "vol" / "inbox", tmp_path / "vol" / "graphs"
    inbox.mkdir(parents=True)
    graphs.mkdir(parents=True)
    assert storage.resolve_candidate("inbox/a/b.zip.old", inbox, graphs).parent.name == "a"
    for bad in ("inbox/../x", "graphs/../../etc/passwd", "elsewhere/x", "inbox/"):
        with pytest.raises(ValueError):
            storage.resolve_candidate(bad, inbox, graphs)


def test_missing_volumes_give_an_empty_report(tmp_path: Path) -> None:
    report = storage.scan(tmp_path / "no-inbox", tmp_path / "no-graphs", set(), set(), now=NOW)
    assert report.candidates == []
    assert report.sessions == []


# ─────────────────────────── API (app/api/admin/storage.py) ───────────────────────────


class _FakeDb:
    """`_scan` asks for every session id, then the sessions with a running rebuild."""

    def __init__(self, session_ids: list[str], busy: list[str]) -> None:
        self._answers = [session_ids, busy]
        self.committed = False

    def scalars(self, _stmt: object) -> list[str]:
        answer = self._answers[0]
        self._answers = [*self._answers[1:], answer]  # cycle: delete re-scans
        return answer

    def commit(self) -> None:
        self.committed = True


def _api(monkeypatch: pytest.MonkeyPatch, inbox: Path, graphs: Path):
    from app.api.admin import storage as api
    from app.settings import settings

    monkeypatch.setattr(settings, "inbox_dir", inbox)
    monkeypatch.setattr(settings, "graph_dir", graphs)
    recorded: list[dict[str, object]] = []
    monkeypatch.setattr(api.audit, "record", lambda _db, **kw: recorded.append(kw))
    return api, recorded


def test_api_report_and_delete(volumes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    inbox, graphs = volumes
    api, recorded = _api(monkeypatch, inbox, graphs)
    db = _FakeDb(["eu19", "nap-fr-rail"], [])
    report = api.get_storage(db, SimpleNamespace(id=None))  # type: ignore[arg-type]
    assert "graphs/motis/eu19/20261001-172636" in {c["id"] for c in report["candidates"]}

    request = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))
    out = api.delete_candidates(
        api.DeleteBody(ids=["graphs/motis/eu19/20261001-172636", "inbox/eu19/osm/osm.pbf"]),
        request,  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
        SimpleNamespace(id=None),  # type: ignore[arg-type]
    )
    assert [d["id"] for d in out["deleted"]] == ["graphs/motis/eu19/20261001-172636"]
    assert out["skipped"] == [
        {"id": "inbox/eu19/osm/osm.pbf", "reason": "no longer a clean-up candidate"}
    ]
    assert out["freed_bytes"] == 500
    assert len(recorded) == 1
    assert recorded[0]["action"] == "storage.cleanup"
    assert db.committed
    assert "graphs/motis/eu19/20261001-172636" not in {c["id"] for c in out["report"]["candidates"]}


def test_api_delete_with_nothing_valid_writes_no_audit(
    volumes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    inbox, graphs = volumes
    api, recorded = _api(monkeypatch, inbox, graphs)
    db = _FakeDb(["eu19", "nap-fr-rail"], [])
    request = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))
    out = api.delete_candidates(
        api.DeleteBody(ids=["inbox/eu19/osm/osm.pbf"]),
        request,  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
        SimpleNamespace(id=None),  # type: ignore[arg-type]
    )
    assert out["deleted"] == []
    assert recorded == []
    assert not db.committed


def test_a_failed_delete_does_not_leak_the_error(
    volumes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    inbox, graphs = volumes
    report = storage.scan(inbox, graphs, {"eu19", "nap-fr-rail"}, set(), now=NOW)

    def _boom(_path: object) -> None:
        raise PermissionError("[Errno 13] Permission denied: '/data/graphs/secret'")

    monkeypatch.setattr(storage.shutil, "rmtree", _boom)
    deleted, skipped = storage.delete(["graphs/motis/eu19/20261001-172636"], report, inbox, graphs)
    assert deleted == []
    assert skipped == [
        {
            "id": "graphs/motis/eu19/20261001-172636",
            "reason": "could not delete (see the web server log)",
        }
    ]
