"""Tests for client lookup service (normalize_phone + lookup_or_create_client)."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from agentsys.models.base import Base
from agentsys.models.tables import Client
from agentsys.services.clients import lookup_or_create_client, normalize_phone


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db_session():
    """SQLite async in-memory session with tables created."""
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session

    await engine.dispose()


# ---------------------------------------------------------------------------
# Phase 3 — normalize_phone unit tests (tasks 3.1–3.2)
# ---------------------------------------------------------------------------


def test_normalize_phone_without_plus() -> None:
    """Raw phone without '+' gets prefixed."""
    assert normalize_phone("5491123456789") == "+5491123456789"


def test_normalize_phone_already_normalized() -> None:
    """Phone already in E.164 format stays unchanged."""
    assert normalize_phone("+5491123456789") == "+5491123456789"


# ---------------------------------------------------------------------------
# Phase 4 — lookup_or_create_client tests (tasks 4.2–4.4)
# ---------------------------------------------------------------------------


async def test_lookup_existing_client(db_session: AsyncSession) -> None:
    """Known phone returns existing active client."""
    existing = Client(phone_number="+5491155551234", name="Kiosco Don José", active=True)
    db_session.add(existing)
    await db_session.commit()
    await db_session.refresh(existing)

    result = await lookup_or_create_client(db_session, "+5491155551234")
    assert result.id == existing.id
    assert result.active is True
    assert result.name == "Kiosco Don José"


async def test_lookup_creates_unregistered_client(db_session: AsyncSession) -> None:
    """Unknown phone auto-registers with active=False."""
    result = await lookup_or_create_client(db_session, "+5491199998888")

    assert result.id is not None
    assert result.phone_number == "+5491199998888"
    assert result.name == "Pendiente de alta"
    assert result.active is False


async def test_lookup_returns_inactive_on_second_call(db_session: AsyncSession) -> None:
    """Second lookup for same unknown phone returns same client, no duplicate."""
    first = await lookup_or_create_client(db_session, "+5491177776666")
    second = await lookup_or_create_client(db_session, "+5491177776666")

    assert first.id == second.id
    assert second.active is False
