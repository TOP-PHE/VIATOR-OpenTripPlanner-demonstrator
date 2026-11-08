"""Integration: the pages listing every cell of a coverage run read none of the cells' JSON
they do not draw, and still show the same data.

On 8 Nov 2026 the share page of a 123-hub run (~7,500 cells) timed out: the loader read every
column of every row, ÖBB's journeys (`external_journeys`) included. Real PostgreSQL 16 (the
fresh-DB harness of test_coverage_web_health.py), one completed run of three cells, each with
ÖBB's journeys and why it scored so. Each page — the admin run view (polled every 5 s), the
share page, the download (small, and large with the threshold lowered to 2 rows) — is asked
twice through the app: as it loads now, and with the loader reading every column (as before).
Checked:

- the two answers are the same, byte for byte;
- the SQL of the first names `external_journeys` only for the small download, and
  `external_alignment_detail` only for the share page and the download;
- reading a column the loader left out raises instead of querying the row.

Invented values only (ZZ hubs, numbers with 99).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.exc import InvalidRequestError

from tests.integration.test_coverage_web_health import (
    _USER,
    _create_run,
    fresh_db,  # noqa: F401  (pytest fixture)
)
from tests.unit import canonical_fixtures as fx

_EMAIL = "zz-cov@zz.invalid"
_PAIRS = (("zz-hub-0", "zz-hub-1"), ("zz-hub-1", "zz-hub-0"), ("zz-hub-0", "zz-hub-2"))
_JOURNEYS = "network_coverage_results.external_journeys"
_DETAIL = "network_coverage_results.external_alignment_detail"
_WHY = {
    "engine_version": 1,
    "profile": "coverage_alignment",
    "counts": {"same_service": 1, "probably_same": 0, "viator_only": 2, "oebb_only": 0},
    "reasons": {"line_label_differs": 2, "shorter_transfer": 1},
}


def _oebb_journeys() -> list[dict[str, Any]]:
    journeys = fx.oebb_journeys(
        [
            {
                "mode": "RAIL",
                "from_uic": "UIC:9999901",
                "to_uic": "UIC:9999902",
                "dep": "2099-09-09T09:09",
                "arr": "2099-09-09T10:39",
                "route": "ZZ 99",
            }
        ],
        [
            {
                "mode": "RAIL",
                "from_uic": "UIC:9999901",
                "to_uic": "UIC:9999902",
                "dep": "2099-09-09T11:09",
                "arr": "2099-09-09T12:39",
                "route": "ZZ 199",
            }
        ],
    )
    return [journey.model_dump(mode="json") for journey in journeys]


@pytest.fixture
def run_id(fresh_db: str) -> uuid.UUID:  # noqa: F811
    from app.db import SessionLocal
    from app.models import NetworkCoverageResult, NetworkCoverageRun

    run_id = _create_run()
    journeys = _oebb_journeys()
    with SessionLocal() as db:
        run = db.get(NetworkCoverageRun, run_id)
        assert run is not None
        run.status = "completed"
        run.verify_externally = True
        run.started_at = datetime(2099, 9, 9, 7, 59, tzinfo=UTC)
        run.finished_at = datetime(2099, 9, 9, 8, 9, tzinfo=UTC)
        for i, (origin, dest) in enumerate(_PAIRS):
            db.add(
                NetworkCoverageResult(
                    run_id=run_id,
                    origin_hub_id=origin,
                    dest_hub_id=dest,
                    status="ok",
                    response_ms=990 + i,
                    num_itineraries=3,
                    best_duration_seconds=5400 + i,
                    best_num_transfers=0,
                    best_operators="ZZ",
                    external_verified_at=datetime(2099, 9, 9, 8, 9, tzinfo=UTC),
                    external_ok=True,
                    external_num_connections=2,
                    external_best_duration_seconds=5400,
                    external_best_transfers=0,
                    external_source="fahrplan.oebb.at",
                    external_journeys=journeys,
                    external_alignment_score=0.99,
                    external_alignment_tier="mostly_agree",
                    external_alignment_detail=_WHY,
                )
            )
        db.commit()
    return run_id


@pytest.fixture
def client(run_id: uuid.UUID) -> Iterator[TestClient]:
    from app.auth import tokens
    from app.main import app
    from app.rate_limit import limiter
    from app.settings import settings

    limiter.reset()
    client = TestClient(app)
    client.cookies.set(
        settings.jwt_cookie_name,
        tokens.issue_jwt(uuid.UUID(_USER), _EMAIL, "platform_admin"),
    )
    yield client
    limiter.reset()


@pytest.fixture
def sql() -> Iterator[list[str]]:
    """Every statement the app sends to PostgreSQL meanwhile."""
    from app.db import engine

    statements: list[str] = []

    def record(_conn, _cursor, statement, *_args) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    yield statements
    event.remove(engine, "before_cursor_execute", record)


def _every_column(run_id: uuid.UUID, **_flags: bool):
    """The rows' SELECT as it was before: every column, whatever the page asks for."""
    from app.models import NetworkCoverageResult

    return select(NetworkCoverageResult).where(NetworkCoverageResult.run_id == run_id)


def _both_ways(
    client: TestClient, sql: list[str], monkeypatch: pytest.MonkeyPatch, path: str
) -> tuple[list[str], list[str]]:
    """The page as it loads now, then as before: the answers must be the same. Returns the
    statements naming `network_coverage_results` each way."""
    from app.network_coverage import runner

    sql.clear()
    now = client.get(path)
    sql_now = [s for s in sql if "network_coverage_results" in s]
    with monkeypatch.context() as patch:
        patch.setattr(runner, "results_query", _every_column)
        sql.clear()
        before = client.get(path)
        sql_before = [s for s in sql if "network_coverage_results" in s]
    assert now.status_code == before.status_code == 200, now.text[:500]
    assert now.content == before.content
    return sql_now, sql_before


def _names(statements: list[str], column: str) -> bool:
    return any(column in statement for statement in statements)


def test_the_polled_run_view_reads_no_cell_json(
    client: TestClient, sql: list[str], monkeypatch: pytest.MonkeyPatch, run_id: uuid.UUID
) -> None:
    now, before = _both_ways(client, sql, monkeypatch, f"/api/admin/network-coverage/runs/{run_id}")
    assert _names(before, _JOURNEYS) and _names(before, _DETAIL)
    assert not _names(now, _JOURNEYS)
    assert not _names(now, _DETAIL)
    cells = client.get(f"/api/admin/network-coverage/runs/{run_id}").json()["results"]
    assert len(cells) == len(_PAIRS)
    assert {c["external_alignment_tier"] for c in cells} == {"mostly_agree"}


def test_the_share_page_reads_why_each_cell_scored_so_but_no_journeys(
    client: TestClient, sql: list[str], monkeypatch: pytest.MonkeyPatch, run_id: uuid.UUID
) -> None:
    now, _before = _both_ways(client, sql, monkeypatch, f"/share/coverage/{run_id}")
    assert not _names(now, _JOURNEYS)
    assert _names(now, _DETAIL)
    page = client.get(f"/share/coverage/{run_id}").text
    assert "line labels differ (2 times)" in page


def test_the_small_download_embeds_oebb_journeys(
    client: TestClient, sql: list[str], monkeypatch: pytest.MonkeyPatch, run_id: uuid.UUID
) -> None:
    path = f"/api/admin/network-coverage/runs/{run_id}/export.html"
    now, _before = _both_ways(client, sql, monkeypatch, path)
    assert _names(now, _JOURNEYS)
    assert _names(now, _DETAIL)
    assert "ZZ 199" in client.get(path).text


def test_the_large_download_reads_no_oebb_journeys(
    client: TestClient, sql: list[str], monkeypatch: pytest.MonkeyPatch, run_id: uuid.UUID
) -> None:
    from app.api.admin import network_coverage as mod

    monkeypatch.setattr(mod, "_EXPORT_LEG_DETAIL_MAX_PAIRS", len(_PAIRS) - 1)
    path = f"/api/admin/network-coverage/runs/{run_id}/export.html"
    now, before = _both_ways(client, sql, monkeypatch, path)
    assert _names(before, _JOURNEYS)
    assert not _names(now, _JOURNEYS)
    assert _names(now, _DETAIL)
    assert "ZZ 199" not in client.get(path).text


def test_a_column_left_out_raises_when_read(run_id: uuid.UUID) -> None:
    from app.db import SessionLocal
    from app.network_coverage import runner

    with SessionLocal() as db:
        run, rows = runner.get_run_with_results(db, run_id)
        assert run is not None
        assert len(rows) == len(_PAIRS)
        assert rows[0].external_alignment_tier == "mostly_agree"
        with pytest.raises(InvalidRequestError):
            _ = rows[0].external_journeys
        with pytest.raises(InvalidRequestError):
            _ = rows[0].external_alignment_detail

    with SessionLocal() as db:
        _run, rows = runner.get_run_with_results(
            db, run_id, alignment_detail=True, external_journeys=True
        )
        assert rows[0].external_alignment_detail == _WHY
        assert [j["id"] for j in rows[0].external_journeys] == [j["id"] for j in _oebb_journeys()]
