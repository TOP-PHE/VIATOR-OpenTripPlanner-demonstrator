"""Runtime config layer — schema-validated, masked, audit-logged, hot-swappable.

Read path: a per-process cache (`_cache`) holds the materialised config dict.
On first access, the cache is populated from the `platform_config` table merged
with `CONFIG_SCHEMA` defaults. The cache is invalidated on PATCH and refreshed
opportunistically (`refresh_after_seconds`) for processes that didn't perform
the PATCH themselves (e.g. the worker container).

Write path: validates against the schema, persists, audits, refreshes cache,
notifies the concurrency module for hot-swap.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit
from .config_schema import (
    CONFIG_SCHEMA,
    MASK_SENTINEL,
    coerce,
    default_for,
    is_sensitive,
    serialize,
)
from .models import PlatformConfig

# ────────────────────────────────── cache ──────────────────────────────────

_lock = threading.RLock()
_cache: dict[str, Any] | None = None
_cache_loaded_at: float = 0.0
_refresh_after_seconds: float = 30.0


def _load_from_db(db: Session) -> dict[str, Any]:
    """Read every PlatformConfig row, fall back to schema defaults for missing keys."""
    stored = {row.key: row.value for row in db.execute(select(PlatformConfig)).scalars().all()}
    out: dict[str, Any] = {}
    for key in CONFIG_SCHEMA:
        raw = stored.get(key)
        out[key] = coerce(key, raw) if raw is not None else default_for(key)
    return out


def _is_cache_stale() -> bool:
    return _cache is None or (time.monotonic() - _cache_loaded_at) > _refresh_after_seconds


def get_all(db: Session, *, force_refresh: bool = False) -> dict[str, Any]:
    """Return the full materialised config dict (typed values)."""
    global _cache, _cache_loaded_at
    with _lock:
        if force_refresh or _is_cache_stale():
            _cache = _load_from_db(db)
            _cache_loaded_at = time.monotonic()
        assert _cache is not None
        return dict(_cache)  # defensive copy


def get(db: Session, key: str) -> Any:
    """Get a single typed config value."""
    return get_all(db)[key]


def invalidate_cache() -> None:
    """Force the next read to repopulate from the DB. Call after writes outside the
    current process (or in tests)."""
    global _cache, _cache_loaded_at
    with _lock:
        _cache = None
        _cache_loaded_at = 0.0


# ──────────────────────────── response shaping ────────────────────────────


def as_response(db: Session) -> dict[str, Any]:
    """GET /api/admin/config payload. Sensitive non-empty values are masked."""
    cfg = get_all(db)
    out: dict[str, Any] = {}
    for key, value in cfg.items():
        if is_sensitive(key) and value:
            out[key] = MASK_SENTINEL
        else:
            out[key] = value
    return out


# ─────────────────────────────── write path ───────────────────────────────


class ConfigValidationError(ValueError):
    """One or more PATCH values failed validation. Carries per-field errors."""

    def __init__(self, errors: dict[str, str]) -> None:
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


def apply_patch(
    db: Session,
    payload: dict[str, Any],
    *,
    actor_user_id: uuid.UUID | None,
    actor_ip: str | None = None,
) -> dict[str, Any]:
    """Validate, persist, audit-log, refresh cache. Returns the new full config (masked).

    OSCAR-pattern semantics:
    - Unknown keys → 400 (the caller surfaces this as HTTPException).
    - The masked sentinel `'********'` on a sensitive field → **skipped** (no-op).
    - Type/bound mismatches → 400 with all field errors collected.
    """
    if not isinstance(payload, dict):
        raise ConfigValidationError({"_": "request body must be a JSON object"})

    errors: dict[str, str] = {}
    coerced: dict[str, Any] = {}

    for key, raw_value in payload.items():
        if key not in CONFIG_SCHEMA:
            errors[key] = "unknown config key"
            continue
        # Skip masked sentinels on sensitive fields — no-op.
        if is_sensitive(key) and raw_value == MASK_SENTINEL:
            continue
        try:
            coerced[key] = coerce(key, raw_value)
        except ValueError as exc:
            errors[key] = str(exc)

    if errors:
        raise ConfigValidationError(errors)

    # Persist + audit. Sensitive values are NEVER written to the audit metadata.
    current = get_all(db)
    actually_changed: list[str] = []

    for key, new_value in coerced.items():
        old_value = current.get(key)
        if old_value == new_value:
            continue
        actually_changed.append(key)

        existing = db.get(PlatformConfig, key)
        serialized = serialize(key, new_value)
        if existing is None:
            db.add(
                PlatformConfig(
                    key=key,
                    value=serialized,
                    updated_by=actor_user_id,
                )
            )
        else:
            existing.value = serialized
            existing.updated_by = actor_user_id

        audit.record(
            db,
            action="config.update",
            actor_user_id=actor_user_id,
            actor_ip=actor_ip,
            target_kind="platform_config",
            target_id=key,
            metadata={
                "key": key,
                "from": MASK_SENTINEL if is_sensitive(key) else _safe(old_value),
                "to": MASK_SENTINEL if is_sensitive(key) else _safe(new_value),
            },
        )

    db.flush()
    invalidate_cache()
    # Notify the concurrency module so its semaphores reflect any new limits.
    # Imported lazily to avoid a circular import at module load.
    from . import concurrency

    concurrency.semaphores.reload_from_config(get_all(db, force_refresh=True))

    return as_response(db)


def _safe(value: Any) -> Any:
    """Make a value JSON-safe for audit metadata."""
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return str(value)
