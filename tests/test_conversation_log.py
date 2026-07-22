"""Tests for services.conversation_log — ConversationLog audit writer (D-014
S4, design AD-6).

Uses a real SQLite in-memory session (same pattern as test_client_lookup.py)
so ConversationLog rows and column constraints are exercised for real — no
mocking of the ORM layer.
"""
from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from agentsys.models.base import Base
from agentsys.models.tables import Client, ConversationLog
from agentsys.services.conversation_log import log_conversation_turn


@pytest.fixture
async def db_session() -> Any:
    """SQLite async in-memory session with tables created."""
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session

    await engine.dispose()


async def test_log_conversation_turn_writes_user_and_assistant_rows(
    db_session: AsyncSession,
) -> None:
    """One completed turn writes one 'user' row and one 'assistant' row."""
    client = Client(phone_number="+5491100000000", name="Kiosco Test", active=True)
    db_session.add(client)
    await db_session.commit()
    await db_session.refresh(client)

    await log_conversation_turn(
        db_session,
        thread_id="+5491100000000",
        client_id=client.id,
        user_text="Hola, tenes azucar?",
        assistant_text="Si, tenemos azucar 1kg.",
    )
    await db_session.commit()

    result = await db_session.execute(
        select(ConversationLog).where(ConversationLog.thread_id == "+5491100000000")
    )
    rows = result.scalars().all()

    assert len(rows) == 2
    roles = {row.role for row in rows}
    assert roles == {"user", "assistant"}

    user_row = next(r for r in rows if r.role == "user")
    assistant_row = next(r for r in rows if r.role == "assistant")
    assert user_row.content == "Hola, tenes azucar?"
    assert assistant_row.content == "Si, tenemos azucar 1kg."
    assert user_row.client_id == client.id
    assert assistant_row.client_id == client.id


async def test_log_conversation_turn_absent_metadata_persists_null(
    db_session: AsyncSession,
) -> None:
    """Absent tokens_used/model_used persist as NULL rather than raising
    (spec: 'Best-effort token/model metadata absent')."""
    await log_conversation_turn(
        db_session,
        thread_id="+5491100000001",
        client_id=None,
        user_text="Hola",
        assistant_text="Hola! Como puedo ayudarte?",
    )
    await db_session.commit()

    result = await db_session.execute(
        select(ConversationLog).where(ConversationLog.thread_id == "+5491100000001")
    )
    rows = result.scalars().all()

    assert len(rows) == 2
    for row in rows:
        assert row.tokens_used is None
        assert row.client_id is None
    assistant_row = next(r for r in rows if r.role == "assistant")
    assert assistant_row.model_used is None


async def test_log_conversation_turn_persists_provided_metadata(
    db_session: AsyncSession,
) -> None:
    """When model_used/tokens_used ARE available, they are persisted on the
    assistant row."""
    await log_conversation_turn(
        db_session,
        thread_id="+5491100000002",
        client_id=None,
        user_text="Hola",
        assistant_text="Hola!",
        model_used="claude-3-5-haiku-latest",
        tokens_used=42,
    )
    await db_session.commit()

    result = await db_session.execute(
        select(ConversationLog).where(ConversationLog.thread_id == "+5491100000002")
    )
    rows = result.scalars().all()
    assistant_row = next(r for r in rows if r.role == "assistant")
    assert assistant_row.model_used == "claude-3-5-haiku-latest"
    assert assistant_row.tokens_used == 42


async def test_log_conversation_turn_does_not_commit(db_session: AsyncSession) -> None:
    """The function only adds rows — the caller owns the commit (design AD-6:
    webhook-owned session/transaction)."""
    await log_conversation_turn(
        db_session,
        thread_id="+5491100000003",
        client_id=None,
        user_text="Hola",
        assistant_text="Hola!",
    )
    # No commit yet — rollback should discard both rows.
    await db_session.rollback()

    result = await db_session.execute(
        select(ConversationLog).where(ConversationLog.thread_id == "+5491100000003")
    )
    assert result.scalars().all() == []
