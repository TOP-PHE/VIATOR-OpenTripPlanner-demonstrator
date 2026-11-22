"""What a session id and the per-session settings may be (`app/session_config.py`).

The id and several settings are written into the compose and nginx fragments
the orchestrator renders, and into the arguments of the build containers. The
checks are held here on what a text field can carry: a line feed, a quote, a
YAML key.
"""

from __future__ import annotations

from typing import Any

import pytest

from app import session_config


@pytest.mark.parametrize("value", ["ab", "nap-fr-rail", "eu19-transit-motis", "a" + "b" * 62])
def test_a_slug_is_a_session_id(value: str) -> None:
    assert session_config.is_session_id(value)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "a",
        "a" + "b" * 63,
        "Nap",
        "1ab",
        "-ab",
        "na p",
        "nap_fr",
        "nap/fr",
        "abc\n",
        "abc\r",
        "abc\n  evil:",
        None,
        12,
        ["abc"],
    ],
)
def test_anything_else_is_not_a_session_id(value: Any) -> None:
    assert not session_config.is_session_id(value)


def test_valid_settings_are_put_in_their_canonical_form() -> None:
    config: dict[str, Any] = {
        "otp_heap": "24G",
        "otp_build_heap": " 36g ",
        "build_max_memory": True,
    }

    session_config.validate_settings(config)

    assert config == {"otp_heap": "24g", "otp_build_heap": "36g", "build_max_memory": True}


def test_settings_that_are_absent_stay_absent() -> None:
    config: dict[str, Any] = {"sources": {"providers": []}, "notes": "kept as it is"}

    session_config.validate_settings(config)

    assert config == {"sources": {"providers": []}, "notes": "kept as it is"}


def test_the_start_period_is_checked_and_kept_as_it_was_sent() -> None:
    config: dict[str, Any] = {"otp_serve_start_period": "480"}

    session_config.validate_settings(config)

    assert config == {"otp_serve_start_period": "480"}


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("otp_heap", "lots"),
        ("otp_heap", '4g"\n    privileged: true\n    x: "'),
        ("otp_heap", 4),
        ("otp_build_heap", "12 GB"),
        ("otp_build_heap", "12g\n    privileged: true"),
        ("otp_serve_start_period", "abc"),
        ("otp_serve_start_period", 5),
        ("otp_serve_start_period", True),
        ("otp_timezone", "Nowhere/Invented"),
        ("build_max_memory", "true"),
    ],
)
def test_a_setting_that_is_not_valid_is_refused(key: str, value: Any) -> None:
    with pytest.raises(ValueError):
        session_config.validate_settings({key: value})
