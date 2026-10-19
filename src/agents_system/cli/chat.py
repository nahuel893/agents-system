"""``agents chat <agent>``: talk to an agent in the terminal.

One ``AgentRuntime`` for the session, each turn run with the whole history so
far (tool calls and results included), the way a stateless caller of the
served app sends it. Each tool call and its result are shown as they happen,
and the reply is printed as text (issue #312).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import uuid
from collections.abc import Sequence
from typing import Any, TextIO

from dotenv import dotenv_values

from agents_system.agent.models import build_chat_model, declared_model
from agents_system.cli._common import CLIError, check_grants, load_agent, load_registry
from agents_system.cli.check import add_runtime_flags

#: The providers `build_chat_model` builds.
PROVIDERS = ("anthropic", "openai_compatible", "groq", "ollama")

#: What the user types to leave; end of input (Ctrl-D) does the same.
EXIT_WORDS = frozenset({"exit", "quit"})

#: A tool result longer than this is cut when shown; the model sees it whole.
SHOWN_RESULT_CHARS = 500


def add_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "chat",
        help="talk to an agent in the terminal",
        description=(
            "An interactive turn loop with an agent. Each tool call and its "
            "result are shown; type exit, or press Ctrl-D, to quit."
        ),
    )
    parser.add_argument(
        "agent", help="an agent folder (./my-agent) or a predefined role name"
    )
    parser.add_argument(
        "--provider",
        choices=PROVIDERS,
        help=(
            "the model provider (default: the agent's declared model, "
            "else ADAPTER_PROVIDER)"
        ),
    )
    parser.add_argument(
        "--model",
        help="the model id, e.g. claude-haiku-5-5 (default: the provider's own)",
    )
    add_runtime_flags(parser)
    parser.set_defaults(run=run)


def build_model(definition: Any, *, provider: str | None, model: str | None) -> Any:
    """The chat model for this session.

    ``--provider`` wins. Without it, the agent's declared model is used, with
    ``--model`` replacing its model id; an agent that declares none uses the
    configured ``ADAPTER_PROVIDER`` (the environment, then ``./.env``).
    Credentials are read by ``build_chat_model``, never taken from flags.
    """
    if provider is not None:
        return build_chat_model(provider, model_override=model)
    declared = declared_model(definition)
    if declared is not None:
        return build_chat_model(
            declared["provider"],
            model_override=model or declared["model"],
            temperature=declared.get("temperature"),
            max_tokens=declared.get("max_tokens"),
        )
    configured = os.environ.get("ADAPTER_PROVIDER") or dotenv_values(".env").get(
        "ADAPTER_PROVIDER"
    )
    if configured:
        return build_chat_model(configured, model_override=model)
    raise CLIError(
        f"agent {definition.role_name!r} declares no model and ADAPTER_PROVIDER "
        f"is not set: pass --provider ({', '.join(PROVIDERS)}), and --model "
        "for a model other than the provider's default"
    )


def _shown(content: Any) -> str:
    text = content if isinstance(content, str) else json.dumps(content, default=str)
    if len(text) > SHOWN_RESULT_CHARS:
        return text[:SHOWN_RESULT_CHARS] + " ..."
    return text


def show_turn(new_messages: Sequence[Any], out: TextIO) -> None:
    """Print one turn's tool calls, their results and the reply."""
    from langchain_core.messages import AIMessage, ToolMessage

    # A result carries its call's id, not always the tool's name.
    called: dict[str, str] = {}
    for message in new_messages:
        if isinstance(message, AIMessage):
            for call in message.tool_calls:
                called[call["id"] or ""] = call["name"]
                out.write(f"  -> {call['name']}({json.dumps(call['args'])})\n")
            if message.text:
                out.write(f"agent> {message.text}\n")
        elif isinstance(message, ToolMessage):
            name = message.name or called.get(message.tool_call_id, "tool")
            out.write(f"  <- {name}: {_shown(message.content)}\n")
    out.flush()


async def chat_loop(runtime: Any, out: TextIO) -> None:
    from langchain_core.messages import HumanMessage

    history: list[Any] = []
    session_id = f"cli-chat-{uuid.uuid4().hex[:12]}"
    while True:
        try:
            line = input("you> ")
        except EOFError:
            out.write("\n")
            return
        text = line.strip()
        if text.lower() in EXIT_WORDS:
            return
        if not text:
            continue
        sent = [*history, HumanMessage(text)]
        messages = await runtime.run_turn(sent, session_id=session_id)
        show_turn(messages[len(sent) :], out)
        history = messages


def run(args: argparse.Namespace, out: TextIO) -> int:
    from agents_system.agent.graph import AgentRuntime
    from agents_system.harness.factory import FactoryError, build_runtime
    from agents_system.harness.injector import InjectionError
    from agents_system.harness.loader import DefinitionError
    from agents_system.permissions import AgentPermissionError

    agent = load_agent(args.agent)
    registry = load_registry(args.registry)
    grants = check_grants(args.grant)
    try:
        equipped = build_runtime(agent, registry, grants)
        model = build_model(
            equipped.definition, provider=args.provider, model=args.model
        )
    except (
        AgentPermissionError,
        DefinitionError,
        FactoryError,
        InjectionError,
        ValueError,
    ) as error:
        raise CLIError(f"{args.agent}: {error}") from error
    runtime = AgentRuntime(equipped, model=model)

    tools = ", ".join(spec.name for spec in equipped.tools) or "(none)"
    model_name = (
        getattr(model, "model_name", None)
        or getattr(model, "model", None)
        or type(model).__name__
    )
    out.write(
        f"Chatting with {equipped.definition.role_name} on {model_name}. "
        f"Tools: {tools}. Type exit or press Ctrl-D to quit.\n"
    )
    out.writelines(
        f"  (denied: {name}: {reason}; see `agents check`)\n"
        for name, reason in equipped.denied_tools
    )
    asyncio.run(chat_loop(runtime, out))
    return 0
