"""Per-user data registry, export, delete and retention purge (ADR-011 part 6, #257)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agents_system.audit.sink import AuditSink
from agents_system.subject import TurnActor
from agents_system.subject_data import (
    DEFAULT_REGISTRY,
    SubjectDataPurger,
    SubjectDataRegistry,
    SubjectDeletionError,
    SubjectRef,
    delete_subject,
    export_subject,
    parse_retention,
)

RT = "rt"
ALICE = SubjectRef(RT, "participant:1")
BOB = SubjectRef(RT, "participant:2")
OPERATOR = TurnActor(kind="operator", id="ops")


class MemStore:
    """Reference per-user store keyed by (runtime id, subject key)."""

    def __init__(self, name: str, tables: set[str] | None = None) -> None:
        self.name = name
        self.tables = frozenset(tables or {name})
        self.rows: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.purged_with: list[int] = []

    def add(self, subject: SubjectRef, **row: Any) -> None:
        self.rows.setdefault((subject.runtime_id, subject.subject_key), []).append(row)

    async def export(self, subject: SubjectRef) -> list[dict[str, Any]]:
        return list(self.rows.get((subject.runtime_id, subject.subject_key), []))

    async def delete(self, subject: SubjectRef) -> int:
        return len(self.rows.pop((subject.runtime_id, subject.subject_key), []))

    async def purge(self, *, retention_s: int) -> int:
        self.purged_with.append(retention_s)
        return 0


class MemThreads:
    def __init__(self) -> None:
        self.threads: dict[str, list[str]] = {}

    async def export_thread(self, thread_id: str) -> dict[str, Any] | None:
        msgs = self.threads.get(thread_id)
        return None if msgs is None else {"thread_id": thread_id, "messages": msgs}

    async def delete_thread(self, thread_id: str) -> int:
        return 1 if self.threads.pop(thread_id, None) is not None else 0


def _registry() -> tuple[SubjectDataRegistry, MemStore, MemStore]:
    reg = SubjectDataRegistry()
    facts, usage = MemStore("facts"), MemStore("usage")
    reg.register(facts)
    reg.register(usage)
    return reg, facts, usage


def test_register_rejects_duplicate_name_and_exposes_tables() -> None:
    reg, _, _ = _registry()
    assert reg.tables == {"facts", "usage"}
    with pytest.raises(ValueError, match="already registered"):
        reg.register(MemStore("facts"))


async def test_export_and_delete_cover_every_store_and_thread_only_for_the_subject() -> (
    None
):
    reg, facts, usage = _registry()
    threads = MemThreads()
    facts.add(ALICE, text="a1")
    facts.add(BOB, text="b1")
    usage.add(ALICE, tokens=5)
    usage.add(BOB, tokens=7)
    threads.threads = {"t-alice": ["hi"], "t-bob": ["yo"]}

    exported = await export_subject(reg, ALICE, threads=threads, thread_ids=["t-alice"])
    assert exported["subject_key"] == ALICE.subject_key
    assert exported["stores"] == {"facts": [{"text": "a1"}], "usage": [{"tokens": 5}]}
    assert exported["threads"] == [{"thread_id": "t-alice", "messages": ["hi"]}]

    result = await delete_subject(
        reg, ALICE, actor=OPERATOR, threads=threads, thread_ids=["t-alice"]
    )
    assert result["stores"] == {"facts": 1, "usage": 1}
    assert result["threads"] == 1
    assert await export_subject(reg, ALICE) == {
        "runtime_id": RT,
        "subject_key": ALICE.subject_key,
        "stores": {"facts": [], "usage": []},
        "threads": [],
    }
    assert await facts.export(BOB) == [{"text": "b1"}]
    assert await usage.export(BOB) == [{"tokens": 7}]
    assert "t-bob" in threads.threads


async def test_delete_attempts_every_store_then_raises_on_failure() -> None:
    reg, facts, usage = _registry()
    usage.add(ALICE, tokens=1)

    async def boom(subject: SubjectRef) -> int:
        raise RuntimeError("db down")

    facts.delete = boom  # type: ignore[method-assign]
    with pytest.raises(SubjectDeletionError) as err:
        await delete_subject(reg, ALICE, actor=OPERATOR)
    assert err.value.failed == ["facts"]
    assert err.value.deleted == {"usage": 1}
    assert await usage.export(ALICE) == []


class _Sink:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def record(self, event: Any) -> None:
        self.events.append(event)


async def test_delete_is_audited_with_operator_actor_and_subject_key_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sink = _Sink()
    monkeypatch.setattr(AuditSink, "registered", classmethod(lambda cls: sink))
    reg, facts, _ = _registry()
    facts.add(ALICE, text="x")
    await delete_subject(
        reg, ALICE, actor=OPERATOR, thread_ids=["whatsapp-address-5491100000000"]
    )
    (event,) = sink.events
    assert event.event_type == "subject_data_deleted"
    assert event.actor == "operator:ops"
    assert event.subject == ALICE.subject_key
    assert event.payload["stores"] == {"facts": 1, "usage": 0}
    assert "5491100000000" not in repr(event.model_dump())


def test_parse_retention_like_the_checkpointer() -> None:
    assert parse_retention("never") is None
    assert parse_retention(" NEVER ") is None
    assert parse_retention(86400) == 86400
    assert parse_retention("60") == 60
    for bad in (0, -1, "0", "soon", True):
        with pytest.raises(ValueError):
            parse_retention(bad)


async def test_purger_enforces_each_stores_own_retention() -> None:
    reg, facts, usage = _registry()
    purger = SubjectDataPurger(
        reg, retention={"facts": 30, "usage": None}, interval_s=0.01
    )
    await purger.start()
    await asyncio.sleep(0.05)
    await purger.stop()
    assert facts.purged_with and set(facts.purged_with) == {30}
    assert usage.purged_with == []  # never: kept


async def test_purger_survives_a_failing_store() -> None:
    reg, facts, usage = _registry()

    async def boom(*, retention_s: int) -> int:
        raise RuntimeError("x")

    facts.purge = boom  # type: ignore[method-assign]
    purger = SubjectDataPurger(reg, retention={"facts": 1, "usage": 2}, interval_s=0.01)
    await purger.run_once()
    assert usage.purged_with == [2]


def test_purger_refuses_retention_for_an_unknown_or_unpurgeable_store() -> None:
    reg, _, _ = _registry()
    with pytest.raises(ValueError, match="unknown"):
        SubjectDataPurger(reg, retention={"nope": 1}, interval_s=1)


def test_default_registry_starts_empty_for_the_core() -> None:
    assert isinstance(DEFAULT_REGISTRY, SubjectDataRegistry)
