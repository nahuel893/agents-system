"""Smoke test — BADIE sales agent with a real LLM through the full harness.

Runs a simulated sales conversation end-to-end:

  Trigger → Factory → Injector → AgentRuntime → Interceptor → Connector stubs

Provider is selectable so you can test offline (Ollama, no rate limit) or
against a hosted API (Groq). The runtime itself is provider-agnostic — only
this script's model construction changes.

Usage:
    uv run python scripts/smoke.py              # default: ollama (local)
    uv run python scripts/smoke.py groq         # hosted Groq (needs GROQ_API_KEY)
    uv run python scripts/smoke.py ollama        # local Ollama
"""
from __future__ import annotations

import asyncio
import os
import sys
import textwrap

from dotenv import load_dotenv
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AnyMessage, HumanMessage

load_dotenv()

# Allow running from repo root without installing the package
sys.path.insert(0, str(__file__ + "/../../src"))

from agentsys.agent.graph import AgentRuntime  # noqa: E402
from agentsys.connectors.stubs import build_badie_registry  # noqa: E402
from agentsys.harness.factory import build_runtime  # noqa: E402

# Provider config: (model name, builder fn)
GROQ_MODEL = "llama-3.3-70b-versatile"
OLLAMA_MODEL = "qwen2.5:3b"

GRANTED_PERMISSIONS = [
    "read:catalog",
    "read:client_registry",
    "write:orders",
    "write:order_items",
    "send:message",
]

TURNS = [
    "Hola! Qué productos tienen disponibles?",
    "Me interesa el azúcar. Cuánto cuesta y hay stock?",
    "Perfecto, quiero hacer un pedido: 3 unidades de azúcar La Colmena 1kg. Mi número es 5491112345678.",
]


def _build_model(provider: str) -> tuple[BaseChatModel, str]:
    """Construct the chat model for the chosen provider."""
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

    print(f"ERROR: unknown provider '{provider}'. Use 'groq' or 'ollama'.")
    sys.exit(1)


def _print_messages(messages: list[AnyMessage], turn: int) -> None:
    print(f"\n{'─'*60}")
    print(f"  TURN {turn}")
    print(f"{'─'*60}")
    for msg in messages:
        role = type(msg).__name__.replace("Message", "").upper()
        content = getattr(msg, "content", "")
        if isinstance(content, list):
            content = " ".join(
                block.get("text", "") for block in content if isinstance(block, dict)
            )
        if content:
            wrapped = textwrap.indent(textwrap.fill(str(content), 70), "  ")
            print(f"\n[{role}]\n{wrapped}")
        tool_calls = getattr(msg, "tool_calls", None)
        if tool_calls:
            for tc in tool_calls:
                print(f"\n  → tool_call: {tc['name']}({tc['args']})")


async def main() -> None:
    provider = sys.argv[1] if len(sys.argv) > 1 else "ollama"

    print("Building BADIE EquippedRuntime...")
    registry = build_badie_registry()
    equipped = build_runtime(
        role_type="sales-agent",
        registry=registry,
        granted_permissions=GRANTED_PERMISSIONS,
        client="badie",
    )
    print(f"  Role: {equipped.definition.role_name}")
    print(f"  Tools granted: {[t.name for t in equipped.tools]}")
    print(f"  Skills: {[s.name for s in equipped.skills]}")

    model, label = _build_model(provider)
    print(f"\nConnecting to {label}...")

    runtime = AgentRuntime(runtime=equipped, model=model)
    print("  AgentRuntime ready.\n")

    history: list[AnyMessage] = []

    for i, user_text in enumerate(TURNS, start=1):
        print(f"\n>>> USER: {user_text}")
        history.append(HumanMessage(content=user_text))

        try:
            result = await runtime.run_turn(
                messages=history,
                session_id="smoke-session-001",
                permissions=tuple(GRANTED_PERMISSIONS),
            )
        except Exception as exc:
            if "rate_limit" in str(exc).lower() or "429" in str(exc):
                import re

                wait = re.search(r"try again in (.+?)\.", str(exc))
                hint = f" — retry in {wait.group(1)}" if wait else ""
                print(f"\n[RATE LIMIT]{hint}")
                sys.exit(1)
            raise

        _print_messages(result[len(history):], turn=i)
        history = result

    print(f"\n{'═'*60}")
    print("  Smoke test complete.")
    print(f"{'═'*60}\n")


if __name__ == "__main__":
    asyncio.run(main())
