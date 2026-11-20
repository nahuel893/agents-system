"""Merging a deployment override into a generic definition, with every invariant checked."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import structlog

from agents_system.harness.definition.directives import (
    _descriptions_of,
    _resolve_mapping_directive,
    _resolve_permissions,
)
from agents_system.harness.definition.errors import DefinitionError
from agents_system.harness.definition.model import AgentDefinition, RawDefinition
from agents_system.harness.definition.model_config import _resolve_model_declaration
from agents_system.harness.definition.policy import (
    _DEPLOYMENT_RULE,
    _PLATFORM_DEFAULT_LIMITS,
    _POLICY_FIELDS,
)
from agents_system.harness.definition.prompt import _PROMPT_SEPARATOR
from agents_system.harness.definition.safeguards import (
    _validate_audit_redaction,
    _validate_escalation_kept,
    _validate_escalation_wording,
)
from agents_system.harness.definition.validators import (
    _validate_autonomy,
    _validate_command_tools,
    _validate_execution_limits,
    _validate_permissions,
    _validate_tools,
    _validate_untrusted_input_exec,
    _validate_untrusted_input_monotonic,
)

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# merge
# ---------------------------------------------------------------------------


def merge(
    generic: RawDefinition,
    override: RawDefinition,
    *,
    limit_defaults: Mapping[str, Any] = _PLATFORM_DEFAULT_LIMITS,
) -> AgentDefinition:
    """Validate and merge a generic definition with an override.

    Logs the outcome (invariant violation or successful merge) and delegates
    the actual work to ``_merge_validated``.
    """
    # Deferred import: see `_ensure_command_tool_permission_registered`'s
    # docstring for why a module-level `agents_system.permissions` import
    # in this file is a real circular import, not a theoretical one.
    from agents_system.permissions import UntrustedInputGrantError

    try:
        result = _merge_validated(generic, override, limit_defaults=limit_defaults)
    except (DefinitionError, UntrustedInputGrantError) as exc:
        # UntrustedInputGrantError is NOT a DefinitionError subclass (its
        # root is AgentPermissionError, per spec's own hierarchy; making it
        # inherit DefinitionError too would need `permissions` to import
        # from `harness.loader`, a real cycle) — caught alongside it here
        # so this R4 violation still gets the same invariant-violation log
        # every other merge-time rejection does.
        logger.error(
            "loader.invariant_violation",
            role=generic.role_name,
            deployment=override.deployment,
            detail=str(exc),
        )
        raise
    logger.info(
        "loader.definition_merged",
        role=result.role_name,
        deployment=result.deployment,
        tools=len(result.tools),
    )
    return result


def _merge_validated(
    generic: RawDefinition,
    override: RawDefinition,
    *,
    limit_defaults: Mapping[str, Any] = _PLATFORM_DEFAULT_LIMITS,
) -> AgentDefinition:
    """Apply override merge directives to the generic definition.

    Validates all structural invariants and raises ``DefinitionError`` on any
    violation before returning the frozen ``AgentDefinition``.
    """
    # --- Validate autonomy BEFORE resolving other fields ---
    _validate_autonomy(generic.autonomy, override.autonomy)

    # --- Validate untrusted_input monotonicity, same stage as autonomy ---
    _validate_untrusted_input_monotonic(generic, override)
    # `bool(...)`: both operands are typed `bool | None` on `RawDefinition`
    # (`None` means "not declared"), but `load_generic()` always resolves
    # `generic.untrusted_input` to a concrete bool before `merge()` is
    # reachable through `resolve()` — the coercion only guards a `merge()`
    # call built directly from an unresolved `RawDefinition`.
    resolved_untrusted_input: bool = bool(
        override.untrusted_input
        if override.untrusted_input is not None
        else generic.untrusted_input
    )

    # --- Resolve tools ---
    resolved_tools = override.tools  # override declares its own tool subset
    _validate_tools(generic, override)

    # --- Resolve command_tools (ADR-002 C.12) --- same subtractive shape as
    # tools above: the override names the NAME SUBSET it keeps, validated
    # against the role's own declared set, and the full entries are looked
    # up from the role's declarations (a deployment never redefines one).
    _validate_command_tools(generic, override)
    resolved_command_tools = tuple(
        generic.command_tool_declarations[name] for name in override.command_tools
    )

    # --- Resolve permissions ---
    parent_perms = (
        list(generic.permissions) if isinstance(generic.permissions, list) else []
    )
    who = f"deployment '{override.deployment}' of role '{generic.role_name}'"
    against = f"role '{generic.role_name}'"
    resolved_perms = _resolve_permissions(parent_perms, override.permissions, who=who)
    _validate_permissions(generic, resolved_perms)

    # --- ADR-002 C.11: untrusted_input ⊥ exec:*, checked once permissions
    # are fully resolved (the merge branch of resolve()'s two return paths) ---
    _validate_untrusted_input_exec(
        resolved_untrusted_input, resolved_perms, role_name=generic.role_name
    )

    # --- Resolve the policy mappings (a plain mapping replaces the role's) ---
    policies = {
        field: _resolve_mapping_directive(
            getattr(generic, field), getattr(override, field), who=who, field=field
        )
        for field in _POLICY_FIELDS
    }
    # The role's descriptions carry over a plain mapping too (review of
    # #112): a deployment may describe only the conditions it adds
    # (`_validate_escalation_wording`), so replacing the role's left every
    # inherited condition a bare name in the prompt.
    resolved_escalation = policies["escalation_rules"]
    inherited_descriptions = _descriptions_of(generic.escalation_rules)
    if inherited_descriptions:
        resolved_escalation = {
            **resolved_escalation,
            "descriptions": {
                **inherited_descriptions,
                **_descriptions_of(resolved_escalation),
            },
        }
    resolved_audit = policies["audit_policy"]

    # --- Issue #106: the importer ceiling's escalation and audit rules ---
    # `docs/platform/deployment.md` always said a deployment cannot remove an
    # escalation rule; nothing checked it. A replacing mapping (`conditions:
    # []`) or a `remove:` directive dropped conditions, and
    # `capture_tool_input: true` switched redaction off.
    _validate_escalation_kept(
        generic.escalation_rules,
        resolved_escalation,
        who=who,
        against=against,
        rule=_DEPLOYMENT_RULE,
    )
    declared_escalation = override.escalation_rules
    _validate_escalation_wording(
        generic.escalation_rules,
        declared_escalation if isinstance(declared_escalation, Mapping) else {},
        resolved_escalation,
        who=who,
        against=against,
        rule=_DEPLOYMENT_RULE,
    )
    _validate_audit_redaction(
        generic.audit_policy,
        resolved_audit,
        who=who,
        against=against,
        rule=_DEPLOYMENT_RULE,
    )

    # --- Resolve execution_limits ---
    resolved_limits: Mapping[str, Any] | None
    ov_limits = override.execution_limits
    if ov_limits is None or ov_limits == "inherit":
        resolved_limits = (
            dict(generic.execution_limits)
            if isinstance(generic.execution_limits, dict)
            else None
        )
    elif isinstance(ov_limits, dict):
        # Validate stricter-only invariant before accepting
        baseline = (
            dict(generic.execution_limits)
            if isinstance(generic.execution_limits, dict)
            else limit_defaults
        )
        _validate_execution_limits(baseline, ov_limits, defaults=limit_defaults)
        # MERGED over the baseline, never substituted for it. Replacing the
        # dict dropped every key the deployment did not name, and
        # `_effective_limits` then backfilled those from the PLATFORM
        # defaults rather than from the role -- so declaring ONE stricter
        # limit raised the ceiling on all the others. On `operator-agent`
        # that turned 30s/10 calls into 60s/20 calls for the only role that
        # can run host commands. The validator passed the whole time,
        # because every key actually declared really was stricter; the
        # escape was in the keys left out.
        resolved_limits = {**baseline, **ov_limits}
    else:
        resolved_limits = None

    # --- Resolve model (D-037, issue #13) --- unlike execution_limits above,
    # a deployment's `model:` REPLACES the role's wholesale rather than
    # merging per key: picking a provider+model is a deliberate, complete
    # operator choice, not a partial tweak, and there is no ceiling to
    # measure it against (model/provider selection is not a capability or
    # safety escalation).
    ov_model = override.model
    if ov_model is None or ov_model == "inherit":
        # PR #141 review: this used to take `generic.model` straight, never
        # validating it -- so a malformed role `model:` passed silently
        # through any deployment override that itself said nothing about
        # `model:` (the common case). `_resolve_model_declaration` validates
        # it exactly like the no-override path in `resolve()` does.
        resolved_model = _resolve_model_declaration(
            generic.model, who=f"role '{generic.role_name}'"
        )
    else:
        resolved_model = _resolve_model_declaration(ov_model, who=who)

    # --- Resolve context ---
    resolved_context: dict[str, Any]
    if override.context:
        resolved_context = dict(override.context)
    else:
        resolved_context = dict(generic.context)

    # --- Skills come entirely from the override (platform level is always []) ---
    resolved_skills = list(override.skills)

    # --- Resolve system_prompt ---
    #
    # COMPOSED, not replaced. `system_prompt=override.system_prompt` threw
    # away everything the role chain had folded, so a deployed agent never
    # saw its platform role's prose at all -- and after the taxonomy landed,
    # never saw `base`'s or `agent`'s either. The standing instructions those
    # files carry ("report what ran verbatim", "a confident zero is worse
    # than an error") existed only for a role resolved WITHOUT a deployment,
    # which is not how anything runs.
    #
    # `docs/platform/deployment.md` already described the intended shape:
    # "the deployment role.md EXTENDS the generic role with client-specific
    # context". Same separator the factory uses for skills, so the composed
    # prompt reads as one document.
    prompt_parts = [
        part for part in (generic.system_prompt, override.system_prompt) if part.strip()
    ]
    resolved_prompt = _PROMPT_SEPARATOR.join(prompt_parts)

    return AgentDefinition(
        role_name=generic.role_name,
        version=generic.version,
        deployment=override.deployment,
        system_prompt=resolved_prompt,
        tools=tuple(resolved_tools),
        skills=tuple(resolved_skills),
        context=resolved_context,
        permissions=tuple(resolved_perms),
        autonomy=override.autonomy or generic.autonomy,
        escalation_rules=resolved_escalation,
        delegation_policy=policies["delegation_policy"],
        memory_policy=policies["memory_policy"],
        audit_policy=resolved_audit,
        execution_limits=resolved_limits,
        model=resolved_model,
        untrusted_input=resolved_untrusted_input,
        command_tools=resolved_command_tools,
        # design.md D4 (PR3). This branch only runs for a `str` (predefined
        # role) locator with a deployment override (see `resolve`'s guard),
        # so `generic.skills_folder`/`inline_skills` are always `None`/`{}`
        # here — carried through anyway so a future non-`str`-only caller of
        # `merge()` does not silently lose them.
        skills_folder=generic.skills_folder,
        inline_skills=dict(generic.inline_skills),
        importer_root=generic.importer_root,
    )
