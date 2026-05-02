"""Embedding service abstraction.

Provides a Protocol so the rest of the codebase doesn't depend on any
concrete provider. Three implementations:

- ``OpenAIEmbeddingProvider`` — cloud (text-embedding-3-small, requires API key)
- ``LocalBGEEmbeddingProvider`` — self-hosted BGE-M3 via sentence-transformers
- ``FakeEmbeddingProvider`` — deterministic vectors for tests

A factory ``get_embedding_provider(settings)`` selects the implementation
based on ``settings.embedding_provider``.
"""

from __future__ import annotations

import asyncio
import hashlib
import struct
from typing import TYPE_CHECKING, Any, Protocol

from openai import AsyncOpenAI

from badie.config import Settings

# Module-level placeholder so tests can patch ``embeddings.SentenceTransformer``.
# Real import is lazy inside ``LocalBGEEmbeddingProvider.__init__`` to avoid
# loading torch (~500MB) when the local provider is never used.
SentenceTransformer: Any = None

if TYPE_CHECKING:
    pass


class EmbeddingProvider(Protocol):
    """Embeds a batch of texts into fixed-dimension vectors."""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        ...


class OpenAIEmbeddingProvider:
    """Production provider using OpenAI's embeddings API."""

    def __init__(
        self,
        api_key: str,
        model: str = "text-embedding-3-small",
        dimensions: int = 512,
    ) -> None:
        self._client = AsyncOpenAI(api_key=api_key)
        self._model = model
        self._dimensions = dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        response = await self._client.embeddings.create(
            model=self._model,
            input=texts,
            dimensions=self._dimensions,
        )
        return [item.embedding for item in response.data]


class LocalBGEEmbeddingProvider:
    """Self-hosted provider using sentence-transformers (default: BGE-M3).

    Lazy-imports ``sentence_transformers`` so importing this module does
    not pull in torch unless the local provider is actually instantiated.

    The native model output is 1024 dims for BGE-M3 — vectors are truncated
    to ``dimensions`` (Matryoshka representation learning).
    """

    def __init__(
        self,
        model_name: str = "BAAI/bge-m3",
        dimensions: int = 512,
    ) -> None:
        global SentenceTransformer
        if SentenceTransformer is None:
            from sentence_transformers import SentenceTransformer as _ST

            SentenceTransformer = _ST
        self._model = SentenceTransformer(model_name)
        self._dimensions = dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        loop = asyncio.get_event_loop()
        # Bridge sync encode() to async — runs in default thread pool
        vectors = await loop.run_in_executor(
            None, lambda: self._model.encode(texts, batch_size=32)
        )
        # Truncate native dims to configured dims (Matryoshka)
        return [list(v[: self._dimensions]) for v in vectors]


class FakeEmbeddingProvider:
    """Deterministic provider for tests — hash-based, no API calls."""

    def __init__(self, dimensions: int = 512) -> None:
        self._dimensions = dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vector_for(text) for text in texts]

    def _vector_for(self, text: str) -> list[float]:
        # Use SHA-256 as a seed source; expand by hashing with counter
        # until we have enough bytes for `dimensions` floats.
        needed_bytes = self._dimensions * 4  # 4 bytes per float (uint32 → [0,1])
        buf = bytearray()
        counter = 0
        while len(buf) < needed_bytes:
            digest = hashlib.sha256(f"{counter}:{text}".encode()).digest()
            buf.extend(digest)
            counter += 1

        floats: list[float] = []
        for i in range(self._dimensions):
            offset = i * 4
            (uint32,) = struct.unpack_from(">I", buf, offset)
            floats.append(uint32 / 0xFFFFFFFF)  # normalize to [0, 1]
        return floats


def get_embedding_provider(settings: Settings) -> EmbeddingProvider:
    """Return the embedding provider configured in ``settings``.

    Selection:
      - ``embedding_provider="local"`` → ``LocalBGEEmbeddingProvider``
        (sentence-transformers, default for fresh clones — no API key needed)
      - ``embedding_provider="openai"`` → ``OpenAIEmbeddingProvider``
        (cloud, requires ``openai_api_key``)
    """
    if settings.embedding_provider == "openai":
        return OpenAIEmbeddingProvider(
            api_key=settings.openai_api_key,
            model=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
        )
    return LocalBGEEmbeddingProvider(
        model_name=settings.embedding_model_local,
        dimensions=settings.embedding_dimensions,
    )
