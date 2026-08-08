"""Runner script: seed realistic demo data for Distribuidora BADIE.

Populates ``clients``, ``orders``, and ``order_items`` with deterministic,
internally-consistent data so a BI/analytics agent has something to query —
right now every analytics table is empty. Idempotent: safe to re-run, matches
existing rows by ``external_id`` instead of duplicating them.

Usage::

    uv run python scripts/seed_demo_data.py
"""

from __future__ import annotations

import asyncio
import sys

import structlog
from sqlalchemy.ext.asyncio import async_sessionmaker

from agentsys.config import get_settings
from agentsys.models.base import get_engine
from agentsys.observability import setup_logging
from agentsys.services.seed_data import generate_demo_dataset, seed_database


async def main() -> int:
    setup_logging()
    logger = structlog.get_logger()
    settings = get_settings()

    engine = get_engine(settings.database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        dataset = generate_demo_dataset()
        async with factory() as session:
            result = await seed_database(session, dataset)
        logger.info(
            "seed_demo_data.done",
            clients_inserted=result.clients_inserted,
            clients_updated=result.clients_updated,
            orders_inserted=result.orders_inserted,
            orders_updated=result.orders_updated,
        )
        return 0
    finally:
        await engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
