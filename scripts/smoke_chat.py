"""End-to-end smoke (D-011) for the generic sales-agent fixture.

Unlike ``scripts/smoke_rag.py`` (which probes the retriever directly with
distilled keywords), this exercises the full chain — LLM, Layer-2 interceptor,
RAG connector, and pgvector — against neutral request language.

For each turn it prints the user message, the catalog lookup the agent chose,
and the agent's reply.

Usage::

    PYTHONUNBUFFERED=1 uv run python scripts/smoke_chat.py          # local Ollama
    PYTHONUNBUFFERED=1 uv run python scripts/smoke_chat.py groq     # hosted Groq
"""

from __future__ import annotations

import asyncio
import os
import sys
import textwrap
from pathlib import Path

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import async_sessionmaker

from agentsys.agent.graph import AgentRuntime
from agentsys.config import get_settings
from agentsys.connectors.rag_connector import build_acme_rag_registry
from agentsys.harness.factory import build_runtime
from agentsys.harness.loader import RootConfig
from agentsys.models.base import get_engine
from agentsys.observability import setup_logging
from agentsys.services.embeddings import get_embedding_provider

GROQ_MODEL = "llama-3.3-70b-versatile"
OLLAMA_MODEL = "qwen2.5:3b"

GRANTED_PERMISSIONS = [
    "read:catalog",
    "read:client_registry",
    "write:orders",
    "write:order_items",
    "send:message",
]

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CLIENT_A_ROOTS = RootConfig(
    platform_root=_REPO_ROOT / "platform",
    deployments_root=(
        _REPO_ROOT / "tests" / "fixtures" / "agents" / "overrides" / "deployments"
    ),
)

REQUEST_TURNS = [
    "Please find an option for this request.",
    "Can you clarify the available choices?",
    "Prepare a summary before completing the request.",
]


def _build_model(provider: str) -> tuple[BaseChatModel, str]:
    """Construct the chat model for the chosen provider (default: local Ollama)."""
    if provider == "groq":
        from langchain_groq import ChatGroq

        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            print("ERROR: GROQ_API_KEY not set. Add it to your .env file.")
            sys.exit(1)
        return ChatGroq(
            model=GROQ_MODEL, api_key=SecretStr(api_key)
        ), f"Groq ({GROQ_MODEL})"

    if provider == "ollama":
        from langchain_ollama import ChatOllama

        return ChatOllama(model=OLLAMA_MODEL, temperature=0), f"Ollama ({OLLAMA_MODEL})"

    print(f"ERROR: unknown provider '{provider}'. Use 'ollama' or 'groq'.")
    sys.exit(1)


def _print_turn(user_text: str, new_messages: list[AnyMessage]) -> None:
    """Show the customer message, the distilled catalog lookups, then the reply."""
    print(f"\n{'─' * 72}")
    print(f"CLIENTE: {user_text}")
    final_reply = ""
    for msg in new_messages:
        if not isinstance(msg, AIMessage):
            continue
        for call in msg.tool_calls or []:
            print(f"  · el agente busca: {call['name']}({call['args']})")
        content = msg.content
        if isinstance(content, list):
            content = " ".join(
                block.get("text", "") for block in content if isinstance(block, dict)
            )
        if content and not msg.tool_calls:
            final_reply = str(content)
    reply = textwrap.fill(final_reply, 78) if final_reply else "(no text reply)"
    print(f"AGENTE: {reply}")


async def main() -> int:
    setup_logging()
    provider = sys.argv[1] if len(sys.argv) > 1 else "ollama"
    settings = get_settings()

    print("Loading embedder (BGE-M3) and assembling the runtime...")
    embedder = get_embedding_provider(settings)
    registry = build_acme_rag_registry(settings, embedder)

    engine = get_engine(settings.database_url)
    session_provider = async_sessionmaker(engine, expire_on_commit=False)

    equipped = build_runtime(
        role_type="sales-agent",
        registry=registry,
        granted_permissions=GRANTED_PERMISSIONS,
        client="client-a",
        roots=_CLIENT_A_ROOTS,
        session_provider=session_provider,
    )

    model, label = _build_model(provider)
    runtime = AgentRuntime(runtime=equipped, model=model)
    print(f"Generic end-to-end smoke — {label}")

    history: list[AnyMessage] = []
    try:
        for user_text in REQUEST_TURNS:
            history.append(HumanMessage(content=user_text))
            result = await runtime.run_turn(
                messages=history,
                session_id="smoke-criollo-001",
                permissions=tuple(GRANTED_PERMISSIONS),
            )
            _print_turn(user_text, result[len(history) :])
            history = result
    finally:
        await engine.dispose()

    print(f"\n{'═' * 72}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
