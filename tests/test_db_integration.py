"""Integration smoke test for database connectivity and pgvector extension.

Requires a live Postgres instance with the pgvector extension installed.
Skipped by default — opt-in with::

    uv run pytest -m integration -v

Prerequisites (run once before this test):

    docker compose up -d
    uv run python scripts/init_db.py
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from agentsys.config import get_settings
from agentsys.models.base import get_db, get_engine, get_session_factory


@pytest.mark.integration
async def test_db_connectivity_and_pgvector() -> None:
    """Verify DB is reachable and the vector extension is installed.

    Asserts:
    - ``SELECT 1`` returns 1 (basic connectivity).
    - ``pg_extension`` contains an entry for ``vector`` (pgvector present).
    """
    settings = get_settings()
    engine = get_engine(settings.database_url)
    try:
        factory = get_session_factory(engine)
        async with get_db(factory) as session:
            scalar = (await session.execute(text("SELECT 1"))).scalar()
            assert scalar == 1, f"Expected SELECT 1 == 1, got {scalar!r}"

            ext = await session.execute(
                text("SELECT extname FROM pg_extension WHERE extname='vector'")
            )
            extname = ext.scalar()
            assert extname == "vector", (
                "pgvector extension not found; run 'uv run python scripts/init_db.py' first"
            )
    finally:
        await engine.dispose()
