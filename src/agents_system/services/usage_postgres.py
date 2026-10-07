"""Postgres usage ledger over the scope handle (ADR-011 part 5, #252).

Every statement runs in a ``PostgresScope`` transaction, so the row-level
security policy of migration ``009`` fences it to one (runtime id, subject
key): an unfiltered query here still sees only that subject's rows.

The ledger registers itself in the default `SubjectDataRegistry` at import,
unbound; the served app binds it to its scope provider at boot
(`PostgresUsageLedger.bind`). Unbound, it holds nothing and exports and
deletes nothing. It declares no ``purge``: a purge across subjects has no
scope to run in under row-level security, so rows are kept until the operator
deletes the subject (the monthly cap needs at least a month of history).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from agents_system.extras import raise_missing_extra
from agents_system.services.scope_postgres import PostgresScopeProvider
from agents_system.subject_data import DEFAULT_REGISTRY, SubjectRef
from agents_system.usage import USAGE_TABLE, UsageRecord, UsageTotals

try:
    from sqlalchemy import text
except ImportError as exc:
    raise_missing_extra("postgres", "The Postgres usage ledger", exc)

_COLUMNS = (
    "runtime_id, subject_key, id, occurred_at, actor, model_id, input_tokens, "
    "output_tokens, total_tokens, cost_usd, model_calls, tool_calls, timed_out"
)


class PostgresUsageLedger:
    name = "usage"
    tables = frozenset({USAGE_TABLE})

    def __init__(self, provider: PostgresScopeProvider | None = None) -> None:
        self._provider = provider

    def bind(self, provider: PostgresScopeProvider) -> None:
        self._provider = provider

    def _tx(self, runtime_id: str, subject_key: str) -> Any:
        assert self._provider is not None
        return self._provider.scope(runtime_id, subject_key).transaction()

    async def record(self, record: UsageRecord) -> None:
        async with self._tx(record.runtime_id, record.subject_key) as conn:
            await conn.execute(
                text(
                    f"INSERT INTO {USAGE_TABLE} ({_COLUMNS}) VALUES (:runtime_id, "
                    ":subject_key, :id, :occurred_at, :actor, :model_id, "
                    ":input_tokens, :output_tokens, :total_tokens, :cost_usd, "
                    ":model_calls, :tool_calls, :timed_out)"
                ),
                {
                    "runtime_id": record.runtime_id,
                    "subject_key": record.subject_key,
                    "id": record.id,
                    "occurred_at": record.occurred_at,
                    "actor": record.actor,
                    "model_id": record.model_id,
                    "input_tokens": record.input_tokens,
                    "output_tokens": record.output_tokens,
                    "total_tokens": record.total_tokens,
                    "cost_usd": record.cost_usd,
                    "model_calls": record.model_calls,
                    "tool_calls": record.tool_calls,
                    "timed_out": record.timed_out,
                },
            )

    async def totals(
        self, runtime_id: str, subject_key: str, *, since: datetime
    ) -> UsageTotals:
        # No subject predicate on purpose: the policy is what fences the read.
        async with self._tx(runtime_id, subject_key) as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT coalesce(sum(total_tokens), 0), "
                        "coalesce(sum(cost_usd), 0), count(*), "
                        f"count(*) FILTER (WHERE cost_usd IS NULL) FROM {USAGE_TABLE} "
                        "WHERE occurred_at >= :since"
                    ),
                    {"since": since},
                )
            ).one()
        return UsageTotals(
            tokens=int(row[0]),
            cost_usd=float(row[1]),
            turns=int(row[2]),
            unpriced_turns=int(row[3]),
        )

    async def query(
        self, runtime_id: str, subject_key: str, *, since: datetime | None = None
    ) -> list[UsageRecord]:
        async with self._tx(runtime_id, subject_key) as conn:
            rows = await conn.execute(
                text(
                    f"SELECT {_COLUMNS} FROM {USAGE_TABLE} "
                    "WHERE (CAST(:since AS timestamptz) IS NULL "
                    "OR occurred_at >= CAST(:since AS timestamptz)) ORDER BY occurred_at"
                ),
                {"since": since},
            )
            return [UsageRecord(**dict(r._mapping)) for r in rows]

    async def export(self, subject: SubjectRef) -> list[dict[str, Any]]:
        if self._provider is None:
            return []
        records = await self.query(subject.runtime_id, subject.subject_key)
        return [
            {
                "id": r.id,
                "occurred_at": r.occurred_at.isoformat(),
                "actor": r.actor,
                "model_id": r.model_id,
                "input_tokens": r.input_tokens,
                "output_tokens": r.output_tokens,
                "total_tokens": r.total_tokens,
                "cost_usd": r.cost_usd,
                "model_calls": r.model_calls,
                "tool_calls": r.tool_calls,
                "timed_out": r.timed_out,
            }
            for r in records
        ]

    async def delete(self, subject: SubjectRef) -> int:
        if self._provider is None:
            return 0
        async with self._tx(subject.runtime_id, subject.subject_key) as conn:
            result = await conn.execute(text(f"DELETE FROM {USAGE_TABLE}"))
            return int(result.rowcount)


#: The process-wide instance, registered so the migration's table has a store.
USAGE_LEDGER = PostgresUsageLedger()
if "usage" not in DEFAULT_REGISTRY.stores:
    DEFAULT_REGISTRY.register(USAGE_LEDGER)
