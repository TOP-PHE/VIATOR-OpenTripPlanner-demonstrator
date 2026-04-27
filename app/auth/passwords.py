"""Password hashing — bcrypt 12 rounds (matches OSCAR)."""

from __future__ import annotations

from passlib.context import CryptContext

# 12 rounds is the OSCAR convention. Higher is slower; lower trades off attacker cost.
_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=12)

# NIST 800-63B alignment: enforce minimum length only. No composition rules.
MIN_PASSWORD_LENGTH = 12


class WeakPasswordError(ValueError):
    """Raised by callers when a password fails the policy."""


def hash_password(plain: str) -> str:
    return _pwd.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return _pwd.verify(plain, hashed)


def assert_strong_enough(plain: str) -> None:
    """Raise WeakPasswordError if the password violates policy."""
    if len(plain) < MIN_PASSWORD_LENGTH:
        raise WeakPasswordError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
