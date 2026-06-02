from __future__ import annotations


def test_agent_state_fields() -> None:
    from agentsys.agent.state import AgentState

    state: AgentState = {
        "messages": [],
        "session_id": "s1",
        "current_permissions": ("read:catalog",),
    }

    assert state["messages"] == []
    assert state["session_id"] == "s1"
    assert state["current_permissions"] == ("read:catalog",)
