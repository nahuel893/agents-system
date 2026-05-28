"""Unit tests for RAG catalog retrieval orchestration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from badie.config import Settings
from badie.services import rag
from badie.services.catalog import (
    KeywordSearchCandidate,
    VectorSearchCandidate,
)


@dataclass
class StubEmbedder:
    vectors: list[list[float]]

    def __post_init__(self) -> None:
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return self.vectors


@dataclass
class RaisingStubEmbedder:
    calls: list[list[str]] = field(default_factory=list)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        msg = "embedding service unavailable"
        raise RuntimeError(msg)


async def test_search_catalog_fallback_on_embed_empty_vector(
    monkeypatch,
) -> None:
    settings = Settings(
        _env_file=None,
        rag_threshold_direct=0.92,
        rag_threshold_ambiguous=0.82,
        rag_keyword_top_k=3,
    )
    embedder = StubEmbedder(vectors=[])

    async def fake_search_keywords(
        session: Any, *, query: str, limit: int
    ) -> list[KeywordSearchCandidate]:
        return [
            KeywordSearchCandidate("SKU-K1", "Quilmes 1L"),
            KeywordSearchCandidate("SKU-K2", "Quilmes 970cc"),
        ]

    monkeypatch.setattr(rag.catalog, "search_keywords", fake_search_keywords)

    result = await rag.search_catalog(
        object(),
        "quilmes",
        settings=settings,
        embedder=embedder,
    )

    assert result.classification == "ambiguous"
    assert result.source == "keyword_fallback"
    assert len(result.candidates) == 2
    assert result.candidates[0].sku == "SKU-K1"
    assert result.candidates[0].similarity is None
    assert result.candidates[1].similarity is None


async def test_search_catalog_fallback_on_embed_exception(
    monkeypatch,
) -> None:
    settings = Settings(
        _env_file=None,
        rag_threshold_direct=0.92,
        rag_threshold_ambiguous=0.82,
        rag_keyword_top_k=3,
    )
    embedder = RaisingStubEmbedder()

    async def fake_search_keywords(
        session: Any, *, query: str, limit: int
    ) -> list[KeywordSearchCandidate]:
        return [KeywordSearchCandidate("SKU-K3", "Coca-Cola 2L")]

    monkeypatch.setattr(rag.catalog, "search_keywords", fake_search_keywords)

    result = await rag.search_catalog(
        object(),
        "coca",
        settings=settings,
        embedder=embedder,
    )

    assert result.classification == "ambiguous"
    assert result.source == "keyword_fallback"
    assert len(result.candidates) == 1
    assert result.candidates[0].sku == "SKU-K3"


async def test_search_catalog_fallback_returns_no_match_on_empty_keywords(
    monkeypatch,
) -> None:
    settings = Settings(
        _env_file=None,
        rag_threshold_direct=0.92,
        rag_threshold_ambiguous=0.82,
        rag_keyword_top_k=3,
    )
    embedder = StubEmbedder(vectors=[])

    async def fake_search_keywords(
        session: Any, *, query: str, limit: int
    ) -> list[KeywordSearchCandidate]:
        return []

    monkeypatch.setattr(rag.catalog, "search_keywords", fake_search_keywords)

    result = await rag.search_catalog(
        object(),
        "xyzzy",
        settings=settings,
        embedder=embedder,
    )

    assert result.classification == "no_match"
    assert result.source == "keyword_fallback"
    assert result.candidates == []


async def test_search_catalog_fallback_respects_keyword_top_k_cap(
    monkeypatch,
) -> None:
    settings = Settings(
        _env_file=None,
        rag_threshold_direct=0.92,
        rag_threshold_ambiguous=0.82,
        rag_keyword_top_k=2,
    )
    embedder = RaisingStubEmbedder()

    async def fake_search_keywords(
        session: Any, *, query: str, limit: int
    ) -> list[KeywordSearchCandidate]:
        return [
            KeywordSearchCandidate(f"SKU-{i}", f"desc-{i}") for i in range(5)
        ]

    monkeypatch.setattr(rag.catalog, "search_keywords", fake_search_keywords)

    result = await rag.search_catalog(
        object(),
        "beer",
        settings=settings,
        embedder=embedder,
    )

    assert result.classification == "ambiguous"
    assert len(result.candidates) == 2
    assert result.candidates[0].sku == "SKU-0"


async def test_search_catalog_fallback_logs_once_on_embed_failure(
    monkeypatch, caplog: pytest.LogCaptureFixture
) -> None:
    settings = Settings(
        _env_file=None,
        rag_threshold_direct=0.92,
        rag_threshold_ambiguous=0.82,
        rag_keyword_top_k=3,
    )
    embedder = RaisingStubEmbedder()

    async def fake_search_keywords(
        session: Any, *, query: str, limit: int
    ) -> list[KeywordSearchCandidate]:
        return []

    monkeypatch.setattr(rag.catalog, "search_keywords", fake_search_keywords)

    await rag.search_catalog(
        object(),
        "nonexistent",
        settings=settings,
        embedder=embedder,
    )

    assert len(caplog.records) == 1
    assert "embedding_failure" in caplog.text
    assert "nonexistent" in caplog.text


async def test_search_catalog_fallback_does_not_retry_embed(
    monkeypatch,
) -> None:
    settings = Settings(
        _env_file=None,
        rag_threshold_direct=0.92,
        rag_threshold_ambiguous=0.82,
        rag_keyword_top_k=3,
    )
    embedder = RaisingStubEmbedder()

    async def fake_search_keywords(
        session: Any, *, query: str, limit: int
    ) -> list[KeywordSearchCandidate]:
        return []

    monkeypatch.setattr(rag.catalog, "search_keywords", fake_search_keywords)

    await rag.search_catalog(
        object(),
        "no-retry",
        settings=settings,
        embedder=embedder,
    )

    assert len(embedder.calls) == 1  # exactly one embed attempt, no retry


async def test_search_catalog_returns_direct_vector_match_and_caps_top_k(
    monkeypatch,
) -> None:
    settings = Settings(
        _env_file=None,
        rag_threshold_direct=0.92,
        rag_threshold_ambiguous=0.82,
        rag_top_k=2,
        rag_hnsw_ef_search=77,
    )
    embedder = StubEmbedder(vectors=[[0.1, 0.2, 0.3]])
    captured_call: dict[str, object] = {}

    async def fake_search_vector(session, *, embedding, limit, ef_search):
        captured_call.update(
            {
                "session": session,
                "embedding": embedding,
                "limit": limit,
                "ef_search": ef_search,
            }
        )
        return [
            VectorSearchCandidate("SKU-1", "Quilmes Cristal 1L", 0.04),
            VectorSearchCandidate("SKU-2", "Quilmes Lager 970cc", 0.08),
            VectorSearchCandidate("SKU-3", "Cerveza rubia lata", 0.12),
        ]

    monkeypatch.setattr(rag.catalog, "search_vector", fake_search_vector)

    session = object()
    result = await rag.search_catalog(
        session,
        "  quilmes 1 litro  ",
        settings=settings,
        embedder=embedder,
    )

    assert embedder.calls == [["quilmes 1 litro"]]
    assert captured_call == {
        "session": session,
        "embedding": [0.1, 0.2, 0.3],
        "limit": 2,
        "ef_search": 77,
    }
    assert result.classification == "direct"
    assert result.source == "vector"
    assert len(result.candidates) == 2
    assert result.candidates[0].sku == "SKU-1"
    assert result.candidates[0].description == "Quilmes Cristal 1L"
    assert result.candidates[0].source == "vector"
    assert result.candidates[0].similarity == 0.96
    assert result.candidates[1].similarity == 0.92


async def test_search_catalog_returns_ambiguous_for_mid_band_similarity(
    monkeypatch,
) -> None:
    settings = Settings(
        _env_file=None,
        rag_threshold_direct=0.92,
        rag_threshold_ambiguous=0.82,
    )
    embedder = StubEmbedder(vectors=[[0.5, 0.4, 0.3]])

    async def fake_search_vector(session, *, embedding, limit, ef_search):
        return [
            VectorSearchCandidate("SKU-9", "Coca-Cola 2.25L", 0.15),
            VectorSearchCandidate("SKU-10", "Coca-Cola Zero 2.25L", 0.17),
        ]

    monkeypatch.setattr(rag.catalog, "search_vector", fake_search_vector)

    result = await rag.search_catalog(
        object(),
        "coca cola",
        settings=settings,
        embedder=embedder,
    )

    assert result.classification == "ambiguous"
    assert result.source == "vector"
    assert [candidate.sku for candidate in result.candidates] == ["SKU-9", "SKU-10"]
    assert result.candidates[0].similarity == 0.85


async def test_search_catalog_returns_no_match_and_no_candidates_below_threshold(
    monkeypatch,
) -> None:
    settings = Settings(
        _env_file=None,
        rag_threshold_direct=0.92,
        rag_threshold_ambiguous=0.82,
    )
    embedder = StubEmbedder(vectors=[[0.9, 0.1, 0.3]])

    async def fake_search_vector(session, *, embedding, limit, ef_search):
        return [
            VectorSearchCandidate("SKU-50", "Agua mineral 500ml", 0.25),
            VectorSearchCandidate("SKU-51", "Soda 1.5L", 0.28),
        ]

    monkeypatch.setattr(rag.catalog, "search_vector", fake_search_vector)

    result = await rag.search_catalog(
        object(),
        "agua con gas",
        settings=settings,
        embedder=embedder,
    )

    assert result.classification == "no_match"
    assert result.source == "vector"
    assert result.candidates == []
