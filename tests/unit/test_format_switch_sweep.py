"""Issue #381: a provider's format switch quarantines its old-format file.

GTFS and NeTEx share the slot name `<id>.zip`; only the subdir differs. The
sweep compares the `(subdir, filename)` slot, so `netex/<id>.zip` is renamed
`*.zip.orphaned` once the provider is GTFS (and the reverse), at the refresh
end and at the config save itself. Ids are invented.
"""

from __future__ import annotations

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


def _provider(pid: str, fmt: str) -> dict[str, Any]:
    return {
        "id": pid,
        "label": pid,
        "timetable": {"format": fmt, "url": f"https://zz.example.invalid/{pid.lower()}.zip"},
    }


def _config(*providers: tuple[str, str]) -> dict[str, Any]:
    return {"sources": {"providers": [_provider(pid, fmt) for pid, fmt in providers]}}


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


# ───────────────────────── the sweep (refresh end) ─────────────────────────


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
    """Two ids that differ only by case: the lower-case one is refused, so
    the list cannot be read, and the sweep renames nothing (it renamed
    every file before: an empty expected set)."""
    files = {"gtfs/zz-a.zip": b"g", "netex/zz-a.zip": b"n", "gtfs/zz-orphan.zip": b"o"}
    root = _inbox(tmp_path, files)
    config = _config(("ZZ-A", "gtfs"), ("zz-a", "gtfs"))
    expected = inbox_sweep.expected_provider_filenames(config)
    assert expected is None
    assert inbox_sweep.sweep_orphaned_provider_files(root, expected) == []
    assert inbox_sweep.quarantine_format_switch(root, _config(("ZZ-A", "netex_epip")), config) == []
    assert inbox_sweep.quarantine_format_switch(root, config, _config(("ZZ-A", "gtfs"))) == []
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


# ───────────────────────── quarantine at save ─────────────────────────


@pytest.mark.parametrize(
    ("old_fmt", "new_fmt", "orphaned"),
    [
        ("netex_nordic", "gtfs", "netex/zz-a.zip"),
        ("gtfs", "netex_epip", "gtfs/zz-a.zip"),
    ],
)
def test_quarantine_format_switch(
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
    events = inbox_sweep.quarantine_format_switch(root, old, new)
    assert events == [f"{orphaned} → zz-a.zip.orphaned"]
    # The removed provider's file waits for the next refresh, as before.
    assert "gtfs/zz-c.zip" in _names(root)
    assert f"{orphaned}.orphaned" in _names(root)


def test_netex_profiles_share_a_subdir_so_no_switch(tmp_path: Path) -> None:
    root = _inbox(tmp_path, {"netex/zz-a.zip": b"n"})
    old, new = _config(("ZZ-A", "netex_nordic")), _config(("ZZ-A", "netex_epip"))
    assert inbox_sweep.quarantine_format_switch(root, old, new) == []
    assert _names(root) == {"netex/zz-a.zip"}


class _Db:
    def __init__(self, row: Any) -> None:
        self.row = row
        self.committed = False

    def get(self, _model: object, sid: str) -> Any:
        return self.row if sid == self.row.id else None

    def commit(self) -> None:
        self.committed = True


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
) -> list[dict[str, Any]]:
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
    assert db.committed
    return recorded


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
    (event,) = _save(monkeypatch, tmp_path, row, body)
    assert event["action"] == "session.updated"
    assert event["metadata"]["orphaned"] == [f"{orphaned} → zz-a.zip.orphaned"]
    assert _names(root) == {kept, "gtfs/zz-b.zip", f"{orphaned}.orphaned"}


def test_a_save_without_switch_renames_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    files = {"gtfs/zz-a.zip": b"g", "netex/zz-b.zip": b"n", "gtfs/zz-c.zip": b"c"}
    root = _inbox(tmp_path, files)
    row = _row(_config(("ZZ-A", "gtfs"), ("ZZ-B", "netex_epip"), ("ZZ-C", "gtfs")))
    new = _config(("ZZ-A", "gtfs"), ("ZZ-B", "netex_epip"))
    new["sources"]["providers"][0]["label"] = "ZZ renamed"
    (event,) = _save(monkeypatch, tmp_path, row, SessionPatch(config=new))
    assert "orphaned" not in event["metadata"]
    assert _names(root) == set(files)


def test_a_rename_only_save_touches_no_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    files = {"netex/zz-a.zip": b"n"}
    root = _inbox(tmp_path, files)
    row = _row(_config(("ZZ-A", "gtfs")))
    (event,) = _save(monkeypatch, tmp_path, row, SessionPatch(name="ZZ other"))
    assert event["metadata"] == {"changes": {"name": {"from": "ZZ session", "to": "ZZ other"}}}
    assert _names(root) == set(files)


@pytest.mark.parametrize(
    "body",
    [
        # The new provider list fails validation.
        SessionPatch(config=_config(("ZZ-A", "gtfs"), ("zz-bad", "gtfs"))),
        # The config is valid, a later field of the same save is not.
        SessionPatch(config=_config(("ZZ-A", "gtfs")), state="zz-no-such-state"),
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
    assert not db.committed
    assert _names(root) == set(files)


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


@pytest.mark.usefixtures("no_progress_record")
@pytest.mark.parametrize(
    ("now_fmt", "orphaned"), [("gtfs", "netex/zz-a.zip"), ("netex_epip", "gtfs/zz-a.zip")]
)
async def test_the_refresh_end_quarantines_the_old_format_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, now_fmt: str, orphaned: str
) -> None:
    root = _inbox(tmp_path, {"gtfs/zz-a.zip": b"g", "netex/zz-a.zip": b"n"})
    monkeypatch.setattr(settings, "inbox_dir", tmp_path)
    recorded: list[dict[str, Any]] = []

    async def nothing_fetched(*_a: Any, **_k: Any) -> tuple[list[Any], ...]:
        return [], [], []

    monkeypatch.setattr(api, "_gather_refresh_outcomes", nothing_fetched)
    monkeypatch.setattr(api.audit, "record", lambda _db, **kw: recorded.append(kw))
    monkeypatch.setattr(api.station_extract_upload, "after_refresh", lambda *_a, **_k: None)
    row = _row(_config(("ZZ-A", now_fmt)))
    db = SimpleNamespace(
        get=lambda _model, _sid: row, commit=lambda: None, refresh=lambda *_a, **_k: None
    )
    answer = await api.refresh_sources(
        SID,
        REQUEST,  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
        ACTOR,  # type: ignore[arg-type]
        BackgroundTasks(),
    )
    assert answer.orphaned == [f"{orphaned} → zz-a.zip.orphaned"]
    (event,) = recorded
    assert event["metadata"]["orphaned"] == answer.orphaned
    assert f"{orphaned}.orphaned" in _names(root)


# ───────────────────────── what the build reads ─────────────────────────


def test_the_build_inputs_list_one_file_per_provider_after_a_switch(tmp_path: Path) -> None:
    """The MOTIS input listing and the snapshot inputs both glob `*.zip`:
    after the sweep each provider shows once, in its current format."""
    root = _inbox(
        tmp_path,
        {"gtfs/zz-a.zip": b"ga", "netex/zz-a.zip": b"na", "netex/zz-b.zip": b"nb"},
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
