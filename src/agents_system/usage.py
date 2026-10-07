"""Per-end-user usage ledger and budget caps (ADR-011 part 5, #252).

The ledger records one row per turn, keyed by (runtime id, subject key) and
written through the scope handle (`agents_system.scope`), so a Postgres
backend fences it with row-level security. It is also a `SubjectDataStore`:
the operator export and delete reach it like any other per-user store.

Caps are set per runtime id by the deployment (`create_app(budgets=...)`,
`USER_BUDGETS`, or `AgentRuntime(budget=...)`); an agent definition cannot
raise them. A cap is reached when the subject's total for the UTC day or
month is at or above it.

This module depends only on `agents_system.scope` and `agents_system.subject_data`.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from agents_system.scope import InMemoryScopeProvider
from agents_system.subject_data import SubjectDataStore, SubjectRef

USAGE_TABLE = "subject_usage"
DEFAULT_REFUSAL_MESSAGE = (
    "You have reached your usage limit for now. Please try again later."
)
_CAP_FIELDS = ("daily_usd", "monthly_usd", "daily_tokens", "monthly_tokens")
_TOKEN_CAPS = ("daily_tokens", "monthly_tokens")


@dataclass(frozen=True)
class BudgetCaps:
    """Per-user caps of one runtime: USD and/or tokens, per UTC day and month."""

    daily_usd: float | None = None
    monthly_usd: float | None = None
    daily_tokens: int | None = None
    monthly_tokens: int | None = None
    message: str = DEFAULT_REFUSAL_MESSAGE

    @property
    def has_usd_cap(self) -> bool:
        return self.daily_usd is not None or self.monthly_usd is not None


def parse_budgets(raw: Mapping[str, Any]) -> dict[str, BudgetCaps]:
    """``{runtime_id: {daily_usd, monthly_usd, daily_tokens, monthly_tokens, message}}``.

    The shape of ``DEPLOY_GRANTS``: keyed by runtime id. A value already a
    ``BudgetCaps`` passes through. Raises ``ValueError`` on an unknown key, a
    non-positive cap, or an entry with no cap at all.
    """
    parsed: dict[str, BudgetCaps] = {}
    for runtime_id, value in raw.items():
        if isinstance(value, BudgetCaps):
            parsed[runtime_id] = value
            continue
        unknown = set(value) - {*_CAP_FIELDS, "message"}
        if unknown:
            raise ValueError(
                f"budget for {runtime_id!r} has unknown key(s): {sorted(unknown)}"
            )
        caps: dict[str, Any] = {}
        for name in _CAP_FIELDS:
            if value.get(name) is None:
                continue
            cap = value[name]
            integral = name in _TOKEN_CAPS
            if isinstance(cap, bool) or not isinstance(
                cap, int if integral else (int, float)
            ):
                raise ValueError(f"budget {runtime_id!r}.{name} must be a number")  # noqa: TRY004 - settings validators raise ValueError
            if cap <= 0:
                raise ValueError(f"budget {runtime_id!r}.{name} must be positive")
            caps[name] = cap if integral else float(cap)
        if not caps:
            raise ValueError(f"budget for {runtime_id!r} sets no cap")
        if "message" in value:
            caps["message"] = str(value["message"])
        parsed[runtime_id] = BudgetCaps(**caps)
    return parsed


def window_starts(now: datetime) -> tuple[datetime, datetime]:
    """Start of the UTC day and of the UTC month containing *now*."""
    now = now.astimezone(UTC)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return day, day.replace(day=1)


@dataclass(frozen=True)
class UsageRecord:
    """One turn's usage. ``priced`` is False when the model had no price."""

    runtime_id: str
    subject_key: str
    actor: str | None = None
    model_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cost_usd: float | None = None
    model_calls: int | None = None
    tool_calls: int = 0
    timed_out: bool = False
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    @property
    def priced(self) -> bool:
        return self.cost_usd is not None


@dataclass(frozen=True)
class UsageTotals:
    """Sums over a window. Unreported tokens count as zero; ``unpriced_turns`` flags them."""

    tokens: int = 0
    cost_usd: float = 0.0
    turns: int = 0
    unpriced_turns: int = 0


def totals_of(records: list[UsageRecord]) -> UsageTotals:
    return UsageTotals(
        tokens=sum(r.total_tokens or 0 for r in records),
        cost_usd=sum(r.cost_usd or 0.0 for r in records),
        turns=len(records),
        unpriced_turns=sum(1 for r in records if not r.priced),
    )


@runtime_checkable
class UsageLedger(SubjectDataStore, Protocol):
    async def record(self, record: UsageRecord) -> None: ...

    async def totals(
        self, runtime_id: str, subject_key: str, *, since: datetime
    ) -> UsageTotals: ...

    async def query(
        self, runtime_id: str, subject_key: str, *, since: datetime | None = None
    ) -> list[UsageRecord]: ...


class InMemoryUsageLedger:
    """Reference ledger over `InMemoryScopeProvider`: library mode, no Postgres."""

    name = "usage"
    tables = frozenset({USAGE_TABLE})

    def __init__(self, provider: InMemoryScopeProvider) -> None:
        self.provider = provider

    def _scope(self, runtime_id: str, subject_key: str) -> Any:
        return self.provider.scope(runtime_id, subject_key)

    async def record(self, record: UsageRecord) -> None:
        self._scope(record.runtime_id, record.subject_key).put(
            USAGE_TABLE, record.id, record
        )

    async def query(
        self, runtime_id: str, subject_key: str, *, since: datetime | None = None
    ) -> list[UsageRecord]:
        rows = self._scope(runtime_id, subject_key).rows(USAGE_TABLE).values()
        return sorted(
            (r for r in rows if since is None or r.occurred_at >= since),
            key=lambda r: r.occurred_at,
        )

    async def totals(
        self, runtime_id: str, subject_key: str, *, since: datetime
    ) -> UsageTotals:
        return totals_of(await self.query(runtime_id, subject_key, since=since))

    async def export(self, subject: SubjectRef) -> list[dict[str, Any]]:
        records = await self.query(subject.runtime_id, subject.subject_key)
        return [
            {**dataclasses.asdict(r), "occurred_at": r.occurred_at.isoformat()}
            for r in records
        ]

    async def delete(self, subject: SubjectRef) -> int:
        scope = self._scope(subject.runtime_id, subject.subject_key)
        rows = scope.rows(USAGE_TABLE)
        for key in rows:
            scope.delete(USAGE_TABLE, key)
        return len(rows)


async def reached_cap(
    ledger: UsageLedger,
    caps: BudgetCaps,
    runtime_id: str,
    subject_key: str,
    *,
    pending: UsageTotals = UsageTotals(),
    now: datetime | None = None,
) -> tuple[str, float, float] | None:
    """The first cap the subject has reached as ``(name, limit, used)``, or ``None``.

    ``pending`` is usage of the running turn that is not in the ledger yet.
    """
    day, month = window_starts(now or datetime.now(UTC))
    daily = await ledger.totals(runtime_id, subject_key, since=day)
    monthly = await ledger.totals(runtime_id, subject_key, since=month)
    checks = (
        ("daily_usd", caps.daily_usd, daily.cost_usd + pending.cost_usd),
        ("monthly_usd", caps.monthly_usd, monthly.cost_usd + pending.cost_usd),
        ("daily_tokens", caps.daily_tokens, daily.tokens + pending.tokens),
        ("monthly_tokens", caps.monthly_tokens, monthly.tokens + pending.tokens),
    )
    for name, limit, used in checks:
        if limit is not None and used >= limit:
            return name, float(limit), float(used)
    return None
