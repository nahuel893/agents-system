"""Embedding service abstraction.

Provides a Protocol so the rest of the codebase doesn't depend on any
concrete provider. Two implementations:

- ``OpenAIEmbeddingProvider`` — production (text-embedding-3-small)
- ``FakeEmbeddingProvider`` — deterministic vectors for tests
"""

from __future__ import annotations

import hashlib
import struct
from typing import Protocol

from openai import AsyncOpenAI


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
