"""Unit tests for PR #33 — orphan inbox-file cleanup.

When an operator removes a provider from `sessions.config.sources.providers`
(via the UI's "remove" button or a SQL UPDATE), the corresponding `<feed_id>.zip`
file lingers in `/data/inbox/<sid>/gtfs/`. The OTP entrypoint's
`gtfs/*.zip` glob picks the orphan up at build time and bakes its stale
data into the graph — exactly the bug that surfaced 2026-05-11 with
BrittanyFerries (operator removed it via UI, file stayed, the next build
failed on BrittanyFerries' multi-line stop_desc).

These tests pin the new behaviour:
  1. `expected_provider_filenames` derives the right filenames from a
     v0.1.6 provider config
  2. `sweep_orphaned_provider_files` only renames files NOT in the
     expected set
  3. Sweep is idempotent and lossless (renames, never deletes)
  4. NeTEx subdir is swept symmetrically to gtfs
  5. Files with already-non-`.zip` extensions are ignored (no double-rename)

Helpers live in `app.inbox_sweep` (a leaf module — no DB import) so
these tests run fast and don't need psycopg.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.inbox_sweep import expected_provider_filenames, sweep_orphaned_provider_files

# ────────────────────────── expected_provider_filenames ──────────────────────────


def test_expected_filenames_for_v016_provider_list():
    """The canonical case: a v0.1.6 native shape with two providers,
    both GTFS. They map to `<id_lower>.zip`."""
    config = {
        "sources": {
            "providers": [
                {
                    "id": "SNCF",
                    "label": "SNCF",
                    "country_iso": "FR",
                    "timetable": {"format": "gtfs"},
                },
                {
                    "id": "RENFE",
                    "label": "Renfe",
                    "country_iso": "FR",
                    "timetable": {"format": "gtfs"},
                },
            ]
        }
    }
    assert expected_provider_filenames(config) == {"sncf.zip", "renfe.zip"}


def test_expected_filenames_empty_when_no_providers():
    """No providers configured → no expected filenames. The sweep would
    then rename every existing zip (which is correct: if no providers
    are declared, there's nothing the build is supposed to ingest)."""
    assert expected_provider_filenames({}) == set()
    assert expected_provider_filenames({"sources": {}}) == set()
    assert expected_provider_filenames({"sources": {"providers": []}}) == set()


def test_expected_filenames_returns_empty_on_malformed_config():
    """Defensive: a malformed provider list shouldn't crash the sweep —
    we return empty so nothing gets renamed (rather than aggressively
    quarantining everything because of one bad provider)."""
    # Missing required `id` field — normalize_providers raises ValueError
    config = {"sources": {"providers": [{"label": "Anonymous", "country_iso": "FR"}]}}
    assert expected_provider_filenames(config) == set()


def test_expected_filenames_lowercases_feed_id():
    """The entrypoint scans `gtfs/*.zip` and lifts the stem to feedId by
    upper-casing. So the inbox filenames are lowercase. The sweep's
    expected-set must match that convention or it'd rename valid files."""
    config = {
        "sources": {
            "providers": [
                {
                    "id": "EUROSTARINTERNAT",
                    "label": "Eurostar",
                    "country_iso": "FR",
                    "timetable": {"format": "gtfs"},
                }
            ]
        }
    }
    assert expected_provider_filenames(config) == {"eurostarinternat.zip"}


# ────────────────────────── sweep_orphaned_provider_files ──────────────────────────


def _make_session_inbox(
    tmp_path: Path,
    gtfs_files: list[str],
    netex_files: list[str] | None = None,
) -> Path:
    """Helper: build a realistic session inbox tree with given filenames."""
    inbox = tmp_path / "nap-fr-rail"
    (inbox / "gtfs").mkdir(parents=True)
    (inbox / "netex").mkdir(parents=True)
    (inbox / "osm").mkdir(parents=True)
    for name in gtfs_files:
        (inbox / "gtfs" / name).write_bytes(b"fake")
    for name in netex_files or []:
        (inbox / "netex" / name).write_bytes(b"fake")
    return inbox


def test_sweep_renames_orphans_in_gtfs_subdir(tmp_path):
    """The canonical case: inbox has SNCF + EUROSTAR + a lingering
    BRITTANYFERRIES that was removed from the provider list. Sweep
    renames only BrittanyFerries."""
    inbox = _make_session_inbox(
        tmp_path,
        gtfs_files=["sncf.zip", "eurostarinternat.zip", "brittanyferries.zip"],
    )
    expected = {"sncf.zip", "eurostarinternat.zip"}
    events = sweep_orphaned_provider_files(inbox, expected)

    assert (inbox / "gtfs" / "sncf.zip").exists()
    assert (inbox / "gtfs" / "eurostarinternat.zip").exists()
    assert not (inbox / "gtfs" / "brittanyferries.zip").exists()
    assert (inbox / "gtfs" / "brittanyferries.zip.orphaned").exists()
    assert events == ["gtfs/brittanyferries.zip → brittanyferries.zip.orphaned"]


def test_sweep_is_no_op_when_no_orphans(tmp_path):
    """When every file in the inbox matches the expected set, the sweep
    does nothing and returns an empty events list — important so a clean
    refresh doesn't add noise to the audit log / API response."""
    inbox = _make_session_inbox(tmp_path, gtfs_files=["sncf.zip", "renfe.zip"])
    events = sweep_orphaned_provider_files(inbox, {"sncf.zip", "renfe.zip"})

    assert events == []
    assert (inbox / "gtfs" / "sncf.zip").exists()
    assert (inbox / "gtfs" / "renfe.zip").exists()


def test_sweep_is_idempotent(tmp_path):
    """Running the sweep twice produces the same end state. The first
    run renames `brittanyferries.zip` → `brittanyferries.zip.orphaned`;
    the second run sees only the .orphaned file (which doesn't end in
    just `.zip`) and ignores it."""
    inbox = _make_session_inbox(tmp_path, gtfs_files=["sncf.zip", "brittanyferries.zip"])
    expected = {"sncf.zip"}

    events_first = sweep_orphaned_provider_files(inbox, expected)
    events_second = sweep_orphaned_provider_files(inbox, expected)

    assert events_first == ["gtfs/brittanyferries.zip → brittanyferries.zip.orphaned"]
    assert events_second == []
    # State is stable
    assert (inbox / "gtfs" / "brittanyferries.zip.orphaned").exists()


def test_sweep_ignores_non_zip_files(tmp_path):
    """README.md, .zip.old, .zip.broken, and other non-`.zip` files
    must not get caught by the sweep — they're either documentation
    or already-rotated state."""
    inbox = _make_session_inbox(
        tmp_path,
        gtfs_files=[
            "sncf.zip",  # active, in expected
            "trenitalia.zip.broken",  # parked by operator earlier
            "renfe.zip.old",  # legacy rotation
            "brittanyferries.zip.orphaned",  # already-quarantined orphan
        ],
    )
    # README at the gtfs subdir level (defensive — operators sometimes
    # drop notes in there)
    (inbox / "gtfs" / "README.txt").write_text("notes")

    events = sweep_orphaned_provider_files(inbox, {"sncf.zip"})

    # Nothing should be renamed — only `<x>.zip` files are candidates
    assert events == []
    assert (inbox / "gtfs" / "sncf.zip").exists()
    assert (inbox / "gtfs" / "trenitalia.zip.broken").exists()
    assert (inbox / "gtfs" / "renfe.zip.old").exists()
    assert (inbox / "gtfs" / "brittanyferries.zip.orphaned").exists()
    assert (inbox / "gtfs" / "README.txt").exists()


def test_sweep_handles_netex_subdir_symmetrically(tmp_path):
    """The entrypoint scans both `gtfs/*.zip` and `netex/*.zip`. Orphans
    in the netex subdir must be quarantined too, otherwise the bug just
    moves there."""
    inbox = _make_session_inbox(
        tmp_path,
        gtfs_files=["sncf.zip"],
        netex_files=["sncf.zip", "removed_netex_provider.zip"],
    )

    events = sweep_orphaned_provider_files(
        inbox,
        # Only SNCF in expected; the netex provider was removed
        {"sncf.zip"},
    )

    # gtfs/sncf.zip survives because it matches expected
    assert (inbox / "gtfs" / "sncf.zip").exists()
    # netex/sncf.zip ALSO survives — same filename matches expected,
    # regardless of which subdir it lives in (matches the entrypoint's
    # scan logic exactly: filename equality, not subdir-qualified)
    assert (inbox / "netex" / "sncf.zip").exists()
    # netex/removed_netex_provider.zip gets quarantined
    assert (inbox / "netex" / "removed_netex_provider.zip.orphaned").exists()
    assert events == ["netex/removed_netex_provider.zip → removed_netex_provider.zip.orphaned"]


def test_sweep_handles_missing_subdirs_gracefully(tmp_path):
    """Fresh sessions may not have a `netex/` subdir yet (only gtfs).
    Sweep must not crash — just iterate what's actually there."""
    inbox = tmp_path / "fresh-session"
    (inbox / "gtfs").mkdir(parents=True)
    (inbox / "gtfs" / "orphan.zip").write_bytes(b"fake")
    # Deliberately no netex/ subdir

    events = sweep_orphaned_provider_files(inbox, set())

    assert events == ["gtfs/orphan.zip → orphan.zip.orphaned"]
    assert (inbox / "gtfs" / "orphan.zip.orphaned").exists()


def test_sweep_handles_completely_missing_inbox(tmp_path):
    """Operator created a session but never staged any files. Sweep
    on a non-existent inbox dir is a no-op."""
    events = sweep_orphaned_provider_files(tmp_path / "no-such-session", set())
    assert events == []


def test_sweep_continues_on_individual_rename_failure(monkeypatch, tmp_path):
    """One stuck file (locked, perm-denied, race) shouldn't abort the
    whole sweep — log and continue. Tested by patching Path.rename
    to throw OSError on a specific named file (NOT on an Nth-call basis,
    because Path.iterdir() order differs between OSes — alphabetical
    on Windows NTFS but inode-order on Linux ext4)."""
    inbox = _make_session_inbox(
        tmp_path,
        gtfs_files=["orphan_a.zip", "orphan_b.zip", "orphan_c.zip"],
    )

    # Fail specifically when orphan_b.zip is the source — works regardless
    # of iteration order.
    real_rename = Path.rename

    def flaky_rename(self, target):
        if self.name == "orphan_b.zip":
            raise OSError("simulated EACCES on orphan_b")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", flaky_rename)

    events = sweep_orphaned_provider_files(inbox, set())

    # 2 of 3 files renamed; the failing one is reported via the log,
    # not the events list (events tracks successes only)
    assert len(events) == 2
    # Files A and C got renamed, B stayed unchanged
    assert (inbox / "gtfs" / "orphan_a.zip.orphaned").exists()
    assert (inbox / "gtfs" / "orphan_b.zip").exists()
    assert not (inbox / "gtfs" / "orphan_b.zip.orphaned").exists()
    assert (inbox / "gtfs" / "orphan_c.zip.orphaned").exists()


# ────────────────────────── end-to-end via expected_filenames ──────────────────────────


def test_full_workflow_brittanyferries_2026_05_11(tmp_path):
    """End-to-end re-enactment of the 2026-05-11 incident:
    1. Operator has 8 active providers including BrittanyFerries
    2. They remove BrittanyFerries from the provider list via the UI
    3. Refresh providers re-fetches the remaining 7 (BrittanyFerries' file lingers)
    4. The orphan sweep should quarantine BrittanyFerries' file

    Pinning this scenario as a regression test means a future refactor
    that breaks the sweep can't silently re-introduce the bug."""
    inbox = _make_session_inbox(
        tmp_path,
        gtfs_files=[
            "sncf.zip",
            "sncf-2.zip",
            "renfe.zip",
            "eurostarinternat.zip",
            "rgionhauts-de-fr.zip",
            "rgionprovence-al.zip",
            "rgionbretagne.zip",
            "brittanyferries.zip",  # ← removed from providers, file lingers
        ],
    )
    # Provider list AFTER the operator removed BrittanyFerries
    config = {
        "sources": {
            "providers": [
                {
                    "id": pid,
                    "label": pid,
                    "country_iso": "FR",
                    "timetable": {"format": "gtfs"},
                }
                for pid in [
                    "SNCF",
                    "SNCF-2",
                    "RENFE",
                    "EUROSTARINTERNAT",
                    "RGIONHAUTS-DE-FR",
                    "RGIONPROVENCE-AL",
                    "RGIONBRETAGNE",
                ]
            ]
        }
    }

    expected = expected_provider_filenames(config)
    events = sweep_orphaned_provider_files(inbox, expected)

    assert events == ["gtfs/brittanyferries.zip → brittanyferries.zip.orphaned"]
    # 7 active providers' files survive
    for name in ("sncf.zip", "sncf-2.zip", "renfe.zip", "eurostarinternat.zip"):
        assert (inbox / "gtfs" / name).exists()
    # BrittanyFerries quarantined
    assert (inbox / "gtfs" / "brittanyferries.zip.orphaned").exists()
    assert not (inbox / "gtfs" / "brittanyferries.zip").exists()


# Silence unused-fixture warning if pytest evolves
_ = pytest
