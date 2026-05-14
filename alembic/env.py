"""
Alembic migration environment.

Wired to the application's settings (the single source of truth for
DATABASE_URL) and to ``SQLModel.metadata`` as the autogenerate target.

Phase 1 has no migrations until M5, so ``alembic upgrade head`` on the M4
scaffold is a successful no-op against an empty schema.
"""

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from app.core.config import get_settings
from app.db.models import SQLModel  # noqa: F401 -- import populates SQLModel.metadata

# Alembic Config object — provides access to values in alembic.ini.
config = context.config

# Inject the runtime DATABASE_URL so alembic.ini stays secret-free.
config.set_main_option("sqlalchemy.url", get_settings().database_url)

# Configure Python logging from alembic.ini.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Autogenerate compares the live database against this metadata object.
target_metadata = SQLModel.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode — emit SQL without a DB connection."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode — against a live database connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
