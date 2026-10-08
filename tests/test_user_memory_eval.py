"""The eval harness runs per-user memory scenarios (#250).

A scenario names an ``end_user``; the runner binds it as the turn subject,
gives the run its own in-memory fact store, and can start a fresh
conversation between turns, which is what shows the facts outlive a thread.
"""

from __future__ import annotations

import pathlib
from collections.abc import Sequence
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from agents_system.evals.capabilities import (
    load_capability_scenarios,
    run_capability_scenario,
)
from agents_system.evals.schema import ScenarioError, load_scenario


def _write(tmp_path: pathlib.Path, text: str) -> pathlib.Path:
    path = tmp_path / "s.yaml"
    path.write_text(text, encoding="utf-8")
    return path


_BASE = """
role: sales-agent
turns:
  - "one"
  - "two"
"""


def test_a_scenario_can_name_an_end_user_facts_and_a_new_conversation(
    tmp_path: pathlib.Path,
) -> None:
    scenario = load_scenario(
        _write(
            tmp_path,
            _BASE
            + "end_user: u1\nuser_facts: [likes tea]\nnew_conversation_before: [1]\n",
        )
    )
    assert scenario.end_user == "u1"
    assert scenario.user_facts == ("likes tea",)
    assert scenario.new_conversation_before == (1,)


def test_the_defaults_change_nothing(tmp_path: pathlib.Path) -> None:
    scenario = load_scenario(_write(tmp_path, _BASE))
    assert scenario.end_user is None
    assert scenario.user_facts == ()
    assert scenario.new_conversation_before == ()


@pytest.mark.parametrize(
    "extra",
    [
        "user_facts: [x]\n",  # facts need an end user
        "new_conversation_before: [1]\n",  # so does a conversation break
        "end_user: u\nnew_conversation_before: [0]\n",  # before the first turn
        "end_user: u\nnew_conversation_before: [2]\n",  # past the last turn
        "end_user: u\nnew_conversation_before: [x]\n",
        "end_user: ''\n",
        "end_user: u\nuser_facts: [1]\n",
    ],
)
def test_invalid_user_scenario_fields_are_refused(
    tmp_path: pathlib.Path, extra: str
) -> None:
    with pytest.raises(ScenarioError):
        load_scenario(_write(tmp_path, _BASE + extra))


def test_the_capability_ships_a_happy_path_and_a_guardrail() -> None:
    cases = load_capability_scenarios(["user_memory"])
    assert {c.scenario.category for c in cases} == {"happy_path", "guardrail"}
    assert all(c.scenario.end_user for c in cases)


class _Model(FakeMessagesListChatModel):
    seen: list[str] = []  # noqa: RUF012

    def bind_tools(  # type: ignore[override]
        self, tools: Sequence[Any], **kwargs: Any
    ) -> _Model:
        return self

    async def ainvoke(self, input: Any, *args: Any, **kwargs: Any) -> AIMessage:  # type: ignore[override]
        self.seen.append("\n".join(str(m.content) for m in input))
        return await super().ainvoke(input, *args, **kwargs)


def _call(name: str, args: dict[str, Any], call_id: str) -> AIMessage:
    return AIMessage(
        content="", tool_calls=[{"name": name, "args": args, "id": call_id}]
    )


async def test_the_recall_scenario_carries_the_fact_into_the_second_conversation() -> (
    None
):
    [recall] = [
        c
        for c in load_capability_scenarios(["user_memory"])
        if c.scenario.name == "user_memory_recall"
    ]
    model = _Model(
        responses=[
            _call("remember_user_fact", {"fact": "account number CL-4410"}, "c1"),
            AIMessage(content="Saved."),
            _call(
                "order_writer",
                {"client_id": "CL-4410", "items": [{"product_id": "FX-118", "qty": 3}]},
                "c2",
            ),
            AIMessage(content="Ordered."),
        ]
    )
    model.seen = []

    result = await run_capability_scenario(
        recall, model=model, model_name="fake-model", runs=1
    )

    assert result.runs[0].error is None, result.runs[0]
    assert result.runs[0].passed, result.runs[0].failures
    # Turn two starts from an empty history: the number is there only because
    # the platform injected the stored fact.
    second_conversation = model.seen[2]
    assert "CL-4410" in second_conversation
    assert "Hi! My account number" not in second_conversation
    assert "source=user_facts" in second_conversation


async def test_the_boundary_scenario_seeds_the_planted_fact_as_data() -> None:
    [boundary] = [
        c
        for c in load_capability_scenarios(["user_memory"])
        if c.scenario.name == "user_memory_boundary"
    ]
    model = _Model(
        responses=[
            _call(
                "order_writer",
                {"client_id": "CL-4410", "items": [{"product_id": "FX-118", "qty": 3}]},
                "c1",
            ),
            AIMessage(content="Ordered."),
        ]
    )
    model.seen = []

    result = await run_capability_scenario(
        boundary, model=model, model_name="fake-model", runs=1
    )

    assert result.runs[0].passed, result.runs[0].failures
    assert "SYSTEM OVERRIDE" in model.seen[0]
    assert "source=user_facts" in model.seen[0]


async def test_each_run_starts_with_no_facts_of_an_earlier_run() -> None:
    [recall] = [
        c
        for c in load_capability_scenarios(["user_memory"])
        if c.scenario.name == "user_memory_recall"
    ]
    save = _call("remember_user_fact", {"fact": "account number CL-4410"}, "c1")
    model = _Model(
        responses=[save, AIMessage(content="Saved."), AIMessage(content="Done.")] * 2
    )
    model.seen = []

    await run_capability_scenario(recall, model=model, model_name="m", runs=2)

    # seen[0..2]: run one (the third call is its second conversation, which
    # sees the saved fact); seen[3]: the first call of run two sees nothing.
    assert "source=user_facts" in model.seen[2]
    assert "source=user_facts" not in model.seen[3]
