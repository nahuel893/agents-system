"""Initialize database: create pgvector extension and the ORM-owned tables.

Alembic-owned tables are deliberately NOT created here — see
``agentsys.models.base.create_orm_owned_tables``. ``audit_event`` is RANGE
partitioned, and SQLAlchemy cannot express that, so creating it from metadata
would produce a plain unpartitioned table with no error and no warning. Run
``alembic upgrade head`` for those.
"""

import asyncio

from sqlalchemy import text

from agentsys.config import get_settings

# Imported from the package, not from `models.base`, so the full table
# inventory is registered before create_all runs — see agentsys/models/__init__.
from agentsys.models import create_orm_owned_tables, get_engine


async def init_db() -> None:
    """Create the pgvector extension and every ORM-owned table."""
    settings = get_settings()
    engine = get_engine(settings.database_url)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(create_orm_owned_tables)
    await engine.dispose()
    print("Database initialized successfully.")
    print("Alembic-owned tables were skipped — run: alembic upgrade head")


if __name__ == "__main__":
    asyncio.run(init_db())
