"""Tests for the demo-data seeder (BI/analytics agent test fixture data).

Split in two groups, per the module's own split:

- Pure generation tests: no I/O, no database — exercise
  ``generate_demo_dataset`` / ``generate_clients`` / ``generate_orders``
  directly and assert shape, determinism, and arithmetic integrity.
- Persistence tests: use an in-memory sqlite engine (same pattern as
  ``test_sync_clients.py``) to exercise ``seed_database`` — insert,
  idempotent re-run, and integrity after a round-trip through the ORM.
"""

from __future__ import annotations

import random
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from agentsys.models.base import Base
from agentsys.models.tables import Client, Order, OrderItem
from agentsys.services.seed_data import (
    BUSINESS_TYPES,
    ORDER_STATUSES,
    ZONES,
    generate_clients,
    generate_demo_dataset,
    generate_orders,
    seed_database,
)

# ---------------------------------------------------------------------------
# Pure generation — shape
# ---------------------------------------------------------------------------


def test_generate_clients_returns_requested_count() -> None:
    clients = generate_clients(random.Random(1), count=30)
    assert len(clients) == 30


def test_generate_clients_use_known_zones_and_business_types() -> None:
    clients = generate_clients(random.Random(1), count=40)
    assert {c.zone for c in clients} <= set(ZONES)
    assert {c.business_type for c in clients} <= set(BUSINESS_TYPES)


def test_generate_clients_have_unique_external_ids() -> None:
    clients = generate_clients(random.Random(1), count=40)
    external_ids = [c.external_id for c in clients]
    assert len(external_ids) == len(set(external_ids))


def test_generate_clients_phone_numbers_are_digit_only_and_13_chars() -> None:
    clients = generate_clients(random.Random(1), count=10)
    for c in clients:
        assert c.phone_number.isdigit()
        assert len(c.phone_number) == 13
        assert c.phone_number.startswith("549")


def test_generate_orders_item_count_within_one_to_six() -> None:
    clients = generate_clients(random.Random(1), count=10)
    orders = generate_orders(random.Random(2), clients, count=100)
    for order in orders:
        assert 1 <= len(order.items) <= 6


def test_generate_orders_status_is_a_known_value() -> None:
    clients = generate_clients(random.Random(1), count=10)
    orders = generate_orders(random.Random(2), clients, count=100)
    for order in orders:
        assert order.status in ORDER_STATUSES


def test_generate_orders_created_at_within_trailing_12_months() -> None:
    from agentsys.services.seed_data import ANCHOR_NOW

    clients = generate_clients(random.Random(1), count=10)
    orders = generate_orders(random.Random(2), clients, count=200, anchor_now=ANCHOR_NOW)
    for order in orders:
        age_days = (ANCHOR_NOW - order.created_at).days
        assert 0 <= age_days <= 365


def test_generate_orders_have_unique_external_ids() -> None:
    clients = generate_clients(random.Random(1), count=10)
    orders = generate_orders(random.Random(2), clients, count=150)
    external_ids = [o.external_id for o in orders]
    assert len(external_ids) == len(set(external_ids))


def test_generate_orders_returns_empty_when_no_clients() -> None:
    orders = generate_orders(random.Random(2), clients=(), count=50)
    assert orders == ()


# ---------------------------------------------------------------------------
# Pure generation — arithmetic integrity (the non-negotiable invariant)
# ---------------------------------------------------------------------------


def test_order_item_subtotal_equals_quantity_times_unit_price() -> None:
    clients = generate_clients(random.Random(1), count=15)
    orders = generate_orders(random.Random(2), clients, count=150)
    checked = 0
    for order in orders:
        for item in order.items:
            assert item.subtotal == item.quantity * item.unit_price
            checked += 1
    assert checked > 0


def test_order_total_amount_equals_sum_of_item_subtotals() -> None:
    clients = generate_clients(random.Random(1), count=15)
    orders = generate_orders(random.Random(2), clients, count=150)
    for order in orders:
        expected = sum((i.subtotal for i in order.items), start=Decimal("0.00"))
        assert order.total_amount == expected


# ---------------------------------------------------------------------------
# Pure generation — determinism (fixed seed => identical output)
# ---------------------------------------------------------------------------


def test_generate_clients_deterministic_for_same_seed() -> None:
    a = generate_clients(random.Random(42), count=25)
    b = generate_clients(random.Random(42), count=25)
    assert a == b


def test_generate_demo_dataset_deterministic_for_same_seed() -> None:
    a = generate_demo_dataset(seed=123, num_clients=20, num_orders=80)
    b = generate_demo_dataset(seed=123, num_clients=20, num_orders=80)
    assert a == b


def test_generate_demo_dataset_default_seed_is_reproducible() -> None:
    """The script relies on the *default* seed being stable across calls —
    this is what makes two separate ``uv run python scripts/seed_demo_data.py``
    invocations idempotent."""
    a = generate_demo_dataset()
    b = generate_demo_dataset()
    assert a == b


def test_generate_demo_dataset_default_counts_within_spec_range() -> None:
    dataset = generate_demo_dataset()
    assert 25 <= len(dataset.clients) <= 40
    assert 200 <= len(dataset.orders) <= 400


# ---------------------------------------------------------------------------
# Persistence — in-memory sqlite, no live database (mirrors test_sync_clients.py)
# ---------------------------------------------------------------------------


@pytest.fixture
async def db_engine():
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


def _factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def test_seed_database_inserts_clients_and_orders(db_engine: AsyncEngine) -> None:
    dataset = generate_demo_dataset(seed=7, num_clients=5, num_orders=12)

    async with _factory(db_engine)() as session:
        result = await seed_database(session, dataset)

    assert result.clients_inserted == 5
    assert result.clients_updated == 0
    assert result.orders_inserted == 12
    assert result.orders_updated == 0

    async with _factory(db_engine)() as session:
        clients = (await session.execute(select(Client))).scalars().all()
        orders = (await session.execute(select(Order))).scalars().all()
        items = (await session.execute(select(OrderItem))).scalars().all()
        assert len(clients) == 5
        assert len(orders) == 12
        assert len(items) == sum(len(o.items) for o in dataset.orders)


async def test_seed_database_is_idempotent_no_duplicates(db_engine: AsyncEngine) -> None:
    dataset = generate_demo_dataset(seed=7, num_clients=5, num_orders=12)

    async with _factory(db_engine)() as session:
        await seed_database(session, dataset)

    async with _factory(db_engine)() as session:
        second = await seed_database(session, dataset)

    assert second.clients_inserted == 0
    assert second.clients_updated == 5
    assert second.orders_inserted == 0
    assert second.orders_updated == 12

    async with _factory(db_engine)() as session:
        clients = (await session.execute(select(Client))).scalars().all()
        orders = (await session.execute(select(Order))).scalars().all()
        items = (await session.execute(select(OrderItem))).scalars().all()
        assert len(clients) == 5
        assert len(orders) == 12
        # Items must not double up on the second run.
        assert len(items) == sum(len(o.items) for o in dataset.orders)


async def test_seed_database_persisted_totals_match_summed_items(
    db_engine: AsyncEngine,
) -> None:
    dataset = generate_demo_dataset(seed=7, num_clients=5, num_orders=12)

    async with _factory(db_engine)() as session:
        await seed_database(session, dataset)

    async with _factory(db_engine)() as session:
        orders = (await session.execute(select(Order))).scalars().all()
        for order in orders:
            items = (
                (
                    await session.execute(
                        select(OrderItem).where(OrderItem.order_id == order.id)
                    )
                )
                .scalars()
                .all()
            )
            summed = sum(item.subtotal for item in items)
            assert order.total_amount == pytest.approx(summed, rel=1e-9)
