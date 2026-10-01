"""Login credentials (`oauth2_password`) — the Austrian NAP's scripted-download
flow: a stored login is exchanged for a Bearer token at use time.

Pure unit tests: the token endpoint is an httpx MockTransport, the DB a
stand-in, the same no-Postgres approach as the rest of tests/unit.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import HTTPException

from app import credentials as crypto
from app import router_config
from app.api import credentials as credentials_api
from app.master import nap_importer

SECRET = "test-jwt-secret-32-bytes-or-more!"
TOKEN_URL = "https://user.example.at/auth/realms/x/protocol/openid-connect/token"
LOGIN = {
    "token_url": TOKEN_URL,
    "client_id": "dbp-script-download",
    "username": "ph@example.com",
    "password": " s3cret pass ",
}


@pytest.fixture(autouse=True)
def _no_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(nap_importer, "_validate_safe_http_url", lambda url: url)
    crypto._token_cache.clear()


def _token_portal(calls: list[httpx.Request], *, status: int = 200, body: Any = None) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        payload = {"access_token": "tok-1", "expires_in": 300} if body is None else body
        return httpx.Response(status, json=payload)

    return handler


class _Cred:
    def __init__(self, auth_type: str, plaintext: str, param_name: str | None = None) -> None:
        self.id = uuid.uuid4()
        self.user_id = uuid.uuid4()
        self.name = "AT NAP login"
        self.auth_type = auth_type
        self.param_name = param_name
        self.ciphertext, self.nonce = crypto.encrypt(plaintext, SECRET)
        self.last_used_at = None
        self.note = None
        self.created_at = None


# ─────────────────────────── secret validation ───────────────────────────


def test_login_secret_is_canonicalised_and_keeps_password_spaces() -> None:
    stored = crypto.validate_secret("oauth2_password", json.dumps({**LOGIN, "client_id": " c "}))
    assert json.loads(stored) == {**LOGIN, "client_id": "c"}


@pytest.mark.parametrize(
    ("secret", "message"),
    [
        ("not json", "JSON object"),
        ("[1]", "JSON object"),
        (json.dumps({**LOGIN, "extra": 1}), "unknown fields"),
        (json.dumps({**LOGIN, "password": ""}), "'password'"),
        (json.dumps({k: v for k, v in LOGIN.items() if k != "username"}), "'username'"),
        (json.dumps({**LOGIN, "token_url": "http://x.example/t"}), "https"),
        (json.dumps({**LOGIN, "scope": 3}), "'scope'"),
    ],
)
def test_bad_login_secrets_are_refused_without_echoing_values(secret: str, message: str) -> None:
    with pytest.raises(ValueError, match=message) as exc:
        crypto.validate_secret("oauth2_password", secret)
    assert LOGIN["password"] not in str(exc.value)


def test_static_secrets_are_stored_as_typed() -> None:
    assert crypto.validate_secret("header", " key ") == " key "


def test_login_scheme_needs_no_param_name() -> None:
    assert crypto.validate_param_name("oauth2_password", None) is None
    with pytest.raises(ValueError):
        crypto.validate_param_name("oauth2_password", "ApiKey")


def test_a_login_cannot_become_a_static_header() -> None:
    with pytest.raises(ValueError, match="authorize"):
        crypto.apply_to_request(
            "https://x.example/f",
            auth_type="oauth2_password",
            plaintext=json.dumps(LOGIN),
            param_name=None,
        )


# ─────────────────────────── token exchange ───────────────────────────


async def test_password_grant_posts_the_login_and_returns_the_token() -> None:
    calls: list[httpx.Request] = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(_token_portal(calls))) as c:
        token, ttl = await crypto.fetch_oauth2_token(c, {**LOGIN, "scope": "openid"})
    assert (token, ttl) == ("tok-1", 300.0)
    (request,) = calls
    form = dict(httpx.QueryParams(request.content.decode()))
    assert form == {
        "grant_type": "password",
        "client_id": "dbp-script-download",
        "username": "ph@example.com",
        "password": " s3cret pass ",
        "scope": "openid",
    }


async def test_refused_login_reports_the_oauth_error_but_never_the_password() -> None:
    body = {"error": "invalid_grant", "error_description": "Invalid user credentials"}
    handler = _token_portal([], status=401, body=body)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(crypto.CredentialLoginError) as exc:
            await crypto.fetch_oauth2_token(c, LOGIN)
    assert "HTTP 401 invalid_grant: Invalid user credentials" in str(exc.value)
    assert "s3cret" not in str(exc.value)


@pytest.mark.parametrize(
    ("status", "body", "message"),
    [
        (302, {}, "HTTP 302"),
        (200, {"token_type": "bearer"}, "no access_token"),
        (500, "oops", "HTTP 500"),
    ],
)
async def test_unusable_token_answers_are_login_errors(
    status: int, body: Any, message: str
) -> None:
    handler = _token_portal([], status=status, body=body)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(crypto.CredentialLoginError, match=message):
            await crypto.fetch_oauth2_token(c, LOGIN)


async def test_non_json_token_answer_is_a_login_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(crypto.CredentialLoginError, match="not JSON"):
            await crypto.fetch_oauth2_token(c, LOGIN)


async def test_unreachable_token_endpoint_is_a_login_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(crypto.CredentialLoginError, match="failed"):
            await crypto.fetch_oauth2_token(c, LOGIN)


async def test_token_url_goes_through_the_ssrf_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(url: str) -> str:
        raise ValueError("private address")

    monkeypatch.setattr(nap_importer, "_validate_safe_http_url", refuse)
    calls: list[httpx.Request] = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(_token_portal(calls))) as c:
        with pytest.raises(crypto.CredentialLoginError, match="refused"):
            await crypto.fetch_oauth2_token(c, LOGIN)
    assert calls == []


async def test_authorize_reuses_a_live_token_and_relogs_after_rotation() -> None:
    calls: list[httpx.Request] = []
    cred = _Cred("oauth2_password", json.dumps(LOGIN))
    async with httpx.AsyncClient(transport=httpx.MockTransport(_token_portal(calls))) as c:
        first = await crypto.authorize(c, cred, "https://x.example/a", SECRET)  # type: ignore[arg-type]
        second = await crypto.authorize(c, cred, "https://x.example/b", SECRET)  # type: ignore[arg-type]
        cred.ciphertext, cred.nonce = crypto.encrypt(json.dumps(LOGIN), SECRET)  # rotated
        await crypto.authorize(c, cred, "https://x.example/c", SECRET)  # type: ignore[arg-type]
    assert first == ("https://x.example/a", {"Authorization": "Bearer tok-1"})
    assert second[1] == first[1]
    assert len(calls) == 2


async def test_a_token_close_to_expiry_is_not_reused() -> None:
    calls: list[httpx.Request] = []
    handler = _token_portal(calls, body={"access_token": "t", "expires_in": 20})
    cred = _Cred("oauth2_password", json.dumps(LOGIN))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        await crypto.authorize(c, cred, "https://x.example/a", SECRET)  # type: ignore[arg-type]
        await crypto.authorize(c, cred, "https://x.example/a", SECRET)  # type: ignore[arg-type]
    assert len(calls) == 2


async def test_authorize_applies_static_schemes_without_a_login() -> None:
    cred = _Cred("header", "k-123", "ApiKey")
    async with httpx.AsyncClient(transport=httpx.MockTransport(_token_portal([]))) as c:
        out = await crypto.authorize(c, cred, "https://x.example/f", SECRET)  # type: ignore[arg-type]
    assert out == ("https://x.example/f", {"ApiKey": "k-123"})


async def test_authorize_reports_a_corrupt_stored_login() -> None:
    cred = _Cred("oauth2_password", "{}")
    async with httpx.AsyncClient(transport=httpx.MockTransport(_token_portal([]))) as c:
        with pytest.raises(crypto.CredentialLoginError, match="unusable"):
            await crypto.authorize(c, cred, "https://x.example/f", SECRET)  # type: ignore[arg-type]


# ─────────────────────────── other consumers ───────────────────────────


def test_otp_router_config_leaves_a_login_credential_out() -> None:
    providers = [
        {
            "id": "OBB",
            "gtfs_rt": {"alerts_url": "https://rt.example/a"},
            "gtfs_rt_credential_id": "c1",
        }
    ]
    creds = {"c1": ("oauth2_password", json.dumps(LOGIN), None)}
    config = json.loads(router_config.render_router_config(providers, credentials=creds))  # type: ignore[arg-type]
    (updater,) = config["updaters"]
    assert updater["url"] == "https://rt.example/a"
    assert "headers" not in updater


# ─────────────────────────── API ───────────────────────────


class _Result:
    def scalar_one_or_none(self) -> None:
        return None


class _Db:
    def __init__(self, cred: _Cred | None = None) -> None:
        self.cred = cred
        self.added: list[Any] = []

    def execute(self, *a: Any) -> _Result:
        return _Result()

    def add(self, obj: Any) -> None:
        self.added.append(obj)
        obj.id = uuid.uuid4()
        obj.created_at = None
        obj.last_used_at = None

    def flush(self) -> None: ...

    def commit(self) -> None: ...

    def close(self) -> None: ...

    def get(self, model: Any, key: Any) -> _Cred | None:
        return self.cred


class _Actor:
    def __init__(self, uid: uuid.UUID | None = None) -> None:
        self.id = uid or uuid.uuid4()


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(credentials_api.audit, "record", lambda db, **kw: None)
    monkeypatch.setattr(credentials_api, "client_ip", lambda request: "127.0.0.1")
    monkeypatch.setattr(credentials_api.settings, "jwt_secret", SECRET)


def test_create_stores_a_validated_login(api: None) -> None:
    db = _Db()
    body = credentials_api.CredentialCreate(
        name="AT NAP login", auth_type="oauth2_password", secret=json.dumps(LOGIN)
    )
    out = credentials_api.create_credential(body, None, db, _Actor())  # type: ignore[arg-type]
    assert out.auth_type == "oauth2_password"
    (row,) = db.added
    assert json.loads(crypto.decrypt(row.ciphertext, row.nonce, SECRET)) == LOGIN


def test_create_refuses_an_incomplete_login(api: None) -> None:
    body = credentials_api.CredentialCreate(
        name="x", auth_type="oauth2_password", secret=json.dumps({"token_url": TOKEN_URL})
    )
    with pytest.raises(HTTPException) as exc:
        credentials_api.create_credential(body, None, _Db(), _Actor())  # type: ignore[arg-type]
    assert exc.value.status_code == 400


def test_patch_to_a_login_needs_the_secret_re_entered(api: None) -> None:
    cred = _Cred("bearer", "tok")
    patch = credentials_api.CredentialPatch(auth_type="oauth2_password")
    with pytest.raises(HTTPException, match="re-entered"):
        credentials_api.patch_credential(cred.id, patch, None, _Db(cred), _Actor(cred.user_id))  # type: ignore[arg-type]


def test_patch_validates_a_rotated_login(api: None) -> None:
    cred = _Cred("oauth2_password", json.dumps(LOGIN))
    db = _Db(cred)
    bad = credentials_api.CredentialPatch(secret="{}")
    with pytest.raises(HTTPException) as exc:
        credentials_api.patch_credential(cred.id, bad, None, db, _Actor(cred.user_id))  # type: ignore[arg-type]
    assert exc.value.status_code == 400
    good = credentials_api.CredentialPatch(secret=json.dumps({**LOGIN, "password": "new"}))
    credentials_api.patch_credential(cred.id, good, None, db, _Actor(cred.user_id))  # type: ignore[arg-type]
    assert json.loads(crypto.decrypt(cred.ciphertext, cred.nonce, SECRET))["password"] == "new"


def test_patch_from_a_login_to_a_token_with_a_new_secret(api: None) -> None:
    cred = _Cred("oauth2_password", json.dumps(LOGIN))
    patch = credentials_api.CredentialPatch(auth_type="bearer", secret="tok")
    out = credentials_api.patch_credential(cred.id, patch, None, _Db(cred), _Actor(cred.user_id))  # type: ignore[arg-type]
    assert out.auth_type == "bearer"


async def test_check_login_reports_success_without_the_token(
    api: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_token(client: Any, login: dict[str, str]) -> tuple[str, float]:
        assert login == LOGIN
        return "tok-secret", 300.0

    monkeypatch.setattr(crypto, "fetch_oauth2_token", fake_token)
    cred = _Cred("oauth2_password", json.dumps(LOGIN))
    out = await credentials_api.check_login(cred.id, _Db(cred), _Actor(cred.user_id))  # type: ignore[arg-type]
    assert out.ok
    assert "300 s" in out.detail
    assert "tok-secret" not in out.detail


async def test_check_login_reports_a_refusal(api: None, monkeypatch: pytest.MonkeyPatch) -> None:
    async def refused(client: Any, login: dict[str, str]) -> tuple[str, float]:
        raise crypto.CredentialLoginError("login refused: HTTP 401 invalid_grant")

    monkeypatch.setattr(crypto, "fetch_oauth2_token", refused)
    cred = _Cred("oauth2_password", json.dumps(LOGIN))
    out = await credentials_api.check_login(cred.id, _Db(cred), _Actor(cred.user_id))  # type: ignore[arg-type]
    assert not out.ok
    assert "invalid_grant" in out.detail


async def test_check_login_reports_a_corrupt_login(api: None) -> None:
    cred = _Cred("oauth2_password", "{}")
    out = await credentials_api.check_login(cred.id, _Db(cred), _Actor(cred.user_id))  # type: ignore[arg-type]
    assert not out.ok
    assert "unusable" in out.detail


@pytest.mark.parametrize("owner_matches", [True, False])
async def test_check_login_refuses_static_or_foreign_credentials(
    api: None, owner_matches: bool
) -> None:
    cred = _Cred("bearer", "tok") if owner_matches else _Cred("oauth2_password", "{}")
    actor = _Actor(cred.user_id if owner_matches else None)
    with pytest.raises(HTTPException) as exc:
        await credentials_api.check_login(cred.id, _Db(cred), actor)  # type: ignore[arg-type]
    assert exc.value.status_code == (400 if owner_matches else 404)


# ─────────────────────────── templates ───────────────────────────

ROOT = Path(__file__).resolve().parents[2]


def test_credentials_page_offers_the_login_scheme_and_nap_presets() -> None:
    html = (ROOT / "app" / "templates" / "credentials.html").read_text(encoding="utf-8")
    assert '<option value="oauth2_password">' in html
    assert "dbp-script-download" in html
    assert "param_name: 'ApiKey'" in html
    assert "/test-login" in html
    for field in ("token_url", "client_id", "username", "password"):
        assert f'data-login-field="{field}"' in html


def test_session_provider_card_keeps_and_edits_credential_ids() -> None:
    html = (ROOT / "app" / "templates" / "admin" / "sessions.html").read_text(encoding="utf-8")
    block = html[
        html.index("// ── Timetable credential picker") : html.index("// ── end timetable")
    ]
    for key in ("gtfs_rt_credential_id", "mct_credential_id", "stations_csv_credential_id"):
        assert key in block
    assert "provider.timetable_credential_id = credId" in block
    assert re.search(r"collectCredentialIds\(card, ttSource, provider\);", html)
