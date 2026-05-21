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


# ─── Audit-2026-05.md follow-up — serve heap (v0.1.32.21) ────────────────────


def test_default_serve_heap_for_build_with_no_build_heap():
    """When build heap is missing/None/empty, fall back to the 4g floor —
    no way to derive a smarter default without knowing the build."""
    from app.otp_heap import default_serve_heap_for_build

    assert default_serve_heap_for_build(None) == "4g"
    assert default_serve_heap_for_build("") == "4g"


def test_default_serve_heap_for_build_proportional():
    """Serve heap ≈ build_heap / 3, rounded up. Validates the headline
    cases that operators are most likely to pick."""
    from app.otp_heap import default_serve_heap_for_build

    # 12g build → 4g serve (floored)
    assert default_serve_heap_for_build("12g") == "4g"
    # 24g build (France-wide standard) → 8g serve
    assert default_serve_heap_for_build("24g") == "8g"
    # 36g build → 12g serve
    assert default_serve_heap_for_build("36g") == "12g"
    # 48g build (Europe-wide rail) → 16g serve (rounded up from 16)
    assert default_serve_heap_for_build("48g") == "16g"
    # 64g build (Europe-wide multi-modal) → 22g serve
    assert default_serve_heap_for_build("64g") == "22g"
    # 72g build (max) → 24g+ serve (round-up)
    assert default_serve_heap_for_build("72g") == "24g"


def test_default_serve_heap_floor_is_4g():
    """Anything below 12g build still gets 4g serve — never go below
    the JVM minimum that an IDF graph needs."""
    from app.otp_heap import default_serve_heap_for_build

    assert default_serve_heap_for_build("4g") == "4g"
    assert default_serve_heap_for_build("8g") == "4g"
    assert default_serve_heap_for_build("11g") == "4g"


def test_default_serve_heap_invalid_input_falls_back_to_4g():
    """Defensive: bad strings shouldn't crash the orchestrator at boot.
    They get the 4g floor, which is too small for a France-wide graph
    but at least lets the container START so the operator sees the
    OOM-loop and fixes the value via the UI."""
    from app.otp_heap import default_serve_heap_for_build

    assert default_serve_heap_for_build("not a heap") == "4g"
    assert default_serve_heap_for_build("12 GB") == "4g"
    assert default_serve_heap_for_build("12gb") == "4g"


def test_default_serve_heap_megabytes_returns_floor():
    """Megabyte-suffixed build heaps imply tiny graphs — 4g serve
    floor still applies (no point computing m/3)."""
    from app.otp_heap import default_serve_heap_for_build

    assert default_serve_heap_for_build("8192m") == "4g"
    assert default_serve_heap_for_build("4096m") == "4g"


def test_common_serve_heaps_no_duplicates_and_sorted():
    """Same invariants as COMMON_HEAPS — no duplicate values, ordered
    low-to-high so the dropdown reads naturally."""
    from app.otp_heap import COMMON_SERVE_HEAPS

    seen = set()
    for value, _label in COMMON_SERVE_HEAPS:
        assert value not in seen, f"duplicate serve heap value: {value}"
        seen.add(value)

    sizes = [int(v.rstrip("gGmM")) for v, _ in COMMON_SERVE_HEAPS if v.endswith(("g", "G"))]
    assert sizes == sorted(sizes), "COMMON_SERVE_HEAPS not monotonically increasing"


# ─── v0.1.38 — build cgroup mem_limit derivation ────────────────────────────


@pytest.mark.parametrize(
    "heap,gb",
    [
        ("8g", 8),
        ("12g", 12),
        ("24g", 24),
        ("72g", 72),
        ("12G", 12),  # case-insensitive unit
        ("  16g ", 16),  # trimmed
        ("8192m", 8),  # exact MB→GB
        ("4096m", 4),
        ("512m", 1),  # sub-GB rounds UP (never 0 — a 0g cap is nonsense)
        ("1536m", 2),  # 1.5 GB rounds up to 2
        ("1024m", 1),
    ],
)
def test_heap_to_gb(heap, gb):
    from app.otp_heap import heap_to_gb

    assert heap_to_gb(heap) == gb


def test_heap_to_gb_rejects_malformed():
    from app.otp_heap import heap_to_gb

    with pytest.raises(ValueError, match="doesn't match"):
        heap_to_gb("12gb")
    with pytest.raises(ValueError, match="doesn't match"):
        heap_to_gb("garbage")


@pytest.mark.parametrize(
    "heap,limit",
    [
        # Reproduces every pairing documented in .env.example + otp_heap.py:
        ("8g", "12g"),  # default compose fallback (OTP_HEAP=8g / mem_limit=12g)
        ("12g", "16g"),
        ("24g", "28g"),  # .env France-wide example
        ("36g", "42g"),
        ("48g", "56g"),
        ("64g", "74g"),
        ("72g", "84g"),  # "≈84 GB cgroup cap on a 96 GB host" note
        ("8192m", "12g"),  # MB heap → GB cap
    ],
)
def test_mem_limit_for_heap(heap, limit):
    from app.otp_heap import mem_limit_for_heap

    assert mem_limit_for_heap(heap) == limit


def test_mem_limit_headroom_is_at_least_4g():
    """Small heaps still get the flat 4 GB native floor."""
    from app.otp_heap import heap_to_gb, mem_limit_for_heap

    for heap in ("4g", "8g", "12g", "20g"):
        assert heap_to_gb(mem_limit_for_heap(heap)) - heap_to_gb(heap) >= 4


def test_mem_limit_for_heap_none_empty_malformed_uses_default():
    """A session with no/blank/garbage heap still gets a matched cap derived
    from DEFAULT_HEAP (12g → 16g) rather than a too-small static value."""
    from app.otp_heap import mem_limit_for_heap

    assert mem_limit_for_heap(None) == "16g"  # type: ignore[arg-type]
    assert mem_limit_for_heap("") == "16g"
    assert mem_limit_for_heap("not-a-heap") == "16g"
    # Caller-supplied default is honoured (worker passes settings.otp_build_heap).
    assert mem_limit_for_heap(None, default="24g") == "28g"  # type: ignore[arg-type]
    assert mem_limit_for_heap("bogus", default="48g") == "56g"


# ─── v0.1.38 — max-memory rebuild auto heap sizing ──────────────────────────


@pytest.mark.parametrize(
    "host_gb,heap",
    [
        # (default reserve 8g) the derived cap must always fit host-reserve:
        (96, "75g"),  # avail 88 → 75g (cap 87 ≤ 88)
        (47, "33g"),  # avail 39 → 33g (cap 38 ≤ 39)
        (32, "20g"),  # avail 24 → 20g (cap 24 ≤ 24, exact)
        (24, "12g"),  # avail 16 → 12g (cap 16 ≤ 16; closed-form 13g would overshoot)
    ],
)
def test_auto_build_heap_fits_host(host_gb, heap):
    from app.otp_heap import auto_build_heap, heap_to_gb, mem_limit_for_heap

    assert auto_build_heap(host_gb) == heap
    # Invariant: the derived cgroup cap never exceeds host - reserve.
    assert heap_to_gb(mem_limit_for_heap(heap)) <= host_gb - 8


def test_auto_build_heap_returns_none_when_box_too_small():
    """A box that can't fit even an 8g build after the reserve gets None —
    the caller keeps the configured heap rather than shrinking it."""
    from app.otp_heap import auto_build_heap

    assert auto_build_heap(16) is None  # avail 8, 8g cap is 12 > 8
    assert auto_build_heap(12) is None  # avail 4 < min 8
    assert auto_build_heap(8) is None


def test_auto_build_heap_custom_reserve():
    from app.otp_heap import auto_build_heap, heap_to_gb, mem_limit_for_heap

    heap = auto_build_heap(64, reserve_gb=16)
    assert heap is not None
    # Cap must fit 64 - 16 = 48.
    assert heap_to_gb(mem_limit_for_heap(heap)) <= 48
