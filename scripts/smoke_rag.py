"""Smoke test (D-011): exercise the REAL RAG catalog connector end-to-end.

Drives the D-010 async connector (``build_badie_rag_registry`` ->
``catalog_search``) against the populated ``catalog_embeddings`` table
(``database_url``) using the real BGE-M3 embedder. This proves the stub->RAG
roadmap works on real data: a colloquial WhatsApp-style query is embedded,
matched by cosine similarity over pgvector, and classified.

Usage::

    PYTHONUNBUFFERED=1 uv run python scripts/smoke_rag.py
    PYTHONUNBUFFERED=1 uv run python scripts/smoke_rag.py "cerveza rubia" "vino malbec"

Pass queries as CLI args, or omit them for a default colloquial set.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import async_sessionmaker

from agentsys.config import get_settings
from agentsys.connectors.rag_connector import build_badie_rag_registry
from agentsys.models.base import get_engine
from agentsys.observability import setup_logging
from agentsys.services.embeddings import get_embedding_provider

logger = structlog.get_logger()

# DISTILLED product intents — the short queries the LLM extracts from a
# customer's colloquial message before calling the catalog tool. The retriever
# matches on intent keywords, not raw conversational phrases: filler words
# ("che", "bien helada", "para la noche") dilute the embedding and drag matches
# off-target. For the end-to-end criollo flow (where the LLM does the
# distilling), see scripts/smoke_chat.py.
DEFAULT_QUERIES = [
    "cerveza rubia",
    "vino tinto malbec",
    "coca cola sin azucar",
    "agua sin gas",
    "whisky",
    "vino blanco seco",
    "vino dulce de postre",
    "aperitivo campari",
]


def _print_result(query: str, output: dict[str, Any]) -> None:
    classification = output.get("classification")
    results = output.get("results", [])
    print()
    print(f"query: {query!r}")
    print(f"  classification: {classification}")
    if not results:
        print("  (no matches)")
        return
    for rank, item in enumerate(results, 1):
        similarity = item.get("similarity")
        score = f"{similarity:.4f}" if isinstance(similarity, (int, float)) else "n/a"
        print(f"  {rank}. [{score}] {item['description']}  (sku={item['sku']})")


async def main() -> int:
    setup_logging()
    settings = get_settings()
    queries = sys.argv[1:] or DEFAULT_QUERIES

    logger.info(
        "smoke_rag.start",
        provider=settings.embedding_provider,
        queries=len(queries),
    )

    # Real embedder (BGE-M3 by default) — loaded once and reused for all queries.
    embedder = get_embedding_provider(settings)

    # The REAL D-010 connector, wired exactly as production wires it.
    registry = build_badie_rag_registry(settings, embedder)
    connector = registry.get("catalog_search").connector

    # Turn-scoped sessions over the bot DB, where catalog_embeddings lives.
    engine = get_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    print("=" * 72)
    print("D-011 smoke_rag — real connector over catalog_embeddings")
    print("=" * 72)

    try:
        for query in queries:
            async with session_factory() as session:
                output = await connector({"q": query}, session=session)
            _print_result(query, output)
    finally:
        await engine.dispose()

    print()
    print("=" * 72)
    logger.info("smoke_rag.complete", queries=len(queries))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
