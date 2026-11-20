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
import pathlib
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

import structlog

from agents_system.execution_limits import validate_execution_limits
from agents_system.harness.definition.command_tools import (
    _parse_command_tools,
    _revalidate_command_tools,
)
from agents_system.harness.definition.directives import (
    _check_escalation_descriptions,
    _check_mapping_directive,
    _descriptions_of,
    _resolve_list_directive,
    _resolve_mapping_directive,
    _resolve_permissions,
    _union_preserving_order,
)
from agents_system.harness.definition.errors import (
    DefinitionError,
    PredefinedRoleDeprecationWarning,
)
from agents_system.harness.definition.frontmatter import (
    _as_str_list,
    _parse_escalation_descriptions,
    _read_md,
)
from agents_system.harness.definition.locate import (
    _DEPRECATED_PREDEFINED_ROLES,
    _contained_folder,
    _deployment_folder,
    _extends_override_target,
    _extends_target,
    _locator_key,
    _locator_label,
    _read_contained_md,
    _role_folder,
    _shown_path,
    _warn_deprecated_role,
)
from agents_system.harness.definition.model import (
    AgentDefinition,
    CommandToolDeclaration,
    CommandToolParam,
    FolderLocator,
    InlineLocator,
    RawDefinition,
    RoleLocator,
)
from agents_system.harness.definition.model_config import (
    _resolve_model_declaration,
)
from agents_system.harness.definition.policy import (
    _DEFAULT_AUTONOMY,
    _DEPLOYMENT_RULE,
    _FOLD_RULE,
    _IMPORTER_RULE,
    _PLATFORM_DEFAULT_LIMITS,
    _POLICY_FIELDS,
    PLATFORM_DEFAULT_LIMITS,
)
from agents_system.harness.definition.prompt import (
    _PROMPT_SEPARATOR,
    _append_base_contract,
    _split_design_notes,
)
from agents_system.harness.definition.roots import (
    RootConfig,
    _require_deployments_root,
    _require_platform_root,
)
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

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Load functions
# ---------------------------------------------------------------------------


#: How deep a role may sit below its root. A taxonomy that needs more than
#: this has stopped being a taxonomy; the cap turns a runaway chain into a
#: named error instead of a slow walk.
_MAX_ROLE_CHAIN_DEPTH = 8


def _parse_untrusted_input(
    policy_fm: Mapping[str, Any], *, source: pathlib.Path | str
) -> bool | None:
    """Read `untrusted_input` from parsed policy.md frontmatter (ADR-002 C.11).

    Accepts only a real YAML bool (`true`/`false`) or plain absence (the key
    never written — `None`, meaning "not declared", inherit). YAML happily
    parses a quoted `"false"` as a string (truthy in Python — a typo would
    SILENTLY DISARM the exec:* invariant for whatever role declares it),
    `0`/`1` as int, and an explicit `null` as `None` — indistinguishable
    from the key being absent, which is exactly the ambiguity a security-
    relevant flag must not have written on disk. Each of those raises
    loudly, naming the offending file, instead of silently coercing.

    `Agent(...)` runs its `untrusted_input` parameter, and every
    `Agent.from_folder(...)` override, through this same function (issue
    #93); `source` then names the agent instead of a file.
    """
    if "untrusted_input" not in policy_fm:
        return None
    value = policy_fm["untrusted_input"]
    if value is None:
        raise DefinitionError(
            f"Invariant violation — untrusted_input: {source} declares "
            f"'untrusted_input: null', which is ambiguous with the key "
            f"being absent entirely. Omit it to inherit, or declare "
            f"an explicit true/false."
        )
    if not isinstance(value, bool):
        raise DefinitionError(
            f"Invariant violation — untrusted_input: {source} declares "
            f"untrusted_input={value!r} ({type(value).__name__}), which is "
            f"not a boolean. Only a real bool (YAML true/false, Python "
            f"True/False) is accepted — not a string, an int, or null."
        )
    return value


def _validate_inline_skills(
    agent: str, skills: Iterable[str], inline_skills: Mapping[str, Any]
) -> None:
    """Every ``skill_contents`` key must also be listed in ``skills``.

    ``_load_skills`` only looks up the skills a definition declares, so the
    content of an unlisted name would never be used, with no error anywhere.
    Shared by ``Agent.__post_init__`` (the inline form, and a
    ``from_folder`` call that also overrides ``skills``) and
    ``_apply_agent_folder_overrides`` (a ``from_folder`` call that keeps the
    folder's own ``skills``, known only once the folder is read).
    """
    unlisted = set(inline_skills) - set(skills)
    if unlisted:
        raise DefinitionError(
            f"Invariant violation — skill_contents: agent '{agent}': "
            f"skill_contents names a skill not listed in `skills`: "
            f"{sorted(unlisted)}. Declare it in `skills` too, or its content "
            f"is never used."
        )


#: Issue #75 — the only `FolderLocator.overrides` keys the loader applies,
#: each mapped to the `RawDefinition` field it replaces: exactly the fields
#: `Agent.from_folder` accepts (`agent.spec._AGENT_OVERRIDABLE_FIELDS`; a test
#: pins the two equal, since `agent.spec` imports this module and not the
#: other way round). `extends` maps to `None`: it replaces the parent
#: locator (`_extends_override_target`), not a field. Anything else a
#: `FolderLocator` carries — `command_tool_declarations`, `command_tools`,
#: `deployment`, `skills_folder`, `importer_root` — is rejected, not applied.
_FOLDER_OVERRIDE_FIELDS: Mapping[str, str | None] = {
    "name": "role_name",
    "version": "version",
    "system_prompt": "system_prompt",
    "extends": None,
    "tools": "tools",
    "skills": "skills",
    "skill_contents": "inline_skills",
    "context": "context",
    "permissions": "permissions",
    "autonomy": "autonomy",
    "escalation_rules": "escalation_rules",
    "delegation_policy": "delegation_policy",
    "memory_policy": "memory_policy",
    "audit_policy": "audit_policy",
    "execution_limits": "execution_limits",
    "untrusted_input": "untrusted_input",
    "model": "model",
}


def _apply_agent_folder_overrides(
    definition: RawDefinition, overrides: Mapping[str, Any]
) -> RawDefinition:
    """Apply `Agent.from_folder(path, **overrides)`'s Python parameters
    (design.md D3) onto a folder-read `RawDefinition`, field by field,
    REPLACING — never merging — the folder's own value: an explicitly passed
    field wins outright, and a field never passed keeps exactly what the
    folder declared.

    Only the keys in `_FOLDER_OVERRIDE_FIELDS` are applied, and any other
    key raises (issue #75): a `FolderLocator` can be built directly, without
    `Agent`'s own check, and used to set any `RawDefinition` field,
    command tool declarations included. Two keys are renamed onto a
    differently-named field: `name` -> `role_name` (so an overridden name is
    reflected consistently in both places), and `skill_contents` ->
    `inline_skills` (design.md D4, PR3 — the same carrier an
    `Agent(skill_contents=...)` populates via `_to_locator`'s
    `InlineLocator` branch, so `Agent.from_folder(path,
    skill_contents={...})` overrides a same-named folder skill the same
    way). `extends` is not a `RawDefinition` field: it replaces the
    manifest's `extends:` as the parent locator, in `_load_role_files`
    (`_extends_override_target`). `untrusted_input` is parsed like the
    policy file's own value.

    `Agent(skill_contents=...)`'s own `__post_init__` rejects a key not
    listed in `skills` at construction time -- but a `skill_contents`
    override lives only in `_folder_overrides` (never on the `Agent`
    instance's own `skills`/`skill_contents` fields, which stay at their
    empty defaults for a folder-backed `Agent`), so that guard never sees it.
    Restoring parity here, against the resulting `skills` (the folder's own
    declared skills, replaced by a `skills=` override in this same call, same
    as every other field this function applies) matters for more than a
    typo: `_fold_parent_into_child` unions `skills` ADDITIVELY across
    `extends`, so an unvalidated key can silently win over a same-named skill
    introduced only by a parent -- checked here, before that fold ever runs,
    so it is rejected regardless of what the chain later adds.
    """
    if not overrides:
        return definition
    who = f"agent '{definition.role_name}'"
    unknown = sorted(set(overrides) - set(_FOLDER_OVERRIDE_FIELDS))
    if unknown:
        raise DefinitionError(
            f"Invariant violation — overrides: {who} is given override(s) "
            f"{unknown}, which Agent.from_folder cannot override. Allowed: "
            f"{sorted(_FOLDER_OVERRIDE_FIELDS)}."
        )
    _parse_untrusted_input(overrides, source=who)
    changes: dict[str, Any] = {}
    for key, value in overrides.items():
        target = _FOLDER_OVERRIDE_FIELDS[key]
        if target is not None:
            changes[target] = dict(value) if target == "inline_skills" else value
    if "inline_skills" in changes:
        _validate_inline_skills(
            changes.get("role_name", definition.role_name),
            changes.get("skills", definition.skills),
            changes["inline_skills"],
        )
    return dataclasses.replace(definition, **changes) if changes else definition


def _load_role_files(
    locator: RoleLocator, roots: RootConfig
) -> tuple[RawDefinition, RoleLocator | None, bool]:
    """Read one role folder, or unwrap an inline definition. Returns its
    definition, its parent locator (``extends:`` placed by
    ``_extends_target``), and abstractness.

    Dispatches on ``locator``'s kind (design.md D1). An ``InlineLocator``
    returns its own ``raw`` unchanged, reading nothing for itself —
    ``is_abstract`` is always ``False`` for an inline definition
    (abstractness is a folder-manifest-only concept), and a string
    ``locator.parent`` is placed exactly like a manifest's ``extends:``. A
    ``FolderLocator`` reads ``role.md``/``manifest.md``/
    ``policy.md`` from ``locator.path``, using the identical parsing this
    function has always applied to a platform role folder. A bare ``str``
    resolves ``platform_root/roles/<name>``, byte-for-byte unchanged from
    before this change. This is the old body of ``load_generic``, with the
    two directives the frontmatter has always been allowed to carry now
    actually read.

    Issue #75: a ``FolderLocator``'s own ``path`` must resolve strictly
    inside its ``root``, and each of its three files is read inside that
    root through ``_read_contained_md``. Errors name files relative to their
    root (``vip-support/policy.md``, ``roles/<name>/policy.md``), never by
    host path.
    """
    if isinstance(locator, InlineLocator):
        inline_parent = locator.parent
        if isinstance(inline_parent, str):
            inline_parent = _extends_target(inline_parent, current=locator, roots=roots)
        return _plain_policies(locator.raw), inline_parent, False

    if isinstance(locator, FolderLocator):
        folder = _contained_folder(locator)
        role_type = folder.name
        shown = pathlib.Path(_shown_path(locator.root, folder))
        importer = locator

        def read(filename: str) -> tuple[dict[str, Any], str]:
            return _read_contained_md(importer, folder, filename, shown=shown)

    else:
        role_type = locator
        folder = _role_folder(_require_platform_root(roots.platform_root), role_type)
        shown = pathlib.Path("roles", role_type)
        if not folder.is_dir():
            raise DefinitionError(f"Role '{role_type}' has no folder at {shown}.")

        def read(filename: str) -> tuple[dict[str, Any], str]:
            return _read_md(folder / filename, shown=shown / filename)

    role_fm, role_body = read("role.md")
    role_body = _split_design_notes(role_body, source=shown / "role.md")
    manifest_fm, _ = read("manifest.md")
    policy_fm, policy_body = read("policy.md")

    role_name: str = str(role_fm.get("name", manifest_fm.get("role", role_type)))
    version: str = str(role_fm.get("version", manifest_fm.get("version", "1.0")))

    # Key ABSENT means no parent. A key present with no value (`extends:`,
    # `null`) is not the same thing: it reaches `_extends_target` and fails,
    # rather than silently dropping the inheritance its author meant to
    # declare. No shipped manifest writes one; a root role omits the key.
    # An `Agent.from_folder(path, extends=...)` override replaces the key.
    parent: RoleLocator | None
    if isinstance(locator, FolderLocator) and "extends" in locator.overrides:
        parent = _extends_override_target(locator, roots)
    elif "extends" in manifest_fm:
        parent = _extends_target(manifest_fm["extends"], current=locator, roots=roots)
    else:
        parent = None
    is_abstract = bool(manifest_fm.get("abstract", False))

    # ADR-002 C.12. Only a platform role's own manifest.md ORIGINATES command
    # tool declarations; the resulting dict is keyed by name so a deployment
    # override (which never originates one, see `load_override`) can narrow
    # `command_tools` by name without needing to restate argv/params/tier.
    command_tool_declarations = {
        decl.name: decl
        for decl in _parse_command_tools(manifest_fm, source=shown / "manifest.md")
    }

    # Issue #88 — `descriptions` (name -> prose) is populated two ways, and
    # both may be present at once: parsed from this file's own
    # `## escalation_rules` prose (`_parse_escalation_descriptions`, the
    # predefined-role convention), and/or declared directly in frontmatter
    # (`escalation_rules.descriptions:`) -- the shape an importer agent's
    # `FolderLocator` folder can use instead of hand-formatted prose. An
    # explicit frontmatter entry wins over a same-named parsed one, since it
    # is the more deliberate of the two.
    escalation_rules = dict(policy_fm.get("escalation_rules") or {})
    # Checked before the prose is merged in, which used to hide a declared
    # value that is not a mapping (review of #112).
    _check_escalation_descriptions(escalation_rules, who=str(shown / "policy.md"))
    parsed_descriptions = _parse_escalation_descriptions(
        policy_body,
        known_conditions=_as_str_list(escalation_rules.get("conditions")),
        source=shown / "policy.md",
    )
    if parsed_descriptions or "descriptions" in escalation_rules:
        escalation_rules = {
            **escalation_rules,
            "descriptions": {
                **parsed_descriptions,
                **_descriptions_of(escalation_rules),
            },
        }

    definition = RawDefinition(
        role_name=role_name,
        version=version,
        deployment=None,
        system_prompt=role_body,
        tools=_as_str_list(manifest_fm.get("tools")),
        skills=_as_str_list(manifest_fm.get("skills")),
        context=dict(manifest_fm.get("context") or {}),
        permissions=manifest_fm.get("permissions", []),
        # "" means NOT DECLARED, resolved by the fold below or defaulted at
        # the root. Applying "supervised" here erased the difference between
        # a role that chose it and one that said nothing -- so a silent
        # child LOOSENED a `confirm` parent, while `execution_limits`
        # inherited on omission. Same policy file, opposite behaviour.
        autonomy=str(policy_fm.get("autonomy", "")),
        escalation_rules=escalation_rules,
        delegation_policy=dict(policy_fm.get("delegation_policy") or {}),
        memory_policy=_declared_memory_policy(policy_fm),
        audit_policy=dict(policy_fm.get("audit_policy") or {}),
        execution_limits=policy_fm.get("execution_limits"),
        # D-037 (issue #13, ADR-004). Absent means NOT DECLARED (`None`): the
        # role inherits its parent's `model:` (`_fold_parent_into_child`, per
        # key, same shape as `execution_limits`) or, at the chain root, falls
        # back to the global `ADAPTER_PROVIDER`-driven default.
        model=policy_fm.get("model"),
        # ADR-002 C.11. Absent means NOT DECLARED (`None`), same reasoning as
        # `autonomy` above: a child role inherits the parent's value, so
        # defaulting here would erase the difference between "declared
        # false" and "said nothing", which is exactly the distinction the
        # monotonicity invariant needs. `_parse_untrusted_input` type-checks
        # whatever was actually written on disk.
        untrusted_input=_parse_untrusted_input(policy_fm, source=shown / "policy.md"),
        # ADR-002 C.12. Dict preserves manifest declaration order.
        command_tools=list(command_tool_declarations),
        command_tool_declarations=command_tool_declarations,
        # design.md D4 (PR3). Only a `FolderLocator` has an importer-owned
        # folder to resolve `skills/` from; a bare platform-role string
        # leaves this `None` (the default), so a platform role's own
        # `platform_root/roles/<name>/skills/`, if one happened to exist, is
        # never treated as a skills source (design.md's own requirement).
        skills_folder=folder / "skills" if isinstance(locator, FolderLocator) else None,
        # Issue #75. The root every skill read walks from (`_load_skills`).
        importer_root=locator.root if isinstance(locator, FolderLocator) else None,
    )
    if isinstance(locator, FolderLocator) and locator.overrides:
        definition = _plain_policies(
            _apply_agent_folder_overrides(definition, locator.overrides)
        )
    return definition, parent, is_abstract


def _plain_policies(definition: RawDefinition) -> RawDefinition:
    """``definition`` with each policy mapping and ``execution_limits`` a
    plain ``dict``, as the YAML path builds them (review of #112).

    A ``FolderLocator``'s overrides and an ``InlineLocator``'s ``raw`` can be
    built directly, without ``Agent``'s own conversion (#75), and a check
    that tested ``isinstance(..., dict)`` skipped any other mapping: a
    ``MappingProxyType`` ``escalation_rules`` reworded an inherited
    condition and redirected ``escalate_to`` past
    ``_apply_importer_ceiling``. Converted once here, where an importer's
    values enter the chain, no check can be skipped for the value's type.
    """
    changes: dict[str, Any] = {
        field: dict(value)
        for field in (*_POLICY_FIELDS, "execution_limits", "model")
        if isinstance(value := getattr(definition, field), Mapping)
        and not isinstance(value, dict)
    }
    return dataclasses.replace(definition, **changes) if changes else definition


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


def _resolve_role_chain(
    locator: RoleLocator,
    roots: RootConfig,
    *,
    limit_defaults: Mapping[str, Any] = _PLATFORM_DEFAULT_LIMITS,
) -> tuple[RawDefinition, bool]:
    """Walk ``extends:`` to the root and fold the chain back down.

    Returns the fully composed definition and whether the LEAF is abstract.
    Ancestors may be abstract — that is what abstract is for.

    Operates over ``RoleLocator`` values (design.md D1): membership in
    ``seen`` and the cycle message are keyed by ``_locator_key``, not a bare
    role-type string, so a folder/inline locator is compared by identity
    rather than accidentally colliding with a same-named platform role. The
    next hop is always ``_load_role_files``'s returned ``parent``, already
    placed by ``_extends_target`` (design.md D2). A parent that fails to
    load is reported with its underlying reason, never as merely missing.

    Every importer-authored definition in the chain is held to its parent's
    effective ``autonomy``/``execution_limits``/audit redaction, and has its
    escalation conditions added to its parent's, before it is folded -- and
    an importer-authored root, which has no parent, is held to the platform
    defaults. See ``_apply_importer_ceiling``. Only the importer end of a
    chain can be importer-authored: a platform role never ``extends:`` an
    importer folder, so platform-to-platform folds are never checked here.

    A concrete predefined role named by the caller -- the leaf itself, or the
    parent an importer-authored definition names -- emits a
    ``PredefinedRoleDeprecationWarning`` (ADR-010, #222). One the platform
    tree reaches on its own (``developer-agent`` -> ``operator-agent``) does
    not: the caller did not name it.
    """
    # (definition, written by the importer?) -- leaf first.
    chain: list[tuple[RawDefinition, bool]] = []
    seen: list[str] = []
    labels: list[str] = []  # `seen`, as error messages show it
    leaf_is_abstract = False

    original_key = _locator_key(locator)
    current_locator: RoleLocator | None = locator
    while current_locator is not None:
        key = _locator_key(current_locator)
        label = _locator_label(current_locator)
        if key in seen:
            cycle = " -> ".join([*labels, label])
            raise DefinitionError(
                f"Invariant violation — extends: role inheritance cycle: {cycle}"
            )
        seen.append(key)
        labels.append(label)

        if len(seen) > _MAX_ROLE_CHAIN_DEPTH:
            raise DefinitionError(
                f"Invariant violation — extends: role chain deeper than "
                f"{_MAX_ROLE_CHAIN_DEPTH}: {' -> '.join(labels)}"
            )

        try:
            definition, parent, is_abstract = _load_role_files(current_locator, roots)
        except DefinitionError as exc:
            if key == original_key:
                raise
            raise DefinitionError(
                f"Invariant violation — extends: role '{labels[-2]}' extends "
                f"'{label}', which could not be loaded: {exc}"
            ) from exc

        if key == original_key:
            leaf_is_abstract = is_abstract
        named_by_caller = key == original_key or (bool(chain) and chain[-1][1])
        if (
            named_by_caller
            and isinstance(current_locator, str)
            and current_locator in _DEPRECATED_PREDEFINED_ROLES
        ):
            _warn_deprecated_role(current_locator)
        importer_authored = isinstance(current_locator, FolderLocator | InlineLocator)
        chain.append((definition, importer_authored))
        current_locator = parent

    # chain is leaf-first; fold root-first so a child composes onto its parent.
    resolved, root_is_importer_authored = chain[-1]
    resolved = _resolve_chain_root(resolved)
    if root_is_importer_authored:
        resolved = _apply_importer_ceiling(
            None, resolved, limit_defaults=limit_defaults
        )
    for child, child_is_importer_authored in reversed(chain[:-1]):
        if child_is_importer_authored:
            child = _apply_importer_ceiling(
                resolved, child, limit_defaults=limit_defaults
            )
        resolved = _fold_parent_into_child(resolved, child)

    return resolved, leaf_is_abstract


def _declared_memory_policy(policy_fm: Mapping[str, Any]) -> dict[str, Any]:
    """The `memory_policy` a `policy.md` declares: absent or null is ``{}``
    (the schema defaults), but an explicit non-mapping (``[]``, ``false``) is
    an error, not silently the permissive default (#26)."""
    value = policy_fm.get("memory_policy")
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise DefinitionError(
            f"memory_policy in policy.md must be a mapping, got {type(value).__name__}"
        )
    return dict(value)


def load_generic(
    locator: RoleLocator,
    *,
    roots: RootConfig | None = None,
    limit_defaults: Mapping[str, Any] = _PLATFORM_DEFAULT_LIMITS,
) -> RawDefinition:
    """Resolve a role locator (design.md D1) and everything it extends.

    Returns the FULLY COMPOSED role — the union of its whole ``extends:``
    chain. That placement matters: ``resolve`` hands this straight to
    ``merge``, whose ``_validate_tools`` enforces that a deployment may only
    narrow. Because the chain is already folded here, that existing
    subtractive check now runs against the entire inherited surface without
    ``_merge_validated`` changing at all.

    Parameters
    ----------
    locator:
        A predefined-role name (``str``, e.g. ``"sales-agent"``), an
        importer-supplied folder (``FolderLocator``), or an already-built
        definition (``InlineLocator``).
    roots:
        Injectable path config.  Defaults to the real repo roots.
    limit_defaults:
        What an undeclared execution limit resolves to when an
        importer-authored definition is checked (#278, ``_limit_defaults``).
    """
    if roots is None:
        roots = RootConfig()

    resolved, is_abstract = _resolve_role_chain(
        locator, roots, limit_defaults=limit_defaults
    )

    if not resolved.autonomy:
        # Nothing in the chain declared one. `supervised` is the platform
        # floor, applied once here rather than at every load.
        resolved = dataclasses.replace(resolved, autonomy=_DEFAULT_AUTONOMY)

    if resolved.untrusted_input is None:
        # ADR-002 C.11 — explicit decision, not a neutral default.
        #
        # Nothing in the chain declared it, so this falls back to `False`.
        # For most bools in this file `False` is the uncontroversially safe
        # default (a narrower grant). For THIS flag it is the opposite:
        # `False` disarms `_validate_untrusted_input_exec` below, so a role
        # that actually needs `true` but whose author forgot to declare it
        # would silently resolve as trusted — fail-OPEN for the exact
        # invariant this ADR item exists to add.
        #
        # It is accepted here anyway, for one reason only: every concrete
        # platform role's chain passes through `agent` (or `base`), and
        # both now declare `untrusted_input` explicitly (see
        # `platform/roles/agent/policy.md`, `platform/roles/base/policy.md`)
        # — so this branch is dead code for the real, shipped role surface.
        # `tests/test_untrusted_input_invariant.py::
        # test_every_concrete_platform_role_explicitly_declares_untrusted_input`
        # makes that a checked guarantee: it fails the moment a new concrete
        # role lands without extending agent/base and without declaring its
        # own value, forcing a deliberate choice instead of a silent one.
        #
        # A blanket "fail at load for ANY concrete role missing the
        # declaration" was considered and rejected as this issue's fix:
        # dozens of pre-existing, unrelated test fixtures across the suite
        # (e.g. every `fx-*` role in `tests/fixtures/agents/hierarchy/`,
        # `simple-role`) declare no `untrusted_input` and would all need
        # touching — out of proportion to ADR-002 C.11's scope.
        resolved = dataclasses.replace(resolved, untrusted_input=False)

    if is_abstract:
        raise DefinitionError(
            f"Role '{resolved.role_name}' is declared abstract and cannot "
            f"be built directly. Extend it from a concrete role instead."
        )

    return resolved


def load_override(
    client: str,
    role_type: str,
    *,
    roots: RootConfig | None = None,
) -> RawDefinition | None:
    """Read deployments/{client}/{role_type}/…; returns None if folder absent."""
    if roots is None:
        roots = RootConfig()

    folder = _deployment_folder(
        _require_deployments_root(roots.deployments_root), client, role_type
    )

    if not folder.exists():
        # Not an error — a role may legitimately have no override. But it is
        # not neutral either: `resolve` falls back to the generic role, and
        # since a deployment may only NARROW the platform role, that fallback
        # WIDENS the tool surface to the role's full allowance. A typo in the
        # client name therefore grants more, not less. Make it observable.
        logger.warning(
            "loader.override_not_found",
            client=client,
            role_type=role_type,
            path=str(folder),
        )
        return None

    # Errors name files relative to the deployments root (issue #75).
    shown = pathlib.Path(client, role_type)
    role_fm, role_body = _read_md(folder / "role.md", shown=shown / "role.md")
    role_body = _split_design_notes(role_body, source=shown / "role.md")
    manifest_fm, _ = _read_md(folder / "manifest.md", shown=shown / "manifest.md")
    policy_fm, _ = _read_md(folder / "policy.md", shown=shown / "policy.md")

    # Use parent role_type as the role_name fallback
    role_name = str(role_fm.get("name", manifest_fm.get("role", role_type)))
    version = str(role_fm.get("version", manifest_fm.get("version", "1.0")))
    deployment = str(manifest_fm.get("deployment", client))

    # A deployment's parent is fixed by its directory -- `deployments/{client}/
    # {role_type}/` overrides `platform/roles/{role_type}/`. So `extends:` here
    # cannot CHOOSE anything, and for a long time nothing read it at all. That
    # left the same key authoritative in a role manifest and silently inert in
    # a deployment one, which is worse than uniformly ignored: a contradiction
    # reads as a decision and does nothing.
    #
    # It is now checked. Declaring the truth is allowed; declaring a lie is not.
    declared_parent = manifest_fm.get("extends")
    if declared_parent is not None:
        # Only CHECKED, never resolved -- the folder already fixes the
        # parent -- so comparing the last segment cannot select a wrong one,
        # and existing deployment manifests write `roles/<name>`, a form
        # `_extends_target` deliberately rejects.
        target = str(declared_parent).strip().rstrip("/").rsplit("/", 1)[-1]
        if target != role_type:
            raise DefinitionError(
                f"Invariant violation — extends: deployment "
                f"'{client}/{role_type}' declares 'extends: {declared_parent}', "
                f"but a deployment override always extends the platform role "
                f"its own folder names ('{role_type}'). A deployment cannot "
                f"choose a different parent; remove the line or correct it."
            )

    return RawDefinition(
        role_name=role_name,
        version=version,
        deployment=deployment,
        system_prompt=role_body,
        tools=_as_str_list(manifest_fm.get("tools")),
        skills=_as_str_list(manifest_fm.get("skills")),
        context=dict(manifest_fm.get("context") or {}),
        permissions=manifest_fm.get("permissions", []),
        # "" means NOT DECLARED here too -- see `_load_role_files`. A
        # deployment that says nothing must keep the role's level; resetting
        # it to the platform floor LOOSENS a `confirm` role.
        autonomy=str(policy_fm.get("autonomy", "")),
        escalation_rules=dict(policy_fm.get("escalation_rules") or {}),
        delegation_policy=dict(policy_fm.get("delegation_policy") or {}),
        memory_policy=_declared_memory_policy(policy_fm),
        audit_policy=dict(policy_fm.get("audit_policy") or {}),
        execution_limits=policy_fm.get("execution_limits"),
        # D-037 (issue #13). `None` means the deployment says nothing and
        # inherits the resolved role's `model:` -- see `_merge_validated`. A
        # declared mapping REPLACES the role's wholesale (a deployment
        # operator picking a model is a deliberate, complete choice, not a
        # partial tweak -- unlike the role-to-role fold in
        # `_fold_parent_into_child`, which merges per key).
        model=policy_fm.get("model"),
        # ADR-002 C.11. `None` means the override says nothing and inherits
        # the resolved role's value — see `_validate_untrusted_input_monotonic`.
        # `_parse_untrusted_input` type-checks whatever was actually written.
        untrusted_input=_parse_untrusted_input(policy_fm, source=shown / "policy.md"),
        # ADR-002 C.12. NAMES only, same shape as `tools` above — a
        # deployment never originates a full declaration (`command_tool_
        # declarations` stays empty), it only narrows the role's set by
        # name. `_validate_command_tools` enforces the subset below.
        command_tools=_as_str_list(manifest_fm.get("command_tools")),
    )


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


# ---------------------------------------------------------------------------
# resolve — public entry point
# ---------------------------------------------------------------------------


def _limit_defaults(deployment_limits: Mapping[str, Any]) -> Mapping[str, Any]:
    """#278 -- what an undeclared limit is held to under a deployment ceiling.

    A deployment value above the platform default replaces it, so the agent
    may declare anything up to it. One below leaves the platform default as
    the load-time ceiling: the runtime applies the deployment's value
    (``graph._effective_limits``), so a declaration above it is capped, not
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
