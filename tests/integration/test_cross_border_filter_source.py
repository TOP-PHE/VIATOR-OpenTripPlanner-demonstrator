"""Integration: the `cross_border_filter` derived-provider source (§12).

A corridors-session provider owns no feed — it links to a national provider's
slot in another session and the cross-border filter runs on it at refresh,
landing the filtered subset at the corridors provider's own slot. Here we stage
a synthetic Renfe feed (one ES→FR route + one domestic ES route) at a national
slot, configure a derived provider that points at it, and assert refresh
materialises the cross-border subset.

Fresh-DB harness mirrors the other integration tests. Skips when Postgres is down.
"""

from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from alembic import command

_BOOTSTRAP = "test-bootstrap-token"


def _postgres_or_skip() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgresql"):
        pytest.skip("DATABASE_URL is not Postgres; skipping cross-border-filter test")
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
def client(fresh_db: str):
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


def _cross_border_gtfs_bytes() -> bytes:
    """A tiny Renfe-like feed: one ES→FR route (kept) + one domestic ES route
    (dropped by the cross-border filter)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(
            "agency.txt",
            "agency_id,agency_name,agency_url,agency_timezone\nRENFE,Renfe,https://renfe.es,Europe/Madrid\n",
        )
        z.writestr(
            "stops.txt",
            "stop_id,stop_name,stop_lat,stop_lon\n"
            "7100001,Barcelona Sants,41.38,2.14\n"  # ES (71)
            "8700001,Lyon Part-Dieu,45.76,4.86\n"  # FR (87)
            "7100002,Madrid Atocha,40.41,-3.69\n",  # ES (71)
        )
        z.writestr(
            "routes.txt",
            "route_id,agency_id,route_short_name,route_type\n"
            "AVE-XB,RENFE,AVE,2\n"  # cross-border ES->FR
            "AVE-DOM,RENFE,AVE,2\n",  # domestic ES only
        )
        z.writestr(
            "trips.txt",
            "route_id,service_id,trip_id\nAVE-XB,WD,T-XB\nAVE-DOM,WD,T-DOM\n",
        )
        z.writestr(
            "stop_times.txt",
            "trip_id,stop_sequence,stop_id,arrival_time,departure_time\n"
            "T-XB,1,7100001,08:00,08:00\n"  # Barcelona (ES) — origin
            "T-XB,2,8700001,13:00,13:00\n"  # Lyon (FR)
            "T-DOM,1,7100001,09:00,09:00\n"  # Barcelona (ES)
            "T-DOM,2,7100002,12:00,12:00\n",  # Madrid (ES) — domestic
        )
    return buf.getvalue()


def _stage_national_feed(inbox: Path, *, session_id: str, provider_id: str) -> None:
    slot = inbox / session_id / "gtfs" / f"{provider_id.lower()}.zip"
    slot.parent.mkdir(parents=True, exist_ok=True)
    slot.write_bytes(_cross_border_gtfs_bytes())


def _make_corridors_session(
    client: TestClient, admin: dict[str, str], sid: str, *, derived_from: dict
) -> None:
    r = client.post(
        "/api/sessions",
        headers=admin,
        json={"id": sid, "name": "EU corridors test", "category": "NAP", "config": {}},
    )
    assert r.status_code == 201, r.text

    from sqlalchemy.orm.attributes import flag_modified

    from app.db import SessionLocal
    from app.models import Session as SessionRow

    with SessionLocal() as db:
        s = db.get(SessionRow, sid)
        assert s is not None
        s.config = {
            "sources": {
                "providers": [
                    {
                        "id": "RENFE-XB",
                        "timetable": {
                            "format": "gtfs",
                            "source": "cross_border_filter",
                            "derived_from": derived_from,
                            "home_country": "ES",
                            "rail_only": True,
                        },
                    }
                ]
            }
        }
        flag_modified(s, "config")
        db.commit()


def test_refresh_materialises_cross_border_subset(
    client: TestClient, admin: dict[str, str], inbox: Path
) -> None:
    _stage_national_feed(inbox, session_id="nap-es-rail", provider_id="RENFE")
    sid = "eu-corridors-test"
    _make_corridors_session(
        client, admin, sid, derived_from={"session_id": "nap-es-rail", "provider_id": "RENFE"}
    )

    r = client.post(f"/api/sessions/{sid}/sources/refresh", headers=admin)
    assert r.status_code == 200, r.text
    body = r.json()

    # The derived feed landed at the corridors provider's own slot.
    out_slot = inbox / sid / "gtfs" / "renfe-xb.zip"
    assert out_slot.is_file()

    # It is the *filtered* subset: only the cross-border route survives.
    with zipfile.ZipFile(out_slot) as z:
        routes = z.read("routes.txt").decode()
    assert "AVE-XB" in routes
    assert "AVE-DOM" not in routes  # domestic ES route dropped

    # The refresh response surfaces the run with its stats.
    fetched = {f["key"]: f for f in body["fetched"]}
    key = "provider[RENFE-XB].cross_border_filter"
    assert key in fetched
    assert fetched[key]["routes_kept"] == 1


def test_refresh_skips_when_source_feed_missing(
    client: TestClient, admin: dict[str, str], inbox: Path
) -> None:
    # No national feed staged → the derived provider can't resolve its source.
    sid = "eu-corridors-missing"
    _make_corridors_session(
        client, admin, sid, derived_from={"session_id": "nap-es-rail", "provider_id": "RENFE"}
    )

    r = client.post(f"/api/sessions/{sid}/sources/refresh", headers=admin)
    assert r.status_code == 200, r.text
    body = r.json()

    assert not (inbox / sid / "gtfs" / "renfe-xb.zip").exists()
    skipped = {s_["key"]: s_ for s_ in body["skipped"]}
    key = "provider[RENFE-XB].cross_border_filter"
    assert key in skipped
    assert "source feed not found" in skipped[key]["error"]


def _make_national_session(
    client: TestClient, admin: dict[str, str], sid: str, provider_id: str
) -> None:
    r = client.post(
        "/api/sessions",
        headers=admin,
        json={"id": sid, "name": "ES national", "category": "NAP", "config": {}},
    )
    assert r.status_code == 201, r.text

    from sqlalchemy.orm.attributes import flag_modified

    from app.db import SessionLocal
    from app.models import Session as SessionRow

    with SessionLocal() as db:
        s = db.get(SessionRow, sid)
        assert s is not None
        s.config = {
            "sources": {
                "providers": [
                    {"id": provider_id, "timetable": {"format": "gtfs", "source": "upload"}}
                ]
            }
        }
        flag_modified(s, "config")
        db.commit()


def test_upload_to_national_cascades_to_derived(
    client: TestClient, admin: dict[str, str], inbox: Path
) -> None:
    # §12 single-source-of-truth cascade: uploading the national feed should
    # auto-rebuild the cross-border view derived from it in another session.
    _make_national_session(client, admin, "nap-es-rail", "RENFE")
    sid = "eu-corridors-cascade"
    _make_corridors_session(
        client, admin, sid, derived_from={"session_id": "nap-es-rail", "provider_id": "RENFE"}
    )

    r = client.post(
        "/api/sessions/nap-es-rail/uploads",
        headers=admin,
        data={"declared_standard": "GTFS", "provider_id": "RENFE"},
        files={"file": ("renfe.zip", _cross_border_gtfs_bytes(), "application/zip")},
    )
    assert r.status_code == 201, r.text

    # The cascade materialised the corridors cross-border subset — no separate
    # refresh of the corridors session needed.
    out_slot = inbox / sid / "gtfs" / "renfe-xb.zip"
    assert out_slot.is_file()
    with zipfile.ZipFile(out_slot) as z:
        routes = z.read("routes.txt").decode()
    assert "AVE-XB" in routes
    assert "AVE-DOM" not in routes


def test_per_provider_refresh_runs_filter_for_derived(
    client: TestClient, admin: dict[str, str], inbox: Path
) -> None:
    # "Refresh this provider" on a derived provider must run the filter, not
    # 400 with "no URLs to refresh" (the operator-reported bug).
    _stage_national_feed(inbox, session_id="nap-es-rail", provider_id="RENFE")
    sid = "eu-corridors-per-provider"
    _make_corridors_session(
        client, admin, sid, derived_from={"session_id": "nap-es-rail", "provider_id": "RENFE"}
    )

    r = client.post(f"/api/sessions/{sid}/providers/RENFE-XB/refresh", headers=admin)
    assert r.status_code == 200, r.text
    body = r.json()

    out_slot = inbox / sid / "gtfs" / "renfe-xb.zip"
    assert out_slot.is_file()
    fetched = {f["key"]: f for f in body["fetched"]}
    assert "provider[RENFE-XB].cross_border_filter" in fetched
