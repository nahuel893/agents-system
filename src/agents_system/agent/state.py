from __future__ import annotations

from typing import Annotated, NotRequired

from langchain_core.messages import AnyMessage
from langchain_core.messages.ai import UsageMetadata
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict


class AgentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    session_id: str
    current_permissions: tuple[str, ...]
    tool_call_count: int
    # #289 -- error ToolMessages (a raising tool, a tool timeout) so far THIS
    # turn. Reset to 0 by `run_turn` like `tool_call_count`; absent in a state
    # built by hand, which reads as 0.
    tool_error_count: NotRequired[int]
    # #78 Phase 0 -- one entry per `_call_model` invocation made so far THIS
    # turn, in order. `None` means that particular model call reported no
    # `usage_metadata` (see `TurnUsage`'s honesty rule in `agent/graph.py`).
    # No reducer, same as `tool_call_count`: `run_turn` always resets this to
    # `[]` in `initial_state`, so a resumed checkpointed thread never
    # inherits a prior turn's usage (design AD-1 gotcha).
    turn_usage: list[UsageMetadata | None]
    # ADR-011 part 5 -- set when a per-user cap stopped the turn at a model
    # call. Absent otherwise; never inherited (the turn builds a fresh state).
    budget_refused: NotRequired[bool]
