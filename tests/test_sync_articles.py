"""Tests for the catalog sync pipeline (medallion → catalog_embeddings)."""

from __future__ import annotations

import pytest
from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    insert,
    select,
)
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from agentsys.models.base import Base
from agentsys.models.tables import CatalogEmbedding
from agentsys.services.embeddings import FakeEmbeddingProvider
from agentsys.services.sync_articles import sync_articles


# ---------------------------------------------------------------------------
# Fixtures — simulate medallion's gold.dim_articulo with a plain table
# ---------------------------------------------------------------------------


medallion_metadata = MetaData()

dim_articulo = Table(
    "dim_articulo",
    medallion_metadata,
    Column("id_articulo", Integer, primary_key=True),
    Column("des_articulo", String(200), nullable=False),
    Column("marca", String(150)),
    Column("generico", String(150)),
    Column("calibre", String(150)),
)


@pytest.fixture
async def medallion_engine():
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(medallion_metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def bot_engine():
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


async def _seed_medallion(engine: AsyncEngine, rows: list[dict]) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        for row in rows:
            await session.execute(insert(dim_articulo).values(**row))
        await session.commit()


def _med_session(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


def _bot_session(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_sync_articles_inserts_new(
    medallion_engine: AsyncEngine, bot_engine: AsyncEngine
) -> None:
    await _seed_medallion(
        medallion_engine,
        [
            {"id_articulo": 1, "des_articulo": "Quilmes Cristal 1L",
             "marca": "QUILMES", "generico": "CERVEZAS", "calibre": "RETORNABLE 1000"},
            {"id_articulo": 2, "des_articulo": "Brahma Lata 473",
             "marca": "BRAHMA", "generico": "CERVEZAS", "calibre": "LATA 473"},
            {"id_articulo": 3, "des_articulo": "Coca-Cola 2.25L",
             "marca": "COCA COLA", "generico": "GASEOSAS", "calibre": "DESC 2250"},
        ],
    )

    embedder = FakeEmbeddingProvider(dimensions=512)
    async with _med_session(medallion_engine)() as msess, _bot_session(bot_engine)() as bsess:
        result = await sync_articles(msess, bsess, embedder, batch_size=10)

    assert result.processed == 3
    assert result.errors == 0

    async with _bot_session(bot_engine)() as bsess:
        rows = (await bsess.execute(select(CatalogEmbedding))).scalars().all()
        assert len(rows) == 3
        skus = {r.sku for r in rows}
        assert skus == {"1", "2", "3"}


async def test_sync_articles_updates_existing(
    medallion_engine: AsyncEngine, bot_engine: AsyncEngine
) -> None:
    # Seed an existing row in catalog_embeddings
    embedder = FakeEmbeddingProvider(dimensions=512)
    async with _bot_session(bot_engine)() as bsess:
        bsess.add(
            CatalogEmbedding(
                sku="1",
                description="OLD DESCRIPTION",
                embedding=[0.0] * 512,
                active=True,
            )
        )
        await bsess.commit()

    await _seed_medallion(
        medallion_engine,
        [
            {"id_articulo": 1, "des_articulo": "Quilmes Cristal 1L",
             "marca": "QUILMES", "generico": "CERVEZAS", "calibre": "RETORNABLE 1000"},
        ],
    )

    async with _med_session(medallion_engine)() as msess, _bot_session(bot_engine)() as bsess:
        result = await sync_articles(msess, bsess, embedder, batch_size=10)

    assert result.processed == 1

    async with _bot_session(bot_engine)() as bsess:
        rows = (await bsess.execute(select(CatalogEmbedding))).scalars().all()
        assert len(rows) == 1  # NOT duplicated
        assert "Quilmes Cristal 1L" in rows[0].description
        assert rows[0].description != "OLD DESCRIPTION"


async def test_sync_articles_skips_empty_description(
    medallion_engine: AsyncEngine, bot_engine: AsyncEngine
) -> None:
    await _seed_medallion(
        medallion_engine,
        [
            {"id_articulo": 1, "des_articulo": "Quilmes 1L", "marca": "Q", "generico": "C", "calibre": "R"},
            {"id_articulo": 2, "des_articulo": "", "marca": None, "generico": None, "calibre": None},
        ],
    )

    embedder = FakeEmbeddingProvider(dimensions=512)
    async with _med_session(medallion_engine)() as msess, _bot_session(bot_engine)() as bsess:
        result = await sync_articles(msess, bsess, embedder, batch_size=10)

    assert result.processed == 1
    assert result.errors == 1

    async with _bot_session(bot_engine)() as bsess:
        rows = (await bsess.execute(select(CatalogEmbedding))).scalars().all()
        assert len(rows) == 1
        assert rows[0].sku == "1"


async def test_sync_articles_batches(
    medallion_engine: AsyncEngine, bot_engine: AsyncEngine
) -> None:
    """With batch_size=2 and 5 articles, embedder is called 3 times (2+2+1)."""
    rows = [
        {"id_articulo": i, "des_articulo": f"Articulo {i}", "marca": "M", "generico": "G", "calibre": "C"}
        for i in range(1, 6)
    ]
    await _seed_medallion(medallion_engine, rows)

    call_count = 0
    base_embedder = FakeEmbeddingProvider(dimensions=512)

    class CountingEmbedder:
        async def embed(self, texts: list[str]) -> list[list[float]]:
            nonlocal call_count
            call_count += 1
            return await base_embedder.embed(texts)

    async with _med_session(medallion_engine)() as msess, _bot_session(bot_engine)() as bsess:
        result = await sync_articles(msess, bsess, CountingEmbedder(), batch_size=2)

    assert result.processed == 5
    assert call_count == 3  # 2 + 2 + 1
