"""Runner script: sync gold.dim_articulo → catalog_embeddings.

Usage::

    uv run python scripts/sync_articles.py

Reads connection URLs and embedding provider config from .env via Settings.
Defaults to ``embedding_provider="local"`` (BGE-M3, no API key required).
Set ``EMBEDDING_PROVIDER=openai`` + ``OPENAI_API_KEY=sk-...`` to use the cloud.
"""

from __future__ import annotations

import asyncio
import sys

import structlog
from sqlalchemy.ext.asyncio import async_sessionmaker

from badie.config import get_settings
from badie.models.base import get_engine
from badie.observability import setup_logging
from badie.services.embeddings import get_embedding_provider
from badie.services.medallion import get_medallion_engine
from badie.services.sync_articles import sync_articles


async def main() -> int:
    setup_logging()
    logger = structlog.get_logger()
    settings = get_settings()

    logger.info("sync_articles.provider_selected", provider=settings.embedding_provider)
    embedder = get_embedding_provider(settings)

    medallion_engine = get_medallion_engine(settings.medallion_database_url)
    bot_engine = get_engine(settings.database_url)

    med_factory = async_sessionmaker(medallion_engine, expire_on_commit=False)
    bot_factory = async_sessionmaker(bot_engine, expire_on_commit=False)

    try:
        async with med_factory() as msess, bot_factory() as bsess:
            result = await sync_articles(msess, bsess, embedder, batch_size=100)
        logger.info("sync_articles.done", processed=result.processed, errors=result.errors)
        return 0
    finally:
        await medallion_engine.dispose()
        await bot_engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
