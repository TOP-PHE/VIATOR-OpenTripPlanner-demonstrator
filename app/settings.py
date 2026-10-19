"""Process-level settings — env-driven, not user-editable.

User-editable runtime config (SMTP, concurrency limits, registration policy,
retention) lives in `platform_config` and is managed via the admin UI — see
`app.config_schema`.

Anything that needs to change without redeploying belongs in `platform_config`.
Anything that's an infrastructure invariant (DB URL, JWT secret, paths) belongs here.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings

_log = logging.getLogger(__name__)
_HEADROOM_RE = re.compile(r"^(\d+)\s*[gG]?$")
_HEADROOM_DEFAULTS = {"otp_serve_mem_headroom_gb": 6, "otp_build_mem_headroom_gb": 4}


def headroom_gb_or_default(value: Any, default: int, name: str) -> int:
    """Whole gigabytes from `value` (int, "8" or "8g"), else `default` (logged)."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    match = _HEADROOM_RE.match(value.strip()) if isinstance(value, str) else None
    if match:
        return int(match.group(1))
    _log.warning("%s=%r is not a whole number of GB; using %d", name.upper(), value, default)
    return default


class Settings(BaseSettings):
    # Persistence
    database_url: str = "postgresql+psycopg://otp:otp@postgres:5432/otp"
    inbox_dir: Path = Path("/data/inbox")
    graph_dir: Path = Path("/data/graphs")

    # Phase-1 upload UI (HTTP basic auth — kept until journey UI lands).
    # Off unless BOTH are set; unset and empty mean the same thing (Phase-2 mode).
    admin_user: str = ""
    admin_password: str = ""
    max_upload_mb: int = 2048
    debounce_seconds: int = 1800
    # v0.1.32 — default bumped 12g → 24g. The 12g default fit when VIATOR
    # was France-only / single-provider (IDF-scale). With multi-NAP and
    # EU-scale rail-focused sessions the standard case is now ~3-8
    # providers, and 12g OOMs deep into "Intersecting unconnected areas".
    # Operators on tight VPS dial down per-session via session.config.
    # The .env's OTP_BUILD_HEAP override still wins; this only affects
    # the fallback when neither env nor session config sets a value.
    otp_build_heap: str = "24g"

    # Issue #374 — memory caps with no swap (app/memory_caps.py). Read by web,
    # which renders the per-session compose file, and by the worker.
    # OTP serve containers: capped by default at their -Xmx plus headroom
    # (app/otp_heap.serve_mem_limit_for_heap); the headroom setting can widen
    # the cap, never narrow it below heap + 4g. False = no cap (as before #374).
    otp_serve_mem_cap: bool = True
    otp_serve_mem_headroom_gb: int = 6
    # The build cap (app/otp_heap.mem_limit_for_heap) has no swap behind it
    # either; this widens it (never below heap + max(4g, heap/6)).
    otp_build_mem_headroom_gb: int = 4
    # MOTIS has no heap to derive a cap from (its memory follows the imported
    # data), so its caps are opt-in: empty = no cap. Set them from the
    # "Container memory" panel (peak plus a margin), e.g. "32g".
    motis_serve_mem_limit: str = ""
    motis_build_mem_limit: str = ""

    # JWT-based auth (step 3+)
    jwt_secret: str = "change-me-in-prod-use-32-bytes-random"  # noqa: S105 — placeholder default; .env JWT_SECRET MUST override in any non-dev environment
    jwt_alg: str = "HS256"
    jwt_ttl_seconds: int = 12 * 3600  # 12 h
    jwt_cookie_name: str = "viator_jwt"
    jwt_cookie_secure: bool = False  # set True behind TLS

    # First-platform-admin bootstrap. Empty disables the bootstrap endpoint.
    bootstrap_token: str = ""

    # Used to build magic-link URLs in confirmation / password-reset emails.
    public_base_url: str = "http://localhost:8000"

    # Structured logging — see app.logging_config.setup_logging.
    # log_format=json emits JSON to stdout (production / containers);
    # log_format=console emits human-readable colored output (local dev).
    log_level: str = "INFO"
    log_format: str = "json"

    # Build/release version surfaced in the UI header and /healthz/version.
    # Baked into the web image at build time via a Docker `ARG VIATOR_VERSION`
    # promoted to ENV (see docker/web/Dockerfile). The GHA workflow sets it
    # from the git tag (e.g. `v0.1.8`); local `docker compose build` passes
    # the value of the host-side `.env` `VIATOR_VERSION` — see compose `args:`.
    # If the env var is missing entirely (running tests, bare `python -m app`),
    # default `"dev"` keeps the badge non-blank.
    viator_version: str = "dev"

    # Multimodal Station Mapping module (MSMM) — the journey typeahead's
    # station source, see app/station_module.py. Both empty (the default):
    # the module is not used and the typeahead serves VIATOR's own
    # master_stations list. The URL is the module's in-network address,
    # `http://msmm-web:8000`; the token is the module's MSMM_INTERNAL_TOKEN,
    # generated on the server and kept in docker/.env only. repr=False keeps
    # it out of every repr of the settings, and SecretStr out of any dump of
    # them: app/station_module.py reads it with get_secret_value() only when it
    # builds the Authorization header. No `MSMM_` prefix on purpose:
    # the module refuses any unknown `MSMM_` name in its own environment.
    station_module_url: str = ""
    station_module_token: SecretStr = Field(default=SecretStr(""), repr=False)

    @field_validator("otp_serve_mem_headroom_gb", "otp_build_mem_headroom_gb", mode="before")
    @classmethod
    def _tolerant_headroom(cls, value: Any, info: Any) -> Any:
        """A headroom typo in `.env` must not stop web or the worker.

        Accepts an integer, or one followed by "g" ("8g" is read as 8); any
        other value is logged and replaced by the field's default.
        """
        return headroom_gb_or_default(value, _HEADROOM_DEFAULTS[info.field_name], info.field_name)

    class Config:
        env_file = ".env"


settings = Settings()
