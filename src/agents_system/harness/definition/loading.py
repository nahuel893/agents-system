"""Loading: the generic definition of a role, walked through its ``extends:`` chain, and the override a deployment adds to it."""

from __future__ import annotations

import dataclasses
import pathlib
from collections.abc import Mapping
from typing import Any

import structlog

from agents_system.harness.definition.errors import DefinitionError
from agents_system.harness.definition.files import (
    _declared_memory_policy,
    _load_role_files,
    parse_untrusted_input,
)
from agents_system.harness.definition.fold import (
    _apply_importer_ceiling,
    _fold_parent_into_child,
    _resolve_chain_root,
)
from agents_system.harness.definition.frontmatter import _read_md, as_str_list
from agents_system.harness.definition.locate import (
    _DEPRECATED_PREDEFINED_ROLES,
    _deployment_folder,
    _locator_key,
    _locator_label,
    _warn_deprecated_role,
)
from agents_system.harness.definition.model import (
    FolderLocator,
    InlineLocator,
    RawDefinition,
    RoleLocator,
)
from agents_system.harness.definition.policy import (
    _DEFAULT_AUTONOMY,
    _PLATFORM_DEFAULT_LIMITS,
)
from agents_system.harness.definition.prompt import _split_design_notes
from agents_system.harness.definition.roots import RootConfig, require_deployments_root

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Load functions
# ---------------------------------------------------------------------------


#: How deep a role may sit below its root. A taxonomy that needs more than
#: this has stopped being a taxonomy; the cap turns a runaway chain into a
#: named error instead of a slow walk.
_MAX_ROLE_CHAIN_DEPTH = 8


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
        require_deployments_root(roots.deployments_root), client, role_type
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
        tools=as_str_list(manifest_fm.get("tools")),
        skills=as_str_list(manifest_fm.get("skills")),
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
        # `parse_untrusted_input` type-checks whatever was actually written.
        untrusted_input=parse_untrusted_input(policy_fm, source=shown / "policy.md"),
        # ADR-002 C.12. NAMES only, same shape as `tools` above — a
        # deployment never originates a full declaration (`command_tool_
        # declarations` stays empty), it only narrows the role's set by
        # name. `_validate_command_tools` enforces the subset below.
        command_tools=as_str_list(manifest_fm.get("command_tools")),
    )
