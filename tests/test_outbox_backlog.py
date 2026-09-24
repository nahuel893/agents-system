"""Tests for #141 — outbox backlog counts consumed by GET /health.

``count_outbox_backlog`` must split non-terminal ``outbox_work`` rows by
lease state (unleased == pending, leased == currently or previously claimed,
leased_expired == leased AND already past expiry against the database
clock) without depending on a live database -- the fake session below
inspects the compiled statement text the same way ``tests/test_outbox_worker.py``
already does for other ``services/outbox.py`` queries.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from agentsys.services.outbox import OutboxBacklogCounts, count_outbox_backlog

_DATABASE_NOW = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)


class _BacklogSession:
    """Returns *pending* for a ``lease_expires_at IS NULL`` count query,
    *leased* for a plain ``lease_expires_at IS NOT NULL`` one, *now* for the
    ``clock_timestamp()`` lookup, and *leased_expired* for the query that
    additionally compares ``lease_expires_at`` against the database clock --
    whichever order ``count_outbox_backlog`` issues them in."""

    def __init__(
        self,
        *,
        pending: int,
        leased: int,
        leased_expired: int = 0,
        now: datetime = _DATABASE_NOW,
    ) -> None:
        self._pending = pending
        self._leased = leased
        self._leased_expired = leased_expired
        self._now = now
        self.statements: list[str] = []

    async def scalar(self, statement: object) -> int | datetime:
        compiled = str(statement)
        self.statements.append(compiled)
        if "clock_timestamp" in compiled:
            return self._now
        if "lease_expires_at IS NOT NULL" in compiled and " < " in compiled:
            return self._leased_expired
        if "lease_expires_at IS NOT NULL" in compiled:
            return self._leased
        if "lease_expires_at IS NULL" in compiled:
            return self._pending
        raise AssertionError(f"unexpected statement shape: {compiled}")


class TestCountOutboxBacklog:
    async def test_splits_pending_leased_and_leased_expired_counts(self) -> None:
        session = _BacklogSession(pending=3, leased=2, leased_expired=1)

        result = await count_outbox_backlog(session)  # type: ignore[arg-type]

        assert result == OutboxBacklogCounts(pending=3, leased=2, leased_expired=1)

    async def test_zero_backlog_returns_zero_counts(self) -> None:
        session = _BacklogSession(pending=0, leased=0, leased_expired=0)

        result = await count_outbox_backlog(session)  # type: ignore[arg-type]

        assert result == OutboxBacklogCounts(pending=0, leased=0, leased_expired=0)

    async def test_leased_expired_is_independent_of_leased(self) -> None:
        """A live (not-yet-expired) lease is ``leased`` but must not itself
        be counted as ``leased_expired`` -- the two queries are genuinely
        different filters, not one derived from the other in Python."""
        session = _BacklogSession(pending=0, leased=5, leased_expired=2)

        result = await count_outbox_backlog(session)  # type: ignore[arg-type]

        assert result.leased == 5
        assert result.leased_expired == 2

    async def test_both_queries_exclude_terminal_rows(self) -> None:
        """The pending, leased, and leased_expired counts must all filter
        out completed and failed rows -- a terminal row is neither waiting
        nor in flight."""
        session = _BacklogSession(pending=1, leased=1, leased_expired=1)

        await count_outbox_backlog(session)  # type: ignore[arg-type]

        count_statements = [s for s in session.statements if "clock_timestamp" not in s]
        assert len(count_statements) == 3
        for compiled in count_statements:
            assert "completed_at IS NULL" in compiled
            assert "failed_at IS NULL" in compiled

    async def test_leased_expired_query_compares_against_the_database_clock(
        self,
    ) -> None:
        """The expired-lease count must be bounded by the database's own
        clock (``clock_timestamp()``, matching every other lease comparison
        in this module), not app-server wall time -- a skewed app clock must
        not mis-classify a lease that is, from the database's point of view,
        still live."""
        session = _BacklogSession(pending=0, leased=1, leased_expired=1)

        await count_outbox_backlog(session)  # type: ignore[arg-type]

        assert any("clock_timestamp" in s for s in session.statements)
        expired_statements = [
            s
            for s in session.statements
            if "lease_expires_at IS NOT NULL" in s and " < " in s
        ]
        assert len(expired_statements) == 1

    async def test_none_scalar_result_counts_as_zero(self) -> None:
        """``session.scalar`` returning ``None`` (an empty/degenerate result)
        must not propagate as ``None`` -- the caller (`/health`) does
        arithmetic on these counts."""

        class _NoneSession:
            async def scalar(self, statement: object) -> datetime | None:
                if "clock_timestamp" in str(statement):
                    return _DATABASE_NOW
                return None

        result = await count_outbox_backlog(_NoneSession())  # type: ignore[arg-type]

        assert result == OutboxBacklogCounts(pending=0, leased=0, leased_expired=0)

    async def test_non_int_scalar_result_raises(self) -> None:
        """``COUNT(*)`` is always ``NULL`` or an integer against a real
        database. Anything else (e.g. a test double that silently returns a
        truthy non-int instead of failing) must raise rather than be
        laundered through ``or 0`` into a wrong, falsely-truthy count --
        ``/health``'s degraded-backlog check does arithmetic and comparisons
        on this value and must not be fooled by it."""

        class _WrongTypeSession:
            async def scalar(self, statement: object) -> object:
                return object()

        with pytest.raises(TypeError):
            await count_outbox_backlog(_WrongTypeSession())  # type: ignore[arg-type]
