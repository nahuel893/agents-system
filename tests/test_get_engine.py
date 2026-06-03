"""Unit tests for get_engine() configuration.

Verifies that the engine is constructed with pool_pre_ping=True so stale
connections are detected and recycled before being handed to application code.
"""

from __future__ import annotations

import pytest

from agentsys.models.base import get_engine


@pytest.mark.asyncio
async def test_get_engine_has_pool_pre_ping_enabled() -> None:
    """get_engine must enable pool_pre_ping so stale connections are recycled.

    SQLAlchemy 2.x: pool._pre_ping reflects the pool_pre_ping kwarg passed
    to create_async_engine. Both StaticPool (in-memory sqlite) and
    AsyncAdaptedQueuePool expose this attribute.
    """
    engine = get_engine("sqlite+aiosqlite:///test_pre_ping.db")
    try:
        assert engine.pool._pre_ping is True, (
            "get_engine() must pass pool_pre_ping=True to create_async_engine; "
            f"got pool._pre_ping={engine.pool._pre_ping!r}"
        )
    finally:
        await engine.dispose()
