"""Reference backends for the platform-generic ports (issue #113 / ADR-002 C.15).

`services.knowledge.KnowledgeBase`, `services.summaries.ConversationSummarizer`,
`services.escalation.EscalationChannel`, and `services.orders.OrderWriter` are
`Protocol` ports whose own modules deliberately ship no implementation — a
consuming business's content, transcript store, human channel, and order
system are never the library's to guess (see each module's docstring).

That left every one of those ports with literally nothing behind it, in any
environment, including tests: every test in this repository that exercises a
knowledge/summary/escalation/order tool necessarily exercised only the
fail-closed path in `connectors/platform_connectors.py` and
`connectors/order_connector.py`. This module closes that gap with reference
implementations the library ships, that a consumer opts INTO — nothing here
is wired by default, so the fail-closed behaviour those connectors already
implement is unchanged unless a consumer explicitly passes one of these
classes to a `build_*_tool_spec` call. See `docs/platform/reference-backends.md`
for wiring examples.

These are reference backends, not production integrations: in-memory storage
that does not survive a process restart, and no external system behind any of
them (`manifesto.md`'s platform/client boundary is why a real paging service
or vector database is a client delivery's job, not the library's).
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Any, Iterable

import structlog
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage

_logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# knowledge_retrieval -> InMemoryKnowledgeBase
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KnowledgeDocument:
    """One markdown document the in-memory knowledge base is seeded with."""

    id: str
    title: str
    content: str


def _snippet(content: str, limit: int = 240) -> str:
    text = " ".join(content.split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


class InMemoryKnowledgeBase:
    """Reference `KnowledgeBase`: markdown documents held in process memory.

    Seed it at construction or via `add_document`. `search` does a
    case-insensitive substring match over title + content — enough to
    exercise the `knowledge_retrieval` contract end to end, not a retrieval
    strategy a production deployment should keep. A miss returns an empty
    `results` list: per the port's contract, empty is a legitimate answer,
    never substituted with unrelated hits.
    """

    def __init__(self, documents: Iterable[KnowledgeDocument] = ()) -> None:
        self._documents: dict[str, KnowledgeDocument] = {
            doc.id: doc for doc in documents
        }

    def add_document(self, document: KnowledgeDocument) -> None:
        self._documents[document.id] = document

    async def search(self, session: Any, *, query: str) -> dict[str, Any]:
        needle = query.casefold()
        hits = [
            {"id": doc.id, "title": doc.title, "snippet": _snippet(doc.content)}
            for doc in self._documents.values()
            if needle in doc.title.casefold() or needle in doc.content.casefold()
        ]
        return {"results": hits}


# ---------------------------------------------------------------------------
# escalation_notifier -> LoggingEscalationChannel
# ---------------------------------------------------------------------------


class LoggingEscalationChannel:
    """Reference `EscalationChannel`: writes a structured log/audit entry.

    This does NOT reach a human. There is no paging system, no Slack, no SMS
    behind it — it logs and returns `status: "logged"`, deliberately never
    `"notified"`, so nothing downstream can read this as a human having
    actually been reached. A consumer that needs a real human channel wires a
    different `EscalationChannel`; this one exists so the `escalation_notifier`
    tool's bound path is exercisable at all.
    """

    def __init__(self) -> None:
        self._counter = itertools.count(1)

    async def notify(
        self, session: Any, *, reason: str, details: str
    ) -> dict[str, Any]:
        escalation_id = f"logged-escalation-{next(self._counter):06d}"
        _logger.info(
            "escalation.logged",
            escalation_id=escalation_id,
            reason=reason,
            details=details,
        )
        return {"escalation_id": escalation_id, "status": "logged"}


# ---------------------------------------------------------------------------
# order_writer -> InMemoryOrderWriter
# ---------------------------------------------------------------------------


class InMemoryOrderWriter:
    """Reference `OrderWriter`: orders held in process memory.

    Persistence lasts for the life of the process, never a database — a real
    deployment supplies its own writer over its own schema. `get_order` is a
    reference-only convenience (outside the `OrderWriter` protocol) so a
    caller can prove a created order is actually retrievable.
    """

    def __init__(self) -> None:
        self._orders: dict[str, dict[str, Any]] = {}
        self._counter = itertools.count(1)

    async def create_order(
        self, session: Any, *, client_id: str, items: list[dict[str, Any]]
    ) -> dict[str, Any]:
        order_id = f"mem-order-{next(self._counter):06d}"
        order: dict[str, Any] = {
            "order_id": order_id,
            "client_id": client_id,
            "items": list(items),
            "status": "created",
        }
        self._orders[order_id] = dict(order)
        return order

    def get_order(self, order_id: str) -> dict[str, Any] | None:
        order = self._orders.get(order_id)
        return dict(order) if order is not None else None


# ---------------------------------------------------------------------------
# conversation_summarizer -> LLMConversationSummarizer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TranscriptTurn:
    """One stored message this reference summarizer can be seeded with."""

    role: str
    text: str


class LLMConversationSummarizer:
    """Reference `ConversationSummarizer`: summarizes with the role's own LLM.

    `model` MUST be the same `BaseChatModel` instance (or an equivalently
    configured one) the calling role's `AgentRuntime` already uses — this
    class adds no provider config of its own, per ADR-002 C.15.

    Storage is in-memory and keyed by `session_id`; seed it with
    `seed_session` (from tests, or from a deployment that mirrors its own
    transcript store into this one). A `session_id` with nothing seeded
    raises `LookupError`, per the port's contract: an implementation that
    cannot read the conversation must raise rather than summarize nothing.
    """

    def __init__(self, model: BaseChatModel, *, default_max_messages: int = 20) -> None:
        self._model = model
        self._default_max_messages = default_max_messages
        self._sessions: dict[str, list[TranscriptTurn]] = {}

    def seed_session(self, session_id: str, turns: Iterable[TranscriptTurn]) -> None:
        self._sessions.setdefault(session_id, []).extend(turns)

    async def summarize(
        self, session: Any, *, session_id: str, max_messages: int | None
    ) -> dict[str, Any]:
        turns = self._sessions.get(session_id)
        if not turns:
            raise LookupError(f"no conversation recorded for session_id={session_id!r}")

        cap = max_messages if max_messages is not None else self._default_max_messages
        bounded = turns[-cap:] if cap and cap > 0 else turns
        transcript = "\n".join(f"{turn.role}: {turn.text}" for turn in bounded)

        response = await self._model.ainvoke(
            [
                HumanMessage(
                    content=(
                        "Summarize the following conversation in 2-3 sentences, "
                        f"from the assistant's point of view:\n\n{transcript}"
                    )
                )
            ]
        )
        content = response.content
        summary = content if isinstance(content, str) else str(content)

        return {
            "session_id": session_id,
            "summary": summary,
            "message_count": len(bounded),
        }
