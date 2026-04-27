"""Email sender — real `aiosmtplib` delivery, OSCAR-pattern templates.

Reads SMTP config live from `platform_config` (see spec §11.1) so an admin can
PATCH SMTP settings and the next email picks them up. If `SMTP_HOST` is empty
the email is **logged but not sent** — useful for local dev so the magic link
is still visible to the operator.

Templates are Jinja2, loaded from `app/auth/email_templates/`. They were
lifted from OSCAR's `mailer.js` (HTML structure + UIC copyright footer) and
rebranded with the VIATOR palette.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Any

import aiosmtplib
from jinja2 import Environment, PackageLoader, select_autoescape

from .. import config_service
from ..db import SessionLocal


log = logging.getLogger(__name__)


_env = Environment(
    loader=PackageLoader("app.auth", "email_templates"),
    autoescape=select_autoescape(enabled_extensions=("html",)),
    trim_blocks=True,
    lstrip_blocks=True,
)


# ──────────────────────────── exceptions ────────────────────────────


class SmtpNotConfiguredError(RuntimeError):
    """SMTP_HOST is empty in platform_config — nothing to send through."""


class EmailSendError(RuntimeError):
    """The SMTP server rejected the message or the connection failed."""


# ──────────────────────────── public API ────────────────────────────


async def send_verification_email(*, to_email: str, name: str, magic_link: str) -> None:
    """Account-confirmation magic-link email."""
    html = _env.get_template("verification.html").render(name=name, magic_link=magic_link)
    text = _env.get_template("verification.txt").render(name=name, magic_link=magic_link)
    await _deliver(
        to=to_email,
        subject="VIATOR — confirm your account",
        html=html,
        text=text,
        purpose="verification",
    )


async def send_password_reset_email(*, to_email: str, name: str, magic_link: str) -> None:
    """Password-reset magic-link email."""
    html = _env.get_template("password_reset.html").render(name=name, magic_link=magic_link)
    text = _env.get_template("password_reset.txt").render(name=name, magic_link=magic_link)
    await _deliver(
        to=to_email,
        subject="VIATOR — reset your password",
        html=html,
        text=text,
        purpose="password_reset",
    )


async def send_test_email(*, to_email: str) -> None:
    """SMTP-connectivity test email triggered from the admin UI.

    Unlike the magic-link helpers, this does NOT silently no-op when SMTP is
    unconfigured — it raises so the admin endpoint can surface the failure.
    """
    sent_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    html = _env.get_template("smtp_test.html").render(to=to_email, sent_at=sent_at)
    text = _env.get_template("smtp_test.txt").render(to=to_email, sent_at=sent_at)

    cfg = _read_smtp_config()
    if not cfg["SMTP_HOST"]:
        raise SmtpNotConfiguredError("SMTP_HOST is empty in platform_config")

    await _send_via_smtp(
        cfg,
        to=to_email,
        subject="VIATOR — SMTP test",
        html=html,
        text=text,
    )


# ──────────────────────────── delivery ────────────────────────────


def _read_smtp_config() -> dict[str, Any]:
    with SessionLocal() as db:
        return config_service.get_all(db)


async def _deliver(
    *,
    to: str,
    subject: str,
    html: str,
    text: str,
    purpose: str,
) -> None:
    """Send through SMTP if configured; otherwise log a development fallback.

    Used by the magic-link helpers, which must not break the registration flow
    when SMTP isn't set up locally — devs see the link in the log instead.
    """
    cfg = _read_smtp_config()
    if not cfg["SMTP_HOST"]:
        # Find the magic link by scanning the text for the URL — best-effort;
        # if not present (e.g. SMTP test), just log the subject.
        log.warning(
            "[EMAIL DISABLED — SMTP_HOST not set] purpose=%s to=%s subject=%r",
            purpose, to, subject,
        )
        log.info("[EMAIL BODY for %s — %s]\n%s", purpose, to, text)
        return

    await _send_via_smtp(cfg, to=to, subject=subject, html=html, text=text)


async def _send_via_smtp(
    cfg: dict[str, Any],
    *,
    to: str,
    subject: str,
    html: str,
    text: str,
) -> None:
    """Perform the actual SMTP delivery. Raises EmailSendError on failure."""
    msg = EmailMessage()
    msg["From"] = cfg["SMTP_FROM"]
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    use_tls, start_tls = _tls_modes(str(cfg["SMTP_SECURE"]).lower())

    try:
        await aiosmtplib.send(
            msg,
            hostname=cfg["SMTP_HOST"],
            port=int(cfg["SMTP_PORT"]),
            username=cfg["SMTP_USER"] or None,
            password=cfg["SMTP_PASS"] or None,
            use_tls=use_tls,
            start_tls=start_tls,
            timeout=15,
        )
    except aiosmtplib.SMTPException as exc:
        log.error(
            "SMTP send failed: host=%s port=%s code=%s msg=%s",
            cfg["SMTP_HOST"], cfg["SMTP_PORT"],
            getattr(exc, "code", None), exc,
        )
        raise EmailSendError(str(exc)) from exc
    except (ConnectionError, OSError, TimeoutError) as exc:
        log.error(
            "SMTP transport failed: host=%s port=%s err=%s",
            cfg["SMTP_HOST"], cfg["SMTP_PORT"], exc,
        )
        raise EmailSendError(str(exc)) from exc

    log.info("email sent: to=%s subject=%r host=%s", to, subject, cfg["SMTP_HOST"])


def _tls_modes(secure: str) -> tuple[bool, bool]:
    """Map the SMTP_SECURE config string to aiosmtplib (use_tls, start_tls).

      'none'     → no encryption
      'starttls' → upgrade after EHLO (default for port 587)
      'tls'      → SMTPS, implicit TLS from connection start (port 465)
    """
    if secure == "tls":
        return True, False
    if secure == "starttls":
        return False, True
    if secure == "none":
        return False, False
    # Unknown — be conservative, prefer STARTTLS.
    log.warning("unknown SMTP_SECURE=%r; defaulting to STARTTLS", secure)
    return False, True
