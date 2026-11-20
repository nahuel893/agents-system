"""Merge directives: checking and resolving ``inherit``, ``add``, ``remove`` and ``override`` on list and mapping fields."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agents_system.harness.definition.errors import DefinitionError
from agents_system.harness.definition.frontmatter import _as_str_list


def _union_preserving_order(parent: list[str], child: list[str]) -> list[str]:
    merged = list(parent)
    merged.extend(item for item in child if item not in merged)
    return merged


# ---------------------------------------------------------------------------
# Merge helpers
# ---------------------------------------------------------------------------


def _resolve_permissions(
    parent_perms: list[str],
    override_perms: list[str] | str,
    *,
    who: str = "a definition",
) -> list[str]:
    """Resolve permissions, honouring every list directive the module documents.

    This used to handle only the scalar ``"inherit"`` and a plain list, and
    fall through to returning the PARENT'S FULL SET for every other shape --
    including ``{inherit: true, remove: [...]}``, which this module's own
    docstring advertises and which `_resolve_list_directive` right below has
    implemented all along.

    So a deployment that explicitly removed a permission kept it. Silently,
    and in the direction that grants rather than denies: the author reads
    their manifest, sees the removal, and is wrong. `escalation_rules` and
    the other list fields already routed through the shared resolver; only
    permissions -- the field where being wrong costs the most -- did not.
    """
    return _resolve_list_directive(
        list(parent_perms), override_perms, who=who, field="permissions"
    )


#: The keys an ``{inherit: true, ...}`` directive may carry on a list field,
#: and on ``escalation_rules``, where ``add``/``remove`` edit ``conditions``.
_INHERIT_DIRECTIVE_KEYS = frozenset({"inherit", "add", "remove"})


#: Every key that makes a mapping a directive rather than plain data.
_DIRECTIVE_KEYS = _INHERIT_DIRECTIVE_KEYS | {"override"}


def _check_list_directive(value: Any, *, who: str, field: str) -> None:
    """Raise unless ``value`` is a shape `_resolve_list_directive` honours
    (issue #106): absent, ``"inherit"``, a list (a bare string is a one-item
    list, as ``_as_str_list`` reads it), ``{inherit: true, add: [...],
    remove: [...]}`` or ``{override: [...]}``.

    Any other mapping -- ``{remove: [...]}`` without ``inherit: true``, a
    typo'd key, ``inherit`` mixed with ``override`` -- used to resolve to the
    parent's FULL set, which keeps exactly what the author meant to remove.
    Shared by every path: ``Agent(permissions=...)`` at construction, a
    folder or predefined-role manifest in the fold, and a deployment in
    ``_merge_validated``.
    """
    if value is None or isinstance(value, str | list | tuple):
        return
    if isinstance(value, Mapping):
        keys = set(value)
        if value.get("inherit") is True and keys <= _INHERIT_DIRECTIVE_KEYS:
            return
        if keys == {"override"}:
            return
    raise DefinitionError(
        f"Invariant violation — {field}: {who} declares {field}={value!r}, "
        f"which is not a recognised directive. Use a list, 'inherit', "
        f"{{inherit: true, add: [...], remove: [...]}} or {{override: [...]}}."
    )


def _resolve_list_directive(
    parent_list: list[str],
    override_value: Any,
    *,
    who: str = "a definition",
    field: str = "permissions",
) -> list[str]:
    """Resolve a list-type field that may carry merge directives.

    Handles:
    - scalar ``"inherit"`` → return parent list as-is
    - ``{inherit: true, add: [...]}``    → parent + additions (dedup)
    - ``{inherit: true, remove: [...]}`` → parent minus removals
    - ``{override: <value>}``            → replace entirely
    - plain list                         → use override list as-is
    - any other string                   → a one-item list, as ``_as_str_list``
    - None / absent                      → return parent list

    Anything else raises ``DefinitionError`` naming ``who`` and ``field``
    (``_check_list_directive``, issue #106). It used to resolve to the
    parent's full set.
    """
    _check_list_directive(override_value, who=who, field=field)

    if override_value is None or override_value == "inherit":
        return list(parent_list)

    if isinstance(override_value, str):
        return [override_value]

    if isinstance(override_value, list | tuple):
        return list(override_value)

    if "override" in override_value:
        return _as_str_list(override_value["override"])

    # {inherit: true, add: [...], remove: [...]}
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
    return [r for r in result if r not in removals]


def _descriptions_of(rules: Mapping[str, Any]) -> Mapping[str, Any]:
    """``rules['descriptions']``, or an empty mapping when it has none."""
    descriptions = rules.get("descriptions")
    return descriptions if isinstance(descriptions, Mapping) else {}


def _check_escalation_descriptions(rules: Mapping[str, Any], *, who: str) -> None:
    """Raise unless ``rules['descriptions']``, when present, is a mapping of
    condition name to description.

    Any other value -- a YAML null from a bare ``descriptions:`` key, a list,
    a string -- used to replace every description the definition inherits,
    so each inherited condition rendered as a bare name, which #82/#88
    showed is not enough for the model to escalate. The fold kept them
    before issue #106 routed it through ``_resolve_mapping_directive``;
    rejecting the value is the fail-closed reading of both.
    """
    if "descriptions" in rules and not isinstance(rules["descriptions"], Mapping):
        raise DefinitionError(
            f"Invariant violation — escalation_rules: {who} declares "
            f"descriptions={rules['descriptions']!r}, which is not a mapping "
            f"of condition name to description. Leave the key out to keep "
            f"the inherited descriptions."
        )


def _check_mapping_directive(value: Any, *, who: str, field: str) -> None:
    """Raise unless ``value`` is a shape `_resolve_mapping_directive`
    honours (issue #106): absent, ``"inherit"``, a plain mapping, or
    ``{inherit: true, ...}``. On ``escalation_rules`` the directive may also
    carry ``add``/``remove``, which edit ``conditions``.

    A mapping with a directive key it cannot honour -- ``add`` without
    ``inherit: true``, ``inherit: 'yes'``, ``override`` -- is an authoring
    mistake: it used to replace the whole mapping, or be merged key by key
    into the policy as literal keys. Shared by ``Agent(...)`` at
    construction, the fold, and ``_merge_validated``.

    ``escalation_rules.descriptions`` must be a mapping too
    (``_check_escalation_descriptions``).
    """
    if value is None or value == "inherit":
        return
    if isinstance(value, Mapping):
        if field == "escalation_rules":
            _check_escalation_descriptions(value, who=who)
        directive_keys = set(value) & _DIRECTIVE_KEYS
        if not directive_keys:
            return
        allowed = (
            _INHERIT_DIRECTIVE_KEYS if field == "escalation_rules" else {"inherit"}
        )
        if value.get("inherit") is True and directive_keys <= allowed:
            return
    conditions = (
        ", with add: [...]/remove: [...] editing conditions"
        if field == "escalation_rules"
        else ""
    )
    raise DefinitionError(
        f"Invariant violation — {field}: {who} declares {field}={value!r}, "
        f"which is not a recognised directive. Use a mapping, or "
        f"{{inherit: true, ...}} to keep the parent's keys{conditions}."
    )


def _resolve_mapping_directive(
    parent_mapping: Mapping[str, Any],
    override_value: Any,
    *,
    who: str = "a definition",
    field: str = "escalation_rules",
) -> dict[str, Any]:
    """Resolve a mapping-type field that may carry ``inherit: true`` directive.

    If ``inherit: true`` is in the override dict, merge parent values as the
    base and overlay the override's non-directive keys on top.  Without
    ``inherit: true`` the override dict replaces the parent completely.

    An empty mapping keeps the parent's (issue #106): a deployment that
    leaves a policy field out loads it as ``{}``, and replacing the role's
    mapping with it silently dropped every escalation condition and audit
    setting the role had. An unrecognised shape raises
    (``_check_mapping_directive``).
    """
    _check_mapping_directive(override_value, who=who, field=field)

    if not isinstance(override_value, Mapping) or not override_value:
        # Absent, "inherit", or an empty mapping: nothing to change.
        return dict(parent_mapping)

    if override_value.get("inherit") is not True:
        # plain override dict — no inherit directive
        return dict(override_value)

    # Start from parent, then apply the override's extra directives
    result = dict(parent_mapping)
    for k, v in override_value.items():
        if k in ("inherit",):
            continue
        if k in ("add", "remove"):
            # Only on escalation_rules (`_check_mapping_directive`): they
            # edit its `conditions` list.
            result["conditions"] = _resolve_list_directive(
                _as_str_list(result.get("conditions")),
                {"inherit": True, k: v},
                who=who,
                field=f"{field}.{k}",
            )
        elif k == "descriptions" and isinstance(v, Mapping):
            # Issue #88 — same reasoning as `descriptions`' role-chain
            # merge (`_fold_parent_into_child`): a deployment that
            # supplies its own descriptions for the conditions it
            # `add`s should not silently erase the generic role's
            # inherited ones by replacing the whole mapping. Any mapping,
            # not only a `dict`: a read-only one replaced them (review of
            # #112). On `escalation_rules` a value that is not a mapping
            # was rejected above (`_check_escalation_descriptions`).
            result["descriptions"] = {**_descriptions_of(result), **v}
        else:
            result[k] = v
    return result
