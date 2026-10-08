"""Per-user memory: durable facts about each end user (#250, ADR-011).

Working memory is the checkpointer thread, and it expires. This is memory
**about the end user**, keyed by (runtime id, subject key) and reached only
through the scope handle (`agents_system.scope`), so a Postgres backend fences
it with row-level security and a query that forgets its filter still sees one
user's rows. It is not agent-own memory (#25, #29).

- Three subject-scoped tools, `remember_user_fact`, `forget_user_fact` and
  `list_user_facts`, receive the handle; the model never passes a user id.
- A bounded summary of the user's facts is added to each turn as an
  untrusted-data block (`agents_system.agent.untrusted`), never as plain text
  and never inside the system prompt: the facts were written from user input.
- Limits (facts per user, bytes per fact, injected size) and the eviction
  rule (least used first, then oldest) are enforced here, by the platform.
- Opt-in per agent through `memory_policy.user_memory` (#26), off by default.

This module depends only on `agents_system.scope`, `subject_data`, the
untrusted-data primitive and the tool registry.
"""

from __future__ import annotations

import dataclasses
import hashlib
from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from agents_system.agent.untrusted import UntrustedData
from agents_system.harness.registry import Tier, ToolSpec
from agents_system.scope import InMemoryScope, InMemoryScopeProvider
from agents_system.subject_data import SubjectRef

USER_FACTS_TABLE = "subject_facts"
#: The source name of the untrusted-data block that carries a user's facts.
USER_FACTS_SOURCE = "user_facts"
#: The name of the store in the subject data registry; also the key of its
#: retention in ``SUBJECT_DATA_RETENTION``.
USER_FACTS_STORE = "user_facts"


class FactRejected(ValueError):
    """The fact cannot be stored; the message is safe to show the model."""


@dataclass(frozen=True)
class UserMemoryLimits:
    """The platform's bounds on one user's memory. Set by the deployment."""

    max_facts: int = 50
    max_fact_bytes: int = 500
    #: Size of the untrusted-data block the facts are injected as, in
    #: characters. Kept under the block primitive's own per-source default.
    max_injected_chars: int = 1_500
    #: Seconds a fact lives after it was written; ``None`` keeps it until it
    #: is forgotten, evicted or the subject is deleted.
    ttl_s: int | None = None

    def __post_init__(self) -> None:
        for name in ("max_facts", "max_fact_bytes", "max_injected_chars"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer, got {value!r}")
        if self.ttl_s is not None and (
            isinstance(self.ttl_s, bool)
            or not isinstance(self.ttl_s, int)
            or self.ttl_s < 1
        ):
            raise ValueError(
                f"ttl_s must be positive seconds or None, got {self.ttl_s!r}"
            )


def limits_from_settings(settings: Any) -> UserMemoryLimits:
    """The deployment's limits: ``USER_MEMORY_*`` and the facts' retention."""
    retention = getattr(settings, "subject_data_retention", None) or {}
    return UserMemoryLimits(
        max_facts=settings.user_memory_max_facts,
        max_fact_bytes=settings.user_memory_max_fact_bytes,
        max_injected_chars=settings.user_memory_max_injected_chars,
        ttl_s=retention.get(USER_FACTS_STORE),
    )


@dataclass(frozen=True)
class UserFact:
    id: str
    content: str
    created_at: datetime
    last_used_at: datetime
    uses: int = 0
    expires_at: datetime | None = None

    def expired(self, now: datetime) -> bool:
        return self.expires_at is not None and self.expires_at <= now


def normalize(content: str) -> str:
    """One line, single spaces: what is stored and compared."""
    return " ".join(content.split())


def fact_id(content: str) -> str:
    """Stable id of a fact's content: remembering it twice is one fact."""
    digest = hashlib.sha256(normalize(content).casefold().encode()).hexdigest()
    return digest[:16]


class UserFactBackend(Protocol):
    """Rows of one (runtime id, subject key), through its scope handle."""

    async def all(self) -> list[UserFact]: ...

    async def put(self, fact: UserFact) -> None: ...

    async def remove(self, ids: Sequence[str]) -> int: ...

    async def touch(self, ids: Sequence[str], now: datetime) -> None: ...

    def serialized(self) -> AbstractAsyncContextManager[None]:
        """Hold this subject exclusively: no other writer interleaves inside.

        A read-modify-write over the subject's facts (remember, then evict to
        the maximum) runs inside it, so concurrent writers cannot both see the
        old state and erase each other's facts.
        """
        ...


class InMemoryFactBackend:
    """Reference backend over `InMemoryScope`: library mode, no Postgres."""

    def __init__(self, scope: InMemoryScope) -> None:
        self._scope = scope

    @asynccontextmanager
    async def serialized(self) -> AsyncIterator[None]:
        async with self._scope.lock:
            yield

    async def all(self) -> list[UserFact]:
        return list(self._scope.rows(USER_FACTS_TABLE).values())

    async def put(self, fact: UserFact) -> None:
        self._scope.put(USER_FACTS_TABLE, fact.id, fact)

    async def remove(self, ids: Sequence[str]) -> int:
        existing = self._scope.rows(USER_FACTS_TABLE)
        removed = [i for i in ids if i in existing]
        for fact_key in removed:
            self._scope.delete(USER_FACTS_TABLE, fact_key)
        return len(removed)

    async def touch(self, ids: Sequence[str], now: datetime) -> None:
        for fact in await self.all():
            if fact.id in ids:
                await self.put(
                    dataclasses.replace(fact, uses=fact.uses + 1, last_used_at=now)
                )


def backend_for(scope: Any) -> UserFactBackend:
    """The fact backend behind a scope handle: the handle decides the storage."""
    if isinstance(scope, InMemoryScope):
        return InMemoryFactBackend(scope)
    from agents_system.services.user_facts_postgres import (
        PostgresFactBackend,
        PostgresScope,
    )

    if isinstance(scope, PostgresScope):
        return PostgresFactBackend(scope)
    raise TypeError(f"no user-fact backend for a {type(scope).__name__} scope")


def _eviction_order(fact: UserFact) -> tuple[int, datetime, datetime]:
    # Least used first; among equally used, the one used (or written) longest ago.
    return (fact.uses, fact.last_used_at, fact.created_at)


class UserFacts:
    """One user's memory: the limits, expiry and eviction over a backend."""

    def __init__(self, backend: UserFactBackend, limits: UserMemoryLimits) -> None:
        self._backend = backend
        self._limits = limits

    async def list(self, *, now: datetime | None = None) -> list[UserFact]:
        """Unexpired facts, newest first. Expired rows are dropped on the way."""
        now = now or datetime.now(UTC)
        rows = await self._backend.all()
        expired = [f.id for f in rows if f.expired(now)]
        if expired:
            await self._backend.remove(expired)
        live = [f for f in rows if not f.expired(now)]
        return sorted(live, key=lambda f: f.created_at, reverse=True)

    async def remember(
        self, content: object, *, now: datetime | None = None
    ) -> UserFact:
        if not isinstance(content, str):
            raise FactRejected("the fact must be text")
        text = normalize(content)
        if not text:
            raise FactRejected("the fact is empty")
        size = len(text.encode())
        if size > self._limits.max_fact_bytes:
            raise FactRejected(
                f"the fact is {size} bytes; the limit is "
                f"{self._limits.max_fact_bytes} bytes. Store a shorter fact"
            )
        async with self._backend.serialized():
            return await self._remember(text, now or datetime.now(UTC))

    async def _remember(self, text: str, now: datetime) -> UserFact:
        key = fact_id(text)
        existing = next((f for f in await self.list(now=now) if f.id == key), None)
        fact = UserFact(
            id=key,
            content=text,
            created_at=existing.created_at if existing else now,
            last_used_at=now,
            uses=existing.uses if existing else 0,
            expires_at=(
                now + timedelta(seconds=self._limits.ttl_s)
                if self._limits.ttl_s is not None
                else None
            ),
        )
        await self._backend.put(fact)
        await self._evict(keep=key, now=now)
        return fact

    async def _evict(self, *, keep: str, now: datetime) -> int:
        rows = await self.list(now=now)
        excess = len(rows) - self._limits.max_facts
        if excess <= 0:
            return 0
        candidates = sorted((f for f in rows if f.id != keep), key=_eviction_order)
        return await self._backend.remove([f.id for f in candidates[:excess]])

    async def forget(self, key: object) -> bool:
        if not isinstance(key, str) or not key:
            return False
        return await self._backend.remove([key]) > 0

    async def touch(self, ids: Sequence[str], *, now: datetime | None = None) -> None:
        await self._backend.touch(ids, now or datetime.now(UTC))

    async def injection_block(
        self, *, now: datetime | None = None, mark_used: bool = False
    ) -> UntrustedData | None:
        """The facts as one untrusted-data block, or ``None`` when there are none.

        Most used first, then most recent, up to ``max_injected_chars``; a fact
        that does not fit is skipped, never cut. With ``mark_used`` the
        injected facts count as used, which is what eviction ranks by.
        """
        now = now or datetime.now(UTC)
        ranked = sorted(
            await self.list(now=now),
            key=lambda f: (f.uses, f.last_used_at),
            reverse=True,
        )
        lines: list[str] = []
        used: list[str] = []
        size = 0
        for fact in ranked:
            line = f"- {fact.content}"
            added = len(line) + (1 if lines else 0)
            if size + added > self._limits.max_injected_chars:
                continue
            lines.append(line)
            used.append(fact.id)
            size += added
        if not lines:
            return None
        if mark_used:
            await self.touch(used, now=now)
        return UntrustedData(
            USER_FACTS_SOURCE,
            "\n".join(lines),
            max_chars=self._limits.max_injected_chars,
        )


class InMemoryUserFactStore:
    """The subject data store of the in-process backend (export and delete)."""

    name = USER_FACTS_STORE
    tables = frozenset({USER_FACTS_TABLE})

    def __init__(self, provider: InMemoryScopeProvider) -> None:
        self._provider = provider

    async def export(self, subject: SubjectRef) -> list[dict[str, Any]]:
        scope = self._provider.scope(subject.runtime_id, subject.subject_key)
        now = datetime.now(UTC)
        return [
            export_row(f)
            for f in await InMemoryFactBackend(scope).all()
            if not f.expired(now)
        ]

    async def purge(self, *, retention_s: int) -> int:
        """Delete every expired fact of every subject (``retention_s`` is unused).

        A fact's lifetime is its own ``expires_at``, set from the configured
        retention when it was written; the purger calls this so the facts of a
        subject who never returns do not stay stored.
        """
        now = datetime.now(UTC)
        return self._provider.sweep(USER_FACTS_TABLE, lambda f: f.expired(now))

    async def delete(self, subject: SubjectRef) -> int:
        scope = self._provider.scope(subject.runtime_id, subject.subject_key)
        backend = InMemoryFactBackend(scope)
        return await backend.remove([f.id for f in await backend.all()])


def export_row(fact: UserFact) -> dict[str, Any]:
    """A fact as the operator export shows it."""
    return {
        "id": fact.id,
        "content": fact.content,
        "created_at": fact.created_at.isoformat(),
        "last_used_at": fact.last_used_at.isoformat(),
        "uses": fact.uses,
        "expires_at": fact.expires_at.isoformat() if fact.expires_at else None,
    }


# --- the tools --------------------------------------------------------------------

_REMEMBER_DESCRIPTION = (
    "Save one short, stable fact about the person you are talking to, so you "
    "remember it in later conversations: a preferred unit or format, an entity "
    "they usually ask about, a recurring request. Save only what the person "
    "told you about themselves, never secrets or one-off details. The fact is "
    "stored for this person only."
)
_FORGET_DESCRIPTION = (
    "Delete one stored fact about the person you are talking to, by the id "
    "list_user_facts returned. Use it when they ask you to forget something."
)
_LIST_DESCRIPTION = (
    "List the facts stored about the person you are talking to, with their ids."
)


def build_user_memory_tool_specs(
    limits: UserMemoryLimits | None = None,
) -> tuple[ToolSpec, ToolSpec, ToolSpec]:
    """The three per-user memory tools, unregistered.

    Each is ``subject_scoped``: the interceptor hands its connector the scope
    handle of the turn's end user, so the tools hold and accept no user id.
    An agent still needs ``memory_policy.user_memory: true`` and a deploy
    grant of ``read:user_memory`` / ``write:user_memory`` to use them.
    """
    bounds = limits or UserMemoryLimits()

    def facts(scope: Any) -> UserFacts:
        return UserFacts(backend_for(scope), bounds)

    async def remember(
        inputs: dict[str, Any], *, session: Any = None, scope: Any
    ) -> dict[str, Any]:
        try:
            fact = await facts(scope).remember(inputs.get("fact"))
        except FactRejected as exc:
            return {"error": str(exc), "error_kind": "fact_rejected"}
        return {"remembered": True, "id": fact.id}

    async def forget(
        inputs: dict[str, Any], *, session: Any = None, scope: Any
    ) -> dict[str, Any]:
        return {"forgotten": await facts(scope).forget(inputs.get("id"))}

    async def list_facts(
        inputs: dict[str, Any], *, session: Any = None, scope: Any
    ) -> dict[str, Any]:
        return {
            "facts": [
                {"id": f.id, "fact": f.content} for f in await facts(scope).list()
            ]
        }

    def spec(**kwargs: Any) -> ToolSpec:
        return ToolSpec(subject_scoped=True, needs_user_memory=True, **kwargs)

    return (
        spec(
            name="remember_user_fact",
            description=_REMEMBER_DESCRIPTION,
            required_permissions=("write:user_memory",),
            input_schema={
                "type": "object",
                "properties": {
                    "fact": {
                        "type": "string",
                        "description": (
                            f"One fact, at most {bounds.max_fact_bytes} bytes, "
                            "for example 'prefers weights in kilograms'"
                        ),
                    }
                },
                "required": ["fact"],
            },
            connector=remember,
            tier=Tier.T2,
        ),
        spec(
            name="forget_user_fact",
            description=_FORGET_DESCRIPTION,
            required_permissions=("write:user_memory",),
            input_schema={
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "A fact id from list_user_facts",
                    }
                },
                "required": ["id"],
            },
            connector=forget,
            tier=Tier.T2,
        ),
        spec(
            name="list_user_facts",
            description=_LIST_DESCRIPTION,
            required_permissions=("read:user_memory",),
            input_schema={"type": "object", "properties": {}},
            connector=list_facts,
            tier=Tier.T0,
        ),
    )
