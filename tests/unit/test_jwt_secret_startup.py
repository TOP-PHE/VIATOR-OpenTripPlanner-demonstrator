"""Web does not start with a JWT secret that cannot protect a session.

Whoever knows `JWT_SECRET` signs a session for any role. The repository is
public, so the default of the settings and the line of `docker/.env.example`
are known to everyone; and a secret shorter than the HS256 digest is weaker
than the signature. `_startup` refuses both before it serves anything.
"""

from __future__ import annotations

import secrets
from pathlib import Path

import pytest

from app import main
from app.settings import JWT_SECRET_MIN_BYTES, Settings, jwt_secret_problem, settings

_REPOSITORY = Path(__file__).resolve().parents[2]


def _example_secret() -> str:
    lines = (_REPOSITORY / "docker" / ".env.example").read_text(encoding="utf-8").splitlines()
    (line,) = [line for line in lines if line.startswith("JWT_SECRET=")]
    return line.removeprefix("JWT_SECRET=")


def test_the_default_of_the_settings_is_refused() -> None:
    assert jwt_secret_problem(Settings.model_fields["jwt_secret"].default) is not None


def test_the_value_of_the_example_env_file_is_refused() -> None:
    example = _example_secret()

    assert len(example) >= JWT_SECRET_MIN_BYTES  # refused for being published, not for its length
    assert jwt_secret_problem(example) is not None


@pytest.mark.parametrize("secret", ["", "short", "x" * (JWT_SECRET_MIN_BYTES - 1)])
def test_a_secret_shorter_than_the_digest_is_refused(secret: str) -> None:
    problem = jwt_secret_problem(secret)

    assert problem is not None
    assert str(JWT_SECRET_MIN_BYTES) in problem


def test_the_length_is_counted_in_bytes() -> None:
    assert jwt_secret_problem("x" * JWT_SECRET_MIN_BYTES) is None
    # Two bytes each in UTF-8: half as many characters reach the same length.
    assert jwt_secret_problem("é" * (JWT_SECRET_MIN_BYTES // 2)) is None


def test_a_generated_secret_is_accepted() -> None:
    assert jwt_secret_problem(secrets.token_urlsafe(64)) is None


def test_the_tests_themselves_run_with_a_secret_web_accepts() -> None:
    # tests/conftest.py sets it: every test that starts the application goes
    # through `_startup`, so the suite must not rely on the refused default.
    assert jwt_secret_problem(settings.jwt_secret) is None


def test_startup_stops_on_a_published_placeholder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "jwt_secret", _example_secret())

    with pytest.raises(RuntimeError, match="JWT_SECRET is still a placeholder"):
        main._startup()


def test_the_refusal_says_what_to_do_and_never_prints_the_secret(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "zz-too-short-to-sign"
    monkeypatch.setattr(settings, "jwt_secret", secret)

    with pytest.raises(RuntimeError) as refused:
        main._refuse_an_unusable_jwt_secret()

    captured = capsys.readouterr()
    assert "openssl rand" in str(refused.value)
    assert secret not in str(refused.value)
    assert secret not in captured.out + captured.err


def test_startup_goes_on_with_a_usable_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "jwt_secret", secrets.token_urlsafe(64))

    main._refuse_an_unusable_jwt_secret()  # no exception
