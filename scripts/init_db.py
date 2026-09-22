"""Initialize database: create the pgvector extension.

``audit_event`` is the platform's only ORM-declared table, and it is entirely
Alembic-owned (see ``agentsys.models.base.ALEMBIC_OWNED``): it is RANGE
partitioned, and SQLAlchemy cannot express that, so creating it from metadata
would produce a plain unpartitioned table with no error and no warning. Run
``alembic upgrade head`` for it — this script no longer has any ORM-owned
table left to bulk-create.
"""

import asyncio

from sqlalchemy import text

from agentsys.config import get_settings
from agentsys.models.base import get_engine


async def init_db() -> None:
    """Create the pgvector extension."""
    settings = get_settings()
    engine = get_engine(settings.database_url)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    await engine.dispose()
    print("Database initialized successfully.")
    print("Alembic-owned tables were skipped — run: alembic upgrade head")


if __name__ == "__main__":
    asyncio.run(init_db())
