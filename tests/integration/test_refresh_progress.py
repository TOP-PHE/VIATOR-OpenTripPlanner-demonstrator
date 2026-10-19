"""Integration: the progress record of a session's source refreshes (issue #359).

On a real PostgreSQL (`app/refresh_progress.py`, table
`source_refresh_progress`):

- a refresh's progress is read back by a new request (a page reload) and by
  another client (another browser), the bytes of the feed being downloaded
  included, while the refresh runs;
- the end result (fetched / unchanged / failed counts, the failed labels, the
  station-module follow-up) stays until the next refresh;
- a second refresh of the same kind is refused with 409
  `refresh_already_running` and its start time, the claim holds against two
  threads at once, and the OSM refresh rotates nothing when refused;
- a provider card's refresh writes a record of its own (scope: its feed id)
  with its station-module follow-up bound to its run; the same provider
  twice, a card refresh during the session's and the session's during a card
  refresh are refused, two providers may run together;
- a record whose heartbeat stopped for longer than the lease is shown
  `interrupted`, a new refresh takes it over, and the old refresh's writes
  change nothing;
- no URL, token or reason text ever reaches the record or the API.

Invented values only (ZZ providers, sizes with 99). Fresh-DB harness like the
other integration tests; skips when Postgres is down.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import random
import threading
import time
import uuid
import zipfile
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from alembic import command

_BOOTSTRAP = "test-bootstrap-token"
SID = "zz-progress"
# A credential-looking query value that must never reach the record.
_SECRET_MARK = "ZZSECRET99"
_HOST = "zz-feeds.example.invalid"
MIB = 1024 * 1024


def _postgres_or_skip() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgresql"):
        pytest.skip("DATABASE_URL is not Postgres; skipping refresh-progress test")
    try:
        with create_engine(url).connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError as exc:
        pytest.skip(f"Postgres not reachable ({exc})")
    return url


@pytest.fixture
def fresh_db(monkeypatch: pytest.MonkeyPatch) -> str:
    url = _postgres_or_skip()
    from app.settings import settings as live

    monkeypatch.setattr(live, "bootstrap_token", _BOOTSTRAP)
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE;"))
        conn.execute(text("CREATE SCHEMA public;"))
    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "alembic")
    command.upgrade(cfg, "head")
    from app import config_service

    config_service.invalidate_cache()
    return url


@pytest.fixture
def client(fresh_db: str) -> Iterator[TestClient]:
    from app.main import app

    with TestClient(app, follow_redirects=False) as c:
        yield c


@pytest.fixture
def admin(client: TestClient) -> dict[str, str]:
    r = client.post(
        "/api/auth/bootstrap-platform-user",
        json={
            "token": _BOOTSTRAP,
            "email": "admin@viator.example",
            "name": "Admin",
            "password": "a-strong-admin-password",
        },
    )
    r.raise_for_status()
    jwt = r.json()["jwt"]
    client.cookies.clear()
    return {"Authorization": f"Bearer {jwt}"}


@pytest.fixture
def inbox(_isolated_inbox: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from app.settings import settings as live

    monkeypatch.setattr(live, "inbox_dir", _isolated_inbox)
    return _isolated_inbox


@pytest.fixture
def no_station_module(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import station_module

    monkeypatch.setattr(station_module, "enabled", lambda: False)


def _url(name: str) -> str:
    return f"https://{_HOST}/{name}.zip?apikey={_SECRET_MARK}"


def _provider(pid: str, fmt: str = "gtfs") -> dict[str, Any]:
    return {
        "id": pid,
        "country_iso": "ZZ",
        "timetable": {"format": fmt, "url": _url(pid.lower())},
    }


def _make_session(
    client: TestClient, admin: dict[str, str], pids: list[str], fmt: str = "gtfs"
) -> None:
    r = client.post(
        "/api/sessions",
        headers=admin,
        json={"id": SID, "name": "ZZ progress", "category": "NAP", "config": {}},
    )
    assert r.status_code == 201, r.text
    from sqlalchemy.orm.attributes import flag_modified

    from app.db import SessionLocal
    from app.models import Session as SessionRow

    with SessionLocal() as db:
        s = db.get(SessionRow, SID)
        assert s is not None
        s.config = {"sources": {"providers": [_provider(p, fmt) for p in pids]}}
        flag_modified(s, "config")
        db.commit()


# A fixed date for every zip entry: `writestr` with a bare name stamps the
# current time (2-second resolution), so two builds of the "same" feed a
# moment apart differ in their bytes and sha256, and a refresh rightly
# downloads them as a new file. The fixtures must be the same file each time.
_ZIP_DATE = (2026, 1, 1, 0, 0, 0)


def _gtfs_bytes(padding: int = 0) -> bytes:
    """An invented GTFS zip, byte-identical on every call for one `padding`."""
    buf = io.BytesIO()
    members = {
        "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\n"
        "ZZ,ZZ Rail,https://zz.invalid,UTC\n",
        "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\nZZ99,ZZ Halt,0.99,0.99\n",
        "routes.txt": "route_id,agency_id,route_short_name,route_type\nR,ZZ,R,2\n",
        "trips.txt": "route_id,service_id,trip_id\nR,WD,T\n",
        "stop_times.txt": "trip_id,stop_sequence,stop_id,arrival_time,departure_time\n"
        "T,1,ZZ99,08:00,08:00\n",
    }
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in members.items():
            z.writestr(zipfile.ZipInfo(name, _ZIP_DATE), text, compress_type=zipfile.ZIP_DEFLATED)
        if padding:
            z.writestr(
                zipfile.ZipInfo("padding.bin", _ZIP_DATE),
                random.Random(99).randbytes(padding),
                compress_type=zipfile.ZIP_STORED,
            )
    return buf.getvalue()


def test_the_fixture_feeds_are_the_same_file_every_time() -> None:
    """The second refresh above expects "unchanged" (same sha256): a fixture
    stamped with the current time would be a new file once the clock passes
    a 2-second zip date step (main run 2d4de26 failed so)."""
    first = _gtfs_bytes(padding=99)
    for info in zipfile.ZipFile(io.BytesIO(first)).infolist():
        assert info.date_time == _ZIP_DATE
    assert _gtfs_bytes(padding=99) == first
    small = _gtfs_bytes()
    assert _gtfs_bytes() == small


def _read(sid: str = SID) -> dict[str, Any]:
    from app import refresh_progress

    return refresh_progress.read(sid)


# A running refresh writes its progress through a writer thread of its own
# (`RefreshRun._write` inside `async with`): a read made while the refresh
# runs may come before that write has landed, more so on a loaded runner
# (main run 39225159844 read `current == []`, issue #373 item 1).
_WAIT_STEP_SECONDS = 0.05
_WAIT_DEADLINE_SECONDS = 5.0


def _wait_for_record(wanted: Callable[[dict[str, Any]], bool], what: str) -> dict[str, Any]:
    """Read the record until `wanted(record)` holds, and return it; fail
    with the last record read when it does not hold within the deadline."""
    deadline = time.monotonic() + _WAIT_DEADLINE_SECONDS
    while True:
        record = _read()
        if wanted(record):
            return record
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"{what}: not in the record after {_WAIT_DEADLINE_SECONDS} s; "
                f"last record read: {record!r}"
            )
        time.sleep(_WAIT_STEP_SECONDS)


def _age_heartbeat(minutes: int) -> None:
    from app.db import SessionLocal

    with SessionLocal() as db:
        db.execute(
            text(
                "UPDATE source_refresh_progress "
                "SET heartbeat_at = now() - make_interval(mins => :m), "
                "started_at = now() - make_interval(mins => :m + 1)"
            ),
            {"m": minutes},
        )
        db.commit()


def _mock_downloads(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    """Every AsyncClient the refresh opens answers from `handler`."""
    real = httpx.AsyncClient

    def client_with_mock(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    from app.api.admin import sessions as sessions_api

    monkeypatch.setattr(sessions_api.httpx, "AsyncClient", client_with_mock)


# ── the record across reloads and clients, bytes while downloading ───────


@pytest.mark.usefixtures("inbox", "no_station_module")
def test_progress_is_server_state_seen_by_every_reader_while_it_runs(
    client: TestClient, admin: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.main import app

    _make_session(client, admin, ["ZZBIG", "ZZFAIL", "ZZRAIL"])
    big = _gtfs_bytes(padding=3 * MIB + 99)
    seen: list[dict[str, Any]] = []
    other_browser: list[dict[str, Any]] = []

    def bytes_shown(received: int) -> Callable[[dict[str, Any]], bool]:
        def shown(record: dict[str, Any]) -> bool:
            current = record["providers"]["current"]
            return bool(current) and current[0]["bytes"] == received

        return shown

    async def big_body() -> AsyncIterator[bytes]:
        for start in range(0, len(big), MIB):
            if start:
                # The previous chunk was reported; its write goes through the
                # run's writer thread: a new request (a reload) reads it from
                # the database once it has landed.
                seen.append(_wait_for_record(bytes_shown(start), f"{start} bytes received"))
            yield big[start : start + MIB]

    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[-1]
        if name == "zzbig.zip":
            return httpx.Response(
                200, headers={"content-length": str(len(big))}, content=big_body()
            )
        if name == "zzfail.zip":
            return httpx.Response(404)
        return httpx.Response(200, content=_gtfs_bytes())

    _mock_downloads(monkeypatch, handler)

    # Another browser reads the record through the API while the first one
    # waits for its POST: the reader runs on a thread and polls.
    stop = threading.Event()

    def poll() -> None:
        browser = TestClient(app)
        while not stop.is_set():
            r = browser.get(f"/api/sessions/{SID}/sources/progress", headers=admin)
            body = r.json()
            if body["providers"] and body["providers"]["state"] == "running":
                other_browser.append(body)
            stop.wait(0.01)

    reader = threading.Thread(target=poll)
    reader.start()
    try:
        r = client.post(f"/api/sessions/{SID}/sources/refresh", headers=admin)
    finally:
        stop.set()
        reader.join()
    assert r.status_code == 200, r.text

    # While ZZBIG downloaded: running, 0 of 3 done, its bytes and size known.
    assert seen, "the download body was never read"
    first = seen[0]["providers"]
    assert first["state"] == "running"
    assert first["total"] == 3
    assert first["done"] == 0
    current = first["current"][0]
    assert (current["key"], current["bytes"], current["expected"]) == (
        "provider[ZZBIG].timetable(gtfs)",
        MIB,
        len(big),
    )
    assert current["step"] == "downloading"
    assert current["elapsed_seconds"] >= 0
    assert [s["providers"]["current"][0]["bytes"] for s in seen] == [MIB, 2 * MIB, 3 * MIB]
    assert first["elapsed_seconds"] >= 0
    assert other_browser, "the other browser never saw the running refresh"

    # The end result stays for every later reader.
    done = client.get(f"/api/sessions/{SID}/sources/progress", headers=admin).json()
    providers = done["providers"]
    assert providers["state"] == "finished"
    assert (providers["total"], providers["done"]) == (3, 3)
    assert (providers["fetched"], providers["unchanged"], providers["failed"]) == (2, 0, 1)
    assert providers["not_reached"] == 0
    assert providers["failed_keys"] == ["provider[ZZFAIL].timetable(gtfs)"]
    assert providers["current"] == []
    assert providers["finished_at"] is not None
    assert providers["extract"]["state"] == "off"
    assert done["osm"] is None

    # A second refresh: the files are unchanged (sha256 match); the record is
    # the new refresh's, the old counts are gone.
    r = client.post(f"/api/sessions/{SID}/sources/refresh", headers=admin)
    assert r.status_code == 200, r.text
    again = _read()["providers"]
    assert (again["fetched"], again["unchanged"], again["failed"]) == (0, 2, 1)
    assert sorted(u["key"] for u in r.json()["unchanged"]) == [
        "provider[ZZBIG].timetable(gtfs)",
        "provider[ZZRAIL].timetable(gtfs)",
    ]

    # Never a URL, a token or a reason text, in the API or in the table.
    for blob in (json.dumps(done), json.dumps(seen), json.dumps(other_browser)):
        assert _HOST not in blob
        assert _SECRET_MARK not in blob
        assert "Not Found" not in blob
        assert "download failed" not in blob
    from app.db import SessionLocal

    with SessionLocal() as db:
        raw = db.execute(
            text("SELECT detail::text || extract::text FROM source_refresh_progress")
        ).scalar_one()
    assert _HOST not in raw
    assert _SECRET_MARK not in raw


# ── the steps after a download, and the page stays answered meanwhile ────

_NETEX_XML = (
    b'<?xml version="1.0"?><PublicationDelivery xmlns="http://www.netex.org.uk/netex" '
    b'version="1.0"><ParticipantRef>ZZ</ParticipantRef><dataObjects>'
    b'<ServiceJourney id="zz99"/></dataObjects></PublicationDelivery>'
)


def _netex_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(zipfile.ZipInfo("zz.xml", _ZIP_DATE), _NETEX_XML)
    return buf.getvalue()


def _shows(record: dict[str, Any], step: str) -> bool:
    """The record shows `step`, with the structure check's scanned bytes
    once that write (queued after the step's own) has landed too."""
    current = record["current"]
    if not current or current[0]["step"] != step:
        return False
    return step != "checking_structure" or current[0]["scanned"] == 50 * MIB


@pytest.mark.usefixtures("inbox", "no_station_module")
@pytest.mark.parametrize("slow", ["checking_structure", "storing"])
def test_the_progress_api_answers_while_a_slow_step_runs(
    client: TestClient, admin: dict[str, str], monkeypatch: pytest.MonkeyPatch, slow: str
) -> None:
    """Production 15 Oct: after DB's 2 GB download the block stayed on
    "100 %" for 20 minutes. The structure check and the copy into the slot
    now show as steps, and they run off the event loop: a GET on the SAME
    event loop as the refresh answers while the step is held (a step on the
    loop would hold the GET until it ended, and the step would be gone)."""
    from app import ingestion, netex_structure
    from app.main import app

    _make_session(client, admin, ["ZZNETEX"], fmt="netex_epip")
    real_client = httpx.AsyncClient
    _mock_downloads(monkeypatch, lambda _r: httpx.Response(200, content=_netex_bytes()))
    entered = threading.Event()
    release = threading.Event()
    held: list[bool] = []  # False: the wait timed out (the step held the loop)

    def hold() -> None:
        # Released by the test once its GET has answered. The bound only
        # matters when the step wrongly holds the event loop (the GET can
        # then never run): longer than the reads below, so a loaded runner
        # never releases it early.
        entered.set()
        held.append(release.wait(60))

    real_fingerprint = netex_structure.fingerprint

    def slow_fingerprint(path: Path, progress: Any = None) -> Any:
        if slow == "checking_structure":
            progress(0, 99 * MIB)
            progress(50 * MIB, 99 * MIB)  # past a 5 % step: written
            hold()
        return real_fingerprint(path, progress)

    real_place = ingestion.place

    def slow_place(*args: Any, **kwargs: Any) -> bool:
        if slow == "storing":
            hold()
        return real_place(*args, **kwargs)

    monkeypatch.setattr(netex_structure, "fingerprint", slow_fingerprint)
    monkeypatch.setattr(ingestion, "place", slow_place)
    progress_url = f"/api/sessions/{SID}/sources/progress"

    async def scenario() -> tuple[dict[str, Any], bool, httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with real_client(transport=transport, base_url="http://zz.invalid") as web:
            post = asyncio.create_task(
                web.post(f"/api/sessions/{SID}/sources/refresh", headers=admin)
            )
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 30
            while not (entered.is_set() or post.done()) and loop.time() < deadline:
                await asyncio.sleep(0.01)
            assert entered.is_set(), "the slow step never started"
            record: dict[str, Any] = {}
            deadline = loop.time() + 30
            while loop.time() < deadline:  # the step's write lands through the writer thread
                r = await asyncio.wait_for(web.get(progress_url, headers=admin), timeout=15)
                assert r.status_code == 200
                record = r.json()["providers"]
                if _shows(record, slow):
                    break
                await asyncio.sleep(0.01)
            answered_while_held = not release.is_set()
            release.set()
            return record, answered_while_held, await post

    record, answered_while_held, response = asyncio.run(scenario())
    assert held == [True], "the step's wait timed out: it ran on the event loop"
    assert answered_while_held
    assert record["state"] == "running"
    current = record["current"][0]
    assert current["key"] == "provider[ZZNETEX].timetable(netex_epip)"
    assert current["step"] == slow
    assert current["elapsed_seconds"] >= 0
    if slow == "checking_structure":
        assert (current["scanned"], current["scan_total"]) == (50 * MIB, 99 * MIB)
    assert response.status_code == 200, response.text
    assert [f["key"] for f in response.json()["fetched"]] == [
        "provider[ZZNETEX].timetable(netex_epip)"
    ]
    done = _read()["providers"]
    assert (done["state"], done["fetched"], done["current"]) == ("finished", 1, [])
    # The file reached the slot and the rebuild was queued (on the loop's thread).
    from app.db import SessionLocal
    from app.models import RebuildJob

    with SessionLocal() as db:
        assert db.query(RebuildJob).filter(RebuildJob.session_id == SID).count() == 1


@pytest.mark.usefixtures("inbox", "no_station_module")
def test_derived_providers_are_counted_and_recorded(
    client: TestClient, admin: dict[str, str]
) -> None:
    from sqlalchemy.orm.attributes import flag_modified

    from app.db import SessionLocal
    from app.models import Session as SessionRow

    _make_session(client, admin, [])
    with SessionLocal() as db:
        s = db.get(SessionRow, SID)
        assert s is not None
        s.config = {
            "sources": {
                "providers": [
                    {
                        "id": "ZZXB",
                        "timetable": {
                            "format": "gtfs",
                            "source": "cross_border_filter",
                            "derived_from": {"session_id": "zz-none", "provider_id": "ZZRAIL"},
                        },
                    }
                ]
            }
        }
        flag_modified(s, "config")
        db.commit()
    r = client.post(f"/api/sessions/{SID}/sources/refresh", headers=admin)
    assert r.status_code == 200, r.text
    record = _read()["providers"]
    assert (record["total"], record["done"], record["failed"]) == (1, 1, 1)
    assert record["failed_keys"] == ["provider[ZZXB].cross_border_filter"]


@pytest.mark.usefixtures("inbox")
def test_the_progress_api_needs_a_content_manager(
    client: TestClient, admin: dict[str, str]
) -> None:
    _make_session(client, admin, ["ZZRAIL"])
    assert client.get(f"/api/sessions/{SID}/sources/progress").status_code in (401, 403)
    r = client.get("/api/sessions/zz-none/sources/progress", headers=admin)
    assert r.status_code == 404
    body = client.get(f"/api/sessions/{SID}/sources/progress", headers=admin).json()
    assert body["providers"] is None
    assert body["osm"] is None
    assert body["now"]


# ── one refresh of a kind at a time ──────────────────────────────────────


@pytest.mark.usefixtures("inbox", "no_station_module")
def test_a_second_refresh_is_refused_while_one_runs(
    client: TestClient, admin: dict[str, str], inbox: Path
) -> None:
    from app import refresh_progress

    _make_session(client, admin, ["ZZRAIL"])
    run = refresh_progress.claim(SID, refresh_progress.PROVIDERS, 1)
    started = _read()["providers"]["started_at"]

    r = client.post(f"/api/sessions/{SID}/sources/refresh", headers=admin)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error"] == "refresh_already_running"
    assert detail["scope"] == "providers"
    assert detail["started_at"] == started
    assert started in detail["message"]
    # The running refresh's record is untouched.
    assert _read()["providers"]["state"] == "running"

    with pytest.raises(refresh_progress.RefreshRunning):
        refresh_progress.claim(SID, refresh_progress.PROVIDERS, 1)

    # An OSM refresh is another kind: allowed. Refused while one runs, it
    # rotates nothing.
    osm_dir = inbox / SID / "osm"
    osm_dir.mkdir(parents=True)
    (osm_dir / "osm.pbf").write_bytes(b"\x00\x00\x00\x0dZZ99")
    from app.db import SessionLocal
    from app.models import Session as SessionRow

    with SessionLocal() as db:
        s = db.get(SessionRow, SID)
        assert s is not None
        s.config = {**s.config, "sources": {**s.config["sources"], "osm_pbf": _url("osm")}}
        db.commit()
    refresh_progress.claim(SID, refresh_progress.OSM, 1)
    r = client.post(f"/api/sessions/{SID}/sources/osm/refresh", headers=admin)
    assert r.status_code == 409
    assert r.json()["detail"]["scope"] == "osm"
    assert sorted(p.name for p in osm_dir.iterdir()) == ["osm.pbf"]

    # Once the running refresh has finished, a new one may start.
    run.finish(refresh_progress.FINISHED)
    second = refresh_progress.claim(SID, refresh_progress.PROVIDERS, 1)
    assert second.run_id != run.run_id


@pytest.mark.usefixtures("inbox", "no_station_module")
def test_a_provider_card_refresh_waits_for_the_session_refresh(
    client: TestClient, admin: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import refresh_progress

    _make_session(client, admin, ["ZZRAIL"])
    run = refresh_progress.claim(SID, refresh_progress.PROVIDERS, 1)
    started = _read()["providers"]["started_at"]
    r = client.post(f"/api/sessions/{SID}/providers/ZZRAIL/refresh", headers=admin)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert (detail["error"], detail["scope"], detail["provider_id"]) == (
        "refresh_already_running",
        "providers",
        None,
    )
    assert detail["started_at"] == started
    # Refused before anything was written: no record of its own.
    assert _read()["single"] == []
    # A dead session refresh (past its lease) does not block the card.
    _age_heartbeat(refresh_progress.LEASE_SECONDS // 60 + 1)
    _mock_downloads(monkeypatch, lambda _r: httpx.Response(200, content=_gtfs_bytes()))
    r = client.post(f"/api/sessions/{SID}/providers/ZZRAIL/refresh", headers=admin)
    assert r.status_code == 200, r.text
    assert _read()["providers"]["state"] == "interrupted"
    # Nor does a finished one.
    second = refresh_progress.claim(SID, refresh_progress.PROVIDERS, 1)
    second.finish(refresh_progress.FINISHED)
    assert run.run_id != second.run_id
    assert (
        client.post(f"/api/sessions/{SID}/providers/ZZRAIL/refresh", headers=admin).status_code
        == 200
    )
    # The card's refresh wrote its own record, never the session's.
    record = _read()["providers"]
    assert (record["state"], record["done"], record["fetched"]) == ("finished", 0, 0)
    [single] = _read()["single"]
    assert (single["provider_id"], single["state"], single["unchanged"]) == (
        "ZZRAIL",
        "finished",
        1,
    )


# ── one provider alone (the provider card's Refresh) ─────────────────────


def _run_id(scope: str) -> uuid.UUID:
    from app.db import SessionLocal

    with SessionLocal() as db:
        found: uuid.UUID = db.execute(
            text("SELECT run_id FROM source_refresh_progress WHERE session_id = :s AND scope = :c"),
            {"s": SID, "c": scope},
        ).scalar_one()
    return found


@pytest.mark.usefixtures("inbox")
def test_a_provider_card_refresh_has_its_own_record_and_follow_up(
    client: TestClient, admin: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import refresh_progress, station_module
    from app import station_extract_upload as up

    _make_session(client, admin, ["ZZRAIL", "ZZBUS"])
    whole = refresh_progress.claim(SID, refresh_progress.PROVIDERS, 2)
    whole.finish(refresh_progress.FAILED, error="ZzError")
    monkeypatch.setattr(station_module, "enabled", lambda: True)
    queued: list[tuple[Any, ...]] = []
    monkeypatch.setattr(up, "after_refresh", lambda *a, **k: queued.append((a, k)))
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        # While its download runs, the card's refresh is read back as running,
        # its task once the writer thread has written it.
        record = _wait_for_record(
            lambda rec: bool(rec["single"]) and bool(rec["single"][0]["current"]),
            "the card refresh's current task",
        )["single"][0]
        assert (record["state"], record["scope"], record["provider_id"]) == (
            "running",
            "provider",
            "ZZRAIL",
        )
        assert record["current"][0]["key"] == "provider[ZZRAIL].timetable(gtfs)"
        return httpx.Response(200, content=_gtfs_bytes(padding=99_999))

    _mock_downloads(monkeypatch, handler)
    r = client.post(f"/api/sessions/{SID}/providers/ZZRAIL/refresh", headers=admin)
    assert r.status_code == 200, r.text
    assert seen == ["/zzrail.zip"]
    body = client.get(f"/api/sessions/{SID}/sources/progress", headers=admin).json()
    [single] = body["single"]
    assert (single["state"], single["total"], single["done"], single["fetched"]) == (
        "finished",
        1,
        1,
        1,
    )
    assert single["extract"]["state"] == "queued"
    # The whole session's last result stays as it was.
    assert (body["providers"]["state"], body["providers"]["error"]) == ("failed", "zzerror")
    assert _SECRET_MARK not in json.dumps(body)
    assert _HOST not in json.dumps(body)

    # Its station-module pass is bound to this refresh's run, not the session's.
    run_id = _run_id("ZZRAIL")
    assert queued == [((SID, "ZZRAIL"), {"run_id": run_id})]

    def one_feed(sid: str, *, observer: Any, only_provider: str | None, **_k: Any) -> Any:
        assert (sid, only_provider) == (SID, "ZZRAIL")
        observer.started(1)
        observer.feed_started("ZZRAIL")
        assert _read()["single"][0]["extract"]["current"] == "ZZRAIL"
        observer.outcome(up.FeedOutcome("ZZRAIL", up.SKIPPED, up.BUDGET))
        observer.finished("finished")
        return up.PassResult()

    monkeypatch.setattr(up, "send_session", one_feed)
    up.send_after_refresh(SID, "ZZRAIL", run_id=run_id)
    after = _read()
    extract = after["single"][0]["extract"]
    assert (extract["state"], extract["total"], extract["skipped"]) == ("finished", 1, 1)
    assert extract["reasons"] == {"budget": 1}
    assert after["providers"]["extract"] is None


@pytest.mark.usefixtures("inbox", "no_station_module")
def test_who_may_run_beside_a_provider_card_refresh(
    client: TestClient, admin: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import refresh_progress

    _make_session(client, admin, ["ZZRAIL", "ZZBUS"])
    single = refresh_progress.claim(SID, refresh_progress.provider_scope("ZZRAIL"), 1)
    started = _read()["single"][0]["started_at"]

    # The same provider again: refused, its record untouched.
    r = client.post(f"/api/sessions/{SID}/providers/ZZRAIL/refresh", headers=admin)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert (detail["error"], detail["scope"], detail["provider_id"]) == (
        "refresh_already_running",
        "provider",
        "ZZRAIL",
    )
    assert detail["started_at"] == started
    assert "provider ZZRAIL" in detail["message"]
    assert _read()["single"][0]["state"] == "running"

    # The whole session's refresh: refused (it would download ZZRAIL too).
    r = client.post(f"/api/sessions/{SID}/sources/refresh", headers=admin)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert (detail["error"], detail["scope"], detail["provider_id"]) == (
        "provider_refresh_running",
        "provider",
        "ZZRAIL",
    )
    assert detail["started_at"] == started
    assert _read()["providers"] is None

    # Another provider alone, and OSM, may run beside it.
    _mock_downloads(monkeypatch, lambda _r: httpx.Response(200, content=_gtfs_bytes()))
    r = client.post(f"/api/sessions/{SID}/providers/ZZBUS/refresh", headers=admin)
    assert r.status_code == 200, r.text
    refresh_progress.claim(SID, refresh_progress.OSM, 1).finish(refresh_progress.FINISHED)
    shown = [(rec["provider_id"], rec["state"]) for rec in _read()["single"]]
    assert shown == [("ZZRAIL", "running"), ("ZZBUS", "finished")]

    # A dead card refresh blocks nothing; the next one says what it replaced.
    _age_heartbeat(refresh_progress.LEASE_SECONDS // 60 + 1)
    whole = refresh_progress.claim(SID, refresh_progress.PROVIDERS, 2)
    scope = refresh_progress.provider_scope("ZZRAIL")
    with pytest.raises(refresh_progress.RefreshRunning) as refused:
        refresh_progress.claim(SID, scope, 1)
    assert refused.value.detail()["error"] == "refresh_already_running"
    assert refused.value.detail()["scope"] == "providers"
    whole.finish(refresh_progress.FINISHED)
    taker = refresh_progress.claim(SID, refresh_progress.provider_scope("ZZRAIL"), 1)
    record = _read()["single"][0]
    assert record["previous"]["state"] == "interrupted"
    # The dead refresh's late writes change nothing.
    single.done("provider[ZZRAIL].timetable(gtfs)", {"status": "fetched"})
    single.finish(refresh_progress.FINISHED)
    assert (_read()["single"][0]["state"], _read()["single"][0]["done"]) == ("running", 0)
    taker.finish(refresh_progress.FINISHED)


def _edit_config_meanwhile(note: int) -> None:
    """Another request saves the session's config (a key of its own)."""
    from sqlalchemy.orm.attributes import flag_modified

    from app.db import SessionLocal
    from app.models import Session as SessionRow

    with SessionLocal() as other:
        row = other.get(SessionRow, SID)
        assert row is not None
        row.config = {**row.config, f"zz_note_{note}": 99}
        flag_modified(row, "config")
        other.commit()


@pytest.mark.parametrize("route", ["providers/ZZRAIL/refresh", "sources/refresh"])
@pytest.mark.usefixtures("inbox", "no_station_module")
def test_a_config_saved_during_a_download_is_kept(
    client: TestClient, admin: dict[str, str], monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    """The refresh writes the session's config after its downloads: it
    re-reads the row under a lock first, so a save made meanwhile (another
    card's refresh, the config form) is not overwritten."""
    from app.api.admin import sessions as sessions_api
    from app.db import SessionLocal
    from app.models import Session as SessionRow

    _make_session(client, admin, ["ZZRAIL"])
    _mock_downloads(monkeypatch, lambda _r: httpx.Response(200, content=_gtfs_bytes()))
    assert client.post(f"/api/sessions/{SID}/{route}", headers=admin).status_code == 200
    # The same file again ("unchanged": nothing in the request commits before
    # the refresh writes the config); the config is saved while it runs.
    edits = iter(range(99))
    real_task = sessions_api._refresh_one_task

    async def task_then_save(*args: Any) -> dict[str, Any]:
        outcome = await real_task(*args)
        _edit_config_meanwhile(next(edits))
        return outcome

    monkeypatch.setattr(sessions_api, "_refresh_one_task", task_then_save)
    r = client.post(f"/api/sessions/{SID}/{route}", headers=admin)
    assert r.status_code == 200, r.text
    assert [u["key"] for u in r.json()["unchanged"]] == ["provider[ZZRAIL].timetable(gtfs)"]
    with SessionLocal() as db:
        row = db.get(SessionRow, SID)
        assert row is not None
        assert row.config["zz_note_0"] == 99
        # The refresh's own change is there too.
        assert row.config["_meta"]["last_refresh_completed_at"]
        assert row.config["sources"]["providers"][0]["id"] == "ZZRAIL"


@pytest.mark.usefixtures("inbox", "no_station_module")
def test_a_config_saved_during_an_osm_download_is_kept(
    client: TestClient, admin: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #366: the OSM refresh writes the config after its download
    too (the refresh time): it re-reads the row under a lock first."""
    from app.api.admin import sessions as sessions_api
    from app.db import SessionLocal
    from app.models import Session as SessionRow

    _make_session(client, admin, ["ZZRAIL"])
    with SessionLocal() as db:
        s = db.get(SessionRow, SID)
        assert s is not None
        s.config = {**s.config, "sources": {**s.config["sources"], "osm_pbf": _url("osm")}}
        db.commit()

    async def fetched_while_saved(*_args: Any) -> dict[str, Any]:
        _edit_config_meanwhile(0)
        return {"status": "fetched", "key": "osm_pbf"}

    monkeypatch.setattr(sessions_api, "_refresh_one_task", fetched_while_saved)
    r = client.post(f"/api/sessions/{SID}/sources/osm/refresh", headers=admin)
    assert r.status_code == 200, r.text
    assert [f["key"] for f in r.json()["fetched"]] == ["osm_pbf"]
    with SessionLocal() as db:
        row = db.get(SessionRow, SID)
        assert row is not None
        assert row.config["zz_note_0"] == 99
        assert row.config["_meta"]["last_refresh_completed_at"]
        assert row.config["sources"]["osm_pbf"] == _url("osm")


@pytest.mark.usefixtures("fresh_db")
def test_a_card_record_stays_while_its_follow_up_runs(
    client: TestClient, admin: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review round 1: a card's record whose station-module pass is still
    queued or running is shown (and so polled) whatever the session's
    refresh does; only a finished session refresh supersedes it."""
    from app import refresh_progress, station_module

    _make_session(client, admin, ["ZZRAIL"])
    monkeypatch.setattr(station_module, "enabled", lambda: True)
    card = refresh_progress.claim(SID, "ZZRAIL", 1)
    card.finish(refresh_progress.FINISHED)
    whole = refresh_progress.claim(SID, refresh_progress.PROVIDERS, 1)

    def single() -> list[tuple[str, str | None]]:
        return [(r["provider_id"], (r["extract"] or {}).get("state")) for r in _read()["single"]]

    # The session's refresh runs: the card's queued pass is still shown.
    assert single() == [("ZZRAIL", "queued")]
    body = client.get(f"/api/sessions/{SID}/sources/progress", headers=admin).json()
    assert body["providers"]["state"] == "running"
    follow = refresh_progress.ExtractFollowUp(SID, card.run_id, scope="ZZRAIL")
    follow.started(1)
    follow._stop.set()  # no beat in this test
    # Finished after the card, while the card's pass runs: still shown.
    whole.finish(refresh_progress.FINISHED)
    assert single() == [("ZZRAIL", "running")]
    # The pass ended: the finished session refresh supersedes the card.
    follow.finished("finished")
    assert single() == []


@pytest.mark.usefixtures("fresh_db")
def test_only_a_finished_session_refresh_supersedes_a_card(
    client: TestClient, admin: dict[str, str]
) -> None:
    from app import refresh_progress

    _make_session(client, admin, ["ZZRAIL"])
    refresh_progress.claim(SID, "ZZRAIL", 1).finish(refresh_progress.FINISHED)
    whole = refresh_progress.claim(SID, refresh_progress.PROVIDERS, 2)
    whole.finish(refresh_progress.FAILED, error="ZzError")
    # Failed (it may never have reached ZZRAIL): the card stays.
    assert [r["provider_id"] for r in _read()["single"]] == ["ZZRAIL"]
    refresh_progress.claim(SID, refresh_progress.PROVIDERS, 2)
    _age_heartbeat(refresh_progress.LEASE_SECONDS // 60 + 1)
    assert _read()["providers"]["state"] == "interrupted"
    assert [r["provider_id"] for r in _read()["single"]] == ["ZZRAIL"]


@pytest.mark.usefixtures("inbox")
def test_a_removed_provider_s_record_is_not_shown(
    client: TestClient, admin: dict[str, str]
) -> None:
    from sqlalchemy.orm.attributes import flag_modified

    from app import refresh_progress
    from app.db import SessionLocal
    from app.models import Session as SessionRow

    _make_session(client, admin, ["ZZRAIL", "ZZBUS"])
    refresh_progress.claim(SID, "ZZRAIL", 1).finish(refresh_progress.FINISHED)

    def shown() -> list[str]:
        body = client.get(f"/api/sessions/{SID}/sources/progress", headers=admin).json()
        return [r["provider_id"] for r in body["single"]]

    assert shown() == ["ZZRAIL"]
    with SessionLocal() as db:
        s = db.get(SessionRow, SID)
        assert s is not None
        s.config = {"sources": {"providers": [_provider("ZZBUS")]}}
        flag_modified(s, "config")
        db.commit()
    assert shown() == []


@pytest.mark.usefixtures("inbox", "no_station_module")
def test_a_running_refresh_is_said_before_a_card_with_nothing_to_download(
    client: TestClient, admin: dict[str, str]
) -> None:
    """The order before this change: 409 first, then the 400 "no URLs"."""
    from sqlalchemy.orm.attributes import flag_modified

    from app import refresh_progress
    from app.db import SessionLocal
    from app.models import Session as SessionRow

    _make_session(client, admin, [])
    with SessionLocal() as db:
        s = db.get(SessionRow, SID)
        assert s is not None
        s.config = {
            "sources": {
                "providers": [
                    {
                        "id": "ZZEMPTY",
                        "country_iso": "ZZ",
                        "timetable": {"format": "gtfs", "source": "upload"},
                    }
                ]
            }
        }
        flag_modified(s, "config")
        db.commit()
    route = f"/api/sessions/{SID}/providers/ZZEMPTY/refresh"
    first = client.post(route, headers=admin)
    assert first.status_code == 400, first.text
    whole = refresh_progress.claim(SID, refresh_progress.PROVIDERS, 1)
    r = client.post(route, headers=admin)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["scope"] == "providers"
    whole.finish(refresh_progress.FINISHED)
    card = refresh_progress.claim(SID, "ZZEMPTY", 1)
    r = client.post(route, headers=admin)
    assert (r.status_code, r.json()["detail"]["provider_id"]) == (409, "ZZEMPTY")
    card.finish(refresh_progress.FINISHED)
    assert client.post(route, headers=admin).status_code == 400
    # Never claimed by the refused requests: the card record is the test's own.
    assert _read()["single"][0]["total"] == 1


@pytest.mark.usefixtures("fresh_db")
def test_a_session_claim_and_a_card_claim_at_once_have_one_winner(
    client: TestClient, admin: dict[str, str]
) -> None:
    from app import refresh_progress

    _make_session(client, admin, ["ZZRAIL"])
    scopes = [refresh_progress.PROVIDERS, "ZZRAIL"] * 3
    barrier = threading.Barrier(len(scopes))
    won: list[str] = []
    refused: list[str] = []

    def contend(scope: str) -> None:
        barrier.wait()
        try:
            refresh_progress.claim(SID, scope, 1)
            won.append(scope)
        except refresh_progress.RefreshRunning as exc:
            refused.append(exc.scope)

    threads = [threading.Thread(target=contend, args=(scope,)) for scope in scopes]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(won) == 1
    assert refused == [won[0]] * (len(scopes) - 1)


@pytest.mark.usefixtures("fresh_db")
def test_a_claim_waits_for_the_sessions_claim_lock(
    client: TestClient, admin: dict[str, str]
) -> None:
    """The rules between records hold because a claim checks the other
    records and takes its row under the session's advisory lock: while
    another transaction holds that lock, a claim waits."""
    from app import refresh_progress
    from app.db import SessionLocal

    _make_session(client, admin, ["ZZRAIL"])
    claimed = threading.Event()
    with SessionLocal() as holder:
        holder.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"refresh_progress:{SID}"}
        )
        waiter = threading.Thread(
            target=lambda: (refresh_progress.claim(SID, "ZZRAIL", 1), claimed.set())
        )
        waiter.start()
        assert not claimed.wait(0.5)
        holder.commit()
    assert claimed.wait(10)
    waiter.join()


@pytest.mark.usefixtures("fresh_db")
def test_a_card_pass_never_writes_into_another_record(
    client: TestClient, admin: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import refresh_progress, station_module

    _make_session(client, admin, ["ZZRAIL"])
    monkeypatch.setattr(station_module, "enabled", lambda: True)
    first = refresh_progress.claim(SID, "ZZRAIL", 0)
    first.finish(refresh_progress.FINISHED)
    # The right run under the wrong scope, and the session's scope: nothing.
    refresh_progress.ExtractFollowUp(SID, first.run_id).started(1)
    refresh_progress.ExtractFollowUp(SID, first.run_id, scope="ZZBUS").started(1)
    assert _read()["single"][0]["extract"]["state"] == "queued"
    # A newer card refresh of the same provider owns the row: the older
    # refresh's pass writes nothing into it.
    late = refresh_progress.ExtractFollowUp(SID, first.run_id, scope="ZZRAIL")
    second = refresh_progress.claim(SID, "ZZRAIL", 0)
    late.started(1)
    assert _read()["single"][0]["extract"] is None
    second.finish(refresh_progress.FINISHED)
    late.finished("finished")
    assert _read()["single"][0]["extract"]["state"] == "queued"


@pytest.mark.usefixtures("inbox", "no_station_module")
def test_a_derived_provider_card_refresh_is_recorded_without_a_follow_up(
    client: TestClient, admin: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy.orm.attributes import flag_modified

    from app import station_module
    from app.db import SessionLocal
    from app.models import Session as SessionRow

    _make_session(client, admin, [])
    with SessionLocal() as db:
        s = db.get(SessionRow, SID)
        assert s is not None
        s.config = {
            "sources": {
                "providers": [
                    {
                        "id": "ZZXB",
                        "timetable": {
                            "format": "gtfs",
                            "source": "cross_border_filter",
                            "derived_from": {"session_id": "zz-none", "provider_id": "ZZRAIL"},
                        },
                    }
                ]
            }
        }
        flag_modified(s, "config")
        db.commit()
    monkeypatch.setattr(station_module, "enabled", lambda: True)
    r = client.post(f"/api/sessions/{SID}/providers/ZZXB/refresh", headers=admin)
    assert r.status_code == 200, r.text
    [record] = _read()["single"]
    assert (record["provider_id"], record["total"], record["done"], record["failed"]) == (
        "ZZXB",
        1,
        1,
        1,
    )
    assert record["failed_keys"] == ["provider[ZZXB].cross_border_filter"]
    assert record["extract"] is None


@pytest.mark.usefixtures("fresh_db")
def test_which_card_records_the_page_shows(client: TestClient, admin: dict[str, str]) -> None:
    from app import refresh_progress
    from app.db import SessionLocal

    _make_session(client, admin, ["ZZRAIL"])

    def started(scope: str, minutes: int) -> None:
        with SessionLocal() as db:
            db.execute(
                text(
                    "UPDATE source_refresh_progress SET started_at = now() - "
                    "make_interval(mins => :m) WHERE scope = :c"
                ),
                {"m": minutes, "c": scope},
            )
            db.commit()

    for pid, minutes in (("ZZOLD", 50), ("ZZNEW", 30)):
        refresh_progress.claim(SID, pid, 1).finish(refresh_progress.FINISHED)
        started(pid, minutes)
    # The newest ended one only.
    assert [r["provider_id"] for r in _read()["single"]] == ["ZZNEW"]
    # Older than the whole session's latest refresh: superseded, not shown.
    refresh_progress.claim(SID, refresh_progress.PROVIDERS, 1).finish(refresh_progress.FINISHED)
    started(refresh_progress.PROVIDERS, 10)
    assert _read()["single"] == []
    # Running ones are always shown, newest first, at most SINGLE_SHOWN_MAX.
    for n in range(refresh_progress.SINGLE_SHOWN_MAX + 1):
        refresh_progress.claim(SID, f"ZZRUN{n}", 1)
        started(f"ZZRUN{n}", 9 - n)
    shown = [r["provider_id"] for r in _read()["single"]]
    assert shown == [f"ZZRUN{n}" for n in range(refresh_progress.SINGLE_SHOWN_MAX, 0, -1)]
    # A row of a scope this version does not know is never shown.
    with SessionLocal() as db:
        db.execute(
            text(
                "INSERT INTO source_refresh_progress (session_id, scope, run_id, state, "
                "started_at, heartbeat_at, finished_at) VALUES (:s, 'zzlegacy', :r, "
                "'finished', now(), now(), now())"
            ),
            {"s": SID, "r": uuid.uuid4()},
        )
        db.commit()
    body = _read()
    assert "zzlegacy" not in json.dumps(body)
    assert body["providers"]["provider_id"] is None


@pytest.mark.usefixtures("inbox", "no_station_module")
def test_an_osm_refresh_that_breaks_while_rotating_is_recorded_failed(
    client: TestClient, admin: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api.admin import sessions as sessions_api
    from app.db import SessionLocal
    from app.main import app
    from app.models import Session as SessionRow

    _make_session(client, admin, ["ZZRAIL"])
    with SessionLocal() as db:
        s = db.get(SessionRow, SID)
        assert s is not None
        s.config = {**s.config, "sources": {**s.config["sources"], "osm_pbf": _url("osm")}}
        db.commit()

    def broken(_inbox: Path) -> list[str]:
        raise OSError("stand-in")

    monkeypatch.setattr(sessions_api, "_rotate_osm_pbf", broken)
    with TestClient(app, raise_server_exceptions=False) as quiet:
        r = quiet.post(f"/api/sessions/{SID}/sources/osm/refresh", headers=admin)
    assert r.status_code == 500
    osm = _read()["osm"]
    assert osm["state"] == "failed"
    assert osm["error"] == "oserror"


@pytest.mark.usefixtures("fresh_db")
def test_two_claims_at_once_have_one_winner(client: TestClient, admin: dict[str, str]) -> None:
    from app import refresh_progress

    _make_session(client, admin, ["ZZRAIL"])
    barrier = threading.Barrier(4)
    won: list[uuid.UUID] = []
    refused: list[str] = []

    def contend() -> None:
        barrier.wait()
        try:
            won.append(refresh_progress.claim(SID, refresh_progress.PROVIDERS, 1).run_id)
        except refresh_progress.RefreshRunning:
            refused.append("409")

    threads = [threading.Thread(target=contend) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(won) == 1
    assert refused == ["409"] * 3


# ── the lease ────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("inbox", "no_station_module")
def test_a_dead_refresh_is_interrupted_and_taken_over(
    client: TestClient, admin: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import refresh_progress

    _make_session(client, admin, ["ZZRAIL"])
    dead = refresh_progress.claim(SID, refresh_progress.PROVIDERS, 1)
    with dead.task("provider[ZZRAIL].timetable(gtfs)"):
        dead.bytes_received(99, 999)

    # Fresh heartbeat: still running for every reader, and refused.
    assert _read()["providers"]["state"] == "running"
    assert client.post(f"/api/sessions/{SID}/sources/refresh", headers=admin).status_code == 409

    # No heartbeat for longer than the lease: the process is gone.
    _age_heartbeat(refresh_progress.LEASE_SECONDS // 60 + 1)
    shown = _read()["providers"]
    assert shown["state"] == "interrupted"
    assert shown["finished_at"] == shown["heartbeat_at"]
    assert shown["done"] == 0
    # The task it died in is no longer "now" (review round 1): no current task.
    assert shown["current"] == []

    # A claim takes the row over while the dead refresh's writes change
    # nothing (the new refresh is running too: only the run_id tells them apart).
    taker = refresh_progress.claim(SID, refresh_progress.PROVIDERS, 1)
    dead.done("provider[ZZRAIL].timetable(gtfs)", {"status": "fetched"})
    assert _read()["providers"]["done"] == 0
    taker.finish(refresh_progress.FINISHED)
    # A finished refresh's own late write changes nothing either.
    taker.done("provider[ZZRAIL].timetable(gtfs)", {"status": "fetched"})
    assert _read()["providers"]["done"] == 0
    _age_heartbeat(0)
    refresh_progress.claim(SID, refresh_progress.PROVIDERS, 1)
    _age_heartbeat(refresh_progress.LEASE_SECONDS // 60 + 1)
    shown = _read()["providers"]

    # The next refresh takes the row over and says what it replaced.
    _mock_downloads(monkeypatch, lambda _r: httpx.Response(200, content=_gtfs_bytes()))
    r = client.post(f"/api/sessions/{SID}/sources/refresh", headers=admin)
    assert r.status_code == 200, r.text
    after = _read()["providers"]
    assert after["state"] == "finished"
    assert after["fetched"] == 1
    assert after["previous"] == {"state": "interrupted", "started_at": shown["started_at"]}

    # The dead refresh comes back to life: its writes change nothing.
    dead.done("provider[ZZRAIL].timetable(gtfs)", {"status": "skipped"})
    dead.finish(refresh_progress.FAILED, error="ZombieError")
    final = _read()["providers"]
    assert final["state"] == "finished"
    assert (final["done"], final["fetched"], final["failed"]) == (1, 1, 0)
    assert final["error"] is None
    assert final["finished_at"] == after["finished_at"]


@pytest.mark.usefixtures("fresh_db")
def test_a_stale_record_is_taken_over_by_a_claim_without_a_read(
    client: TestClient, admin: dict[str, str]
) -> None:
    from app import refresh_progress

    _make_session(client, admin, ["ZZRAIL"])
    refresh_progress.claim(SID, refresh_progress.OSM, 1)
    _age_heartbeat(refresh_progress.LEASE_SECONDS // 60 + 1)
    refresh_progress.claim(SID, refresh_progress.OSM, 1)
    osm = _read()["osm"]
    assert osm["state"] == "running"
    assert osm["previous"]["state"] == "interrupted"


# ── the end of a refresh: failed, interrupted ────────────────────────────


@pytest.mark.usefixtures("inbox", "no_station_module")
def test_a_refresh_that_raises_is_recorded_failed(
    client: TestClient, admin: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api.admin import sessions as sessions_api

    _make_session(client, admin, ["ZZRAIL", "ZZBUS"])

    async def broken(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError(f"boom {_url('zz')}")

    monkeypatch.setattr(sessions_api, "_gather_refresh_outcomes", broken)
    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as quiet:
        r = quiet.post(f"/api/sessions/{SID}/sources/refresh", headers=admin)
    assert r.status_code == 500
    record = _read()["providers"]
    assert record["state"] == "failed"
    assert record["error"] == "runtimeerror"
    assert record["not_reached"] == 2
    assert record["extract"] is None
    assert _SECRET_MARK not in json.dumps(record)


@pytest.mark.usefixtures("fresh_db")
def test_a_cancelled_refresh_is_recorded_interrupted(
    client: TestClient, admin: dict[str, str]
) -> None:
    from app import refresh_progress

    _make_session(client, admin, ["ZZRAIL"])

    async def cancelled() -> None:
        run = refresh_progress.claim(SID, refresh_progress.PROVIDERS, 2)
        async with run:
            run.done("provider[ZZRAIL].timetable(gtfs)", {"status": "fetched"})
            raise asyncio.CancelledError

    refresh = cancelled()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(refresh)
    record = _read()["providers"]
    assert record["state"] == "interrupted"
    assert (record["done"], record["not_reached"]) == (1, 1)


# ── the station-module follow-up ─────────────────────────────────────────


@pytest.mark.usefixtures("fresh_db")
def test_the_extract_follow_up_is_counted_on_the_latest_refresh(
    client: TestClient, admin: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import refresh_progress, station_module
    from app import station_extract_upload as up

    _make_session(client, admin, ["ZZRAIL"])
    monkeypatch.setattr(station_module, "enabled", lambda: True)
    run = refresh_progress.claim(SID, refresh_progress.PROVIDERS, 0)
    run.finish(refresh_progress.FINISHED)
    queued = _read()["providers"]["extract"]
    assert queued["state"] == "queued"
    assert queued["updated_at"] is not None

    follow = refresh_progress.ExtractFollowUp(SID, run.run_id)
    follow.started(5)
    follow.feed_started("ZZRAIL")
    running = _read()["providers"]["extract"]
    assert running["state"] == "running"
    assert running["current"] == "ZZRAIL"
    for state, reason in (
        (up.SENT, None),
        (up.HELD, None),
        (up.SKIPPED, up.BUDGET),
        (up.SKIPPED, up.BUDGET),
        (up.UNAVAILABLE, up.NETWORK),
    ):
        follow.outcome(up.FeedOutcome("ZZRAIL", state, reason))
    follow.finished("finished")
    extract = _read()["providers"]["extract"]
    assert extract["state"] == "finished"
    assert (extract["total"], extract["handled"]) == (5, 5)
    assert (extract["sent"], extract["held"], extract["skipped"], extract["unavailable"]) == (
        1,
        1,
        2,
        1,
    )
    assert extract["reasons"] == {"budget": 2, "network": 1}
    assert extract["current"] is None
    assert extract["finished_at"] is not None


@pytest.mark.usefixtures("fresh_db")
def test_a_pass_queued_by_one_refresh_never_writes_into_the_next(
    client: TestClient, admin: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import refresh_progress, station_module
    from app import station_extract_upload as up

    _make_session(client, admin, ["ZZRAIL"])
    monkeypatch.setattr(station_module, "enabled", lambda: True)
    first = refresh_progress.claim(SID, refresh_progress.PROVIDERS, 0)
    first.finish(refresh_progress.FINISHED)
    late = refresh_progress.ExtractFollowUp(SID, first.run_id)

    # Refresh B runs: neither A's pass nor a pass with B's id may write.
    second = refresh_progress.claim(SID, refresh_progress.PROVIDERS, 0)
    late.started(1)
    early = refresh_progress.ExtractFollowUp(SID, second.run_id)
    early.started(1)
    assert _read()["providers"]["extract"] is None
    second.finish(refresh_progress.FINISHED)
    late.outcome(up.FeedOutcome("ZZRAIL", up.SENT))
    late.finished("finished")
    assert _read()["providers"]["extract"]["state"] == "queued"


def _age_extract(minutes: int) -> None:
    from datetime import UTC, datetime, timedelta

    from app.db import SessionLocal

    when = (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat(timespec="seconds")
    with SessionLocal() as db:
        db.execute(
            text(
                "UPDATE source_refresh_progress "
                "SET extract = jsonb_set(extract, '{updated_at}', to_jsonb(CAST(:w AS text)))"
            ),
            {"w": when},
        )
        db.commit()


@pytest.mark.usefixtures("fresh_db")
def test_a_silent_follow_up_is_shown_interrupted(
    client: TestClient, admin: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import refresh_progress, station_module

    _make_session(client, admin, ["ZZRAIL"])
    monkeypatch.setattr(station_module, "enabled", lambda: True)
    run = refresh_progress.claim(SID, refresh_progress.PROVIDERS, 0)
    run.finish(refresh_progress.FINISHED)
    # Queued: the pass budget is 30 minutes, so 35 without a start means the
    # web process lost its queue.
    _age_extract(34)
    assert _read()["providers"]["extract"]["state"] == "queued"
    _age_extract(36)
    assert _read()["providers"]["extract"]["state"] == "interrupted"

    follow = refresh_progress.ExtractFollowUp(SID, run.run_id)
    follow.started(3)
    follow._stop.set()  # no beat in this test: the clock is moved by hand
    _age_extract(4)
    assert _read()["providers"]["extract"]["state"] == "running"
    _age_extract(6)
    assert _read()["providers"]["extract"]["state"] == "interrupted"


# ── the table goes with its session ──────────────────────────────────────


@pytest.mark.usefixtures("inbox")
def test_deleting_the_session_deletes_its_records(
    client: TestClient, admin: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import refresh_progress, sessions_orchestrator
    from app.api.admin import sessions as sessions_api
    from app.db import SessionLocal
    from app.settings import settings as live

    # The delete regenerates the compose / nginx fragments, touches the
    # worker's reload trigger and removes the session's graph folders: all
    # under /data in production, here in the test's own folder (CI does not
    # run as root).
    monkeypatch.setattr(sessions_api, "_RELOAD_TRIGGER", tmp_path / "generated" / ".reload-trigger")
    regenerate = sessions_orchestrator.regenerate
    monkeypatch.setattr(
        sessions_orchestrator,
        "regenerate",
        lambda db, **_k: regenerate(db, output_dir=tmp_path / "generated"),
    )
    monkeypatch.setattr(live, "graph_dir", tmp_path / "graphs")
    (tmp_path / "generated").mkdir()

    _make_session(client, admin, ["ZZRAIL"])
    refresh_progress.claim(SID, refresh_progress.OSM, 1).finish(refresh_progress.FINISHED)
    r = client.delete(f"/api/sessions/{SID}", headers=admin)
    assert r.status_code in (200, 204), r.text
    with SessionLocal() as db:
        assert db.execute(text("SELECT count(*) FROM source_refresh_progress")).scalar_one() == 0


# ── the revision ─────────────────────────────────────────────────────────

_REVISION = "20261015_1800_refresh_progress"
_BEFORE = "20261015_1200_hub_station_id"


def _revision_module() -> Any:
    import importlib.util

    path = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{_REVISION}.py"
    spec = importlib.util.spec_from_file_location("refresh_progress_revision", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_revision_goes_up_down_up_and_reruns() -> None:
    """The table appears with its CHECK, the downgrade drops it and nothing
    else, and an upgrade over an existing table (a re-run) does nothing."""
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy.exc import IntegrityError

    url = _postgres_or_skip()
    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "alembic")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE;"))
        conn.execute(text("CREATE SCHEMA public;"))
    command.upgrade(cfg, _BEFORE)
    before = set(sa_inspect(engine).get_table_names())
    assert "source_refresh_progress" not in before

    command.upgrade(cfg, _REVISION)
    assert set(sa_inspect(engine).get_table_names()) == before | {"source_refresh_progress"}
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (id, email, name, password_hash, role) "
                "VALUES ('99999999-9999-4999-8999-999999999999', 'zz@zz.invalid', 'ZZ', 'x', "
                "'platform_admin')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO sessions (id, name, category, state, created_by) "
                "VALUES ('zz-s', 'ZZ', 'NAP', 'created', '99999999-9999-4999-8999-999999999999')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO source_refresh_progress "
                "(session_id, scope, run_id, state, started_at, heartbeat_at) "
                "VALUES ('zz-s', 'osm', gen_random_uuid(), 'finished', now(), now())"
            )
        )
        detail = conn.execute(text("SELECT detail, extract FROM source_refresh_progress")).one()
    assert tuple(detail) == ({}, {})
    refused_state = text("UPDATE source_refresh_progress SET state = 'zz'")
    with engine.connect() as conn, pytest.raises(IntegrityError):
        conn.execute(refused_state)

    # A re-run of the upgrade over the existing table changes nothing.
    command.stamp(cfg, _BEFORE)
    command.upgrade(cfg, _REVISION)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM source_refresh_progress")).scalar() == 1

    command.downgrade(cfg, _BEFORE)
    assert set(sa_inspect(engine).get_table_names()) == before
    command.upgrade(cfg, "head")
    assert "source_refresh_progress" in sa_inspect(engine).get_table_names()


def test_the_models_check_is_the_revisions_check() -> None:
    from app.models.runtime import REFRESH_STATE_CHECK, REFRESH_STATES

    assert _revision_module().STATE_CHECK == REFRESH_STATE_CHECK
    from app import refresh_progress

    assert REFRESH_STATES == (
        refresh_progress.RUNNING,
        refresh_progress.FINISHED,
        refresh_progress.FAILED,
        refresh_progress.INTERRUPTED,
    )
