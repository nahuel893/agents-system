"""Reading one definition from its three files, or from an inline or folder agent, into a ``RawDefinition``."""

from __future__ import annotations

import dataclasses
import pathlib
from collections.abc import Iterable, Mapping
from typing import Any

from agents_system.harness.definition.command_tools import _parse_command_tools
from agents_system.harness.definition.directives import (
    _check_escalation_descriptions,
    _descriptions_of,
)
from agents_system.harness.definition.errors import DefinitionError
from agents_system.harness.definition.frontmatter import (
    _as_str_list,
    _parse_escalation_descriptions,
    _read_md,
)
from agents_system.harness.definition.locate import (
    _contained_folder,
    _extends_override_target,
    _extends_target,
    _read_contained_md,
    _role_folder,
    _shown_path,
)
from agents_system.harness.definition.model import (
    FolderLocator,
    InlineLocator,
    RawDefinition,
    RoleLocator,
)
from agents_system.harness.definition.policy import _POLICY_FIELDS
from agents_system.harness.definition.prompt import _split_design_notes
from agents_system.harness.definition.roots import RootConfig, _require_platform_root


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
