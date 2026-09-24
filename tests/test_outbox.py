"""Focused unit tests for the durable inbox/outbox storage primitive."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agents_system.models.outbox import InboundMessage, OutboxWork
from agents_system.services.outbox import (
    accept_inbound_message,
    pending_outbox_statement,
)


class _ConstraintError(Exception):
    def __init__(self, constraint_name: str) -> None:
        super().__init__(constraint_name)
        self.constraint_name = constraint_name


class _Session:
    def __init__(self, *, commit_error: Exception | None = None) -> None:
        self.added: list[InboundMessage | OutboxWork] = []
        self.commit_error = commit_error
        self.commit_count = 0
        self.rollback_count = 0
        self.existing_inbound: InboundMessage | None = None

    def add_all(self, rows: list[InboundMessage | OutboxWork]) -> None:
        self.added.extend(rows)

    async def commit(self) -> None:
        self.commit_count += 1
        if self.commit_error is not None:
            raise self.commit_error

    async def rollback(self) -> None:
        self.rollback_count += 1

    async def scalar(self, statement: object) -> InboundMessage | None:
        return self.existing_inbound


class TestInboxOutboxSchema:
    def test_inbound_message_identity_is_unique(self) -> None:
        column = InboundMessage.__table__.c.meta_message_id

        assert column.unique is True
        assert column.nullable is False

    def test_outbox_has_one_work_row_per_inbound_message(self) -> None:
        column = OutboxWork.__table__.c.inbound_message_id

        assert column.unique is True
        assert column.foreign_keys

    def test_persistence_timestamps_use_database_defaults(self) -> None:
        columns = (
            InboundMessage.__table__.c.received_at,
            OutboxWork.__table__.c.enqueued_at,
            OutboxWork.__table__.c.available_at,
        )

        assert all(column.server_default is not None for column in columns)

    def test_pending_statement_excludes_completed_work_and_handles_expired_leases(
        self,
    ) -> None:
        now = datetime.now(UTC)
        statement = pending_outbox_statement(now=now, limit=10)
        sql = str(statement.compile(compile_kwargs={"literal_binds": True}))

        assert "completed_at IS NULL" in sql
        assert "failed_at IS NULL" in sql
        assert "lease_expires_at IS NULL" in sql
        assert "lease_expires_at <" in sql
        assert "FOR UPDATE" in sql

    def test_outbox_records_recoverable_delivery_and_replay_state(self) -> None:
        table = OutboxWork.__table__

        assert table.c.attempt_count.server_default is not None
        assert table.c.lease_owner.nullable is True
        assert table.c.failed_at.nullable is True
        assert table.c.last_error.nullable is True
        assert table.c.outbound_body.nullable is True
        assert table.c.outbound_send_key.nullable is True

    def test_conversation_key_is_required(self) -> None:
        """#46 follow-up (migration 004): required, not backfill-only."""
        column = OutboxWork.__table__.c.conversation_key

        assert column.nullable is False

    def test_pending_statement_excludes_a_row_with_an_older_non_terminal_sibling(
        self,
    ) -> None:
        """#46 follow-up: the per-conversation ordering predicate is a
        correlated NOT EXISTS over conversation_key, gated on the same
        non-terminal condition as the outer row (see
        pending_outbox_statement's docstring for why that -- not lease
        state -- is what makes backoff and live leases both block
        siblings)."""
        now = datetime.now(UTC)
        statement = pending_outbox_statement(now=now, limit=10)
        sql = str(statement.compile(compile_kwargs={"literal_binds": True}))

        assert "NOT (EXISTS" in sql or "NOT EXISTS" in sql
        assert sql.count("conversation_key") >= 2  # outer row + subquery join
        assert sql.count("completed_at IS NULL") >= 2
        assert sql.count("failed_at IS NULL") >= 2


class TestAcceptInboundMessage:
    async def test_new_message_commits_inbound_and_outbox_together(self) -> None:
        session = _Session()

        result = await accept_inbound_message(
            cast(AsyncSession, session),
            meta_message_id="wamid.new-message",
            payload={"text": "hello"},
            conversation_key="5491100000000",
        )

        assert result.duplicate is False
        assert session.commit_count == 1
        assert len(session.added) == 2
        inbound, work = session.added
        assert isinstance(inbound, InboundMessage)
        assert isinstance(work, OutboxWork)
        assert work.inbound_message_id == inbound.id

    async def test_duplicate_message_is_idempotent_after_unique_conflict(self) -> None:
        existing = InboundMessage(
            id=uuid4(),
            meta_message_id="wamid.duplicate",
            payload={"text": "first"},
        )
        wrapped_error = Exception("asyncpg integrity error")
        wrapped_error.__cause__ = _ConstraintError("uq_webhook_inbox_meta_message_id")
        duplicate_error = IntegrityError("INSERT", {}, wrapped_error)
        session = _Session(commit_error=duplicate_error)
        session.existing_inbound = existing

        result = await accept_inbound_message(
            cast(AsyncSession, session),
            meta_message_id="wamid.duplicate",
            payload={"text": "retry"},
            conversation_key="5491100000000",
        )

        assert result.duplicate is True
        assert result.inbound_message_id == existing.id
        assert session.rollback_count == 1

    async def test_unrelated_integrity_error_propagates_despite_matching_inbox(
        self,
    ) -> None:
        existing = InboundMessage(
            id=uuid4(),
            meta_message_id="wamid.unrelated-error",
            payload={"text": "first"},
        )
        unrelated_error = IntegrityError(
            "INSERT",
            {},
            _ConstraintError("uq_outbox_work_inbound_message"),
        )
        session = _Session(commit_error=unrelated_error)
        session.existing_inbound = existing

        with pytest.raises(IntegrityError) as raised:
            await accept_inbound_message(
                cast(AsyncSession, session),
                meta_message_id="wamid.unrelated-error",
                payload={"text": "retry"},
                conversation_key="5491100000000",
            )

        assert raised.value is unrelated_error
        assert session.rollback_count == 1

    async def test_commit_failure_rolls_back_and_does_not_report_acceptance(
        self,
    ) -> None:
        commit_error = RuntimeError("database unavailable")
        session = _Session(commit_error=commit_error)

        with pytest.raises(RuntimeError, match="database unavailable"):
            await accept_inbound_message(
                cast(AsyncSession, session),
                meta_message_id="wamid.commit-failure",
                payload={"text": "hello"},
                conversation_key="5491100000000",
            )

        assert session.rollback_count == 1

    async def test_empty_conversation_key_is_rejected(self) -> None:
        session = _Session()

        with pytest.raises(ValueError, match="conversation_key"):
            await accept_inbound_message(
                cast(AsyncSession, session),
                meta_message_id="wamid.no-key",
                payload={"text": "hello"},
                conversation_key="",
            )

        assert session.commit_count == 0
