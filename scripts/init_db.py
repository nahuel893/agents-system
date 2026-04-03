"""Initialize database: create pgvector extension and all tables."""

import asyncio

from sqlalchemy import text

from badie.config import get_settings
from badie.models import Base
from badie.models.base import get_engine


async def init_db() -> None:
    """Create the pgvector extension and all ORM tables."""
    settings = get_settings()
    engine = get_engine(settings.database_url)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()
    print("Database initialized successfully.")


if __name__ == "__main__":
    asyncio.run(init_db())
