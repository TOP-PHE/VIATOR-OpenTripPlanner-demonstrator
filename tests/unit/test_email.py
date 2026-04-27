"""Email module — template rendering + SMTP_SECURE → aiosmtplib mode mapping.

Real SMTP delivery is exercised in `tests/integration/test_smtp_test.py`
with `aiosmtplib.send` mocked.
"""

from __future__ import annotations

from app.auth.email import _env, _tls_modes


# ────────────────── Template rendering ──────────────────


def test_verification_template_contains_link_and_name() -> None:
    html = _env.get_template("verification.html").render(
        name="Patrick", magic_link="https://test/confirm/abc123"
    )
    assert "Patrick" in html
    assert "https://test/confirm/abc123" in html
    assert "VIATOR" in html
    assert "TrackOnPath SAS" in html
    assert "UIC" in html


def test_verification_text_template_renders_cleanly() -> None:
    text = _env.get_template("verification.txt").render(
        name="Patrick", magic_link="https://test/confirm/xyz"
    )
    assert "Patrick" in text
    assert "https://test/confirm/xyz" in text
    assert "24 hours" in text


def test_password_reset_templates_render() -> None:
    html = _env.get_template("password_reset.html").render(
        name="Alice", magic_link="https://test/reset/qq"
    )
    text = _env.get_template("password_reset.txt").render(
        name="Alice", magic_link="https://test/reset/qq"
    )
    for body in (html, text):
        assert "Alice" in body
        assert "https://test/reset/qq" in body
    assert "2 hours" in text


def test_smtp_test_template_renders() -> None:
    html = _env.get_template("smtp_test.html").render(to="ops@example.org", sent_at="now")
    assert "ops@example.org" in html
    assert "SMTP" in html


def test_html_autoescape_prevents_injection() -> None:
    """Hostile name with HTML must be escaped, not rendered as markup."""
    html = _env.get_template("verification.html").render(
        name="<script>alert(1)</script>",
        magic_link="https://test/confirm/aaa",
    )
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


# ────────────────── _tls_modes mapping ──────────────────


def test_tls_modes_starttls() -> None:
    use_tls, start_tls = _tls_modes("starttls")
    assert use_tls is False
    assert start_tls is True


def test_tls_modes_tls() -> None:
    use_tls, start_tls = _tls_modes("tls")
    assert use_tls is True
    assert start_tls is False


def test_tls_modes_none() -> None:
    use_tls, start_tls = _tls_modes("none")
    assert use_tls is False
    assert start_tls is False


def test_tls_modes_unknown_falls_back_to_starttls() -> None:
    use_tls, start_tls = _tls_modes("garbage")
    assert use_tls is False
    assert start_tls is True
