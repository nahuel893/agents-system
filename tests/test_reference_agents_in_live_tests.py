"""ADR-010 (#225): no live test resolves a concrete predefined role by name.

`pyproject.toml` turns `PredefinedRoleDeprecationWarning` into an error for
the whole suite, live tests included, and the loader emits it whenever a
concrete role is resolved by name. So a live test that resolved one would
fail on its own, unless the module silenced the warning. This file checks
that the error filter is in place, that no live module silences it, and
that the scenario files a live module loads name an agent folder under
`examples/agents/` instead of a role.
"""

from __future__ import annotations

import pathlib
import re
import tomllib

import pytest

from agents_system.evals.schema import load_scenarios
from agents_system.harness import loader

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_TESTS = _REPO_ROOT / "tests"
_REFERENCE_AGENTS_ROOT = _REPO_ROOT / "examples" / "agents"
_DEPRECATION = "agents_system.harness.loader.PredefinedRoleDeprecationWarning"
# The filter names the stdlib category and matches the message: a dotted
# custom category would make pytest import `agents_system` before pytest-cov
# starts (see the comment above `filterwarnings` in pyproject.toml).
_DEPRECATION_FILTER_PREFIX = "error:Predefined role "
_DEPRECATION_FILTER_SUFFIX = ":DeprecationWarning"

_LIVE_MODULES = sorted(_TESTS.glob("test_live_*.py"))

#: Scenario directories a live module loads from disk.
_LIVE_SCENARIO_DIRS = (_REPO_ROOT / "evals" / "scenarios" / "guardrails",)


def test_the_deprecation_warning_is_an_error_for_every_test() -> None:
    config = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text("utf-8"))
    filters = config["tool"]["pytest"]["ini_options"]["filterwarnings"]

    (entry,) = [f for f in filters if f.startswith(_DEPRECATION_FILTER_PREFIX)]
    assert entry.endswith(_DEPRECATION_FILTER_SUFFIX)
    assert _DEPRECATION not in entry


def test_the_deprecation_filter_matches_the_message_the_loader_emits() -> None:
    config = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text("utf-8"))
    filters = config["tool"]["pytest"]["ini_options"]["filterwarnings"]
    (entry,) = [f for f in filters if f.startswith(_DEPRECATION_FILTER_PREFIX)]
    message = entry.removeprefix("error:").removesuffix(_DEPRECATION_FILTER_SUFFIX)

    with pytest.warns(DeprecationWarning) as caught:
        loader._warn_deprecated_role("sales-agent")

    assert re.match(message, str(caught[0].message))
    assert not re.match(message, "pkg.thing is deprecated")


def test_the_live_modules_are_found() -> None:
    names = {module.name for module in _LIVE_MODULES}

    assert {"test_live_smoke.py", "test_live_eval_composition.py"} <= names


@pytest.mark.parametrize("module", _LIVE_MODULES, ids=lambda module: module.name)
def test_no_live_module_silences_the_predefined_role_deprecation(
    module: pathlib.Path,
) -> None:
    assert "PredefinedRoleDeprecationWarning" not in module.read_text("utf-8"), (
        f"{module.name} silences the deprecation, so it could resolve a "
        "concrete predefined role by name; use a reference agent from "
        "examples/agents/ instead"
    )


def test_the_per_role_live_drivers_are_gone() -> None:
    """The per-role scenarios left the live gate (ADR-010 open question 5);
    composition scenarios on reference agents replace them."""
    assert not (_TESTS / "test_live_eval_roles.py").exists()
    assert not (_TESTS / "test_live_eval_sales_agent.py").exists()


def test_every_live_scenario_names_a_reference_agent_folder() -> None:
    reference_agents = {
        path.name for path in _REFERENCE_AGENTS_ROOT.iterdir() if path.is_dir()
    }
    scenarios = [
        scenario
        for directory in _LIVE_SCENARIO_DIRS
        for scenario in load_scenarios(directory)
    ]

    assert scenarios
    for scenario in scenarios:
        assert scenario.role == "", (
            f"{scenario.name} names the predefined role {scenario.role!r}"
        )
        assert scenario.agent_folder in reference_agents, (
            f"{scenario.name} names {scenario.agent_folder!r}, which is not a "
            "reference agent folder"
        )
