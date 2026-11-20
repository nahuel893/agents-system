"""Folding an ``extends:`` chain: a parent into its child, the root of a chain, and the ceiling an importer agent inherits."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any

from agents_system.harness.definition.directives import (
    _check_mapping_directive,
    _resolve_list_directive,
    _resolve_mapping_directive,
    _union_preserving_order,
)
from agents_system.harness.definition.errors import DefinitionError
from agents_system.harness.definition.frontmatter import _as_str_list
from agents_system.harness.definition.model import RawDefinition
from agents_system.harness.definition.policy import (
    _DEFAULT_AUTONOMY,
    _FOLD_RULE,
    _IMPORTER_RULE,
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
    _validate_execution_limits,
    _validate_untrusted_input_monotonic,
)


def _fold_parent_into_child(
    parent: RawDefinition, child: RawDefinition
) -> RawDefinition:
    """Compose a parent role into its child. ADDITIVE for capability.

    Role-to-role inheritance widens: a child adds tools and permissions to
    what its parent already grants. Both sides are authored by the platform,
    so no trust boundary is crossed — unlike deployment-to-role, which stays
    subtractive and is enforced later by ``_merge_validated``.

    ``autonomy`` and ``execution_limits`` are the child's to declare, in
    either direction. An earlier version of this function enforced them as
    ceilings here, reusing the deployment-path validators. That was wrong,
    and building the taxonomy surfaced it immediately: ``data-agent`` runs
    ``autonomy: full`` and could not descend from a ``supervised`` base.

    The subtractive rule exists because a deployment is authored by someone
    else. Applying it between two roles imports a trust boundary that is not
    there -- the same person writes both files, so a child declaring ``full``
    is a design decision, not an escalation, and blocking it buys no safety
    while making the hierarchy unusable for any role that legitimately runs
    unsupervised.

    The ceiling that matters is unchanged: ``_merge_validated`` still refuses
    a deployment that elevates either field, now measured against the fully
    resolved chain.

    That "same author" reasoning holds only while both sides are platform
    roles. A child written by the importer -- an ``Agent(...)``, an
    ``Agent.from_folder(...)``, any ``FolderLocator``/``InlineLocator`` --
    crosses the same kind of trust boundary a deployment does, so
    ``_resolve_role_chain`` checks it with ``_apply_importer_ceiling``
    before this fold runs. Nothing here changes for a platform-role child.

    ``untrusted_input`` (ADR-002 C.11) is the one field where that "no trust
    boundary between two platform roles" reasoning does NOT apply, and is
    validated here: it is not a policy choice like ``autonomy``, it is a
    fact about where untrusted input can reach, so a child declaring
    ``false`` under a ``true`` parent is rejected exactly like a deployment
    override would be — see ``_validate_untrusted_input_monotonic``.
    """

    # An undeclared child keeps its parent's level. Defaulting at load time
    # erased the difference between choosing `supervised` and saying
    # nothing, so a silent child LOOSENED a `confirm` parent -- while
    # `execution_limits` inherited on omission. Same file, opposite rule.
    resolved_autonomy = child.autonomy or parent.autonomy

    # ADR-002 C.11. Unlike autonomy, a DECLARED reversal here is rejected
    # even between two platform roles: `untrusted_input` is not a policy
    # choice a role's own author may loosen at will, it is a fact about
    # where untrusted input can reach, and a role `extends:`-ing a
    # `true` parent could otherwise declare `false` and add `exec:*` —
    # the exact lethal-trifecta gap this flag exists to close — with the
    # deployment-override check (`_merge_validated`) never seeing a
    # conflict, because by then the chain has already resolved to `false`.
    _validate_untrusted_input_monotonic(parent, child)
    resolved_untrusted_input = (
        child.untrusted_input
        if child.untrusted_input is not None
        else parent.untrusted_input
    )

    who = f"role '{child.role_name}'"
    against = f"parent '{parent.role_name}'"
    parent_perms = (
        list(parent.permissions) if isinstance(parent.permissions, list) else []
    )
    child_perms = child.permissions
    if isinstance(child_perms, str) and child_perms != "inherit":
        # A bare string is a one-item list, as `_as_str_list` reads `tools:`.
        # It used to resolve to the parent's set, dropping the grant.
        child_perms = [child_perms]
    if isinstance(child_perms, list | tuple):
        # A plain list ADDS here, unlike at the deployment edge where it
        # replaces: role-to-role composition is additive for capability.
        resolved_perms: list[str] = _union_preserving_order(
            parent_perms, list(child_perms)
        )
    else:
        # Everything else — "inherit", {inherit: true, add/remove: [...]},
        # {override: [...]} — goes through the shared resolver, which is the
        # only place those shapes are implemented.
        #
        # `list(child.permissions)` used to run on ALL non-list shapes, so a
        # dict yielded its KEYS: a child using the documented removal
        # directive received the literal strings 'inherit' and 'remove' as
        # permissions AND kept the one it asked to remove. Permissions gate
        # sensitive tool calls, so that failed in the granting direction
        # twice over. The fix that closed this at the deployment edge left it
        # live one layer up.
        #
        # An unrecognised directive raises (issue #106) instead of keeping
        # the parent's full set.
        resolved_perms = _resolve_list_directive(
            parent_perms, child_perms, who=who, field="permissions"
        )

    parent_limits = (
        dict(parent.execution_limits)
        if isinstance(parent.execution_limits, dict)
        else None
    )
    child_limits = child.execution_limits
    if isinstance(child_limits, dict):
        # MERGED, for the same reason as at the deployment edge: substituting
        # dropped every ceiling the parent set and the child did not restate,
        # and `_effective_limits` then backfilled those from the looser
        # PLATFORM defaults. A child tightening one limit quietly loosened the
        # rest -- the identical defect, still live one layer up.
        resolved_limits: dict[str, Any] | str | None = {
            **(parent_limits or {}),
            **child_limits,
        }
    else:
        resolved_limits = parent_limits

    # D-037 (issue #13, ADR-004) -- `model` folds exactly like
    # `execution_limits` above: per-key merge, so a child role may override
    # just `temperature` while inheriting `provider`/`model` from its
    # parent. No ceiling either: model/provider selection is not a
    # capability or safety escalation between two platform roles, same
    # "same author writes both files" reasoning `execution_limits`/
    # `autonomy` document above.
    parent_model = dict(parent.model) if isinstance(parent.model, dict) else None
    child_model = child.model
    if isinstance(child_model, dict):
        resolved_model: dict[str, Any] | str | None = {
            **(parent_model or {}),
            **child_model,
        }
    elif child_model is None or child_model == "inherit":
        resolved_model = parent_model
    else:
        # Issue #146 review: a non-mapping, non-None, non-'inherit' value
        # (a bare scalar -- the same authoring typo `_resolve_model_
        # declaration` already rejects at every OTHER boundary: the
        # no-override path in `resolve()` and a deployment override in
        # `merge()`) used to fall through to the branch above and silently
        # resolve to the PARENT's model, discarding the child's malformed
        # declaration with no error at all. A per-key merge cannot simply
        # run the child value through `_resolve_model_declaration` here --
        # a legitimate partial override (e.g. `model: {temperature: 0.7}`
        # alone, inheriting `provider`/`model` from the parent) would fail
        # ITS OWN completeness check -- so only the shape (mapping / None /
        # 'inherit') is rejected here; full-shape validation still runs on
        # the fully-resolved value in `resolve()`/`merge()`.
        raise DefinitionError(
            f"Invariant violation — model: {who} declares a malformed "
            f"model: value {child_model!r}; expected a mapping with "
            "'provider'/'model' keys (or omit the key, or set it to "
            "'inherit', to inherit)."
        )

    def _merge_mapping(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
        merged = dict(a)
        merged.update(b)
        return merged

    def _fold_policy(field: str) -> dict[str, Any]:
        """The child's policy mapping, key by key over the parent's: what
        ``{inherit: true, ...}`` means at the deployment edge, where a plain
        mapping replaces instead. So one resolver serves both, and a child's
        own ``{inherit: true, add: [...]}`` is resolved rather than stored as
        literal ``inherit``/``add`` keys (issue #106).

        A child ``conditions:`` list still replaces the parent's, which is
        why a predefined role restates its whole inherited list (see
        ``agent/policy.md``). ``descriptions`` accumulates instead (issue
        #88): it is prose, written once at the role that introduces a
        condition, and every descendant inherits it without repeating it.
        """
        child_value = getattr(child, field)
        _check_mapping_directive(child_value, who=who, field=field)
        if isinstance(child_value, Mapping):
            child_value = {**child_value, "inherit": True}
        return _resolve_mapping_directive(
            getattr(parent, field), child_value, who=who, field=field
        )

    resolved_escalation = _fold_policy("escalation_rules")
    resolved_audit = _fold_policy("audit_policy")
    # Issue #106: the one safety rule that holds on EVERY fold, a predefined
    # child role's included -- unlike autonomy and limits, which the same
    # author may set either way. An importer child's `conditions` were
    # already added to its parent's (`_apply_importer_ceiling`), so only a
    # `remove:` directive can still drop one there.
    _validate_escalation_kept(
        parent.escalation_rules,
        resolved_escalation,
        who=who,
        against=against,
        rule=_FOLD_RULE,
    )
    _validate_audit_redaction(
        parent.audit_policy, resolved_audit, who=who, against=against, rule=_FOLD_RULE
    )

    # ADR-002 C.12. Additive, like `tools`/`permissions` above -- but unlike
    # those plain string lists, a NAME collision between two platform roles
    # is a real ambiguity (which entry's argv/params/tier wins?), so it is
    # rejected rather than silently deduplicated.
    merged_declarations = dict(parent.command_tool_declarations)
    for name, declaration in child.command_tool_declarations.items():
        if name in merged_declarations:
            raise DefinitionError(
                f"Invariant violation — command_tools: role "
                f"'{child.role_name}' redeclares command tool '{name}', "
                f"already declared by an ancestor role. A descendant may "
                f"not redeclare a command tool; give it a different name."
            )
        merged_declarations[name] = declaration

    prompts = [p for p in (parent.system_prompt, child.system_prompt) if p.strip()]

    return RawDefinition(
        # The leaf is the role being resolved, so it owns its identity.
        role_name=child.role_name,
        version=child.version,
        deployment=None,
        system_prompt=_PROMPT_SEPARATOR.join(prompts),
        tools=_union_preserving_order(parent.tools, child.tools),
        skills=_union_preserving_order(parent.skills, child.skills),
        context=_merge_mapping(parent.context, child.context),
        permissions=resolved_perms,
        autonomy=resolved_autonomy,
        escalation_rules=resolved_escalation,
        delegation_policy=_fold_policy("delegation_policy"),
        memory_policy=_fold_policy("memory_policy"),
        audit_policy=resolved_audit,
        execution_limits=resolved_limits,
        model=resolved_model,
        untrusted_input=resolved_untrusted_input,
        command_tools=_union_preserving_order(
            parent.command_tools, child.command_tools
        ),
        command_tool_declarations=merged_declarations,
        # design.md D4 (PR3). NOT additive like `tools`/`skills` above: these
        # two are "this locator's own folder/inline source", not a
        # capability to inherit down the chain — the CHILD's own value
        # always wins (a platform-role ancestor's is always `None`/`{}`
        # anyway, since only a `FolderLocator`/`InlineLocator` ever sets
        # them). `_resolve_role_chain` folds root-first, so after the last
        # fold `child` is the original leaf, and its own value is what
        # survives.
        skills_folder=child.skills_folder,
        inline_skills=dict(child.inline_skills),
        importer_root=child.importer_root,
    )


def _resolve_chain_root(root: RawDefinition) -> RawDefinition:
    """``root``'s own directives, resolved against an empty parent (issue
    #106).

    A chain root has nothing to fold onto, so its directives were never
    resolved or checked: ``resolve`` reads any ``permissions`` that is not a
    list as none at all (``{override: [read:catalog]}`` granted nothing), an
    unrecognised directive passed silently, and ``{inherit: true, add:
    [...]}`` in a policy mapping stayed as literal keys.
    """
    who = f"role '{root.role_name}'"
    changes: dict[str, Any] = {
        field: _resolve_mapping_directive(
            {}, getattr(root, field), who=who, field=field
        )
        for field in _POLICY_FIELDS
    }
    changes["permissions"] = _resolve_list_directive(
        [], root.permissions, who=who, field="permissions"
    )
    return dataclasses.replace(root, **changes)


def _apply_importer_ceiling(
    parent: RawDefinition | None,
    child: RawDefinition,
    *,
    limit_defaults: Mapping[str, Any] = _PLATFORM_DEFAULT_LIMITS,
) -> RawDefinition:
    """Hold an importer-authored ``child`` to ``parent``'s effective safety
    settings: additive for capability, subtractive for safety
    (``platform/roles/base/policy.md``). Returns ``child``, ready to fold.

    ``_fold_parent_into_child`` lets a platform role loosen ``autonomy`` or
    ``execution_limits`` because the same author writes both files. An
    importer-authored definition (``FolderLocator``/``InlineLocator``, which
    is what ``Agent._to_locator()`` produces) is written by someone else, so
    it may match or tighten its parent's values, never exceed them. This
    reuses the deployment validators, measured against the parent's
    EFFECTIVE values: an undeclared autonomy is the platform floor, and a
    missing or null limit is the platform default -- exactly what the
    runtime would enforce for the parent. The same holds for audit redaction
    (``_validate_audit_redaction``).

    Escalation conditions are the one field made additive rather than
    checked (issue #93): a platform child restates its parent's whole
    ``conditions`` list, but an importer child's list is ADDED to its
    parent's here, so ``conditions: []`` cannot switch off escalation the
    parent requires. The returned ``child`` carries the union. Nor may it
    reword an inherited condition or change ``escalate_to`` (issue #106,
    ``_validate_escalation_wording``).

    ``parent`` is ``None`` for an importer-authored chain root. Having no
    parent does not mean having no ceiling: the platform defaults are what
    such a definition would otherwise run with, so they are its ceiling.
    Otherwise leaving out ``extends`` would be the way around this check.

    ``limit_defaults`` is what an undeclared limit resolves to: the platform
    defaults, or, under a deployment ceiling (#278), ``_limit_defaults``.

    Errors name agents by ``role_name`` -- never a locator or its host path.
    """
    who = f"agent '{child.role_name}'"
    if parent is None:
        source = (
            "platform default"
            if limit_defaults is _PLATFORM_DEFAULT_LIMITS
            else "deployment ceiling"
        )
        ceiling_autonomy = _DEFAULT_AUTONOMY
        ceiling_limits: Mapping[str, Any] = {}
        ceiling_audit: Mapping[str, Any] = {}
    else:
        source = f"parent '{parent.role_name}' effective"
        ceiling_autonomy = parent.autonomy or _DEFAULT_AUTONOMY
        ceiling_limits = (
            parent.execution_limits if isinstance(parent.execution_limits, dict) else {}
        )
        ceiling_audit = parent.audit_policy

    # What the fold will resolve to: a key the child leaves out keeps the
    # parent's value.
    _validate_audit_redaction(
        ceiling_audit,
        {**ceiling_audit, **child.audit_policy},
        who=who,
        against=source,
    )
    _validate_autonomy(
        ceiling_autonomy,
        child.autonomy,
        who=who,
        against=f"{source} autonomy",
        rule=_IMPORTER_RULE,
    )
    # Only a dict is folded over the parent's limits; any other shape
    # inherits them unchanged (`_fold_parent_into_child`), so it cannot raise
    # anything.
    if isinstance(child.execution_limits, dict):
        _validate_execution_limits(
            ceiling_limits,
            child.execution_limits,
            who=who,
            against=source,
            rule=_IMPORTER_RULE,
            defaults=limit_defaults,
        )

    rules = child.escalation_rules
    if parent is None or not isinstance(rules, Mapping):
        return child
    # What the fold will resolve to: it always inherits, so an `escalate_to`
    # the child leaves out keeps the parent's.
    _validate_escalation_wording(
        parent.escalation_rules,
        rules,
        {**parent.escalation_rules, **rules},
        who=who,
        against=source,
        rule=_IMPORTER_RULE,
    )
    if "conditions" not in rules:
        # An undeclared `conditions` already keeps the parent's in the fold.
        return child
    conditions = _union_preserving_order(
        _as_str_list(parent.escalation_rules.get("conditions")),
        _as_str_list(rules["conditions"]),
    )
    return dataclasses.replace(
        child, escalation_rules={**rules, "conditions": conditions}
    )
