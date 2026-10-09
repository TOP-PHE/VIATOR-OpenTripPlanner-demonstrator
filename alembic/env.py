"""Alembic migration environment.

Resolves the database URL from app settings (which read from env), and exposes
the project's `Base.metadata` so future autogenerate revisions see all models.
"""

from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

# Import every model module so Base.metadata is fully populated.
# Importing app.models triggers the package __init__ which imports all submodules.
from app.models import Base
from app.settings import settings

config = context.config

# disable_existing_loggers=False: the default (True) disables every logger that
# already exists when this runs. The container runs `alembic upgrade head` in a
# process of its own, but the integration tests run it inside the pytest
# process, where that silenced every `app.*` logger for every later test and let
# "this line is not logged" assertions pass for the wrong reason (#337).
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Inject the runtime DATABASE_URL — never store it in alembic.ini.
config.set_main_option("sqlalchemy.url", settings.database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Generate SQL scripts without a live DB connection (used for review/CI dry-runs)."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations against a live DB connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
