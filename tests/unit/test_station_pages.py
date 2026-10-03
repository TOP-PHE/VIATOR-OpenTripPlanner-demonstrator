"""The station panel's nav group and its page routes (unit 5).

A page guard reads the JWT and nothing else, so every page is rendered here
without a database, once per role. The integration suite does the same through
the real app.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from app.api import pages
from tests.station_fixtures import STATION_TABS, assert_scripts_parse, page_request

LEGACY = "/admin/master/stations"

# path -> (page function, who may open it)
BOTH = ("platform_admin", "content_manager")
PAGES: dict[str, tuple[Callable[..., Any], tuple[str, ...]]] = {
    "/admin/stations/nap": (pages.admin_station_nap_page, BOTH),
    "/admin/stations/registers": (pages.admin_station_registers_page, BOTH),
    "/admin/stations/trainline": (pages.admin_station_trainline_page, BOTH),
    "/admin/stations/reference": (pages.admin_station_reference_page, BOTH),
    "/admin/stations/sources": (pages.admin_station_sources_page, ("platform_admin",)),
    LEGACY: (pages.admin_master_stations_page, BOTH),
}
TITLES = {
    "/admin/stations/nap": "NAP station information",
    "/admin/stations/registers": "Infrastructure registers",
    "/admin/stations/trainline": "Trainline codes",
    "/admin/stations/reference": "VIATOR station reference",
    "/admin/stations/sources": "Sources and integration",
    LEGACY: "Trainline codes",
}


def render(path: str, role: str | None) -> Any:
    return PAGES[path][0](page_request(path, role))


def nav_of(html: str) -> str:
    match = re.search(r"<nav>(.*?)</nav>", html, re.DOTALL)
    assert match
    return match.group(1)


# ── the five routes, for both roles ────────────────────────────────────


def test_there_are_five_station_screens() -> None:
    assert set(STATION_TABS) == set(PAGES) - {LEGACY}
    assert len(STATION_TABS) == 5


@pytest.mark.parametrize("path", sorted(PAGES))
@pytest.mark.parametrize("role", ["platform_admin", "content_manager", "end_user"])
def test_each_page_renders_for_exactly_the_roles_it_is_for(path: str, role: str) -> None:
    response = render(path, role)
    if role in PAGES[path][1]:
        assert response.status_code == 200
        html = response.body.decode()
        assert f"<h1>{TITLES[path]}</h1>" in html
        assert "TrackOnPath SAS" in html  # the base template's footer
    else:
        # Authenticated but not allowed: 403, not a redirect.
        assert response.status_code == 403


@pytest.mark.parametrize("path", sorted(PAGES))
def test_an_anonymous_browser_is_redirected_to_login(path: str) -> None:
    response = render(path, None)
    assert response.status_code == 303
    assert response.headers["location"] == f"/login?next={path}"


def test_the_old_address_serves_the_same_page_as_the_trainline_screen() -> None:
    old = render(LEGACY, "content_manager").body.decode()
    new = render("/admin/stations/trainline", "content_manager").body.decode()
    assert old == new
    assert 'id="search-form"' in old  # the panel itself, not a stub


def test_the_trainline_screen_says_what_it_is_for() -> None:
    html = render("/admin/stations/trainline", "content_manager").body.decode()
    assert "One input among several" in html
    assert "matched by railway code only" in html
    assert "<h1>Master stations</h1>" not in html


# ── the tab bar on every screen ────────────────────────────────────────


@pytest.mark.parametrize("path", STATION_TABS)
def test_the_active_tab_is_marked(path: str) -> None:
    html = render(path, "platform_admin").body.decode()
    assert f'href="{path}" aria-current="page"' in html
    # Exactly one link is the current page (the stylesheet names the attribute too).
    assert len(re.findall(r'<a href="[^"]+" aria-current="page">', html)) == 1
    for other in STATION_TABS:
        assert f'href="{other}"' in html


def test_a_content_manager_sees_four_tabs() -> None:
    html = render("/admin/stations/reference", "content_manager").body.decode()
    tabs = re.search(r'<nav class="sp-tabs".*?</nav>', html, re.DOTALL)
    assert tabs
    assert re.findall(r'href="(/admin/stations/\w+)"', tabs.group(0)) == list(STATION_TABS[:4])


# ── the nav group ──────────────────────────────────────────────────────


def test_a_content_manager_gets_the_stations_group_with_four_entries() -> None:
    nav = nav_of(render("/admin/stations/reference", "content_manager").body.decode())
    group = re.search(r'<details class="nav-group nav-group-left">(.*?)</details>', nav, re.DOTALL)
    assert group, "the Stations group is missing for a content manager"
    assert "<summary>Stations</summary>" in group.group(1)
    assert 'role="menu"' in group.group(1)
    assert re.findall(r'href="([^"]+)" role="menuitem"', group.group(1)) == list(STATION_TABS[:4])
    # The group is in the dual-role block, not the platform-admin one: nothing
    # else of the platform-admin block is in a content manager's nav.
    assert "/admin/sessions" not in nav
    assert "Admin dashboard" not in nav
    assert "/admin/stations/sources" not in nav


def test_a_platform_admin_gets_all_five_entries() -> None:
    nav = nav_of(render("/admin/stations/reference", "platform_admin").body.decode())
    group = re.search(r'<details class="nav-group nav-group-left">(.*?)</details>', nav, re.DOTALL)
    assert group
    assert re.findall(r'href="([^"]+)" role="menuitem"', group.group(1)) == list(STATION_TABS)
    # The pre-existing platform-admin group is still there, after it.
    assert nav.index("<summary>Stations</summary>") < nav.index(
        "<summary>Admin dashboard</summary>"
    )
    assert "/admin/sessions" in nav


def test_the_single_stations_link_is_replaced_by_the_group() -> None:
    nav = nav_of(render("/admin/stations/reference", "platform_admin").body.decode())
    assert '<a href="/admin/master/stations">Stations</a>' not in nav
    assert nav.count("<summary>Stations</summary>") == 1


def test_an_end_user_has_no_stations_group() -> None:
    from app.templating import templates
    from tests.station_fixtures import page_request as request_for

    request = request_for("/journey", "end_user")
    html = templates.TemplateResponse(
        request, "_base.html", {"current_user": pages._maybe_user(request)}
    ).body.decode()
    nav = nav_of(html)
    assert "Stations" not in nav
    assert "/admin/stations/" not in nav
    assert '<a href="/journey">Search</a>' in nav


# ── inline JavaScript ──────────────────────────────────────────────────


@pytest.mark.parametrize("path", STATION_TABS)
def test_every_inline_script_parses(path: str, tmp_path: Path) -> None:
    html = render(path, "platform_admin").body.decode()
    assert assert_scripts_parse(html, tmp_path) >= 2  # the shared helpers and the logout script
