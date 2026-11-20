"""Agent definition loader.

Reads YAML-frontmatter definition files from disk, merges platform generic
roles with deployment overrides, enforces structural invariants, and returns
a frozen ``AgentDefinition``.

Public API
----------
- ``RootConfig``          — injectable path config (platform + deployments roots)
- ``load_generic``        — reads platform/roles/{role_type}/{role,manifest,policy}.md
- ``load_override``       — reads deployments/{client}/{role_type}/…; None if absent
- ``merge``               — applies merge directives + validates invariants
- ``resolve``             — top-level entry point: load → merge → validate → return

Where the code lives
--------------------
This module holds ``resolve`` and answers for the public names. The rest is
in ``harness.definition``, one concern per module: ``roots``, ``model``,
``errors`` and ``policy`` (types and constants); ``frontmatter``, ``fsread``,
``locate`` and ``files`` (reading a definition from disk); ``prompt``
(prompt composition); ``directives``, ``validators``, ``safeguards``,
``model_config`` and ``command_tools`` (the checks); ``fold``, ``loading``
and ``merge`` (the ``extends:`` chain and the deployment override).

Merge directive vocabulary
--------------------------
- Field absent in override  → inherited from parent unchanged
- Scalar ``"inherit"``       → take parent value as-is
- ``{inherit: true, add: [...]}``    → parent list + additions (dedup, preserve order)
- ``{inherit: true, remove: [...]}`` → parent list minus removals
- ``{override: <value>}``            → replace parent value (invariants still apply)

Invariants (enforced in ``merge``, raises ``DefinitionError`` on violation)
---------------------------------------------------------------------------
1. set(override.tools) ⊆ set(parent.tools)
2. resolved permissions ⊆ parent permissions
3. autonomy_rank(override) ≤ autonomy_rank(parent)
4. If execution_limits present in override: each numeric limit ≤ parent/platform default

NOTE: tool-name registry validation (against a live ToolRegistry) is NOT in
scope for this module — that is the injector's responsibility.

Prompt composition (ADR-002 B.8/B.9)
-------------------------------------
- A ``role.md`` prose body may carry a ``## design notes`` heading. Text
  above it is model-facing and folds into ``system_prompt``; text at/after
  it is developer-only rationale and is stripped before composition
  (``_split_design_notes``). A near-miss heading (wrong level, a typo)
  raises ``DefinitionError`` rather than silently leaking.
- ``resolve()`` appends a fixed six-clause base prompt contract
  (``_append_base_contract``) to every composed prompt it returns, exactly
  once regardless of ``extends:`` chain depth — no role.md or deployment
  override declares it and none can omit or contradict it.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from agents_system.execution_limits import validate_execution_limits
from agents_system.harness.definition.command_tools import _revalidate_command_tools
from agents_system.harness.definition.errors import (
    DefinitionError,
    PredefinedRoleDeprecationWarning,
)
from agents_system.harness.definition.loading import load_generic, load_override
from agents_system.harness.definition.merge import merge
from agents_system.harness.definition.model import (
    AgentDefinition,
    CommandToolDeclaration,
    CommandToolParam,
    FolderLocator,
    InlineLocator,
    RawDefinition,
    RoleLocator,
)
from agents_system.harness.definition.model_config import _resolve_model_declaration
from agents_system.harness.definition.policy import (
    _PLATFORM_DEFAULT_LIMITS,
    PLATFORM_DEFAULT_LIMITS,
)
from agents_system.harness.definition.prompt import _append_base_contract
from agents_system.harness.definition.roots import RootConfig
from agents_system.harness.definition.validators import _validate_untrusted_input_exec
from agents_system.harness.registry import Tier

if TYPE_CHECKING:
    from agents_system.agent.spec import Agent

#: The public names this module answers for. The types live in
#: ``harness.definition`` and keep this import path; a private name is
#: imported from the module that defines it.
__all__ = [
    "PLATFORM_DEFAULT_LIMITS",
    "AgentDefinition",
    "CommandToolDeclaration",
    "CommandToolParam",
    "DefinitionError",
    "FolderLocator",
    "InlineLocator",
    "PredefinedRoleDeprecationWarning",
    "RawDefinition",
    "RoleLocator",
    "RootConfig",
    "Tier",
    "load_generic",
    "load_override",
    "merge",
    "resolve",
]


# ---------------------------------------------------------------------------
# resolve — public entry point
# ---------------------------------------------------------------------------


def _limit_defaults(deployment_limits: Mapping[str, Any]) -> Mapping[str, Any]:
    """#278 -- what an undeclared limit is held to under a deployment ceiling.

    A deployment value above the platform default replaces it, so the agent
    may declare anything up to it. One below leaves the platform default as
    the load-time ceiling: the runtime applies the deployment's value
    (``limits._effective_limits``), so a declaration above it is capped, not
    refused.
    """
    if not deployment_limits:
        return _PLATFORM_DEFAULT_LIMITS
    merged = dict(_PLATFORM_DEFAULT_LIMITS)
    for key, value in deployment_limits.items():
        merged[key] = max(merged[key], value)
    return merged


def resolve(
    locator: RoleLocator | Agent,
    *,
    client: str | None = None,
    roots: RootConfig | None = None,
    execution_limits: Mapping[str, Any] | None = None,
) -> AgentDefinition:
    """Load, merge, validate, and return a fully resolved ``AgentDefinition``.

    Every command tool declaration the resolved chain carries is checked
    again here (``_revalidate_command_tools``, issue #75), whichever locator
    built it.

    Parameters
    ----------
    locator:
        A predefined-role name (``str``, e.g. ``"sales-agent"``), an
        ``Agent`` (issue #168: resolved through its own ``_to_locator()``),
        an importer-supplied folder (``FolderLocator``), or an already-built
        definition (``InlineLocator`` — design.md D1).
    client:
        Optional deployment client name.  Only valid together with a ``str``
        locator (design.md D1 Q5) — a folder- or inline-sourced agent has no
        deployment tree to look an override up in. If given and the override
        folder exists, the override is merged on top of the generic
        definition. If the folder does not exist, the generic definition is
        returned.
    roots:
        Injectable path config.  Defaults to the real repo roots.
    execution_limits:
        #278 -- the deployment's ceiling for this runtime
        (``total_execution_timeout_s``, ``tool_call_timeout_s``,
        ``max_tool_calls``). It replaces the platform default as the ceiling
        an importer-authored definition is held to, so the agent may declare
        up to it; the definition alone still cannot raise anything. Checked
        by ``validate_execution_limits`` first.
    """
    limit_defaults = _limit_defaults(
        validate_execution_limits(execution_limits, who="the deployment")
    )
    if not isinstance(locator, str | FolderLocator | InlineLocator):
        # Deferred: `agent.spec` imports this module.
        from agents_system.agent.spec import Agent

        if isinstance(locator, Agent):
            locator = locator._to_locator()

    if client is not None and not isinstance(locator, str):
        raise DefinitionError(
            "Invariant violation — client: a deployment override (client=) "
            "is only valid together with a predefined-role (str) locator; "
            f"got {type(locator).__name__}. A folder- or inline-sourced "
            "agent has no deployment tree to look an override up in."
        )

    if roots is None:
        roots = RootConfig()

    generic = load_generic(locator, roots=roots, limit_defaults=limit_defaults)
    # Issue #75: every locator kind, not only a parsed manifest.
    _revalidate_command_tools(generic)

    if client is not None:
        if not isinstance(locator, str):
            # Unreachable: the guard above already rejected a non-`str`
            # locator combined with `client` before any resolution work
            # began. Re-checked here as a real `raise` — not `assert`, which
            # `python -O` strips — purely so mypy narrows `locator: str` for
            # the `load_override` call below without relying on a
            # runtime-optional statement for type safety.
            raise DefinitionError(
                "Invariant violation — client: a deployment override "
                "(client=) is only valid together with a predefined-role "
                "(str) locator."
            )
        override = load_override(client, locator, roots=roots)
        if override is not None:
            merged = merge(generic, override, limit_defaults=limit_defaults)
            return dataclasses.replace(
                merged,
                system_prompt=_append_base_contract(merged.system_prompt),
            )

    # No override — wrap the generic RawDefinition into a frozen AgentDefinition
    parent_perms = (
        list(generic.permissions) if isinstance(generic.permissions, list) else []
    )
    exec_limits: Mapping[str, Any] | None = (
        dict(generic.execution_limits)
        if isinstance(generic.execution_limits, dict)
        else None
    )
    # D-037 (issue #13, ADR-004). No deployment override exists on this
    # path, so this is the ONLY validation a role's own `model:` gets when
    # resolved without one -- the same depth `execution_limits` gets here
    # (shape-coerced, not ceiling-checked; the fold already happened in
    # `_resolve_role_chain`/`_fold_parent_into_child`). PR #141 review:
    # `_resolve_model_declaration` also raises on a malformed non-mapping
    # value instead of silently discarding it as `None`.
    resolved_model = _resolve_model_declaration(
        generic.model, who=f"role '{generic.role_name}'"
    )

    # --- ADR-002 C.11: untrusted_input ⊥ exec:*, the no-override branch ---
    # `merge()` is never called on this path, so nothing above has validated
    # anything — this call is the only guard a role resolved with no
    # deployment override gets. `bool(...)`: see the identical coercion note
    # in `_merge_validated`.
    resolved_untrusted_input: bool = bool(generic.untrusted_input)
    _validate_untrusted_input_exec(
        resolved_untrusted_input, parent_perms, role_name=generic.role_name
    )

    return AgentDefinition(
        role_name=generic.role_name,
        version=generic.version,
        deployment=None,
        system_prompt=_append_base_contract(generic.system_prompt),
        tools=tuple(generic.tools),
        skills=tuple(generic.skills),
        context=dict(generic.context),
        permissions=tuple(parent_perms),
        autonomy=generic.autonomy,
        escalation_rules=dict(generic.escalation_rules),
        delegation_policy=dict(generic.delegation_policy),
        memory_policy=dict(generic.memory_policy),
        audit_policy=dict(generic.audit_policy),
        execution_limits=exec_limits,
        model=resolved_model,
        untrusted_input=resolved_untrusted_input,
        command_tools=tuple(
            generic.command_tool_declarations[name] for name in generic.command_tools
        ),
        skills_folder=generic.skills_folder,
        inline_skills=dict(generic.inline_skills),
        importer_root=generic.importer_root,
    )
