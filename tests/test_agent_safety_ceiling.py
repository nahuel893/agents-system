"""Importer-authored agents are additive for capability, subtractive for safety.

PR #85 review, finding 1 (HIGH). `platform/roles/base/policy.md` states the
rule: a descendant may match or tighten `autonomy` and `execution_limits`,
never loosen or raise them (`execution_limits: null` means the platform
defaults). Role-to-role folds between two PLATFORM roles deliberately do not
enforce it (see `_fold_parent_into_child`), because the same author writes
both files. An `Agent(...)`, an `Agent.from_folder(...)` or any other
`FolderLocator`/`InlineLocator` is written by the importer instead, so every
hop whose child is importer-authored is checked against its parent's
effective values -- and an importer-authored root, which has no parent,
against the platform defaults.
"""

from __future__ import annotations

import math
import pathlib
from typing import Any

import pytest

from agents_system.harness.factory import build_runtime
from agents_system.harness.injector import ToolRegistry
from agents_system.harness.loader import (
    DefinitionError,
    InlineLocator,
    RawDefinition,
    RootConfig,
    resolve,
)

ROOTS = RootConfig()


def _write_folder(
    base: pathlib.Path,
    name: str,
    *,
    extends: str | None = None,
    autonomy: str = "supervised",
    limits_yaml: str = "null",
) -> pathlib.Path:
    folder = base / name
    folder.mkdir(parents=True)
    (folder / "role.md").write_text(
        f'---\nname: {name}\nversion: "1.0"\n---\n\n# Role: {name}\n\nBody.\n',
        encoding="utf-8",
    )
    extends_line = f"extends: {extends}\n" if extends else ""
    (folder / "manifest.md").write_text(
        f'---\nrole: {name}\nversion: "1.0"\n{extends_line}tools: []\n'
        "skills: []\ncontext: {}\npermissions: []\n---\n\nBody.\n",
        encoding="utf-8",
    )
    (folder / "policy.md").write_text(
        f'---\nrole: {name}\nversion: "1.0"\nautonomy: {autonomy}\n'
        f"execution_limits: {limits_yaml}\n---\n\nBody.\n",
        encoding="utf-8",
    )
    return folder


def _assert_clean_message(exc: pytest.ExceptionInfo[DefinitionError]) -> str:
    """Names things by agent name: no locator repr, no host path."""
    message = str(exc.value)
    assert "Locator(" not in message
    assert "RawDefinition(" not in message
    assert str(ROOTS.platform_root) not in message
    return message


# ---------------------------------------------------------------------------
# The reviewer's probes: Agent -> predefined role
# ---------------------------------------------------------------------------


def test_agent_cannot_loosen_a_predefined_roles_autonomy() -> None:
    from agents_system.agent.spec import Agent

    agent = Agent(
        name="rogue-sales", extends="platform/roles/sales-agent", autonomy="full"
    )

    with pytest.raises(DefinitionError) as exc:
        resolve(agent._to_locator(), roots=ROOTS)

    message = _assert_clean_message(exc)
    assert "autonomy" in message
    assert "'rogue-sales'" in message
    assert "'sales-agent'" in message
    assert "'full'" in message
    assert "'supervised'" in message
    assert "subtractive for safety" in message


def test_agent_cannot_raise_limits_above_the_platform_defaults_a_null_parent_means() -> (
    None
):
    from agents_system.agent.spec import Agent

    # sales-agent declares `execution_limits: null` -> the platform defaults.
    agent = Agent(
        name="rogue-sales",
        extends="platform/roles/sales-agent",
        execution_limits={"max_tool_calls": 999999},
    )

    with pytest.raises(DefinitionError) as exc:
        resolve(agent._to_locator(), roots=ROOTS)

    message = _assert_clean_message(exc)
    assert "execution_limits" in message
    assert "'rogue-sales'" in message
    assert "max_tool_calls=999999" in message
    assert "max_tool_calls=20" in message
    assert "subtractive for safety" in message


_SALES_GRANT = [
    "read:catalog",
    "read:client_registry",
    "write:orders",
    "write:order_items",
    "read:price_lists",
    "send:message",
    "read:session",
    "send:escalation",
]


@pytest.mark.parametrize(
    ("field", "loosening"),
    [
        ("autonomy", {"autonomy": "full"}),
        ("max_tool_calls", {"execution_limits": {"max_tool_calls": 999999}}),
    ],
)
def test_build_runtime_refuses_the_loosened_agent_before_equipping_anything(
    field: str, loosening: dict[str, Any]
) -> None:
    """The live-runtime half of the reviewer's probe: `build_runtime` resolves
    through the same loader, so the ceiling holds there too. The same
    registry and grant equip a well-behaved agent first, so the refusal is
    the ceiling and nothing else."""
    from conftest import build_test_registry

    from agents_system.agent.spec import Agent

    registry: ToolRegistry = build_test_registry()
    compliant = build_runtime(
        Agent(name="ok-sales", extends="platform/roles/sales-agent")._to_locator(),
        registry,
        granted_permissions=_SALES_GRANT,
        roots=ROOTS,
    )
    assert compliant.definition.autonomy == "supervised"

    rogue = Agent(name="rogue-sales", extends="platform/roles/sales-agent", **loosening)
    with pytest.raises(DefinitionError, match=field):
        build_runtime(
            rogue._to_locator(),
            registry,
            granted_permissions=_SALES_GRANT,
            roots=ROOTS,
        )


# ---------------------------------------------------------------------------
# Matching or tightening stays allowed
# ---------------------------------------------------------------------------


def test_agent_may_match_or_tighten_its_parent() -> None:
    from agents_system.agent.spec import Agent

    matched = resolve(
        Agent(
            name="same", extends="platform/roles/sales-agent", autonomy="supervised"
        )._to_locator(),
        roots=ROOTS,
    )
    assert matched.autonomy == "supervised"

    tightened = resolve(
        Agent(
            name="strict",
            extends="platform/roles/sales-agent",
            autonomy="confirm",
            execution_limits={"max_tool_calls": 5, "total_execution_timeout_s": 60},
        )._to_locator(),
        roots=ROOTS,
    )
    assert tightened.autonomy == "confirm"
    assert tightened.execution_limits is not None
    assert tightened.execution_limits["max_tool_calls"] == 5


def test_agent_extending_a_full_autonomy_role_may_keep_full() -> None:
    from agents_system.agent.spec import Agent

    definition = resolve(
        Agent(
            name="my-summary", extends="platform/roles/summary-agent", autonomy="full"
        )._to_locator(),
        roots=ROOTS,
    )
    assert definition.autonomy == "full"


# ---------------------------------------------------------------------------
# The parent's EFFECTIVE value is the ceiling, not just the platform default
# ---------------------------------------------------------------------------


def test_agent_cannot_raise_a_limit_its_parent_agent_tightened() -> None:
    from agents_system.agent.spec import Agent

    parent = Agent(
        name="tight", extends="agent", execution_limits={"max_tool_calls": 5}
    )
    # 10 is under the platform default (20) but above the parent's 5.
    child = Agent(name="loose", extends=parent, execution_limits={"max_tool_calls": 10})

    with pytest.raises(DefinitionError) as exc:
        resolve(child._to_locator(), roots=ROOTS)

    message = _assert_clean_message(exc)
    assert "'loose'" in message
    assert "'tight'" in message
    assert "max_tool_calls=10" in message
    assert "max_tool_calls=5" in message


def test_a_null_limit_on_the_child_means_the_platform_default_and_is_checked() -> None:
    """`graph._effective_limits` reads a null value as "use the platform
    default", so a child writing `max_tool_calls: None` under a parent that
    set 5 would run with 20. Rejected like any other raise."""
    from agents_system.agent.spec import Agent

    parent = Agent(
        name="tight", extends="agent", execution_limits={"max_tool_calls": 5}
    )
    child = Agent(
        name="nulled", extends=parent, execution_limits={"max_tool_calls": None}
    )

    with pytest.raises(DefinitionError, match="max_tool_calls"):
        resolve(child._to_locator(), roots=ROOTS)


def test_a_null_limit_under_a_null_parent_is_the_platform_default_and_allowed() -> None:
    from agents_system.agent.spec import Agent

    agent = Agent(
        name="plain", extends="agent", execution_limits={"max_tool_calls": None}
    )

    resolve(agent._to_locator(), roots=ROOTS)


@pytest.mark.parametrize("value", [math.nan, math.inf])
def test_non_finite_limits_fail_closed(value: float) -> None:
    from agents_system.agent.spec import Agent

    agent = Agent(
        name="odd", extends="agent", execution_limits={"max_tool_calls": value}
    )

    with pytest.raises(DefinitionError, match="max_tool_calls"):
        resolve(agent._to_locator(), roots=ROOTS)


def test_a_non_numeric_limit_is_a_definition_error_not_a_type_error() -> None:
    from agents_system.agent.spec import Agent

    agent = Agent(
        name="odd", extends="agent", execution_limits={"max_tool_calls": "999999"}
    )

    with pytest.raises(DefinitionError) as exc:
        resolve(agent._to_locator(), roots=ROOTS)

    message = _assert_clean_message(exc)
    assert "max_tool_calls" in message
    assert "not a number" in message


# ---------------------------------------------------------------------------
# Every importer hop: Agent -> Agent -> folder Agent -> predefined role
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("loosening_hop", ["folder", "middle", "top"])
def test_every_importer_hop_is_checked(
    tmp_path: pathlib.Path, loosening_hop: str
) -> None:
    from agents_system.agent.spec import Agent

    folder = _write_folder(
        tmp_path / "agents",
        "mid-folder",
        extends="platform/roles/sales-agent",
        autonomy="full" if loosening_hop == "folder" else "supervised",
    )
    folder_agent = Agent.from_folder(folder)
    middle = Agent(
        name="middle",
        extends=folder_agent,
        autonomy="full" if loosening_hop == "middle" else "",
    )
    top = Agent(
        name="top", extends=middle, autonomy="full" if loosening_hop == "top" else ""
    )

    with pytest.raises(DefinitionError) as exc:
        resolve(top._to_locator(), roots=ROOTS)

    message = _assert_clean_message(exc)
    assert str(tmp_path) not in message
    expected_agent = {"folder": "mid-folder", "middle": "middle", "top": "top"}
    assert f"'{expected_agent[loosening_hop]}'" in message


def test_a_clean_mixed_chain_still_resolves(tmp_path: pathlib.Path) -> None:
    from agents_system.agent.spec import Agent

    folder = _write_folder(
        tmp_path / "agents", "mid-folder", extends="platform/roles/sales-agent"
    )
    top = Agent(
        name="top",
        extends=Agent(name="middle", extends=Agent.from_folder(folder)),
        autonomy="confirm",
    )

    definition = resolve(top._to_locator(), roots=ROOTS)

    assert definition.autonomy == "confirm"


def test_from_folder_python_overrides_are_checked_too(tmp_path: pathlib.Path) -> None:
    from agents_system.agent.spec import Agent

    folder = _write_folder(
        tmp_path / "agents", "mid-folder", extends="platform/roles/sales-agent"
    )

    with pytest.raises(DefinitionError, match="autonomy"):
        resolve(Agent.from_folder(folder, autonomy="full")._to_locator(), roots=ROOTS)
    with pytest.raises(DefinitionError, match="max_tool_calls"):
        resolve(
            Agent.from_folder(
                folder, execution_limits={"max_tool_calls": 999}
            )._to_locator(),
            roots=ROOTS,
        )


# ---------------------------------------------------------------------------
# An importer-authored ROOT has no parent: the platform defaults are its ceiling
# ---------------------------------------------------------------------------


def test_a_parentless_folder_agent_cannot_exceed_the_platform_defaults(
    tmp_path: pathlib.Path,
) -> None:
    from agents_system.agent.spec import Agent

    full = _write_folder(tmp_path / "agents", "full-root", autonomy="full")
    with pytest.raises(DefinitionError) as exc:
        resolve(Agent.from_folder(full)._to_locator(), roots=ROOTS)
    message = _assert_clean_message(exc)
    assert str(tmp_path) not in message
    assert "'full-root'" in message
    assert "platform default" in message

    unbounded = _write_folder(
        tmp_path / "agents",
        "unbounded-root",
        limits_yaml="\n  total_execution_timeout_s: 999999",
    )
    with pytest.raises(DefinitionError, match="total_execution_timeout_s=999999"):
        resolve(Agent.from_folder(unbounded)._to_locator(), roots=ROOTS)


def test_a_parentless_inline_definition_cannot_exceed_the_platform_defaults() -> None:
    raw = RawDefinition(
        role_name="bare-inline",
        version="1.0",
        deployment=None,
        system_prompt="",
        tools=[],
        skills=[],
        context={},
        permissions=[],
        autonomy="full",
        escalation_rules={},
        delegation_policy={},
        memory_policy={},
        audit_policy={},
        execution_limits=None,
    )

    with pytest.raises(DefinitionError, match="'bare-inline'"):
        resolve(InlineLocator(raw=raw), roots=ROOTS)


# ---------------------------------------------------------------------------
# Platform-tree folds are untouched (out of scope for this fix)
# ---------------------------------------------------------------------------


def test_platform_role_to_role_folds_keep_their_existing_behaviour() -> None:
    """`summary-agent` declares `full` under a `supervised` chain. That is a
    platform-authored fold and stays allowed here."""
    assert resolve("summary-agent", roots=ROOTS).autonomy == "full"
