"""Tests for the agent factory — the assembler (D-004).

The factory is the glue layer: it takes a resolved ``AgentDefinition`` (loader),
resolves the granted tool surface (injector), loads the deployment's skill files
from disk, composes the final system prompt, and returns a frozen
``EquippedRuntime``.

It is the keystone that makes everything below it usable by the Agent Runtime
(LangGraph / bind_tools), which is a LATER slice — the factory does NOT call an
LLM or bind tools to a model.

Strict TDD: these tests are written before factory.py exists.
"""

from __future__ import annotations

import pathlib
from typing import Any

import pytest
import structlog

REPO_ROOT = pathlib.Path(__file__).parent.parent
FIXTURE_BASE = REPO_ROOT / "tests" / "fixtures" / "agents"
GENERIC_ROOTS_DIR = FIXTURE_BASE / "generic-role"
OVERRIDE_ROOTS_DIR = FIXTURE_BASE / "overrides"

# The six permissions the platform sales-agent declares (see
# platform/roles/sales-agent/manifest.md). The generic deployment inherits them.
SALES_PERMISSIONS = [
    "read:catalog",
    "read:client_registry",
    "write:orders",
    "write:order_items",
    "read:price_lists",
    "send:message",
]


def _fixture_roots() -> Any:
    """RootConfig pointing at the simple-role fixtures."""
    from agents_system.harness.loader import RootConfig

    return RootConfig(
        platform_root=GENERIC_ROOTS_DIR,
        deployments_root=OVERRIDE_ROOTS_DIR / "deployments",
    )


def _client_a_roots() -> Any:
    """RootConfig for platform roles and generic client deployment fixtures."""
    from agents_system.harness.loader import RootConfig

    return RootConfig(
        platform_root=REPO_ROOT / "platform",
        deployments_root=OVERRIDE_ROOTS_DIR / "deployments",
    )


def _spec(name: str, perms: list[str]) -> Any:
    from agents_system.harness.registry import Tier, ToolSpec

    # Mirror the pre-tier write:/send: heuristic so fixtures keep their
    # original sensitivity classification (ADR-002 C.10).
    tier = Tier.T2 if any(p.startswith(("write:", "send:")) for p in perms) else Tier.T1
    return ToolSpec(
        name=name,
        required_permissions=tuple(perms),
        connector=lambda: None,
        tier=tier,
    )


def _sales_registry() -> Any:
    """Registry holding the RESOLVED sales-agent surface.

    Six tools, not five: `escalation_notifier` is inherited from
    `platform/roles/agent`. The injector raises `InjectionError: Unknown tool`
    for any declared tool the registry lacks, and it grades against the
    resolved chain, so a registry built from the leaf manifest alone no longer
    boots the role.
    """
    from agents_system.harness.registry import ToolRegistry

    reg = ToolRegistry()
    reg.register(_spec("message_sender", ["send:message"]))
    reg.register(_spec("catalog_search", ["read:catalog"]))
    reg.register(_spec("order_writer", ["write:orders", "write:order_items"]))
    reg.register(_spec("session_state", []))
    reg.register(_spec("client_lookup", ["read:client_registry"]))
    reg.register(_spec("escalation_notifier", ["send:escalation"]))
    return reg


# ---------------------------------------------------------------------------
# Happy path — generic client deployment
# ---------------------------------------------------------------------------
def test_build_runtime_attaches_resolved_definition() -> None:
    from agents_system.harness.factory import build_runtime

    runtime = build_runtime(
        "sales-agent",
        _sales_registry(),
        SALES_PERMISSIONS,
        client="client-a",
        roots=_client_a_roots(),
    )

    assert runtime.definition.role_name == "sales-agent"
    assert runtime.definition.deployment == "client-a"


def test_build_runtime_grants_all_tools_when_permitted() -> None:
    from agents_system.harness.factory import build_runtime

    runtime = build_runtime(
        "sales-agent",
        _sales_registry(),
        SALES_PERMISSIONS,
        client="client-a",
        roots=_client_a_roots(),
    )

    granted_names = {t.name for t in runtime.tools}
    assert granted_names == {
        "message_sender",
        "catalog_search",
        "order_writer",
        "session_state",
        "client_lookup",
    }
    assert runtime.denied_tools == ()


def test_build_runtime_denies_tools_missing_permissions() -> None:
    from agents_system.harness.factory import build_runtime

    # Only read:catalog granted → catalog_search (needs read:catalog) and
    # session_state (needs nothing) pass; the rest are denied.
    runtime = build_runtime(
        "sales-agent",
        _sales_registry(),
        ["read:catalog"],
        client="client-a",
        roots=_client_a_roots(),
    )

    granted_names = {t.name for t in runtime.tools}
    assert granted_names == {"catalog_search", "session_state"}

    denied_names = {name for name, _reason in runtime.denied_tools}
    assert denied_names == {"message_sender", "order_writer", "client_lookup"}


# ---------------------------------------------------------------------------
# permission-model PR3 (issue #38) — EquippedRuntime.deploy_grant_ceiling
# ---------------------------------------------------------------------------


def test_build_runtime_deploy_grant_ceiling_accepts_mixed_class_and_name_grant() -> (
    None
):
    """Accepted grant forms (spec): `granted_permissions` may mix a
    `Permission` subclass and a registered wire-name string; every entry
    normalizes through the registry into the stored `deploy_grant_ceiling`
    frozenset.

    PR #55 security review (MEDIUM): also proves the class-form grant
    (`Write`) equips Layer-1 tools identically to how a string-form grant
    would — `Write` covers sales-agent's own `write:orders`/`write:order_items`
    (R3), so `order_writer` must be GRANTED, not silently dropped by a
    string-only tool-surface intersection.
    """
    from agents_system.harness.factory import build_runtime
    from agents_system.permissions import Write, permission_registry

    send_message_cls = permission_registry.resolve("send:message")

    runtime = build_runtime(
        "sales-agent",
        _sales_registry(),
        [Write, "send:message"],
        client="client-a",
        roots=_client_a_roots(),
    )

    assert runtime.deploy_grant_ceiling == frozenset({Write, send_message_cls})

    granted_names = {t.name for t in runtime.tools}
    denied_names = {name for name, _reason in runtime.denied_tools}
    # order_writer requires write:orders + write:order_items, both covered
    # by the class-form Write grant (R3); message_sender requires
    # send:message (granted by name); session_state requires nothing.
    assert granted_names == {"order_writer", "message_sender", "session_state"}
    # catalog_search (read:catalog) and client_lookup (read:client_registry)
    # are covered by neither granted entry. escalation_notifier is not part
    # of this role+client+roots combo's definition.tools at all (see
    # test_build_runtime_grants_all_tools_when_permitted, which evaluates
    # only these same 5 tools for sales-agent/client-a).
    assert denied_names == {"catalog_search", "client_lookup"}


def test_build_runtime_deploy_grant_ceiling_by_class_and_by_name_are_identical() -> (
    None
):
    """Scenario: Grant by class / Grant by name — granting the SAME
    permission as a class object vs. as its registered wire-name string
    must normalize to an identical ceiling."""
    from agents_system.harness.factory import build_runtime
    from agents_system.permissions import permission_registry

    write_orders_cls = permission_registry.resolve("write:orders")
    send_message_cls = permission_registry.resolve("send:message")

    by_class = build_runtime(
        "sales-agent",
        _sales_registry(),
        [write_orders_cls, send_message_cls],
        client="client-a",
        roots=_client_a_roots(),
    )
    by_name = build_runtime(
        "sales-agent",
        _sales_registry(),
        ["write:orders", "send:message"],
        client="client-a",
        roots=_client_a_roots(),
    )

    expected = frozenset({write_orders_cls, send_message_cls})
    assert by_class.deploy_grant_ceiling == expected
    assert by_name.deploy_grant_ceiling == expected


def test_build_runtime_deploy_grant_ceiling_default_empty() -> None:
    """No `deploy_grant_ceiling` bleeds through when granted_permissions is
    empty — the dataclass default, not an accident of the resolution loop."""
    from agents_system.harness.factory import build_runtime

    runtime = build_runtime(
        "sales-agent",
        _sales_registry(),
        [],
        client="client-a",
        roots=_client_a_roots(),
    )

    assert runtime.deploy_grant_ceiling == frozenset()


async def test_build_runtime_deploy_grant_ceiling_excludes_permission_role_does_not_declare() -> (
    None
):
    """PR #55 security review (MEDIUM-HIGH): the ceiling is
    `definition.permissions ∩ grant` under R3 (a granted class must cover a
    DECLARED permission), never a raw union of the grant. A grant naming a
    permission sales-agent never declares (`read:reports`) must not appear
    in `deploy_grant_ceiling`, and a tool requiring it must not pass
    Layer-2 either — even if the caller's `current_permissions` claims it."""
    import dataclasses as dc

    from agents_system.harness.factory import build_runtime
    from agents_system.harness.interceptor import PolicyViolation, intercept
    from agents_system.harness.registry import Tier, ToolSpec
    from agents_system.permissions import permission_registry

    reports_cls = permission_registry.resolve(
        "read:reports"
    )  # NOT in SALES_PERMISSIONS

    runtime = build_runtime(
        "sales-agent",
        _sales_registry(),
        [*SALES_PERMISSIONS, "read:reports"],
        client="client-a",
        roots=_client_a_roots(),
    )

    assert reports_cls not in runtime.deploy_grant_ceiling

    # ...nor does it pass Layer-2, even when a caller's current_permissions
    # explicitly claims it: the ceiling is the hard bound.
    report_reader_spec = ToolSpec(
        name="report_reader",
        required_permissions=("read:reports",),
        connector=lambda inputs: {"ok": True},
        tier=Tier.T1,
        always_revalidate=True,
    )
    probe_runtime = dc.replace(runtime, tools=(report_reader_spec,))

    with pytest.raises(PolicyViolation) as exc_info:
        await intercept(
            "report_reader",
            {},
            probe_runtime,
            current_permissions=["read:reports"],
        )
    assert exc_info.value.reason == "permission_revoked"


def test_build_runtime_rejects_t3_grant_for_untrusted_input_role() -> None:
    """PR #55 security review (HIGH) — spec.md R4 scenario 'Untrusted role
    cannot be equipped with a T3 grant at deploy time': sales-agent
    declares `untrusted_input: true`; granting it a T3-tier permission
    (`exec:command`, which it does not even declare) must raise
    `UntrustedInputGrantError` at build_runtime, not equip silently."""
    from agents_system.harness.factory import build_runtime
    from agents_system.permissions import UntrustedInputGrantError

    with pytest.raises(UntrustedInputGrantError):
        build_runtime(
            "sales-agent",
            _sales_registry(),
            ["exec:command"],
            client="client-a",
            roots=_client_a_roots(),
        )


def test_build_runtime_deploy_grant_ceiling_does_not_affect_granted_tools() -> None:
    """Existing-behavior regression guard: for a pure string-form grant
    that exactly matches the role's own declared permission names, the
    granted tool surface is unaffected by the R3-bounded ceiling
    computation added alongside it (PR #55 security review)."""
    from agents_system.harness.factory import build_runtime

    runtime = build_runtime(
        "sales-agent",
        _sales_registry(),
        SALES_PERMISSIONS,
        client="client-a",
        roots=_client_a_roots(),
    )

    granted_names = {t.name for t in runtime.tools}
    assert granted_names == {
        "message_sender",
        "catalog_search",
        "order_writer",
        "session_state",
        "client_lookup",
    }
    assert runtime.denied_tools == ()


def test_build_runtime_loads_declared_skills_in_order() -> None:
    from agents_system.harness.factory import build_runtime

    runtime = build_runtime(
        "sales-agent",
        _sales_registry(),
        SALES_PERMISSIONS,
        client="client-a",
        roots=_client_a_roots(),
    )

    skill_names = [s.name for s in runtime.skills]
    assert skill_names == [
        "request_structuring",
        "query_normalization",
        "confirmation_workflow",
    ]
    request_skill = next(s for s in runtime.skills if s.name == "request_structuring")
    assert "Structured request fields" in request_skill.content


def test_build_runtime_composes_prompt_from_role_body_and_skills() -> None:
    from agents_system.harness.factory import build_runtime

    runtime = build_runtime(
        "sales-agent",
        _sales_registry(),
        SALES_PERMISSIONS,
        client="client-a",
        roots=_client_a_roots(),
    )

    prompt = runtime.system_prompt
    assert "Generic Client A request assistant" in prompt
    # Each skill body is concatenated into the composed prompt.
    assert "Structured request fields" in prompt
    # Skills appear AFTER the role body.
    assert prompt.index("Generic Client A request assistant") < prompt.index(
        "Structured request fields"
    )


# ---------------------------------------------------------------------------
# Generic role (no client) — no skills, prompt is the role body + escalation
# ---------------------------------------------------------------------------
def test_build_runtime_generic_role_has_no_skills() -> None:
    from agents_system.harness.factory import build_runtime

    runtime = build_runtime("sales-agent", _sales_registry(), SALES_PERMISSIONS)

    assert runtime.skills == ()
    assert runtime.definition.deployment is None
    # With no skills, the composed prompt is the role body plus the rendered
    # escalation_rules block (issue #36) -- there is no skill content to
    # insert, but `sales-agent`'s policy.md declares real conditions, so the
    # composed prompt is no longer byte-identical to the bare role body.
    assert "customer_not_registered" in runtime.system_prompt
    assert runtime.system_prompt.rstrip().endswith("Answer in the user's language.")


# ---------------------------------------------------------------------------
# Failure mode — a declared skill with no file on disk fails loud
# ---------------------------------------------------------------------------
def test_build_runtime_missing_skill_file_raises() -> None:
    from agents_system.harness.factory import FactoryError, build_runtime
    from agents_system.harness.registry import ToolRegistry

    # client-a/simple-role declares skills [skill_one, skill_two] but the
    # fixture has no skills/ directory → the factory must fail loud.
    reg = ToolRegistry()
    reg.register(_spec("tool_alpha", ["read:catalog"]))
    reg.register(_spec("tool_beta", ["read:client_registry"]))

    with pytest.raises(FactoryError):
        build_runtime(
            "simple-role",
            reg,
            ["read:catalog", "read:client_registry"],
            client="client-a",
            roots=_fixture_roots(),
        )


# ---------------------------------------------------------------------------
# Defensive guard — skills declared but no client to load them from
# ---------------------------------------------------------------------------
def test_load_skills_without_client_raises() -> None:
    from agents_system.harness.factory import FactoryError, _load_skills
    from agents_system.harness.loader import AgentDefinition

    definition = AgentDefinition(
        role_name="sales-agent",
        version="1.0",
        deployment=None,
        system_prompt="",
        tools=(),
        skills=("ghost_skill",),  # declared, but client is None below
        context={},
        permissions=(),
        autonomy="supervised",
        escalation_rules={},
        delegation_policy={},
        memory_policy={},
        audit_policy={},
        execution_limits=None,
    )

    with pytest.raises(FactoryError):
        _load_skills(definition, None, _fixture_roots())


# ---------------------------------------------------------------------------
# Auditability — building a runtime emits a structured event
# ---------------------------------------------------------------------------
def test_build_runtime_logs_built_event() -> None:
    from agents_system.harness.factory import build_runtime

    with structlog.testing.capture_logs() as logs:
        build_runtime(
            "sales-agent",
            _sales_registry(),
            SALES_PERMISSIONS,
            client="client-a",
            roots=_client_a_roots(),
        )

    events = [e["event"] for e in logs]
    assert "factory.runtime_built" in events


def test_build_runtime_logs_each_skill_loaded() -> None:
    from agents_system.harness.factory import build_runtime

    with structlog.testing.capture_logs() as logs:
        build_runtime(
            "sales-agent",
            _sales_registry(),
            SALES_PERMISSIONS,
            client="client-a",
            roots=_client_a_roots(),
        )

    loaded = [e for e in logs if e["event"] == "factory.skill_loaded"]
    assert {e["skill"] for e in loaded} == {
        "request_structuring",
        "query_normalization",
        "confirmation_workflow",
    }


def test_build_runtime_logs_skill_missing_before_raising() -> None:
    from agents_system.harness.factory import FactoryError, build_runtime
    from agents_system.harness.registry import ToolRegistry

    reg = ToolRegistry()
    reg.register(_spec("tool_alpha", ["read:catalog"]))
    reg.register(_spec("tool_beta", ["read:client_registry"]))

    with structlog.testing.capture_logs() as logs, pytest.raises(FactoryError):
        build_runtime(
            "simple-role",
            reg,
            ["read:catalog", "read:client_registry"],
            client="client-a",
            roots=_fixture_roots(),
        )

    events = [e["event"] for e in logs]
    assert "factory.skill_missing" in events


def test_loading_skills_with_an_absent_deployments_root_raises_clearly(
    tmp_path: pathlib.Path,
) -> None:
    """An absent root must not be reported as missing skill files.

    `_load_skills` joins `deployments_root` into a path and then checks each
    skill file. With the root absent, every skill "does not exist", so the
    failure surfaced as `FactoryError: skill 'x' has no file at ...` —
    reading as a deployment authoring mistake when the real cause is a
    consumer that never passed a root. The guard now fires first and names
    what is actually wrong.
    """
    import pytest

    from agents_system.harness.factory import _load_skills
    from agents_system.harness.loader import (
        AgentDefinition,
        DefinitionError,
        RootConfig,
    )

    definition = AgentDefinition(
        role_name="simple-role",
        version="1.0",
        deployment="client-a",
        system_prompt="body",
        tools=(),
        skills=("some_skill",),
        context={},
        permissions=(),
        autonomy="supervised",
        escalation_rules={},
        delegation_policy={},
        memory_policy={},
        audit_policy={},
        execution_limits=None,
    )

    roots = RootConfig(
        platform_root=tmp_path / "platform",
        deployments_root=tmp_path / "absent",
    )

    with pytest.raises(DefinitionError) as excinfo:
        _load_skills(definition, "client-a", roots)

    assert "absent" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Issue #36 — resolved escalation_rules must reach the composed prompt
# ---------------------------------------------------------------------------
#
# `policy.md`'s `escalation_rules.conditions` (`escalate_to` + `conditions`)
# is structured data, parsed by `harness/loader.py::resolve()` into
# `AgentDefinition.escalation_rules` -- but before this fix, `_compose_prompt`
# never read it. A role's own `role.md` prose was the only place an
# escalation condition ever reached the model, and only if someone
# remembered to restate it there (see `accountant-agent`, which never does).


def _fx_definition(escalation_rules: dict[str, Any]) -> Any:
    """A minimal `AgentDefinition` with a realistic, contract-appended
    `system_prompt`, so `_compose_prompt`'s strip/reappend logic exercises
    the exact same base-contract text `resolve()` would produce."""
    from agents_system.harness.loader import AgentDefinition, _append_base_contract

    return AgentDefinition(
        role_name="fx-role",
        version="1.0",
        deployment=None,
        system_prompt=_append_base_contract("You are a test role."),
        tools=(),
        skills=(),
        context={},
        permissions=(),
        autonomy="supervised",
        escalation_rules=escalation_rules,
        delegation_policy={},
        memory_policy={},
        audit_policy={},
        execution_limits=None,
    )


def test_compose_prompt_renders_escalate_to_and_conditions_with_no_skills() -> None:
    from agents_system.harness.factory import _compose_prompt

    definition = _fx_definition(
        {
            "escalate_to": "human",
            "conditions": [
                "required_tool_missing",
                "figure_requested_outside_report_catalog",
            ],
        }
    )

    prompt = _compose_prompt(definition, ())

    assert "human" in prompt
    assert "required_tool_missing" in prompt
    assert "figure_requested_outside_report_catalog" in prompt


def test_compose_prompt_escalation_block_precedes_base_contract_no_skills() -> None:
    from agents_system.harness.factory import _compose_prompt

    definition = _fx_definition(
        {"escalate_to": "human", "conditions": ["confidence_below_threshold"]}
    )

    prompt = _compose_prompt(definition, ())

    escalation_index = prompt.index("confidence_below_threshold")
    contract_index = prompt.index("## base contract")
    assert escalation_index < contract_index
    # The base contract is still the prompt's LAST block (ADR-002 B.9).
    assert prompt.rstrip().endswith("Answer in the user's language.")


def test_compose_prompt_escalation_block_precedes_base_contract_with_skills() -> None:
    """Skills insert content too (`_strip_base_contract` / re-append path) --
    the escalation block must still land before the contract, not after."""
    from agents_system.harness.factory import LoadedSkill, _compose_prompt

    definition = _fx_definition(
        {"escalate_to": "human", "conditions": ["no_knowledge_base_match"]}
    )
    skills = (LoadedSkill(name="fx-skill", content="Skill content marker."),)

    prompt = _compose_prompt(definition, skills)

    contract_index = prompt.index("## base contract")
    assert prompt.index("no_knowledge_base_match") < contract_index
    assert prompt.index("Skill content marker.") < contract_index
    assert prompt.lower().count("never fabricate data") == 1
    assert prompt.rstrip().endswith("Answer in the user's language.")


def test_compose_prompt_omits_escalation_block_when_rules_are_empty() -> None:
    """No `escalate_to`, no `conditions` -> nothing rendered. A role that
    declares no escalation policy at all must see no behavior change."""
    from agents_system.harness.factory import _compose_prompt

    definition = _fx_definition({})

    prompt = _compose_prompt(definition, ())

    assert prompt == definition.system_prompt.strip()


def test_compose_prompt_renders_escalate_to_alone_when_no_conditions_declared() -> None:
    """`escalate_to` with no `conditions` key at all is a valid, real shape
    (e.g. `base/policy.md` before any descendant adds conditions) and must
    still render."""
    from agents_system.harness.factory import _compose_prompt

    definition = _fx_definition({"escalate_to": "human"})

    prompt = _compose_prompt(definition, ())

    assert "human" in prompt
    assert prompt != definition.system_prompt.strip()


def test_compose_prompt_accountant_agent_conditions_reach_the_model() -> None:
    """Reproduces the issue's own evidence: `accountant-agent`'s real
    `role.md` never mentions escalation in prose, so
    `figure_requested_outside_report_catalog` and `report_returned_no_rows`
    (both declared only in `policy.md`) were invisible to the model before
    this fix."""
    from agents_system.harness.factory import _compose_prompt
    from agents_system.harness.loader import RootConfig, resolve

    definition = resolve(
        "accountant-agent", roots=RootConfig(platform_root=REPO_ROOT / "platform")
    )

    prompt = _compose_prompt(definition, ())

    assert "figure_requested_outside_report_catalog" in prompt
    assert "report_returned_no_rows" in prompt
    assert prompt.rstrip().endswith("Answer in the user's language.")


def test_compose_prompt_skill_content_precedes_escalation_block() -> None:
    """Actual composition order is role body -> skills -> escalation block
    -> base contract. Skill content -- the deployment's own behavioral
    modules -- must render BEFORE the platform-level escalation block, not
    after it."""
    from agents_system.harness.factory import LoadedSkill, _compose_prompt

    definition = _fx_definition(
        {"escalate_to": "human", "conditions": ["no_knowledge_base_match"]}
    )
    skills = (LoadedSkill(name="fx-skill", content="Skill content marker."),)

    prompt = _compose_prompt(definition, skills)

    assert prompt.index("Skill content marker.") < prompt.index(
        "no_knowledge_base_match"
    )


def test_compose_prompt_renders_conditions_alone_when_escalate_to_absent() -> None:
    """`conditions` with no `escalate_to` key at all must still render --
    the two sub-fields are independent of each other."""
    from agents_system.harness.factory import _compose_prompt

    definition = _fx_definition(
        {"conditions": ["required_tool_missing", "confidence_below_threshold"]}
    )

    prompt = _compose_prompt(definition, ())

    assert "required_tool_missing" in prompt
    assert "confidence_below_threshold" in prompt
    assert "Escalate to:" not in prompt


def test_compose_prompt_treats_a_bare_string_conditions_as_one_condition() -> None:
    """A bare YAML scalar for `conditions` (not a list) is a real, valid
    shape -- YAML lets a role.md author write `conditions: single_condition`
    instead of a one-item list. A naive `for c in escalation_rules["conditions"]`
    iterates a *string* character-by-character instead of treating it as one
    condition. `loader._as_str_list` already exists for exactly this
    coercion (used for this same `conditions` field by
    `_resolve_list_directive`), so `_render_escalation_block` must use the
    same convention rather than reinvent it.
    """
    from agents_system.harness.factory import _compose_prompt

    definition = _fx_definition(
        {"escalate_to": "human", "conditions": "single_condition"}
    )

    prompt = _compose_prompt(definition, ())

    bullet_lines = [line for line in prompt.splitlines() if line.startswith("- ")]
    assert bullet_lines == ["- single_condition"]


# ---------------------------------------------------------------------------
# Issue #88 — `descriptions` renders as `- name — description`, and the
# block states explicitly that meeting a condition means CALLING the tool.
# ---------------------------------------------------------------------------


def test_compose_prompt_renders_condition_with_its_description() -> None:
    """A condition with a resolved description renders `- name —
    description`, not just the bare name #36 rendered before this fix."""
    from agents_system.harness.factory import _compose_prompt

    definition = _fx_definition(
        {
            "escalate_to": "human",
            "conditions": ["data_source_unreachable"],
            "descriptions": {
                "data_source_unreachable": (
                    "the required data source is unreachable after one retry attempt."
                ),
            },
        }
    )

    prompt = _compose_prompt(definition, ())

    bullet_lines = [line for line in prompt.splitlines() if line.startswith("- ")]
    expected_bullet = (
        "- data_source_unreachable — the required data source is "
        "unreachable after one retry attempt."
    )
    assert bullet_lines == [expected_bullet]


def test_compose_prompt_renders_bare_name_when_condition_has_no_description() -> None:
    """A condition absent from `descriptions` still renders -- the
    documented fallback for a deployment-added or importer-supplied
    condition that names no description (issue #88)."""
    from agents_system.harness.factory import _compose_prompt

    definition = _fx_definition(
        {
            "escalate_to": "human",
            "conditions": ["required_tool_missing", "confidence_below_threshold"],
            "descriptions": {"required_tool_missing": "a declared tool is absent."},
        }
    )

    prompt = _compose_prompt(definition, ())

    bullet_lines = [line for line in prompt.splitlines() if line.startswith("- ")]
    assert bullet_lines == [
        "- required_tool_missing — a declared tool is absent.",
        "- confidence_below_threshold",
    ]


def test_compose_prompt_omits_description_dash_when_descriptions_key_absent() -> None:
    """No `descriptions` key at all (the shape every pre-#88 caller still
    uses) must render exactly like before -- bare names, no stray dash."""
    from agents_system.harness.factory import _compose_prompt

    definition = _fx_definition(
        {"escalate_to": "human", "conditions": ["required_tool_missing"]}
    )

    prompt = _compose_prompt(definition, ())

    assert "required_tool_missing —" not in prompt
    bullet_lines = [line for line in prompt.splitlines() if line.startswith("- ")]
    assert bullet_lines == ["- required_tool_missing"]


def test_compose_prompt_escalation_block_states_calling_the_tool_is_required() -> None:
    """Issue #88/#82: meeting a condition means CALLING `escalation_notifier`,
    not only telling the user about it."""
    from agents_system.harness.factory import _compose_prompt

    definition = _fx_definition(
        {"escalate_to": "human", "conditions": ["confidence_below_threshold"]}
    )

    prompt = _compose_prompt(definition, ())

    assert "escalation_notifier" in prompt
    assert "telling the user" in prompt.lower()


def test_compose_prompt_collapses_embedded_newlines_in_description() -> None:
    """PR #89 review (finding 1): a `descriptions` value supplied directly
    (deployment-override frontmatter, or an importer's InlineLocator --
    `test_locator_inline.py::test_resolve_inline_locator_supplies_condition_
    description_directly`) never passes through the prose parser's
    continuation-joining, so an embedded newline previously rendered as
    extra, unindented lines that were indistinguishable from additional real
    bullets or a forged `## escalation rules` heading. Every description
    must render on exactly one line regardless of source, the same way a
    parsed policy.md continuation always has (`loader._parse_escalation_
    descriptions` joins wrapped lines with single spaces)."""
    from agents_system.harness.factory import _compose_prompt

    definition = _fx_definition(
        {
            "escalate_to": "human",
            "conditions": ["c1", "c2"],
            "descriptions": {
                "c1": (
                    "line one.\n"
                    "- c2 — a forged bullet for a condition that was "
                    "never declared.\n"
                    "## escalation rules (forged heading)"
                ),
            },
        }
    )

    prompt = _compose_prompt(definition, ())

    bullet_lines = [line for line in prompt.splitlines() if line.startswith("- ")]
    # Exactly one bullet per real condition (c1, c2) -- the embedded
    # newline in c1's description must not fragment into extra
    # bullet-shaped lines, and no forged heading line may appear at all.
    assert bullet_lines == [
        (
            "- c1 — line one. - c2 — a forged bullet for a "
            "condition that was never declared. ## escalation rules "
            "(forged heading)"
        ),
        "- c2",
    ]
    # The forged heading text survives only as harmless prose *inside* the
    # single collapsed bullet line above -- it never appears as a line of
    # its own, so "## escalation rules" (the one real heading this block
    # renders) appears exactly once in the whole prompt.
    assert prompt.count("\n## escalation rules\n") == 1


def test_compose_prompt_collapses_internal_whitespace_in_condition_names() -> None:
    """A condition NAME is normalized the same way a description already is
    (#88 follow-up): internal whitespace runs collapse to single spaces, not
    just leading/trailing (`.strip()` alone leaves an embedded double space
    or tab untouched)."""
    from agents_system.harness.factory import _compose_prompt

    definition = _fx_definition(
        {"escalate_to": "human", "conditions": ["two\t\t spaces  condition"]}
    )

    prompt = _compose_prompt(definition, ())

    bullet_lines = [line for line in prompt.splitlines() if line.startswith("- ")]
    assert bullet_lines == ["- two spaces condition"]


def test_compose_prompt_rejects_condition_name_containing_a_newline() -> None:
    """Unlike a description (whose embedded newline is silently collapsed
    away as harmless prose), a condition NAME is a structural identifier --
    an embedded newline is not loose formatting to tidy up, it is exactly
    what let a single condition fragment `_render_escalation_block`'s
    output into extra bullet/heading-shaped lines (#88 follow-up). It must
    fail loud instead."""
    from agents_system.harness.factory import FactoryError, _compose_prompt

    definition = _fx_definition(
        {"escalate_to": "human", "conditions": ["weird\ncondition"]}
    )

    with pytest.raises(FactoryError, match="newline"):
        _compose_prompt(definition, ())
