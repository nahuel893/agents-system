"""Issue #106 -- the loader fails closed on malformed directives, and the
deployment boundary holds the same safety ceiling importer agents have.

Every case below was reproduced by a reviewer probe first (the adversarial
review of #94):

1. `dataclasses.replace` on a folder agent silently dropped the fields it
   set, `untrusted_input` included (P16).
2. `_resolve_list_directive` resolved an unrecognised mapping to the
   parent's FULL set, so a manifest's `{remove: [...]}` kept what it meant
   to remove (P6b). Every path now rejects it: Agent parameters, folder
   manifests, predefined-role folds and deployments.
3. A deployment could wipe its role's escalation conditions and switch off
   audit redaction (P17).
4. An importer could reword an inherited escalation condition or blank
   `escalate_to` (P10). The review of #112 found the same rule skipped for
   a non-`dict` mapping, a `descriptions` that is not a mapping, and a
   deployment mapping that leaves `escalate_to` or the descriptions out.
5. An int limit too large for a float escaped as `OverflowError` (P1).
6. The base `dict` methods mutated an Agent's "frozen" mapping.

Every rejection is a `DefinitionError` naming the agent, role or deployment,
the field and the reason.
"""

from __future__ import annotations

import dataclasses
import pathlib
from types import MappingProxyType
from typing import Any

import pytest

from agents_system.harness.loader import (
    DefinitionError,
    FolderLocator,
    InlineLocator,
    RawDefinition,
    RootConfig,
    _resolve_list_directive,
    _resolve_mapping_directive,
    _validate_execution_limits,
    resolve,
)

ROOTS = RootConfig()


def _raw(name: str) -> RawDefinition:
    """An empty importer definition, as a locator built directly holds it."""
    return RawDefinition(
        role_name=name,
        version="1.0",
        deployment=None,
        system_prompt="",
        tools=[],
        skills=[],
        context={},
        permissions=[],
        autonomy="",
        escalation_rules={},
        delegation_policy={},
        memory_policy={},
        audit_policy={},
        execution_limits=None,
        untrusted_input=None,
    )


def _message(exc: pytest.ExceptionInfo[DefinitionError], *parts: str) -> str:
    message = str(exc.value)
    for part in parts:
        assert part in message, f"{part!r} not in {message!r}"
    return message


def _write(folder: pathlib.Path, name: str, text: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(text, encoding="utf-8")


def _write_agent_folder(
    base: pathlib.Path,
    name: str,
    *,
    extends: str | None = "sales-agent",
    manifest_extra: str = "",
    policy_extra: str = "",
    policy_body: str = "Body.\n",
) -> pathlib.Path:
    """An importer folder, as `probe_sec_1.py`/`probe_sec_2.py` wrote it."""
    folder = base / name
    _write(folder, "role.md", f'---\nname: {name}\nversion: "1.0"\n---\n\nBody.\n')
    extends_line = f"extends: {extends}\n" if extends else ""
    _write(
        folder,
        "manifest.md",
        f'---\nrole: {name}\nversion: "1.0"\n{extends_line}tools: []\n'
        f"skills: []\ncontext: {{}}\n{manifest_extra}---\n\nBody.\n",
    )
    _write(
        folder,
        "policy.md",
        f'---\nrole: {name}\nversion: "1.0"\n{policy_extra}---\n\n{policy_body}',
    )
    return folder


def _write_platform_role(
    platform_root: pathlib.Path,
    name: str,
    *,
    extends: str | None = None,
    manifest_extra: str = "permissions: [read:session]\n",
    policy_extra: str = "",
) -> None:
    """A predefined role under `platform_root/roles/`."""
    folder = platform_root / "roles" / name
    _write(folder, "role.md", f"---\nname: {name}\n---\n\nbody\n")
    extends_line = f"extends: {extends}\n" if extends else ""
    _write(
        folder,
        "manifest.md",
        f"---\nrole: {name}\n{extends_line}tools: []\nskills: []\ncontext: {{}}\n"
        f"{manifest_extra}---\n\nm\n",
    )
    _write(
        folder,
        "policy.md",
        f"---\nrole: {name}\nautonomy: supervised\nuntrusted_input: false\n"
        f"{policy_extra}---\n\np\n",
    )


def _write_deployment(
    deployments_root: pathlib.Path,
    client: str,
    role: str,
    *,
    tools: str = "[catalog_search]",
    manifest_extra: str = "permissions: inherit\n",
    policy_extra: str = "",
) -> None:
    folder = deployments_root / client / role
    _write(folder, "role.md", f'---\nname: {role}\nversion: "1.0"\n---\n\nclient.\n')
    _write(
        folder,
        "manifest.md",
        f'---\nrole: {role}\nversion: "1.0"\ndeployment: {client}\ntools: {tools}\n'
        f"skills: []\ncontext: {{}}\n{manifest_extra}---\n",
    )
    _write(
        folder,
        "policy.md",
        f'---\nrole: {role}\nversion: "1.0"\n{policy_extra}---\n',
    )


def _deployment_roots(tmp_path: pathlib.Path) -> RootConfig:
    return RootConfig(
        platform_root=ROOTS.platform_root, deployments_root=tmp_path / "deployments"
    )


# ---------------------------------------------------------------------------
# 1. replace() on a folder agent is rejected, never silently dropped (P16)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"untrusted_input": True}, id="untrusted_input"),
        pytest.param({"audit_policy": {"capture_tool_input": True}}, id="audit"),
        pytest.param({"permissions": ["exec:command"]}, id="permissions"),
        # A default-equal value: `replace()` cannot tell it from "not
        # passed", which is why folding changes into overrides would still
        # drop this one.
        pytest.param({"tools": ()}, id="default-equal-tools"),
        pytest.param({}, id="no-change"),
    ],
)
def test_replace_on_a_folder_agent_is_rejected(
    tmp_path: pathlib.Path, changes: dict[str, Any]
) -> None:
    """P16: `replace(Agent.from_folder(p), untrusted_input=True)` showed True
    on the object but resolved to False, keeping `exec:command`."""
    from agents_system.agent.spec import Agent

    agent = Agent.from_folder(
        _write_agent_folder(tmp_path, "p16", extends="operator-agent")
    )

    with pytest.raises(DefinitionError) as exc:
        dataclasses.replace(agent, **changes)

    _message(exc, "'p16'", "replace", "Agent.from_folder(path, **overrides)")


def test_the_same_change_as_a_from_folder_override_applies(
    tmp_path: pathlib.Path,
) -> None:
    """The supported way: the override reaches the loader, and the R4
    barrier refuses operator-agent's T3 permissions."""
    from agents_system.agent.spec import Agent
    from agents_system.permissions import UntrustedInputGrantError

    folder = _write_agent_folder(tmp_path, "p16", extends="operator-agent")

    with pytest.raises(UntrustedInputGrantError, match="exec:command"):
        resolve(
            Agent.from_folder(folder, untrusted_input=True)._to_locator(), roots=ROOTS
        )


def test_replace_on_an_inline_agent_still_works() -> None:
    from agents_system.agent.spec import Agent

    agent = dataclasses.replace(
        Agent(name="bot", extends="agent"), untrusted_input=True
    )

    assert resolve(agent._to_locator(), roots=ROOTS).untrusted_input is True


# ---------------------------------------------------------------------------
# 2. An unrecognised directive is rejected on every path (P6b)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "directive",
    [
        pytest.param({"remove": ["a"]}, id="remove-without-inherit"),
        pytest.param({"inherit": False, "remove": ["a"]}, id="inherit-false"),
        pytest.param({"inherit": "true", "remove": ["a"]}, id="inherit-string"),
        pytest.param({"inherit": True, "remvoe": ["a"]}, id="typo"),
        pytest.param({"inherit": True, "override": ["a"]}, id="mixed"),
        pytest.param({"foo": 1}, id="unknown"),
        pytest.param(42, id="not-a-list-or-mapping"),
    ],
)
def test_an_unrecognised_list_directive_raises(directive: Any) -> None:
    """Each of these used to resolve to the parent's full set."""
    with pytest.raises(DefinitionError) as exc:
        _resolve_list_directive(
            ["a", "b"], directive, who="role 'bot'", field="permissions"
        )

    _message(exc, "role 'bot'", "permissions", "not a recognised directive")


def test_a_bare_string_is_a_one_item_list() -> None:
    """As `_as_str_list` reads `tools:` and `Agent(permissions="x")` reads
    it. It used to resolve to the parent's full set."""
    assert _resolve_list_directive(["a", "b"], "read:x") == ["read:x"]


def test_a_folder_manifest_directive_without_inherit_raises(
    tmp_path: pathlib.Path,
) -> None:
    """P6b: the removal was dropped and `write:orders` kept."""
    from agents_system.agent.spec import Agent

    folder = _write_agent_folder(
        tmp_path,
        "p6",
        manifest_extra="permissions:\n  remove: [send:message, write:orders]\n",
    )

    with pytest.raises(DefinitionError) as exc:
        resolve(Agent.from_folder(folder)._to_locator(), roots=ROOTS)

    _message(exc, "'p6'", "permissions", "not a recognised directive")


def test_a_predefined_role_directive_without_inherit_raises(
    tmp_path: pathlib.Path,
) -> None:
    _write_platform_role(
        tmp_path, "parent", manifest_extra="permissions: [read:a, read:b]\n"
    )
    _write_platform_role(
        tmp_path,
        "child",
        extends="parent",
        manifest_extra="permissions:\n  remove: [read:b]\n",
    )

    with pytest.raises(DefinitionError) as exc:
        resolve("child", roots=RootConfig(platform_root=tmp_path))

    _message(exc, "'child'", "permissions", "not a recognised directive")


def test_a_deployment_directive_without_inherit_raises(
    tmp_path: pathlib.Path,
) -> None:
    _write_deployment(
        tmp_path / "deployments",
        "client-x",
        "sales-agent",
        manifest_extra="permissions:\n  remove: [write:orders]\n",
    )

    with pytest.raises(DefinitionError) as exc:
        resolve("sales-agent", client="client-x", roots=_deployment_roots(tmp_path))

    _message(exc, "'client-x'", "permissions", "not a recognised directive")


def test_a_chain_root_resolves_its_own_directive(tmp_path: pathlib.Path) -> None:
    """A root has no fold, so its directive used to be dropped: `{override:
    [read:catalog]}` resolved to no permissions, and an unrecognised one
    passed unchecked."""
    from agents_system.agent.spec import Agent

    root = _write_agent_folder(
        tmp_path,
        "root-ok",
        extends=None,
        manifest_extra="permissions:\n  override: [read:catalog]\n",
    )
    bad = _write_agent_folder(
        tmp_path,
        "root-bad",
        extends=None,
        manifest_extra="permissions:\n  remove: [read:catalog]\n",
    )

    definition = resolve(Agent.from_folder(root)._to_locator(), roots=ROOTS)
    assert definition.permissions == ("read:catalog",)
    with pytest.raises(DefinitionError) as exc:
        resolve(Agent.from_folder(bad)._to_locator(), roots=ROOTS)
    _message(exc, "'root-bad'", "permissions", "not a recognised directive")


@pytest.mark.parametrize(
    ("field", "directive"),
    [
        pytest.param("escalation_rules", {"add": ["x"]}, id="add-without-inherit"),
        pytest.param("escalation_rules", {"inherit": "yes"}, id="inherit-string"),
        pytest.param("audit_policy", {"inherit": True, "remove": ["x"]}, id="remove"),
        pytest.param("delegation_policy", {"override": {}}, id="override"),
    ],
)
def test_an_unrecognised_mapping_directive_raises(
    field: str, directive: dict[str, Any]
) -> None:
    """`add`/`remove` only mean something under `inherit: true`, and only for
    `escalation_rules` (they edit `conditions`). Anything else used to be
    stored as literal keys or replace the whole mapping."""
    from agents_system.agent.spec import Agent

    with pytest.raises(DefinitionError) as exc:
        _resolve_mapping_directive({}, directive, who="role 'bot'", field=field)
    _message(exc, "role 'bot'", field, "not a recognised directive")

    with pytest.raises(DefinitionError) as exc:
        Agent(name="bot", extends="agent", **{field: directive})
    _message(exc, "'bot'", field, "not a recognised directive")


def test_a_deployment_mapping_directive_is_checked(tmp_path: pathlib.Path) -> None:
    _write_deployment(
        tmp_path / "deployments",
        "client-x",
        "sales-agent",
        policy_extra="delegation_policy:\n  inherit: 'yes'\n  allowed: true\n",
    )

    with pytest.raises(DefinitionError) as exc:
        resolve("sales-agent", client="client-x", roots=_deployment_roots(tmp_path))

    _message(exc, "'client-x'", "delegation_policy", "not a recognised directive")


def test_an_importer_inherit_directive_is_resolved_not_stored() -> None:
    """The fold used to merge `{inherit: true, add: [...]}` key by key, so
    the condition was never added and `inherit`/`add` became policy keys."""
    from agents_system.agent.spec import Agent

    definition = resolve(
        Agent(
            name="bot",
            extends="agent",
            escalation_rules={"inherit": True, "add": ["vip_complaint"]},
        )._to_locator(),
        roots=ROOTS,
    )

    assert definition.escalation_rules["conditions"] == [
        "required_tool_missing",
        "confidence_below_threshold",
        "explicit_user_request",
        "vip_complaint",
    ]
    assert "inherit" not in definition.escalation_rules
    assert "add" not in definition.escalation_rules


# ---------------------------------------------------------------------------
# 3. Deployments and predefined-role folds keep escalation and redaction (P17)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "escalation",
    [
        pytest.param("escalation_rules:\n  conditions: []\n", id="wiped"),
        pytest.param("escalation_rules:\n  escalate_to: human\n", id="replaced"),
        pytest.param(
            "escalation_rules:\n  inherit: true\n  remove: [customer_requests_human]\n",
            id="removed",
        ),
    ],
)
def test_a_deployment_cannot_remove_an_escalation_condition(
    tmp_path: pathlib.Path, escalation: str
) -> None:
    """P17: `conditions: []` resolved to no conditions at all."""
    _write_deployment(
        tmp_path / "deployments", "client-x", "sales-agent", policy_extra=escalation
    )

    with pytest.raises(DefinitionError) as exc:
        resolve("sales-agent", client="client-x", roots=_deployment_roots(tmp_path))

    _message(exc, "'client-x'", "escalation_rules", "customer_requests_human")


@pytest.mark.parametrize(
    "audit",
    [
        pytest.param("audit_policy:\n  capture_tool_input: true\n", id="replace"),
        pytest.param(
            "audit_policy:\n  inherit: true\n  capture_tool_input: 'no'\n",
            id="inherit-truthy-string",
        ),
    ],
)
def test_a_deployment_cannot_switch_off_redaction(
    tmp_path: pathlib.Path, audit: str
) -> None:
    """P17: `capture_tool_input: true` was accepted."""
    _write_deployment(
        tmp_path / "deployments", "client-x", "sales-agent", policy_extra=audit
    )

    with pytest.raises(DefinitionError) as exc:
        resolve("sales-agent", client="client-x", roots=_deployment_roots(tmp_path))

    _message(exc, "'client-x'", "audit_policy", "capture_tool_input")


def test_a_deployment_cannot_drop_a_redacted_key(tmp_path: pathlib.Path) -> None:
    """A plain mapping replaces the role's whole `audit_policy`, so leaving
    `redact_keys` out of it drops them."""
    _write_platform_role(
        tmp_path,
        "keeper",
        policy_extra="audit_policy:\n  log_tool_calls: true\n  redact_keys: [iban]\n",
    )
    _write_deployment(
        tmp_path / "deployments",
        "client-x",
        "keeper",
        tools="[]",
        policy_extra="audit_policy:\n  log_tool_calls: true\n",
    )
    roots = RootConfig(
        platform_root=tmp_path, deployments_root=tmp_path / "deployments"
    )

    with pytest.raises(DefinitionError) as exc:
        resolve("keeper", client="client-x", roots=roots)

    _message(exc, "'client-x'", "audit_policy", "iban")


def test_a_deployment_that_says_nothing_keeps_the_roles_policy(
    tmp_path: pathlib.Path,
) -> None:
    """A policy field the deployment leaves out used to resolve to `{}`:
    escalation, audit logging and delegation rules all silently gone."""
    _write_deployment(tmp_path / "deployments", "client-x", "sales-agent")

    generic = resolve("sales-agent", roots=ROOTS)
    deployed = resolve(
        "sales-agent", client="client-x", roots=_deployment_roots(tmp_path)
    )

    assert deployed.escalation_rules == generic.escalation_rules
    assert deployed.audit_policy == generic.audit_policy
    assert deployed.delegation_policy == generic.delegation_policy
    assert deployed.memory_policy == generic.memory_policy


def test_a_deployment_can_still_add_conditions_and_tighten(
    tmp_path: pathlib.Path,
) -> None:
    _write_deployment(
        tmp_path / "deployments",
        "client-x",
        "sales-agent",
        policy_extra=(
            "escalation_rules:\n  inherit: true\n  add: [vip_complaint]\n"
            "  descriptions:\n    vip_complaint: a VIP customer complains.\n"
            "audit_policy:\n  inherit: true\n  redact_keys: [iban]\n"
        ),
    )

    deployed = resolve(
        "sales-agent", client="client-x", roots=_deployment_roots(tmp_path)
    )

    assert deployed.escalation_rules["conditions"][-1] == "vip_complaint"
    assert "customer_requests_human" in deployed.escalation_rules["conditions"]
    assert (
        deployed.escalation_rules["descriptions"]["vip_complaint"]
        == "a VIP customer complains."
    )
    assert deployed.audit_policy["redact_keys"] == ["iban"]


def test_a_predefined_role_cannot_drop_an_inherited_condition(
    tmp_path: pathlib.Path,
) -> None:
    _write_platform_role(
        tmp_path,
        "parent",
        policy_extra="escalation_rules:\n  conditions: [a, b]\n",
    )
    _write_platform_role(
        tmp_path,
        "child",
        extends="parent",
        policy_extra="escalation_rules:\n  conditions: [a, c]\n",
    )

    with pytest.raises(DefinitionError) as exc:
        resolve("child", roots=RootConfig(platform_root=tmp_path))

    _message(exc, "'child'", "escalation_rules", "'b'")


def test_an_importer_remove_directive_cannot_drop_an_inherited_condition() -> None:
    """`conditions` are added to the parent's, so only a `remove:` directive
    could still drop one; the fold used to store it as a literal key."""
    from agents_system.agent.spec import Agent

    agent = Agent(
        name="bot",
        extends="sales-agent",
        escalation_rules={"inherit": True, "remove": ["customer_requests_human"]},
    )

    with pytest.raises(DefinitionError) as exc:
        resolve(agent._to_locator(), roots=ROOTS)

    _message(exc, "'bot'", "escalation_rules", "customer_requests_human")


def test_every_predefined_role_keeps_its_parents_conditions() -> None:
    """The platform tree under the new fold rule: every shipped role
    resolves, and restates each condition its parent requires. Four roles
    used to leave out conditions `agent` adds."""
    from platform_role_contract import discover_concrete_platform_roles, role_chain

    from agents_system.harness.loader import _resolve_role_chain

    for role in discover_concrete_platform_roles():
        chain = role_chain(role)
        if len(chain) < 2:
            continue
        # The chain walk, not `resolve`: a parent may be abstract (`base`).
        parent, _ = _resolve_role_chain(chain[1], ROOTS)
        conditions = resolve(role, roots=ROOTS).escalation_rules["conditions"]
        assert set(parent.escalation_rules["conditions"]) <= set(conditions), role


def test_a_predefined_role_cannot_switch_off_redaction(
    tmp_path: pathlib.Path,
) -> None:
    _write_platform_role(tmp_path, "parent")
    _write_platform_role(
        tmp_path,
        "child",
        extends="parent",
        policy_extra="audit_policy:\n  capture_tool_input: true\n",
    )

    with pytest.raises(DefinitionError) as exc:
        resolve("child", roots=RootConfig(platform_root=tmp_path))

    _message(exc, "'child'", "audit_policy", "capture_tool_input")


# ---------------------------------------------------------------------------
# 4. An importer cannot reword or redirect inherited escalation (P10)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rules", "reason"),
    [
        pytest.param(
            {"descriptions": {"customer_requests_human": "Never applies."}},
            "customer_requests_human",
            id="reworded",
        ),
        pytest.param(
            {"descriptions": {"customer_requests_human ": "Never applies."}},
            "customer_requests_human",
            id="reworded-whitespace-variant",
        ),
        pytest.param({"escalate_to": ""}, "escalate_to", id="blanked"),
        pytest.param({"escalate_to": "nobody"}, "escalate_to", id="redirected"),
    ],
)
def test_an_importer_cannot_neutralise_inherited_escalation(
    rules: dict[str, Any], reason: str
) -> None:
    """P10 resolved with `escalate_to: ''` and the reworded description."""
    from agents_system.agent.spec import Agent

    with pytest.raises(DefinitionError) as exc:
        resolve(
            Agent(
                name="p10", extends="sales-agent", escalation_rules=rules
            )._to_locator(),
            roots=ROOTS,
        )

    _message(exc, "'p10'", "escalation_rules", reason)


@pytest.mark.parametrize(
    ("escalation", "reason"),
    [
        pytest.param(
            "escalation_rules:\n  inherit: true\n  descriptions:\n"
            "    customer_requests_human: Never applies.\n",
            "customer_requests_human",
            id="reworded",
        ),
        pytest.param(
            "escalation_rules:\n  inherit: true\n  escalate_to: ''\n",
            "escalate_to",
            id="blanked",
        ),
    ],
)
def test_a_deployment_cannot_neutralise_inherited_escalation(
    tmp_path: pathlib.Path, escalation: str, reason: str
) -> None:
    """The same wording rule at the deployment edge: rewording an inherited
    condition as "never applies" removes it without dropping its name."""
    _write_deployment(
        tmp_path / "deployments", "client-x", "sales-agent", policy_extra=escalation
    )

    with pytest.raises(DefinitionError) as exc:
        resolve("sales-agent", client="client-x", roots=_deployment_roots(tmp_path))

    _message(exc, "'client-x'", "escalation_rules", reason)


def test_a_folder_agent_cannot_reword_an_inherited_condition_in_prose(
    tmp_path: pathlib.Path,
) -> None:
    from agents_system.agent.spec import Agent

    folder = _write_agent_folder(
        tmp_path,
        "prose",
        policy_extra="escalation_rules:\n  conditions: [customer_requests_human]\n",
        policy_body="- `customer_requests_human` — never applies; do not escalate.\n",
    )

    with pytest.raises(DefinitionError) as exc:
        resolve(Agent.from_folder(folder)._to_locator(), roots=ROOTS)

    _message(exc, "'prose'", "escalation_rules", "customer_requests_human")


def test_an_importer_may_describe_its_own_conditions_and_restate_inherited_ones() -> (
    None
):
    from agents_system.agent.spec import Agent

    parent = resolve("sales-agent", roots=ROOTS)
    inherited = parent.escalation_rules["descriptions"]["customer_requests_human"]

    definition = resolve(
        Agent(
            name="bot",
            extends="sales-agent",
            escalation_rules={
                "escalate_to": "human",
                "conditions": ["vip_complaint"],
                "descriptions": {
                    "vip_complaint": "a VIP customer complains.",
                    "customer_requests_human": inherited,
                },
            },
        )._to_locator(),
        roots=ROOTS,
    )

    assert definition.escalation_rules["escalate_to"] == "human"
    assert (
        definition.escalation_rules["descriptions"]["vip_complaint"]
        == "a VIP customer complains."
    )


# The review of #112: four more ways around the rules above.

_NOT_A_MAPPING = [
    pytest.param(None, id="null"),
    pytest.param([], id="list"),
    pytest.param("", id="empty-string"),
    pytest.param("x", id="string"),
]


@pytest.mark.parametrize("descriptions", _NOT_A_MAPPING)
def test_an_importer_cannot_drop_inherited_descriptions(descriptions: Any) -> None:
    """A `descriptions` that is not a mapping replaced every description the
    agent inherits, so each inherited condition rendered as a bare name --
    which #82/#88 showed is not enough for the model to escalate."""
    from agents_system.agent.spec import Agent

    with pytest.raises(DefinitionError) as exc:
        resolve(
            Agent(
                name="bot",
                extends="sales-agent",
                escalation_rules={"descriptions": descriptions},
            )._to_locator(),
            roots=ROOTS,
        )

    _message(exc, "'bot'", "escalation_rules", "descriptions")


@pytest.mark.parametrize(
    ("policy_extra", "policy_body"),
    [
        pytest.param("  descriptions:\n", "Body.\n", id="null"),
        pytest.param("  descriptions: []\n", "Body.\n", id="list"),
        pytest.param("  descriptions: none\n", "Body.\n", id="string"),
        pytest.param(
            "  conditions: [vip_complaint]\n  descriptions: []\n",
            "- `vip_complaint` — a VIP customer complains.\n",
            id="list-beside-prose",
        ),
    ],
)
def test_a_folder_agent_cannot_drop_inherited_descriptions(
    tmp_path: pathlib.Path, policy_extra: str, policy_body: str
) -> None:
    """The same in `policy.md`, where a bare `descriptions:` key is a YAML
    null. Descriptions parsed from the file's prose used to hide the
    malformed key rather than reject it."""
    from agents_system.agent.spec import Agent

    folder = _write_agent_folder(
        tmp_path,
        "vip",
        policy_extra=f"escalation_rules:\n{policy_extra}",
        policy_body=policy_body,
    )

    with pytest.raises(DefinitionError) as exc:
        resolve(Agent.from_folder(folder)._to_locator(), roots=ROOTS)

    _message(exc, "vip", "escalation_rules", "descriptions")


@pytest.mark.parametrize(
    "descriptions",
    [
        pytest.param("\n", id="null"),
        pytest.param(" []\n", id="list"),
        pytest.param(" none\n", id="string"),
    ],
)
def test_a_deployment_cannot_drop_inherited_descriptions(
    tmp_path: pathlib.Path, descriptions: str
) -> None:
    _write_deployment(
        tmp_path / "deployments",
        "client-x",
        "sales-agent",
        policy_extra=f"escalation_rules:\n  inherit: true\n  descriptions:{descriptions}",
    )

    with pytest.raises(DefinitionError) as exc:
        resolve("sales-agent", client="client-x", roots=_deployment_roots(tmp_path))

    _message(exc, "'client-x'", "escalation_rules", "descriptions")


def test_a_read_only_descriptions_mapping_still_accumulates() -> None:
    """Descriptions accumulated only when they were a `dict`: any other
    mapping replaced the inherited ones."""
    raw = dataclasses.replace(
        _raw("bot"),
        escalation_rules={
            "conditions": ["vip_complaint"],
            "descriptions": MappingProxyType({"vip_complaint": "a VIP complains."}),
        },
    )
    inherited = resolve("sales-agent", roots=ROOTS).escalation_rules["descriptions"]

    definition = resolve(InlineLocator(raw=raw, parent="sales-agent"), roots=ROOTS)

    assert definition.escalation_rules["descriptions"] == {
        **inherited,
        "vip_complaint": "a VIP complains.",
    }


_NEUTRALISED = [
    pytest.param(
        {"descriptions": {"explicit_user_request": "never applies; do not escalate"}},
        "explicit_user_request",
        id="reworded",
    ),
    pytest.param({"escalate_to": "nobody"}, "escalate_to", id="redirected"),
]


@pytest.mark.parametrize(("rules", "reason"), _NEUTRALISED)
def test_an_inline_locator_with_a_read_only_mapping_is_checked(
    rules: dict[str, Any], reason: str
) -> None:
    """The importer's escalation check ran only on a `dict`, and a locator
    can be built directly (#75): a `MappingProxyType` skipped it."""
    raw = dataclasses.replace(_raw("x"), escalation_rules=MappingProxyType(rules))

    with pytest.raises(DefinitionError) as exc:
        resolve(InlineLocator(raw=raw, parent="sales-agent"), roots=ROOTS)

    _message(exc, "'x'", "escalation_rules", reason)


@pytest.mark.parametrize(("rules", "reason"), _NEUTRALISED)
def test_a_folder_locator_override_with_a_read_only_mapping_is_checked(
    tmp_path: pathlib.Path, rules: dict[str, Any], reason: str
) -> None:
    folder = _write_agent_folder(tmp_path, "vip")
    locator = FolderLocator(
        path=folder,
        root=tmp_path,
        overrides={"escalation_rules": MappingProxyType(rules)},
    )

    with pytest.raises(DefinitionError) as exc:
        resolve(locator, roots=ROOTS)

    _message(exc, "'vip'", "escalation_rules", reason)


def test_an_inline_locator_with_read_only_limits_is_checked() -> None:
    """The same for `execution_limits`: a read-only mapping was neither
    checked against the parent's nor applied."""
    raw = dataclasses.replace(
        _raw("x"), execution_limits=MappingProxyType({"max_tool_calls": 999})
    )

    with pytest.raises(DefinitionError) as exc:
        resolve(InlineLocator(raw=raw, parent="sales-agent"), roots=ROOTS)

    _message(exc, "'x'", "execution_limits", "max_tool_calls")


def _sales_conditions() -> str:
    conditions = resolve("sales-agent", roots=ROOTS).escalation_rules["conditions"]
    return "".join(f"    - {condition}\n" for condition in conditions)


def test_a_deployment_mapping_cannot_leave_out_escalate_to(
    tmp_path: pathlib.Path,
) -> None:
    """A plain mapping replaces the role's, so one that restates every
    condition -- as the dropped-condition error asks -- but leaves out
    `escalate_to` resolved with nobody to escalate to."""
    _write_deployment(
        tmp_path / "deployments",
        "client-x",
        "sales-agent",
        policy_extra=f"escalation_rules:\n  conditions:\n{_sales_conditions()}",
    )

    with pytest.raises(DefinitionError) as exc:
        resolve("sales-agent", client="client-x", roots=_deployment_roots(tmp_path))

    _message(exc, "'client-x'", "escalation_rules", "escalate_to", "'human'")


def test_a_deployment_mapping_keeps_the_inherited_descriptions(
    tmp_path: pathlib.Path,
) -> None:
    """The same plain mapping, now complete, describes only the condition
    it adds -- all it may describe. The role's descriptions of the others
    used to be replaced with it."""
    _write_deployment(
        tmp_path / "deployments",
        "client-x",
        "sales-agent",
        policy_extra=(
            "escalation_rules:\n  escalate_to: human\n  conditions:\n"
            f"{_sales_conditions()}    - vip_complaint\n"
            "  descriptions:\n    vip_complaint: a VIP complains.\n"
        ),
    )
    generic = resolve("sales-agent", roots=ROOTS)

    deployed = resolve(
        "sales-agent", client="client-x", roots=_deployment_roots(tmp_path)
    )

    assert deployed.escalation_rules["escalate_to"] == "human"
    assert deployed.escalation_rules["descriptions"] == {
        **generic.escalation_rules["descriptions"],
        "vip_complaint": "a VIP complains.",
    }


# ---------------------------------------------------------------------------
# 5. A limit too large for a float is a DefinitionError (P1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(10**400, id="10**400"),
        pytest.param(-(10**400), id="-10**400"),
        # Too long even for `str()`: pytest needs an explicit id for it too.
        pytest.param(10**5000, id="10**5000"),
    ],
)
def test_a_huge_int_limit_is_a_definition_error(value: int) -> None:
    """`math.isfinite(10**400)` raised `OverflowError`; printing a
    5000-digit int raises `ValueError`. Neither may escape."""
    with pytest.raises(DefinitionError) as exc:
        _validate_execution_limits({}, {"max_tool_calls": value})

    _message(exc, "execution_limits", "max_tool_calls")


def test_a_huge_int_limit_on_an_agent_names_the_agent() -> None:
    from agents_system.agent.spec import Agent

    with pytest.raises(DefinitionError) as exc:
        resolve(
            Agent(
                name="p1",
                extends="sales-agent",
                execution_limits={"max_tool_calls": 10**400},
            )._to_locator(),
            roots=ROOTS,
        )

    _message(exc, "'p1'", "execution_limits", "max_tool_calls")


# ---------------------------------------------------------------------------
# 6. The base dict methods cannot mutate an Agent's mappings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda m: dict.__setitem__(m, "k", 1), id="setitem"),
        pytest.param(lambda m: dict.__delitem__(m, "extra"), id="delitem"),
        pytest.param(lambda m: dict.update(m, k=1), id="update"),
        pytest.param(lambda m: dict.__init__(m, {"k": 1}), id="init"),
        pytest.param(lambda m: dict.clear(m), id="clear"),
        pytest.param(lambda m: dict.pop(m, "extra"), id="pop"),
        pytest.param(lambda m: dict.setdefault(m, "k", 1), id="setdefault"),
    ],
)
def test_the_base_dict_methods_cannot_mutate_an_agent(mutate: Any) -> None:
    """`_FrozenDict` was a `dict` subclass, so `dict.__setitem__(m, ...)`
    skipped its blocked mutators."""
    from agents_system.agent.spec import Agent

    agent = Agent(
        name="bot",
        skills=("s",),
        skill_contents={"s": "x"},
        context={"extra": {"list": [1, 2]}},
        audit_policy={"log_tool_calls": True},
    )

    for mapping in (
        agent.context,
        agent.context["extra"],
        agent.skill_contents,
        agent.audit_policy,
    ):
        with pytest.raises(TypeError):
            mutate(mapping)

    assert dict(agent.context) == {"extra": {"list": (1, 2)}}
    assert dict(agent.skill_contents) == {"s": "x"}
    assert dict(agent.audit_policy) == {"log_tool_calls": True}
