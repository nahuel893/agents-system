"""Offline tests for `scripts/agents_extending.py` (#227, ADR-010 part 3):
list every agent that extends a given agent, read from the `extends:` graph
across `platform/roles/` and `examples/agents/`."""

from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest
from scripts.agents_extending import UnknownAgentError, agents_extending, build_graph

REPO = pathlib.Path(__file__).resolve().parent.parent


def _agent(root: pathlib.Path, name: str, extends: str | None) -> None:
    folder = root / name
    folder.mkdir(parents=True)
    line = f"extends: {extends}\n" if extends else ""
    (folder / "manifest.md").write_text(
        f'---\nrole: {name}\nversion: "1.0"\n{line}tools: []\n---\n', encoding="utf-8"
    )


@pytest.fixture
def roots(tmp_path: pathlib.Path) -> list[pathlib.Path]:
    platform = tmp_path / "platform"
    examples = tmp_path / "examples"
    _agent(platform, "base", None)
    _agent(platform, "agent", "platform/roles/base")
    _agent(examples, "operator", "platform/roles/agent")
    _agent(examples, "developer", "../operator")  # sibling, relative form
    _agent(examples, "auditor", "developer")  # bare-name form, two levels deep
    _agent(examples, "sales", "platform/roles/agent")
    return [platform, examples]


def test_lists_direct_and_transitive_extenders(roots: list[pathlib.Path]) -> None:
    assert agents_extending("operator", roots) == ["auditor", "developer"]


def test_direct_only_stops_at_one_level(roots: list[pathlib.Path]) -> None:
    assert agents_extending("operator", roots, direct=True) == ["developer"]


def test_leaf_agent_has_no_extenders(roots: list[pathlib.Path]) -> None:
    assert agents_extending("sales", roots) == []


def test_all_three_extends_spellings_resolve_to_the_same_node(
    roots: list[pathlib.Path],
) -> None:
    graph = build_graph(roots)
    assert graph["developer"] == "operator"
    assert graph["operator"] == "agent"
    assert graph["auditor"] == "developer"


def test_unknown_agent_is_an_error_not_an_empty_list(
    roots: list[pathlib.Path],
) -> None:
    with pytest.raises(UnknownAgentError, match="nope"):
        agents_extending("nope", roots)


def test_a_cycle_terminates(tmp_path: pathlib.Path) -> None:
    _agent(tmp_path, "a", "b")
    _agent(tmp_path, "b", "a")
    assert agents_extending("a", [tmp_path]) == ["b"]


def test_real_repo_operator_agent_lists_developer_agent() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/agents_extending.py", "operator-agent"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.split() == ["developer-agent"]


def test_real_repo_agent_lists_every_reference_agent() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/agents_extending.py", "agent"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    assert {
        "sales-agent",
        "operator-agent",
        "developer-agent",
        "data-agent",
    } <= set(result.stdout.split())


def test_cli_unknown_agent_exits_nonzero() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/agents_extending.py", "nope-agent"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "nope-agent" in result.stderr
