"""Process-level settings — env-driven, not user-editable.

User-editable runtime config (SMTP, concurrency limits, registration policy,
retention) lives in `platform_config` and is managed via the admin UI — see
`app.config_schema`.

Anything that needs to change without redeploying belongs in `platform_config`.
Anything that's an infrastructure invariant (DB URL, JWT secret, paths) belongs here.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Persistence
    database_url: str = "postgresql+psycopg://otp:otp@postgres:5432/otp"
    inbox_dir: Path = Path("/data/inbox")
    graph_dir: Path = Path("/data/graphs")

    # Phase-1 upload UI (HTTP basic auth — kept until journey UI lands)
    admin_user: str = "admin"
    admin_password: str = "admin"  # noqa: S105 — pydantic-settings default; operator MUST override via .env in prod
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

    class Config:
        env_file = ".env"


settings = Settings()
