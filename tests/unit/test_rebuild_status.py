"""Unit tests for v0.1.20's rebuild-status helpers.

Two pure functions are covered here:

  _classify_rebuild_log  — parses an OTP entrypoint log tail for the
                           streetGraph-cache-hit / cache-miss markers.
                           Three outcomes: True / False / None.
  _snapshot_to_info      — maps a GraphSnapshot ORM row to the wire-
                           format SnapshotInfo. Pure dict shaping;
                           tested with stub objects to avoid a DB.

The state-derivation and joined-response logic in `_job_to_response`
is integration-shaped (needs a SQLAlchemy session + GraphSnapshot row)
and is exercised by the integration tests; we don't try to recreate
the ORM machinery here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from app.api.admin.sessions import _classify_rebuild_log, _snapshot_to_info


# ───────────────────────── _classify_rebuild_log ───────────────────────


def test_cache_hit_marker_detected():
    """The exact string the OTP entrypoint emits on cache hit. If this regex
    drifts away from the entrypoint, operators stop seeing the green "⚡ cache
    hit" pill — the whole point of the v0.1.7 cache work goes invisible."""
    log = (
        "+ docker run otp-build\n"
        "streetGraph.obj cache hit (key=2c8c1d4a128537728cb7ceb87d005a5175a6a998fc8449b9f9648eb729395279:transit-focused) — copying ...\n"
        "Phase 1/2 — SKIPPED (using cached streetGraph.obj)\n"
        "Phase 2/2 — building transit graph (heap=20g) ...\n"
        "Build complete\n"
    )
    out = _classify_rebuild_log(log)
    assert out["cache_hit"] is True


def test_cache_miss_after_key_change_detected():
    """Operator changed the OSM scope or the OSM PBF — entrypoint's miss
    branch fires. Operator sees this as a 25-30 min phase-1 rebuild."""
    log = (
        "streetGraph.obj cache miss (key changed: 7c43fb73…:transit-focused → "
        "2c8c1d4a…:transit-focused) — rebuilding\n"
        "Phase 1/2 — building street graph (heap=20g) ...\n"
    )
    out = _classify_rebuild_log(log)
    assert out["cache_hit"] is False


def test_cache_empty_first_build_treated_as_miss():
    """First build for a new session: no .key file exists, entrypoint says
    'cache empty'. Same operator-visible meaning as a miss — a slow build."""
    log = (
        "streetGraph.obj cache empty — building from scratch\n"
        "Phase 1/2 — building street graph (heap=20g) ...\n"
    )
    out = _classify_rebuild_log(log)
    assert out["cache_hit"] is False


def test_unknown_when_log_lacks_marker():
    """Pre-v0.1.7 logs, builds that crashed before the cache phase, or 32k
    truncation cutting off the relevant line — all yield None. UI must
    handle None by NOT claiming cache miss when we honestly don't know."""
    out = _classify_rebuild_log(
        "queued at 2026-04-30 …\nBuild started\n(some other unrelated lines)\n"
    )
    assert out["cache_hit"] is None


def test_empty_log_returns_none():
    """Defensive: log can be None or empty for a freshly-enqueued job."""
    assert _classify_rebuild_log(None)["cache_hit"] is None
    assert _classify_rebuild_log("")["cache_hit"] is None


def test_hit_takes_precedence_when_both_strings_appear():
    """If the same log somehow contains both phrases (e.g. a retry within
    the same job), 'hit' wins because it's the line that actually
    determines the outcome of THIS build's phase 1."""
    log = (
        "streetGraph.obj cache miss (key changed) — rebuilding\n"
        "(retry kicked in)\n"
        "streetGraph.obj cache hit (key=…) — copying\n"
        "Phase 1/2 — SKIPPED (using cached streetGraph.obj)\n"
    )
    # The order of the `if` checks in the helper makes "hit" the first
    # branch to fire; pin that. If it ever flips, the operator sees a
    # red "cache miss" pill on a build that actually used the cache.
    assert _classify_rebuild_log(log)["cache_hit"] is True


# ───────────────────────── _snapshot_to_info ───────────────────────────


@dataclass
class _StubSnapshot:
    """Just enough of GraphSnapshot to drive the converter without an ORM."""

    built_at: datetime | None = field(default_factory=lambda: datetime(2026, 5, 4, 14, 30, 12, tzinfo=UTC))
    feed_signature: str = "a3f8d09c0123456789abcdef0123456789abcdef0123456789abcdef01234567"
    is_current: bool = True
    timetable_main_version: str = "2026-W14_2026-W39"
    timetable_update_version: int = 3
    service_period_start: date | None = field(default_factory=lambda: date(2026, 4, 1))
    service_period_end: date | None = field(default_factory=lambda: date(2026, 9, 30))
    source_uploads: list[dict[str, Any]] = field(default_factory=lambda: [
        {"upload_id": "u1", "filename": "sncf.zip", "sha256": "aa11", "kind": "GTFS"},
        {"upload_id": "u2", "filename": "france.osm.pbf", "sha256": "bb22", "kind": "OSM-PBF"},
    ])
    main_version_source: str = "auto"


def test_snapshot_to_info_full_round_trip():
    """Happy path: every field copied through, dates serialised as ISO."""
    info = _snapshot_to_info(_StubSnapshot())
    assert info.is_current is True
    assert info.timetable_main_version == "2026-W14_2026-W39"
    assert info.timetable_update_version == 3
    assert info.service_period_start == "2026-04-01"
    assert info.service_period_end == "2026-09-30"
    assert info.built_at.startswith("2026-05-04T14:30:12")
    assert len(info.source_uploads) == 2
    assert info.source_uploads[0]["filename"] == "sncf.zip"


def test_snapshot_to_info_handles_missing_timestamps():
    """A snapshot row that's somehow missing dates (shouldn't happen post-
    v0.1.20 because the worker writes them, but defensive: we should NOT
    500 the rebuilds endpoint just because one row is malformed)."""
    snap = _StubSnapshot(
        built_at=None,
        service_period_start=None,
        service_period_end=None,
    )
    info = _snapshot_to_info(snap)
    assert info.built_at == ""
    assert info.service_period_start == ""
    assert info.service_period_end == ""


def test_snapshot_to_info_handles_empty_source_uploads():
    """Worker is wired to call record_snapshot() with the session's Upload
    rows. Refresh-from-URL doesn't populate Upload, so a session built
    purely from refreshed providers will have an empty list. Verify the
    converter keeps it as [], not None."""
    info = _snapshot_to_info(_StubSnapshot(source_uploads=[]))
    assert info.source_uploads == []


def test_snapshot_to_info_falls_back_main_version_source():
    """`main_version_source` is NOT NULL in the schema (server_default='auto'),
    but a stubbed-out test object with None should still produce a valid
    response. Defensive default."""
    info = _snapshot_to_info(_StubSnapshot(main_version_source=None))
    assert info.main_version_source == "auto"
