"""Interactive chat MVP — BADIE sales agent over the REAL catalog (D-011).

Wires the whole stack end-to-end::

    stdin → AgentRuntime (Ollama) → Layer-2 interceptor → RAG connector → pgvector

Unlike ``scripts/smoke.py`` (hardcoded stub connectors), this builds the REAL
RAG registry (``build_badie_rag_registry``) and a turn-scoped ``AsyncSession``
provider over ``database_url``, so asking for a product runs a live semantic
search over ``catalog_embeddings``.

Usage::

    # interactive REPL
    PYTHONUNBUFFERED=1 uv run python scripts/chat.py

    # piped — one message per line, reads until EOF
    echo "tenes vino malbec?" | PYTHONUNBUFFERED=1 uv run python scripts/chat.py

    # hosted provider instead of local Ollama
    uv run python scripts/chat.py groq
"""

from __future__ import annotations

import asyncio
import os
import sys
import textwrap
from collections.abc import Iterator

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from sqlalchemy.ext.asyncio import async_sessionmaker

from agentsys.agent.graph import AgentRuntime
from agentsys.config import get_settings
from agentsys.connectors.rag_connector import build_badie_rag_registry
from agentsys.harness.factory import build_runtime
from agentsys.models.base import get_engine
from agentsys.observability import setup_logging
from agentsys.services.embeddings import get_embedding_provider

GROQ_MODEL = "llama-3.3-70b-versatile"
OLLAMA_MODEL = "qwen2.5:3b"

# The full BADIE sales surface. read:catalog unlocks the RAG catalog_search;
# the rest cover the stub connectors (client lookup, order writing, messaging).
GRANTED_PERMISSIONS = [
    "read:catalog",
    "read:client_registry",
    "write:orders",
    "write:order_items",
    "send:message",
]

_EXIT_WORDS = {"exit", "quit", "salir", "chau", "q"}


def _build_model(provider: str) -> tuple[BaseChatModel, str]:
    """Construct the chat model for the chosen provider (default: local Ollama)."""
    if provider == "groq":
        from langchain_groq import ChatGroq

        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            print("ERROR: GROQ_API_KEY not set. Add it to your .env file.")
            sys.exit(1)
        return ChatGroq(model=GROQ_MODEL, api_key=api_key), f"Groq ({GROQ_MODEL})"

    if provider == "ollama":
        from langchain_ollama import ChatOllama

        return ChatOllama(model=OLLAMA_MODEL, temperature=0), f"Ollama ({OLLAMA_MODEL})"

    print(f"ERROR: unknown provider '{provider}'. Use 'ollama' or 'groq'.")
    sys.exit(1)


def _read_messages() -> Iterator[str]:
    """Yield user input lines. Works for both an interactive TTY and a pipe."""
    while True:
        try:
            line = input("VOS> ")
        except EOFError:
            return
        line = line.strip()
        if not line:
            continue
        if line.lower() in _EXIT_WORDS:
            return
        yield line


def _print_turn(new_messages: list[AnyMessage]) -> None:
    """Show the catalog lookups the agent made, then its final text reply."""
    final_reply = ""
    for msg in new_messages:
        if not isinstance(msg, AIMessage):
            continue
        for call in msg.tool_calls or []:
            print(f"  · tool: {call['name']}({call['args']})")
        content = msg.content
        if isinstance(content, list):
            content = " ".join(
                block.get("text", "") for block in content if isinstance(block, dict)
            )
        if content and not msg.tool_calls:
            final_reply = str(content)

    if final_reply:
        print(f"\nAGENTE: {textwrap.fill(final_reply, 78)}\n")
    else:
        print("\nAGENTE: (no text reply)\n")


async def main() -> int:
    setup_logging()
    provider = sys.argv[1] if len(sys.argv) > 1 else "ollama"
    settings = get_settings()

    print("Loading embedder (BGE-M3) and assembling the runtime...")
    # Embedder is heavy — load once and reuse across the whole conversation.
    embedder = get_embedding_provider(settings)
    registry = build_badie_rag_registry(settings, embedder)

    # Turn-scoped sessions over the bot DB, where catalog_embeddings lives.
    engine = get_engine(settings.database_url)
    session_provider = async_sessionmaker(engine, expire_on_commit=False)

    equipped = build_runtime(
        role_type="sales-agent",
        registry=registry,
        granted_permissions=GRANTED_PERMISSIONS,
        client="badie",
        session_provider=session_provider,
    )

    model, label = _build_model(provider)
    runtime = AgentRuntime(runtime=equipped, model=model)

    print(f"Ready. Model: {label}. Tools: {[t.name for t in equipped.tools]}")
    print("Ask for products in plain language (or 'salir' to quit).\n")

    history: list[AnyMessage] = []
    try:
        for user_text in _read_messages():
            history.append(HumanMessage(content=user_text))
            result = await runtime.run_turn(
                messages=history,
                session_id="chat-session-001",
                permissions=tuple(GRANTED_PERMISSIONS),
            )
            _print_turn(result[len(history) :])
            history = result
    finally:
        await engine.dispose()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
