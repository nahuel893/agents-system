"""AgentRuntime — LangGraph-based, provider-agnostic agent loop (D-007/D-009).

Turns a static EquippedRuntime into a live multi-turn agent via a 2-node
LangGraph graph:
  - call_model  : invokes the bound model with the current message history
  - execute_tools: dispatches each tool_call through the Layer-2 interceptor

The loop continues until the model produces an AIMessage with no tool_calls.

D-009: intercept() is now async-native. Sync connectors are wrapped with
asyncio.to_thread inside intercept() itself. _execute_tools opens one
turn-scoped AsyncSession (when session_provider is set) and forwards it to
intercept(). The session is NOT committed here — the orchestrator/webhook owns
commit after run_turn() returns.

Concurrency note: the tool-call loop remains sequential. A shared AsyncSession
must not be used concurrently — do not convert the loop to asyncio.gather.

State is NOT persisted by the graph itself. A stateless caller supplies the
full message history per turn. A caller that passes a ``thread_id`` to a runtime
built with a checkpointer gets the thread's history read before the turn and
the turn saved after it (issue #24, see ``_Thread``).

Where the code lives (this module only re-exports the public names, which keep
the import path ``agents_system.agent.graph``):
  - ``runtime``      : ``AgentRuntime``, the multi-turn loop
  - ``builder``      : the StateGraph and its routing
  - ``nodes/model``  : the model node; ``nodes/tools``: the tool nodes
  - ``thread``       : checkpointer-backed threads and turn progress
  - ``limits``       : enforced execution limits
  - ``usage``        : ``TurnUsage``, ``TurnResult`` and turn cost
"""

from __future__ import annotations

from agents_system.agent.nodes.model import TRUSTED_CONTEXT_HEADER, BudgetGate
from agents_system.agent.runtime import AgentRuntime
from agents_system.agent.thread import DEFAULT_CHECKPOINTER_TIMEOUT_S
from agents_system.agent.usage import TurnResult, TurnUsage, model_display_name

#: Every public name this module defined before the split. Each keeps this
#: import path; a private name is imported from the module that defines it.
__all__ = [
    "DEFAULT_CHECKPOINTER_TIMEOUT_S",
    "TRUSTED_CONTEXT_HEADER",
    "AgentRuntime",
    "BudgetGate",
    "TurnResult",
    "TurnUsage",
    "model_display_name",
]
