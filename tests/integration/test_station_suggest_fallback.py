"""The fallback of `POST /api/stations/suggest` on VIATOR's own `master_stations`.

On PostgreSQL (skipped without it, like the other integration tests): the
ordering, the cap of ten rows and the literal wildcards can only be proved
against the real database. Invented stations only: ZZ names, codes
beginning with 99.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from alembic import command


def _postgres_or_skip() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgresql"):
        pytest.skip("DATABASE_URL is not Postgres; skipping station-suggest fallback test")
    try:
        with create_engine(url).connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError as exc:
        pytest.skip(f"Postgres not reachable ({exc})")
    return url


# (uic, name, country, latitude, longitude)
STATIONS = [
    ("9900001", "Zzmoor Halt", "ZY", 45.1, 6.1),
    ("9900002", "Zzmoor Central", "ZX", 45.2, 6.2),
    ("9900003", "Zzmoor Abbey", "ZY", 45.3, 6.3),
    ("9900004", "Zzmoor Bridge", None, 45.4, 6.4),
    ("9900005", "Zzmoor Upper", "ZX", 45.5, 6.5),
    ("9900006", "Zzmoor Lower", "ZX", 45.6, 6.6),
    ("9900007", "Zzmoor East", "ZZ", 45.7, 6.7),
    ("9900008", "Zzmoor West", "ZZ", 45.8, 6.8),
    ("9900009", "Zzmoor North", "ZZ", 45.9, 6.9),
    ("9900010", "Zzmoor South", "ZZ", 46.0, 7.0),
    ("9900011", "Zzmoor Quay", "ZZ", 46.1, 7.1),
    ("9900012", "Zzmoor Park", "ZZ", 46.2, 7.2),
    ("9900013", "Zzmoor Nowhere", "ZX", None, None),
    ("9900020", "Zz 100% Halt", "ZZ", 47.0, 8.0),
    ("9900021", "Zz 1000 Halt", "ZZ", 47.1, 8.1),
    ("9900022", "Zz a_b Halt", "ZZ", 47.2, 8.2),
    ("9900023", "Zz axb Halt", "ZZ", 47.3, 8.3),
    ("9900024", "Zz back\\slash", "ZZ", 47.4, 8.4),
    ("9900025", "Zz backxslash", "ZZ", 47.5, 8.5),
    ("9900030", "Zzcode Station", "ZZ", 48.0, 9.0),
]


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
        for uic, name, country, lat, lon in STATIONS:
            conn.execute(
                text(
                    "INSERT INTO master_stations (uic, name, country_iso, latitude, longitude) "
                    "VALUES (:uic, :name, :country, :lat, :lon)"
                ),
                {"uic": uic, "name": name, "country": country, "lat": lat, "lon": lon},
            )
    return url


@pytest.fixture
def db(fresh_db: str) -> Iterator[Session]:
    from app.db import SessionLocal

    with SessionLocal() as session:
        yield session


def _names(rows: list[dict]) -> list[str]:
    return [r["name"] for r in rows]


def test_at_most_ten_rows_ordered_by_country_then_name(db: Session) -> None:
    from app.api.station_suggest import fallback_rows

    rows = fallback_rows(db, "zzmoor")

    # 12 matches with a position (Zzmoor Nowhere has none): the first ten of
    # (country_iso, name), NULL country last as PostgreSQL orders it ascending.
    assert _names(rows) == [
        "Zzmoor Central",
        "Zzmoor Lower",
        "Zzmoor Upper",
        "Zzmoor Abbey",
        "Zzmoor Halt",
        "Zzmoor East",
        "Zzmoor North",
        "Zzmoor Park",
        "Zzmoor Quay",
        "Zzmoor South",
    ]
    assert rows[0] == {
        "name": "Zzmoor Central",
        "latitude": 45.2,
        "longitude": 6.2,
        "country_iso": "ZX",
        "uic": "9900002",
        "source": "viator",
    }


def test_a_row_without_a_position_is_not_served(db: Session) -> None:
    from app.api.station_suggest import fallback_rows

    assert fallback_rows(db, "Zzmoor Nowhere") == []


@pytest.mark.parametrize(
    ("q", "expected"),
    [
        pytest.param("100%", ["Zz 100% Halt"], id="percent-is-literal"),
        pytest.param("a_b", ["Zz a_b Halt"], id="underscore-is-literal"),
        pytest.param("k\\s", ["Zz back\\slash"], id="backslash-is-literal"),
    ],
)
def test_wildcards_in_the_text_are_literal(db: Session, q: str, expected: list[str]) -> None:
    from app.api.station_suggest import fallback_rows

    assert _names(fallback_rows(db, q)) == expected


def test_the_uic_is_found_exactly_and_not_by_prefix(db: Session) -> None:
    from app.api.station_suggest import fallback_rows

    assert _names(fallback_rows(db, "9900030")) == ["Zzcode Station"]
    assert fallback_rows(db, "990003") == []


def test_an_end_user_gets_the_fallback_through_the_route(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.auth import tokens
    from app.main import app
    from app.settings import settings

    monkeypatch.setattr(settings, "station_module_url", "")
    jwt = tokens.issue_jwt(uuid.uuid4(), "zz-end-user@example.invalid", "end_user")

    with TestClient(app) as client:
        client.cookies.set(settings.jwt_cookie_name, jwt)
        answer = client.post("/api/stations/suggest", json={"q": "  zz   100%  "})

    assert answer.status_code == 200
    assert answer.json() == [
        {
            "name": "Zz 100% Halt",
            "latitude": 47.0,
            "longitude": 8.0,
            "country_iso": "ZZ",
            "uic": "9900020",
            "source": "viator",
        }
    ]
