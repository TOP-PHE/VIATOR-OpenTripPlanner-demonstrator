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
    admin_password: str = "admin"
    max_upload_mb: int = 2048
    debounce_seconds: int = 1800
    otp_build_heap: str = "12g"

    # JWT-based auth (step 3+)
    jwt_secret: str = "change-me-in-prod-use-32-bytes-random"
    jwt_alg: str = "HS256"
    jwt_ttl_seconds: int = 12 * 3600  # 12 h
    jwt_cookie_name: str = "viator_jwt"
    jwt_cookie_secure: bool = False  # set True behind TLS

    # First-platform-admin bootstrap. Empty disables the bootstrap endpoint.
    bootstrap_token: str = ""

    # Used to build magic-link URLs in confirmation / password-reset emails.
    public_base_url: str = "http://localhost:8000"

    class Config:
        env_file = ".env"


settings = Settings()
