"""Multi-feed GTFS ingestion (v0.1.4).

Covers the two units that decide where multi-feed files land and how
`config.sources.gtfs` is interpreted at refresh time:

  - `normalize_gtfs_sources(...)`: legacy str / list / empty → canonical list
  - `dispatch(..., staged_filename=...)`:
      * single-feed (legacy): rotates ALL files in subdir to .old
      * multi-feed: rotates ONLY the matching file, leaves siblings live
"""

from __future__ import annotations

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
