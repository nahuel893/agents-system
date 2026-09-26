"""Unit tests for get_engine() configuration.

Verifies that the engine is constructed with pool_pre_ping=True so stale
connections are detected and recycled before being handed to application code.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agents_system.config import Settings
from agents_system.models.base import get_bi_engine, get_engine, get_sql_engine
from agents_system.services.medallion import get_medallion_engine


@pytest.mark.asyncio
async def test_get_engine_has_pool_pre_ping_enabled() -> None:
    """get_engine must enable pool_pre_ping so stale connections are recycled.

    SQLAlchemy 2.x: pool._pre_ping reflects the pool_pre_ping kwarg passed
    to create_async_engine. Both StaticPool (in-memory sqlite) and
    AsyncAdaptedQueuePool expose this attribute.
    """
    engine = get_engine(
        "sqlite+aiosqlite:///test_pre_ping.db", Settings(_env_file=None)
    )
    try:
        assert engine.pool._pre_ping is True, (
            "get_engine() must pass pool_pre_ping=True to create_async_engine; "
            f"got pool._pre_ping={engine.pool._pre_ping!r}"
        )
    finally:
        await engine.dispose()


def test_operational_engine_uses_its_configured_pool_limits() -> None:
    settings = Settings(
        _env_file=None,
        database_pool_size=7,
        database_max_overflow=1,
        max_concurrent_turns=1,
        database_pool_timeout_s=19,
        database_pool_recycle_s=601,
    )

    with patch("agents_system.models.base.create_async_engine") as create_engine:
        get_engine("postgresql+asyncpg://db/operational", settings)

    create_engine.assert_called_once_with(
        "postgresql+asyncpg://db/operational",
        echo=False,
        pool_pre_ping=True,
        pool_size=7,
        max_overflow=1,
        pool_timeout=19,
        pool_recycle=601,
    )


def test_bi_engine_uses_its_configured_pool_limits() -> None:
    settings = Settings(
        _env_file=None,
        bi_pool_size=3,
        bi_max_overflow=1,
        bi_pool_timeout_s=18,
        bi_pool_recycle_s=602,
    )

    with patch("agents_system.models.base.create_async_engine") as create_engine:
        get_bi_engine("postgresql+asyncpg://db/bi", settings)

    create_engine.assert_called_once_with(
        "postgresql+asyncpg://db/bi",
        echo=False,
        pool_pre_ping=True,
        pool_size=3,
        max_overflow=1,
        pool_timeout=18,
        pool_recycle=602,
    )


def test_medallion_engine_uses_its_configured_pool_limits() -> None:
    settings = Settings(
        _env_file=None,
        medallion_pool_size=4,
        medallion_max_overflow=1,
        medallion_pool_timeout_s=17,
        medallion_pool_recycle_s=602,
    )

    with patch("agents_system.services.medallion.create_async_engine") as create_engine:
        get_medallion_engine("postgresql+asyncpg://db/warehouse", settings)

    create_engine.assert_called_once_with(
        "postgresql+asyncpg://db/warehouse",
        echo=False,
        pool_pre_ping=True,
        pool_size=4,
        max_overflow=1,
        pool_timeout=17,
        pool_recycle=602,
    )


def test_sql_tool_engine_uses_its_small_configured_pool_limits() -> None:
    settings = Settings(
        _env_file=None,
        sql_pool_size=2,
        sql_max_overflow=0,
        sql_pool_timeout_s=11,
        sql_pool_recycle_s=603,
    )

    with patch("agents_system.models.base.create_async_engine") as create_engine:
        get_sql_engine("postgresql+asyncpg://db/sql-readonly", settings)

    create_engine.assert_called_once_with(
        "postgresql+asyncpg://db/sql-readonly",
        echo=False,
        pool_pre_ping=True,
        pool_size=2,
        max_overflow=0,
        pool_timeout=11,
        pool_recycle=603,
    )
