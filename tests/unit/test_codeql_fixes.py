"""Fixes for three CodeQL alerts on main.

#41 py/stack-trace-exposure  — /api/admin/config/smtp/test returned str(exc)
#42 py/full-ssrf             — the legacy `nap_url` field reached httpx
#89 py/log-injection         — runner logged a config-supplied timezone name
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, patch

import aiosmtplib
import pytest
from fastapi import HTTPException

from app.api.admin import config as config_api
from app.api.admin import sessions as sessions_api
from app.auth import email
from app.logging_config import one_line
from app.master import nap_importer
from app.network_coverage import runner

CFG = {
    "SMTP_HOST": "smtp.example",
    "SMTP_PORT": "587",
    "SMTP_USER": "u",
    "SMTP_PASS": "p",
    "SMTP_SECURE": "starttls",
    "SMTP_FROM": "viator@example.org",
}


# ─────────────────────────── #41 SMTP test errors ───────────────────────────


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (aiosmtplib.SMTPAuthenticationError(535, "bad creds"), email.SmtpAuthError),
        (aiosmtplib.SMTPConnectError("refused"), email.SmtpConnectionError),
        (aiosmtplib.SMTPServerDisconnected("gone"), email.SmtpConnectionError),
        (TimeoutError("slow"), email.SmtpConnectionError),
        (aiosmtplib.SMTPRecipientsRefused([]), email.EmailSendError),
    ],
)
async def test_smtp_failures_are_classified(raised: Exception, expected: type) -> None:
    with (
        patch("app.auth.email.aiosmtplib.send", new=AsyncMock(side_effect=raised)),
        pytest.raises(expected),
    ):
        await email._send_via_smtp(CFG, to="a@b.example", subject="s", html="h", text="t")


@pytest.mark.parametrize(
    ("exc", "needle"),
    [
        (email.SmtpAuthError("535 5.7.8 secret detail"), "SMTP_USER / SMTP_PASS"),
        (email.SmtpConnectionError("[Errno 111] at /usr/lib/x.py"), "SMTP_HOST"),
        (email.EmailSendError("550 relay denied for internal-host"), "audit log"),
    ],
)
def test_smtp_failure_message_is_fixed_text(exc: Exception, needle: str) -> None:
    message = config_api._smtp_failure_message(exc)
    assert needle in message
    assert str(exc) not in message


# ─────────────────────────── #42 legacy nap_url ───────────────────────────


class _Result:
    def __init__(self, value: Any) -> None:
        self.value = value

    def scalar_one_or_none(self) -> Any:
        return self.value


class _Db:
    def __init__(self, stored: str | None) -> None:
        self.stored = stored
        self.queries = 0

    def execute(self, stmt: Any) -> _Result:
        self.queries += 1
        return _Result(self.stored)


def test_legacy_nap_url_accepts_the_default_fr_nap_without_a_lookup() -> None:
    db = _Db(None)
    url = nap_importer.DEFAULT_FR_NAP_URL
    assert sessions_api._legacy_nap_url(db, url) == url  # type: ignore[arg-type]
    assert db.queries == 0


def test_legacy_nap_url_returns_the_stored_catalogue_url() -> None:
    db = _Db("https://nap.example/api/datasets")
    url = sessions_api._legacy_nap_url(db, "https://nap.example/api/datasets")  # type: ignore[arg-type]
    assert url == "https://nap.example/api/datasets"


def test_legacy_nap_url_refuses_anything_else() -> None:
    db = _Db(None)
    with pytest.raises(HTTPException) as exc:
        sessions_api._legacy_nap_url(db, "http://169.254.169.254/latest")  # type: ignore[arg-type]
    assert exc.value.status_code == 400


# ─────────────────────────── #89 timezone log line ───────────────────────────


def test_unknown_timezone_log_line_cannot_be_split(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # In a full `pytest` run the integration migration tests go first, and
    # alembic/env.py's fileConfig() disables every logger that already
    # exists (disable_existing_loggers defaults to True) — this one included.
    monkeypatch.setattr(runner.log, "disabled", False)
    cfg = runner.CoverageConfig()
    with caplog.at_level(logging.WARNING, logger=runner.log.name):
        zone = runner._resolve_timezone("Bad/Zone\nFAKE ERROR forged", cfg)
    assert str(zone) == "UTC"
    (record,) = [r for r in caplog.records if "unknown timezone" in r.getMessage()]
    assert "\n" not in record.getMessage()
    assert "Bad/ZoneFAKE ERROR forged" in record.getMessage()


# ─────────── #43 #44 #45 #70 #71 log values kept on one line ───────────


def test_one_line_escapes_line_breaks_and_caps() -> None:
    assert one_line("a@b.example\r\nFAKE ERROR") == "a@b.example\\r\\nFAKE ERROR"
    assert one_line(12) == "12"
    assert len(one_line("x" * 500)) == 200


async def test_disabled_email_logs_the_recipient_on_one_line(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # See the timezone test above: alembic's fileConfig may have disabled it.
    monkeypatch.setattr(email.log, "disabled", False)
    monkeypatch.setattr(email, "_read_smtp_config", lambda: {**CFG, "SMTP_HOST": ""})
    with caplog.at_level(logging.INFO, logger=email.log.name):
        await email._deliver(
            to="x@y.example\nFAKE ERROR forged",
            subject="s",
            html="h",
            text="body",
            purpose="test",
        )
    lines = [r.getMessage() for r in caplog.records if r.name == email.log.name]
    assert lines
    assert all("x@y.example\\nFAKE ERROR forged" in m for m in lines)
    assert not any("\nFAKE ERROR" in m for m in lines)
