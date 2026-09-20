"""Async RAG catalog connector (D-010).

Wraps services.rag.search_catalog into a harness connector following the
D-009 async contract: ``async def connector(inputs, *, session) -> dict``.
The connector is READ-ONLY and never commits — the orchestrator owns the
turn-scoped session and its transaction (per D-009).
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncEngine

from agentsys.config import Settings
from agentsys.connectors.platform_connectors import (
    build_conversation_summarizer_tool_spec,
    build_escalation_notifier_tool_spec,
    build_knowledge_retrieval_tool_spec,
)
from agentsys.connectors.stubs import (
    client_lookup,
    message_sender,
    session_state,
)
from agentsys.connectors.acme_reports import CATALOG as _BI_CATALOG
from agentsys.connectors.order_connector import build_order_writer_tool_spec
from agentsys.connectors.report_connector import build_report_tool_spec
from agentsys.connectors.operator import (
    TerminalPolicy,
    build_operator_tool_specs,
)
from agentsys.harness.registry import ToolRegistry, ToolSpec
from agentsys.services.catalog import CatalogTables
from agentsys.services.orders import OrderWriter
from agentsys.services.embeddings import (
    EmbeddingProvider,
    get_embedding_provider,
)
from agentsys.services.escalation import EscalationChannel
from agentsys.services.knowledge import KnowledgeBase
from agentsys.services.rag import (
    CatalogSearchResult,
    CatalogSource,
    search_catalog,
)
from agentsys.services.summaries import ConversationSummarizer

ConnectorOutput = dict[str, Any]
AsyncConnector = Callable[..., Awaitable[ConnectorOutput]]

_CATALOG_RAG_DESCRIPTION = (
    "Search the product catalog by meaning using semantic vector search. "
    "Returns matching products as {sku, description, similarity} plus a "
    "classification of match confidence: 'direct' (one confident match), "
    "'ambiguous' (several plausible matches — ask the customer to choose), "
    "or 'no_match' (nothing relevant). This tool ONLY finds and identifies "
    "products by name; it does NOT return cost, availability, or inventory. "
    "Never invent monetary values or availability from its output."
)

_CATALOG_RAG_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "q": {
            "type": "string",
            "description": (
                "Natural-language product query, e.g. 'yerba 1 kilo' or "
                "'aceite de girasol'."
            ),
        }
    },
    "required": ["q"],
}


def _map_result(result: CatalogSearchResult) -> ConnectorOutput:
    return {
        "results": [
            {
                "sku": candidate.sku,
                "description": candidate.description,
                "similarity": candidate.similarity,  # float | None (None -> JSON null)
            }
            for candidate in result.candidates
        ],
        "classification": result.classification,
    }


def build_catalog_rag_connector(
    embedder: EmbeddingProvider, settings: Settings, source: CatalogSource
) -> AsyncConnector:
    """Build an async connector closure over the embedder, settings and source.

    *source* is the consumer's catalog storage. It is captured here rather
    than imported by `services.rag` so the retrieval strategy stays free of
    any one deployment's schema.
    """

    async def catalog_search_rag(
        inputs: dict[str, Any], *, session: Any = None
    ) -> ConnectorOutput:
        q = (inputs.get("q") or "").strip()
        if not q:
            return {"results": [], "classification": "no_match"}
        result = await search_catalog(
            session, q, settings=settings, embedder=embedder, source=source
        )
        return _map_result(result)

    return catalog_search_rag


def build_acme_rag_registry(
    settings: Settings,
    embedder: EmbeddingProvider | None = None,
    bi_engine: AsyncEngine | None = None,
    terminal_policy: TerminalPolicy | None = None,
    order_writer: OrderWriter | None = None,
    knowledge_base: KnowledgeBase | None = None,
    summarizer: ConversationSummarizer | None = None,
    escalation_channel: EscalationChannel | None = None,
) -> ToolRegistry:
    """Return a ToolRegistry with the async RAG catalog connector and the rest.

    The three platform-generic tools declared by the generic roles under
    ``platform/roles/`` are always registered and bound only when the
    deployment supplies the corresponding port. Unbound, each one says it is
    not configured — the platform owns no knowledge base, no conversation
    store and no escalation channel (issue #39).

    The embedder is resolved once at build time and captured in the connector
    closure (BGE-M3 is heavy — load once per registry, not per call).
    """
    if embedder is None:
        embedder = get_embedding_provider(settings)

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="catalog_search",
            description=_CATALOG_RAG_DESCRIPTION,
            required_permissions=("read:catalog",),
            input_schema=_CATALOG_RAG_INPUT_SCHEMA,
            connector=build_catalog_rag_connector(
                embedder, settings, CatalogTables()
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="client_lookup",
            description=(
                "Look up a client by phone number. Returns client_id, name, "
                "and phone. Use this before creating an order."
            ),
            required_permissions=("read:client_registry",),
            input_schema={
                "type": "object",
                "properties": {
                    "phone": {
                        "type": "string",
                        "description": (
                            "Client phone number in international format, "
                            "e.g. 5491112345678"
                        ),
                    }
                },
                "required": ["phone"],
            },
            connector=client_lookup,
        )
    )
    # Always present, bound only when the deployment supplies an order system.
    # platform/roles/sales-agent names order_writer, and a tool a manifest
    # names but the registry lacks makes the whole role unbuildable via
    # InjectionError. Unbound, it answers that the order was not created —
    # never the `ord-NNNN` the stub used to mint for an order that existed
    # nowhere (issue #39).
    registry.register(build_order_writer_tool_spec(order_writer))
    registry.register(
        ToolSpec(
            name="message_sender",
            description="Send a WhatsApp message to a phone number.",
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
        )
    )
    registry.register(
        ToolSpec(
            name="session_state",
            description="Get or set session state data for the current conversation.",
            required_permissions=(),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["get", "set"],
                    },
                    "session_id": {"type": "string"},
                    "data": {"type": "object"},
                },
                "required": ["action", "session_id"],
            },
            connector=session_state,
        )
    )
    # Always present, bound only when a verified read-only engine exists.
    # platform/roles/data-agent names run_report, and a tool a manifest
    # names but the registry lacks makes the whole role unbuildable via
    # InjectionError. Unbound, it answers that reporting is unavailable.
    registry.register(build_report_tool_spec(bi_engine, _BI_CATALOG))

    # Always present, bound only when the deployment supplies the system.
    # The generic role manifests name all three, and a tool a manifest names
    # but the registry lacks makes the whole role unbuildable via
    # InjectionError. Unbound, each says so rather than answering: the stubs
    # these replace returned three hardcoded knowledge hits, one fixed summary
    # sentence, and `esc-NNNN` for an escalation no human ever received.
    registry.register(build_knowledge_retrieval_tool_spec(knowledge_base))
    registry.register(build_conversation_summarizer_tool_spec(summarizer))
    registry.register(build_escalation_notifier_tool_spec(escalation_channel))
    # The operator tools, registered INERT: `build_operator_tool_specs()` with
    # no policy refuses every command and roots reads at the process cwd.
    # `platform/roles/operator-agent` names both, and a tool a manifest names
    # but the registry lacks makes the whole role unbuildable -- so the choice
    # is between an inert tool and no operator role at all. An application
    # that wants real terminal access builds its own `TerminalPolicy` and
    # registers these specs itself.
    for spec in build_operator_tool_specs(terminal_policy):
        registry.register(spec)

    return registry
