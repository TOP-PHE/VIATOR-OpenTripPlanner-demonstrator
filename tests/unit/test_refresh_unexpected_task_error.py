"""One refresh task's unexpected exception skips that task only.

Issue of 19 Oct: a refresh of about a hundred feeds stopped after the first
one. A NAP lookup's credential read (`_authorize` → `db.get`) waited for a
database connection, the pool raised its TimeoutError, and the exception
went past `_refresh_one_task` (it caught only the expected errors): the
refresh ended "failed" with every other task not reached.

Now `_refresh_one_task` (every download: the whole session's refresh, a
provider card's, the OSM one) and `_materialise_derived_provider` (every
cross-border filter) turn any `Exception` into a `skipped` outcome whose
reason names the exception type only, log one ERROR line (label and type)
and roll the request's session back; a cancellation still ends the refresh.
The end-to-end runs on PostgreSQL are in
tests/integration/test_refresh_progress.py.
"""

from __future__ import annotations

import asyncio
import io
import logging
import uuid
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy.exc import TimeoutError as PoolTimeout

from app import feed_fetch, feed_resolvers, refresh_progress
from app.api.admin import sessions as sessions_api
from app.master import nap_importer

_SECRET = "ZZSECRET99"
_POOL_TEXT = f"QueuePool limit reached https://zz.invalid/f?apikey={_SECRET}"
_REASON = "unexpected error ({}); see the web log"
_NAP_RESOLVER = {
    "type": "json_api",
    "url": "https://zz.invalid/api/data-sets",
    "items": "",
    "match": {"name": "ZZ"},
    "download": "https://zz.invalid/api/data-sets/{id}/file",
}


class _Db:
    """The request's session, as far as a refresh task uses it."""

    def __init__(self, *, get_raises: BaseException | None = None) -> None:
        self.get_raises = get_raises
        self.rollbacks = 0
        self.flush_raises: Exception | None = None
        self.rollback_raises: Exception | None = None

    def get(self, _model: Any, _key: Any) -> Any:
        if self.get_raises is not None:
            raise self.get_raises
        return None

    def flush(self) -> None:
        if self.flush_raises is not None:
            raise self.flush_raises

    def rollback(self) -> None:
        self.rollbacks += 1
        if self.rollback_raises is not None:
            raise self.rollback_raises


def _gtfs_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name in ("agency.txt", "stops.txt", "routes.txt", "trips.txt", "stop_times.txt"):
            z.writestr(name, "id\n1\n")
    return buf.getvalue()


@pytest.fixture
def staging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "zz-inbox"
    monkeypatch.setattr(sessions_api.settings, "inbox_dir", root)
    monkeypatch.setattr(feed_resolvers, "_validate_safe_http_url", lambda url: url)
    monkeypatch.setattr(nap_importer, "_validate_safe_http_url", lambda url: url)
    path = root / "zz" / "_staging"
    path.mkdir(parents=True)
    return path


@pytest.fixture
def placed(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """`ingestion.place` and the rebuild queue, without a disk slot or a database."""
    calls: list[str] = []

    def fake_place(path: Path, kind: str, *, session_id: str, staged_filename: Any) -> bool:
        calls.append(kind)
        return False

    monkeypatch.setattr(sessions_api.ingestion, "place", fake_place)
    return calls


def _answer_every_download(monkeypatch: pytest.MonkeyPatch) -> None:
    real = httpx.AsyncClient

    def mocked(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(
            lambda _r: httpx.Response(200, content=_gtfs_zip())
        )
        return real(*args, **kwargs)

    monkeypatch.setattr(sessions_api.httpx, "AsyncClient", mocked)


def _task(pid: str, credential: str | None = None, resolver: Any = None) -> Any:
    return sessions_api._RefreshTask(
        f"provider[{pid}].timetable(gtfs)",
        "GTFS",
        f"https://zz.invalid/{pid.lower()}.zip",
        f"{pid.lower()}.zip",
        credential,
        resolver,
    )


def _errors(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]


async def test_a_pool_timeout_in_a_nap_lookup_skips_the_task_with_its_type_only(
    staging: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The incident's path: resolve → auth → `_authorize` → `db.get`."""
    db = _Db(get_raises=PoolTimeout(_POOL_TEXT))
    task = _task("ZZNAP", credential=str(uuid.uuid4()), resolver=_NAP_RESOLVER)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(500))
    ) as c:
        outcome = await sessions_api._refresh_one_task(c, db, "zz", staging, task)  # type: ignore[arg-type]
    assert outcome == {
        "status": "skipped",
        "key": "provider[ZZNAP].timetable(gtfs)",
        "url": task.url,
        "reason": _REASON.format("TimeoutError"),
    }
    assert db.rollbacks == 1
    assert _errors(caplog) == [
        "refresh.task_failed_unexpectedly key=provider[ZZNAP].timetable(gtfs) type=TimeoutError"
    ]
    assert _SECRET not in caplog.text
    # The feed-status panel's "Last result" says the same.
    state = feed_fetch.load_state(sessions_api._fetch_state_dir("zz"), task.label)
    assert state["last_attempt"]["reason"] == _REASON.format("TimeoutError")


async def test_the_tasks_after_an_unexpected_error_still_run_and_the_progress_counts_them(
    staging: Path, placed: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    real = sessions_api._authorize

    async def breaking(client: Any, db: Any, credential_id: Any, url: str) -> Any:
        if credential_id == "zz-broken":
            raise RuntimeError(f"stand-in bug for {url}?apikey={_SECRET}")
        return await real(client, db, credential_id, url)

    monkeypatch.setattr(sessions_api, "_authorize", breaking)
    _answer_every_download(monkeypatch)
    run = refresh_progress.RefreshRun(
        "zz",
        refresh_progress.PROVIDERS,
        uuid.uuid4(),
        refresh_progress._initial_detail(3, None),
    )
    run._execute = lambda *_a, **_k: None  # type: ignore[method-assign]
    work = [_task("ZZONE"), _task("ZZBUG", credential="zz-broken"), _task("ZZTHREE")]
    db = _Db()

    fetched, unchanged, skipped = await sessions_api._gather_refresh_outcomes(
        db,  # type: ignore[arg-type]
        "zz",
        staging,
        {},
        work,
        run,
    )

    assert [f["key"] for f in fetched] == [work[0].label, work[2].label]
    assert unchanged == []
    assert [(s["key"], s["reason"]) for s in skipped] == [
        (work[1].label, _REASON.format("RuntimeError"))
    ]
    assert placed == ["GTFS", "GTFS"]
    detail = run.detail
    assert (detail["done"], detail["total"], detail["fetched"], detail["failed"]) == (3, 3, 2, 1)
    assert detail["failed_keys"] == [work[1].label]
    assert db.rollbacks == 1


def test_a_cancellation_still_ends_the_refresh(
    staging: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def cancelled(*_a: Any, **_k: Any) -> Any:
        raise asyncio.CancelledError

    monkeypatch.setattr(sessions_api, "_resolve_and_download", cancelled)
    db = _Db()
    one = sessions_api._refresh_one_task(None, db, "zz", staging, _task("ZZONE"))  # type: ignore[arg-type]
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(one)
    assert db.rollbacks == 0


async def test_a_failed_rollback_is_logged_and_the_task_is_still_skipped(
    staging: Path, caplog: pytest.LogCaptureFixture
) -> None:
    db = _Db(get_raises=PoolTimeout(_POOL_TEXT))
    db.rollback_raises = RuntimeError(f"stand-in {_SECRET}")
    async with httpx.AsyncClient() as c:
        outcome = await sessions_api._refresh_one_task(
            c,
            db,  # type: ignore[arg-type]
            "zz",
            staging,
            _task("ZZONE", credential=str(uuid.uuid4())),
        )
    assert outcome["reason"] == _REASON.format("TimeoutError")
    assert "refresh.rollback_failed type=RuntimeError" in caplog.text
    assert _SECRET not in caplog.text


async def test_a_failed_credential_stamp_rolls_the_session_back(
    staging: Path, placed: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    cred = SimpleNamespace(id=uuid.uuid4(), last_used_at=None)

    async def authorize(_client: Any, _db: Any, _cid: Any, url: str) -> Any:
        return url, {}, cred

    monkeypatch.setattr(sessions_api, "_authorize", authorize)
    db = _Db()
    db.flush_raises = RuntimeError("stand-in flush")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, content=_gtfs_zip()))
    ) as c:
        outcome = await sessions_api._refresh_one_task(
            c,
            db,  # type: ignore[arg-type]
            "zz",
            staging,
            _task("ZZONE", credential="zz-cred"),
        )
    assert outcome["status"] == "fetched"
    assert db.rollbacks == 1


async def test_a_failed_dispatch_rolls_the_session_back(
    staging: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_a: Any, **_k: Any) -> bool:
        raise PoolTimeout("stand-in")

    monkeypatch.setattr(sessions_api.ingestion, "place", broken)
    db = _Db()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, content=_gtfs_zip()))
    ) as c:
        outcome = await sessions_api._refresh_one_task(c, db, "zz", staging, _task("ZZONE"))  # type: ignore[arg-type]
    assert outcome["status"] == "skipped"
    assert outcome["reason"].startswith("dispatch failed")
    assert db.rollbacks == 1


# ── cross-border filters (derived providers) ──


def _derived_config(*pids: str) -> dict[str, Any]:
    return {
        "sources": {
            "providers": [
                {
                    "id": pid,
                    "country_iso": "ZZ",
                    "timetable": {
                        "format": "gtfs",
                        "source": "cross_border_filter",
                        "derived_from": {"session_id": "zz-national", "provider_id": "ZZNAT"},
                    },
                }
                for pid in pids
            ]
        }
    }


async def test_an_unexpected_error_in_one_derived_provider_skips_it_only(
    staging: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def filtering(_db: Any, _sid: str, provider: Any, _tt: Any, _st: Path, key: str) -> Any:
        if provider["id"] == "ZZXA":
            raise PoolTimeout(_POOL_TEXT)
        return {"status": "fetched", "key": key}

    monkeypatch.setattr(sessions_api, "_filter_derived_provider", filtering)
    db = _Db()
    outcomes = await sessions_api._run_derived_filters(
        db,  # type: ignore[arg-type]
        "zz",
        _derived_config("ZZXA", "ZZXB"),
        staging,
    )
    assert outcomes == [
        {
            "status": "skipped",
            "key": "provider[ZZXA].cross_border_filter",
            "error": _REASON.format("TimeoutError"),
        },
        {"status": "fetched", "key": "provider[ZZXB].cross_border_filter"},
    ]
    assert db.rollbacks == 1
    assert _errors(caplog) == [
        "refresh.task_failed_unexpectedly key=provider[ZZXA].cross_border_filter type=TimeoutError"
    ]
    assert _SECRET not in caplog.text


def test_a_cancelled_derived_provider_still_ends_the_refresh(
    staging: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def cancelled(*_a: Any, **_k: Any) -> Any:
        raise asyncio.CancelledError

    monkeypatch.setattr(sessions_api, "_filter_derived_provider", cancelled)
    db = _Db()
    run = sessions_api._run_derived_filters(db, "zz", _derived_config("ZZXA"), staging)  # type: ignore[arg-type]
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run)
    assert db.rollbacks == 0


async def test_a_label_of_an_unexpected_shape_is_not_logged_as_is(
    staging: Path, caplog: pytest.LogCaptureFixture
) -> None:
    assert refresh_progress.task_label("provider[ZZ].timetable(gtfs)") == (
        "provider[ZZ].timetable(gtfs)"
    )
    task = _task("ZZONE", credential=str(uuid.uuid4()))._replace(label="zz\nforged line")
    async with httpx.AsyncClient() as c:
        await sessions_api._refresh_one_task(
            c,
            _Db(get_raises=PoolTimeout(_POOL_TEXT)),  # type: ignore[arg-type]
            "zz",
            staging,
            task,
        )
    assert _errors(caplog) == ["refresh.task_failed_unexpectedly key=task type=TimeoutError"]
