"""Feed-status panel (PR 2 of the NAP work): the status payload fields and
the template block that renders them.

Backend: `_refresh_one_task` records each task's latest outcome in its fetch
state; `_decorate_status` / `_format_ok` turn that state into the panel's
fields. Template: static trip-wires, same approach as
test_sessions_template_js.py — plus the wording rule that a file is
"format OK", never described as validated.
"""

from __future__ import annotations

import io
import re
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from app import feed_fetch, feed_resolvers
from app.api.admin import sessions as sessions_api
from app.api.admin.sessions import ProviderStatus, _decorate_status, _format_ok

TEMPLATE = Path(__file__).resolve().parents[2] / "app" / "templates" / "admin" / "sessions.html"
LABEL = "provider[SNCF].timetable(gtfs)"


def _gtfs_zip_member_text() -> bytes:
    """A GTFS file of 3 MiB of invented rows: big enough to be cut mid-body."""
    row = b"ZZ1,ZZ invented agency,https://zz.example,Europe/Paris\n"
    return b"agency_id,agency_name,agency_url,agency_timezone\n" + row * (
        3 * 1024 * 1024 // len(row)
    )


def _gtfs_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name in ("agency.txt", "stops.txt", "routes.txt", "trips.txt", "stop_times.txt"):
            z.writestr(name, "id\n1\n")
    return buf.getvalue()


# ─────────────────────────── last_attempt recording ───────────────────────────


@pytest.fixture
def inbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "vps-inbox"
    root.mkdir()
    monkeypatch.setattr(sessions_api.settings, "inbox_dir", root)
    monkeypatch.setattr(feed_resolvers, "_validate_safe_http_url", lambda url: url)

    def fake_place(path: Path, kind: str, *, session_id: str, staged_filename: str) -> bool:
        target = root / session_id / "gtfs" / staged_filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
        return True

    # The copy into the slot (ingestion.place, run off the event loop); the
    # rebuild queue needs a database these tests do not have.
    monkeypatch.setattr(sessions_api.ingestion, "place", fake_place)
    monkeypatch.setattr(sessions_api.ingestion, "queue_rebuild", lambda *_a, **_k: None)
    return root


async def _run(inbox: Path, response: httpx.Response) -> dict[str, Any]:
    task = sessions_api._RefreshTask(LABEL, "GTFS", "https://x.example/sncf.zip", "sncf.zip", None)
    staging = inbox / "s1" / "_staging"
    staging.mkdir(parents=True, exist_ok=True)
    transport = httpx.MockTransport(lambda r: response)
    async with httpx.AsyncClient(transport=transport) as client:
        return await sessions_api._refresh_one_task(client, None, "s1", staging, task)  # type: ignore[arg-type]


def _state(inbox: Path) -> dict[str, Any]:
    return feed_fetch.load_state(sessions_api._fetch_state_dir("s1"), LABEL)


async def test_each_refresh_records_its_outcome(inbox: Path) -> None:
    await _run(inbox, httpx.Response(200, content=_gtfs_zip(), headers={"ETag": '"v1"'}))
    first = _state(inbox)
    assert first["last_attempt"]["status"] == "fetched"
    assert first["etag"] == '"v1"'

    await _run(inbox, httpx.Response(503))
    second = _state(inbox)
    assert second["last_attempt"]["status"] == "skipped"
    assert "503" in second["last_attempt"]["reason"]
    # A failed attempt must not disturb what the next conditional GET replays.
    assert second["etag"] == '"v1"'
    assert second["sha256"] == first["sha256"]
    assert datetime.fromisoformat(second["last_attempt"]["at"]) <= datetime.now(UTC)


async def test_a_recorded_failure_does_not_make_a_304_trusted(inbox: Path) -> None:
    """State holding only `last_attempt` has no url, so no validators are sent."""
    await _run(inbox, httpx.Response(503))
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=_gtfs_zip())

    task = sessions_api._RefreshTask(LABEL, "GTFS", "https://x.example/sncf.zip", "sncf.zip", None)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out = await sessions_api._refresh_one_task(
            client, None, "s1", inbox / "s1" / "_staging", task
        )  # type: ignore[arg-type]
    assert out["status"] == "fetched"
    assert "if-none-match" not in seen[0].headers


async def test_a_resumed_download_records_how_often(
    inbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A body cut part way and resumed (app/feed_fetch.py): the count reaches
    the panel's "Last result"; a download without a cut records none."""
    monkeypatch.setattr(feed_fetch, "RESUME_DELAYS", (0.0,))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(zipfile.ZipInfo("agency.txt"), _gtfs_zip_member_text())
        for name in ("stops.txt", "routes.txt", "trips.txt", "stop_times.txt"):
            z.writestr(name, "id\n1\n")
    body = buf.getvalue()
    cut = [True]

    async def stream(data: bytes, cut_after: int | None) -> Any:
        yield data[: cut_after if cut_after is not None else len(data)]
        if cut_after is not None:
            raise httpx.ReadError("zz cut")

    def server(request: httpx.Request) -> httpx.Response:
        headers = {"ETag": '"zz-v1"', "Accept-Ranges": "bytes"}
        wanted = request.headers.get("range")
        if wanted:
            start = int(wanted.removeprefix("bytes=").removesuffix("-"))
            headers["Content-Range"] = f"bytes {start}-{len(body) - 1}/{len(body)}"
            headers["Content-Length"] = str(len(body) - start)
            return httpx.Response(206, headers=headers, content=stream(body[start:], None))
        headers["Content-Length"] = str(len(body))
        cut_after = 2 * 1024 * 1024 if cut.pop() else None
        return httpx.Response(200, headers=headers, content=stream(body, cut_after))

    task = sessions_api._RefreshTask(LABEL, "GTFS", "https://x.example/sncf.zip", "sncf.zip", None)
    staging = inbox / "s1" / "_staging"
    staging.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        out = await sessions_api._refresh_one_task(client, None, "s1", staging, task)  # type: ignore[arg-type]
    assert out["status"] == "fetched"
    assert out["resumes"] == 1
    assert _state(inbox)["last_attempt"]["resumes"] == 1

    await _run(inbox, httpx.Response(200, content=_gtfs_zip()))
    assert "resumes" not in _state(inbox)["last_attempt"]


# ─────────────────────────── status fields ───────────────────────────


def _status(**kw: Any) -> ProviderStatus:
    return ProviderStatus(feed_id="SNCF", state="ok", **kw)


def test_decorate_fills_the_grouping_keys() -> None:
    status = _status(fetched_at=datetime.now(UTC))
    provider = {
        "id": "SNCF",
        "label": "SNCF Voyageurs",
        "country_iso": "FR",
        "timetable": {
            "format": "gtfs",
            "source": "nap",
            "resolver": {"type": "tdg", "dataset_id": "x", "resource_id": 1},
        },
    }
    fetch_state = {
        "sha256": "ab",
        "checked_at": "2026-09-29T10:00:00+00:00",
        "last_attempt": {"at": "2026-09-29T10:00:00+00:00", "status": "unchanged", "reason": "r"},
    }
    _decorate_status(status, provider, fetch_state, None)
    assert (status.label, status.country_iso, status.format) == ("SNCF Voyageurs", "FR", "gtfs")
    assert status.source == "nap"
    assert status.resolver_type == "tdg"
    assert status.checked_at == datetime(2026, 9, 29, 10, tzinfo=UTC)
    assert status.last_attempt == fetch_state["last_attempt"]
    assert status.format_ok is True


def test_decorate_ignores_a_resolver_on_a_non_nap_source() -> None:
    status = _status()
    provider = {"id": "SNCF", "timetable": {"source": "url", "resolver": {"type": "tdg"}}}
    _decorate_status(status, provider, {"last_attempt": "garbage"}, None)
    assert status.resolver_type is None
    assert status.last_attempt is None


@pytest.mark.parametrize(
    ("has_file", "kw", "state", "expected"),
    [
        (False, {"source": "url"}, {"sha256": "ab"}, None),  # nothing in the slot
        (True, {"source": "url"}, {"sha256": "ab"}, True),  # arrived through the checked fetch
        (True, {"source": "nap"}, {"sha256": "ab"}, True),
        (True, {"source": "url"}, {}, None),  # predates the check
        (True, {"source": "url"}, {"last_attempt": {"status": "skipped"}}, None),
        (True, {"source": "upload", "upload_filename": "a.zip"}, {}, True),  # upload runs detect
        (True, {"source": "upload"}, {}, None),  # file put there by hand
        (True, {"source": "cross_border_filter"}, {"sha256": "ab"}, None),
    ],
)
def test_format_ok(
    has_file: bool, kw: dict[str, Any], state: dict[str, Any], expected: bool | None
) -> None:
    status = _status(fetched_at=datetime.now(UTC) if has_file else None, **kw)
    assert _format_ok(status, state) is expected


# ─────────────────────────── template ───────────────────────────


@pytest.fixture(scope="module")
def template_text() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def _panel_js(text: str) -> str:
    start = text.index("// ── Feed status panel")
    return text[start : text.index("// ── end feed status panel", start)]


def _panel_html(text: str) -> str:
    start = text.index('data-role="feed-status"')
    return text[start : text.index("</section>", start)]


def test_panel_section_is_in_every_session_detail(template_text: str) -> None:
    html = _panel_html(template_text)
    assert 'data-sid="{{ s.id }}"' in html
    assert 'data-role="feed-status-groups"' in html
    for mode in ("url", "nap", "upload", "cross_border_filter"):
        assert f'<option value="{mode}">' in html


def test_panel_renders_on_every_status_load(template_text: str) -> None:
    body = template_text[template_text.index("async function loadProviderStatuses") :]
    assert "renderFeedStatusPanel(sid, statuses);" in body[: body.index("\n}\n")]


@pytest.mark.parametrize(
    "name",
    [
        "feedModeLabel",
        "feedFormatCheckHTML",
        "feedLastResultHTML",
        "feedRowHTML",
        "feedGroupsByCountry",
        "feedCountLabel",
        "feedStateCounts",
        "feedCountryHTML",
        "renderFeedStatusPanel",
    ],
)
def test_panel_helpers_are_defined(template_text: str, name: str) -> None:
    assert re.search(rf"function\s+{name}\s*\(", _panel_js(template_text))


def test_panel_says_format_ok_never_validated(template_text: str) -> None:
    """PH decision: the only check is a format check. The UI must say
    "format OK" and must never call a feed "validated"."""
    panel = _panel_js(template_text) + _panel_html(template_text)
    assert "format OK" in panel
    assert not re.search(r"validat", panel, re.IGNORECASE)


def test_skip_reasons_are_escaped(template_text: str) -> None:
    """Skip reasons quote upstream responses — never inject them raw."""
    assert "escHTML(a.reason)" in _panel_js(template_text)


def test_resume_count_is_a_number_and_escaped(template_text: str) -> None:
    """The "resumed N times" note reads the stored count as a number and
    escapes it, like every other value of the panel."""
    js = _panel_js(template_text)
    body = js[js.index("function feedLastResultHTML") :]
    body = body[: body.index("\n}\n")]
    assert "Number(a.resumes)" in body
    assert "escHTML(String(resumes))" in body
