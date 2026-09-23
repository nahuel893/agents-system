"""Integration smoke test for database connectivity.

Requires a live Postgres instance. Skipped by default — opt-in with::

    uv run pytest -m integration -v

Prerequisites (run once before this test):

    docker compose up -d
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from agentsys.config import get_settings
from agentsys.models.base import get_db, get_engine, get_session_factory


@pytest.mark.integration
async def test_db_connectivity() -> None:
    """Verify the database is reachable through the real session factory.

    Asserts ``SELECT 1`` returns 1 (basic connectivity) end-to-end through
    ``get_session_factory`` / ``get_db`` — the wiring ``test_get_engine.py``
    doesn't exercise against a live connection.
    """
    settings = get_settings()
    engine = get_engine(settings.database_url)
    try:
        factory = get_session_factory(engine)
        async with get_db(factory) as session:
            scalar = (await session.execute(text("SELECT 1"))).scalar()
            assert scalar == 1, f"Expected SELECT 1 == 1, got {scalar!r}"
    finally:
        await engine.dispose()
