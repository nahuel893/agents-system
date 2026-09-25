"""Agent factory — the assembler.

Combines the three harness primitives into a single, ready-to-run value object:

1. ``loader.resolve``            → a validated ``AgentDefinition`` (WHO / WHAT / HOW)
2. ``injector.resolve_tool_surface`` → the granted tool surface (Layer 1 RBAC)
3. skill files on disk           → the deployment's behavioural modules

It then composes the final system prompt (role body + skill bodies +
escalation rules) and returns a frozen ``EquippedRuntime``.

Scope boundary
--------------
The factory does NOT talk to an LLM and does NOT bind tools to a model. Turning
an ``EquippedRuntime`` into a live, executing agent (LangGraph, ``bind_tools``)
is the Agent Runtime's job — a later slice.

Prompt composition contract
---------------------------
The composed prompt is the role body, followed by each skill file's content
verbatim, joined by a ``---`` separator, in the order the skills are declared
in the manifest, followed by the resolved ``escalation_rules`` rendered as a
short block (issue #36 — see ``_render_escalation_block``). Skill files own
their own headings; the factory does not rewrite them (same principle as the
skill-resolver: pass content, preserve author intent). The ADR-002 B.9 base
contract (``_BASE_PROMPT_CONTRACT``) is always the LAST block, regardless of
skills or escalation content — see ``_compose_prompt``.

Skills are deployment-specific: they live in
``deployments/{client}/{role_type}/skills/{name}.md``. A generic role (no
client) has no skills, so its prompt is the role body plus its escalation
block (if any).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any, cast

import structlog

from agents_system.harness.injector import (
    _emit,
    resolve_command_tool_surface,
    resolve_tool_surface,
)
from agents_system.harness.loader import (
    _BASE_PROMPT_CONTRACT,
    AgentDefinition,
    RootConfig,
    _as_str_list,
    _require_deployments_root,
    _strip_base_contract,
    resolve,
)
from agents_system.harness.registry import ToolRegistry, ToolSpec
from agents_system.permissions import Permission
from agents_system.permissions.permission_registry import permission_registry

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


logger = structlog.get_logger()

_SKILL_SEPARATOR = "\n\n---\n\n"


class FactoryError(Exception):
    """Raised when a runtime cannot be assembled (e.g. a skill file is missing)."""


@dataclasses.dataclass(frozen=True)
class LoadedSkill:
    """A deployment skill module loaded from disk."""

    name: str
    content: str


@dataclasses.dataclass(frozen=True)
class EquippedRuntime:
    """A fully assembled, ready-to-run agent specification.

    This is NOT a live agent — it carries everything the Agent Runtime needs to
    instantiate one: the resolved definition (context / policy / autonomy live
    there), the composed system prompt, the granted tool surface, the denied
    tools (for audit), and the loaded skill modules.

    D-009: session_provider is an optional async_sessionmaker. When set,
    _execute_tools opens one AsyncSession per turn and forwards it to async
    connectors. Defaults to None for backward compatibility.
    """

    definition: AgentDefinition
    system_prompt: str
    tools: tuple[ToolSpec, ...]
    denied_tools: tuple[tuple[str, str], ...]
    skills: tuple[LoadedSkill, ...]
    session_provider: async_sessionmaker[AsyncSession] | None = None
    deploy_grant_ceiling: frozenset[type[Permission]] = frozenset()
    """The deploy-time grant ceiling (issue #38, design.md Resolved Decision
    5): the exact permission classes this runtime was explicitly granted at
    boot (`main.py`'s `DEPLOY_GRANTS`, or a library caller's
    `granted_permissions`). Layer-2 revalidation (`interceptor.intercept`,
    `AgentRuntime.run_turn`'s default) bounds itself to THIS set — never to
    `definition.permissions`, the role's full declared set — so a role that
    merely DECLARES a permission is not the same as a deployment actually
    GRANTING it. Independent of, and resolved alongside, the existing
    `tools`/`denied_tools` computation (unchanged by this field).
    """


def _load_skills(
    definition: AgentDefinition,
    client: str | None,
    roots: RootConfig,
) -> tuple[LoadedSkill, ...]:
    """Load every declared skill file from the deployment's skills/ directory."""
    if not definition.skills:
        return ()

    if client is None:
        # Generic roles declare no skills; reaching here means a definition was
        # built with skills but no deployment to load them from.
        raise FactoryError(
            f"Role '{definition.role_name}' declares skills {list(definition.skills)} "
            f"but no client deployment was given to load them from."
        )

    # Guarded at the point of use, the same way the loader guards
    # `platform_root`. Defence in depth, and honestly labelled as such: on
    # the public path `build_runtime` calls `resolve` first, which already
    # raises for an absent root whenever `client is not None`, so this guard
    # fires only for a direct call to this private function. It stays because
    # the alternative reading -- a skills path built from an absent root --
    # reports "skill file missing" for every skill, which blames the
    # deployment author for a consumer's misconfiguration.
    skills_dir = (
        _require_deployments_root(roots.deployments_root)
        / client
        / definition.role_name
        / "skills"
    )

    loaded: list[LoadedSkill] = []
    for name in definition.skills:
        path = skills_dir / f"{name}.md"
        if not path.exists():
            logger.error(
                "factory.skill_missing",
                skill=name,
                role=definition.role_name,
                deployment=definition.deployment,
                path=str(path),
            )
            # D-007: record skill_missing event before raising
            _emit(
                "record_skill_missing",
                definition=definition,
                skill=name,
                path=str(path),
            )
            raise FactoryError(
                f"Skill '{name}' declared by {definition.role_name}/"
                f"{client} has no file at {path}"
            )
        content = path.read_text(encoding="utf-8").strip()
        loaded.append(LoadedSkill(name=name, content=content))
        logger.info(
            "factory.skill_loaded",
            skill=name,
            role=definition.role_name,
            deployment=definition.deployment,
        )
        # D-007: record skill_loaded event
        _emit(
            "record_skill_loaded",
            definition=definition,
            skill=name,
        )

    return tuple(loaded)


_ESCALATION_HEADING = "## escalation rules"


def _render_escalation_block(escalation_rules: Mapping[str, Any]) -> str:
    """Render the resolved ``escalation_rules`` into a short, model-facing
    block, or ``""`` when there is nothing to say (issue #36).

    ``escalation_rules`` (``escalate_to`` + ``conditions``) is structured
    policy data parsed from ``policy.md`` by ``harness.loader.resolve()`` —
    but before this function existed, nothing ever read it back out.
    ``policy.md``'s prose explains WHY a condition exists; role.md was the
    only place its NAME ever reached the model, and only if a role.md author
    happened to restate it there. ``accountant-agent`` is the case that
    proved the gap: its ``role.md`` never mentions escalation, so
    ``figure_requested_outside_report_catalog`` — declared only in its
    ``policy.md`` — never influenced the model at all.

    Unknown/empty input renders nothing: a role that declares no escalation
    policy gets no block, so this is a strict addition for roles that do.

    ``conditions`` is coerced through ``loader._as_str_list`` — the same
    coercion ``loader._resolve_mapping_directive`` already applies to this
    exact field for the ``add``/``remove`` deployment directives — rather
    than iterated directly. YAML permits a bare scalar
    (``conditions: single_condition``) as well as a list; iterating a bare
    *string* directly yields one bullet per CHARACTER, not one condition.
    """
    escalate_to = str(escalation_rules.get("escalate_to") or "").strip()
    conditions = [
        condition
        for condition in (
            c.strip() for c in _as_str_list(escalation_rules.get("conditions"))
        )
        if condition
    ]

    if not escalate_to and not conditions:
        return ""

    lines = [_ESCALATION_HEADING, ""]
    if escalate_to:
        lines.append(f"Escalate to: {escalate_to}")
    if conditions:
        if escalate_to:
            lines.append("")
        lines.append(
            "Escalate immediately, rather than guess, whenever any of these apply:"
        )
        lines.extend(f"- {condition}" for condition in conditions)

    return "\n".join(lines)


def _compose_prompt(
    definition: AgentDefinition,
    skills: tuple[LoadedSkill, ...],
) -> str:
    """Compose the final system prompt: role body + skill bodies +
    escalation rules + base contract.

    ADR-002 B.9's six-clause base contract must be the LAST block of the
    prompt the model actually receives: the Agent Runtime sends THIS
    function's return value (``EquippedRuntime.system_prompt``) to the
    model, not ``definition.system_prompt`` (`agent/graph.py`'s
    `_call_model` uses ``equipped.system_prompt``). ``resolve()`` already
    appends the contract to ``definition.system_prompt`` so that value
    alone still satisfies B.9 for a caller reading it directly, but if this
    function simply appended skill (or escalation) content after it, the
    contract would end up in the middle rather than last.

    When there is neither skill content nor a resolved escalation policy to
    insert, ``definition.system_prompt`` is already correct as-is (the
    contract is already its final block) and is returned unchanged. Whenever
    either exists, the contract is stripped back off
    (``loader._strip_base_contract``, the exact inverse of
    ``loader._append_base_contract``), skill content and then the escalation
    block (issue #36) are inserted in that order, and the contract is
    appended once more — so it still appears exactly once overall, now
    genuinely last.
    """
    escalation_block = _render_escalation_block(definition.escalation_rules)

    if not skills and not escalation_block:
        return definition.system_prompt.strip()

    role_prompt = _strip_base_contract(definition.system_prompt).strip()
    parts = [role_prompt]
    parts.extend(skill.content for skill in skills)
    if escalation_block:
        parts.append(escalation_block)
    parts.append(_BASE_PROMPT_CONTRACT.strip())
    return _SKILL_SEPARATOR.join(p for p in parts if p)


def build_runtime(
    role_type: str,
    registry: ToolRegistry,
    granted_permissions: Iterable[str | type[Permission]],
    *,
    client: str | None = None,
    roots: RootConfig | None = None,
    session_provider: async_sessionmaker[AsyncSession] | None = None,
) -> EquippedRuntime:
    """Assemble an ``EquippedRuntime`` for a role (optionally a client deployment).

    Parameters
    ----------
    role_type:
        The role folder name (e.g. ``"sales-agent"``).
    registry:
        The live tool registry the granted surface is resolved against.
    granted_permissions:
        The requesting identity's permission grants — the deploy-time grant
        ceiling (issue #38). Accepts registered wire-name strings,
        ``Permission`` subclasses, or a mixture of both (spec: "Accepted
        grant forms"); every entry is normalized through the registry into
        ``EquippedRuntime.deploy_grant_ceiling``. The effective tool surface
        is ``role.permissions ∩ granted_permissions`` (enforced by the
        injector) — a SEPARATE, string-keyed computation this ceiling does
        not change.
    client:
        Optional deployment client. When given, the deployment override is
        merged on top of the generic role and its skill files are loaded.
    roots:
        Injectable path config. Defaults to the real repo roots.
    """
    if roots is None:
        roots = RootConfig()

    # Materialised once: an `Iterable` may be a one-shot generator, and it is
    # now consumed by THREE resolutions below (registry tools, ADR-002 C.12
    # command tools, and the deploy_grant_ceiling below) rather than one.
    granted = list(granted_permissions)

    definition = resolve(role_type, client=client, roots=roots)
    # `resolve_tool_surface`/`resolve_command_tool_surface` are typed
    # `Iterable[str]` and are UNCHANGED by this task (design.md: "no
    # behavior change to tool-surface resolution"): their string-set
    # intersection already tolerates a stray `Permission` class entry
    # (it simply never matches `definition.permissions`, a str tuple) —
    # this cast documents that existing tolerance rather than widening
    # their signature.
    surface = resolve_tool_surface(definition, registry, cast("list[str]", granted))
    command_surface = resolve_command_tool_surface(
        definition, cast("list[str]", granted)
    )
    skills = _load_skills(definition, client, roots)
    system_prompt = _compose_prompt(definition, skills)

    granted_tools = surface.granted + command_surface.granted
    denied_tools = surface.denied + command_surface.denied

    # issue #38 — the deploy-time grant ceiling, persisted independently of
    # the string-keyed tool-surface resolution above. `resolve_tool_surface`
    # deliberately keeps consuming the raw string/class values (no behavior
    # change to tool-surface resolution in this task); this is a SEPARATE,
    # class-based normalization Layer-2 revalidation bounds itself to.
    deploy_grant_ceiling = frozenset(
        permission_registry.resolve(p) if isinstance(p, str) else p for p in granted
    )

    logger.info(
        "factory.runtime_built",
        role=definition.role_name,
        deployment=definition.deployment,
        tools=len(granted_tools),
        denied=len(denied_tools),
        skills=len(skills),
    )
    # D-007: record runtime_built event
    _emit(
        "record_runtime_built",
        definition=definition,
        tools_count=len(granted_tools),
        denied_count=len(denied_tools),
        skills_count=len(skills),
    )

    return EquippedRuntime(
        definition=definition,
        system_prompt=system_prompt,
        tools=granted_tools,
        denied_tools=denied_tools,
        skills=skills,
        session_provider=session_provider,
        deploy_grant_ceiling=deploy_grant_ceiling,
    )
