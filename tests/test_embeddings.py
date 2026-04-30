"""Tests for the embedding service abstraction (Protocol + Fake + OpenAI)."""

from __future__ import annotations

from badie.services.embeddings import (
    EmbeddingProvider,
    FakeEmbeddingProvider,
    OpenAIEmbeddingProvider,
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
