"""Staleness tracking — sources_changed_at vs last_refresh_completed_at.

Three states the warning function answers:
  - Empty config / no sources_changed_at: silent (no warning)
  - sources_changed_at set, no last_refresh_completed_at: warn
    "configured but never refreshed"
  - sources_changed_at > last_refresh_completed_at: warn "URL(s) edited
    after last refresh"
  - sources_changed_at <= last_refresh_completed_at: silent (fresh)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def _ts(offset_seconds: int = 0) -> str:
    return (datetime.now(UTC) + timedelta(seconds=offset_seconds)).isoformat()


# ───────────────── staleness_warning ─────────────────


class TestStalenessWarning:
    def test_empty_config_returns_none(self):
        from app.staleness import staleness_warning

        assert staleness_warning({}) is None
        assert staleness_warning({"_meta": {}}) is None

    def test_changed_but_never_refreshed_warns(self):
        from app.staleness import staleness_warning

        cfg = {"_meta": {"sources_changed_at": _ts()}}
        warning = staleness_warning(cfg)
        assert warning is not None
        assert "never refreshed" in warning

    def test_changed_after_refresh_warns(self):
        from app.staleness import staleness_warning

        cfg = {
            "_meta": {
                "last_refresh_completed_at": _ts(-100),
                "sources_changed_at": _ts(),
            }
        }
        warning = staleness_warning(cfg)
        assert warning is not None
        assert "edited after the last refresh" in warning

    def test_refreshed_after_change_silent(self):
        from app.staleness import staleness_warning

        cfg = {
            "_meta": {
                "sources_changed_at": _ts(-100),
                "last_refresh_completed_at": _ts(),
            }
        }
        assert staleness_warning(cfg) is None

    def test_same_timestamp_silent(self):
        """Edge case: refresh and save in the same second. We don't warn —
        the operator is most likely refreshing right after a save."""
        from app.staleness import staleness_warning

        ts = _ts()
        cfg = {
            "_meta": {
                "sources_changed_at": ts,
                "last_refresh_completed_at": ts,
            }
        }
        assert staleness_warning(cfg) is None


# ──────────────── mark_sources_changed / mark_refresh_completed ────────────────


class TestMarkers:
    def test_mark_sources_changed_creates_meta(self):
        from app.staleness import mark_sources_changed

        cfg: dict = {}
        mark_sources_changed(cfg)
        assert "_meta" in cfg
        assert "sources_changed_at" in cfg["_meta"]
        # Round-trip parses as ISO 8601
        ts = cfg["_meta"]["sources_changed_at"]
        datetime.fromisoformat(ts)

    def test_mark_refresh_completed_preserves_other_meta(self):
        from app.staleness import mark_refresh_completed

        cfg = {"_meta": {"sources_changed_at": "2026-01-01T00:00:00+00:00"}}
        mark_refresh_completed(cfg)
        # Both keys present; the one we just set is more recent.
        assert cfg["_meta"]["sources_changed_at"] == "2026-01-01T00:00:00+00:00"
        assert (
            cfg["_meta"]["last_refresh_completed_at"]
            > cfg["_meta"]["sources_changed_at"]
        )


# ─────────────────── sources_subtree_equal ───────────────────


class TestSourcesSubtreeEqual:
    def test_same_sources_equal(self):
        from app.staleness import sources_subtree_equal

        a = {"sources": {"providers": [{"id": "SNCF"}]}}
        b = {"sources": {"providers": [{"id": "SNCF"}]}}
        assert sources_subtree_equal(a, b)

    def test_non_sources_changes_dont_break_equality(self):
        from app.staleness import sources_subtree_equal

        a = {"sources": {"providers": [{"id": "SNCF"}]}, "osm_scope": "transit-focused"}
        b = {"sources": {"providers": [{"id": "SNCF"}]}, "osm_scope": "comprehensive"}
        # Equality is over the `sources` subtree — osm_scope differs but
        # sources are identical, so we report equal (no staleness bump).
        assert sources_subtree_equal(a, b)

    def test_url_change_breaks_equality(self):
        from app.staleness import sources_subtree_equal

        a = {"sources": {"osm_pbf": "https://a.example/x.pbf"}}
        b = {"sources": {"osm_pbf": "https://b.example/x.pbf"}}
        assert not sources_subtree_equal(a, b)

    def test_provider_added_breaks_equality(self):
        from app.staleness import sources_subtree_equal

        a = {"sources": {"providers": [{"id": "SNCF"}]}}
        b = {"sources": {"providers": [{"id": "SNCF"}, {"id": "IDFM"}]}}
        assert not sources_subtree_equal(a, b)

    def test_none_configs(self):
        from app.staleness import sources_subtree_equal

        assert sources_subtree_equal(None, None)
        assert sources_subtree_equal({}, {})
        assert not sources_subtree_equal({"sources": {"a": 1}}, {})
