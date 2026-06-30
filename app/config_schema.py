"""Canonical CONFIG_SCHEMA for VIATOR's runtime-editable platform settings.

This is the **single source of truth** for which keys exist, what types they
take, what bounds apply, and which fields are sensitive (masked in GET responses).

See spec §12.1.
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

FieldType = Literal["str", "int", "bool", "secret"]


class FieldSpec(TypedDict, total=False):
    type: FieldType
    default: Any
    min: int  # for int fields
    max: int  # for int fields
    choices: list[str]  # for str fields with enumerated values
    sensitive: bool


# Sentinel used in GET responses for non-empty sensitive fields and (round-trip safe)
# accepted by PATCH as "no change for this field".
MASK_SENTINEL = "********"


CONFIG_SCHEMA: dict[str, FieldSpec] = {
    # ── SMTP ──────────────────────────────────────────────────────────
    "SMTP_HOST": {"type": "str", "default": ""},
    "SMTP_PORT": {"type": "int", "default": 587, "min": 1, "max": 65535},
    "SMTP_SECURE": {"type": "str", "default": "starttls", "choices": ["none", "starttls", "tls"]},
    "SMTP_USER": {"type": "str", "default": ""},
    "SMTP_PASS": {"type": "secret", "default": "", "sensitive": True},
    "SMTP_FROM": {"type": "str", "default": "no-reply@viator.local"},
    # ── Concurrency / server protection ───────────────────────────────
    "MAX_CONCURRENT_JOURNEYS": {"type": "int", "default": 20, "min": 1, "max": 200},
    "MAX_CONCURRENT_REBUILDS": {"type": "int", "default": 1, "min": 1, "max": 4},
    "MAX_CONCURRENT_UPLOADS": {"type": "int", "default": 3, "min": 1, "max": 20},
    "JOURNEY_TIMEOUT_MS": {"type": "int", "default": 8000, "min": 1000, "max": 60000},
    # ── Fanout behaviour ──────────────────────────────────────────────
    "FANOUT_TIMEOUT_MS": {"type": "int", "default": 10000, "min": 1000, "max": 60000},
    "FANOUT_PARTIAL_OK": {"type": "bool", "default": True},
    "STORE_RAW_RESPONSE": {"type": "bool", "default": True},
    # ── Journey query depth ───────────────────────────────────────────
    # How wide each engine searches per fanout call. Both OTP and MOTIS
    # accept these knobs (their fetch_plan signatures match). Defaults
    # are OTP's live-UI historical values (12/6h); operators tune up for
    # apples-to-apples engine comparison on dense corridors. Cost is OTP
    # RAPTOR's near-quadratic searchWindow scaling — bumping window to
    # 12h roughly triples wall-time per query on dense national feeds.
    "OTP_NUM_ITINERARIES": {"type": "int", "default": 12, "min": 1, "max": 50},
    "OTP_SEARCH_WINDOW_SECONDS": {"type": "int", "default": 21600, "min": 600, "max": 86400},
    # ── Swiss OJP reference comparison ────────────────────────────────
    # Opt-in journey-search comparison against an external reference OJP
    # endpoint (opentransportdata.swiss OJP 2.0). See
    # docs/ojp-reference-comparison-design.md. The feature stays dormant
    # until OJP_COMPARISON_ENABLED is true AND OJP_API_TOKEN is set — the
    # journey-UI toggle is hidden otherwise. The token is a platform-level
    # secret, stored here like SMTP_PASS (not in the per-provider
    # credential vault — it's not a provider feed credential).
    "OJP_COMPARISON_ENABLED": {"type": "bool", "default": False},
    "OJP_API_ENDPOINT": {
        "type": "str",
        "default": "https://api.opentransportdata.swiss/ojp20",
    },
    "OJP_API_TOKEN": {"type": "secret", "default": "", "sensitive": True},
    "OJP_TIMEOUT_MS": {"type": "int", "default": 10000, "min": 1000, "max": 60000},
    # ── Network coverage matrix ───────────────────────────────────────
    # Tunables for the background coverage runner (app/network_coverage/
    # runner.py). Defaults are bit-identical to the prior hardcoded
    # module constants so flipping any operator-tunable here is a
    # behaviour-neutral change. Config is read once at execute_run start
    # and frozen for the run's lifetime — editing mid-run does not
    # disturb the in-flight job.
    #
    # `_NUM_ITINERARIES` / `_SEARCH_WINDOW_SECONDS` mirror the OTP-side
    # query depth knobs above but apply to the coverage path (which uses
    # a wider window than the live UI to surface ALL alternatives per pair
    # within the depart window — see runner module docstring for the
    # 4h-vs-24h analysis). They're separate from OTP_* so operators can
    # tune coverage independently of the live journey-UI experience.
    #
    # `_PAIR_PARALLELISM` = 5 keeps OTP comfortable while cutting wall-
    # time ~5x vs sequential. `_PAIR_TIMEOUT_MS` = 60s matches OTP's
    # apiProcessingTimeout default. The VERIFY_* trio governs the
    # opt-in ÖBB-HAFAS external-verify sweep (PR-E): low parallelism (2)
    # + 500ms inter-call sleep keeps us under HAFAS's documented soft
    # cap of ~1-2 req/s/IP.
    "COVERAGE_NUM_ITINERARIES": {"type": "int", "default": 50, "min": 1, "max": 200},
    "COVERAGE_SEARCH_WINDOW_SECONDS": {"type": "int", "default": 14400, "min": 600, "max": 86400},
    "COVERAGE_PAIR_TIMEOUT_MS": {"type": "int", "default": 60000, "min": 5000, "max": 300000},
    "COVERAGE_PAIR_PARALLELISM": {"type": "int", "default": 5, "min": 1, "max": 20},
    "COVERAGE_VERIFY_PARALLELISM": {"type": "int", "default": 2, "min": 1, "max": 10},
    "COVERAGE_VERIFY_TIMEOUT_S": {"type": "int", "default": 30, "min": 5, "max": 300},
    "COVERAGE_VERIFY_SLEEP_MS": {"type": "int", "default": 500, "min": 0, "max": 10000},
    # ── Coverage K-slot time-slicing (PR-3) ───────────────────────────
    # The legacy per-pair coverage call was one numItineraries=50,
    # window=4h request. That worked for the 08:00 "morning peak"
    # baseline but threw away the apples-to-apples comparison surface
    # across MOTIS/OTP/OJP — each planner had its own preferred
    # internal anchor, so two engines could return a similar count but
    # cover wildly different time bands.
    #
    # PR-3 replaces that with K time-slot calls per pair: each slot is
    # `window/K` seconds wide (default K=6, window=86400 = 4h slots),
    # all engines see the same time grid, and the runner filters every
    # returned trip on whether its FIRST TRANSIT LEG's scheduled
    # departure falls in [day_start, day_end) of the origin's local
    # timezone. K=1 preserves the legacy single-call behaviour — this
    # IS the rollback flag.
    #
    # `_NUM_ITINERARIES_PER_SLOT` is intentionally lower than the
    # legacy `COVERAGE_NUM_ITINERARIES` (10 vs 50) because we're now
    # querying K times — 6 * 10 = 60 max trips/pair instead of 50, but
    # spread evenly across the day instead of clustered at one anchor.
    # Each slot's per-call timeout (20s) is well under OTP's 60s
    # apiProcessingTimeout default.
    #
    # `_WITHIN_PAIR_PARALLELISM` bounds the slot fan-out per pair so we
    # don't multiplicatively saturate OTP when combined with
    # `COVERAGE_PAIR_PARALLELISM`. Effective concurrency ceiling is
    # PAIR_PARALLELISM * WITHIN_PAIR_PARALLELISM = 3*3 = 9 simultaneous
    # fetch_plan calls (vs PR-2's 5 single calls).
    "COVERAGE_SLOT_COUNT": {"type": "int", "default": 6, "min": 1, "max": 24},
    "COVERAGE_NUM_ITINERARIES_PER_SLOT": {"type": "int", "default": 10, "min": 1, "max": 50},
    "COVERAGE_SLOT_TIMEOUT_MS": {"type": "int", "default": 20000, "min": 5000, "max": 120000},
    "COVERAGE_WITHIN_PAIR_PARALLELISM": {"type": "int", "default": 3, "min": 1, "max": 10},
    # Day-window defaults (per-run overridable via the create-run form).
    # The string-time format is "HH:MM" 24-hour; "24:00" is special-cased
    # server-side to mean "end of day" so 00:00-24:00 = full day. Per-
    # run window/timezone columns on `network_coverage_runs` default to
    # NULL = "use the platform_config defaults here".
    "COVERAGE_DEFAULT_WINDOW_START": {"type": "str", "default": "00:00"},
    "COVERAGE_DEFAULT_WINDOW_END": {"type": "str", "default": "24:00"},
    "COVERAGE_DEFAULT_TIMEZONE": {
        "type": "str",
        "default": "UTC",
        "choices": [
            "UTC",
            "Europe/Berlin",
            "Europe/Vienna",
            "Europe/Paris",
            "Europe/Madrid",
            "Europe/Stockholm",
            "Europe/Warsaw",
            "Europe/Budapest",
            "Europe/Prague",
            "Europe/Helsinki",
            "Europe/Athens",
            "Europe/Brussels",
            "Europe/Amsterdam",
            "Europe/Lisbon",
            "Europe/Rome",
        ],
    },
    # ── Master data refresh ───────────────────────────────────────────
    "MASTER_STATIONS_REFRESH_DAYS": {"type": "int", "default": 30, "min": 1, "max": 365},
    "MASTER_CARRIERS_REFRESH_DAYS": {"type": "int", "default": 90, "min": 1, "max": 365},
    # ── Worker timing (v0.1.11) ───────────────────────────────────────
    # How long the worker coalesces rebuild requests for one session in a
    # window. Default 1800 = 30 min (matches the legacy `.env`
    # DEBOUNCE_SECONDS the worker was reading from before v0.1.11).
    # Set to 0 for "rebuild starts on click" (demo-friendly; no coalescing).
    # The cache TTL is 30 s, so changes here take effect within 30 s.
    "REBUILD_DEBOUNCE_SECONDS": {"type": "int", "default": 1800, "min": 0, "max": 7200},
    # How often the worker polls the rebuild_jobs table. Lower = rebuilds
    # start sooner after their debounce window expires; higher = less DB
    # chatter. 15 was the hardcoded default before v0.1.11.
    "WORKER_TICK_SECONDS": {"type": "int", "default": 15, "min": 5, "max": 300},
    # ── Replay safety caps ────────────────────────────────────────────
    "REPLAY_MAX_BATCH_SIZE": {"type": "int", "default": 1000, "min": 10, "max": 10000},
    "REPLAY_MAX_RPS": {"type": "int", "default": 5, "min": 1, "max": 50},
    # ── Registration policy ───────────────────────────────────────────
    "REGISTRATION_OPEN": {"type": "bool", "default": True},
    "REGISTRATION_DEFAULT_ROLE": {
        "type": "str",
        "default": "end_user",
        "choices": ["end_user", "content_manager"],
    },
    # ── Retention (three tiers, see §11.1) ────────────────────────────
    "AUDIT_RETENTION_DAYS": {"type": "int", "default": 365, "min": 30, "max": 3650},
    "JOURNEY_SEARCH_RETENTION_DAYS": {"type": "int", "default": 365, "min": 30, "max": 3650},
    "JOURNEY_TRIPS_RETENTION_DAYS": {"type": "int", "default": 180, "min": 30, "max": 3650},
    "JOURNEY_RAW_RESPONSE_RETENTION_DAYS": {"type": "int", "default": 30, "min": 7, "max": 365},
}


# ────────────────────────────────── helpers ──────────────────────────────────


def is_sensitive(key: str) -> bool:
    spec = CONFIG_SCHEMA[key]
    return bool(spec.get("sensitive")) or spec["type"] == "secret"


def coerce(key: str, raw: Any) -> Any:
    """Convert a value (often a str from DB or HTTP body) to the schema type.

    Raises ValueError with a precise message if the value is invalid.
    """
    if key not in CONFIG_SCHEMA:
        raise ValueError(f"unknown config key: {key!r}")
    spec = CONFIG_SCHEMA[key]
    t = spec["type"]

    if raw is None:
        return None

    if t in ("str", "secret"):
        s = str(raw)
        choices = spec.get("choices")
        if choices is not None and s not in choices:
            raise ValueError(f"{key}: must be one of {choices}, got {s!r}")
        return s

    if t == "int":
        try:
            n = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key}: expected int, got {raw!r}") from exc
        lo = spec.get("min")
        hi = spec.get("max")
        if lo is not None and n < lo:
            raise ValueError(f"{key}: {n} below minimum {lo}")
        if hi is not None and n > hi:
            raise ValueError(f"{key}: {n} above maximum {hi}")
        return n

    if t == "bool":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            normalized = raw.strip().lower()
            if normalized in ("true", "1", "yes", "on"):
                return True
            if normalized in ("false", "0", "no", "off"):
                return False
        raise ValueError(f"{key}: expected bool, got {raw!r}")

    raise ValueError(f"{key}: unsupported field type {t!r}")


def serialize(key: str, value: Any) -> str | None:
    """Convert a typed value back to a string for storage in platform_config.value."""
    if value is None:
        return None
    spec = CONFIG_SCHEMA[key]
    t = spec["type"]
    if t == "bool":
        return "true" if value else "false"
    return str(value)


def default_for(key: str) -> Any:
    return CONFIG_SCHEMA[key].get("default")
