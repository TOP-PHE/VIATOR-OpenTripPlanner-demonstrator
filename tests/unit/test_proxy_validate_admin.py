"""The two endpoints nginx asks before it serves a proxied location.

`/api/auth/proxy-validate-admin` answers `/grafana/` and `/prometheus/`:
platform administrators only. `/api/auth/proxy-validate` stays open to every
signed-in role with the same headers: locations outside nginx.conf ask it,
and the station mapping module's include reads `X-Forwarded-User` and
`X-Viator-Role` from its answer.

No database: both endpoints decide on the session's token alone.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.auth import routes as auth_routes
from app.auth import tokens
from app.settings import settings

ADMIN_ONLY = "/api/auth/proxy-validate-admin"
ANY_ROLE = "/api/auth/proxy-validate"
ROLES = ("platform_admin", "content_manager", "end_user")
GRAFANA_ROLE = {"platform_admin": "Admin", "content_manager": "Editor", "end_user": "Viewer"}
IDENTITY_HEADERS = ("X-Forwarded-User", "X-Forwarded-Role", "X-Viator-Role")


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(auth_routes.router)
    return TestClient(app)


def _session(role: str) -> tuple[str, str]:
    """A fresh account's e-mail and the token of its session."""
    user_id = uuid.uuid4()
    email = f"zz-{user_id.hex[:8]}@example.invalid"
    return email, tokens.issue_jwt(user_id, email, role)


def _bearer(jwt: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {jwt}"}


# ───────────────────── the administrators' endpoint ─────────────────────


def test_an_administrator_gets_200_with_the_identity_headers(client: TestClient) -> None:
    email, jwt = _session("platform_admin")

    answer = client.get(ADMIN_ONLY, headers=_bearer(jwt))

    assert answer.status_code == 200
    assert answer.content == b""
    assert answer.headers["X-Forwarded-User"] == email
    assert answer.headers["X-Forwarded-Role"] == "Admin"
    assert answer.headers["X-Viator-Role"] == "platform_admin"


def test_the_administrators_cookie_is_accepted_as_the_browser_sends_it(
    client: TestClient,
) -> None:
    email, jwt = _session("platform_admin")
    client.cookies.set(settings.jwt_cookie_name, jwt)

    answer = client.get(ADMIN_ONLY)

    assert answer.status_code == 200
    assert answer.headers["X-Forwarded-User"] == email


@pytest.mark.parametrize("role", ["content_manager", "end_user", "zz-unknown-role"])
def test_any_other_role_gets_403_and_no_identity(client: TestClient, role: str) -> None:
    _, jwt = _session(role)

    answer = client.get(ADMIN_ONLY, headers=_bearer(jwt))

    assert answer.status_code == 403
    for header in IDENTITY_HEADERS:
        assert header not in answer.headers


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer zz-not-a-token"}, {"Authorization": "Basic enp6Onp6eg=="}],
    ids=["no session", "a token that is not one", "basic credentials"],
)
def test_no_session_gets_401(client: TestClient, headers: dict[str, str]) -> None:
    answer = client.get(ADMIN_ONLY, headers=headers)

    assert answer.status_code == 401
    for header in IDENTITY_HEADERS:
        assert header not in answer.headers


def test_an_expired_administrator_session_gets_401(client: TestClient) -> None:
    user_id = uuid.uuid4()
    jwt = tokens.issue_jwt(user_id, "zz@example.invalid", "platform_admin", ttl_seconds=-60)

    assert client.get(ADMIN_ONLY, headers=_bearer(jwt)).status_code == 401


# ───────────────── the endpoint the other callers rely on ─────────────────


@pytest.mark.parametrize("role", ROLES)
def test_the_first_endpoint_still_answers_every_role_with_its_headers(
    client: TestClient, role: str
) -> None:
    email, jwt = _session(role)

    answer = client.get(ANY_ROLE, headers=_bearer(jwt))

    assert answer.status_code == 200
    assert answer.headers["X-Forwarded-User"] == email
    assert answer.headers["X-Forwarded-Role"] == GRAFANA_ROLE[role]
    assert answer.headers["X-Viator-Role"] == role


def test_the_first_endpoint_still_refuses_a_request_without_session(
    client: TestClient,
) -> None:
    assert client.get(ANY_ROLE).status_code == 401


# ─────────────────────────── both, as nginx uses them ───────────────────────────


@pytest.mark.parametrize("path", [ANY_ROLE, ADMIN_ONLY])
def test_neither_is_rate_limited(client: TestClient, path: str) -> None:
    """nginx asks on every proxied request: one dashboard is dozens of them."""
    _, jwt = _session("platform_admin")

    statuses = {client.get(path, headers=_bearer(jwt)).status_code for _ in range(150)}

    assert statuses == {200}


@pytest.mark.parametrize("path", [ANY_ROLE, ADMIN_ONLY])
def test_neither_is_in_the_published_api(path: str) -> None:
    (route,) = [r for r in auth_routes.router.routes if getattr(r, "path", None) == path]
    assert route.include_in_schema is False  # type: ignore[attr-defined]
