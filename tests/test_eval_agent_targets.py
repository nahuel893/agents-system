"""#221 (ADR-010): a scenario can target a predefined role, an agent folder
under a configured root, or an inline ``Agent`` passed from Python.

Only the model is faked; resolve()/build_runtime()/AgentRuntime are real.
"""

from __future__ import annotations

import pathlib
import textwrap
from collections.abc import Sequence
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from agents_system.agent.spec import Agent
from agents_system.evals.runner import AGENT_KIND_FOLDER, run_scenario
from agents_system.evals.schema import (
    Scenario,
    ScenarioAssertions,
    ScenarioError,
    load_scenario,
)
from agents_system.harness.loader import PredefinedRoleDeprecationWarning


class ToolAwareFakeModel(FakeMessagesListChatModel):
    def bind_tools(  # type: ignore[override]
        self, tools: Sequence[Any], **kwargs: Any
    ) -> ToolAwareFakeModel:
        return self


def _model() -> ToolAwareFakeModel:
    return ToolAwareFakeModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "c1",
                        "name": "catalog_search",
                        "args": {"q": "alpha"},
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="Found it."),
        ]
    )


def _write_folder(root: pathlib.Path, name: str = "stock-bot") -> pathlib.Path:
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "role.md").write_text(
        f"---\nname: {name}\nversion: '1.0'\n---\n\n# Role: {name}\n\nAnswers stock questions.\n"
    )
    (folder / "manifest.md").write_text(
        textwrap.dedent(
            f"""\
            ---
            role: {name}
            extends: platform/roles/agent
            version: "1.0"
            tools: [catalog_search]
            permissions:
              - read:catalog
            ---

            # Manifest
            """
        )
    )
    (folder / "policy.md").write_text(
        f"---\nrole: {name}\nversion: '1.0'\nautonomy: supervised\n---\n\n# Policy\n"
    )
    return folder


def _scenario(**kwargs: Any) -> Scenario:
    kwargs.setdefault(
        "assertions", ScenarioAssertions(tools_called=("catalog_search",))
    )
    return Scenario(name="targets", turns=("Do you have alpha?",), **kwargs)


async def test_inline_agent_runs_and_reports_identifier_and_kind() -> None:
    from conftest import build_test_registry

    agent = Agent(
        name="triage-bot",
        extends="agent",
        tools=("catalog_search",),
        permissions=("read:catalog",),
    )
    result = await run_scenario(
        _scenario(),
        model=_model(),
        model_name="fake",
        registry=build_test_registry(),
        agent=agent,
    )
    assert result.runs[0].passed, result.runs[0].failures
    assert result.agent == "triage-bot"
    assert result.agent_kind == "inline"
    assert result.to_dict()["agent"] == "triage-bot"
    assert result.to_dict()["agent_kind"] == "inline"


async def test_folder_agent_runs_under_the_configured_root(
    tmp_path: pathlib.Path,
) -> None:
    from conftest import build_test_registry

    _write_folder(tmp_path)
    result = await run_scenario(
        _scenario(agent_folder="stock-bot"),
        model=_model(),
        model_name="fake",
        registry=build_test_registry(),
        agents_root=tmp_path,
    )
    assert result.runs[0].passed, result.runs[0].failures
    assert result.agent == "stock-bot"
    assert result.agent_kind == AGENT_KIND_FOLDER


async def test_role_scenario_keeps_resolving_and_reports_kind_role() -> None:
    from conftest import build_test_registry

    # ADR-010 (#222): a role scenario names a concrete predefined role, which
    # still resolves and is deprecated.
    with pytest.warns(
        PredefinedRoleDeprecationWarning, match="examples/agents/sales-agent/"
    ):
        result = await run_scenario(
            _scenario(role="sales-agent"),
            model=_model(),
            model_name="fake",
            registry=build_test_registry(),
        )
    assert result.agent == "sales-agent"
    assert result.role == "sales-agent"
    assert result.agent_kind == "role"
    assert result.runs[0].passed, result.runs[0].failures


async def test_folder_explicit_empty_grant_overrides_the_all_declared_default(
    tmp_path: pathlib.Path,
) -> None:
    from conftest import build_test_registry

    _write_folder(tmp_path)
    scenario = _scenario(
        agent_folder="stock-bot",
        granted_permissions=(),
        assertions=ScenarioAssertions(tools_not_called=("catalog_search",)),
    )
    result = await run_scenario(
        scenario,
        model=ToolAwareFakeModel(responses=[AIMessage(content="No tools.")]),
        model_name="fake",
        registry=build_test_registry(),
        agents_root=tmp_path,
    )
    assert result.runs[0].passed


@pytest.mark.parametrize("folder", ["../outside", "/etc", "stock-bot/../../x"])
async def test_folder_escaping_the_root_fails_before_any_model_call(
    tmp_path: pathlib.Path, folder: str
) -> None:
    from conftest import build_test_registry

    root = tmp_path / "agents"
    _write_folder(root)
    _write_folder(tmp_path, "outside")

    class Boom(ToolAwareFakeModel):
        def _generate(self, *a: Any, **k: Any) -> Any:
            raise AssertionError("model must not be called")

    with pytest.raises(ScenarioError, match="escapes"):
        await run_scenario(
            _scenario(agent_folder=folder),
            model=Boom(responses=[AIMessage(content="x")]),
            model_name="fake",
            registry=build_test_registry(),
            agents_root=root,
        )


async def test_symlink_escaping_the_root_fails(tmp_path: pathlib.Path) -> None:
    from conftest import build_test_registry

    root = tmp_path / "agents"
    root.mkdir()
    _write_folder(tmp_path, "real")
    (root / "link").symlink_to(tmp_path / "real")
    with pytest.raises(ScenarioError, match="escapes"):
        await run_scenario(
            _scenario(agent_folder="link"),
            model=_model(),
            model_name="fake",
            registry=build_test_registry(),
            agents_root=root,
        )


async def test_missing_folder_and_missing_root_fail_clearly(
    tmp_path: pathlib.Path,
) -> None:
    from conftest import build_test_registry

    with pytest.raises(ScenarioError, match="not found"):
        await run_scenario(
            _scenario(agent_folder="nope"),
            model=_model(),
            model_name="fake",
            registry=build_test_registry(),
            agents_root=tmp_path,
        )
    with pytest.raises(ScenarioError, match="agents_root"):
        await run_scenario(
            _scenario(agent_folder="nope"),
            model=_model(),
            model_name="fake",
            registry=build_test_registry(),
        )


async def test_scenario_without_any_target_fails(tmp_path: pathlib.Path) -> None:
    from conftest import build_test_registry

    with pytest.raises(ScenarioError, match="no agent"):
        await run_scenario(
            _scenario(),
            model=_model(),
            model_name="fake",
            registry=build_test_registry(),
        )


def test_scenario_cannot_name_both_a_role_and_a_folder() -> None:
    with pytest.raises(ScenarioError, match="both"):
        _scenario(role="sales-agent", agent_folder="x")


def test_yaml_agent_key_loads_and_role_key_is_unchanged(
    tmp_path: pathlib.Path,
) -> None:
    f = tmp_path / "s.yaml"
    f.write_text("agent: stock-bot\nturns: [hi]\n")
    s = load_scenario(f)
    assert s.agent_folder == "stock-bot"
    assert s.role == ""
    g = tmp_path / "t.yaml"
    g.write_text("role: sales-agent\nturns: [hi]\n")
    assert load_scenario(g).role == "sales-agent"
    h = tmp_path / "u.yaml"
    h.write_text("role: sales-agent\nagent: x\nturns: [hi]\n")
    with pytest.raises(ScenarioError, match="both"):
        load_scenario(h)
    i = tmp_path / "v.yaml"
    i.write_text("turns: [hi]\n")
    with pytest.raises(ScenarioError, match="'role' or 'agent'"):
        load_scenario(i)


async def test_shipped_fixture_scenario_runs_offline() -> None:
    from conftest import build_test_registry

    root = pathlib.Path(__file__).parent / "fixtures" / "agents" / "eval_targets"
    result = await run_scenario(
        load_scenario(root / "stock_lookup.yaml"),
        model=_model(),
        model_name="fake",
        registry=build_test_registry(),
        agents_root=root,
    )
    assert result.runs[0].passed, result.runs[0].failures
    assert (result.agent, result.agent_kind) == ("stock-bot", "folder")


def test_scenario_keeps_positional_name_role_turns_order() -> None:
    """Pre-#221 callers built ``Scenario(name, role, turns)`` positionally."""
    scenario = Scenario("old", "sales-agent", ("hello",))

    assert scenario.name == "old"
    assert scenario.role == "sales-agent"
    assert scenario.turns == ("hello",)
    assert scenario.assertions == ScenarioAssertions()


def test_scenario_rejects_an_empty_agent_folder() -> None:
    with pytest.raises(ScenarioError, match="must be non-empty"):
        _scenario(agent_folder="")
    with pytest.raises(ScenarioError, match="must be non-empty"):
        _scenario(agent_folder="   ")


async def test_a_role_with_an_inline_agent_names_its_agent_more_than_once() -> None:
    from conftest import build_test_registry

    with pytest.raises(ScenarioError, match="names its agent more than once"):
        await run_scenario(
            _scenario(role="sales-agent"),
            model=_model(),
            model_name="fake",
            registry=build_test_registry(),
            agent=Agent(name="inline-bot", extends="agent"),
        )


async def test_no_audit_sink_starts_when_target_resolution_fails() -> None:
    from conftest import build_test_registry

    from agents_system.evals.runner import _CapturingAuditSink

    built: list[_CapturingAuditSink] = []

    def factory() -> _CapturingAuditSink:
        sink = _CapturingAuditSink()
        built.append(sink)
        return sink

    with pytest.raises(ScenarioError, match="no agent"):
        await run_scenario(
            _scenario(),
            model=_model(),
            model_name="fake",
            registry=build_test_registry(),
            audit_sink_factory=factory,
        )

    assert built == []
