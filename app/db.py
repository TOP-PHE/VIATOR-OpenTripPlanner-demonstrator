from datetime import datetime
from sqlalchemy import String, Integer, DateTime, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .settings import settings


engine = create_engine(settings.database_url, future=True, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    pass


class Upload(Base):
    __tablename__ = "uploads"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    user: Mapped[str] = mapped_column(String(64))
    filename: Mapped[str] = mapped_column(String(512))
    declared_standard: Mapped[str] = mapped_column(String(64))
    detected_kind: Mapped[str] = mapped_column(String(64))
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer)
    stored_path: Mapped[str] = mapped_column(String(1024))
    version_label: Mapped[str] = mapped_column(String(128), default="")
    triggered_rebuild: Mapped[int] = mapped_column(Integer, default=0)


class RebuildJob(Base):
    __tablename__ = "rebuild_jobs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|running|done|failed
    log: Mapped[str] = mapped_column(Text, default="")
    graph_path: Mapped[str] = mapped_column(String(1024), default="")


def init_db() -> None:
    Base.metadata.create_all(engine)
