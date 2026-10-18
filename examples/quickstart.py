# The Quickstart of docs/platform/library-usage.md, as a file you can run:
# an agent with one tool of your own, running one turn.
#
#   uv run --env-file .env python examples/quickstart.py
#
# Needs ANTHROPIC_API_KEY (and the `anthropic` extra outside this repository).
# tests/test_quickstart_example.py keeps this file and the documented block
# the same code; only comments may differ.
import asyncio

from langchain_core.messages import HumanMessage

import agents_system


# Your tool: a function from a dict of inputs to a dict.
def word_count(inputs: dict, /) -> dict:
    return {"words": len(inputs["text"].split())}


registry = agents_system.ToolRegistry()
registry.register(
    agents_system.ToolSpec(
        name="word_count",
        description="Count the words in a text.",
        input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
        required_permissions=(),  # pure computation: nothing to grant
        connector=word_count,
        tier=agents_system.Tier.T0,
    )
)
agent = agents_system.Agent(name="word-counter", tools=("word_count",))
equipped = agents_system.build_runtime(agent, registry, granted_permissions=[])


async def main(model):
    runtime = agents_system.AgentRuntime(equipped, model=model)
    return await runtime.run_turn(
        [HumanMessage("How many words are in 'the quick brown fox'?")],
        session_id="quickstart",
    )


if __name__ == "__main__":
    from langchain_anthropic import ChatAnthropic  # reads ANTHROPIC_API_KEY

    messages = asyncio.run(main(ChatAnthropic(model="claude-sonnet-5-5")))
    print(messages[-1].text)
