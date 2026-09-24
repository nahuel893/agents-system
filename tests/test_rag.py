# type: ignore
# pyright: reportMissingImports=false, reportCallIssue=false, reportArgumentType=false
"""Unit tests for RAG catalog retrieval orchestration."""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, field
from typing import Any

import pytest

from agents_system.config import Settings
from agents_system.services import rag
from agents_system.services.rag import (
    KeywordSearchCandidate,
    VectorSearchCandidate,
)


@dataclass
class FuncCatalogSource:
    """Adapts loose query functions to the CatalogSource protocol.

    These tests used to reach the same functions by monkeypatching
    `rag.catalog`. That only worked because the module imported its storage;
    passing them in is the same test with the coupling removed.
    """

    search_vector_fn: Any = None
    search_keywords_fn: Any = None

    async def search_vector(
        self, session: Any, *, embedding: list[float], limit: int, ef_search: int
    ) -> list[VectorSearchCandidate]:
        return await self.search_vector_fn(
            session, embedding=embedding, limit=limit, ef_search=ef_search
        )

    async def search_keywords(
        self, session: Any, *, query: str, limit: int
    ) -> list[KeywordSearchCandidate]:
        return await self.search_keywords_fn(session, query=query, limit=limit)


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


async def test_search_catalog_fallback_on_embed_empty_vector() -> None:
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
            KeywordSearchCandidate("SKU-K1", "BrandA 1L"),
            KeywordSearchCandidate("SKU-K2", "BrandA 970cc"),
        ]

    source = FuncCatalogSource(search_keywords_fn=fake_search_keywords)

    result = await rag.search_catalog(
        object(),
        "branda",
        settings=settings,
        embedder=embedder,
        source=source,
    )

    assert result.classification == "ambiguous"
    assert result.source == "keyword_fallback"
    assert len(result.candidates) == 2
    assert result.candidates[0].sku == "SKU-K1"
    assert result.candidates[0].similarity is None
    assert result.candidates[1].similarity is None


async def test_search_catalog_fallback_on_embed_exception() -> None:
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

    source = FuncCatalogSource(search_keywords_fn=fake_search_keywords)

    result = await rag.search_catalog(
        object(),
        "coca",
        settings=settings,
        embedder=embedder,
        source=source,
    )

    assert result.classification == "ambiguous"
    assert result.source == "keyword_fallback"
    assert len(result.candidates) == 1
    assert result.candidates[0].sku == "SKU-K3"


async def test_search_catalog_fallback_returns_no_match_on_empty_keywords() -> None:
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

    source = FuncCatalogSource(search_keywords_fn=fake_search_keywords)

    result = await rag.search_catalog(
        object(),
        "xyzzy",
        settings=settings,
        embedder=embedder,
        source=source,
    )

    assert result.classification == "no_match"
    assert result.source == "keyword_fallback"
    assert result.candidates == []


async def test_search_catalog_fallback_respects_keyword_top_k_cap() -> None:
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
        return [KeywordSearchCandidate(f"SKU-{i}", f"desc-{i}") for i in range(5)]

    source = FuncCatalogSource(search_keywords_fn=fake_search_keywords)

    result = await rag.search_catalog(
        object(),
        "beer",
        settings=settings,
        embedder=embedder,
        source=source,
    )

    assert result.classification == "ambiguous"
    assert len(result.candidates) == 2
    assert result.candidates[0].sku == "SKU-0"


async def test_search_catalog_fallback_logs_once_on_embed_failure(
    caplog: pytest.LogCaptureFixture,
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

    source = FuncCatalogSource(search_keywords_fn=fake_search_keywords)

    await rag.search_catalog(
        object(),
        "nonexistent",
        settings=settings,
        embedder=embedder,
        source=source,
    )

    assert len(caplog.records) == 1
    assert "embedding_failure" in caplog.text
    assert "nonexistent" in caplog.text


async def test_search_catalog_fallback_does_not_retry_embed() -> None:
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

    source = FuncCatalogSource(search_keywords_fn=fake_search_keywords)

    await rag.search_catalog(
        object(),
        "no-retry",
        settings=settings,
        embedder=embedder,
        source=source,
    )

    assert len(embedder.calls) == 1  # exactly one embed attempt, no retry


async def test_search_catalog_returns_direct_vector_match_and_caps_top_k() -> None:
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
            VectorSearchCandidate("SKU-1", "BrandA Cristal 1L", 0.04),
            VectorSearchCandidate("SKU-2", "BrandA Lager 970cc", 0.08),
            VectorSearchCandidate("SKU-3", "Cerveza rubia lata", 0.12),
        ]

    source = FuncCatalogSource(search_vector_fn=fake_search_vector)

    session = object()
    result = await rag.search_catalog(
        session,
        "  branda 1 litro  ",
        settings=settings,
        embedder=embedder,
        source=source,
    )

    assert embedder.calls == [["branda 1 litro"]]
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
    assert result.candidates[0].description == "BrandA Cristal 1L"
    assert result.candidates[0].source == "vector"
    assert result.candidates[0].similarity == 0.96
    assert result.candidates[1].similarity == 0.92


async def test_search_catalog_returns_ambiguous_for_mid_band_similarity() -> None:
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

    source = FuncCatalogSource(search_vector_fn=fake_search_vector)

    result = await rag.search_catalog(
        object(),
        "coca cola",
        settings=settings,
        embedder=embedder,
        source=source,
    )

    assert result.classification == "ambiguous"
    assert result.source == "vector"
    assert [candidate.sku for candidate in result.candidates] == ["SKU-9", "SKU-10"]
    assert result.candidates[0].similarity == 0.85


async def test_search_catalog_returns_no_match_and_no_candidates_below_threshold() -> (
    None
):
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

    source = FuncCatalogSource(search_vector_fn=fake_search_vector)

    result = await rag.search_catalog(
        object(),
        "agua con gas",
        settings=settings,
        embedder=embedder,
        source=source,
    )

    assert result.classification == "no_match"
    assert result.source == "vector"
    assert result.candidates == []


# --- Dependency inversion: the catalog source is injected, never imported ---


@dataclass
class StubCatalogSource:
    """A CatalogSource that needs no database and no client schema."""

    vector: list[Any] = field(default_factory=list)
    keyword: list[Any] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.vector_calls: list[dict[str, Any]] = []
        self.keyword_calls: list[dict[str, Any]] = []

    async def search_vector(
        self, session: Any, *, embedding: list[float], limit: int, ef_search: int
    ) -> list[Any]:
        self.vector_calls.append(
            {"embedding": embedding, "limit": limit, "ef_search": ef_search}
        )
        return self.vector

    async def search_keywords(
        self, session: Any, *, query: str, limit: int
    ) -> list[Any]:
        self.keyword_calls.append({"query": query, "limit": limit})
        return self.keyword


async def test_search_catalog_uses_the_injected_source_not_a_module_import() -> None:
    """The orchestration must run with no `agents_system.services.catalog` present.

    Before the inversion this could only be tested by monkeypatching
    `rag.catalog`, which proved the coupling rather than removing it.
    """
    settings = Settings(
        _env_file=None,
        rag_threshold_direct=0.92,
        rag_threshold_ambiguous=0.82,
        rag_top_k=5,
        rag_hnsw_ef_search=40,
    )
    source = StubCatalogSource(
        vector=[rag.VectorSearchCandidate("SKU-1", "Yerba 1kg", 0.05)]
    )

    result = await rag.search_catalog(
        object(),
        "yerba",
        settings=settings,
        embedder=StubEmbedder(vectors=[[0.1, 0.2]]),
        source=source,
    )

    assert result.classification == "direct"
    assert result.candidates[0].sku == "SKU-1"
    assert source.vector_calls == [
        {"embedding": [0.1, 0.2], "limit": 5, "ef_search": 40}
    ]


def test_rag_module_does_not_import_client_domain() -> None:
    """`services.rag` must not reach into any client-owned module.

    A fresh-interpreter probe rather than a substring scan of the file. The
    scan this replaces promised "any client-owned module" and checked two
    literals naming one of them: adding `from agents_system.models.tables import
    CatalogEmbedding` -- ACME's actual `catalog_embeddings` ORM table -- and
    using it left the scan green, while a docstring reword turned it red. It
    fired on prose and missed the worst real violation.

    Run in a subprocess so no other test's imports leak into `sys.modules`
    and make this vacuously pass. Same pattern, and same reason, as the
    laziness probes in `test_public_api.py`.
    """
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys\n"
            "import agents_system.services.rag\n"
            "client_owned = {\n"
            "    'agents_system.services.catalog',\n"
            "    'agents_system.services.clients',\n"
            "    'agents_system.services.conversation_log',\n"
            "    'agents_system.services.seed_data',\n"
            "    'agents_system.services.sync_articles',\n"
            "    'agents_system.services.sync_clients',\n"
            "    'agents_system.services.medallion',\n"
            "    'agents_system.models.tables',\n"
            "}\n"
            "leaked = sorted(client_owned & set(sys.modules))\n"
            "assert not leaked, leaked\n",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(pathlib.Path(rag.__file__).resolve().parents[3]),
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_rag_module_has_no_function_local_import_of_client_domain() -> None:
    """The blind spot the runtime probe cannot see, kept from the old scan.

    `catalog.py` now imports `rag.py`, so a module-level re-import of
    `services.catalog` here would be a hard circular-import crash — which
    makes a FUNCTION-LOCAL import the reflex fix a developer reaches for,
    and the only way the backwards edge can realistically come back.

    The subprocess probe above snapshots `sys.modules` after importing the
    module, not after running `search_catalog`, so it never sees one. The
    substring scan this pair replaced DID catch it. Replacing one check with
    the other traded a blind spot for a different blind spot; keeping both
    covers the class.

    Parsed rather than grepped, so it cannot fire on prose the way the
    substring scan did.
    """
    import ast

    tree = ast.parse(pathlib.Path(rag.__file__).read_text(encoding="utf-8"))
    client_owned = ("catalog", "clients", "conversation_log", "tables")

    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        module = getattr(node, "module", None) or ""
        names = [module] + [a.name for a in node.names]
        for name in names:
            assert not any(part in name.split(".") for part in client_owned), (
                f"services/rag.py imports client-owned '{name}'"
            )


def test_importing_rag_does_not_load_the_embeddings_stack() -> None:
    """Pins the TYPE_CHECKING guard, which nothing checked.

    `EmbeddingProvider` is annotation-only; a plain runtime import makes
    anything touching this module load the embeddings stack and, through it,
    the OpenAI SDK — including `services/catalog.py`, which needs neither.
    The behaviour change was real and correct, and reverting the guard left
    the whole suite green, so a future edit would have restored the
    regression silently.
    """
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys\n"
            "import agents_system.services.rag\n"
            "heavy = sorted({'openai', 'torch', 'sentence_transformers'} "
            "& set(sys.modules))\n"
            "assert not heavy, heavy\n",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(pathlib.Path(rag.__file__).resolve().parents[3]),
    )

    assert result.returncode == 0, result.stdout + result.stderr
