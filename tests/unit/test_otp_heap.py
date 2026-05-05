"""Unit tests for v0.1.23's `otp_build_heap` validation.

The validator is the fail-fast gate for the new per-session heap
dropdown. A bad value at save time raises a 400 instead of silently
falling back to the env-var default at the worker — important because
otherwise an operator who carefully picked '32g' in the UI would have
no way to tell their choice was ignored when the build still OOM'd.
"""

from __future__ import annotations

import pytest

from app.otp_heap import COMMON_HEAPS, DEFAULT_HEAP, validate_heap

# ──────────────────────── happy path ────────────────────────


def test_default_heap_matches_settings():
    """The default exposed by the module must match `settings.otp_build_heap`'s
    default. If they drift, a session that didn't set the field would inherit
    a different value than a legacy session — confusing."""
    assert DEFAULT_HEAP == "12g"


def test_none_returns_default():
    """Legacy sessions (pre-v0.1.23) won't have `otp_build_heap` set."""
    assert validate_heap(None) == "12g"


def test_empty_string_returns_default():
    """Operator clearing the field falls back to default rather than 400."""
    assert validate_heap("") == "12g"


def test_explicit_default_arg_is_honoured():
    """Caller can override the fallback (worker passes `settings.otp_build_heap`
    so a VPS with .env override sees its env value when the session config
    doesn't set the field)."""
    assert validate_heap(None, default="24g") == "24g"
    assert validate_heap("", default="36g") == "36g"


@pytest.mark.parametrize(
    "value,expected",
    [
        ("12g", "12g"),
        ("24g", "24g"),
        ("36g", "36g"),
        ("8192m", "8192m"),
        # Case insensitivity at the unit position — JVM accepts both 'g' and 'G'.
        ("12G", "12g"),
        ("24G", "24g"),
        # Whitespace gets trimmed.
        ("  16g  ", "16g"),
        ("16g\n", "16g"),
    ],
)
def test_valid_values_normalize_to_lowercase_unit(value, expected):
    """Round-trip happy path: any well-formed value comes back lowercased."""
    assert validate_heap(value) == expected


def test_all_curated_dropdown_values_validate():
    """Every entry in the UI dropdown must validate. CI catches the
    embarrassing case where someone adds a bad option to the curated list."""
    for heap_value, _label in COMMON_HEAPS:
        assert validate_heap(heap_value) == heap_value


# ──────────────────────── failure modes ────────────────────────


@pytest.mark.parametrize(
    "bad_value",
    [
        "12 g",  # space between qty and unit
        "12gb",  # 'gb' is two chars
        "12gigs",  # nonsense suffix
        "12.5g",  # JVM doesn't accept fractional heap
        "g12",  # backwards
        "12",  # missing unit (JVM defaults to bytes which is wrong)
        "G",  # missing qty
        "twelve_g",  # alphabet
        "-12g",  # negative
        "0x12g",  # hex
    ],
)
def test_malformed_strings_rejected(bad_value):
    """Pin a handful of plausible-but-wrong values that operators might
    type by accident. Each gets a clear 400 instead of silently falling
    back to env default."""
    with pytest.raises(ValueError, match="doesn't match the expected pattern"):
        validate_heap(bad_value)


def test_non_string_value_rejected_with_clear_message():
    """JSONB can hold numbers / booleans if hand-edited via raw API."""
    with pytest.raises(ValueError, match="must be a string"):
        validate_heap(12)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="must be a string"):
        validate_heap(True)  # type: ignore[arg-type]


def test_whitespace_only_rejected():
    """Bare whitespace looks like 'something' but isn't a valid heap."""
    with pytest.raises(ValueError):
        validate_heap("   ")


# ──────────────────────── COMMON_HEAPS structure ────────────────────


def test_common_heaps_starts_with_default():
    """The default must be the first dropdown entry — that's what the
    browser renders as the initial selection."""
    assert COMMON_HEAPS[0][0] == DEFAULT_HEAP


def test_common_heaps_unique_values():
    """Defensive: copy-paste edits could introduce duplicates."""
    seen: set[str] = set()
    for value, _label in COMMON_HEAPS:
        assert value not in seen, f"duplicate heap value: {value}"
        seen.add(value)


def test_common_heaps_monotonically_increasing():
    """The dropdown list should be ordered low-to-high so operators
    scrolling down can pick the next-bigger value when their build
    OOMs at the current pick."""
    sizes = [int(v.rstrip("gGmM")) for v, _ in COMMON_HEAPS if v.endswith(("g", "G"))]
    assert sizes == sorted(sizes), "COMMON_HEAPS not monotonically increasing"
