"""Declarative base + shared column conventions."""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Naming convention so Alembic-generated constraint names are stable across runs.
# Without this, every autogenerate produces noise from different default names.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    # Convenience: every concrete model can opt in to created_at via TimestampMixin below.

    type_annotation_map: ClassVar[dict[type, Any]] = {}


class TimestampMixin:
    """`created_at` populated by Postgres' clock at insert.

    Models that need this can multiple-inherit from `(TimestampMixin, Base)`.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
