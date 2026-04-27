"""bcrypt round-trip + strength policy."""

from __future__ import annotations

import pytest

from app.auth.passwords import (
    MIN_PASSWORD_LENGTH,
    WeakPasswordError,
    assert_strong_enough,
    hash_password,
    verify_password,
)


def test_round_trip() -> None:
    h = hash_password("a-perfectly-fine-passphrase")
    assert verify_password("a-perfectly-fine-passphrase", h)
    assert not verify_password("wrong", h)


def test_hash_is_not_plaintext() -> None:
    h = hash_password("plaintext-leak-check")
    assert "plaintext-leak-check" not in h
    assert h.startswith("$2")  # bcrypt sigil


def test_two_hashes_of_same_password_differ() -> None:
    """Distinct salts → distinct hashes (otherwise the salt is broken)."""
    a = hash_password("same-input")
    b = hash_password("same-input")
    assert a != b
    assert verify_password("same-input", a)
    assert verify_password("same-input", b)


def test_strength_minimum() -> None:
    with pytest.raises(WeakPasswordError):
        assert_strong_enough("short")
    # exactly the minimum is fine
    assert_strong_enough("x" * MIN_PASSWORD_LENGTH)
