"""Agent factory — the assembler.

Combines the three harness primitives into a single, ready-to-run value object:

1. ``loader.resolve``            → a validated ``AgentDefinition`` (WHO / WHAT / HOW)
2. ``injector.resolve_tool_surface`` → the granted tool surface (Layer 1 RBAC)
3. skill files on disk           → the deployment's behavioural modules

It then composes the final system prompt (role body + skill bodies) and returns
a frozen ``EquippedRuntime``.

Scope boundary
--------------
The factory does NOT talk to an LLM and does NOT bind tools to a model. Turning
an ``EquippedRuntime`` into a live, executing agent (LangGraph, ``bind_tools``)
is the Agent Runtime's job — a later slice.

Prompt composition contract
---------------------------
The composed prompt is the role body followed by each skill file's content,
verbatim, joined by a ``---`` separator, in the order the skills are declared in
the manifest. Skill files own their own headings; the factory does not rewrite
them (same principle as the skill-resolver: pass content, preserve author
intent).

Skills are deployment-specific: they live in
``deployments/{client}/{role_type}/skills/{name}.md``. A generic role (no
client) has no skills, so its prompt is just the role body.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Iterable

import structlog

from agents_system.harness.injector import (
    _emit,
    resolve_command_tool_surface,
    resolve_tool_surface,
)
from agents_system.harness.loader import (
    AgentDefinition,
    RootConfig,
    _BASE_PROMPT_CONTRACT,
    _require_deployments_root,
    _strip_base_contract,
    resolve,
)
from agents_system.harness.registry import ToolRegistry, ToolSpec

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


def _compose_prompt(
    definition: AgentDefinition,
    skills: tuple[LoadedSkill, ...],
) -> str:
    """Compose the final system prompt: role body + skill bodies + base contract.

    ADR-002 B.9's six-clause base contract must be the LAST block of the
    prompt the model actually receives: the Agent Runtime sends THIS
    function's return value (``EquippedRuntime.system_prompt``) to the
    model, not ``definition.system_prompt`` (`agent/graph.py`'s
    `_call_model` uses ``equipped.system_prompt``). ``resolve()`` already
    appends the contract to ``definition.system_prompt`` so that value
    alone still satisfies B.9 for a caller reading it directly, but if this
    function simply appended skill content after it (as it used to), the
    contract would end up in the middle rather than last whenever a role
    had skills.

    When there is no skill content to insert, ``definition.system_prompt``
    is already correct as-is (the contract is already its final block) and
    is returned unchanged. When skills exist, the contract is stripped back
    off (``loader._strip_base_contract``, the exact inverse of
    ``loader._append_base_contract``), skill content is inserted, and the
    contract is appended once more — so it still appears exactly once
    overall, now genuinely last.
    """
    if not skills:
        return definition.system_prompt.strip()

    role_prompt = _strip_base_contract(definition.system_prompt).strip()
    parts = [role_prompt]
    parts.extend(skill.content for skill in skills)
    parts.append(_BASE_PROMPT_CONTRACT.strip())
    return _SKILL_SEPARATOR.join(p for p in parts if p)


def build_runtime(
    role_type: str,
    registry: ToolRegistry,
    granted_permissions: Iterable[str],
    *,
    client: str | None = None,
    roots: RootConfig | None = None,
    session_provider: "async_sessionmaker[AsyncSession] | None" = None,
) -> EquippedRuntime:
    """Assemble an ``EquippedRuntime`` for a role (optionally a client deployment).

    Parameters
    ----------
    role_type:
        The role folder name (e.g. ``"sales-agent"``).
    registry:
        The live tool registry the granted surface is resolved against.
    granted_permissions:
        The requesting identity's permission grants. The effective tool surface
        is ``role.permissions ∩ granted_permissions`` (enforced by the injector).
    client:
        Optional deployment client. When given, the deployment override is
        merged on top of the generic role and its skill files are loaded.
    roots:
        Injectable path config. Defaults to the real repo roots.
    """
    if roots is None:
        roots = RootConfig()

    # Materialised once: an `Iterable` may be a one-shot generator, and it is
    # now consumed by TWO surface resolutions below (registry tools, then
    # ADR-002 C.12 command tools) rather than one.
    granted = list(granted_permissions)

    definition = resolve(role_type, client=client, roots=roots)
    surface = resolve_tool_surface(definition, registry, granted)
    command_surface = resolve_command_tool_surface(definition, granted)
    skills = _load_skills(definition, client, roots)
    system_prompt = _compose_prompt(definition, skills)

    granted_tools = surface.granted + command_surface.granted
    denied_tools = surface.denied + command_surface.denied

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
    )
