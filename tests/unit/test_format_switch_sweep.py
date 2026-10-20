"""Issue #381: a provider's format switch quarantines its old-format file.

GTFS and NeTEx share the slot name `<id>.zip`; only the subdir differs. The
sweep compares the `(subdir, filename)` slot, so `netex/<id>.zip` is renamed
`*.zip.orphaned` once the provider is GTFS (and the reverse): at the refresh
end, against the config stored at that moment, and after a config save's
commit. A derived (cross-border) provider's file is GTFS whatever format it
declares. Ids are invented.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import BackgroundTasks, HTTPException

from app import graph_snapshots, inbox_sweep, ingestion, worker
from app.api.admin import sessions as api
from app.api.admin.sessions import SessionPatch
from app.settings import settings

SID = "zz-session"
REQUEST = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))
ACTOR = SimpleNamespace(id=uuid.uuid4(), username="zz@example.invalid", role="platform_admin")
QUARANTINED = "session.format_switch.quarantined"


def _provider(pid: str, fmt: str) -> dict[str, Any]:
    return {
        "id": pid,
        "label": pid,
        "timetable": {"format": fmt, "url": f"https://zz.example.invalid/{pid.lower()}.zip"},
    }


def _derived(pid: str, fmt: str) -> dict[str, Any]:
    return {
        "id": pid,
        "label": pid,
        "timetable": {
            "format": fmt,
            "source": "cross_border_filter",
            "derived_from": {"session_id": "zz-national", "provider_id": "ZZ-NAT"},
        },
    }


def _config(*providers: tuple[str, str], derived: tuple[str, str] | None = None) -> dict[str, Any]:
    rows = [_provider(pid, fmt) for pid, fmt in providers]
    if derived is not None:
        rows.append(_derived(*derived))
    return {"sources": {"providers": rows}}


def _inbox(tmp_path: Path, files: dict[str, bytes]) -> Path:
    """`files` maps `subdir/name` to content, under tmp/<SID>."""
    root = tmp_path / SID
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return root


def _names(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


# ───────────────────────── the slot mapping ─────────────────────────


@pytest.mark.parametrize(
    ("fmt", "subdir"),
    [("gtfs", "gtfs"), ("netex_nordic", "netex"), ("netex_epip", "netex")],
)
def test_the_slot_is_where_place_puts_the_timetable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fmt: str, subdir: str
) -> None:
    """`staged_slot_for_format` names the file `place` writes for the kind a
    provider of that format is fetched as (the refresh task's kind)."""
    monkeypatch.setattr(settings, "inbox_dir", tmp_path)
    src = tmp_path / "zz-src.zip"
    src.write_bytes(b"zz")
    slot = ingestion.staged_slot_for_format("ZZ-A", fmt)
    ingestion.place(
        src,
        ingestion.TIMETABLE_FORMAT_DETAILS[fmt]["kind"],
        session_id=SID,
        staged_filename=slot[1],
    )
    assert slot == (subdir, "zz-a.zip")
    assert _names(tmp_path / SID) == {f"{subdir}/zz-a.zip"}


def test_an_unknown_format_has_no_slot() -> None:
    with pytest.raises(ValueError, match="Unknown timetable format"):
        ingestion.staged_slot_for_format("ZZ-A", "zz_format")


def test_expected_slots_carry_the_subdir() -> None:
    config = _config(("ZZ-A", "gtfs"), ("ZZ-B", "netex_epip"), ("ZZ-C", "netex_nordic"))
    assert inbox_sweep.expected_provider_filenames(config) == {
        ("gtfs", "zz-a.zip"),
        ("netex", "zz-b.zip"),
        ("netex", "zz-c.zip"),
    }


@pytest.mark.parametrize("declared", ["gtfs", "netex_epip", "netex_nordic"])
def test_a_derived_provider_slot_is_gtfs_whatever_it_declares(declared: str) -> None:
    """The cross-border filter writes GTFS; a stored derived provider that
    declares NeTEx still loads (reads never refuse it)."""
    config = _config(derived=("ZZ-X", declared))
    (provider,) = ingestion.normalize_providers(config)
    assert ingestion.provider_timetable_format(provider) == "gtfs"
    assert inbox_sweep.expected_provider_filenames(config) == {("gtfs", "zz-x.zip")}


# ───────────────────────── the sweep ─────────────────────────


@pytest.mark.parametrize(
    ("now_fmt", "kept", "orphaned"),
    [
        ("gtfs", "gtfs/zz-a.zip", "netex/zz-a.zip"),  # NeTEx → GTFS
        ("netex_epip", "netex/zz-a.zip", "gtfs/zz-a.zip"),  # GTFS → NeTEx
    ],
)
def test_the_sweep_renames_the_old_format_file(
    tmp_path: Path, now_fmt: str, kept: str, orphaned: str
) -> None:
    root = _inbox(tmp_path, {"gtfs/zz-a.zip": b"g", "netex/zz-a.zip": b"n", "gtfs/zz-b.zip": b"b"})
    expected = inbox_sweep.expected_provider_filenames(_config(("ZZ-A", now_fmt), ("ZZ-B", "gtfs")))
    events = inbox_sweep.sweep_orphaned_provider_files(root, expected)
    assert _names(root) == {kept, "gtfs/zz-b.zip", f"{orphaned}.orphaned"}
    assert events == [f"{orphaned} → zz-a.zip.orphaned"]


def test_a_malformed_provider_list_renames_nothing(tmp_path: Path) -> None:
    """A lower-case id is refused by the id rule, so the list cannot be read
    (here beside its upper-case twin): the sweep renames nothing (it renamed
    every file before: an empty expected set), nor does a save."""
    files = {"gtfs/zz-a.zip": b"g", "netex/zz-a.zip": b"n", "gtfs/zz-orphan.zip": b"o"}
    root = _inbox(tmp_path, files)
    config = _config(("ZZ-A", "gtfs"), ("zz-a", "gtfs"))
    expected = inbox_sweep.expected_provider_filenames(config)
    assert expected is None
    assert inbox_sweep.sweep_orphaned_provider_files(root, expected) == []
    plan = inbox_sweep.plan_save_quarantine(_config(("ZZ-A", "netex_epip")), config)
    assert inbox_sweep.apply_save_quarantine(root, plan) == []
    assert _names(root) == set(files)


def test_a_file_named_in_another_case_is_an_orphan_as_before(tmp_path: Path) -> None:
    """Slot names are the lower-cased id: an upper-case file is not a slot."""
    root = _inbox(tmp_path, {"gtfs/zz-a.zip": b"g", "gtfs/ZZ-A.zip": b"u"})
    expected = inbox_sweep.expected_provider_filenames(_config(("ZZ-A", "gtfs")))
    assert inbox_sweep.sweep_orphaned_provider_files(root, expected) == [
        "gtfs/ZZ-A.zip → ZZ-A.zip.orphaned"
    ]


def test_only_limits_the_sweep_to_the_given_slots(tmp_path: Path) -> None:
    root = _inbox(tmp_path, {"netex/zz-a.zip": b"n", "gtfs/zz-gone.zip": b"o"})
    events = inbox_sweep.sweep_orphaned_provider_files(
        root, {("gtfs", "zz-a.zip")}, only={("netex", "zz-a.zip")}
    )
    assert events == ["netex/zz-a.zip → zz-a.zip.orphaned"]
    assert "gtfs/zz-gone.zip" in _names(root)


def test_an_earlier_orphan_is_never_overwritten(tmp_path: Path) -> None:
    """N1: a taken `.orphaned` name gets a UTC time stamp; both copies stay."""
    root = _inbox(tmp_path, {"netex/zz-a.zip": b"new", "netex/zz-a.zip.orphaned": b"old"})
    (event,) = inbox_sweep.sweep_orphaned_provider_files(root, {("gtfs", "zz-a.zip")})
    (stamped,) = _names(root) - {"netex/zz-a.zip.orphaned"}
    assert stamped.startswith("netex/zz-a.zip.2")
    assert stamped.endswith("Z.orphaned")
    assert event == f"netex/zz-a.zip → {Path(stamped).name}"
    assert (root / "netex" / "zz-a.zip.orphaned").read_bytes() == b"old"
    assert (root / stamped).read_bytes() == b"new"
    # A second clash within the same second gets a counter.
    (root / "netex" / "zz-a.zip").write_bytes(b"newer")
    taken = root / "netex" / "zz-a.zip"
    target = inbox_sweep._orphan_target(taken)
    assert target.name.endswith(".orphaned")
    assert not target.exists()


# ───────────────────────── the save plan ─────────────────────────


@pytest.mark.parametrize(
    ("old_fmt", "new_fmt", "orphaned"),
    [
        ("netex_nordic", "gtfs", "netex/zz-a.zip"),
        ("gtfs", "netex_epip", "gtfs/zz-a.zip"),
    ],
)
def test_the_save_plan_quarantines_the_other_format_file(
    tmp_path: Path, old_fmt: str, new_fmt: str, orphaned: str
) -> None:
    root = _inbox(
        tmp_path,
        {
            "gtfs/zz-a.zip": b"g",
            "netex/zz-a.zip": b"n",
            "gtfs/zz-b.zip": b"b",
            "gtfs/zz-c.zip": b"c",
        },
    )
    old = _config(("ZZ-A", old_fmt), ("ZZ-B", "gtfs"), ("ZZ-C", "gtfs"))
    new = _config(("ZZ-A", new_fmt), ("ZZ-B", "gtfs"))  # ZZ-C removed
    plan = inbox_sweep.plan_save_quarantine(old, new)
    other = "netex" if new_fmt == "gtfs" else "gtfs"
    assert plan.slots == {(other, "zz-a.zip"), ("netex", "zz-b.zip")}
    assert plan.switched == {("gtfs" if other == "netex" else "netex", "zz-a.zip")}
    assert _names(root) == {"gtfs/zz-a.zip", "netex/zz-a.zip", "gtfs/zz-b.zip", "gtfs/zz-c.zip"}
    events = inbox_sweep.apply_save_quarantine(root, plan)
    assert events == [f"{orphaned} → zz-a.zip.orphaned"]
    # The removed provider's file waits for the next refresh, as before.
    assert "gtfs/zz-c.zip" in _names(root)
    assert f"{orphaned}.orphaned" in _names(root)


def test_the_save_plan_does_not_need_the_stored_provider(tmp_path: Path) -> None:
    """S2: a provider removed, then added back in the other format (or an id
    renamed onto an old file name), finds its stale file quarantined."""
    root = _inbox(tmp_path, {"netex/zz-a.zip": b"stale", "gtfs/zz-a.zip": b"g"})
    plan = inbox_sweep.plan_save_quarantine(_config(("ZZ-B", "gtfs")), _config(("ZZ-A", "gtfs")))
    assert inbox_sweep.apply_save_quarantine(root, plan) == ["netex/zz-a.zip → zz-a.zip.orphaned"]
    assert _names(root) == {"gtfs/zz-a.zip", "netex/zz-a.zip.orphaned"}


def test_an_unreadable_stored_config_still_plans_the_saved_one(tmp_path: Path) -> None:
    """The renames depend on the saved config only; no switch is reported."""
    root = _inbox(tmp_path, {"netex/zz-a.zip": b"stale", "gtfs/zz-a.zip": b"g"})
    plan = inbox_sweep.plan_save_quarantine({"sources": "zz-not-a-list"}, _config(("ZZ-A", "gtfs")))
    assert plan.switched == frozenset()
    assert inbox_sweep.apply_save_quarantine(root, plan) == ["netex/zz-a.zip → zz-a.zip.orphaned"]


def test_an_added_provider_is_not_a_switch() -> None:
    """Only a file name already stored, now in another subdir, is a switch
    (the WARNING about a quarantined copy speaks of switching back)."""
    plan = inbox_sweep.plan_save_quarantine(
        _config(("ZZ-B", "netex_epip")), _config(("ZZ-A", "gtfs"), ("ZZ-B", "netex_epip"))
    )
    assert plan.switched == frozenset()
    assert plan.slots == {("netex", "zz-a.zip"), ("gtfs", "zz-b.zip")}


def test_netex_profiles_share_a_subdir_so_no_switch(tmp_path: Path) -> None:
    root = _inbox(tmp_path, {"netex/zz-a.zip": b"n"})
    old, new = _config(("ZZ-A", "netex_nordic")), _config(("ZZ-A", "netex_epip"))
    plan = inbox_sweep.plan_save_quarantine(old, new)
    assert plan.switched == frozenset()
    assert inbox_sweep.apply_save_quarantine(root, plan) == []
    assert _names(root) == {"netex/zz-a.zip"}


def test_a_switch_back_onto_a_quarantined_copy_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """S1: a stale page switches back to a slot whose file was quarantined;
    the copy is not restored, one WARNING says what to do."""
    root = _inbox(tmp_path, {"netex/zz-a.zip": b"n", "gtfs/zz-a.zip.orphaned": b"g"})
    plan = inbox_sweep.plan_save_quarantine(
        _config(("ZZ-A", "netex_epip")), _config(("ZZ-A", "gtfs"))
    )
    assert plan.switched == {("gtfs", "zz-a.zip")}
    with caplog.at_level(logging.WARNING, logger="app.inbox_sweep"):
        inbox_sweep.apply_save_quarantine(root, plan)
    (record,) = [r for r in caplog.records if "switch_target_quarantined" in r.getMessage()]
    assert "gtfs/zz-a.zip.orphaned" in record.getMessage()
    assert "gtfs/zz-a.zip" not in _names(root)  # never restored automatically


def test_a_switch_onto_a_live_file_does_not_warn(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = _inbox(tmp_path, {"gtfs/zz-a.zip": b"g", "gtfs/zz-a.zip.orphaned": b"older"})
    plan = inbox_sweep.plan_save_quarantine(
        _config(("ZZ-A", "netex_epip")), _config(("ZZ-A", "gtfs"))
    )
    with caplog.at_level(logging.WARNING, logger="app.inbox_sweep"):
        inbox_sweep.apply_save_quarantine(root, plan)
    assert not [r for r in caplog.records if "switch_target_quarantined" in r.getMessage()]


# ───────────────────────── quarantine at save (patch_session) ─────────────────────────


class _Db:
    def __init__(self, row: Any, *, commit_error: Exception | None = None) -> None:
        self.row = row
        self.commits = 0
        self.rollbacks = 0
        self.locked: bool | None = None
        self.commit_error = commit_error

    def get(self, _model: object, sid: str, *, with_for_update: bool = False) -> Any:
        self.locked = with_for_update
        return self.row if sid == self.row.id else None

    def commit(self) -> None:
        if self.commit_error is not None:
            raise self.commit_error
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _row(config: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        id=SID,
        name="ZZ session",
        category="NAP",
        state="populated",
        engine="motis",
        config=config,
        include_in_fanout=False,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        archived_at=None,
    )


def _save(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, row: Any, body: SessionPatch
) -> tuple[list[dict[str, Any]], _Db]:
    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr(settings, "inbox_dir", tmp_path)
    monkeypatch.setattr(api.audit, "record", lambda _db, **kw: recorded.append(kw))
    db = _Db(row)
    api.patch_session(
        SID,
        body,
        REQUEST,  # type: ignore[arg-type]
        SimpleNamespace(headers={}),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
        ACTOR,  # type: ignore[arg-type]
    )
    assert db.commits >= 1
    assert db.locked is True  # S1: saves of one session run one after the other
    return recorded, db


@pytest.mark.parametrize(
    ("old_fmt", "new_fmt", "kept", "orphaned"),
    [
        ("netex_epip", "gtfs", "gtfs/zz-a.zip", "netex/zz-a.zip"),
        ("gtfs", "netex_nordic", "netex/zz-a.zip", "gtfs/zz-a.zip"),
    ],
)
def test_a_save_that_switches_the_format_quarantines_the_old_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    old_fmt: str,
    new_fmt: str,
    kept: str,
    orphaned: str,
) -> None:
    root = _inbox(tmp_path, {"gtfs/zz-a.zip": b"g", "netex/zz-a.zip": b"n", "gtfs/zz-b.zip": b"b"})
    row = _row(_config(("ZZ-A", old_fmt), ("ZZ-B", "gtfs")))
    body = SessionPatch(config=_config(("ZZ-A", new_fmt), ("ZZ-B", "gtfs")))
    (saved, quarantined), db = _save(monkeypatch, tmp_path, row, body)
    assert saved["action"] == "session.updated"
    assert "orphaned" not in saved["metadata"]
    assert quarantined["action"] == QUARANTINED
    assert quarantined["metadata"] == {"orphaned": [f"{orphaned} → zz-a.zip.orphaned"]}
    assert db.commits == 2  # the save, then its follow-up audit row
    assert _names(root) == {kept, "gtfs/zz-b.zip", f"{orphaned}.orphaned"}


def test_a_save_without_switch_renames_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    files = {"gtfs/zz-a.zip": b"g", "netex/zz-b.zip": b"n", "gtfs/zz-c.zip": b"c"}
    root = _inbox(tmp_path, files)
    row = _row(_config(("ZZ-A", "gtfs"), ("ZZ-B", "netex_epip"), ("ZZ-C", "gtfs")))
    new = _config(("ZZ-A", "gtfs"), ("ZZ-B", "netex_epip"))
    new["sources"]["providers"][0]["label"] = "ZZ renamed"
    (saved,), db = _save(monkeypatch, tmp_path, row, SessionPatch(config=new))
    assert saved["action"] == "session.updated"
    assert db.commits == 1
    assert _names(root) == set(files)


def test_a_rename_only_save_touches_no_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    files = {"netex/zz-a.zip": b"n", "gtfs/zz-a.zip": b"g"}
    root = _inbox(tmp_path, files)
    row = _row(_config(("ZZ-A", "gtfs")))
    (saved,), _ = _save(monkeypatch, tmp_path, row, SessionPatch(name="ZZ other"))
    assert saved["metadata"] == {"changes": {"name": {"from": "ZZ session", "to": "ZZ other"}}}
    assert _names(root) == set(files)


@pytest.mark.parametrize(
    "body",
    [
        # The new provider list fails validation.
        SessionPatch(config=_config(("ZZ-A", "gtfs"), ("zz-bad", "gtfs"))),
        # The config is valid, a later field of the same save is not.
        SessionPatch(config=_config(("ZZ-A", "gtfs")), state="zz-no-such-state"),
        # A new derived provider declared NeTEx.
        SessionPatch(config=_config(("ZZ-A", "gtfs"), derived=("ZZ-X", "netex_epip"))),
    ],
)
def test_a_refused_save_renames_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, body: SessionPatch
) -> None:
    files = {"gtfs/zz-a.zip": b"g", "netex/zz-a.zip": b"n"}
    root = _inbox(tmp_path, files)
    monkeypatch.setattr(settings, "inbox_dir", tmp_path)
    row = _row(_config(("ZZ-A", "netex_epip")))
    db = _Db(row)
    answer = SimpleNamespace(headers={})
    with pytest.raises(HTTPException) as refused:
        api.patch_session(SID, body, REQUEST, answer, db, ACTOR)  # type: ignore[arg-type]
    assert refused.value.status_code == 400
    assert db.commits == 0
    assert _names(root) == set(files)


def test_a_save_whose_commit_fails_renames_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """M2: the renames come after the commit, never before it."""
    files = {"gtfs/zz-a.zip": b"g", "netex/zz-a.zip": b"n"}
    root = _inbox(tmp_path, files)
    monkeypatch.setattr(settings, "inbox_dir", tmp_path)
    monkeypatch.setattr(api.audit, "record", lambda _db, **_kw: None)
    db = _Db(_row(_config(("ZZ-A", "netex_epip"))), commit_error=RuntimeError("zz commit"))
    body = SessionPatch(config=_config(("ZZ-A", "gtfs")))
    answer = SimpleNamespace(headers={})
    with pytest.raises(RuntimeError, match="zz commit"):
        api.patch_session(SID, body, REQUEST, answer, db, ACTOR)  # type: ignore[arg-type]
    assert _names(root) == set(files)


def test_a_lost_quarantine_audit_row_leaves_the_files_right(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = _inbox(tmp_path, {"gtfs/zz-a.zip": b"g", "netex/zz-a.zip": b"n"})
    monkeypatch.setattr(settings, "inbox_dir", tmp_path)

    def record(_db: Any, **kw: Any) -> None:
        if kw["action"] == QUARANTINED:
            raise RuntimeError("zz audit")

    monkeypatch.setattr(api.audit, "record", record)
    db = _Db(_row(_config(("ZZ-A", "netex_epip"))))
    body = SessionPatch(config=_config(("ZZ-A", "gtfs")))
    with caplog.at_level(logging.WARNING, logger=api.log.name):
        api.patch_session(SID, body, REQUEST, SimpleNamespace(headers={}), db, ACTOR)  # type: ignore[arg-type]
    assert db.commits == 1
    assert db.rollbacks == 1
    assert "session.quarantine_audit_failed type=RuntimeError" in caplog.text
    assert _names(root) == {"gtfs/zz-a.zip", "netex/zz-a.zip.orphaned"}


def test_a_stored_derived_netex_provider_keeps_its_gtfs_file_at_save(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """M1: a derived provider stored with a NeTEx format stays savable, and
    its GTFS file (the filter's output) is kept; a stale netex/ twin goes."""
    root = _inbox(tmp_path, {"gtfs/zz-x.zip": b"xb", "netex/zz-x.zip": b"stale"})
    row = _row(_config(("ZZ-A", "gtfs"), derived=("ZZ-X", "netex_epip")))
    new = _config(("ZZ-A", "gtfs"), derived=("ZZ-X", "netex_epip"))
    new["sources"]["providers"][0]["label"] = "ZZ renamed"
    records, _ = _save(monkeypatch, tmp_path, row, SessionPatch(config=new))
    assert [r["action"] for r in records] == ["session.updated", QUARANTINED]
    assert _names(root) == {"gtfs/zz-x.zip", "netex/zz-x.zip.orphaned"}


@pytest.mark.parametrize("stored_fmt", [None, "gtfs", "netex_nordic"])
def test_a_save_refuses_a_new_non_gtfs_derived_format(
    monkeypatch: pytest.MonkeyPatch, stored_fmt: str | None
) -> None:
    stored = _config(("ZZ-A", "gtfs"))
    if stored_fmt is not None:
        stored = _config(("ZZ-A", "gtfs"), derived=("ZZ-X", stored_fmt))
    db = _Db(_row(stored))
    body = SessionPatch(config=_config(("ZZ-A", "gtfs"), derived=("ZZ-X", "netex_epip")))
    answer = SimpleNamespace(headers={})
    with pytest.raises(HTTPException) as refused:
        api.patch_session(SID, body, REQUEST, answer, db, ACTOR)  # type: ignore[arg-type]
    assert refused.value.status_code == 400
    assert "cross-border filter (derived) provider is GTFS" in str(refused.value.detail)


def test_the_derived_format_check_survives_an_unreadable_stored_config() -> None:
    providers = ingestion.normalize_providers(_config(derived=("ZZ-X", "gtfs")))
    api._refuse_new_derived_formats(providers, {"sources": "zz-not-a-list"})


# ───────────────────────── the refresh end ─────────────────────────


@pytest.fixture
def no_progress_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """`refresh_sources` claims a progress record in the database (issue
    #359); these tests have no database."""
    from app import refresh_progress

    def claim(sid: str, scope: str, total: int, **_k: Any) -> refresh_progress.RefreshRun:
        detail = refresh_progress._initial_detail(total, None)
        return refresh_progress.RefreshRun(sid, scope, uuid.uuid4(), detail)

    monkeypatch.setattr(refresh_progress, "claim", claim)
    monkeypatch.setattr(refresh_progress.RefreshRun, "_execute", lambda _self, _stmt, **_k: None)


async def _refresh(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    start_config: dict[str, Any],
    during: Any = None,
) -> tuple[Any, list[dict[str, Any]]]:
    """Run `refresh_sources` with nothing to download. `during(stored)` runs
    while the refresh is under way (a save or an upload meanwhile); the row
    lock's re-read (`db.refresh`) then loads `stored["config"]`."""
    monkeypatch.setattr(settings, "inbox_dir", tmp_path)
    recorded: list[dict[str, Any]] = []
    stored = {"config": start_config}

    async def gather(*_a: Any, **_k: Any) -> tuple[list[Any], ...]:
        if during is not None:
            during(stored)
        return [], [], []

    def relock(s: Any, **_k: Any) -> None:
        s.config = stored["config"]

    monkeypatch.setattr(api, "_gather_refresh_outcomes", gather)
    monkeypatch.setattr(api.audit, "record", lambda _db, **kw: recorded.append(kw))
    monkeypatch.setattr(api.station_extract_upload, "after_refresh", lambda *_a, **_k: None)
    row = _row(start_config)
    db = SimpleNamespace(get=lambda _model, _sid: row, commit=lambda: None, refresh=relock)
    answer = await api.refresh_sources(
        SID,
        REQUEST,  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
        ACTOR,  # type: ignore[arg-type]
        BackgroundTasks(),
    )
    return answer, recorded


@pytest.mark.usefixtures("no_progress_record")
@pytest.mark.parametrize(
    ("now_fmt", "orphaned"), [("gtfs", "netex/zz-a.zip"), ("netex_epip", "gtfs/zz-a.zip")]
)
async def test_the_refresh_end_quarantines_the_old_format_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, now_fmt: str, orphaned: str
) -> None:
    root = _inbox(tmp_path, {"gtfs/zz-a.zip": b"g", "netex/zz-a.zip": b"n"})
    answer, recorded = await _refresh(monkeypatch, tmp_path, _config(("ZZ-A", now_fmt)))
    assert answer.orphaned == [f"{orphaned} → zz-a.zip.orphaned"]
    (event,) = recorded
    assert event["metadata"]["orphaned"] == answer.orphaned
    assert f"{orphaned}.orphaned" in _names(root)


@pytest.mark.usefixtures("no_progress_record")
async def test_a_switch_saved_during_the_refresh_keeps_its_new_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """M3: the refresh began with ZZ-A in NeTEx; meanwhile a save switched it
    to GTFS (quarantining netex/) and the new GTFS file was uploaded. The end
    sweep reads the stored config under the lock: the new file stays."""
    root = _inbox(tmp_path, {"netex/zz-a.zip": b"n"})

    def switch_and_upload(stored: dict[str, Any]) -> None:
        stored["config"] = _config(("ZZ-A", "gtfs"))
        (root / "netex" / "zz-a.zip").rename(root / "netex" / "zz-a.zip.orphaned")
        (root / "gtfs").mkdir()
        (root / "gtfs" / "zz-a.zip").write_bytes(b"new")

    answer, _ = await _refresh(
        monkeypatch, tmp_path, _config(("ZZ-A", "netex_epip")), switch_and_upload
    )
    assert answer.orphaned == []
    assert _names(root) == {"gtfs/zz-a.zip", "netex/zz-a.zip.orphaned"}


@pytest.mark.usefixtures("no_progress_record")
async def test_a_stale_file_downloaded_after_a_switch_is_quarantined(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """M3, the reverse: the refresh, started in NeTEx, stores netex/zz-a.zip
    again after a save switched ZZ-A to GTFS; the end sweep renames it."""
    root = _inbox(tmp_path, {"gtfs/zz-a.zip": b"g"})

    def switch_then_download_stale(stored: dict[str, Any]) -> None:
        stored["config"] = _config(("ZZ-A", "gtfs"))
        (root / "netex").mkdir()
        (root / "netex" / "zz-a.zip").write_bytes(b"stale")

    answer, _ = await _refresh(
        monkeypatch, tmp_path, _config(("ZZ-A", "netex_epip")), switch_then_download_stale
    )
    assert answer.orphaned == ["netex/zz-a.zip → zz-a.zip.orphaned"]
    assert _names(root) == {"gtfs/zz-a.zip", "netex/zz-a.zip.orphaned"}


@pytest.mark.usefixtures("no_progress_record")
async def test_a_derived_netex_provider_keeps_its_gtfs_file_at_refresh(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """M1: the regression — a derived provider declared NeTEx had its GTFS
    output renamed at every refresh."""
    root = _inbox(tmp_path, {"gtfs/zz-x.zip": b"xb"})
    config = _config(("ZZ-A", "gtfs"), derived=("ZZ-X", "netex_nordic"))
    monkeypatch.setattr(api, "_derived_count", lambda _c: 0)
    answer, _ = await _refresh(monkeypatch, tmp_path, config)
    assert answer.orphaned == []
    assert _names(root) == {"gtfs/zz-x.zip"}


# ───────────────────────── derived providers elsewhere ─────────────────────────


def test_the_feed_status_reads_a_derived_provider_gtfs_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _inbox(tmp_path, {"gtfs/zz-x.zip": b"xb"})
    monkeypatch.setattr(settings, "inbox_dir", tmp_path)
    row = _row(_config(derived=("ZZ-X", "netex_epip")))
    nothing = SimpleNamespace(scalar_one_or_none=lambda: None, all=list)
    db = SimpleNamespace(get=lambda _model, _sid: row, execute=lambda _stmt: nothing)
    status = api.get_providers_status(SID, db, ACTOR)  # type: ignore[arg-type]
    assert status["ZZ-X"].state == "ok"
    assert status["ZZ-X"].size_bytes == 2


@pytest.mark.parametrize(
    ("source_fmt", "reason"),
    [
        (
            "netex_epip",
            "source provider zz-national/ZZ-NAT is not GTFS (format netex_epip); "
            "the cross-border filter reads GTFS only",
        ),
        ("gtfs", "source feed not found: zz-national/ZZ-NAT (refresh that session first)"),
        (None, "source feed not found: zz-national/ZZ-NAT (refresh that session first)"),
    ],
)
def test_a_derived_provider_says_why_its_source_is_missing(
    source_fmt: str | None, reason: str
) -> None:
    """S3: the source's declared format is checked first."""
    source = _row(_config(("ZZ-NAT", source_fmt))) if source_fmt else None
    db = SimpleNamespace(get=lambda _model, _sid: source)
    derived_from = {"session_id": "zz-national", "provider_id": "ZZ-NAT"}
    assert api._missing_source_reason(db, derived_from) == reason  # type: ignore[arg-type]


# ───────────────────────── what the build reads ─────────────────────────


def test_the_build_inputs_list_one_file_per_provider_after_a_switch(tmp_path: Path) -> None:
    """The MOTIS input listing and the snapshot inputs both glob `*.zip`:
    after the sweep each provider shows once, in its current format. The
    quarantined copies (and an older one in gtfs/) are never listed."""
    root = _inbox(
        tmp_path,
        {
            "gtfs/zz-a.zip": b"ga",
            "netex/zz-a.zip": b"na",
            "netex/zz-b.zip": b"nb",
            "gtfs/zz-c.zip.orphaned": b"oc",
        },
    )
    assert len(worker._motis_timetable_files(root)) == 3  # the bug: zz-a twice
    expected = inbox_sweep.expected_provider_filenames(
        _config(("ZZ-A", "gtfs"), ("ZZ-B", "netex_epip"))
    )
    inbox_sweep.sweep_orphaned_provider_files(root, expected)

    files = worker._motis_timetable_files(root)
    assert [str(f.relative_to(root)) for f in files] == ["gtfs/zz-a.zip", "netex/zz-b.zip"]

    no_upload = SimpleNamespace(scalar_one_or_none=lambda: None)
    db = SimpleNamespace(execute=lambda _stmt: no_upload)
    inputs = graph_snapshots.enumerate_session_inputs(db, SID, root)  # type: ignore[arg-type]
    assert sorted((i["kind"], i["filename"]) for i in inputs) == [
        ("GTFS", "zz-a.zip"),
        ("NeTEx", "zz-b.zip"),
    ]
