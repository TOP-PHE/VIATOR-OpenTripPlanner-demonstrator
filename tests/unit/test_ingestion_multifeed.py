"""Multi-feed GTFS ingestion (v0.1.4).

Covers the two units that decide where multi-feed files land and how
`config.sources.gtfs` is interpreted at refresh time:

  - `normalize_gtfs_sources(...)`: legacy str / list / empty → canonical list
  - `dispatch(..., staged_filename=...)`:
      * single-feed (legacy): rotates ALL files in subdir to .old
      * multi-feed: rotates ONLY the matching file, leaves siblings live
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest


@pytest.fixture
def fake_db():
    """A minimal Session stand-in. We don't exercise rebuild enqueueing here
    (covered in integration tests) — the dispatch tests only care about
    on-disk filesystem effects, so we let the rebuild-enqueue path no-op
    by passing a session whose .query() returns nothing."""

    class _DBStub:
        def query(self, *a, **kw):
            class _Q:
                def filter(self, *a, **kw):
                    return self

                def first(self):
                    return None

            return _Q()

        def add(self, _obj):
            pass

        def commit(self):
            pass

    return _DBStub()


@pytest.fixture
def isolated_inbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the `settings.inbox_dir` singleton at a per-test temp dir.

    `app.settings.settings` is constructed at module-import time, so the
    conftest's `INBOX_DIR` env var doesn't propagate after the fact —
    we have to mutate the live singleton.
    """
    from app import settings as settings_module

    inbox = tmp_path / "inbox"
    # exist_ok=True: pytest's tmp_path is per-test on most filesystems but on
    # the GHA Linux runner the dir occasionally pre-exists between class
    # method invocations, surfacing FileExistsError. Idempotent mkdir keeps
    # the fixture deterministic across runtimes.
    inbox.mkdir(exist_ok=True)
    monkeypatch.setattr(settings_module.settings, "inbox_dir", inbox)
    return inbox


# ─────────────────────── normalize_gtfs_sources ───────────────────────


class TestNormalizeGtfsSources:
    """Backward-compat reader from `config.sources.gtfs` to a canonical
    list of {id, url} dicts."""

    def test_none_yields_empty(self):
        from app.ingestion import normalize_gtfs_sources

        assert normalize_gtfs_sources(None) == []
        assert normalize_gtfs_sources("") == []

    def test_legacy_string_becomes_single_entry(self):
        from app.ingestion import normalize_gtfs_sources

        out = normalize_gtfs_sources("https://example.com/sncf.zip")
        # Default feedId for the legacy single-string form is "GTFS" — visible
        # to operators after the migration but stable thereafter.
        assert out == [{"id": "GTFS", "url": "https://example.com/sncf.zip"}]

    def test_list_of_feeds_passes_through(self):
        from app.ingestion import normalize_gtfs_sources

        feeds = [
            {"id": "SNCF", "url": "https://example.com/sncf.zip"},
            {"id": "IDFM", "url": "https://example.com/idfm.zip"},
            {"id": "TRENITALIA", "url": "https://example.com/trenitalia.zip"},
        ]
        assert normalize_gtfs_sources(feeds) == feeds

    def test_invalid_feed_id_rejected(self):
        from app.ingestion import normalize_gtfs_sources

        # Lowercase
        with pytest.raises(ValueError, match="must match"):
            normalize_gtfs_sources([{"id": "sncf", "url": "https://x/y.zip"}])
        # Spaces
        with pytest.raises(ValueError, match="must match"):
            normalize_gtfs_sources([{"id": "FR SNCF", "url": "https://x/y.zip"}])
        # Too short
        with pytest.raises(ValueError, match="must match"):
            normalize_gtfs_sources([{"id": "S", "url": "https://x/y.zip"}])

    def test_duplicate_ids_rejected(self):
        from app.ingestion import normalize_gtfs_sources

        with pytest.raises(ValueError, match="appears twice"):
            normalize_gtfs_sources(
                [
                    {"id": "SNCF", "url": "https://a/x.zip"},
                    {"id": "SNCF", "url": "https://b/x.zip"},
                ]
            )

    def test_non_http_url_rejected(self):
        from app.ingestion import normalize_gtfs_sources

        with pytest.raises(ValueError, match="must be an http"):
            normalize_gtfs_sources([{"id": "SNCF", "url": "ftp://example.com/x.zip"}])

    def test_dict_root_rejected(self):
        from app.ingestion import normalize_gtfs_sources

        with pytest.raises(ValueError, match="must be a string or list"):
            normalize_gtfs_sources({"id": "SNCF", "url": "https://x/y.zip"})


def test_gtfs_staged_filename_lowercases():
    from app.ingestion import gtfs_staged_filename

    # We lowercase so case-insensitive filesystems don't collide on rename.
    # The OTP entrypoint re-uppercases the stem when generating build-config.json.
    assert gtfs_staged_filename("SNCF") == "sncf.zip"
    assert gtfs_staged_filename("FR-SNCF") == "fr-sncf.zip"


# ─────────────────────── dispatch staged_filename ───────────────────────


class TestDispatchStagedFilename:
    """`dispatch(staged_filename=...)` controls how rotation interacts with
    sibling feeds. Single-feed (None) rotates the whole subdir; multi-feed
    rotates only the matching file."""

    def test_single_feed_rotates_everything(self, fake_db, isolated_inbox, tmp_path):
        from app import ingestion

        sid = "test-session"
        gtfs_dir = ingestion.session_inbox(sid) / "gtfs"
        gtfs_dir.mkdir(parents=True, exist_ok=True)
        # Pre-existing siblings — should ALL be rotated to .old by the legacy path.
        (gtfs_dir / "sncf.zip").write_bytes(b"old SNCF")
        (gtfs_dir / "idfm.zip").write_bytes(b"old IDFM")

        # New upload (legacy path: no staged_filename → defaults to gtfs.zip
        # AND rotates everything).
        src = tmp_path / "incoming.zip"
        src.write_bytes(b"new")
        ingestion.dispatch(src, "GTFS", fake_db, session_id=sid)

        # Both pre-existing files should now be `.old`; the new one lands at
        # the default gtfs.zip name.
        assert (gtfs_dir / "sncf.zip.old").exists()
        assert (gtfs_dir / "idfm.zip.old").exists()
        assert (gtfs_dir / "gtfs.zip").read_bytes() == b"new"
        assert not (gtfs_dir / "sncf.zip").exists()  # was rotated, not retained

    def test_multi_feed_rotates_only_matching(self, fake_db, isolated_inbox, tmp_path):
        from app import ingestion

        sid = "test-session"
        gtfs_dir = ingestion.session_inbox(sid) / "gtfs"
        gtfs_dir.mkdir(parents=True, exist_ok=True)
        (gtfs_dir / "sncf.zip").write_bytes(b"old SNCF")
        (gtfs_dir / "idfm.zip").write_bytes(b"old IDFM")

        # Refresh just the SNCF feed via the multi-feed flow.
        src = tmp_path / "fresh-sncf.zip"
        src.write_bytes(b"new SNCF")
        ingestion.dispatch(
            src,
            "GTFS",
            fake_db,
            session_id=sid,
            staged_filename="sncf.zip",
        )

        # SNCF rotated, but IDFM stays live (we don't have a fresh IDFM yet
        # but our existing build-time bundle is still valid).
        assert (gtfs_dir / "sncf.zip").read_bytes() == b"new SNCF"
        assert (gtfs_dir / "sncf.zip.old").read_bytes() == b"old SNCF"
        assert (gtfs_dir / "idfm.zip").read_bytes() == b"old IDFM"
        assert not (gtfs_dir / "idfm.zip.old").exists()

    def test_multi_feed_no_prior_file(self, fake_db, isolated_inbox, tmp_path):
        """Adding a brand-new feed: no rotation needed, nothing else touched."""
        from app import ingestion

        sid = "test-session"
        gtfs_dir = ingestion.session_inbox(sid) / "gtfs"
        gtfs_dir.mkdir(parents=True, exist_ok=True)
        (gtfs_dir / "sncf.zip").write_bytes(b"existing SNCF")

        src = tmp_path / "first-trenitalia.zip"
        src.write_bytes(b"trenitalia data")
        ingestion.dispatch(
            src,
            "GTFS",
            fake_db,
            session_id=sid,
            staged_filename="trenitalia.zip",
        )

        assert (gtfs_dir / "trenitalia.zip").read_bytes() == b"trenitalia data"
        assert (gtfs_dir / "sncf.zip").read_bytes() == b"existing SNCF"
        # No spurious .old files.
        assert not (gtfs_dir / "trenitalia.zip.old").exists()
        assert not (gtfs_dir / "sncf.zip.old").exists()

    def test_non_gtfs_kind_uses_canonical_default(self, fake_db, isolated_inbox, tmp_path):
        """OSM-PBF should still go to osm.pbf regardless of multi-feed feature."""
        from app import ingestion

        sid = "test-session"
        src = tmp_path / "france.osm.pbf"
        src.write_bytes(b"\x00\x00\x00\x00fake-pbf")
        ingestion.dispatch(src, "OSM-PBF", fake_db, session_id=sid)
        osm_dir = ingestion.session_inbox(sid) / "osm"
        assert (osm_dir / "osm.pbf").exists()


# ─────────────────────── place + queue_rebuild ───────────────────────


class TestDispatchSplit:
    """`dispatch` = `place` (files only: an async caller runs it in a worker
    thread) then `queue_rebuild` (the database) when the kind warrants a
    rebuild."""

    def test_a_timetable_queues_one_rebuild(self, isolated_inbox, tmp_path, monkeypatch):
        from app import ingestion

        queued: list[dict] = []
        monkeypatch.setattr(ingestion, "_enqueue_rebuild", lambda db, **k: queued.append(k))
        src = tmp_path / "zz.zip"
        src.write_bytes(b"zz")
        assert ingestion.dispatch(src, "GTFS", "zz-db", session_id="zz-session") is True
        assert queued == [{"session_id": "zz-session", "reason": "new GTFS uploaded"}]

    def test_a_kind_without_a_rebuild_queues_none(self, isolated_inbox, tmp_path, monkeypatch):
        from app import ingestion

        queued: list[dict] = []
        monkeypatch.setattr(ingestion, "_enqueue_rebuild", lambda db, **k: queued.append(k))
        src = tmp_path / "zz.csv"
        src.write_text("zz\n99\n")
        assert ingestion.dispatch(src, "SNCF-MCT", "zz-db", session_id="zz-session") is False
        assert ingestion.place(src, "NeTEx-FR-Arrets", session_id="zz-session") is False
        assert queued == []
        runtime = ingestion.session_inbox("zz-session") / "runtime" / "SNCF-MCT"
        assert (runtime / "latest.csv").read_text() == "zz\n99\n"

    def test_place_needs_no_database(self, isolated_inbox, tmp_path):
        from app import ingestion

        src = tmp_path / "zz.zip"
        src.write_bytes(b"zz")
        assert ingestion.place(src, "GTFS", session_id="zz-session", staged_filename="zz.zip")
        assert (ingestion.session_inbox("zz-session") / "gtfs" / "zz.zip").read_bytes() == b"zz"


# ─────────────── place: one slot, several threads (repair round 1) ───────────────


class TestPlaceIsAtomicAndLockedPerSlot:
    """`place` runs in worker threads: an upload, a refresh's storing step and
    a derived provider can meet on one slot. Copies into one slot directory
    run one at a time, and the slot always holds a whole file."""

    def _slow_copy(self, monkeypatch):
        import shutil
        import threading

        from app import ingestion

        state = {"active": 0, "max": 0}
        guard = threading.Lock()
        started = threading.Event()
        release = threading.Event()
        real = shutil.copy2

        def copy2(src, dst, *a, **k):
            with guard:
                state["active"] += 1
                state["max"] = max(state["max"], state["active"])
            started.set()
            release.wait(5)
            try:
                return real(src, dst, *a, **k)
            finally:
                with guard:
                    state["active"] -= 1

        monkeypatch.setattr(ingestion.shutil, "copy2", copy2)
        return state, started, release

    def test_two_places_on_one_slot_never_mix(self, isolated_inbox, tmp_path, monkeypatch):
        import threading

        from app import ingestion

        slot = ingestion.session_inbox("zz") / "gtfs"
        slot.mkdir(parents=True)
        (slot / "zz.zip").write_bytes(b"zz old " * 99)
        state, started, release = self._slow_copy(monkeypatch)
        bodies = {name: name.encode() * 9999 for name in ("zzA", "zzB")}
        for name, body in bodies.items():
            (tmp_path / name).write_bytes(body)
        threads = [
            threading.Thread(
                target=ingestion.place,
                args=(tmp_path / name, "GTFS"),
                kwargs={"session_id": "zz", "staged_filename": "zz.zip"},
            )
            for name in bodies
        ]
        for t in threads:
            t.start()
        assert started.wait(5)
        # Time for the second thread to reach the copy too, were nothing to stop it.
        time.sleep(0.3)
        release.set()
        for t in threads:
            t.join(10)
        assert state["max"] == 1  # one copy at a time into the slot
        current = (slot / "zz.zip").read_bytes()
        previous = (slot / "zz.zip.old").read_bytes()
        assert current in bodies.values()
        assert previous in bodies.values()  # the first placed, rotated whole
        assert current != previous
        assert sorted(p.name for p in slot.iterdir()) == ["zz.zip", "zz.zip.old"]

    def test_the_slot_keeps_its_whole_file_while_the_copy_runs(
        self, isolated_inbox, tmp_path, monkeypatch
    ):
        import threading

        from app import ingestion

        slot = ingestion.session_inbox("zz") / "gtfs"
        slot.mkdir(parents=True)
        (slot / "zz.zip").write_bytes(b"zz old")
        (slot / "zzother.zip").write_bytes(b"zz other")
        _state, started, release = self._slow_copy(monkeypatch)
        (tmp_path / "new.zip").write_bytes(b"zz new")
        # The legacy single-feed flow: every current file is rotated.
        t = threading.Thread(
            target=ingestion.place, args=(tmp_path / "new.zip", "GTFS"), kwargs={"session_id": "zz"}
        )
        t.start()
        assert started.wait(5)
        # Mid-copy: the slot is untouched, the copy goes to a hidden name no build globs.
        assert (slot / "zz.zip").read_bytes() == b"zz old"
        assert not list(slot.glob("*.zip.old"))
        release.set()
        t.join(10)
        assert sorted(p.name for p in slot.iterdir()) == [
            "gtfs.zip",
            "zz.zip.old",
            "zzother.zip.old",
        ]
        assert (slot / "gtfs.zip").read_bytes() == b"zz new"

    def test_a_failed_copy_leaves_the_slot_and_no_temporary_file(
        self, isolated_inbox, tmp_path, monkeypatch
    ):
        from app import ingestion

        slot = ingestion.session_inbox("zz") / "gtfs"
        slot.mkdir(parents=True)
        (slot / "zz.zip").write_bytes(b"zz old")

        def broken(src, dst, *a, **k):
            Path(dst).write_bytes(b"half")
            raise OSError("stand-in disk full")

        monkeypatch.setattr(ingestion.shutil, "copy2", broken)
        (tmp_path / "new.zip").write_bytes(b"zz new")
        with pytest.raises(OSError, match="stand-in"):
            ingestion.place(tmp_path / "new.zip", "GTFS", session_id="zz", staged_filename="zz.zip")
        assert sorted(p.name for p in slot.iterdir()) == ["zz.zip"]
        assert (slot / "zz.zip").read_bytes() == b"zz old"


def test_the_legacy_upload_copies_off_the_event_loop(isolated_inbox, monkeypatch):
    """`/upload` (app/main.py `_do_upload`): the copy into the slot runs in a
    worker thread, and so does the rebuild queue with the Upload row (#397:
    no database call on the event loop)."""
    import asyncio
    import threading

    from app import main

    loop_thread: list[int] = []
    seen: dict[str, object] = {}

    def place(path, kind, **_k):
        seen["place_on_loop"] = threading.get_ident() == loop_thread[0]
        return True

    def queue_rebuild(db, *, session_id, kind):
        seen["queue_on_loop"] = threading.get_ident() == loop_thread[0]
        seen["queued"] = (session_id, kind)

    class FakeDb:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return None

        def add(self, row):
            seen["row"] = row.triggered_rebuild

        def commit(self):
            return None

    class FakeFile:
        filename = "zz.zip"

        def __init__(self):
            self.chunks = [b"zz", b""]

        async def read(self, _n):
            return self.chunks.pop(0)

    monkeypatch.setattr(main.ingestion, "place", place)
    monkeypatch.setattr(main.ingestion, "queue_rebuild", queue_rebuild)
    monkeypatch.setattr(main.detect, "detect", lambda _p: "GTFS")
    monkeypatch.setattr(main, "SessionLocal", FakeDb)

    async def go():
        loop_thread.append(threading.get_ident())
        return await main._do_upload("GTFS", "zz-v99", FakeFile())

    response = asyncio.run(go())
    assert response.status_code == 303
    assert seen == {
        "place_on_loop": False,
        "queue_on_loop": False,
        "queued": (None, "GTFS"),
        "row": True,
    }
