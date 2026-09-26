"""Issue #93 -- `Agent` API hardening.

Follow-ups from the adversarial reviews of #85 and #91. Every case below was
reproduced by a reviewer probe first:

1. `Agent(...)` parameters skipped the type checks the YAML path enforces.
2. The importer ceiling covered only `autonomy` and `execution_limits`: an
   importer agent could switch off audit redaction or wipe its parent's
   escalation conditions.
3. Execution limits accepted `-inf` and negative values.
4. `Agent.from_folder(p, skill_contents=...)` skipped the `skills` check at
   construction time.
5. An `Agent` could not be pickled, deep-copied or `dataclasses.asdict`-ed.

Every rejection is a `DefinitionError` naming the agent, the field and the
reason.
"""

from __future__ import annotations

import copy
import dataclasses
import math
import pathlib
import pickle
from typing import Any

import pytest

from agents_system.harness.loader import (
    DefinitionError,
    RootConfig,
    _validate_execution_limits,
    resolve,
)

ROOTS = RootConfig()


def _write_folder(
    base: pathlib.Path,
    name: str,
    *,
    extends: str | None = "agent",
    permissions: str = "[]",
    skills: str = "[]",
    policy_extra: str = "",
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
        f"skills: {skills}\ncontext: {{}}\npermissions: {permissions}\n---\n\nBody.\n",
        encoding="utf-8",
    )
    (folder / "policy.md").write_text(
        f'---\nrole: {name}\nversion: "1.0"\n{policy_extra}---\n\nBody.\n',
        encoding="utf-8",
    )
    return folder


def _message(exc: pytest.ExceptionInfo[DefinitionError], *parts: str) -> str:
    message = str(exc.value)
    for part in parts:
        assert part in message, f"{part!r} not in {message!r}"
    return message


# ---------------------------------------------------------------------------
# 1. Parameters are checked like the YAML path checks them
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["false", "no", "true", 0, 1, []])
def test_untrusted_input_must_be_a_real_bool(value: Any) -> None:
    """`'false'` and `'no'` used to resolve to True, `0` and `[]` to False."""
    from agents_system.agent.spec import Agent

    with pytest.raises(DefinitionError) as exc:
        Agent(name="bot", extends="agent", untrusted_input=value)

    _message(exc, "'bot'", "untrusted_input", "not a boolean")


@pytest.mark.parametrize("value", [True, False, None])
def test_untrusted_input_accepts_a_bool_or_none(value: bool | None) -> None:
    from agents_system.agent.spec import Agent

    definition = resolve(
        Agent(name="bot", extends="agent", untrusted_input=value)._to_locator(),
        roots=ROOTS,
    )

    assert definition.untrusted_input is bool(value)


@pytest.mark.parametrize("value", ["false", 0, None])
def test_from_folder_untrusted_input_override_is_checked_at_construction(
    tmp_path: pathlib.Path, value: Any
) -> None:
    """Same rule as a `policy.md` line: a non-bool raises, and an explicit
    `None` is ambiguous with not passing the override at all. Nothing is
    read from disk, so the folder need not exist."""
    from agents_system.agent.spec import Agent

    with pytest.raises(DefinitionError) as exc:
        Agent.from_folder(tmp_path / "never-created", untrusted_input=value)

    _message(exc, "'never-created'", "untrusted_input")


@pytest.mark.parametrize("field", ["tools", "permissions", "skills"])
def test_a_bare_string_is_one_item_not_its_characters(field: str) -> None:
    """A YAML scalar means a one-item list (`_as_str_list`). The Python
    parameter used to be split into single characters instead."""
    from agents_system.agent.spec import Agent

    agent = Agent(name="bot", extends="agent", **{field: "catalog_search"})

    assert getattr(agent, field) == ("catalog_search",)


def test_a_bare_string_tool_resolves_to_that_tool() -> None:
    from agents_system.agent.spec import Agent

    definition = resolve(
        Agent(name="bot", extends="agent", tools="catalog_search")._to_locator(),  # type: ignore[arg-type]
        roots=ROOTS,
    )

    assert "catalog_search" in definition.tools
    assert "c" not in definition.tools


def test_a_bare_string_from_folder_override_is_one_item(
    tmp_path: pathlib.Path,
) -> None:
    from agents_system.agent.spec import Agent

    folder = _write_folder(tmp_path, "bot")
    definition = resolve(
        Agent.from_folder(folder, tools="catalog_search")._to_locator(), roots=ROOTS
    )

    assert "catalog_search" in definition.tools
    assert "c" not in definition.tools


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tools", 5),
        ("tools", ["catalog_search", 5]),
        ("tools", {"catalog_search"}),
        ("skills", {"tone": "x"}),
        ("permissions", 5),
        ("permissions", ["read:catalog", None]),
    ],
)
def test_a_list_parameter_of_the_wrong_shape_raises(field: str, value: Any) -> None:
    from agents_system.agent.spec import Agent

    with pytest.raises(DefinitionError) as exc:
        Agent(name="bot", extends="agent", **{field: value})

    _message(exc, "'bot'", field)


def test_a_permissions_directive_is_honoured_inline_as_from_folder(
    tmp_path: pathlib.Path,
) -> None:
    """The removal used to be ignored inline, and the directive's keys became
    the permissions 'inherit' and 'remove'. `Agent.from_folder` handled the
    same dict correctly; both paths now resolve it the same way."""
    from agents_system.agent.spec import Agent

    directive = {"inherit": True, "remove": ["send:escalation"]}
    inline = resolve(
        Agent(name="bot", extends="agent", permissions=directive)._to_locator(),  # type: ignore[arg-type]
        roots=ROOTS,
    )
    folder = resolve(
        Agent.from_folder(
            _write_folder(tmp_path, "bot"), permissions=directive
        )._to_locator(),
        roots=ROOTS,
    )

    assert inline.permissions == folder.permissions == ("read:session",)


@pytest.mark.parametrize(
    "directive",
    [
        {"remove": ["send:escalation"]},
        {"inherit": False, "add": ["read:catalog"]},
        {"inherit": True, "remvoe": ["send:escalation"]},
    ],
)
def test_a_permissions_directive_the_loader_would_ignore_raises(
    directive: dict[str, Any],
) -> None:
    """The loader resolves a directive it does not recognise to the parent's
    full set, so a typo would keep the permission it meant to remove."""
    from agents_system.agent.spec import Agent

    with pytest.raises(DefinitionError) as exc:
        Agent(name="bot", extends="agent", permissions=directive)  # type: ignore[arg-type]

    _message(exc, "'bot'", "permissions")


@pytest.mark.parametrize(
    "field",
    [
        "context",
        "escalation_rules",
        "delegation_policy",
        "memory_policy",
        "audit_policy",
    ],
)
def test_a_mapping_parameter_must_be_a_mapping(field: str) -> None:
    from agents_system.agent.spec import Agent

    with pytest.raises(DefinitionError) as exc:
        Agent(name="bot", extends="agent", **{field: ["not", "a", "mapping"]})

    _message(exc, "'bot'", field)


def test_skill_contents_must_map_names_to_text() -> None:
    from agents_system.agent.spec import Agent

    with pytest.raises(DefinitionError) as exc:
        Agent(name="bot", skills=("tone",), skill_contents={"tone": 42})  # type: ignore[dict-item]

    _message(exc, "'bot'", "skill_contents")


# ---------------------------------------------------------------------------
# 2. The importer ceiling covers audit redaction and escalation conditions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [True, "yes", 1])
def test_an_importer_agent_cannot_switch_off_free_text_redaction(value: Any) -> None:
    """The reviewer's probe: the Redactor kept a DNI and a card number
    verbatim. Any truthy value switches redaction off at runtime, so any
    truthy value is refused."""
    from agents_system.agent.spec import Agent

    agent = Agent(
        name="bot", extends="sales-agent", audit_policy={"capture_tool_input": value}
    )

    with pytest.raises(DefinitionError) as exc:
        resolve(agent._to_locator(), roots=ROOTS)

    _message(exc, "'bot'", "capture_tool_input", "sales-agent")


def test_a_parentless_importer_agent_cannot_switch_off_redaction() -> None:
    from agents_system.agent.spec import Agent

    agent = Agent(
        name="bot",
        extends=None,  # type: ignore[arg-type]
        audit_policy={"capture_tool_input": True},
    )

    with pytest.raises(DefinitionError) as exc:
        resolve(agent._to_locator(), roots=ROOTS)

    _message(exc, "'bot'", "capture_tool_input", "platform default")


def test_a_folder_agent_cannot_switch_off_redaction(tmp_path: pathlib.Path) -> None:
    from agents_system.agent.spec import Agent

    folder = _write_folder(
        tmp_path, "bot", policy_extra="audit_policy:\n  capture_tool_input: true\n"
    )

    with pytest.raises(DefinitionError) as exc:
        resolve(Agent.from_folder(folder)._to_locator(), roots=ROOTS)

    _message(exc, "'bot'", "capture_tool_input")


def test_an_importer_agent_may_keep_redaction_on() -> None:
    from agents_system.agent.spec import Agent

    definition = resolve(
        Agent(
            name="bot",
            extends="sales-agent",
            audit_policy={"capture_tool_input": False, "redact_keys": ["dni"]},
        )._to_locator(),
        roots=ROOTS,
    )

    assert definition.audit_policy["capture_tool_input"] is False
    assert definition.audit_policy["redact_keys"] == ["dni"]


def test_an_importer_agent_cannot_drop_a_redact_key_its_parent_added() -> None:
    from agents_system.agent.spec import Agent

    parent = Agent(
        name="parent", extends="agent", audit_policy={"redact_keys": ["dni"]}
    )
    child = Agent(name="child", extends=parent, audit_policy={"redact_keys": ["cuit"]})

    with pytest.raises(DefinitionError) as exc:
        resolve(child._to_locator(), roots=ROOTS)

    _message(exc, "'child'", "redact_keys", "dni")


def test_an_importer_agent_may_add_redact_keys() -> None:
    from agents_system.agent.spec import Agent

    parent = Agent(
        name="parent", extends="agent", audit_policy={"redact_keys": ["dni"]}
    )
    child = Agent(
        name="child", extends=parent, audit_policy={"redact_keys": ["dni", "cuit"]}
    )

    definition = resolve(child._to_locator(), roots=ROOTS)

    assert definition.audit_policy["redact_keys"] == ["dni", "cuit"]


def test_redact_keys_must_be_a_list_of_strings() -> None:
    """The Redactor runs `set(redact_keys)`: a bare string would redact its
    characters, not the key it names."""
    from agents_system.agent.spec import Agent

    parent = Agent(
        name="parent", extends="agent", audit_policy={"redact_keys": ["dni"]}
    )
    child = Agent(name="child", extends=parent, audit_policy={"redact_keys": "dni"})

    with pytest.raises(DefinitionError) as exc:
        resolve(child._to_locator(), roots=ROOTS)

    _message(exc, "'child'", "redact_keys")


def test_importer_escalation_conditions_cannot_wipe_the_parents() -> None:
    """The reviewer's probe: `conditions: []` dropped every sales-agent
    condition, `customer_requests_human` included."""
    from agents_system.agent.spec import Agent

    sales = resolve("sales-agent", roots=ROOTS)
    definition = resolve(
        Agent(
            name="bot", extends="sales-agent", escalation_rules={"conditions": []}
        )._to_locator(),
        roots=ROOTS,
    )

    assert (
        definition.escalation_rules["conditions"]
        == sales.escalation_rules["conditions"]
    )
    assert "customer_requests_human" in definition.escalation_rules["conditions"]


def test_importer_escalation_conditions_are_added_after_the_parents() -> None:
    from agents_system.agent.spec import Agent

    sales = resolve("sales-agent", roots=ROOTS)
    definition = resolve(
        Agent(
            name="bot",
            extends="sales-agent",
            escalation_rules={"conditions": ["vip_complaint", "required_tool_missing"]},
        )._to_locator(),
        roots=ROOTS,
    )

    assert definition.escalation_rules["conditions"] == [
        *sales.escalation_rules["conditions"],
        "vip_complaint",
    ]


def test_a_folder_agents_escalation_conditions_are_additive_too(
    tmp_path: pathlib.Path,
) -> None:
    from agents_system.agent.spec import Agent

    folder = _write_folder(
        tmp_path,
        "bot",
        extends="sales-agent",
        policy_extra="escalation_rules:\n  conditions: [vip_complaint]\n",
    )

    definition = resolve(Agent.from_folder(folder)._to_locator(), roots=ROOTS)

    conditions = definition.escalation_rules["conditions"]
    assert "customer_requests_human" in conditions
    assert conditions[-1] == "vip_complaint"


def test_platform_role_folds_still_replace_conditions() -> None:
    """Out of scope: two platform roles are written by the same author.
    `sales-agent` restates its own list, which leaves out `agent`'s
    `explicit_user_request`."""
    agent = resolve("agent", roots=ROOTS)
    sales = resolve("sales-agent", roots=ROOTS)

    assert "explicit_user_request" in agent.escalation_rules["conditions"]
    assert "explicit_user_request" not in sales.escalation_rules["conditions"]


# ---------------------------------------------------------------------------
# 3. Execution limits are finite, non-negative numbers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [-math.inf, -1, -0.5])
def test_negative_limits_are_rejected(value: float) -> None:
    from agents_system.agent.spec import Agent

    agent = Agent(
        name="bot", extends="agent", execution_limits={"max_tool_calls": value}
    )

    with pytest.raises(DefinitionError) as exc:
        resolve(agent._to_locator(), roots=ROOTS)

    _message(exc, "'bot'", "max_tool_calls")


def test_a_bool_limit_is_not_a_number() -> None:
    """`isinstance(True, int)` holds, so `True` used to pass as a limit of 1."""
    from agents_system.agent.spec import Agent

    agent = Agent(
        name="bot", extends="agent", execution_limits={"max_tool_calls": True}
    )

    with pytest.raises(DefinitionError) as exc:
        resolve(agent._to_locator(), roots=ROOTS)

    _message(exc, "'bot'", "max_tool_calls", "not a number")


def test_a_zero_limit_is_a_valid_tightening() -> None:
    from agents_system.agent.spec import Agent

    definition = resolve(
        Agent(
            name="bot", extends="agent", execution_limits={"max_tool_calls": 0}
        )._to_locator(),
        roots=ROOTS,
    )

    assert definition.execution_limits == {"max_tool_calls": 0}


@pytest.mark.parametrize("value", [-1, -math.inf, math.inf, math.nan])
def test_the_deployment_boundary_shares_the_rule(value: float) -> None:
    with pytest.raises(DefinitionError, match="max_tool_calls"):
        _validate_execution_limits({}, {"max_tool_calls": value})


# ---------------------------------------------------------------------------
# 4. from_folder's skill_contents is checked like the inline one
# ---------------------------------------------------------------------------


def test_from_folder_skill_contents_is_checked_against_a_skills_override_at_construction(
    tmp_path: pathlib.Path,
) -> None:
    """With `skills=` passed too, both sides are known without reading the
    folder, so the check runs in `__post_init__`, like the inline one."""
    from agents_system.agent.spec import Agent

    with pytest.raises(DefinitionError) as exc:
        Agent.from_folder(
            tmp_path / "never-created",
            skills=["tone"],
            skill_contents={"tonee": "typo'd key"},
        )

    _message(exc, "'never-created'", "skill_contents", "tonee")


def test_from_folder_skill_contents_is_checked_against_the_folders_skills_at_resolve(
    tmp_path: pathlib.Path,
) -> None:
    """Without `skills=`, the folder's own list is only known once it is read,
    so the same check, with the same message, runs at resolve time."""
    from agents_system.agent.spec import Agent

    folder = _write_folder(tmp_path, "bot", skills="[tone]")
    agent = Agent.from_folder(folder, skill_contents={"tonee": "typo'd key"})

    with pytest.raises(DefinitionError) as exc:
        resolve(agent._to_locator(), roots=ROOTS)

    _message(exc, "'bot'", "skill_contents", "tonee")


def test_inline_skill_contents_error_names_the_agent() -> None:
    from agents_system.agent.spec import Agent

    with pytest.raises(DefinitionError) as exc:
        Agent(name="bot", skills=("tone",), skill_contents={"tonee": "x"})

    _message(exc, "'bot'", "skill_contents", "tonee")


# ---------------------------------------------------------------------------
# 5. An Agent survives pickle, deepcopy and asdict, and stays immutable
# ---------------------------------------------------------------------------


def _agents(tmp_path: pathlib.Path) -> list[Any]:
    from agents_system.agent.spec import Agent

    parent = Agent(
        name="parent",
        extends="agent",
        tools=("catalog_search",),
        permissions=("read:catalog",),
        context={"extra": {"list": [1, 2]}},
        execution_limits={"max_tool_calls": 5},
    )
    inline = Agent(
        name="child",
        extends=parent,
        skills=("tone",),
        skill_contents={"tone": "Be brief."},
        escalation_rules={"conditions": ["vip_complaint"]},
    )
    folder = Agent.from_folder(
        _write_folder(tmp_path, "bot"),
        extends=parent,
        permissions={"inherit": True, "add": ["read:catalog"]},
    )
    return [Agent(name="bare"), parent, inline, folder]


@pytest.mark.parametrize(
    "roundtrip",
    [
        pytest.param(lambda agent: pickle.loads(pickle.dumps(agent)), id="pickle"),
        pytest.param(copy.deepcopy, id="deepcopy"),
        pytest.param(copy.copy, id="copy"),
    ],
)
def test_an_agent_survives_a_copy_and_resolves_the_same(
    tmp_path: pathlib.Path, roundtrip: Any
) -> None:
    for agent in _agents(tmp_path):
        clone = roundtrip(agent)

        assert clone == agent
        assert resolve(clone._to_locator(), roots=ROOTS) == resolve(
            agent._to_locator(), roots=ROOTS
        )


def test_a_copied_agent_is_still_deeply_immutable(tmp_path: pathlib.Path) -> None:
    parent = _agents(tmp_path)[1]

    for clone in (pickle.loads(pickle.dumps(parent)), copy.deepcopy(parent)):
        with pytest.raises(TypeError):
            clone.context["new"] = 1
        with pytest.raises(TypeError):
            clone.context["extra"]["new"] = 1
        with pytest.raises(TypeError):
            clone.execution_limits["max_tool_calls"] = 999
        assert clone.context["extra"]["list"] == (1, 2)


def test_asdict_returns_the_agents_fields(tmp_path: pathlib.Path) -> None:
    for agent in _agents(tmp_path):
        as_dict = dataclasses.asdict(agent)

        assert as_dict["name"] == agent.name
        assert as_dict["tools"] == agent.tools

    inline = _agents(tmp_path / "again")[2]
    as_dict = dataclasses.asdict(inline)
    assert as_dict["extends"]["name"] == "parent"
    assert as_dict["extends"]["context"] == {"extra": {"list": (1, 2)}}
    assert as_dict["skill_contents"] == {"tone": "Be brief."}


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m.__setitem__("k", 1),
        lambda m: m.__delitem__("extra"),
        lambda m: m.clear(),
        lambda m: m.pop("extra"),
        lambda m: m.popitem(),
        lambda m: m.setdefault("k", 1),
        lambda m: m.update(k=1),
        lambda m: m.__ior__({"k": 1}),
    ],
)
def test_every_mapping_mutator_is_blocked(mutate: Any) -> None:
    from agents_system.agent.spec import Agent

    agent = Agent(name="bot", context={"extra": {"list": [1, 2]}})

    # AttributeError: a read-only mapping type may not have the method at all.
    with pytest.raises((TypeError, AttributeError)):
        mutate(agent.context)
    assert dict(agent.context) == {"extra": {"list": (1, 2)}}


def test_calling_init_again_does_not_refill_a_frozen_mapping() -> None:
    """`dict.__init__` merges into an existing dict, so a second call would
    otherwise be a way around the blocked mutators."""
    from agents_system.agent.spec import Agent

    agent = Agent(name="bot", context={"extra": {"list": [1, 2]}})

    agent.context.__init__({"k": 1})  # type: ignore[misc]

    assert dict(agent.context) == {"extra": {"list": (1, 2)}}
