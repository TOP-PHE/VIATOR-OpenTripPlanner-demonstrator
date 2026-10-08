"""The login page's `next`: where a person lands after logging in.

`safe_next` (app/api/pages.py) accepts a path of this site only: it matches
`^/(?![/\\])[A-Za-z0-9._~/-]{0,199}$` and is not `/login`. Anything else is
ignored silently: the person lands on the role's default page, as before.
auth/login.html applies the same pattern in script, after the sign-in; the
template's JS is not executed here, so its text is checked against the
Python pattern.

No database: `/login` reads no table. Values are invented; user ids are
drawn at run time.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import pages
from app.api.pages import safe_next
from app.auth import tokens
from app.main import app
from app.settings import settings

PAGES_SOURCE = Path(pages.__file__).read_text(encoding="utf-8")

# The pattern as auth/login.html writes it, a JS regular-expression literal.
JS_PATTERN_LINE = r"const NEXT_PATTERN = /^\/(?![/\\])[A-Za-z0-9._~/-]{0,199}$/;"


@pytest.fixture
def client() -> TestClient:
    # Not entered as a context manager: the application's startup hook stays off.
    return TestClient(app)


def _cookies(role: str) -> dict[str, str]:
    user_id = uuid.uuid4()
    jwt = tokens.issue_jwt(user_id, f"zz-{user_id.hex[:8]}@example.invalid", role)
    return {settings.jwt_cookie_name: jwt}


def _get_login(client: TestClient, params: dict[str, str], role: str | None = None):
    client.cookies.clear()
    for name, value in (_cookies(role) if role else {}).items():
        client.cookies.set(name, value)
    return client.get("/login", params=params, follow_redirects=False)


# ───────────────────────────── safe_next ─────────────────────────────


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("/msmm/", id="the-module"),
        pytest.param("/journey", id="journey"),
        pytest.param("/", id="the-root"),
        pytest.param("/admin/master/stations", id="a-deep-path"),
        pytest.param("/zz-page_1.2~x", id="every-allowed-sign"),
        pytest.param("/" + "z" * 199, id="two-hundred-characters"),
    ],
)
def test_a_path_of_this_site_is_accepted(value: str) -> None:
    assert safe_next(value) == value


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(None, id="absent"),
        pytest.param("", id="empty"),
        pytest.param("//zz.invalid", id="protocol-relative"),
        pytest.param("/\\zz.invalid", id="slash-backslash"),
        pytest.param("https://zz.invalid", id="an-address"),
        pytest.param("http://zz.invalid/journey", id="a-plain-http-address"),
        pytest.param("javascript:alert(1)", id="a-script-address"),
        pytest.param("/login", id="the-login-page"),
        pytest.param("/" + "z" * 200, id="two-hundred-and-one-characters"),
        pytest.param("journey", id="no-leading-slash"),
        pytest.param("/journey\n", id="a-line-end-at-the-end"),
        pytest.param("/jour\x00ney", id="a-null"),
        pytest.param("/jour\tney", id="a-tab"),
        pytest.param("/journey?zz=1", id="a-query"),
        pytest.param("/journey#zz", id="a-fragment"),
        pytest.param("/%2F%2Fzz.invalid", id="percent-encoded"),
        pytest.param("/zz page", id="a-space"),
        pytest.param("/zz:page", id="a-colon"),
        pytest.param("/zzé", id="a-letter-outside-ascii"),
    ],
)
def test_anything_else_is_refused(value: str | None) -> None:
    assert safe_next(value) is None


def test_every_redirect_to_login_of_the_pages_names_a_valid_next() -> None:
    """The deep links the protected pages build all survive the round trip."""
    paths = re.findall(r'_redirect_to_login\("([^"]*)"\)', PAGES_SOURCE)

    assert "/journey" in paths
    assert "/admin/users" in paths
    for path in paths:
        assert safe_next(path) == path


def test_the_pattern_is_the_design_one() -> None:
    assert pages._NEXT_PATTERN.pattern == r"^/(?![/\\])[A-Za-z0-9._~/-]{0,199}$"


# ───────────────────────────── a person already logged in ─────────────────────────────


@pytest.mark.parametrize("role", ["end_user", "content_manager", "platform_admin"])
def test_a_logged_in_person_goes_to_a_valid_next(client: TestClient, role: str) -> None:
    answer = _get_login(client, {"next": "/msmm/"}, role)

    assert answer.status_code == 303
    assert answer.headers["location"] == "/msmm/"


@pytest.mark.parametrize(
    ("role", "default"),
    [
        ("end_user", "/journey"),
        ("content_manager", "/journey"),
        ("platform_admin", "/admin/users"),
    ],
)
@pytest.mark.parametrize(
    "params",
    [
        pytest.param({}, id="no-next"),
        pytest.param({"next": "//zz.invalid"}, id="protocol-relative"),
        pytest.param({"next": "/\\zz.invalid"}, id="slash-backslash"),
        pytest.param({"next": "https://zz.invalid"}, id="an-address"),
        pytest.param({"next": "/login"}, id="the-login-page"),
    ],
)
def test_a_logged_in_person_with_no_valid_next_goes_to_the_role_default(
    client: TestClient, role: str, default: str, params: dict[str, str]
) -> None:
    answer = _get_login(client, params, role)

    assert answer.status_code == 303
    assert answer.headers["location"] == default


# ───────────────────────────── the login page ─────────────────────────────


def test_the_rendered_login_page_holds_the_pattern(client: TestClient) -> None:
    answer = _get_login(client, {"next": "/msmm/"})

    assert answer.status_code == 200
    assert JS_PATTERN_LINE in answer.text
    assert "value === '/login'" in answer.text
    assert "safeNext() || (body.role === 'platform_admin' ? '/admin/users' : '/journey')" in (
        answer.text
    )


def test_the_script_pattern_is_the_python_pattern() -> None:
    """The JS literal, with its escaped slash undone, is the Python pattern."""
    literal = JS_PATTERN_LINE.removeprefix("const NEXT_PATTERN = /").removesuffix("/;")

    assert literal.replace("\\/", "/", 1) == pages._NEXT_PATTERN.pattern


def test_next_is_read_from_the_page_address_only_never_written_into_the_page(
    client: TestClient,
) -> None:
    marker = f"/zz-{uuid.uuid4().hex[:12]}"
    refused = f"//zz-{uuid.uuid4().hex[:12]}.invalid"

    for value in (marker, refused):
        answer = _get_login(client, {"next": value})

        assert answer.status_code == 200
        assert value not in answer.text
