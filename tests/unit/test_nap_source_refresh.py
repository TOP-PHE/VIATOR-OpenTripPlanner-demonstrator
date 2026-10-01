"""`timetable.source = "nap"` wired through the refresh path.

Covers the glue in app/api/admin/sessions.py + app/ingestion.py: a nap
provider validates, becomes a refresh task carrying its resolver, and
`_refresh_one_task` resolves → downloads → validates → dispatches, or
reports `unchanged` / `skipped` without touching the slot.
"""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from app import feed_resolvers, ingestion
from app.api.admin import sessions as sessions_api
from app.api.admin.sessions import (
    _bucket_outcome,
    _build_refresh_tasks,
    _derive_provider_status,
    _refresh_one_task,
)

TDG = {"type": "tdg", "dataset_id": "64635525318cc75a9a8a771f", "resource_id": 81653}
LABEL = "provider[TRENITAL-FR].timetable(gtfs)"


def _nap_config() -> dict[str, Any]:
    return {
        "sources": {
            "providers": [
                {
                    "id": "TRENITAL-FR",
                    "label": "Trenitalia France",
                    "country_iso": "FR",
                    "timetable": {"format": "gtfs", "source": "nap", "resolver": TDG},
                }
            ]
        }
    }


def _gtfs_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name in ("agency.txt", "stops.txt", "routes.txt", "trips.txt", "stop_times.txt"):
            z.writestr(name, "id\n1\n")
    return buf.getvalue()


# ─────────────────────────── validation ───────────────────────────


def test_nap_provider_validates_and_keeps_its_resolver() -> None:
    (p,) = ingestion.normalize_providers(_nap_config())
    assert p["timetable"] == {"format": "gtfs", "source": "nap", "resolver": TDG}


def test_nap_provider_without_resolver_is_rejected() -> None:
    cfg = _nap_config()
    del cfg["sources"]["providers"][0]["timetable"]["resolver"]
    with pytest.raises(ValueError, match=r"providers\[0\]\.timetable\.resolver"):
        ingestion.normalize_providers(cfg)


def test_nap_provider_becomes_a_task_carrying_its_resolver() -> None:
    (task,) = _build_refresh_tasks(_nap_config())
    assert task.label == LABEL
    assert task.kind == "GTFS"
    assert task.url == "https://transport.data.gouv.fr/resources/81653/download"
    assert task.staged_filename == "trenital-fr.zip"
    assert task.resolver == TDG


def test_bucket_outcome_files_unknown_status_as_skipped() -> None:
    buckets: dict[str, list[dict[str, Any]]] = {"fetched": [], "unchanged": [], "skipped": []}
    _bucket_outcome(buckets, {"status": "unchanged", "key": "a"})
    _bucket_outcome(buckets, {"status": "weird", "key": "b"})
    assert buckets == {"fetched": [], "unchanged": [{"key": "a"}], "skipped": [{"key": "b"}]}


# ─────────────────────────── _refresh_one_task ───────────────────────────


@pytest.fixture
def inbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "vps-inbox"
    root.mkdir()
    monkeypatch.setattr(sessions_api.settings, "inbox_dir", root)
    monkeypatch.setattr(feed_resolvers, "_validate_safe_http_url", lambda url: url)
    return root


@pytest.fixture
def dispatched(monkeypatch: pytest.MonkeyPatch, inbox: Path) -> list[str]:
    """Stand-in for ingestion.dispatch: lands the file in the slot, records it."""
    calls: list[str] = []

    def fake_dispatch(path: Path, kind: str, db: Any, *, session_id: str, staged_filename: str):
        target = inbox / session_id / "gtfs" / staged_filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
        calls.append(kind)
        return True

    monkeypatch.setattr(sessions_api.ingestion, "dispatch", fake_dispatch)
    return calls


def _portal(download: Any) -> Any:
    """transport.data.gouv.fr: dataset API + the /download endpoint."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/api/datasets/"):
            return httpx.Response(200, json={"resources": [{"id": 81653, "is_available": True}]})
        return download(request)

    return handler


async def _run(handler: Any, inbox: Path) -> dict[str, Any]:
    (task,) = _build_refresh_tasks(_nap_config())
    staging = inbox / "s1" / "_staging"
    staging.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await _refresh_one_task(client, None, "s1", staging, task)  # type: ignore[arg-type]


async def test_fetch_then_304_is_unchanged_without_a_second_dispatch(
    inbox: Path, dispatched: list[str]
) -> None:
    body = _gtfs_zip()
    first = await _run(
        _portal(lambda r: httpx.Response(200, content=body, headers={"ETag": '"v1"'})), inbox
    )
    assert first["status"] == "fetched"
    assert first["url"] == "https://transport.data.gouv.fr/resources/81653/download"
    assert dispatched == ["GTFS"]
    assert (inbox / "s1" / "gtfs" / "trenital-fr.zip").read_bytes() == body

    def conditional(request: httpx.Request) -> httpx.Response:
        assert request.headers["if-none-match"] == '"v1"'
        return httpx.Response(304)

    second = await _run(_portal(conditional), inbox)
    assert second["status"] == "unchanged"
    assert dispatched == ["GTFS"], "an unchanged feed must not rotate the slot or queue a rebuild"
    checked = sessions_api._fetch_checked_at("s1", LABEL)
    assert checked is not None
    assert datetime.now(UTC) - checked < timedelta(minutes=1)
    # nothing left in staging either way
    assert list((inbox / "s1" / "_staging").iterdir()) == []


async def test_html_stub_is_skipped_and_the_slot_kept(inbox: Path, dispatched: list[str]) -> None:
    slot = inbox / "s1" / "gtfs" / "trenital-fr.zip"
    slot.parent.mkdir(parents=True)
    slot.write_bytes(b"previous good file")
    outcome = await _run(
        _portal(
            lambda r: httpx.Response(200, content=b"<!DOCTYPE html><title>Maintenance</title>")
        ),
        inbox,
    )
    assert outcome["status"] == "skipped"
    assert "HTML" in outcome["reason"]
    assert dispatched == []
    assert slot.read_bytes() == b"previous good file"


async def test_resolver_failure_is_skipped(inbox: Path, dispatched: list[str]) -> None:
    outcome = await _run(lambda r: httpx.Response(503), inbox)
    assert outcome["status"] == "skipped"
    assert outcome["reason"].startswith("NAP resolver failed")
    assert dispatched == []


async def test_dispatch_failure_is_skipped_and_state_not_saved(
    inbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*a: Any, **k: Any) -> bool:
        raise OSError("disk full")

    monkeypatch.setattr(sessions_api.ingestion, "dispatch", boom)
    outcome = await _run(_portal(lambda r: httpx.Response(200, content=_gtfs_zip())), inbox)
    assert outcome["status"] == "skipped"
    assert "dispatch failed: disk full" in outcome["reason"]
    assert sessions_api._fetch_checked_at("s1", LABEL) is None
    assert list((inbox / "s1" / "_staging").iterdir()) == []


async def test_failure_after_resolution_shows_the_resolved_url(
    inbox: Path, dispatched: list[str]
) -> None:
    resolver = {"type": "permalink", "url": "https://nap.example/netex_{timetable_year}/permalink"}
    task = sessions_api._RefreshTask(
        "provider[SBB].timetable(netex_epip)", "NeTEx-EPIP", "display", "sbb.zip", None, resolver
    )
    staging = inbox / "s1" / "_staging"
    staging.mkdir(parents=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))) as c:
        outcome = await _refresh_one_task(c, None, "s1", staging, task)  # type: ignore[arg-type]
    assert outcome["status"] == "skipped"
    assert outcome["url"].startswith("https://nap.example/netex_20")
    assert "{timetable_year}" not in outcome["url"]


async def test_nap_download_redirect_to_a_private_address_is_blocked(
    inbox: Path, dispatched: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def guard(url: str) -> str:
        if "10.0.0.5" in url:
            raise ValueError("resolves to non-public address")
        return url

    monkeypatch.setattr(feed_resolvers, "_validate_safe_http_url", guard)

    def download(request: httpx.Request) -> httpx.Response:
        if request.url.host == "10.0.0.5":
            return httpx.Response(200, content=_gtfs_zip())
        return httpx.Response(302, headers={"Location": "http://10.0.0.5/internal.zip"})

    (task,) = _build_refresh_tasks(_nap_config())
    staging = inbox / "s1" / "_staging"
    staging.mkdir(parents=True)
    handler = _portal(download)
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as c:
        outcome = await _refresh_one_task(c, None, "s1", staging, task)  # type: ignore[arg-type]
        assert c.event_hooks["request"] == []
    assert outcome["status"] == "skipped"
    assert "non-public" in outcome["reason"]
    assert dispatched == []


async def test_overlapping_refreshes_get_distinct_staging_names(
    inbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    async def fake_fetch(client: Any, **kw: Any) -> Any:
        seen.append(kw["base_name"])
        raise sessions_api.feed_fetch.FetchError("stop here")

    monkeypatch.setattr(sessions_api.feed_fetch, "fetch_validated", fake_fetch)
    task = sessions_api._RefreshTask("provider[X].mct", "SNCF-MCT", "https://x/y.csv", None, None)
    async with httpx.AsyncClient() as client:
        for _ in range(2):
            await _refresh_one_task(client, None, "s1", inbox, task)  # type: ignore[arg-type]
    assert len(set(seen)) == 2


async def test_non_http_url_is_skipped(inbox: Path) -> None:
    task = sessions_api._RefreshTask("provider[X].mct", "SNCF-MCT", "ftp://x/y.csv", None, None)
    async with httpx.AsyncClient() as client:
        outcome = await _refresh_one_task(client, None, "s1", inbox, task)  # type: ignore[arg-type]
    assert outcome == {
        "status": "skipped",
        "key": "provider[X].mct",
        "url": "ftp://x/y.csv",
        "reason": "not an http(s) URL",
    }


def test_current_target_exists_per_kind(inbox: Path) -> None:
    assert not sessions_api._current_target_exists("s1", "GTFS", "a.zip")
    (inbox / "s1" / "gtfs").mkdir(parents=True)
    (inbox / "s1" / "gtfs" / "a.zip").write_bytes(b"x")
    assert sessions_api._current_target_exists("s1", "GTFS", "a.zip")
    assert not sessions_api._current_target_exists("s1", "NeTEx-FR-Horaires", None)


def test_shared_db_slot_never_counts_as_current(inbox: Path) -> None:
    """runtime/SNCF-MCT holds whichever provider loaded last — it proves
    nothing about this task's file, so a 304 must not be trusted."""
    (inbox / "s1" / "runtime" / "SNCF-MCT").mkdir(parents=True)
    (inbox / "s1" / "runtime" / "SNCF-MCT" / "latest.csv").write_bytes(b"x")
    assert not sessions_api._current_target_exists("s1", "SNCF-MCT", None)
    assert not sessions_api._current_target_exists("s1", "SNCF-Stations", None)


def test_upload_forgets_every_task_that_writes_the_slot(inbox: Path) -> None:
    from app import feed_fetch

    state_dir = sessions_api._fetch_state_dir("s1")
    epip = "provider[SNCB].timetable(netex_epip)"
    nordic = "provider[SNCB].timetable(netex_nordic)"
    gtfs = "provider[SNCB].timetable(gtfs)"
    other = "provider[OTHER].timetable(netex_epip)"
    for label in (epip, nordic, gtfs, other, "osm_pbf"):
        feed_fetch.save_state(state_dir, label, {"etag": "x"})

    sessions_api._forget_fetch_state_for_slot("s1", "NeTEx-EPIP", "sncb.zip")
    assert feed_fetch.load_state(state_dir, epip) == {}
    assert feed_fetch.load_state(state_dir, nordic) == {}  # same netex/sncb.zip slot
    assert feed_fetch.load_state(state_dir, gtfs) == {"etag": "x"}  # gtfs/ is another slot
    assert feed_fetch.load_state(state_dir, other) == {"etag": "x"}

    sessions_api._forget_fetch_state_for_slot("s1", "OSM-PBF", None)
    assert feed_fetch.load_state(state_dir, "osm_pbf") == {}
    sessions_api._forget_fetch_state_for_slot("s1", "SNCF-MCT", None)  # no-op, no crash


def test_fetch_checked_at_tolerates_garbage(inbox: Path) -> None:
    state_dir = sessions_api._fetch_state_dir("s1")
    from app import feed_fetch

    feed_fetch.save_state(state_dir, LABEL, {"checked_at": "not-a-date"})
    assert sessions_api._fetch_checked_at("s1", LABEL) is None


# ─────────────────────────── provider status ───────────────────────────


def _old_slot(root: Path, hours: float) -> None:
    import os

    target = root / "gtfs" / "trenital-fr.zip"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"x")
    ts = (datetime.now(UTC) - timedelta(hours=hours)).timestamp()
    os.utime(target, (ts, ts))


def test_recent_unchanged_check_keeps_an_old_file_fresh(tmp_path: Path) -> None:
    _old_slot(tmp_path, hours=72)
    now = datetime.now(UTC)
    status = _derive_provider_status(
        feed_id="TRENITAL-FR",
        timetable_format="gtfs",
        inbox_root=tmp_path,
        latest_audit_meta={"fetched": [], "unchanged": [LABEL], "skipped": []},
        now=now,
        checked_at=now - timedelta(hours=1),
    )
    assert status.state == "ok"
    assert status.error_hint is None


def test_unchanged_timetable_counts_as_this_providers_success(tmp_path: Path) -> None:
    """Mirror of the case above: another task of the same provider failed,
    but its timetable was confirmed current — no failure hint. Fails if
    `unchanged` stops counting as a successful attempt."""
    _old_slot(tmp_path, hours=1)
    status = _derive_provider_status(
        feed_id="TRENITAL-FR",
        timetable_format="gtfs",
        inbox_root=tmp_path,
        latest_audit_meta={
            "fetched": [],
            "unchanged": [LABEL],
            "skipped": ["provider[TRENITAL-FR].mct"],
        },
        now=datetime.now(UTC),
    )
    assert status.error_hint is None


def test_only_skipped_gets_the_failure_hint(tmp_path: Path) -> None:
    _old_slot(tmp_path, hours=1)
    status = _derive_provider_status(
        feed_id="TRENITAL-FR",
        timetable_format="gtfs",
        inbox_root=tmp_path,
        latest_audit_meta={"fetched": [], "unchanged": [], "skipped": [LABEL]},
        now=datetime.now(UTC),
    )
    assert status.error_hint == "last refresh failed — using previous file"


def test_without_a_check_an_old_file_is_stale(tmp_path: Path) -> None:
    _old_slot(tmp_path, hours=72)
    status = _derive_provider_status(
        feed_id="TRENITAL-FR",
        timetable_format="gtfs",
        inbox_root=tmp_path,
        latest_audit_meta=None,
        now=datetime.now(UTC),
    )
    assert status.state == "stale"


# ─────────────────────────── shipped source map ───────────────────────────


def test_eu19_source_map_validates() -> None:
    """app/data/eu19_nap_sources.json feeds a live session PATCH — every entry
    must pass the same provider validation the PATCH runs."""
    import json

    path = Path(__file__).resolve().parents[2] / "app" / "data" / "eu19_nap_sources.json"
    entries = {
        k: v for k, v in json.loads(path.read_text(encoding="utf-8")).items() if k != "_comment"
    }
    providers = [{"id": pid, "timetable": tt} for pid, tt in entries.items()]
    cleaned = ingestion.normalize_providers({"sources": {"providers": providers}})
    assert len(cleaned) == 27
    assert {p["timetable"]["source"] for p in cleaned} == {"nap", "url"}


# ─────────────────────────── login portals (json_api + credential) ───────────────────────────

AT_RESOLVER = {
    "type": "json_api",
    "url": "https://data.example.at/api/public/v1/data-sets",
    "items": "",
    "match": {"name": "ÖBB"},
    "download": "https://data.example.at/api/public/v1/data-sets/{id}/file",
}
AT_LOGIN = {
    "token_url": "https://user.example.at/token",
    "client_id": "dbp-script-download",
    "username": "u",
    "password": "p",
}


class _CredDb:
    def __init__(self, cred: Any) -> None:
        self.cred = cred

    def get(self, model: Any, key: Any) -> Any:
        return self.cred

    def flush(self) -> None: ...


def _login_cred(plaintext: str, auth_type: str = "oauth2_password") -> Any:
    from types import SimpleNamespace

    from app import credentials as crypto

    ciphertext, nonce = crypto.encrypt(plaintext, sessions_api.settings.jwt_secret)
    return SimpleNamespace(
        id=uuid.uuid4(),
        name="AT NAP login",
        auth_type=auth_type,
        param_name=None,
        ciphertext=ciphertext,
        nonce=nonce,
        last_used_at=None,
    )


def _at_task(cred_id: str) -> Any:
    return sessions_api._RefreshTask(
        "provider[OBB].timetable(gtfs)", "GTFS", "display", "obb.zip", cred_id, AT_RESOLVER
    )


async def test_login_portal_logs_in_once_and_signs_lookup_and_download(
    inbox: Path, dispatched: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import credentials as crypto
    from app.master import nap_importer

    monkeypatch.setattr(nap_importer, "_validate_safe_http_url", lambda url: url)
    crypto._token_cache.clear()
    cred = _login_cred(json.dumps(AT_LOGIN))
    seen: list[tuple[str, str | None]] = []

    def portal(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers.get("Authorization")))
        if request.url.host == "user.example.at":
            return httpx.Response(200, json={"access_token": "T", "expires_in": 300})
        if request.url.path.endswith("/data-sets"):
            return httpx.Response(200, json=[{"id": 67, "name": "ÖBB Personenverkehr"}])
        return httpx.Response(200, content=_gtfs_zip())

    staging = inbox / "s1" / "_staging"
    staging.mkdir(parents=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(portal)) as c:
        outcome = await _refresh_one_task(c, _CredDb(cred), "s1", staging, _at_task(str(cred.id)))  # type: ignore[arg-type]
    assert outcome["status"] == "fetched", outcome
    assert outcome["url"] == "https://data.example.at/api/public/v1/data-sets/67/file"
    assert seen == [
        ("/token", None),
        ("/api/public/v1/data-sets", "Bearer T"),
        ("/api/public/v1/data-sets/67/file", "Bearer T"),
    ]
    assert cred.last_used_at is not None
    assert dispatched == ["GTFS"]


async def test_refused_login_skips_the_task_and_keeps_the_slot(
    inbox: Path, dispatched: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import credentials as crypto
    from app.master import nap_importer

    monkeypatch.setattr(nap_importer, "_validate_safe_http_url", lambda url: url)
    crypto._token_cache.clear()
    cred = _login_cred(json.dumps(AT_LOGIN))

    def portal(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_grant"})

    staging = inbox / "s1" / "_staging"
    staging.mkdir(parents=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(portal)) as c:
        outcome = await _refresh_one_task(c, _CredDb(cred), "s1", staging, _at_task(str(cred.id)))  # type: ignore[arg-type]
    assert outcome["status"] == "skipped"
    assert "NAP resolver failed" in outcome["reason"]
    assert "'AT NAP login': login refused" in outcome["reason"]
    assert "invalid_grant" in outcome["reason"]
    assert dispatched == []


async def test_missing_credential_is_reported_for_a_login_portal(inbox: Path) -> None:
    staging = inbox / "s1" / "_staging"
    staging.mkdir(parents=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))) as c:
        outcome = await _refresh_one_task(
            c, _CredDb(None), "s1", staging, _at_task(str(uuid.uuid4()))
        )  # type: ignore[arg-type]
    assert outcome["status"] == "skipped"
    assert "not found" in outcome["reason"]


async def test_undecryptable_credential_is_reported(
    inbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cred = _login_cred(json.dumps(AT_LOGIN))
    monkeypatch.setattr(sessions_api.settings, "jwt_secret", "another-secret-entirely-32-bytes!")
    staging = inbox / "s1" / "_staging"
    staging.mkdir(parents=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))) as c:
        outcome = await _refresh_one_task(c, _CredDb(cred), "s1", staging, _at_task(str(cred.id)))  # type: ignore[arg-type]
    assert outcome["status"] == "skipped"
    assert "cannot be decrypted" in outcome["reason"]
