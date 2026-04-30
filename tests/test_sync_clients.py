"""Tests for the clients sync pipeline (medallion → clients)."""

from __future__ import annotations

import pytest
from sqlalchemy import (
    Boolean,
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

from badie.models.base import Base
from badie.models.tables import Client
from badie.services.sync_clients import sync_clients


# ---------------------------------------------------------------------------
# Fixtures — simulate medallion's gold.dim_cliente with a plain table
# ---------------------------------------------------------------------------


medallion_metadata = MetaData()

dim_cliente = Table(
    "dim_cliente",
    medallion_metadata,
    Column("id_cliente", Integer, primary_key=True),
    Column("razon_social", String(150), nullable=False),
    Column("fantasia", String(150)),
    Column("telefono_movil", String(50)),
    Column("id_lista_precio", Integer),
    Column("des_canal_mkt", String(100)),
    Column("des_localidad", String(100)),
    Column("anulado", Boolean, default=False),
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


async def _seed(engine: AsyncEngine, rows: list[dict]) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        for row in rows:
            await session.execute(insert(dim_cliente).values(**row))
        await session.commit()


def _factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_sync_clients_inserts_new(
    medallion_engine: AsyncEngine, bot_engine: AsyncEngine
) -> None:
    await _seed(
        medallion_engine,
        [
            {"id_cliente": 100, "razon_social": "Kiosco Don José SRL",
             "fantasia": "Kiosco Don José", "telefono_movil": "11-2345-6789",
             "id_lista_precio": 1, "des_canal_mkt": "KIOSCO", "des_localidad": "CABA",
             "anulado": False},
            {"id_cliente": 200, "razon_social": "Almacen El Bajo",
             "fantasia": None, "telefono_movil": "+5491155556666",
             "id_lista_precio": 2, "des_canal_mkt": "ALMACEN", "des_localidad": "AVELLANEDA",
             "anulado": False},
        ],
    )

    async with _factory(medallion_engine)() as msess, _factory(bot_engine)() as bsess:
        result = await sync_clients(msess, bsess)

    assert result.processed == 2
    assert result.errors == 0

    async with _factory(bot_engine)() as bsess:
        rows = (await bsess.execute(select(Client).order_by(Client.external_id))).scalars().all()
        assert len(rows) == 2
        assert rows[0].external_id == 100
        assert rows[0].phone_number == "+5491123456789"
        assert rows[0].name == "Kiosco Don José"  # uses fantasia
        assert rows[0].active is True
        assert rows[0].price_list_id == 1
        assert rows[1].name == "Almacen El Bajo"  # falls back to razon_social
        assert rows[1].phone_number == "+5491155556666"


async def test_sync_clients_skips_anulado(
    medallion_engine: AsyncEngine, bot_engine: AsyncEngine
) -> None:
    await _seed(
        medallion_engine,
        [
            {"id_cliente": 1, "razon_social": "Activo", "fantasia": None,
             "telefono_movil": "11-2345-6789", "id_lista_precio": 1,
             "des_canal_mkt": None, "des_localidad": None, "anulado": False},
            {"id_cliente": 2, "razon_social": "Anulado", "fantasia": None,
             "telefono_movil": "11-9999-9999", "id_lista_precio": 1,
             "des_canal_mkt": None, "des_localidad": None, "anulado": True},
        ],
    )

    async with _factory(medallion_engine)() as msess, _factory(bot_engine)() as bsess:
        result = await sync_clients(msess, bsess)

    assert result.processed == 1

    async with _factory(bot_engine)() as bsess:
        rows = (await bsess.execute(select(Client))).scalars().all()
        assert len(rows) == 1
        assert rows[0].external_id == 1


async def test_sync_clients_skips_invalid_phone(
    medallion_engine: AsyncEngine, bot_engine: AsyncEngine
) -> None:
    await _seed(
        medallion_engine,
        [
            {"id_cliente": 1, "razon_social": "Sin telefono", "fantasia": None,
             "telefono_movil": None, "id_lista_precio": 1,
             "des_canal_mkt": None, "des_localidad": None, "anulado": False},
            {"id_cliente": 2, "razon_social": "Telefono basura", "fantasia": None,
             "telefono_movil": "abc", "id_lista_precio": 1,
             "des_canal_mkt": None, "des_localidad": None, "anulado": False},
            {"id_cliente": 3, "razon_social": "OK", "fantasia": None,
             "telefono_movil": "+5491123456789", "id_lista_precio": 1,
             "des_canal_mkt": None, "des_localidad": None, "anulado": False},
        ],
    )

    async with _factory(medallion_engine)() as msess, _factory(bot_engine)() as bsess:
        result = await sync_clients(msess, bsess)

    assert result.processed == 1
    assert result.errors == 2

    async with _factory(bot_engine)() as bsess:
        rows = (await bsess.execute(select(Client))).scalars().all()
        assert len(rows) == 1
        assert rows[0].external_id == 3


async def test_sync_clients_updates_existing(
    medallion_engine: AsyncEngine, bot_engine: AsyncEngine
) -> None:
    # Seed an existing client with stale data
    async with _factory(bot_engine)() as bsess:
        bsess.add(
            Client(
                external_id=42,
                phone_number="+5491100000000",
                name="STALE NAME",
                active=False,
                price_list_id=99,
            )
        )
        await bsess.commit()

    await _seed(
        medallion_engine,
        [
            {"id_cliente": 42, "razon_social": "Cliente Real SRL",
             "fantasia": "Cliente Real", "telefono_movil": "+5491123456789",
             "id_lista_precio": 3, "des_canal_mkt": None, "des_localidad": None,
             "anulado": False},
        ],
    )

    async with _factory(medallion_engine)() as msess, _factory(bot_engine)() as bsess:
        result = await sync_clients(msess, bsess)

    assert result.processed == 1

    async with _factory(bot_engine)() as bsess:
        rows = (await bsess.execute(select(Client))).scalars().all()
        assert len(rows) == 1  # NOT duplicated
        assert rows[0].external_id == 42
        assert rows[0].name == "Cliente Real"
        assert rows[0].phone_number == "+5491123456789"
        assert rows[0].active is True
        assert rows[0].price_list_id == 3
