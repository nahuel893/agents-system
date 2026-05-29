"""Runner script: sync gold.dim_cliente → clients.

Usage::

    uv run python scripts/sync_clients.py
"""

from __future__ import annotations

import asyncio
import sys

import structlog
from sqlalchemy.ext.asyncio import async_sessionmaker

from agentsys.config import get_settings
from agentsys.models.base import get_engine
from agentsys.observability import setup_logging
from agentsys.services.medallion import get_medallion_engine
from agentsys.services.sync_clients import sync_clients


async def main() -> int:
    setup_logging()
    logger = structlog.get_logger()
    settings = get_settings()

    medallion_engine = get_medallion_engine(settings.medallion_database_url)
    bot_engine = get_engine(settings.database_url)

    med_factory = async_sessionmaker(medallion_engine, expire_on_commit=False)
    bot_factory = async_sessionmaker(bot_engine, expire_on_commit=False)

    try:
        async with med_factory() as msess, bot_factory() as bsess:
            result = await sync_clients(msess, bsess)
        logger.info("sync_clients.done", processed=result.processed, errors=result.errors)
        return 0
    finally:
        await medallion_engine.dispose()
        await bot_engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
