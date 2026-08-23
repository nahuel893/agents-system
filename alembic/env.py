"""Alembic async environment for agentsys migrations.

The database URL comes from the application's own ``Settings`` — the same
composed ``database_url`` the app connects with — not from ``alembic.ini``.
Migrating a server must not require editing a versioned file, and a checked-in
``sqlalchemy.url`` is a standing invitation to commit production credentials.

``DATABASE_URL`` in the environment overrides it, which is how the migration
integration tests point alembic at a throwaway database.
"""

# Make `agentsys` importable before any agentsys import below. This block must
# run at module level, above those imports, so E402/E401 are silenced there
# rather than reordered away.
import sys  # noqa: I001
from pathlib import Path

SRC_PATH = Path(__file__).resolve().parents[1] / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

import os  # noqa: E402
from logging.config import fileConfig  # noqa: E402

from alembic import context  # noqa: E402
from sqlalchemy import pool  # noqa: E402
from sqlalchemy.engine import Connection  # noqa: E402
from sqlalchemy.ext.asyncio import async_engine_from_config  # noqa: E402

# Importing the package (not a submodule) registers EVERY table in
# Base.metadata. Autogenerate diffs against this metadata, so a partial
# inventory would make it propose dropping the tables it could not see.
from agentsys.config import get_settings  # noqa: E402
from agentsys.models import Base  # noqa: E402

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _database_url() -> str:
    """Resolve the target database URL.

    Precedence: ``DATABASE_URL`` in the environment, then the application's
    composed ``Settings.database_url``. ``alembic.ini`` is deliberately not
    consulted — see the module docstring.
    """
    return os.environ.get("DATABASE_URL") or get_settings().database_url


# Feed the resolved URL back into the Alembic config so async_engine_from_config
# below builds the engine against it.
config.set_main_option("sqlalchemy.url", _database_url())

# target_metadata for autogenerate support
target_metadata = Base.metadata


# --- offline mode ---


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    context.configure(
        url=_database_url(),
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
    await connectable.dispose()


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
