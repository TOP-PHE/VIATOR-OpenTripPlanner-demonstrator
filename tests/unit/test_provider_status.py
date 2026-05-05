"""Unit tests for v0.1.19's per-provider fetch status helper.

`_derive_provider_status` is pure: it takes file metadata + an audit-meta
dict + a `now` timestamp, and returns a `ProviderStatus`. No DB, no FS
walking — just inbox-root path lookups via `tmp_path`. So the four
state-derivation rules can be exercised without TestClient or Postgres.

These tests pin the rules:

  - ok       — file present, age ≤ freshness window
  - stale    — file present, age > freshness window
  - error    — file missing AND latest audit row skipped this provider
  - pending  — file missing AND audit either silent or successful

The "error_hint" partial-failure case (file present from earlier run,
but latest audit attempt skipped this provider) is also pinned — it
matters because operators using the UI need a way to tell apart "I have
old data, refreshing failed" from "this is genuinely fresh".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.api.admin.sessions import _derive_provider_status

# ─────────────────────────── Helpers ────────────────────────────


def _write_gtfs(
    inbox_root: Path, feed_id: str, *, mtime_offset_h: float = 0.0, size_bytes: int = 1024
) -> Path:
    """Plant a fake GTFS zip at the canonical inbox path with a chosen mtime.

    The mtime offset is hours **into the past** from now — `mtime_offset_h=2`
    means "fetched 2 h ago". `size_bytes` controls the file's apparent size.
    """
    target = inbox_root / "gtfs" / f"{feed_id.lower()}.zip"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"x" * size_bytes)
    if mtime_offset_h > 0:
        ts = (datetime.now(UTC) - timedelta(hours=mtime_offset_h)).timestamp()
        import os

        os.utime(target, (ts, ts))
    return target


def _now() -> datetime:
    """Anchor time for tests — captured once so freshness math is deterministic."""
    return datetime.now(UTC)


# ───────────────────────────── ok ───────────────────────────────


def test_ok_when_file_present_and_within_freshness_window(tmp_path: Path):
    """Two-hour-old file in a 24-hour window → state=ok."""
    _write_gtfs(tmp_path, "SNCF", mtime_offset_h=2.0, size_bytes=4096)

    status = _derive_provider_status(
        feed_id="SNCF",
        timetable_format="gtfs",
        inbox_root=tmp_path,
        latest_audit_meta=None,
        now=_now(),
        freshness_hours=24,
    )

    assert status.state == "ok"
    assert status.size_bytes == 4096
    assert status.fetched_at is not None
    assert status.error_hint is None


# ───────────────────────────── stale ────────────────────────────


def test_stale_when_file_present_but_older_than_freshness_window(tmp_path: Path):
    """48-hour-old file in a 24-hour window → state=stale; size + ts still
    surfaced so the operator can decide whether to refresh."""
    _write_gtfs(tmp_path, "SNCF", mtime_offset_h=48.0, size_bytes=2048)

    status = _derive_provider_status(
        feed_id="SNCF",
        timetable_format="gtfs",
        inbox_root=tmp_path,
        latest_audit_meta=None,
        now=_now(),
        freshness_hours=24,
    )

    assert status.state == "stale"
    assert status.size_bytes == 2048
    assert status.fetched_at is not None


# ───────────────────────────── pending ──────────────────────────


def test_pending_when_no_file_and_no_audit_history(tmp_path: Path):
    """Brand-new provider, never refreshed → state=pending."""
    status = _derive_provider_status(
        feed_id="TRENITALIA-FR",
        timetable_format="gtfs",
        inbox_root=tmp_path,
        latest_audit_meta=None,
        now=_now(),
        freshness_hours=24,
    )

    assert status.state == "pending"
    assert status.fetched_at is None
    assert status.size_bytes is None
    assert status.error_hint is None


def test_pending_when_no_file_and_audit_didnt_touch_this_provider(tmp_path: Path):
    """The latest audit row exists but mentions a *different* provider's
    keys — so this one was never attempted. Still pending."""
    status = _derive_provider_status(
        feed_id="TRENITALIA-FR",
        timetable_format="gtfs",
        inbox_root=tmp_path,
        latest_audit_meta={
            "fetched": ["provider[SNCF].timetable(gtfs)"],
            "skipped": [],
        },
        now=_now(),
        freshness_hours=24,
    )

    assert status.state == "pending"


# ───────────────────────────── error ────────────────────────────


def test_error_when_audit_skipped_this_provider_and_no_file(tmp_path: Path):
    """Audit row says the latest refresh tried this provider's task but
    skipped it (e.g. 403, network error) and no inbox file exists.
    State must be `error` so the UI surfaces a red pill."""
    status = _derive_provider_status(
        feed_id="TER-OCC",
        timetable_format="gtfs",
        inbox_root=tmp_path,
        latest_audit_meta={
            "fetched": ["provider[SNCF].timetable(gtfs)"],
            "skipped": ["provider[TER-OCC].timetable(gtfs)"],
        },
        now=_now(),
        freshness_hours=24,
    )

    assert status.state == "error"
    assert status.fetched_at is None
    assert status.error_hint is not None
    assert "Refresh" in status.error_hint  # operator nudge


def test_error_hint_when_file_present_but_latest_audit_skipped(tmp_path: Path):
    """Edge case worth pinning: file is from an earlier successful refresh
    (so state remains ok/stale based on age), BUT the *most recent* attempt
    failed for this provider. We surface that as `error_hint` without
    flipping state to error — operators still have usable data, but the
    pill should hint that something is off."""
    _write_gtfs(tmp_path, "SNCF", mtime_offset_h=1.0)

    status = _derive_provider_status(
        feed_id="SNCF",
        timetable_format="gtfs",
        inbox_root=tmp_path,
        latest_audit_meta={
            "fetched": [],
            "skipped": ["provider[SNCF].timetable(gtfs)"],
        },
        now=_now(),
        freshness_hours=24,
    )

    assert status.state == "ok"  # file is 1h old, well within window
    assert status.error_hint is not None
    assert "previous file" in status.error_hint or "failed" in status.error_hint


# ────────────────────── format-specific paths ───────────────────


def test_gtfs_path_resolves_under_gtfs_subdir(tmp_path: Path):
    """GTFS files live at `<inbox>/gtfs/<feed_id_lower>.zip`."""
    _write_gtfs(tmp_path, "IDFM", mtime_offset_h=0.5)

    status = _derive_provider_status(
        feed_id="IDFM",
        timetable_format="gtfs",
        inbox_root=tmp_path,
        latest_audit_meta=None,
        now=_now(),
    )

    assert status.state == "ok"


def test_netex_nordic_path_resolves_under_netex_subdir(tmp_path: Path):
    """NeTEx-Nordic files live at `<inbox>/netex/<feed_id_lower>.zip`,
    not under gtfs/. Important so we don't false-positive on the wrong path."""
    netex_path = tmp_path / "netex" / "entur.zip"
    netex_path.parent.mkdir(parents=True, exist_ok=True)
    netex_path.write_bytes(b"netex-zip-content")

    status = _derive_provider_status(
        feed_id="ENTUR",
        timetable_format="netex_nordic",
        inbox_root=tmp_path,
        latest_audit_meta=None,
        now=_now(),
    )

    assert status.state == "ok"
    assert status.size_bytes == len(b"netex-zip-content")


def test_unknown_timetable_format_degrades_to_pending(tmp_path: Path):
    """Defensive: an unrecognised format on a saved provider shouldn't 500
    the endpoint. Returning pending is the safe fallback — the operator
    will see `Never fetched` and the save-time validator will surface the
    actual format error elsewhere."""
    status = _derive_provider_status(
        feed_id="WHATEVER",
        timetable_format="netex_fr",  # not in TIMETABLE_FORMAT_DETAILS
        inbox_root=tmp_path,
        latest_audit_meta=None,
        now=_now(),
    )

    assert status.state == "pending"


# ─────────────── audit-meta defensiveness ──────────────────────


@pytest.mark.parametrize(
    "broken_meta",
    [
        {"fetched": [None, 42], "skipped": ["provider[FOO].timetable(gtfs)"]},
        {"fetched": "not-a-list", "skipped": []},  # malformed but shouldn't crash
        {},  # missing keys entirely
    ],
)
def test_does_not_crash_on_malformed_audit_meta(tmp_path: Path, broken_meta):
    """Audit metadata is operator-influenced JSONB; we should never crash
    if a row is malformed. The status falls back to pending (when no file)
    or whatever the file says (when present)."""
    status = _derive_provider_status(
        feed_id="FOO",
        timetable_format="gtfs",
        inbox_root=tmp_path,
        latest_audit_meta=broken_meta,
        now=_now(),
    )
    # We don't assert a specific state — just that no exception was raised
    # and the response is well-formed.
    assert status.feed_id == "FOO"
    assert status.state in ("ok", "stale", "pending", "error")
