"""Legacy HTTP-basic guard — `app.security.authed` / `authed_or_none`.

The guard fronts the Phase-1 upload UI (`/`, `/upload`). The contract pinned
here: the legacy credential exists only when BOTH `ADMIN_USER` and
`ADMIN_PASSWORD` are set. An empty setting means "this surface is off".

`tests/conftest.py` gives both settings a non-empty value for the whole suite,
so every test below sets them explicitly.
"""

from __future__ import annotations

import base64

import pytest
from fastapi import HTTPException
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPBasicCredentials
from fastapi.testclient import TestClient

from app import detect, main, security
from app.settings import Settings, settings

KIND = sorted(detect.KNOWN_KINDS)[0]


def _basic(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
def configure(monkeypatch: pytest.MonkeyPatch):
    def _set(admin_user: str, admin_password: str) -> None:
        monkeypatch.setattr(settings, "admin_user", admin_user)
        monkeypatch.setattr(settings, "admin_password", admin_password)

    return _set


@pytest.fixture
def upload_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Swap the upload body for a recorder — nothing is written, no DB is opened."""
    calls: list[str] = []

    async def _recorder(declared_standard: str, version_label: str, file: object) -> object:
        calls.append(declared_standard)
        return RedirectResponse("/", status_code=303)

    monkeypatch.setattr(main, "_do_upload", _recorder)
    return calls


@pytest.fixture
def client() -> TestClient:
    # No `with`: the startup hook (DB, orchestrator) must not run in a unit test.
    return TestClient(main.app, follow_redirects=False)


def _post_upload(client: TestClient, headers: dict[str, str] | None):
    return client.post(
        "/upload",
        headers=headers,
        data={"declared_standard": KIND, "version_label": "unit"},
        files={"file": ("feed.zip", b"PK", "application/zip")},
    )


# ───────────────────────── POST /upload ─────────────────────────


@pytest.mark.parametrize(
    ("admin_user", "admin_password", "sent"),
    [
        pytest.param("", "", ("", ""), id="neither-set_empty-pair"),
        pytest.param("", "", ("admin", "admin"), id="neither-set_some-pair"),
        pytest.param("ops", "", ("ops", ""), id="password-unset"),
        pytest.param("", "s3cret", ("", "s3cret"), id="user-unset"),
    ],
)
def test_upload_is_refused_unless_both_settings_are_set(
    client: TestClient,
    configure,
    upload_calls: list[str],
    admin_user: str,
    admin_password: str,
    sent: tuple[str, str],
) -> None:
    configure(admin_user, admin_password)

    r = _post_upload(client, _basic(*sent))

    assert r.status_code == 401
    assert upload_calls == []


def test_upload_is_refused_without_a_header_when_nothing_is_set(
    client: TestClient, configure, upload_calls: list[str]
) -> None:
    configure("", "")

    r = _post_upload(client, None)

    assert r.status_code == 401
    assert upload_calls == []


def test_upload_accepts_the_configured_credential(
    client: TestClient, configure, upload_calls: list[str]
) -> None:
    configure("ops", "s3cret")

    r = _post_upload(client, _basic("ops", "s3cret"))

    assert r.status_code == 303
    assert upload_calls == [KIND]


@pytest.mark.parametrize(
    "sent", [("ops", "nope"), ("nope", "s3cret"), ("ops", ""), ("", "s3cret"), ("", "")]
)
def test_upload_rejects_anything_but_the_configured_credential(
    client: TestClient, configure, upload_calls: list[str], sent: tuple[str, str]
) -> None:
    configure("ops", "s3cret")

    r = _post_upload(client, _basic(*sent))

    assert r.status_code == 401
    assert upload_calls == []


# ───────────────────────────── GET / ─────────────────────────────


@pytest.mark.parametrize("headers", [None, _basic("", ""), _basic("ops", "s3cret")])
def test_index_redirects_to_login_when_admin_user_is_empty(
    client: TestClient, configure, headers: dict[str, str] | None
) -> None:
    """Phase-2 mode: the bare hostname goes to `/login`, no basic-auth prompt."""
    configure("", "")

    r = client.get("/", headers=headers)

    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_index_guard_refuses_when_only_the_user_is_set(configure) -> None:
    configure("ops", "")

    with pytest.raises(HTTPException) as exc:
        security.authed_or_none(HTTPBasicCredentials(username="ops", password=""))

    assert exc.value.status_code == 401


def test_index_guard_returns_the_user_for_the_configured_credential(configure) -> None:
    configure("ops", "s3cret")

    creds = HTTPBasicCredentials(username="ops", password="s3cret")

    assert security.authed_or_none(creds) == "ops"
    assert security.authed(creds) == "ops"


# ───────────────────────── boot warning ─────────────────────────


class _LogRecorder:
    def __init__(self) -> None:
        self.warnings: list[tuple[str, dict[str, object]]] = []

    def warning(self, event: str, **fields: object) -> None:
        self.warnings.append((event, fields))


def test_boot_warns_when_only_the_user_is_set(configure, monkeypatch: pytest.MonkeyPatch) -> None:
    log = _LogRecorder()
    monkeypatch.setattr(main, "log", log)
    configure("ops", "")

    main._warn_if_legacy_basic_auth_is_locked()

    ((event, fields),) = log.warnings
    assert event == "legacy_basic_auth.locked"
    assert "ADMIN_PASSWORD" in str(fields["reason"])


@pytest.mark.parametrize(
    ("admin_user", "admin_password"), [("", ""), ("", "s3cret"), ("ops", "s3cret")]
)
def test_boot_is_quiet_when_the_surface_is_off_or_fully_configured(
    configure, monkeypatch: pytest.MonkeyPatch, admin_user: str, admin_password: str
) -> None:
    log = _LogRecorder()
    monkeypatch.setattr(main, "log", log)
    configure(admin_user, admin_password)

    main._warn_if_legacy_basic_auth_is_locked()

    assert log.warnings == []


# ─────────────────────────── defaults ───────────────────────────


def test_the_legacy_credential_is_unset_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """With neither env var present the surface is off."""
    monkeypatch.delenv("ADMIN_USER", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)

    fresh = Settings(_env_file=None)

    assert fresh.admin_user == ""
    assert fresh.admin_password == ""
