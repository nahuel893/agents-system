"""Async RAG catalog connector (D-010).

Wraps services.rag.search_catalog into a harness connector following the
D-009 async contract: ``async def connector(inputs, *, session) -> dict``.
The connector is READ-ONLY and never commits — the orchestrator owns the
turn-scoped session and its transaction (per D-009).
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from agentsys.config import Settings
from agentsys.connectors.stubs import (
    client_lookup,
    message_sender,
    order_writer,
    session_state,
)
from agentsys.harness.registry import ToolRegistry, ToolSpec
from agentsys.services.embeddings import (
    EmbeddingProvider,
    get_embedding_provider,
)
from agentsys.services.rag import CatalogSearchResult, search_catalog

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
    embedder: EmbeddingProvider, settings: Settings
) -> AsyncConnector:
    """Build an async connector closure capturing the embedder and settings."""

    async def catalog_search_rag(
        inputs: dict[str, Any], *, session: Any = None
    ) -> ConnectorOutput:
        q = (inputs.get("q") or "").strip()
        if not q:
            return {"results": [], "classification": "no_match"}
        result = await search_catalog(
            session, q, settings=settings, embedder=embedder
        )
        return _map_result(result)

    return catalog_search_rag


def build_badie_rag_registry(
    settings: Settings, embedder: EmbeddingProvider | None = None
) -> ToolRegistry:
    """Return a ToolRegistry with the async RAG catalog connector and 4 sync stubs.

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
            connector=build_catalog_rag_connector(embedder, settings),
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
    registry.register(
        ToolSpec(
            name="order_writer",
            description=(
                "Create a new order for a client. Requires client_id "
                "(from client_lookup) and a list of items with product_id and qty."
            ),
            required_permissions=("write:orders", "write:order_items"),
            input_schema={
                "type": "object",
                "properties": {
                    "client_id": {
                        "type": "string",
                        "description": "Client ID obtained from client_lookup",
                    },
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "product_id": {"type": "string"},
                                "qty": {"type": "integer"},
                            },
                            "required": ["product_id", "qty"],
                        },
                        "description": "List of products to order",
                    },
                },
                "required": ["client_id", "items"],
            },
            connector=order_writer,
        )
    )
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
    return registry
