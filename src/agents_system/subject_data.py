"""Per-user data registry, operator export and delete, retention (ADR-011 part 6).

Every store that keeps data per end user implements ``SubjectDataStore`` and
registers it, so one operator call exports or deletes a subject's data
everywhere and a new store cannot be forgotten (a test fails when a table
with a ``subject_key`` column has no registered store). Each store also
declares its own retention; one purger enforces them all, in the shape of
``CheckpointPurger``.

The conversation checkpointer is not a registered store: its threads are
keyed by conversation, not by subject, so the operator names the subject's
thread ids and the same calls cover them.

This module has no dependency on the rest of the package beyond the audit
event and the actor type.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import structlog

from agents_system.subject import TurnActor

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class SubjectRef:
    """The pair every per-user row is keyed by (ADR-011 part 3)."""

    runtime_id: str
    subject_key: str


@runtime_checkable
class SubjectDataStore(Protocol):
    """A store of per-user data. ``tables`` names every table with a ``subject_key`` column."""

    name: str
    tables: frozenset[str]

    async def export(self, subject: SubjectRef) -> list[dict[str, Any]]: ...

    async def delete(self, subject: SubjectRef) -> int: ...


@runtime_checkable
class RetainedStore(SubjectDataStore, Protocol):
    """A store that expires old rows: ``purge`` deletes rows older than the retention."""

    async def purge(self, *, retention_s: int) -> int: ...


class ThreadStore(Protocol):
    """The conversation checkpointer, addressed by thread id."""

    async def export_thread(self, thread_id: str) -> dict[str, Any] | None: ...

    async def delete_thread(self, thread_id: str) -> int: ...


class SubjectDeletionError(RuntimeError):
    """At least one store failed; the others were still attempted."""

    def __init__(
        self, failed: list[str], deleted: dict[str, int], threads: int = 0
    ) -> None:
        super().__init__(f"subject data deletion failed in: {', '.join(failed)}")
        self.failed = failed
        self.deleted = deleted
        self.threads = threads


class SubjectDataRegistry:
    """The stores a deployment keeps per-user data in."""

    def __init__(self) -> None:
        self._stores: dict[str, SubjectDataStore] = {}

    def register(self, store: SubjectDataStore) -> None:
        if store.name in self._stores:
            raise ValueError(f"subject data store {store.name!r} is already registered")
        self._stores[store.name] = store

    def bind(self, store: SubjectDataStore) -> None:
        """Register *store*, replacing an unbound placeholder of the same name.

        A store that exposes ``unbound`` as true (a migration-guard placeholder
        waiting for its app to supply a provider) yields to the live store.
        Any other existing store of that name is a conflict and raises, rather
        than leave the operator export and delete pointed at another store.
        """
        existing = self._stores.get(store.name)
        if existing is store:
            return
        if existing is not None and not getattr(existing, "unbound", False):
            raise ValueError(
                f"subject data store {store.name!r} is already registered "
                "with a different store"
            )
        self._stores[store.name] = store

    def copy(self) -> SubjectDataRegistry:
        """An independent registry holding the same stores (one per app)."""
        clone = SubjectDataRegistry()
        clone._stores = dict(self._stores)
        return clone

    @property
    def stores(self) -> Mapping[str, SubjectDataStore]:
        return dict(self._stores)

    @property
    def tables(self) -> frozenset[str]:
        return frozenset(t for s in self._stores.values() for t in s.tables)


#: The process-wide registry stores register into; ``create_app`` serves it.
DEFAULT_REGISTRY = SubjectDataRegistry()


def parse_retention(value: object) -> int | None:
    """Seconds, or ``never`` for ``None``: the same grammar as ``CHECKPOINTER_RETENTION_S``."""
    if isinstance(value, str):
        text = value.strip().lower()
        if text == "never":
            return None
        try:
            value = int(text)
        except ValueError:
            raise ValueError(f"retention must be seconds or 'never', got {value!r}")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(
            f"retention must be positive seconds or 'never', got {value!r}"
        )
    return value


async def export_subject(
    registry: SubjectDataRegistry,
    subject: SubjectRef,
    *,
    threads: ThreadStore | None = None,
    thread_ids: Sequence[str] = (),
) -> dict[str, Any]:
    """Everything stored for ``subject``: each registered store plus its threads."""
    stores = {
        name: await store.export(subject) for name, store in registry.stores.items()
    }
    exported: list[dict[str, Any]] = []
    if threads is not None:
        for thread_id in thread_ids:
            thread = await threads.export_thread(thread_id)
            if thread is not None:
                exported.append(thread)
    return {
        "runtime_id": subject.runtime_id,
        "subject_key": subject.subject_key,
        "stores": stores,
        "threads": exported,
    }


async def delete_subject(
    registry: SubjectDataRegistry,
    subject: SubjectRef,
    *,
    actor: TurnActor,
    threads: ThreadStore | None = None,
    thread_ids: Sequence[str] = (),
) -> dict[str, Any]:
    """Delete ``subject``'s data everywhere, audited with ``actor``.

    Every store and every thread is attempted even when one fails; the
    failures are raised as one ``SubjectDeletionError`` after the audit event
    is recorded. The result carries ``audited``: false when no sink took the
    event (the sink is fire-and-forget, so this is best effort).
    """
    deleted: dict[str, int] = {}
    failed: list[str] = []
    for name, store in registry.stores.items():
        try:
            deleted[name] = await store.delete(subject)
        except Exception:
            logger.exception("subject_data.delete_failed", store=name)
            failed.append(name)
    thread_count = 0
    failed_threads = 0
    if threads is not None:
        for thread_id in thread_ids:
            try:
                thread_count += await threads.delete_thread(thread_id)
            except Exception:
                logger.exception(
                    "subject_data.delete_failed",
                    store="checkpointer",
                    thread_id=thread_id,
                )
                failed_threads += 1
    if failed_threads:
        failed.append(f"checkpointer: {failed_threads} thread(s)")
    audited = await _audit_deletion(subject, actor, deleted, thread_count, failed)
    if failed:
        raise SubjectDeletionError(failed, deleted, thread_count)
    return {
        "runtime_id": subject.runtime_id,
        "subject_key": subject.subject_key,
        "stores": deleted,
        "threads": thread_count,
        "audited": audited,
    }


async def _audit_deletion(
    subject: SubjectRef,
    actor: TurnActor,
    stores: dict[str, int],
    threads: int,
    failed: list[str],
) -> bool:
    """Record the deletion; ``False`` when it could not be (the data is gone either way).

    The deletion already happened, so an audit failure must not discard its
    result: it is logged loudly and reported as ``audited: false``.
    """
    from agents_system.audit.recorder import record_subject_data_deleted
    from agents_system.audit.sink import AuditSink

    sink = AuditSink.registered()
    if sink is None:
        logger.warning(
            "subject_data.delete_unaudited", reason="no audit sink registered"
        )
        return False
    try:
        event = await record_subject_data_deleted(
            runtime_id=subject.runtime_id,
            subject_key=subject.subject_key,
            actor=actor.label,
            stores={**{n: 0 for n in stores}, **stores},
            threads=threads,
            failed=failed,
        )
        await sink.record(event)
    except Exception:
        logger.exception("subject_data.delete_unaudited", reason="audit write failed")
        return False
    return True


class SubjectDataPurger:
    """Run every store's purge now and then every ``interval_s`` seconds until stopped.

    ``retention`` maps a store name to seconds or ``None`` (never). A store
    absent from it, or mapped to ``None``, keeps its rows. A failing store is
    logged and retried at the next interval; the others still run.
    """

    def __init__(
        self,
        registry: SubjectDataRegistry,
        *,
        retention: Mapping[str, int | None],
        interval_s: float,
    ) -> None:
        unknown = sorted(set(retention) - set(registry.stores))
        if unknown:
            raise ValueError(
                f"retention names unknown subject data store(s): {', '.join(unknown)}"
            )
        self._registry = registry
        self._retention = dict(retention)
        self._interval_s = interval_s
        self._task: asyncio.Task[None] | None = None

    async def run_once(self) -> dict[str, int]:
        purged: dict[str, int] = {}
        for name, retention_s in self._retention.items():
            store = self._registry.stores[name]
            if retention_s is None:
                continue
            if not isinstance(store, RetainedStore):
                logger.warning("subject_data.store_not_purgeable", store=name)
                continue
            try:
                purged[name] = await store.purge(retention_s=retention_s)
            except Exception:
                logger.warning("subject_data.purge_failed", store=name, exc_info=True)
        return purged

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="subject-data-purger")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _run(self) -> None:
        while True:
            await self.run_once()
            await asyncio.sleep(self._interval_s)
