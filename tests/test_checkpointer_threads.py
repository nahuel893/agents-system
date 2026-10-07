"""Delete-by-thread and per-thread export on the checkpointer (ADR-011 part 6, #257)."""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph

from agents_system.services.checkpointer import CheckpointThreads


async def _seed(saver: InMemorySaver, thread_id: str, text: str) -> None:
    async def reply(state: MessagesState) -> dict[str, list[AIMessage]]:
        return {"messages": [AIMessage(content="ok")]}

    graph = StateGraph(MessagesState)
    graph.add_node("reply", reply)
    graph.add_edge(START, "reply")
    graph.add_edge("reply", END)
    await graph.compile(checkpointer=saver).ainvoke(
        {"messages": [HumanMessage(content=text)]},
        {"configurable": {"thread_id": thread_id}},
    )


async def test_export_and_delete_one_thread_leave_the_others() -> None:
    saver = InMemorySaver()
    await _seed(saver, "t-alice", "hello from alice")
    await _seed(saver, "t-bob", "hello from bob")
    threads = CheckpointThreads(saver)

    exported = await threads.export_thread("t-alice")
    assert exported is not None
    assert exported["thread_id"] == "t-alice"
    contents = [m["data"]["content"] for m in exported["messages"]]
    assert contents == ["hello from alice", "ok"]

    assert await threads.delete_thread("t-alice") == 1
    assert await threads.export_thread("t-alice") is None
    assert await threads.export_thread("t-bob") is not None
    assert await threads.delete_thread("t-alice") == 0
