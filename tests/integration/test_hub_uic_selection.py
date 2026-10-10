"""The coverage hubs' station codes on PostgreSQL (MSMM step 3).

tests/unit/test_hub_uic.py drives the three hub routes with the hubs given
by stand-ins; this file proves, on the real database (skipped without it,
like the other integration tests), what those stand-ins replace: which hubs
a resolve or a check click looks at, in the matrix order, less those already
shown; that a confirm's write reaches the table; and that a resolve writes
nothing. The module is never reached (`httpx.MockTransport`). Invented hubs
only: ZZ names, codes beginning with 99.
"""

from __future__ import annotations

import json
import os
import secrets
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from alembic import command

BASE = "/api/admin/network-coverage/hubs"

# (id, country, sort_order, is_active, uic, uic_origin)
HUBS = [
    ("zz-b-2", "ZB", 100, True, None, None),
    ("zz-a-2", "ZA", 100, True, None, None),
    ("zz-a-1", "ZA", 50, True, None, None),
    ("zz-a-0", "ZA", 100, False, None, None),  # soft-deleted: never looked at
    # These two make the matrix order (country, sort_order, id) differ from
    # the order of the ids alone and from (sort_order, id).
    ("zz-a-9", "ZA", 10, True, None, None),
    ("zz-b-0", "ZB", 20, True, None, None),
    ("zz-b-1", "ZB", 100, True, "9900001", "msmm"),
    ("zz-a-3", "ZA", 100, True, "9900002", "manual"),
    ("zz-a-4", "ZA", 100, False, "9900003", "msmm"),  # soft-deleted
]


def _postgres_or_skip() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgresql"):
        pytest.skip("DATABASE_URL is not Postgres; skipping the hub code tests")
    try:
        with create_engine(url).connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError as exc:
        pytest.skip(f"Postgres not reachable ({exc})")
    return url


@pytest.fixture
def fresh_db() -> str:
    url = _postgres_or_skip()
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE;"))
        conn.execute(text("CREATE SCHEMA public;"))
    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "alembic")
    command.upgrade(cfg, "head")
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM network_coverage_hubs"))  # the seeded hubs
        for hub_id, country, sort_order, active, uic, origin in HUBS:
            conn.execute(
                text(
                    "INSERT INTO network_coverage_hubs "
                    "(id, name, short, country, lat, lon, sort_order, is_active, uic, uic_origin, "
                    " updated_at) "
                    "VALUES (:id, :name, :short, :country, 45.0, 6.0, :sort_order, :active, "
                    " :uic, :origin, '2026-01-01T00:00:00Z')"
                ),
                {
                    "id": hub_id,
                    "name": f"ZZ Hub {hub_id}",
                    "short": hub_id[-3:],
                    "country": country,
                    "sort_order": sort_order,
                    "active": active,
                    "uic": uic,
                    "origin": origin,
                },
            )
    return url


@pytest.fixture
def db(fresh_db: str) -> Iterator[Session]:
    from app.db import SessionLocal

    with SessionLocal() as session:
        yield session


def _ids(hubs: list[Any]) -> list[str]:
    return [hub.id for hub in hubs]


def test_resolve_looks_at_the_active_unresolved_hubs_in_the_matrix_order(db: Session) -> None:
    from app.api.admin.network_coverage import _hubs_to_look_at

    assert _ids(_hubs_to_look_at(db, resolved=False, skip=[])) == [
        "zz-a-9",
        "zz-a-1",
        "zz-a-2",
        "zz-b-0",
        "zz-b-2",
    ]
    assert _ids(_hubs_to_look_at(db, resolved=False, skip=["zz-a-1", "zz-zz"])) == [
        "zz-a-9",
        "zz-a-2",
        "zz-b-0",
        "zz-b-2",
    ]


def test_check_looks_at_the_active_resolved_hubs_in_the_matrix_order(db: Session) -> None:
    from app.api.admin.network_coverage import _hubs_to_look_at

    assert _ids(_hubs_to_look_at(db, resolved=True, skip=[])) == ["zz-a-3", "zz-b-1"]
    assert _ids(_hubs_to_look_at(db, resolved=True, skip=["zz-a-3"])) == ["zz-b-1"]


def test_hubs_by_id_finds_those_that_exist(db: Session) -> None:
    from app.api.admin.network_coverage import _hubs_by_id

    # zz-a-0 is soft-deleted: it takes no code.
    assert sorted(_hubs_by_id(db, ["zz-a-1", "zz-a-0", "zz-zz"])) == ["zz-a-1"]


class _Module:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = json.loads(request.content)
        if request.url.path.endswith("/stations/near"):
            return httpx.Response(
                200,
                json={
                    "stations": [
                        {
                            "name": "ZZ Station",
                            "latitude": body["lat"],
                            "longitude": body["lon"],
                            "country_iso": "ZZ",
                            "uic": "9900042",
                            "parent_uic": None,
                            "distance_m": 0,
                        }
                    ]
                },
            )
        served = [
            {
                "name": "ZZ Station",
                "latitude": 45.0,
                "longitude": 6.0,
                "country_iso": "ZZ",
                "uic": code,
                "parent_uic": None,
            }
            for code in body["uics"]
            if code == "9900042"
        ]
        return httpx.Response(200, json={"stations": served})


@pytest.fixture
def client(fresh_db: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, _Module]]:
    from app import station_module
    from app.auth import tokens
    from app.main import app
    from app.settings import settings

    module = _Module()
    transport = httpx.MockTransport(module)
    real_async = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async(*args, **kwargs)

    monkeypatch.setattr(station_module.httpx, "AsyncClient", factory)
    monkeypatch.setattr(settings, "station_module_url", "http://msmm.invalid:8000")
    monkeypatch.setattr(settings, "station_module_token", SecretStr(secrets.token_hex(32)))
    station_module.reset()
    user = uuid.uuid4()
    test_client = TestClient(app)
    test_client.cookies.set(
        settings.jwt_cookie_name,
        tokens.issue_jwt(user, f"zz-{user.hex[:8]}@example.invalid", "platform_admin"),
    )
    yield test_client, module
    station_module.reset()


def _row(engine_url: str, hub_id: str) -> tuple[Any, ...]:
    with create_engine(engine_url).connect() as conn:
        return tuple(
            conn.execute(
                text(
                    "SELECT uic, uic_origin, updated_at > '2026-01-02T00:00:00Z' "
                    "FROM network_coverage_hubs WHERE id = :id"
                ),
                {"id": hub_id},
            ).one()
        )


def test_resolve_writes_nothing_and_confirm_writes_the_served_code(
    fresh_db: str, client: tuple[TestClient, _Module]
) -> None:
    test_client, module = client

    proposals = test_client.post(f"{BASE}/resolve", json={}).json()["proposals"]

    assert [p["hub_id"] for p in proposals] == ["zz-a-9", "zz-a-1", "zz-a-2", "zz-b-0", "zz-b-2"]
    assert all(p["state"] == "proposed" for p in proposals)
    # One near call per hub at its stored position, never a search by name.
    near = [json.loads(r.content) for r in module.requests if r.url.path.endswith("/near")]
    assert near == [{"lat": 45.0, "lon": 6.0, "radius_m": 300}] * 5
    assert not any(r.url.path.endswith("/search") for r in module.requests)
    assert _row(fresh_db, "zz-a-1") == (None, None, False)

    answer = test_client.post(
        f"{BASE}/confirm",
        json={
            "pairs": [
                {"hub_id": "zz-a-1", "uic": "9900042"},
                {"hub_id": "zz-a-2", "uic": "9900043"},
            ]
        },
    ).json()

    assert [r["state"] for r in answer["results"]] == ["stored", "not_served"]
    assert _row(fresh_db, "zz-a-1") == ("9900042", "msmm", True)
    assert _row(fresh_db, "zz-a-2") == (None, None, False)
    listed = {h["id"]: h for h in test_client.get(BASE).json()}
    assert (listed["zz-a-1"]["uic"], listed["zz-a-1"]["uic_origin"]) == ("9900042", "msmm")
    assert len([r for r in module.requests if r.url.path.endswith("/lookup")]) == 1


def test_a_hub_whose_stored_position_is_not_a_number_is_skipped_without_a_call(
    fresh_db: str, client: tuple[TestClient, _Module]
) -> None:
    """The columns are NOT NULL, but PostgreSQL's float takes 'NaN' and
    'Infinity': only a hand edit of the table can store one."""
    test_client, module = client
    with create_engine(fresh_db).begin() as conn:
        conn.execute(
            text(
                "UPDATE network_coverage_hubs SET lat = 'NaN', lon = 'Infinity' WHERE id = 'zz-a-1'"
            )
        )

    proposals = test_client.post(f"{BASE}/resolve", json={}).json()["proposals"]

    states = {p["hub_id"]: p["state"] for p in proposals}
    assert states["zz-a-1"] == "no_position"
    assert len([r for r in module.requests if r.url.path.endswith("/near")]) == 4
    assert _row(fresh_db, "zz-a-1") == (None, None, False)


def test_confirm_writes_nothing_to_a_soft_deleted_hub(
    fresh_db: str, client: tuple[TestClient, _Module]
) -> None:
    test_client, module = client

    answer = test_client.post(
        f"{BASE}/confirm", json={"pairs": [{"hub_id": "zz-a-0", "uic": "9900042"}]}
    ).json()

    assert [r["state"] for r in answer["results"]] == ["unknown_hub"]
    assert _row(fresh_db, "zz-a-0") == (None, None, False)
    assert module.requests == []


def test_a_typed_code_is_stored_as_manual_and_cleared_by_null(
    fresh_db: str, client: tuple[TestClient, _Module]
) -> None:
    test_client, module = client

    assert test_client.patch(f"{BASE}/zz-b-1", json={"uic": "ZZ-99-7"}).status_code == 200
    assert _row(fresh_db, "zz-b-1")[:2] == ("ZZ-99-7", "manual")
    assert test_client.patch(f"{BASE}/zz-b-1", json={"uic": None}).status_code == 200
    assert _row(fresh_db, "zz-b-1")[:2] == (None, None)
    assert module.requests == []  # a typed code is not looked up
