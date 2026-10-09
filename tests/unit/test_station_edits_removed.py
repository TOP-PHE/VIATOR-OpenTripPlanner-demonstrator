"""Station edits end in VIATOR (MSMM step 3, decisions 53 and 55).

The paged list, the edit and the drift queue of `/api/master/stations` are
gone for every role: each answers 404 or 405, before any login check and
without a database. What stays is the search and the Trainline refresh.
Invented values only.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.api.master import stations
from app.auth import tokens
from app.db import get_db
from app.main import app
from app.settings import settings

REMOVED = [
    pytest.param("GET", "/api/master/stations", None, id="the-paged-list"),
    pytest.param("GET", "/api/master/stations/", None, id="the-paged-list-slash"),
    pytest.param("PATCH", "/api/master/stations/9900001", {"name": "ZZ"}, id="the-edit"),
    pytest.param("GET", "/api/master/stations/drift", None, id="the-drift-list"),
    pytest.param(
        "POST",
        "/api/master/stations/9900001/drift/resolve",
        {"action": "adopt_full"},
        id="the-drift-resolve",
    ),
    pytest.param("POST", "/api/master/stations", {"name": "ZZ"}, id="a-create"),
]


class UntouchedDb:
    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"the database was used: {name}")


@pytest.fixture
def client() -> Iterator[TestClient]:
    app.dependency_overrides[get_db] = UntouchedDb
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.parametrize("role", [None, "end_user", "content_manager", "platform_admin"])
@pytest.mark.parametrize(("method", "path", "body"), REMOVED)
def test_the_removed_routes_answer_404_or_405_for_every_role(
    client: TestClient, role: str | None, method: str, path: str, body: dict[str, str] | None
) -> None:
    client.cookies.clear()
    if role is not None:
        jwt = tokens.issue_jwt(uuid.uuid4(), f"zz-{role}@example.invalid", role)
        client.cookies.set(settings.jwt_cookie_name, jwt)

    answer = client.request(method, path, json=body)

    assert answer.status_code in (404, 405)


def test_only_the_search_and_the_refresh_remain() -> None:
    routes = {
        (method, route.path)
        for route in stations.router.routes
        for method in getattr(route, "methods", ())
    }
    assert routes == {
        ("POST", "/api/master/stations/search"),
        ("POST", "/api/master/stations/refresh-trainline"),
    }


def test_the_edit_and_drift_helpers_are_gone() -> None:
    for name in (
        "StationPatch",
        "DriftResolveBody",
        "StationResponse",
        "pinned_page",
        "drift_uics_of",
        "list_stations",
        "patch_station",
        "list_drift",
        "resolve_drift",
    ):
        assert not hasattr(stations, name), name


def test_the_published_api_has_no_station_edit() -> None:
    paths = app.openapi()["paths"]
    station_paths = {path for path in paths if path.startswith("/api/master/stations")}
    assert station_paths == {
        "/api/master/stations/search",
        "/api/master/stations/refresh-trainline",
    }
    assert set(paths["/api/master/stations/search"]) == {"post"}
    assert set(paths["/api/master/stations/refresh-trainline"]) == {"post"}


class CommitDb:
    def __init__(self) -> None:
        self.commits = 0

    def commit(self) -> None:
        self.commits += 1


@pytest.mark.parametrize(
    ("role", "status"),
    [
        pytest.param("content_manager", 200, id="content-manager"),
        pytest.param("platform_admin", 200, id="platform-admin"),
        pytest.param("end_user", 403, id="end-user"),
    ],
)
def test_the_trainline_refresh_stays_for_both_roles_of_the_page(
    monkeypatch: pytest.MonkeyPatch, role: str, status: int
) -> None:
    """The refresh feeds the fallback list (decision 53); its button stays."""
    counts = {"added": 1, "updated": 2, "skipped_manual": 0, "pending_drift": 0}
    refreshed: list[object] = []
    audited: list[dict[str, object]] = []

    async def refresh(db: object) -> dict[str, int]:
        refreshed.append(db)
        return counts

    def record(db: object, **fields: object) -> None:
        audited.append(fields)

    monkeypatch.setattr(stations.trainline, "refresh", refresh)
    monkeypatch.setattr(stations.audit, "record", record)
    db = CommitDb()
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        jwt = tokens.issue_jwt(uuid.uuid4(), f"zz-{role}@example.invalid", role)
        client.cookies.set(settings.jwt_cookie_name, jwt)
        answer = client.post("/api/master/stations/refresh-trainline")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert answer.status_code == status
    if status == 200:
        assert answer.json() == counts
        assert refreshed == [db]
        assert [entry["action"] for entry in audited] == ["master_stations.refresh.trainline"]
        assert db.commits == 1
    else:
        assert refreshed == []
        assert audited == []
