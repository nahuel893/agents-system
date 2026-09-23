"""Focused W2a tests for recoverable outbox claim and failure state changes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from agentsys.models.audit_event import AuditEvent
from agentsys.models.outbox import OutboxWork
from agentsys.services.outbox import (
    DEFAULT_LEASE_DURATION,
    MAX_OUTBOX_ATTEMPTS,
    OutboxLeaseLostError,
    claim_available_outbox_work,
    complete_outbox_work,
    persist_outbound_intent,
    record_outbox_failure,
)


class _ScalarResult:
    def __init__(self, rows: list[OutboxWork]) -> None:
        self._rows = rows

    def all(self) -> list[OutboxWork]:
        return self._rows


class _ClaimSession:
    def __init__(self, rows: list[OutboxWork], *, database_now: datetime) -> None:
        self.rows = rows
        self.database_now = database_now
        self.added: list[object] = []
        self.commit_count = 0
        self.rollback_count = 0

    async def scalar(self, statement: object) -> datetime:
        return self.database_now

    async def scalars(self, statement: object) -> _ScalarResult:
        return _ScalarResult(self.rows)

    def add(self, row: object) -> None:
        self.added.append(row)

    async def commit(self) -> None:
        self.commit_count += 1

    async def rollback(self) -> None:
        self.rollback_count += 1


class _MutationSession:
    def __init__(
        self,
        *,
        current_work: OutboxWork | None,
        database_now: datetime,
    ) -> None:
        self.current_work = current_work
        self.database_now = database_now
        self.added: list[object] = []
        self.commit_count = 0
        self.rollback_count = 0

    async def scalar(self, statement: object) -> OutboxWork | datetime | None:
        if "FROM outbox_work" in str(statement):
            return self.current_work
        return self.database_now

    def add(self, row: object) -> None:
        self.added.append(row)

    async def commit(self) -> None:
        self.commit_count += 1

    async def rollback(self) -> None:
        self.rollback_count += 1


def _work(*, attempt_count: int = 0) -> OutboxWork:
    return OutboxWork(
        id=uuid4(),
        inbound_message_id=uuid4(),
        attempt_count=attempt_count,
    )


class TestClaimOutboxWork:
    async def test_claim_commits_lease_before_returning_work_for_processing(
        self,
    ) -> None:
        now = datetime(2026, 8, 4, tzinfo=UTC)
        work = _work()
        session = _ClaimSession([work], database_now=now)

        outcome = await claim_available_outbox_work(
            cast(AsyncSession, session),
            worker_id="worker-a",
            limit=1,
            lease_duration=timedelta(minutes=2),
        )

        assert outcome.claimed == [work]
        assert outcome.terminalized == []
        assert session.commit_count == 1
        assert work.lease_owner == "worker-a"
        assert work.lease_expires_at == now + timedelta(minutes=2)
        assert work.attempt_count == 1

    async def test_expired_work_at_attempt_cap_is_committed_and_returned_for_alert(
        self,
    ) -> None:
        now = datetime(2026, 8, 4, tzinfo=UTC)
        work = _work(attempt_count=MAX_OUTBOX_ATTEMPTS)
        work.lease_owner = "crashed-worker"
        work.lease_expires_at = now - timedelta(seconds=1)
        session = _ClaimSession([work], database_now=now)

        outcome = await claim_available_outbox_work(
            cast(AsyncSession, session),
            worker_id="recovery-worker",
            limit=1,
        )

        assert outcome.claimed == []
        assert outcome.terminalized == [work]
        assert work.failed_at == now
        assert work.lease_owner is None
        assert work.lease_expires_at is None
        assert session.commit_count == 1
        assert any(isinstance(row, AuditEvent) for row in session.added)


class TestOutboxCompletion:
    async def test_completion_is_fenced_to_the_live_owner_lease(self) -> None:
        now = datetime(2026, 8, 4, tzinfo=UTC)
        work = _work(attempt_count=1)
        work.lease_owner = "worker-a"
        work.lease_expires_at = now + timedelta(minutes=10)
        session = _MutationSession(current_work=work, database_now=now)

        await complete_outbox_work(
            cast(AsyncSession, session),
            work=work,
            worker_id="worker-a",
        )

        assert work.completed_at == now
        assert work.lease_owner is None
        assert work.lease_expires_at is None
        assert session.commit_count == 1

    async def test_completion_lease_loss_rolls_back_without_stale_mutation(
        self,
    ) -> None:
        now = datetime(2026, 8, 4, tzinfo=UTC)
        stale_work = _work()
        session = _MutationSession(current_work=None, database_now=now)

        with pytest.raises(OutboxLeaseLostError):
            await complete_outbox_work(
                cast(AsyncSession, session),
                work=stale_work,
                worker_id="worker-a",
            )

        assert stale_work.completed_at is None
        assert session.commit_count == 0
        assert session.rollback_count == 1

    def test_default_lease_exceeds_the_runtime_turn_timeout(self) -> None:
        assert DEFAULT_LEASE_DURATION >= timedelta(minutes=10)


class TestOutboxFailure:
    async def test_failure_schedules_a_bounded_jittered_retry_without_losing_intent(
        self,
    ) -> None:
        now = datetime(2026, 8, 4, tzinfo=UTC)
        work = _work(attempt_count=2)
        work.lease_owner = "worker-a"
        work.lease_expires_at = now + timedelta(minutes=2)
        work.outbound_body = {"text": "replay this reply"}
        work.outbound_send_key = "turn-42"
        session = _MutationSession(current_work=work, database_now=now)

        outcome = await record_outbox_failure(
            cast(AsyncSession, session),
            work=work,
            worker_id="worker-a",
            error="provider response lost",
            jitter=lambda: 0.5,
        )

        assert outcome.terminal is False
        assert outcome.alert_required is False
        assert work.available_at == now + timedelta(seconds=2)
        assert work.lease_owner is None
        assert work.lease_expires_at is None
        assert work.failed_at is None
        assert work.outbound_body == {"text": "replay this reply"}
        assert work.outbound_send_key == "turn-42"
        assert session.commit_count == 1

    async def test_terminal_failure_is_committed_with_durable_audit_escalation(
        self,
    ) -> None:
        now = datetime(2026, 8, 4, tzinfo=UTC)
        work = _work(attempt_count=MAX_OUTBOX_ATTEMPTS)
        work.lease_owner = "worker-a"
        work.lease_expires_at = now + timedelta(minutes=2)
        session = _MutationSession(current_work=work, database_now=now)

        outcome = await record_outbox_failure(
            cast(AsyncSession, session),
            work=work,
            worker_id="worker-a",
            error="provider rejected request",
        )

        assert outcome.terminal is True
        assert outcome.alert_required is True
        assert work.failed_at == now
        assert work.lease_owner is None
        assert work.lease_expires_at is None
        assert work.last_error == "provider rejected request"
        assert session.commit_count == 1
        audit = next(row for row in session.added if isinstance(row, AuditEvent))
        assert audit.event_type == "webhook_delivery_terminal_failure"
        assert audit.correlation_id == f"outbox:{work.id}"
        assert audit.payload["operator_action"] == "required"


class TestOutboundIntent:
    async def test_persisted_send_key_is_replay_metadata_not_delivery_deduplication(
        self,
    ) -> None:
        work = _work()
        now = datetime(2026, 8, 4, tzinfo=UTC)
        work.lease_owner = "worker-a"
        work.lease_expires_at = now + timedelta(minutes=2)
        session = _MutationSession(current_work=work, database_now=now)

        await persist_outbound_intent(
            cast(AsyncSession, session),
            work=work,
            worker_id="worker-a",
            body={"text": "persist before provider send"},
            send_key="turn-42",
        )

        assert work.outbound_body == {"text": "persist before provider send"}
        assert work.outbound_send_key == "turn-42"
        assert session.commit_count == 1

    async def test_lease_lost_rolls_back_without_mutating_stale_work(self) -> None:
        now = datetime(2026, 8, 4, tzinfo=UTC)
        stale_work = _work()
        stale_work.lease_owner = "worker-a"
        stale_work.lease_expires_at = now + timedelta(minutes=2)
        session = _MutationSession(current_work=None, database_now=now)

        with pytest.raises(OutboxLeaseLostError):
            await persist_outbound_intent(
                cast(AsyncSession, session),
                work=stale_work,
                worker_id="worker-a",
                body={"text": "must not persist"},
                send_key="turn-42",
            )

        assert stale_work.outbound_body is None
        assert session.commit_count == 0
        assert session.rollback_count == 1
