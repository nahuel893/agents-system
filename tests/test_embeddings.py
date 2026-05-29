"""Tests for the embedding service abstraction (Protocol + Fake + OpenAI + Local)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np

from agentsys.config import Settings
from agentsys.services.embeddings import (
    EmbeddingProvider,
    FakeEmbeddingProvider,
    LocalBGEEmbeddingProvider,
    OpenAIEmbeddingProvider,
    get_embedding_provider,
)


# ---------------------------------------------------------------------------
# FakeEmbeddingProvider — used in tests to avoid real API calls
# ---------------------------------------------------------------------------


async def test_fake_provider_returns_correct_dimensions() -> None:
    """Vectors have exactly the configured dimensions."""
    provider = FakeEmbeddingProvider(dimensions=512)
    vectors = await provider.embed(["Quilmes 1L"])
    assert len(vectors) == 1
    assert len(vectors[0]) == 512


async def test_fake_provider_deterministic() -> None:
    """Same input must always yield the same vector."""
    provider = FakeEmbeddingProvider(dimensions=512)
    v1 = await provider.embed(["Quilmes 1L"])
    v2 = await provider.embed(["Quilmes 1L"])
    assert v1 == v2


async def test_fake_provider_different_inputs_different_vectors() -> None:
    """Different texts must yield different vectors."""
    provider = FakeEmbeddingProvider(dimensions=512)
    [v1] = await provider.embed(["Quilmes 1L"])
    [v2] = await provider.embed(["Brahma lata"])
    assert v1 != v2


async def test_fake_provider_batch() -> None:
    """Batch of N inputs returns N vectors in matching order."""
    provider = FakeEmbeddingProvider(dimensions=512)
    texts = ["Quilmes 1L", "Brahma lata", "Stella retornable"]
    vectors = await provider.embed(texts)
    assert len(vectors) == 3
    assert all(len(v) == 512 for v in vectors)
    # Order preserved: re-embedding individually matches batch position
    [v0_solo] = await provider.embed([texts[0]])
    assert vectors[0] == v0_solo


async def test_fake_provider_empty_input() -> None:
    """Empty list returns empty list."""
    provider = FakeEmbeddingProvider(dimensions=512)
    assert await provider.embed([]) == []


# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------


def test_fake_provider_implements_protocol() -> None:
    """FakeEmbeddingProvider satisfies the EmbeddingProvider Protocol."""
    provider: EmbeddingProvider = FakeEmbeddingProvider(dimensions=512)
    assert hasattr(provider, "embed")


def test_openai_provider_implements_protocol() -> None:
    """OpenAIEmbeddingProvider satisfies the EmbeddingProvider Protocol."""
    provider: EmbeddingProvider = OpenAIEmbeddingProvider(
        api_key="sk-test", model="text-embedding-3-small", dimensions=512
    )
    assert hasattr(provider, "embed")


# ---------------------------------------------------------------------------
# LocalBGEEmbeddingProvider — uses sentence-transformers (mocked in tests)
# ---------------------------------------------------------------------------


@patch("agentsys.services.embeddings.SentenceTransformer")
async def test_local_provider_truncates_to_dimensions(mock_st: MagicMock) -> None:
    """BGE-M3 outputs 1024 dims natively — provider must truncate to N (Matryoshka)."""
    fake_model = MagicMock()
    fake_model.encode.return_value = np.full((2, 1024), 0.5, dtype=np.float32)
    mock_st.return_value = fake_model

    provider = LocalBGEEmbeddingProvider(model_name="BAAI/bge-m3", dimensions=512)
    vectors = await provider.embed(["Quilmes 1L", "Brahma lata"])

    assert len(vectors) == 2
    assert len(vectors[0]) == 512
    assert len(vectors[1]) == 512
    fake_model.encode.assert_called_once()


@patch("agentsys.services.embeddings.SentenceTransformer")
async def test_local_provider_empty_input(mock_st: MagicMock) -> None:
    """Empty input list returns empty list, never calls encode."""
    fake_model = MagicMock()
    mock_st.return_value = fake_model

    provider = LocalBGEEmbeddingProvider(model_name="BAAI/bge-m3", dimensions=512)
    result = await provider.embed([])

    assert result == []
    fake_model.encode.assert_not_called()


@patch("agentsys.services.embeddings.SentenceTransformer")
def test_local_provider_implements_protocol(mock_st: MagicMock) -> None:
    """LocalBGEEmbeddingProvider satisfies the EmbeddingProvider Protocol."""
    mock_st.return_value = MagicMock()
    provider: EmbeddingProvider = LocalBGEEmbeddingProvider(
        model_name="BAAI/bge-m3", dimensions=512
    )
    assert hasattr(provider, "embed")


# ---------------------------------------------------------------------------
# Factory — selects implementation based on settings
# ---------------------------------------------------------------------------


@patch("agentsys.services.embeddings.SentenceTransformer")
def test_factory_returns_local_when_configured(mock_st: MagicMock) -> None:
    """embedding_provider='local' returns LocalBGEEmbeddingProvider."""
    mock_st.return_value = MagicMock()
    settings = Settings(
        embedding_provider="local",
        embedding_model_local="BAAI/bge-m3",
        embedding_dimensions=512,
    )
    provider = get_embedding_provider(settings)
    assert isinstance(provider, LocalBGEEmbeddingProvider)


def test_factory_returns_openai_when_configured() -> None:
    """embedding_provider='openai' returns OpenAIEmbeddingProvider."""
    settings = Settings(
        embedding_provider="openai",
        openai_api_key="sk-test",
        embedding_model="text-embedding-3-small",
        embedding_dimensions=512,
    )
    provider = get_embedding_provider(settings)
    assert isinstance(provider, OpenAIEmbeddingProvider)
