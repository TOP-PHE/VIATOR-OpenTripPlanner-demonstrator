from pathlib import Path
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://otp:otp@postgres:5432/otp"
    inbox_dir: Path = Path("/data/inbox")
    graph_dir: Path = Path("/data/graphs")
    max_upload_mb: int = 2048
    admin_user: str = "admin"
    admin_password: str = "admin"
    debounce_seconds: int = 1800
    otp_build_heap: str = "12g"

    class Config:
        env_file = ".env"


settings = Settings()
