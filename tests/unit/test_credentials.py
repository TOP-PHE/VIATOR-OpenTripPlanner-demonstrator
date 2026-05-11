"""Unit tests for app/credentials.py — HKDF + AES-GCM crypto + httpx auth.

These are pure-Python tests (no DB, no HTTP). The CRUD API is exercised
in tests/integration/ once Postgres is available.

Coverage focus:
  - Round-trip: encrypt → decrypt returns the original plaintext
  - Tamper-resistance: flipping a bit in ciphertext fails authentication
  - Wrong-key rejection: decrypting with a different JWT_SECRET fails
  - Validation: each auth_type's param_name rules
  - apply_to_request: each auth_type produces the right URL/headers
  - URL preservation: query auth doesn't lose existing query params
"""

from __future__ import annotations

import pytest

# ─────────────────────────── crypto round-trip ───────────────────────────


class TestCryptoRoundTrip:
    def test_encrypt_decrypt_returns_original(self):
        from app.credentials import decrypt, encrypt

        ciphertext, nonce = encrypt("my-api-key-12345", "test-jwt-secret-32-bytes-or-more!")
        plaintext = decrypt(ciphertext, nonce, "test-jwt-secret-32-bytes-or-more!")
        assert plaintext == "my-api-key-12345"

    def test_two_encryptions_of_same_value_differ(self):
        """Fresh nonce per call → ciphertexts must differ even for the same input."""
        from app.credentials import encrypt

        ct1, nonce1 = encrypt("same-secret", "k" * 32)
        ct2, nonce2 = encrypt("same-secret", "k" * 32)
        assert ct1 != ct2 or nonce1 != nonce2  # nonces should differ even more strongly

    def test_long_secret_round_trips(self):
        from app.credentials import decrypt, encrypt

        # Token-shaped: ~256 chars
        secret = "abc.DEF-123_xyz" * 20
        ct, nonce = encrypt(secret, "kk" * 32)
        assert decrypt(ct, nonce, "kk" * 32) == secret

    def test_unicode_secret_round_trips(self):
        """Some operators use non-ASCII passwords; UTF-8 round-trip must hold."""
        from app.credentials import decrypt, encrypt

        secret = "pässwörd-€-中文-🔑"
        ct, nonce = encrypt(secret, "k" * 32)
        assert decrypt(ct, nonce, "k" * 32) == secret


# ─────────────────────────── failure modes ───────────────────────────


class TestCryptoFailureModes:
    def test_wrong_key_raises_decrypt_error(self):
        from app.credentials import CredentialDecryptError, decrypt, encrypt

        ct, nonce = encrypt("secret", "key-A-padded-to-be-long-enough!")
        with pytest.raises(CredentialDecryptError, match="JWT_SECRET rotated"):
            decrypt(ct, nonce, "key-B-completely-different-value!")

    def test_tampered_ciphertext_raises(self):
        """Flipping any bit in the ciphertext should fail GCM auth."""
        from app.credentials import CredentialDecryptError, decrypt, encrypt

        ct, nonce = encrypt("secret", "k" * 32)
        # Flip one bit somewhere in the middle.
        tampered = bytearray(ct)
        tampered[len(tampered) // 2] ^= 0x01
        with pytest.raises(CredentialDecryptError):
            decrypt(bytes(tampered), nonce, "k" * 32)

    def test_empty_secret_rejected(self):
        from app.credentials import encrypt

        with pytest.raises(ValueError, match="empty"):
            encrypt("", "k" * 32)

    def test_empty_jwt_secret_rejected(self):
        from app.credentials import encrypt

        with pytest.raises(ValueError, match="JWT_SECRET is empty"):
            encrypt("anything", "")


# ─────────────────────────── input validation ───────────────────────────


class TestValidateAuthType:
    def test_valid_types_accepted(self):
        from app.credentials import validate_auth_type

        for t in ("none", "bearer", "basic", "query", "header"):
            assert validate_auth_type(t) == t

    def test_case_insensitive(self):
        from app.credentials import validate_auth_type

        assert validate_auth_type("BEARER") == "bearer"
        assert validate_auth_type("  Query  ") == "query"

    def test_unknown_rejected(self):
        from app.credentials import validate_auth_type

        with pytest.raises(ValueError, match="unknown"):
            validate_auth_type("oauth2")


class TestValidateParamName:
    def test_query_requires_param_name(self):
        from app.credentials import validate_param_name

        assert validate_param_name("query", "apikey") == "apikey"
        with pytest.raises(ValueError, match="requires param_name"):
            validate_param_name("query", None)
        with pytest.raises(ValueError, match="requires param_name"):
            validate_param_name("query", "")

    def test_header_requires_param_name(self):
        from app.credentials import validate_param_name

        assert validate_param_name("header", "X-API-Key") == "X-API-Key"
        with pytest.raises(ValueError, match="requires param_name"):
            validate_param_name("header", None)

    def test_bearer_rejects_param_name(self):
        from app.credentials import validate_param_name

        assert validate_param_name("bearer", None) is None
        assert validate_param_name("bearer", "") is None
        with pytest.raises(ValueError, match="must not set param_name"):
            validate_param_name("bearer", "Authorization")

    def test_basic_rejects_param_name(self):
        from app.credentials import validate_param_name

        assert validate_param_name("basic", None) is None
        with pytest.raises(ValueError, match="must not set param_name"):
            validate_param_name("basic", "user")

    def test_param_name_length_capped(self):
        from app.credentials import validate_param_name

        with pytest.raises(ValueError, match="longer than 80"):
            validate_param_name("query", "x" * 81)


# ─────────────────────────── apply_to_request ───────────────────────────


class TestApplyToRequest:
    def test_bearer_sets_authorization_header(self):
        from app.credentials import apply_to_request

        url, headers = apply_to_request(
            "https://api.example/endpoint",
            auth_type="bearer",
            plaintext="my-token",
            param_name=None,
        )
        assert url == "https://api.example/endpoint"
        assert headers == {"Authorization": "Bearer my-token"}

    def test_basic_b64_encodes_authorization_header(self):
        from app.credentials import apply_to_request

        _url, headers = apply_to_request(
            "https://api.example",
            auth_type="basic",
            plaintext="alice:s3cret",
            param_name=None,
        )
        # base64("alice:s3cret") = "YWxpY2U6czNjcmV0"
        assert headers == {"Authorization": "Basic YWxpY2U6czNjcmV0"}

    def test_header_uses_custom_name(self):
        from app.credentials import apply_to_request

        url, headers = apply_to_request(
            "https://api.example",
            auth_type="header",
            plaintext="value-123",
            param_name="X-Custom-Auth",
        )
        assert url == "https://api.example"
        assert headers == {"X-Custom-Auth": "value-123"}

    def test_query_appends_to_url(self):
        from app.credentials import apply_to_request

        url, headers = apply_to_request(
            "https://api.example/data",
            auth_type="query",
            plaintext="MY_KEY_123",
            param_name="apikey",
        )
        assert url == "https://api.example/data?apikey=MY_KEY_123"
        assert headers == {}

    def test_query_preserves_existing_params(self):
        """Adding apikey shouldn't drop the existing format=json param."""
        from app.credentials import apply_to_request

        url, _ = apply_to_request(
            "https://api.example/data?format=json&pretty=true",
            auth_type="query",
            plaintext="MY_KEY",
            param_name="apikey",
        )
        # Existing params preserved, new one appended.
        assert "format=json" in url
        assert "pretty=true" in url
        assert "apikey=MY_KEY" in url

    def test_query_overrides_clashing_param(self):
        """If the URL already has the same param key, our value wins."""
        from app.credentials import apply_to_request

        url, _ = apply_to_request(
            "https://api.example?apikey=OLD_VALUE",
            auth_type="query",
            plaintext="NEW_VALUE",
            param_name="apikey",
        )
        # Only one apikey, with the new value.
        assert url.count("apikey=") == 1
        assert "apikey=NEW_VALUE" in url

    def test_none_returns_url_unchanged(self):
        from app.credentials import apply_to_request

        url, headers = apply_to_request(
            "https://api.example",
            auth_type="none",
            plaintext="anything",
            param_name=None,
        )
        assert url == "https://api.example"
        assert headers == {}


# ─────────────────────────── HKDF key derivation ───────────────────────────


class TestKeyDerivation:
    def test_same_secret_yields_same_key(self):
        """HKDF must be deterministic — re-deriving with the same input
        produces the same key, otherwise round-trip decryption breaks."""
        from app.credentials import _derive_key

        k1 = _derive_key("shared-secret")
        k2 = _derive_key("shared-secret")
        assert k1 == k2
        assert len(k1) == 32  # AES-256

    def test_different_secrets_yield_different_keys(self):
        from app.credentials import _derive_key

        k1 = _derive_key("secret-A")
        k2 = _derive_key("secret-B")
        assert k1 != k2

    def test_str_and_bytes_inputs_equivalent(self):
        """Calling with str vs equivalent bytes must produce the same key."""
        from app.credentials import _derive_key

        k1 = _derive_key("test-secret")
        k2 = _derive_key(b"test-secret")
        assert k1 == k2
