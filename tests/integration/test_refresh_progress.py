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
import uuid
import zipfile
from collections.abc import AsyncIterator, Iterator
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


def _provider(pid: str) -> dict[str, Any]:
    return {
        "id": pid,
        "country_iso": "ZZ",
        "timetable": {"format": "gtfs", "url": _url(pid.lower())},
    }


def _make_session(client: TestClient, admin: dict[str, str], pids: list[str]) -> None:
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
        s.config = {"sources": {"providers": [_provider(p) for p in pids]}}
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

    async def big_body() -> AsyncIterator[bytes]:
        for start in range(0, len(big), MIB):
            if start:
                # The previous chunk was reported; its write goes through the
                # run's writer thread: a new request (a reload) reads it from
                # the database once it has landed.
                for _ in range(300):
                    record = _read()
                    current = record["providers"]["current"]
                    if current and current[0]["bytes"] == start:
                        break
                    await asyncio.sleep(0.01)
                seen.append(record)
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
    assert first["current"] == [
        {"key": "provider[ZZBIG].timetable(gtfs)", "bytes": MIB, "expected": len(big)}
    ]
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
    assert r.json()["detail"]["error"] == "refresh_already_running"
    assert r.json()["detail"]["started_at"] == started
    # A dead session refresh (past its lease) does not block the card.
    _age_heartbeat(refresh_progress.LEASE_SECONDS // 60 + 1)
    _mock_downloads(monkeypatch, lambda _r: httpx.Response(200, content=_gtfs_bytes()))
    r = client.post(f"/api/sessions/{SID}/providers/ZZRAIL/refresh", headers=admin)
    assert r.status_code == 200, r.text
    # Nor does a finished one; the card's refresh writes no record.
    run.finish(refresh_progress.FINISHED)
    assert (
        client.post(f"/api/sessions/{SID}/providers/ZZRAIL/refresh", headers=admin).status_code
        == 200
    )
    record = _read()["providers"]
    assert (record["state"], record["done"], record["fetched"]) == ("finished", 0, 0)


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
    assert shown["current"][0]["bytes"] == 99

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
