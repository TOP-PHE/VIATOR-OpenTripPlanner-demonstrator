"""Database engine + session factory.

Models live in `app.models`. Schema management is owned by Alembic
(`alembic upgrade head`), not by `Base.metadata.create_all()` — this avoids
drift between the ORM definition and the migrations of record.
"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .settings import settings

engine = create_engine(
    settings.database_url,
    future=True,
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    future=True,
)


def get_db() -> Iterator[Session]:
    """FastAPI dependency: yields a SQLAlchemy session and ensures it's closed."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
