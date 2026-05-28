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
"""
from __future__ import annotations

import dataclasses
import pathlib
from typing import Any, Mapping

import yaml

# ---------------------------------------------------------------------------
# Repo-level default roots (overridable via RootConfig)
# ---------------------------------------------------------------------------
_REPO_ROOT = pathlib.Path(__file__).parent.parent.parent.parent
_DEFAULT_PLATFORM_ROOT = _REPO_ROOT / "platform"
_DEFAULT_DEPLOYMENTS_ROOT = _REPO_ROOT / "deployments"

# ---------------------------------------------------------------------------
# Autonomy rank — lower rank is more restrictive (safer)
# ---------------------------------------------------------------------------
_AUTONOMY_RANK: dict[str, int] = {
    "confirm": 0,
    "supervised": 1,
    "full": 2,
}


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class RootConfig:
    """Injectable path roots so tests can point at fixtures."""

    platform_root: pathlib.Path = dataclasses.field(
        default_factory=lambda: _DEFAULT_PLATFORM_ROOT
    )
    deployments_root: pathlib.Path = dataclasses.field(
        default_factory=lambda: _DEFAULT_DEPLOYMENTS_ROOT
    )


class DefinitionError(Exception):
    """Raised when an agent definition violates a structural invariant."""


@dataclasses.dataclass(frozen=True)
class AgentDefinition:
    """Fully resolved, immutable agent definition.

    This is the value object returned by ``resolve``.  It is NOT a live
    runtime — it is a validated snapshot of what the agent is allowed to be
    and do.
    """

    role_name: str
    version: str
    deployment: str | None
    system_prompt: str
    tools: tuple[str, ...]
    skills: tuple[str, ...]
    context: Mapping[str, Any]
    permissions: tuple[str, ...]
    autonomy: str
    escalation_rules: Mapping[str, Any]
    delegation_policy: Mapping[str, Any]
    memory_policy: Mapping[str, Any]
    audit_policy: Mapping[str, Any]
    execution_limits: Mapping[str, Any] | None


@dataclasses.dataclass
class RawDefinition:
    """Intermediate representation: parsed frontmatter + prose body."""

    role_name: str
    version: str
    deployment: str | None
    system_prompt: str  # prose body of role.md
    # manifest fields
    tools: list[str]
    skills: list[str]
    context: dict[str, Any]
    permissions: list[str] | str  # may be "inherit" before merge
    # policy fields
    autonomy: str
    escalation_rules: dict[str, Any]
    delegation_policy: dict[str, Any]
    memory_policy: dict[str, Any]
    audit_policy: dict[str, Any]
    execution_limits: dict[str, Any] | str | None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split a markdown file into (frontmatter_dict, body).

    The frontmatter is the YAML block delimited by leading ``---`` markers.
    If no frontmatter is present, returns an empty dict and the full text.
    """
    stripped = text.lstrip()
    if not stripped.startswith("---"):
        return {}, text

    # Find the closing ---
    lines = stripped.split("\n")
    end_idx: int | None = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_idx = i
            break

    if end_idx is None:
        return {}, text

    yaml_block = "\n".join(lines[1:end_idx])
    body = "\n".join(lines[end_idx + 1 :]).lstrip("\n")
    parsed = yaml.safe_load(yaml_block) or {}
    return parsed, body


def _read_md(path: pathlib.Path) -> tuple[dict[str, Any], str]:
    """Read a markdown file and return (frontmatter, body)."""
    if not path.exists():
        raise DefinitionError(f"Required definition file not found: {path}")
    return _split_frontmatter(path.read_text(encoding="utf-8"))


def _as_str_list(value: Any) -> list[str]:
    """Coerce a YAML value to a list of strings."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        return [value]
    return [str(value)]


def _role_folder(platform_root: pathlib.Path, role_type: str) -> pathlib.Path:
    return platform_root / "roles" / role_type


def _deployment_folder(
    deployments_root: pathlib.Path, client: str, role_type: str
) -> pathlib.Path:
    return deployments_root / client / role_type


# ---------------------------------------------------------------------------
# Load functions
# ---------------------------------------------------------------------------


def load_generic(role_type: str, *, roots: RootConfig | None = None) -> RawDefinition:
    """Read platform/roles/{role_type}/{role,manifest,policy}.md.

    Parameters
    ----------
    role_type:
        The role folder name (e.g. ``"sales-agent"``).
    roots:
        Injectable path config.  Defaults to the real repo roots.
    """
    if roots is None:
        roots = RootConfig()

    folder = _role_folder(roots.platform_root, role_type)

    role_fm, role_body = _read_md(folder / "role.md")
    manifest_fm, _ = _read_md(folder / "manifest.md")
    policy_fm, _ = _read_md(folder / "policy.md")

    role_name: str = str(
        role_fm.get("name", manifest_fm.get("role", role_type))
    )
    version: str = str(role_fm.get("version", manifest_fm.get("version", "1.0")))

    return RawDefinition(
        role_name=role_name,
        version=version,
        deployment=None,
        system_prompt=role_body,
        tools=_as_str_list(manifest_fm.get("tools")),
        skills=_as_str_list(manifest_fm.get("skills")),
        context=dict(manifest_fm.get("context") or {}),
        permissions=manifest_fm.get("permissions", []),
        autonomy=str(policy_fm.get("autonomy", "supervised")),
        escalation_rules=dict(policy_fm.get("escalation_rules") or {}),
        delegation_policy=dict(policy_fm.get("delegation_policy") or {}),
        memory_policy=dict(policy_fm.get("memory_policy") or {}),
        audit_policy=dict(policy_fm.get("audit_policy") or {}),
        execution_limits=policy_fm.get("execution_limits"),
    )


def load_override(
    client: str,
    role_type: str,
    *,
    roots: RootConfig | None = None,
) -> RawDefinition | None:
    """Read deployments/{client}/{role_type}/…; returns None if folder absent."""
    if roots is None:
        roots = RootConfig()

    folder = _deployment_folder(roots.deployments_root, client, role_type)

    if not folder.exists():
        return None

    role_fm, role_body = _read_md(folder / "role.md")
    manifest_fm, _ = _read_md(folder / "manifest.md")
    policy_fm, _ = _read_md(folder / "policy.md")

    # Use parent role_type as the role_name fallback
    role_name = str(
        role_fm.get("name", manifest_fm.get("role", role_type))
    )
    version = str(role_fm.get("version", manifest_fm.get("version", "1.0")))
    deployment = str(manifest_fm.get("deployment", client))

    return RawDefinition(
        role_name=role_name,
        version=version,
        deployment=deployment,
        system_prompt=role_body,
        tools=_as_str_list(manifest_fm.get("tools")),
        skills=_as_str_list(manifest_fm.get("skills")),
        context=dict(manifest_fm.get("context") or {}),
        permissions=manifest_fm.get("permissions", []),
        autonomy=str(policy_fm.get("autonomy", "supervised")),
        escalation_rules=dict(policy_fm.get("escalation_rules") or {}),
        delegation_policy=dict(policy_fm.get("delegation_policy") or {}),
        memory_policy=dict(policy_fm.get("memory_policy") or {}),
        audit_policy=dict(policy_fm.get("audit_policy") or {}),
        execution_limits=policy_fm.get("execution_limits"),
    )


# ---------------------------------------------------------------------------
# Merge helpers
# ---------------------------------------------------------------------------


def _resolve_permissions(
    parent_perms: list[str],
    override_perms: list[str] | str,
) -> list[str]:
    """Resolve permissions, honouring the ``inherit`` keyword."""
    if override_perms == "inherit":
        return list(parent_perms)
    if isinstance(override_perms, list):
        return list(override_perms)
    # fallback
    return list(parent_perms)


def _resolve_list_directive(
    parent_list: list[str],
    override_value: Any,
) -> list[str]:
    """Resolve a list-type field that may carry merge directives.

    Handles:
    - scalar ``"inherit"`` → return parent list as-is
    - ``{inherit: true, add: [...]}``    → parent + additions (dedup)
    - ``{inherit: true, remove: [...]}`` → parent minus removals
    - ``{override: <value>}``            → replace entirely
    - plain list                         → use override list as-is
    - None / absent                      → return parent list
    """
    if override_value is None:
        return list(parent_list)

    if override_value == "inherit":
        return list(parent_list)

    if isinstance(override_value, list):
        return list(override_value)

    if isinstance(override_value, dict):
        if override_value.get("inherit") is True:
            result = list(parent_list)
            additions = _as_str_list(override_value.get("add"))
            removals = set(_as_str_list(override_value.get("remove")))
            # add deduped
            seen = set(result)
            for item in additions:
                if item not in seen:
                    result.append(item)
                    seen.add(item)
            # remove
            result = [r for r in result if r not in removals]
            return result
        if "override" in override_value:
            return _as_str_list(override_value["override"])

    return list(parent_list)


def _resolve_mapping_directive(
    parent_mapping: dict[str, Any],
    override_value: Any,
) -> dict[str, Any]:
    """Resolve a mapping-type field that may carry ``inherit: true`` directive.

    If ``inherit: true`` is in the override dict, merge parent values as the
    base and overlay the override's non-directive keys on top.  Without
    ``inherit: true`` the override dict replaces the parent completely.
    """
    if override_value is None:
        return dict(parent_mapping)

    if override_value == "inherit":
        return dict(parent_mapping)

    if isinstance(override_value, dict):
        if override_value.get("inherit") is True:
            # Start from parent, then apply the override's extra directives
            result = dict(parent_mapping)
            for k, v in override_value.items():
                if k in ("inherit",):
                    continue
                if k == "add":
                    # handled separately for list sub-fields
                    parent_conditions = _as_str_list(result.get("conditions"))
                    result["conditions"] = _resolve_list_directive(
                        parent_conditions, {"inherit": True, "add": v}
                    )
                elif k == "remove":
                    parent_conditions = _as_str_list(result.get("conditions"))
                    result["conditions"] = _resolve_list_directive(
                        parent_conditions, {"inherit": True, "remove": v}
                    )
                else:
                    result[k] = v
            return result
        # plain override dict — no inherit directive
        return dict(override_value)

    return dict(parent_mapping)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_tools(parent: RawDefinition, override: RawDefinition) -> None:
    parent_set = set(parent.tools)
    override_set = set(override.tools)
    extra = override_set - parent_set
    if extra:
        raise DefinitionError(
            f"Invariant violation — tools: override requests tools not present in "
            f"the parent surface: {sorted(extra)}.  "
            f"Parent tools: {sorted(parent_set)}"
        )


def _validate_permissions(
    parent: RawDefinition,
    resolved_perms: list[str],
) -> None:
    parent_perms = (
        set(parent.permissions)
        if isinstance(parent.permissions, list)
        else set()
    )
    resolved_set = set(resolved_perms)
    extra = resolved_set - parent_perms
    if extra:
        raise DefinitionError(
            f"Invariant violation — permissions: resolved set contains permissions "
            f"not present in the parent: {sorted(extra)}.  "
            f"Parent permissions: {sorted(parent_perms)}"
        )


def _validate_autonomy(parent: RawDefinition, override: RawDefinition) -> None:
    parent_rank = _AUTONOMY_RANK.get(parent.autonomy)
    override_rank = _AUTONOMY_RANK.get(override.autonomy)

    if parent_rank is None:
        raise DefinitionError(
            f"Invariant violation — autonomy: unknown parent autonomy level "
            f"'{parent.autonomy}'.  Valid values: {list(_AUTONOMY_RANK)}"
        )
    if override_rank is None:
        raise DefinitionError(
            f"Invariant violation — autonomy: unknown override autonomy level "
            f"'{override.autonomy}'.  Valid values: {list(_AUTONOMY_RANK)}"
        )
    if override_rank > parent_rank:
        raise DefinitionError(
            f"Invariant violation — autonomy: override level '{override.autonomy}' "
            f"(rank {override_rank}) exceeds parent ceiling '{parent.autonomy}' "
            f"(rank {parent_rank}).  Deployments may only restrict, not elevate."
        )


# ---------------------------------------------------------------------------
# merge
# ---------------------------------------------------------------------------


def merge(generic: RawDefinition, override: RawDefinition) -> AgentDefinition:
    """Apply override merge directives to the generic definition.

    Validates all structural invariants and raises ``DefinitionError`` on any
    violation before returning the frozen ``AgentDefinition``.
    """
    # --- Validate autonomy BEFORE resolving other fields ---
    _validate_autonomy(generic, override)

    # --- Resolve tools ---
    resolved_tools = override.tools  # override declares its own tool subset
    _validate_tools(generic, override)

    # --- Resolve permissions ---
    parent_perms = (
        list(generic.permissions)
        if isinstance(generic.permissions, list)
        else []
    )
    resolved_perms = _resolve_permissions(parent_perms, override.permissions)
    _validate_permissions(generic, resolved_perms)

    # --- Resolve escalation_rules ---
    resolved_escalation = _resolve_mapping_directive(
        generic.escalation_rules, override.escalation_rules
    )

    # --- Resolve delegation_policy ---
    resolved_delegation = _resolve_mapping_directive(
        generic.delegation_policy, override.delegation_policy
    )

    # --- Resolve memory_policy ---
    resolved_memory = _resolve_mapping_directive(
        generic.memory_policy, override.memory_policy
    )

    # --- Resolve audit_policy ---
    resolved_audit = _resolve_mapping_directive(
        generic.audit_policy, override.audit_policy
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
        resolved_limits = dict(ov_limits)
    else:
        resolved_limits = None

    # --- Resolve context ---
    resolved_context: dict[str, Any]
    if override.context:
        resolved_context = dict(override.context)
    else:
        resolved_context = dict(generic.context)

    # --- Skills come entirely from the override (platform level is always []) ---
    resolved_skills = list(override.skills)

    return AgentDefinition(
        role_name=generic.role_name,
        version=generic.version,
        deployment=override.deployment,
        system_prompt=override.system_prompt,
        tools=tuple(resolved_tools),
        skills=tuple(resolved_skills),
        context=resolved_context,
        permissions=tuple(resolved_perms),
        autonomy=override.autonomy,
        escalation_rules=resolved_escalation,
        delegation_policy=resolved_delegation,
        memory_policy=resolved_memory,
        audit_policy=resolved_audit,
        execution_limits=resolved_limits,
    )


# ---------------------------------------------------------------------------
# resolve — public entry point
# ---------------------------------------------------------------------------


def resolve(
    role_type: str,
    *,
    client: str | None = None,
    roots: RootConfig | None = None,
) -> AgentDefinition:
    """Load, merge, validate, and return a fully resolved ``AgentDefinition``.

    Parameters
    ----------
    role_type:
        The role folder name (e.g. ``"sales-agent"``).
    client:
        Optional deployment client name.  If given and the override folder
        exists, the override is merged on top of the generic definition.
        If the folder does not exist, the generic definition is returned.
    roots:
        Injectable path config.  Defaults to the real repo roots.
    """
    if roots is None:
        roots = RootConfig()

    generic = load_generic(role_type, roots=roots)

    if client is not None:
        override = load_override(client, role_type, roots=roots)
        if override is not None:
            return merge(generic, override)

    # No override — wrap the generic RawDefinition into a frozen AgentDefinition
    parent_perms = (
        list(generic.permissions)
        if isinstance(generic.permissions, list)
        else []
    )
    exec_limits: Mapping[str, Any] | None = (
        dict(generic.execution_limits)
        if isinstance(generic.execution_limits, dict)
        else None
    )

    return AgentDefinition(
        role_name=generic.role_name,
        version=generic.version,
        deployment=None,
        system_prompt=generic.system_prompt,
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
    )
