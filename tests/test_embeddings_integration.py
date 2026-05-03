"""Integration tests for the local BGE-M3 embedding provider.

These tests load the REAL model (~570MB cached at ~/.cache/huggingface).
Skipped by default — opt-in with::

    uv run pytest -m integration -v

Validates the full sentence-transformers + torch path that unit tests
mock. Complements ``scripts/preflight_local_embeddings.sh`` (which is
a manual benchmark, not a regression test).
"""

from __future__ import annotations

import math

import pytest

from badie.services.embeddings import LocalBGEEmbeddingProvider


def _cosine(a: list[float], b: list[float]) -> float:
    """Plain cosine similarity, no numpy dependency."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    return dot / (norm_a * norm_b)


@pytest.mark.integration
async def test_local_provider_real_bge_m3_truncates() -> None:
    """REAL BGE-M3: confirms 1024-dim native output gets truncated to 512."""
    provider = LocalBGEEmbeddingProvider(
        model_name="BAAI/bge-m3", dimensions=512
    )

    vectors = await provider.embed(
        ["Quilmes Cristal 1L retornable", "Coca-Cola 2.25L"]
    )

    assert len(vectors) == 2
    assert len(vectors[0]) == 512
    assert len(vectors[1]) == 512
    # Different texts must produce different vectors
    assert vectors[0] != vectors[1]


@pytest.mark.integration
async def test_local_provider_real_bge_m3_semantic() -> None:
    """REAL BGE-M3: similar products are closer in vector space than unrelated ones.

    Validates the model produces semantically meaningful vectors — proves the
    integration is not just shape-correct but BEHAVIORALLY correct.
    """
    provider = LocalBGEEmbeddingProvider(
        model_name="BAAI/bge-m3", dimensions=512
    )

    quilmes, quilmes_variant, unrelated = await provider.embed(
        [
            "Quilmes Cristal 1L retornable",
            "cerveza Quilmes un litro",
            "zapatillas Adidas running negras",
        ]
    )

    sim_related = _cosine(quilmes, quilmes_variant)
    sim_unrelated = _cosine(quilmes, unrelated)

    # Quilmes ↔ Quilmes-variant must be MORE similar than Quilmes ↔ zapatillas
    assert sim_related > sim_unrelated, (
        f"Expected related products to be closer: "
        f"sim(quilmes, variant)={sim_related:.4f} vs "
        f"sim(quilmes, unrelated)={sim_unrelated:.4f}"
    )
