"""Catalog retrieval query helpers for vector and keyword search."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from agentsys.models.tables import CatalogEmbedding


@dataclass(frozen=True)
class VectorSearchCandidate:
    sku: str
    description: str
    distance: float


@dataclass(frozen=True)
class KeywordSearchCandidate:
    sku: str
    description: str


async def search_vector(
    session: AsyncSession,
    *,
    embedding: list[float],
    limit: int,
    ef_search: int,
) -> list[VectorSearchCandidate]:
    """Return active catalog candidates ordered by cosine distance."""
    await session.execute(
        text("SELECT set_config('hnsw.ef_search', :value, true)"),
        {"value": str(ef_search)},
    )

    distance = CatalogEmbedding.embedding.cosine_distance(embedding)
    result = await session.execute(
        select(
            CatalogEmbedding.sku.label("sku"),
            CatalogEmbedding.description.label("description"),
            distance.label("distance"),
        )
        .where(CatalogEmbedding.active.is_(True))
        .order_by(distance)
        .limit(limit)
    )

    rows = result.mappings().all()
    return [_to_vector_candidate(row) for row in rows]


async def search_keywords(
    session: AsyncSession,
    *,
    query: str,
    limit: int,
) -> list[KeywordSearchCandidate]:
    """Return active catalog rows matching any normalized keyword term."""
    terms = sorted({t.lower().strip() for t in query.split() if t.strip()})
    if not terms:
        return []

    predicates = [
        CatalogEmbedding.description.ilike(f"%{term}%") for term in terms
    ]
    result = await session.execute(
        select(
            CatalogEmbedding.sku.label("sku"),
            CatalogEmbedding.description.label("description"),
        )
        .where(CatalogEmbedding.active.is_(True))
        .where(or_(*predicates))
        .limit(limit)
    )

    rows = result.mappings().all()
    return [KeywordSearchCandidate(sku=str(r["sku"]), description=str(r["description"])) for r in rows]


def _to_vector_candidate(row: Any) -> VectorSearchCandidate:
    return VectorSearchCandidate(
        sku=str(row["sku"]),
        description=str(row["description"]),
        distance=float(row["distance"]),
    )
