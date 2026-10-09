"""Integration: /api/journey/fanout says when an engine refused the date (#338).

A MOTIS session refuses a date outside its loaded timetable with HTTP 400 and
`{"error": "query time … is outside of loaded timetable window [from, to["}`;
an OTP session answers OUTSIDE_SERVICE_PERIOD. The fanout keeps the database
statuses ("error", "no_route") and adds `reason`, `detail` and, when MOTIS
named it, `timetable_window` to the execution; `detail` is also stored as the
execution's error_message. The engines are mocked at the HTTP layer with
invented values. Fresh-DB harness as in test_federated_fanout.py; skips when
Postgres is down.
"""

from __future__ import annotations

import os

import httpx
import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from alembic import command

_BOOTSTRAP = "test-bootstrap-token"
_REFUSAL = (
    "query time 2031-02-03 04:05:00 is outside of loaded timetable window "
    "[2031-03-01 00:00, 2031-05-30 00:00["
)
_DETAIL = "date outside the loaded timetable (loaded: 2031-03-01 00:00 to 2031-05-30 00:00 UTC)"


def _postgres_or_skip() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgresql"):
        pytest.skip("DATABASE_URL is not Postgres; skipping outside-timetable test")
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
            "email": "zz-admin@viator.example",
            "name": "Admin",
            "password": "a-strong-admin-password",
        },
    )
    r.raise_for_status()
    jwt = r.json()["jwt"]
    client.cookies.clear()
    return {"Authorization": f"Bearer {jwt}"}


def _make_serving_session(sid: str, engine: str) -> None:
    from app.db import SessionLocal
    from app.models import Session as SessionRow
    from app.models.identity import User
    from app.models.sessions import SessionState

    with SessionLocal() as db:
        # `created_by` is NOT NULL → reuse the bootstrapped platform user
        # (the `admin` fixture runs before the test body, so one exists).
        creator = db.query(User).first()
        assert creator is not None, "admin fixture must bootstrap a user first"
        db.add(
            SessionRow(
                id=sid,
                name="XB",
                category="NAP",
                state=SessionState.SERVING.value,
                include_in_fanout=True,
                created_by=creator.id,
                engine=engine,
                config={"sources": {"providers": [{"id": "ZZ-FEED"}]}},
            )
        )
        db.commit()


def _engines(request: httpx.Request) -> httpx.Response:
    if request.url.host == "motis-zz-motis":
        return httpx.Response(400, json={"error": _REFUSAL})
    if request.url.host == "otp-zz-otp":
        return httpx.Response(
            200,
            json={
                "data": {
                    "planConnection": {
                        "edges": [],
                        "routingErrors": [{"code": "OUTSIDE_SERVICE_PERIOD", "description": "zz"}],
                    }
                }
            },
        )
    return httpx.Response(404)


def test_fanout_names_the_refusal_of_each_engine(
    client: TestClient, admin: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_serving_session("zz-motis", "motis")
    _make_serving_session("zz-otp", "otp")
    transport = httpx.MockTransport(_engines)
    real_cls = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_cls(*args, **kwargs)

    # The engine clients' httpx.AsyncClient; the TestClient is an httpx.Client.
    monkeypatch.setattr(httpx, "AsyncClient", factory)

    r = client.post(
        "/api/journey/fanout",
        headers=admin,
        json={
            "from": {"lat": 46.5, "lon": 6.6, "label": "Zz A"},
            "to": {"lat": 47.4, "lon": 8.5, "label": "Zz B"},
            "depart_at": "2031-02-03T05:05:00",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    by_session = {e["session_id"]: e for e in body["executions"]}

    motis = by_session["zz-motis"]
    assert motis["status"] == "error"
    assert motis["reason"] == "outside_timetable"
    assert motis["detail"] == _DETAIL
    assert motis["timetable_window"] == {
        "from": "2031-03-01T00:00:00+00:00",
        "until": "2031-05-30T00:00:00+00:00",
    }

    otp = by_session["zz-otp"]
    assert otp["status"] == "no_route"
    assert otp["reason"] == "outside_timetable"
    assert otp["detail"] == "date outside the loaded timetable"
    assert "timetable_window" not in otp
    assert body["trips"] == []

    from app.db import SessionLocal
    from app.models import JourneySearchExecution

    with SessionLocal() as db:
        stored = {
            e.session_id: e.error_message
            for e in db.query(JourneySearchExecution).filter_by(search_id=body["search_id"])
        }
    assert stored == {"zz-motis": _DETAIL, "zz-otp": "date outside the loaded timetable"}


def _install_engines(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = httpx.MockTransport(_engines)
    real_cls = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_cls(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


def _stored_error_message(search_id: str) -> list[str | None]:
    from app.db import SessionLocal
    from app.models import JourneySearchExecution

    with SessionLocal() as db:
        return [
            e.error_message for e in db.query(JourneySearchExecution).filter_by(search_id=search_id)
        ]


@pytest.mark.parametrize(
    ("sid", "engine", "status", "detail", "window"),
    [
        (
            "zz-motis",
            "motis",
            "error",
            _DETAIL,
            {"from": "2031-03-01T00:00:00+00:00", "until": "2031-05-30T00:00:00+00:00"},
        ),
        ("zz-otp", "otp", "no_route", "date outside the loaded timetable", None),
    ],
    ids=["motis", "otp"],
)
def test_plan_names_the_refusal_of_its_session(
    client: TestClient,
    admin: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    sid: str,
    engine: str,
    status: str,
    detail: str,
    window: dict[str, str] | None,
) -> None:
    """The single-session /plan route says it too, and stores the sentence."""
    _make_serving_session(sid, engine)
    _install_engines(monkeypatch)

    r = client.post(
        "/api/journey/plan",
        headers=admin,
        json={
            "session_id": sid,
            "from": {"lat": 46.5, "lon": 6.6, "label": "Zz A"},
            "to": {"lat": 47.4, "lon": 8.5, "label": "Zz B"},
            "depart_at": "2031-02-03T05:05:00",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == status
    assert body["trips"] == []
    assert body["reason"] == "outside_timetable"
    assert body["detail"] == detail
    assert body.get("timetable_window") == window
    assert _stored_error_message(body["search_id"]) == [detail]
