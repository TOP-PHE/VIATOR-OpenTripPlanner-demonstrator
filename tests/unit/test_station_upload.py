"""The station store and its upload route (unit 2 of the station panel).

  * `inbox/_stations` and `inbox/_staging` are reserved: the Storage page must
    never offer them for deletion as "a session that no longer exists";
  * `POST /api/admin/stations/sources/{key}/versions` streams a file to
    `inbox/_stations/<key>/`, records its sha256, and treats an identical
    re-upload as a no-op. It never calls `detect.detect` nor
    `ingestion.dispatch`;
  * the per-session upload route answers 400, not 500, when `detect` cannot
    classify the file.

Every file here is synthetic. The PLC prefix `ZZ` does not exist.
"""

from __future__ import annotations

import hashlib
import io
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException, Response, UploadFile
from sqlalchemy.exc import IntegrityError

from app import storage
from app.api.admin import sessions as sessions_api
from app.api.admin import station_sources as api
from app.master import station_files as sf
from app.master import station_store
from app.security import require_platform_admin
from app.settings import settings
from tests.station_fixtures import csv_bytes

ACTOR = SimpleNamespace(id=uuid.uuid4(), username="ops@example.org", role="platform_admin")
REQUEST = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))

TELREF = csv_bytes(
    sf.FILE_SHAPES[sf.ERA_TELREF],
    {"plc": "ZZ00001", "uopid": "ZZ00001", "name": "Exampleville Central", "iso2": "ZZ"},
)


@pytest.fixture
def inbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "inbox"
    root.mkdir(exist_ok=True)
    monkeypatch.setattr(settings, "inbox_dir", root)
    return root


def _upload(data: bytes, filename: str = "telref_locations_v3.csv") -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename=filename)


# ── the store ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("station_master_crd_2026-09.csv", date(2026, 9, 1)),
        ("crd_locations_2026-09-14.csv", date(2026, 9, 14)),
        ("crd_locations_20260914.csv", date(2026, 9, 14)),
        ("telref_locations_v3.csv", None),
        ("export_12345678.csv", None),  # eight digits, not a date
        ("issue_2026-13.csv", None),  # no thirteenth month
        # No 30 February: the day is dropped and the month is what remains.
        ("issue_2026-02-30.csv", date(2026, 2, 1)),
    ],
)
def test_as_of_is_read_off_the_file_name(name: str, expected: date | None) -> None:
    assert station_store.as_of_from_filename(name) == expected


def test_safe_filename_strips_paths_and_odd_characters() -> None:
    assert station_store.safe_filename("../../etc/passwd") == "passwd"
    assert (
        station_store.safe_filename("C:\\data\\crd locations (v2).csv") == "crd_locations__v2_.csv"
    )
    assert station_store.safe_filename(None) == "upload.bin"
    assert station_store.safe_filename("") == "upload.bin"


@pytest.mark.parametrize("key", ["../etc", "a/b", "", "9abc", "x", "with.dot", "a" * 70])
def test_a_source_key_cannot_escape_the_store(inbox: Path, key: str) -> None:
    with pytest.raises(ValueError, match="not a station source key"):
        station_store.source_dir(key)


def test_source_keys_of_the_seeds_are_valid_folder_names(inbox: Path) -> None:
    for key in ("CRD", "ERA_TELREF", "OFFLINE_MASTER", "nap_CH_SBB_non_rail_members"):
        assert station_store.source_dir(key) == inbox / "_stations" / key


async def test_receive_hashes_while_streaming_and_keep_moves_it(inbox: Path) -> None:
    received = await station_store.receive(_upload(TELREF), "ERA_TELREF", max_bytes=10_000_000)
    assert received.sha256 == hashlib.sha256(TELREF).hexdigest()
    assert received.size == len(TELREF)
    assert received.path.parent == inbox / "_stations" / "ERA_TELREF" / "_incoming"

    kept = station_store.keep(received, "ERA_TELREF", "telref locations v3.csv")
    assert kept == (
        inbox / "_stations" / "ERA_TELREF" / f"{received.sha256[:16]}-telref_locations_v3.csv"
    )
    assert kept.read_bytes() == TELREF
    assert not received.path.exists()


async def test_an_oversized_upload_leaves_nothing_behind(inbox: Path) -> None:
    with pytest.raises(station_store.UploadTooLarge):
        await station_store.receive(_upload(b"x" * 5000), "CRD", max_bytes=4096)
    assert list((inbox / "_stations" / "CRD" / "_incoming").iterdir()) == []


async def test_discard_removes_the_incoming_file(inbox: Path) -> None:
    received = await station_store.receive(_upload(b"abc"), "CRD", max_bytes=100)
    station_store.discard(received)
    assert not received.path.exists()


# ── the storage page ───────────────────────────────────────────────────


def test_reserved_inbox_folders_are_never_clean_up_candidates(tmp_path: Path) -> None:
    inbox, graphs = tmp_path / "vol" / "inbox", tmp_path / "vol" / "graphs"
    graphs.mkdir(parents=True)
    for rel in (
        "_stations/CRD/0123456789abcdef-crd_locations_2026-09.csv",
        "_staging/20261003-120000-abcd1234/feed.zip",
        "gone-session/gtfs/x.zip",
    ):
        path = inbox / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 10)

    report = storage.scan(inbox, graphs, set(), set())
    assert {c.id for c in report.candidates} == {"inbox/gone-session"}

    usage = {u.session_id: u for u in report.sessions}
    # Reported, since their size matters, but not badged as a deleted session.
    assert usage["_stations"].inbox_bytes == 10
    assert usage["_stations"].known is True
    assert usage["_staging"].known is True
    assert usage["gone-session"].known is False

    # And delete refuses them even when asked by id.
    deleted, skipped = storage.delete(["inbox/_stations", "inbox/_staging"], report, inbox, graphs)
    assert deleted == []
    assert len(skipped) == 2
    assert (inbox / "_stations" / "CRD").is_dir()


def test_the_reserved_names_cannot_be_session_ids() -> None:
    # A session id is a slug starting with a letter, so no session can ever
    # be shadowed by a reserved folder.
    assert frozenset({"_stations", "_staging"}) == storage.INBOX_ROOT_RESERVED
    assert all(name.startswith("_") for name in storage.INBOX_ROOT_RESERVED)
    assert station_store.STORE_DIRNAME in storage.INBOX_ROOT_RESERVED


# ── the upload route ───────────────────────────────────────────────────


class _FakeDb:
    def __init__(self, *, flush_error: Exception | None = None) -> None:
        self.added: list[Any] = []
        self.committed = False
        self.rolled_back = False
        self._flush_error = flush_error

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    def flush(self) -> None:
        if self._flush_error is not None:
            raise self._flush_error
        for obj in self.added:
            if obj.id is None:
                obj.id = uuid.uuid4()

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def refresh(self, obj: Any) -> None:
        obj.acquired_at = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


def _source(**kw: Any) -> SimpleNamespace:
    base = {"id": uuid.uuid4(), "key": "ERA_TELREF", "format": sf.ERA_TELREF, "enabled": True}
    base.update(kw)
    return SimpleNamespace(**base)


class _Harness:
    """Stands in for the two lookups the route makes, and records the audit."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, source: Any | None) -> None:
        self.source = source
        self.versions: list[Any] = []
        self.audit: list[dict[str, Any]] = []
        monkeypatch.setattr(api, "_source_or_404", self._source_or_404)
        monkeypatch.setattr(api, "_existing_version", self._existing_version)
        monkeypatch.setattr(api.audit, "record", lambda _db, **kw: self.audit.append(kw))

    def _source_or_404(self, _db: Any, key: str) -> Any:
        if self.source is None or self.source.key != key:
            raise HTTPException(404, "Station source not found")
        return self.source

    def _existing_version(self, _db: Any, _source: Any, sha256: str) -> Any:
        return next((v for v in self.versions if v.sha256 == sha256), None)

    async def upload(
        self,
        data: bytes,
        *,
        filename: str = "telref_locations_v3.csv",
        as_of: str | None = None,
        db: _FakeDb | None = None,
        key: str = "ERA_TELREF",
    ) -> tuple[Any, Response, _FakeDb]:
        db = db or _FakeDb()
        response = Response()
        out = await api.upload_version(
            key,
            _upload(data, filename),
            REQUEST,  # type: ignore[arg-type]
            response,
            db,  # type: ignore[arg-type]
            ACTOR,  # type: ignore[arg-type]
            as_of,
        )
        self.versions.extend(db.added)
        return out, response, db


def _stored_files(inbox: Path, key: str = "ERA_TELREF") -> list[Path]:
    folder = inbox / "_stations" / key
    return sorted(p for p in folder.rglob("*") if p.is_file()) if folder.exists() else []


async def test_upload_stores_the_file_and_records_its_fingerprint(
    inbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(monkeypatch, _source())
    out, _, db = await harness.upload(TELREF, as_of="2026-09-14")

    sha = hashlib.sha256(TELREF).hexdigest()
    assert out.created is True
    assert out.version.sha256 == sha
    assert out.version.bytes == len(TELREF)
    assert out.version.as_of == "2026-09-14"
    assert out.version.status == "uploaded"
    assert out.version.stats == {"columns": 36}
    assert out.version.source_key == "ERA_TELREF"

    (version,) = db.added
    assert version.source_id == harness.source.id
    assert version.uploaded_by == ACTOR.id
    assert version.filename == "telref_locations_v3.csv"
    assert _stored_files(inbox) == [Path(version.stored_path)]
    assert Path(version.stored_path).name == f"{sha[:16]}-telref_locations_v3.csv"
    assert Path(version.stored_path).read_bytes() == TELREF

    assert db.committed
    assert harness.audit[0]["action"] == "station_source.version.uploaded"
    assert harness.audit[0]["target_id"] == "ERA_TELREF"
    assert harness.audit[0]["metadata"]["sha256"] == sha


async def test_re_uploading_an_identical_file_is_a_no_op(
    inbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(monkeypatch, _source())
    first, _, _ = await harness.upload(TELREF)
    again, response, db = await harness.upload(TELREF, filename="renamed.csv")

    assert again.created is False
    assert response.status_code == 200
    assert again.version.id == first.version.id
    assert db.added == []
    assert not db.committed
    assert len(harness.audit) == 1  # the first upload only
    assert len(_stored_files(inbox)) == 1  # nothing left in _incoming either


async def test_a_lost_race_on_the_unique_key_is_also_a_no_op(
    inbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(monkeypatch, _source())
    winner = SimpleNamespace(
        id=uuid.uuid4(),
        sha256=hashlib.sha256(TELREF).hexdigest(),
        acquired_at=datetime(2026, 10, 3, tzinfo=UTC),
        as_of=None,
        filename="telref_locations_v3.csv",
        bytes=len(TELREF),
        status="uploaded",
        error=None,
        stats=None,
    )
    calls = {"n": 0}

    def existing(_db: Any, _source: Any, _sha: str) -> Any:
        calls["n"] += 1
        return None if calls["n"] == 1 else winner  # absent, then present after the race

    monkeypatch.setattr(api, "_existing_version", existing)
    db = _FakeDb(flush_error=IntegrityError("insert", {}, Exception("duplicate key")))
    out, response, _ = await harness.upload(TELREF, db=db)

    assert out.created is False
    assert response.status_code == 200
    assert out.version.id == str(winner.id)
    assert db.rolled_back
    assert _stored_files(inbox) == []


async def test_a_file_of_the_wrong_shape_is_refused_with_the_column_list(
    inbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(monkeypatch, _source())
    links = csv_bytes(sf.FILE_SHAPES[sf.LINKS])
    with pytest.raises(HTTPException) as exc:
        await harness.upload(links, filename="station_links_crd_2026-09.csv")
    assert exc.value.status_code == 400
    assert "missing columns: iso2, uopid" in exc.value.detail
    assert "unexpected columns: era_uopid, station_id" in exc.value.detail
    assert _stored_files(inbox) == []
    assert harness.audit == []


async def test_a_format_that_is_not_a_station_shape_is_stored_unchecked(
    inbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(monkeypatch, _source(key="TRAINLINE", format="trainline_csv"))
    out, _, _ = await harness.upload(b"id;name\n1;Exampleville\n", key="TRAINLINE")
    assert out.created is True
    assert out.version.stats is None
    assert len(_stored_files(inbox, "TRAINLINE")) == 1


async def test_as_of_falls_back_to_the_date_in_the_file_name(
    inbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(monkeypatch, _source())
    out, _, _ = await harness.upload(TELREF, filename="telref_2026-09.csv")
    assert out.version.as_of == "2026-09-01"


@pytest.mark.parametrize(
    ("source", "data", "as_of", "status"),
    [
        (None, TELREF, None, 404),
        (_source(enabled=False), TELREF, None, 409),
        (_source(), b"", None, 400),
        (_source(), TELREF, "14/09/2026", 400),
    ],
)
async def test_refusals(
    inbox: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: Any,
    data: bytes,
    as_of: str | None,
    status: int,
) -> None:
    harness = _Harness(monkeypatch, source)
    with pytest.raises(HTTPException) as exc:
        await harness.upload(data, as_of=as_of)
    assert exc.value.status_code == status
    assert _stored_files(inbox) == []


async def test_an_upload_over_the_limit_is_413(
    inbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "max_upload_mb", 0)
    harness = _Harness(monkeypatch, _source())
    with pytest.raises(HTTPException) as exc:
        await harness.upload(TELREF)
    assert exc.value.status_code == 413
    assert _stored_files(inbox) == []


def test_the_route_bypasses_detect_and_dispatch() -> None:
    # A station file belongs to no session and stages into no engine inbox:
    # the module must not even import the two things that would refuse it.
    assert not hasattr(api, "detect")
    assert not hasattr(api, "ingestion")
    source = Path(api.__file__).read_text(encoding="utf-8")
    assert "detect.detect(" not in source.split('"""', 2)[2]
    assert "ingestion.dispatch(" not in source.split('"""', 2)[2]


def test_every_route_of_the_router_requires_a_platform_admin() -> None:
    routes = [r for r in api.router.routes if getattr(r, "dependant", None) is not None]
    assert routes
    for route in routes:
        gates = {dep.call for dep in route.dependant.dependencies}
        assert require_platform_admin in gates, f"{route.path} declares no platform_admin gate"


def test_the_router_is_registered() -> None:
    from app.main import app

    # The OpenAPI schema, not `app.routes`: newer FastAPI versions keep an
    # included router as one entry there instead of flattening its routes.
    assert "post" in app.openapi()["paths"]["/api/admin/stations/sources/{key}/versions"]


# ── the per-session upload route: 400, not 500 ─────────────────────────


@pytest.mark.parametrize(
    ("filename", "data", "fragment"),
    [
        # What a station CSV looks like to `detect`: a CSV it does not know.
        (
            "station_master_crd_2026-09.csv",
            csv_bytes(sf.FILE_SHAPES[sf.MASTER]),
            "Detection failed",
        ),
        ("notes.txt", b"hello", "Unsupported extension"),
        ("broken.zip", b"this is not a zip archive", "Detection failed"),
    ],
)
async def test_session_upload_answers_400_when_detect_cannot_classify(
    inbox: Path, monkeypatch: pytest.MonkeyPatch, filename: str, data: bytes, fragment: str
) -> None:
    session = SimpleNamespace(id="xb-test", config={}, state="created")
    db = SimpleNamespace(get=lambda _model, _sid: session)
    with pytest.raises(HTTPException) as exc:
        await sessions_api.upload_to_session(
            "xb-test",
            "GTFS",
            _upload(data, filename),
            REQUEST,  # type: ignore[arg-type]
            db,  # type: ignore[arg-type]
            ACTOR,  # type: ignore[arg-type]
        )
    assert exc.value.status_code == 400
    assert fragment in exc.value.detail
    # The staged copy is removed: a refused upload leaves nothing in the inbox.
    assert list((inbox / "xb-test" / "_staging").iterdir()) == []
