"""Opt-in reference backends for platform tools (issues #113 and #170).

The original four implementations exercise the `KnowledgeBase`,
`ConversationSummarizer`, `EscalationChannel`, and `OrderWriter` ports without
making them production defaults. `ReferenceBackends` adds explicit ToolSpecs
for catalog, client, report, and message tools over the existing disposable
PostgreSQL demo company. None are registered unless a consumer opts in.

The demo source is read-only and its message ledger is process-local: no
reference sender contacts a provider or claims delivery. These are reference
implementations, not production integrations; see
`docs/platform/reference-backends.md` for their contracts and wiring.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import structlog
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from agentsys.connectors.report_connector import build_report_tool_spec
from agentsys.connectors.sales_reports import CATALOG
from agentsys.harness.registry import Tier, ToolSpec

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


# ---------------------------------------------------------------------------
# DemoReferenceBackends -> portable demo/company data (issue #170)
# ---------------------------------------------------------------------------


_CATALOG_SEARCH_SQL = text(
    """
    SELECT codigo_articulo AS sku, detalle AS description
    FROM articulos
    WHERE strpos(lower(codigo_articulo), lower(:query)) > 0
       OR strpos(lower(detalle), lower(:query)) > 0
    ORDER BY codigo_articulo
    LIMIT :limit
    """
)
_CLIENT_LOOKUP_SQL = text(
    """
    SELECT nro_cliente AS client_id, razon_social AS name
    FROM padron_clientes
    WHERE nro_cliente = :client_id
    """
)
_SYNTHETIC_PHONE = re.compile(r"^\+549110000(\d{4})$")
_REFERENCE_DATA_NOT_CONFIGURED = (
    "Reference demo data is not configured, so this lookup cannot be run."
)


class ReferenceBackends:
    """Explicitly opt-in tools over the existing demo database and reports.

    This class never registers tools itself. A deployment explicitly selects the
    individual :class:`ToolSpec` values it needs, and supplies a read-only engine
    for the existing demo/company PostgreSQL schema. Messages are retained only
    in this process; no message provider is configured or called.
    """

    def __init__(self, readonly_engine: AsyncEngine | None) -> None:
        self._readonly_engine = readonly_engine
        self._message_counter = itertools.count(1)
        self._recorded_messages: list[dict[str, str]] = []

    @property
    def recorded_messages(self) -> list[dict[str, str]]:
        """Return copies of the messages recorded during this process."""
        return [dict(message) for message in self._recorded_messages]

    @staticmethod
    def _not_configured() -> dict[str, str]:
        return {
            "error": _REFERENCE_DATA_NOT_CONFIGURED,
            "error_kind": "reference_data_not_configured",
        }

    async def _fetch_rows(
        self, statement: Any, params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        assert self._readonly_engine is not None
        async with self._readonly_engine.connect() as connection:
            result = await connection.execute(statement, params)
            return [dict(row) for row in result.mappings().all()]

    async def _lookup_client(self, phone: Any) -> dict[str, Any]:
        if self._readonly_engine is None:
            return self._not_configured()
        match = _SYNTHETIC_PHONE.fullmatch(phone) if isinstance(phone, str) else None
        if match is None:
            return {"client_id": None, "name": None, "phone": phone}

        try:
            client_id = int(match.group(1))
        except ValueError:
            return {"client_id": None, "name": None, "phone": phone}
        rows = await self._fetch_rows(_CLIENT_LOOKUP_SQL, {"client_id": client_id})
        if not rows:
            return {"client_id": None, "name": None, "phone": phone}

        row = rows[0]
        resolved_id = str(row["client_id"])
        try:
            derived_phone = f"+549110000{int(resolved_id):04d}"
        except (TypeError, ValueError):
            return {"client_id": None, "name": None, "phone": phone}
        if derived_phone != phone:
            return {"client_id": None, "name": None, "phone": phone}
        return {"client_id": resolved_id, "name": row["name"], "phone": phone}

    def catalog_search_tool_spec(self) -> ToolSpec:
        """Return the lexical catalog lookup tool over ``articulos``."""

        async def catalog_search(
            inputs: dict[str, Any], *, session: Any = None
        ) -> dict[str, Any]:
            query = str(inputs.get("q") or "").strip()
            if not query:
                return {"results": [], "classification": "no_match"}
            limit = inputs.get("limit", 10)
            if not isinstance(limit, int) or isinstance(limit, bool):
                limit = 10
            if self._readonly_engine is None:
                return self._not_configured()
            rows = await self._fetch_rows(
                _CATALOG_SEARCH_SQL,
                {"query": query, "limit": max(1, min(limit, 50))},
            )
            return {
                "results": [
                    {
                        "sku": str(row["sku"]),
                        "description": row["description"],
                        "similarity": None,
                    }
                    for row in rows
                ],
                "classification": "ambiguous" if rows else "no_match",
            }

        return ToolSpec(
            name="catalog_search",
            description="Lexically search the existing demo catalog by SKU or description.",
            required_permissions=("read:catalog",),
            input_schema={
                "type": "object",
                "properties": {
                    "q": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                },
                "required": ["q"],
            },
            connector=catalog_search,
            tier=Tier.T1,
        )

    def client_lookup_tool_spec(self) -> ToolSpec:
        """Return the client lookup tool over ``padron_clientes``."""

        async def client_lookup(
            inputs: dict[str, Any], *, session: Any = None
        ) -> dict[str, Any]:
            return await self._lookup_client(inputs.get("phone"))

        return ToolSpec(
            name="client_lookup",
            description="Look up an existing demo client and derive its synthetic phone.",
            required_permissions=("read:client_registry",),
            input_schema={
                "type": "object",
                "properties": {
                    "phone": {
                        "type": "string",
                        "description": (
                            "Demo customer phone: +549110000 followed by a "
                            "four-digit customer ID, e.g. +5491100000001"
                        ),
                    }
                },
                "required": ["phone"],
            },
            connector=client_lookup,
            tier=Tier.T1,
        )

    def run_report_tool_spec(self) -> ToolSpec:
        """Return the existing closed sales-report catalog without alteration."""
        return build_report_tool_spec(self._readonly_engine, CATALOG)

    def message_sender_tool_spec(self) -> ToolSpec:
        """Return an in-process-only sender that validates demo recipients."""

        async def message_sender(
            inputs: dict[str, Any], *, session: Any = None
        ) -> dict[str, Any]:
            recipient = inputs.get("to")
            client = await self._lookup_client(recipient)
            if client.get("error_kind"):
                return client
            if client["client_id"] is None:
                return {"status": "not_found", "to": recipient}
            message_id = f"recorded-message-{next(self._message_counter):06d}"
            self._recorded_messages.append(
                {
                    "message_id": message_id,
                    "to": client["phone"],
                    "text": str(inputs.get("text") or ""),
                }
            )
            return {
                "status": "recorded",
                "message_id": message_id,
                "to": client["phone"],
            }

        return ToolSpec(
            name="message_sender",
            description=(
                "Record a message for an existing demo client in this process only; "
                "it is never sent or delivered."
            ),
            required_permissions=("send:message",),
            input_schema={
                "type": "object",
                "properties": {
                    "to": {
                        "type": "string",
                        "description": "Recipient phone number",
                    },
                    "text": {
                        "type": "string",
                        "description": "Message text to send",
                    },
                },
                "required": ["to", "text"],
            },
            connector=message_sender,
            tier=Tier.T2,
        )
