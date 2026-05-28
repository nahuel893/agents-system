"""RAG orchestration for catalog product retrieval."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from badie.config import Settings
from badie.services import catalog
from badie.services.embeddings import EmbeddingProvider

_logger = logging.getLogger(__name__)

Classification = Literal["direct", "ambiguous", "no_match"]
SearchSource = Literal["vector", "keyword_fallback"]


@dataclass(frozen=True)
class CatalogCandidate:
    sku: str
    description: str
    source: SearchSource
    similarity: float | None


@dataclass(frozen=True)
class CatalogSearchResult:
    classification: Classification
    source: SearchSource
    candidates: list[CatalogCandidate]


async def search_catalog(
    session: AsyncSession,
    query: str,
    *,
    settings: Settings,
    embedder: EmbeddingProvider,
) -> CatalogSearchResult:
    """Embed the query once and classify vector candidates by similarity."""
    trimmed_query = _trim_query(query)

    try:
        vectors = await embedder.embed([trimmed_query])
        query_vector = vectors[0] if vectors else []
        if not query_vector:
            return await _keyword_fallback(
                session, trimmed_query, settings
            )
    except Exception:
        return await _keyword_fallback(
            session, trimmed_query, settings
        )

    vector_candidates = await catalog.search_vector(
        session,
        embedding=query_vector,
        limit=settings.rag_top_k,
        ef_search=settings.rag_hnsw_ef_search,
    )
    candidates = [
        CatalogCandidate(
            sku=candidate.sku,
            description=candidate.description,
            source="vector",
            similarity=_distance_to_similarity(candidate.distance),
        )
        for candidate in vector_candidates[: settings.rag_top_k]
    ]

    classification = _classify_candidates(candidates, settings)
    if classification == "no_match":
        return CatalogSearchResult(
            classification=classification,
            source="vector",
            candidates=[],
        )

    return CatalogSearchResult(
        classification=classification,
        source="vector",
        candidates=candidates,
    )


async def _keyword_fallback(
    session: AsyncSession,
    query: str,
    settings: Settings,
) -> CatalogSearchResult:
    _logger.warning(
        "embedding_failure query=%s fallback=keyword",
        query,
    )
    keyword_candidates = await catalog.search_keywords(
        session, query=query, limit=settings.rag_keyword_top_k
    )
    if not keyword_candidates:
        return CatalogSearchResult(
            classification="no_match",
            source="keyword_fallback",
            candidates=[],
        )
    return CatalogSearchResult(
        classification="ambiguous",
        source="keyword_fallback",
        candidates=[
            CatalogCandidate(
                sku=c.sku,
                description=c.description,
                source="keyword_fallback",
                similarity=None,
            )
            for c in keyword_candidates[: settings.rag_keyword_top_k]
        ],
    )


def _trim_query(query: str) -> str:
    return query.strip()


def _distance_to_similarity(distance: float) -> float:
    return round(1 - distance, 6)


def _classify_candidates(
    candidates: list[CatalogCandidate], settings: Settings
) -> Classification:
    if not candidates:
        return "no_match"

    best_similarity = candidates[0].similarity
    if best_similarity is None:
        return "no_match"
    if best_similarity >= settings.rag_threshold_direct:
        return "direct"
    if best_similarity >= settings.rag_threshold_ambiguous:
        return "ambiguous"
    return "no_match"
