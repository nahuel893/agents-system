"""Alembic async environment for agentsystem migrations."""

from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# --- agentsys imports ---
# Build path to src so 'agentsys' can be imported when running migrations
SRC_PATH = Path(__file__).resolve().parents[1] / "src"
import sys
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from agentsys.models.base import Base  # noqa: E402
from agentsys.models.audit_event import AuditEvent  # noqa: E402

# target_metadata for autogenerate support
target_metadata = Base.metadata

# --- partition creation helper (for cron use) ---


def create_audit_event_partition(month: str) -> None:
    """Create a monthly partition for audit_event.

    Args:
        month: Month string in 'YYYY-MM' format, e.g. '2026-10'.

    Raises:
        ValueError: If the month string is invalid.
    """
    from datetime import date
    import re

    if not re.match(r"^\d{4}-\d{2}$", month):
        raise ValueError(f"Invalid month format: {month!r}. Expected 'YYYY-MM'.")

    year, mon = month.split("-")
    year_i, mon_i = int(year), int(mon)

    part_name = f"audit_event_{year_i}_{mon_i:02d}"
    start = date(year_i, mon_i, 1)
    if mon_i == 12:
        end = date(year_i + 1, 1, 1)
    else:
        end = date(year_i, mon_i + 1, 1)

    op.execute(f"""
        CREATE TABLE IF NOT EXISTS {part_name} PARTITION OF audit_event
          FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}')
    """)


# --- offline mode ---


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


# --- online mode (async) ---


async def run_migrations_online_async() -> None:
    """Run migrations in async 'online' mode."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
        await connection.commit()


def do_run_migrations(connection: Connection) -> None:
    """Configure context and run migrations (sync helper for async)."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    import asyncio
    asyncio.run(run_migrations_online_async())
