"""Async RAG catalog connector (D-010).

Wraps services.rag.search_catalog into a harness connector following the
D-009 async contract: ``async def connector(inputs, *, session) -> dict``.
The connector is READ-ONLY and never commits — the orchestrator owns the
turn-scoped session and its transaction (per D-009).
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from agents_system.config import Settings
from agents_system.services.embeddings import EmbeddingProvider
from agents_system.services.rag import (
    CatalogSearchResult,
    CatalogSource,
    search_catalog,
)

ConnectorOutput = dict[str, Any]
AsyncConnector = Callable[..., Awaitable[ConnectorOutput]]


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
