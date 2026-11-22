"""The orchestrator renders only values it has checked itself.

A session's id, category and serve heap are written into the compose fragment
the worker applies with `docker compose up -d`. The API checks them when a
session is created or saved (`app/session_config.py`); these tests hold the
second check, at render time, on rows that reached the table some other way.
The fragment is read back as YAML: what counts is the document compose sees.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
import yaml

from app.models.sessions import SessionState
from app.sessions_orchestrator import render_compose, render_nginx


@dataclass
class _StubSession:
    """Just enough of SessionRow to drive the orchestrator."""

    id: str
    engine: str | None = "otp"
    category: str = "MERITS"
    state: str = SessionState.SERVING.value
    config: dict[str, Any] = field(default_factory=dict)


def _services(sessions: list[_StubSession]) -> dict[str, Any]:
    services: dict[str, Any] = yaml.safe_load(render_compose(sessions))["services"]  # type: ignore[arg-type]
    return services


_EXTRA_KEYS = '4g"\n    privileged: true\n    labels:\n      zz: "1'


def test_a_serve_heap_that_is_not_a_jvm_size_is_never_written() -> None:
    services = _services([_StubSession(id="zz-otp", config={"otp_heap": _EXTRA_KEYS})])

    assert set(services) == {"otp-zz-otp"}
    service = services["otp-zz-otp"]
    assert service["environment"]["OTP_HEAP"] == "4g"
    assert "privileged" not in service
    assert "zz" not in service["labels"]


def test_the_default_heap_is_derived_from_a_checked_build_heap_only() -> None:
    services = _services(
        [
            _StubSession(id="zz-text", config={"otp_build_heap": "36g"}),
            _StubSession(id="zz-number", config={"otp_build_heap": 36}),
            _StubSession(id="zz-lines", config={"otp_build_heap": _EXTRA_KEYS}),
        ]
    )

    assert services["otp-zz-text"]["environment"]["OTP_HEAP"] == "12g"
    assert services["otp-zz-number"]["environment"]["OTP_HEAP"] == "4g"
    assert services["otp-zz-lines"]["environment"]["OTP_HEAP"] == "4g"
    assert "privileged" not in services["otp-zz-lines"]


@pytest.mark.parametrize("engine", ["otp", "motis"])
def test_a_row_whose_id_is_not_a_slug_is_left_out_of_both_fragments(engine: str) -> None:
    not_a_slug = 'zz:\n    image: zz\n  zz-two"'
    sessions = [
        _StubSession(id=not_a_slug, engine=engine),
        _StubSession(id="zz-kept\n", engine=engine),
        _StubSession(id="zz-ok", engine=engine),
    ]

    assert set(_services(sessions)) == {f"{engine}-zz-ok"}
    nginx = render_nginx(sessions)  # type: ignore[arg-type]
    assert nginx.count("location ") == 1
    assert f"location /{engine}/zz-ok/ {{" in nginx
    assert "image: zz" not in nginx


@pytest.mark.parametrize("engine", ["otp", "motis"])
def test_a_row_whose_category_is_unknown_is_left_out_of_the_compose_fragment(engine: str) -> None:
    sessions = [
        _StubSession(id="zz-odd", engine=engine, category='MERITS"\n      zz: "1'),
        _StubSession(id="zz-ok", engine=engine),
    ]

    services = _services(sessions)

    assert set(services) == {f"{engine}-zz-ok"}
    assert "zz" not in services[f"{engine}-zz-ok"]["labels"]
