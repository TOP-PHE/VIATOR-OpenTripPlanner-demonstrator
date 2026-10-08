"""The station module's licence notice on /journey, and the administrator's menu link.

- `/journey` awaits `station_module.attribution()` and renders, under its
  title, the module's statement and each licence group: Jinja's escaping of
  every value, a licence a link (`rel="noopener"`) only to an http(s)
  address, and no notice at all without an answer.
- `_base.html` shows a "Stations (MSMM)" link to `/msmm/` to a platform
  administrator only, and only when VIATOR uses the module: the templating
  global `station_module_enabled`, which follows `station_module.enabled()`
  (both STATION_MODULE_URL and STATION_MODULE_TOKEN set).

Driven through the real application. No database: `get_db` is overridden and
platform_config is a stand-in. The module is never reached: its calls go
through `httpx.MockTransport`. Invented values only: ZZ names, a `.invalid`
address, a token drawn at run time.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import secrets
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app import config_service, station_module
from app.api import pages
from app.auth import tokens
from app.db import get_db
from app.main import app
from app.settings import settings

MODULE_URL = "http://msmm.invalid:8000"
LICENCE_URL = "https://zz-licence.invalid/odbl"
STATEMENT = "Station data: built by the ZZ module from the sources below."

ATTRIBUTION = {
    "statement": STATEMENT,
    "sources": [
        {"licence": "ZZ licence A", "licence_url": None, "labels": ["ZZ source 1", "ZZ source 2"]},
        {"licence": "ZZ open licence", "licence_url": LICENCE_URL, "labels": ["ZZ source 3"]},
    ],
}


# Where each stand-in platform_config read ran: "off-loop" (a worker thread)
# or "on-loop" (the event loop, which a database call must never block); and
# "module-call" for each call of the module, to prove their order.
CONFIG_READS: list[str] = []


class Module:
    """A stand-in for the module: answers `response`, records every request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.response = httpx.Response(200, json=ATTRIBUTION)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        CONFIG_READS.append("module-call")
        return self.response


@pytest.fixture
def module(monkeypatch: pytest.MonkeyPatch) -> Iterator[Module]:
    stand_in = Module()
    transport = httpx.MockTransport(stand_in)
    real_async = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async(*args, **kwargs)

    monkeypatch.setattr(station_module.httpx, "AsyncClient", factory)
    station_module.reset()
    yield stand_in
    station_module.reset()


@pytest.fixture
def module_on(monkeypatch: pytest.MonkeyPatch, module: Module) -> None:
    monkeypatch.setattr(settings, "station_module_url", MODULE_URL)
    monkeypatch.setattr(settings, "station_module_token", SecretStr(secrets.token_hex(32)))


@pytest.fixture
def module_off(monkeypatch: pytest.MonkeyPatch, module: Module) -> None:
    monkeypatch.setattr(settings, "station_module_url", "")
    monkeypatch.setattr(settings, "station_module_token", SecretStr(""))


@pytest.fixture
def platform_config(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stands in for platform_config: the page reads it without a database."""
    values: dict[str, Any] = {}

    def get_all(db: Any, **_kw: Any) -> dict[str, Any]:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            CONFIG_READS.append("off-loop")
        else:
            CONFIG_READS.append("on-loop")
        return dict(values)

    CONFIG_READS.clear()
    monkeypatch.setattr(config_service, "get_all", get_all)
    return values


@pytest.fixture
def client(platform_config: dict[str, Any]) -> Iterator[TestClient]:
    app.dependency_overrides[get_db] = lambda: object()
    try:
        # Not entered as a context manager: the application's startup hook stays off.
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


def _get(client: TestClient, path: str, role: str | None = "end_user"):
    client.cookies.clear()
    if role is not None:
        user_id = uuid.uuid4()
        jwt = tokens.issue_jwt(user_id, f"zz-{user_id.hex[:8]}@example.invalid", role)
        client.cookies.set(settings.jwt_cookie_name, jwt)
    return client.get(path, follow_redirects=False)


def _notice(html: str) -> str:
    """The notice paragraph of the page, or '' when there is none."""
    start = html.find('<p class="station-attribution">')
    if start < 0:
        return ""
    return html[start : html.index("</p>", start)]


# ───────────────────────────── the notice ─────────────────────────────


def test_the_notice_shows_the_statement_and_each_licence_group(
    client: TestClient, module_on: None, module: Module
) -> None:
    answer = _get(client, "/journey")

    assert answer.status_code == 200
    notice = _notice(answer.text)
    assert STATEMENT in notice
    assert "ZZ licence A: ZZ source 1, ZZ source 2" in notice
    assert (
        f'<a href="{LICENCE_URL}" target="_blank" rel="noopener">ZZ open licence</a>: ZZ source 3'
        in notice
    )
    # Under the title.
    assert answer.text.index("<h1>Search a journey</h1>") < answer.text.index(notice)
    # A licence without an address is no link.
    assert "ZZ licence A</a>" not in notice
    assert len(module.requests) == 1
    assert module.requests[0].url.path == "/internal/v1/attribution"


def test_every_value_of_the_notice_is_escaped(
    client: TestClient, module_on: None, module: Module
) -> None:
    module.response = httpx.Response(
        200,
        json={
            "statement": "<script>zz()</script> ZZ statement",
            "sources": [
                {
                    "licence": "<b>ZZ licence</b>",
                    "licence_url": 'https://zz.invalid/"><img src=x onerror=zz()>',
                    "labels": ["<i>ZZ label</i>", "ZZ & co"],
                },
                {"licence": "<u>ZZ plain</u>", "licence_url": None, "labels": ["ZZ source"]},
            ],
        },
    )

    notice = _notice(_get(client, "/journey").text)

    assert "&lt;script&gt;zz()&lt;/script&gt; ZZ statement" in notice
    assert "&lt;b&gt;ZZ licence&lt;/b&gt;" in notice
    assert "&lt;i&gt;ZZ label&lt;/i&gt;, ZZ &amp; co" in notice
    assert 'href="https://zz.invalid/&#34;&gt;&lt;img src=x onerror=zz()&gt;"' in notice
    # A licence without a link (the CRD case) is escaped as well.
    assert "<br>\n    &lt;u&gt;ZZ plain&lt;/u&gt;: ZZ source" in notice
    for raw in ("<script>", "<b>", "<i>", "<img", "<u>"):
        assert raw not in notice


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("javascript:zz()", id="a-script-address"),
        pytest.param("JAVASCRIPT:zz()", id="a-script-address-in-capitals"),
        pytest.param("data:text/html,zz", id="a-data-address"),
        pytest.param("//zz.invalid/licence", id="protocol-relative"),
        pytest.param("zz-licence", id="not-an-address"),
    ],
)
def test_a_licence_address_that_is_not_http_gives_no_link(
    client: TestClient, module_on: None, module: Module, url: str
) -> None:
    module.response = httpx.Response(
        200,
        json={
            "statement": STATEMENT,
            "sources": [{"licence": "ZZ licence", "licence_url": url, "labels": ["ZZ source"]}],
        },
    )

    notice = _notice(_get(client, "/journey").text)

    assert "ZZ licence: ZZ source" in notice
    assert "<a " not in notice
    assert url not in notice


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("javascript:zz()", id="a-script-address"),
        pytest.param("data:text/html,zz", id="a-data-address"),
        pytest.param(" https://zz.invalid/licence", id="a-space-first"),
    ],
)
def test_the_template_checks_the_address_again(url: str) -> None:
    """Even an answer that would bypass the client's own check gets no link."""
    tpl = pages.templates.env.get_template("journey.html")
    html = tpl.render(
        current_user=None,
        station_attribution={
            "statement": STATEMENT,
            "sources": [{"licence": "ZZ licence", "licence_url": url, "labels": ["ZZ source"]}],
        },
    )

    notice = _notice(html)
    assert "ZZ licence: ZZ source" in notice
    assert "<a " not in notice


def test_a_plain_http_licence_address_is_a_link(
    client: TestClient, module_on: None, module: Module
) -> None:
    url = "http://zz.invalid/licence"
    module.response = httpx.Response(
        200,
        json={
            "statement": STATEMENT,
            "sources": [{"licence": "ZZ licence", "licence_url": url, "labels": ["ZZ source"]}],
        },
    )

    notice = _notice(_get(client, "/journey").text)

    assert f'<a href="{url}" target="_blank" rel="noopener">ZZ licence</a>: ZZ source' in notice


def test_no_notice_and_no_fallback_line_without_the_module(
    client: TestClient, module_off: None, module: Module, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG, logger=station_module.log.name):
        answer = _get(client, "/journey")

    assert answer.status_code == 200
    assert 'station-attribution">' not in answer.text
    assert module.requests == []
    # A VIATOR without the module does not log `reason=off` on every page.
    assert "station_module.fallback" not in caplog.text


@pytest.mark.parametrize("on", [True, False], ids=["with-the-module", "without-the-module"])
def test_platform_config_is_read_off_the_event_loop_after_the_module(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, module: Module, on: bool
) -> None:
    monkeypatch.setattr(settings, "station_module_url", MODULE_URL if on else "")
    monkeypatch.setattr(
        settings, "station_module_token", SecretStr(secrets.token_hex(32) if on else "")
    )

    assert _get(client, "/journey").status_code == 200
    # Off the event loop; and after the module's call, so that no database
    # connection is held while the page waits for the module.
    assert (["module-call", "off-loop"] if on else ["off-loop"]) == CONFIG_READS


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(httpx.Response(503, json={"detail": "ZZ", "code": "database"}), id="503"),
        pytest.param(httpx.Response(403, json={"detail": "ZZ", "code": "token"}), id="403"),
        pytest.param(httpx.Response(200, json={"sources": []}), id="a-wrong-shape"),
    ],
)
def test_no_notice_when_the_module_fails_and_no_call_during_the_pause(
    client: TestClient, module_on: None, module: Module, response: httpx.Response
) -> None:
    module.response = response

    first = _get(client, "/journey")
    second = _get(client, "/journey")

    assert first.status_code == second.status_code == 200
    assert _notice(first.text) == _notice(second.text) == ""
    assert len(module.requests) == 1


def test_the_answer_is_kept_between_pages(
    client: TestClient, module_on: None, module: Module
) -> None:
    first = _get(client, "/journey")
    second = _get(client, "/journey")

    assert STATEMENT in _notice(first.text)
    assert STATEMENT in _notice(second.text)
    assert len(module.requests) == 1


def test_an_anonymous_visitor_is_sent_to_login_without_a_call(
    client: TestClient, module_on: None, module: Module
) -> None:
    answer = _get(client, "/journey", role=None)

    assert answer.status_code == 303
    assert answer.headers["location"] == "/login?next=/journey"
    assert module.requests == []


@pytest.mark.parametrize(
    ("config", "ojp", "hafas"),
    [
        pytest.param({}, False, False, id="nothing-set"),
        pytest.param(
            {"OJP_COMPARISON_ENABLED": True, "OJP_API_TOKEN": "", "HAFAS_COMPARISON_ENABLED": True},
            False,
            True,
            id="ojp-without-a-token",
        ),
        pytest.param(
            {
                "OJP_COMPARISON_ENABLED": True,
                "OJP_API_TOKEN": "zz",
                "HAFAS_COMPARISON_ENABLED": False,
            },
            True,
            False,
            id="ojp-with-a-token",
        ),
    ],
)
def test_the_reference_engine_checkboxes_still_follow_platform_config(
    client: TestClient,
    module_off: None,
    platform_config: dict[str, Any],
    config: dict[str, Any],
    ojp: bool,
    hafas: bool,
) -> None:
    platform_config.update(config)

    html = _get(client, "/journey").text

    assert ('id="compare-ojp"' in html) is ojp
    assert ('id="compare-hafas"' in html) is hafas


# ───────────────────────────── the menu link ─────────────────────────────

MENU_LINK = '<a href="/msmm/" title="Station mapping module">Stations (MSMM)</a>'


@pytest.mark.parametrize(
    ("enabled", "role", "shown"),
    [
        pytest.param(True, "platform_admin", True, id="admin-with-the-module"),
        pytest.param(True, "content_manager", False, id="content-manager-with-the-module"),
        pytest.param(True, "end_user", False, id="end-user-with-the-module"),
        pytest.param(False, "platform_admin", False, id="admin-without-the-module"),
        pytest.param(False, "end_user", False, id="end-user-without-the-module"),
    ],
)
def test_the_menu_link_only_for_an_administrator_and_only_with_the_module(
    client: TestClient,
    module_off: None,
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
    role: str,
    shown: bool,
) -> None:
    monkeypatch.setitem(pages.templates.env.globals, "station_module_enabled", enabled)

    answer = _get(client, "/journey", role=role)

    assert answer.status_code == 200
    assert (MENU_LINK in answer.text) is shown


def test_no_menu_link_for_a_visitor_not_logged_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(pages.templates.env.globals, "station_module_enabled", True)

    html = pages.templates.env.get_template("_base.html").render(current_user=None)

    assert "/msmm/" not in html


@pytest.mark.parametrize(
    ("url", "token", "enabled"),
    [
        pytest.param(MODULE_URL, True, True, id="both-set"),
        pytest.param(MODULE_URL, False, False, id="the-address-only"),
        pytest.param("", True, False, id="the-token-only"),
        pytest.param("", False, False, id="neither"),
    ],
)
def test_the_global_follows_the_client_rule(
    monkeypatch: pytest.MonkeyPatch, url: str, token: bool, enabled: bool
) -> None:
    monkeypatch.setattr(settings, "station_module_url", url)
    monkeypatch.setattr(
        settings, "station_module_token", SecretStr(secrets.token_hex(32) if token else "")
    )
    import app.templating as templating

    try:
        importlib.reload(templating)
        assert templating.templates.env.globals["station_module_enabled"] is enabled
        assert station_module.enabled() is enabled
    finally:
        monkeypatch.undo()
        importlib.reload(templating)
