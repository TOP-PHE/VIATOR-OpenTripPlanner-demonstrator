"""The 403 page a logged-in person gets on a protected page their role may not open.

`_forbidden_html` (app/api/pages.py) renders forbidden.html with status 403:
the message as text (Jinja escapes it), the person's own role, a link to the
role's default page (the one `/login` uses) and a way to sign in as another
user, coming back to the page. It used to render an empty page.

Driven through the real application. No database: `get_db` is overridden and
platform_config is a stand-in (the default page `/journey` reads it). Values
are invented; user ids are drawn at run time.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from html import unescape
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from app import config_service
from app.api import pages
from app.auth import tokens
from app.db import get_db
from app.main import app
from app.settings import settings

PLATFORM_ADMIN_ONLY = "Platform admin access required."
MANAGER_OR_ADMIN = "Content-manager or platform-admin access required."

# Every page that answers 403 to some logged-in role, with its message.
PLATFORM_ADMIN_PAGES = [
    "/admin/users",
    "/admin/config",
    "/admin/sessions",
    "/admin/reports",
    "/admin/storage",
    "/admin/network-coverage",
    "/admin/nap-catalogues",
]
MANAGER_PAGES = ["/admin/master/stations"]

# (role, page, message, default page) for each role that can hit each page.
CASES = [
    pytest.param(role, page, PLATFORM_ADMIN_ONLY, "/journey", id=f"{role}-{page}")
    for role in ("end_user", "content_manager")
    for page in PLATFORM_ADMIN_PAGES
] + [
    pytest.param("end_user", page, MANAGER_OR_ADMIN, "/journey", id=f"end_user-{page}")
    for page in MANAGER_PAGES
]


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    def get_all(db: Any, **_kw: Any) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(config_service, "get_all", get_all)
    app.dependency_overrides[get_db] = lambda: object()
    try:
        # Not entered as a context manager: the application's startup hook stays off.
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


def _log_in(client: TestClient, role: str) -> None:
    client.cookies.clear()
    user_id = uuid.uuid4()
    jwt = tokens.issue_jwt(user_id, f"zz-{user_id.hex[:8]}@example.invalid", role)
    client.cookies.set(settings.jwt_cookie_name, jwt)


def _content(html: str) -> str:
    """The page's <main>, where the 403 page puts what it says."""
    return html[html.index("<main>") : html.index("</main>")]


def _href(html: str, element_id: str) -> str:
    match = re.search(rf'<a [^>]*id="{element_id}" href="([^"]*)"', html)
    assert match is not None, element_id
    return unescape(match.group(1))


# ───────────────────────────── the real pages ─────────────────────────────


def test_the_cases_cover_every_forbidden_page_of_the_router() -> None:
    """A new caller of _forbidden_html must be added to the cases above."""
    source = Path(pages.__file__).read_text(encoding="utf-8")
    assert source.count("return _forbidden_html(request, ") == len(
        PLATFORM_ADMIN_PAGES + MANAGER_PAGES
    )
    for page in PLATFORM_ADMIN_PAGES + MANAGER_PAGES:
        assert f'_redirect_to_login("{page}")' in source


@pytest.mark.parametrize(("role", "page", "message", "default"), CASES)
def test_a_forbidden_page_says_why_and_where_to_go(
    client: TestClient, role: str, page: str, message: str, default: str
) -> None:
    _log_in(client, role)

    answer = client.get(page, follow_redirects=False)

    assert answer.status_code == 403
    assert answer.headers["content-type"].startswith("text/html")
    content = _content(answer.text)
    assert "<h1>Access denied</h1>" in content
    assert f'<p id="forbidden-message">{message}</p>' in content
    assert f'<strong id="forbidden-role">{role.replace("_", " ")}</strong>' in content
    assert _href(content, "forbidden-home") == default
    assert _href(content, "forbidden-switch") == f"/login?next={page}"
    # The default page link works for this person.
    assert client.get(default, follow_redirects=False).status_code == 200


def test_the_switch_user_link_signs_out_before_opening_login(client: TestClient) -> None:
    _log_in(client, "end_user")

    html = client.get("/admin/config", follow_redirects=False).text

    script = html[html.index("document.getElementById('forbidden-switch')") :]
    script = script[: script.index("</script>")]
    assert "fetch('/api/auth/logout', {method: 'POST'})" in script
    assert "globalThis.location.href = dest;" in script
    assert script.index("/api/auth/logout") < script.index("globalThis.location.href")


def test_the_login_page_and_the_403_page_share_the_default_page_rule() -> None:
    assert pages._default_page("platform_admin") == "/admin/users"
    assert pages._default_page("content_manager") == "/journey"
    assert pages._default_page("end_user") == "/journey"


# ───────────────────────────── the message is text ─────────────────────────────


def _probe_app(message: str, path: str = "/zz-probe") -> TestClient:
    probe = FastAPI()

    @probe.get(path, response_class=HTMLResponse)
    def forbidden(request: Request) -> HTMLResponse:
        return pages._forbidden_html(request, message)

    return TestClient(probe)


@pytest.mark.parametrize("role", ["end_user", "content_manager"])
def test_the_message_is_escaped_never_markup(role: str) -> None:
    probe = _probe_app("<b>zz</b> & <script>zz()</script>")
    _log_in(probe, role)

    answer = probe.get("/zz-probe")

    assert answer.status_code == 403
    content = _content(answer.text)
    assert (
        '<p id="forbidden-message">&lt;b&gt;zz&lt;/b&gt; &amp; '
        "&lt;script&gt;zz()&lt;/script&gt;</p>" in content
    )
    assert "<b>zz</b>" not in answer.text
    assert "<script>zz()" not in answer.text
    assert _href(content, "forbidden-home") == "/journey"


def test_a_path_next_may_not_name_signs_in_without_next() -> None:
    probe = _probe_app(PLATFORM_ADMIN_ONLY, path="/zz:probe")
    _log_in(probe, "end_user")

    content = _content(probe.get("/zz:probe").text)

    assert _href(content, "forbidden-switch") == "/login"


def test_without_a_session_the_page_offers_to_sign_in() -> None:
    """No caller does this today; the page still renders, with no role."""
    probe = _probe_app(PLATFORM_ADMIN_ONLY)

    answer = probe.get("/zz-probe")

    assert answer.status_code == 403
    content = _content(answer.text)
    assert f'<p id="forbidden-message">{PLATFORM_ADMIN_ONLY}</p>' in content
    assert "forbidden-role" not in content
    assert _href(content, "forbidden-home") == "/login?next=/zz-probe"
