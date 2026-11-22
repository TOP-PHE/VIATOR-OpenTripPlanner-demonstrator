"""A session's settings are checked when it is created, as when it is saved.

`POST /api/sessions` used to store its `config` as it came, while `PATCH`
checked each setting: a value the save would have refused was rendered and
built all the same. Both now go through `session_config.validate_settings`,
before anything is read from or written to the database. Driven through the
real route functions on stand-in rows; invented values only.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException, Response
from starlette.requests import Request

from app.api.admin import sessions
from app.security import CurrentUser

_EXTRA_KEYS = '4g"\n    privileged: true\n    x: "'


class _Reached(Exception):
    """The route went past its checks: it touched the database."""


class _Db:
    """`get` answers `row`; anything else means the checks let the request through."""

    def __init__(self, row: Any) -> None:
        self.row = row

    def get(self, *_args: Any, **_kwargs: Any) -> Any:
        return self.row

    def __getattr__(self, name: str) -> Any:
        raise _Reached(name)


class _Untouched:
    """A database a refused request must never reach."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the database was reached ({name}) before the refusal")


def _request() -> Request:
    return Request({"type": "http", "headers": [], "client": ("127.0.0.1", 1)})


def _actor() -> CurrentUser:
    return CurrentUser(id=uuid.uuid4(), username="zz@example.invalid", role="platform_admin")


def _create(session_id: str, config: dict[str, Any], db: Any) -> sessions.SessionCreate:
    body = sessions.SessionCreate(id=session_id, name="Zz", category="MANUAL", config=config)
    sessions.create_session(body, _request(), Response(), db, _actor())
    return body


@pytest.mark.parametrize(
    "config",
    [
        {"otp_heap": "lots"},
        {"otp_heap": _EXTRA_KEYS},
        {"otp_build_heap": "12 GB"},
        {"otp_serve_start_period": "abc"},
        {"build_max_memory": "true"},
    ],
)
def test_a_creation_with_a_setting_a_save_refuses_is_refused(config: dict[str, Any]) -> None:
    with pytest.raises(HTTPException) as refused:
        _create("zz-new", config, _Untouched())

    assert refused.value.status_code == 400


@pytest.mark.parametrize("session_id", ["zz-new\n", "zz new", "Zz-new", "zz/new"])
def test_a_creation_with_an_id_that_is_not_a_slug_is_refused(session_id: str) -> None:
    with pytest.raises(HTTPException) as refused:
        _create(session_id, {}, _Untouched())

    assert refused.value.status_code == 400
    assert "slug" in str(refused.value.detail)


def test_a_creation_stores_its_settings_in_their_canonical_form() -> None:
    body = sessions.SessionCreate(
        id="zz-new", name="Zz", category="MANUAL", config={"otp_heap": "24G"}
    )

    with pytest.raises(_Reached):
        sessions.create_session(body, _request(), Response(), _Db(None), _actor())

    assert body.config == {"otp_heap": "24g"}


def test_a_save_still_refuses_the_same_settings() -> None:
    row = SimpleNamespace(id="zz-session", name="Zz", config={}, state="created")
    patch = sessions.SessionPatch(config={"otp_heap": _EXTRA_KEYS})

    with pytest.raises(HTTPException) as refused:
        sessions.patch_session("zz-session", patch, _request(), Response(), _Db(row), _actor())

    assert refused.value.status_code == 400
    assert row.config == {}
