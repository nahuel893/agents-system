"""Checkpointer, usage ledger and store bindings the app lifespan wires."""

from __future__ import annotations

from collections.abc import (
    Callable,
    Mapping,
)
from functools import partial
from typing import TYPE_CHECKING, Any

from agents_system.config import Settings
from agents_system.schedules import InMemoryScheduleStore
from agents_system.scope import InMemoryScopeProvider, ScopeProvider
from agents_system.subject_data import (
    SubjectDataRegistry,
    SubjectDataStore,
)
from agents_system.usage import (
    BudgetCaps,
    InMemoryUsageLedger,
    UsageLedger,
)
from agents_system.user_memory import (
    InMemoryUserFactStore,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

from agents_system.services.db_role import role_is_read_only


def _build_checkpointer_cm(settings: Settings) -> Any:
    """Build the checkpointer's async context manager (design AD-1, issue #24).

    LangGraph's ``AsyncPostgresSaver`` on its own psycopg pool, in the app's
    PostgreSQL database -- the store that already holds the durable webhook
    inbox and outbox -- so a restart or a container recreation keeps every
    conversation. Entering it opens the pool and checks the checkpoint schema
    (created by ``alembic upgrade head``, revision 007); it is held open for
    the app's lifetime through the lifespan's ``AsyncExitStack``.

    Only durability is configured here. How long an idle conversation is kept
    is ``checkpointer_retention_s``, enforced by the purger the lifespan
    starts next to it (``_build_checkpoint_purger``), never by the saver.
    """
    from agents_system.services.checkpointer import open_postgres_checkpointer

    return open_postgres_checkpointer(
        settings.database_url,
        pool_size=settings.checkpointer_pool_size,
        timeout_s=settings.checkpointer_timeout_s,
    )


def _build_checkpoint_purger(checkpointer: Any, settings: Settings) -> Any:
    """The periodic job enforcing ``checkpointer_retention_s`` (issue #24).

    Deletes every conversation idle for longer than the retention and prunes
    each thread's superseded checkpoints, on ``checkpointer_purge_interval_s``.
    The purge is SQL on the saver's own tables, so it exists only for the
    Postgres saver; any other checkpointer (a test double) gets none.
    """
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    from agents_system.services import checkpointer as checkpointer_service

    if not isinstance(checkpointer, AsyncPostgresSaver):
        return None
    return checkpointer_service.CheckpointPurger(
        partial(checkpointer_service.purge_checkpoints, checkpointer.conn),
        retention_s=settings.checkpointer_retention_s,
        interval_s=settings.checkpointer_purge_interval_s,
    )


async def _bi_role_is_read_only(engine: Any) -> bool | None:
    """Ask the database whether the BI role really is read-only.

    Thin BI-scoped wrapper over the shared
    :func:`agents_system.services.db_role.role_is_read_only` (the full
    True/False/None reasoning lives there — see its docstring). Kept as its
    own function, rather than calling the shared one directly at the
    lifespan call site below, so `bi.read_only_check_failed` stays this
    deployment's own log event name and so any future BI-specific behaviour
    has a place to live without touching the shared helper.
    """
    return await role_is_read_only(engine, log_event="bi.read_only_check_failed")


def _build_usage_ledger(
    scope_provider: ScopeProvider | None,
    registry: SubjectDataRegistry,
    budgets: Mapping[str, BudgetCaps],
) -> UsageLedger | None:
    """The per-user usage ledger over the scope provider (ADR-011 part 5, #252).

    With a scope provider every runtime records its subjects' turns; with
    budgets and no provider there is nowhere to count, so the boot fails
    rather than run uncapped. The ledger is registered as a subject data
    store so the operator export and delete reach it.
    """
    if scope_provider is None:
        if budgets:
            raise ValueError(
                "USER_BUDGETS / create_app(budgets=...) need a usage ledger: "
                "pass create_app(scope_provider_factory=...)"
            )
        return None
    ledger: UsageLedger
    if isinstance(scope_provider, InMemoryScopeProvider):
        ledger = InMemoryUsageLedger(scope_provider)
    else:
        from agents_system.services.usage_postgres import PostgresUsageLedger

        # One ledger per app, never the import-time placeholder: the runtime
        # and the operator registry must hold the very same object.
        ledger = PostgresUsageLedger(scope_provider)  # type: ignore[arg-type]
    registry.bind(ledger)
    return ledger


def _bind_user_fact_store(
    scope_provider: ScopeProvider | None, registry: SubjectDataRegistry
) -> None:
    """Register the per-user facts store so export and delete reach it (#250)."""
    if scope_provider is None:
        return
    store: SubjectDataStore
    if isinstance(scope_provider, InMemoryScopeProvider):
        store = InMemoryUserFactStore(scope_provider)
    else:
        from agents_system.services.user_facts_postgres import PostgresUserFactStore

        store = PostgresUserFactStore(scope_provider)  # type: ignore[arg-type]
    registry.bind(store)


def _bind_schedule_store(
    scope_provider: ScopeProvider | None, registry: SubjectDataRegistry
) -> None:
    """Register the schedules store so export and delete reach it (#175)."""
    if scope_provider is None:
        return
    store: SubjectDataStore
    if isinstance(scope_provider, InMemoryScopeProvider):
        store = InMemoryScheduleStore(scope_provider)
    else:
        from agents_system.services.schedules_postgres import PostgresScheduleStore

        store = PostgresScheduleStore(scope_provider)  # type: ignore[arg-type]
    registry.bind(store)


async def _start_scope_provider(
    factory: Callable[[AsyncEngine], ScopeProvider] | None, engine: AsyncEngine
) -> ScopeProvider | None:
    """Build the per-user scope provider and refuse an unfenced database role.

    ADR-011 part 3: only when a per-user feature is configured (``factory`` is
    given), so deployments without one keep booting on any role. A provider
    with a ``verify()`` (Postgres: superuser / BYPASSRLS refusal) is checked
    before any runtime is built.
    """
    if factory is None:
        return None
    provider = factory(engine)
    verify = getattr(provider, "verify", None)
    if verify is not None:
        await verify()
    return provider


def _bind_proactive_store(registry: SubjectDataRegistry, provider: Any) -> None:
    """Serve proactive messages through the operator export, delete and purge.

    The queue table holds message bodies per subject, so the app's registry
    must carry the store bound to the app's database, not only whichever
    module happened to be imported. A no-op without the ``postgres`` extra or
    a Postgres scope provider (nothing could have been queued).
    """
    try:
        from agents_system.services.proactive_postgres import PROACTIVE_STORE
        from agents_system.services.scope_postgres import PostgresScopeProvider
    except ImportError:
        return
    if not isinstance(provider, PostgresScopeProvider):
        return
    if PROACTIVE_STORE.name not in registry.stores:
        registry.register(PROACTIVE_STORE)
    PROACTIVE_STORE.bind(provider)
