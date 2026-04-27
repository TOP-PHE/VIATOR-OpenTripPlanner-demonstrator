"""Schema-level validation: types, bounds, choices, sensitivity."""

from __future__ import annotations

import pytest

from app.config_schema import (
    CONFIG_SCHEMA,
    MASK_SENTINEL,
    coerce,
    default_for,
    is_sensitive,
    serialize,
)


def test_every_key_has_a_default() -> None:
    for key, spec in CONFIG_SCHEMA.items():
        assert "default" in spec, f"{key}: missing default"
        assert spec["type"] in ("str", "int", "bool", "secret"), f"{key}: bad type {spec['type']}"


def test_smtp_pass_is_sensitive() -> None:
    assert is_sensitive("SMTP_PASS")
    assert not is_sensitive("SMTP_HOST")
    assert not is_sensitive("MAX_CONCURRENT_JOURNEYS")


@pytest.mark.parametrize("raw", ["true", "1", "yes", "on", True])
def test_bool_truthy(raw: object) -> None:
    assert coerce("REGISTRATION_OPEN", raw) is True


@pytest.mark.parametrize("raw", ["false", "0", "no", "off", False])
def test_bool_falsy(raw: object) -> None:
    assert coerce("REGISTRATION_OPEN", raw) is False


def test_bool_garbage_rejected() -> None:
    with pytest.raises(ValueError, match="expected bool"):
        coerce("REGISTRATION_OPEN", "maybe")


def test_int_within_bounds() -> None:
    assert coerce("MAX_CONCURRENT_JOURNEYS", "42") == 42


def test_int_below_min_rejected() -> None:
    with pytest.raises(ValueError, match="below minimum"):
        coerce("MAX_CONCURRENT_JOURNEYS", 0)


def test_int_above_max_rejected() -> None:
    with pytest.raises(ValueError, match="above maximum"):
        coerce("MAX_CONCURRENT_JOURNEYS", 1000)


def test_int_garbage_rejected() -> None:
    with pytest.raises(ValueError, match="expected int"):
        coerce("MAX_CONCURRENT_JOURNEYS", "lots")


def test_str_choices_enforced() -> None:
    assert coerce("SMTP_SECURE", "tls") == "tls"
    with pytest.raises(ValueError, match="must be one of"):
        coerce("SMTP_SECURE", "rot13")


def test_unknown_key_rejected() -> None:
    with pytest.raises(ValueError, match="unknown config key"):
        coerce("MADE_UP_KEY", "anything")


def test_serialize_round_trip() -> None:
    assert serialize("MAX_CONCURRENT_JOURNEYS", 30) == "30"
    assert serialize("REGISTRATION_OPEN", True) == "true"
    assert serialize("REGISTRATION_OPEN", False) == "false"
    assert serialize("SMTP_HOST", "smtp.example.com") == "smtp.example.com"
    assert serialize("SMTP_PORT", None) is None


def test_default_for() -> None:
    assert default_for("MAX_CONCURRENT_JOURNEYS") == 20
    assert default_for("REGISTRATION_OPEN") is True
    assert default_for("SMTP_FROM") == "no-reply@viator.local"


def test_mask_sentinel_constant() -> None:
    # Some routes round-trip this string; make sure it's stable.
    assert MASK_SENTINEL == "********"
