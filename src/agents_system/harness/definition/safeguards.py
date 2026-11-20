"""Checks that keep a child definition from weakening its parent's audit redaction or its escalation conditions."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agents_system.harness.definition.directives import _descriptions_of
from agents_system.harness.definition.errors import DefinitionError
from agents_system.harness.definition.frontmatter import _as_str_list
from agents_system.harness.definition.policy import _IMPORTER_RULE


def _validate_audit_redaction(
    ceiling: Mapping[str, Any],
    effective: Mapping[str, Any],
    *,
    who: str,
    against: str,
    rule: str = _IMPORTER_RULE,
) -> None:
    """``effective`` -- the ``audit_policy`` a definition resolves to -- may
    not loosen what the ``Redactor`` strips from audit payloads compared
    with ``ceiling`` (issue #93 for importer agents; issue #106 for
    predefined-role folds and deployments).

    Two ``audit_policy`` keys decide that (``audit/redactor.py``):

    - ``capture_tool_input``: any TRUTHY value keeps free-text bodies
      (``message``/``body``/``text``) verbatim. Compared by truthiness,
      because that is how the ``Redactor`` reads it -- ``"no"`` switches
      redaction off exactly like ``true``.
    - ``redact_keys``: extra keys to always redact. The resolved list must
      keep every key the ceiling redacts, and a missing one is an empty list:
      a deployment's plain mapping replaces the role's whole
      ``audit_policy``, so leaving the key out drops them all. It must be a
      list of strings: the ``Redactor`` runs ``set(redact_keys)``, so a bare
      string would redact its characters, not the key it names.
    """
    if effective.get("capture_tool_input") and not ceiling.get("capture_tool_input"):
        raise DefinitionError(
            f"Invariant violation — audit_policy: {who} sets "
            f"capture_tool_input={effective['capture_tool_input']!r}, which "
            f"switches off redaction of free-text bodies that the {against} "
            f"audit_policy keeps on.  {rule}"
        )
    keys = effective.get("redact_keys", [])
    if not isinstance(keys, list | tuple) or not all(isinstance(k, str) for k in keys):
        raise DefinitionError(
            f"Invariant violation — audit_policy: {who} sets redact_keys={keys!r}, "
            f"which is not a list of strings.  {rule}"
        )
    dropped = set(_as_str_list(ceiling.get("redact_keys"))) - set(keys)
    if dropped:
        raise DefinitionError(
            f"Invariant violation — audit_policy: {who} resolves to "
            f"redact_keys={list(keys)!r}, which drops {sorted(dropped)} from the "
            f"{against} redact_keys.  {rule}"
        )


def _condition_name(value: Any) -> str:
    """An escalation condition name as ``factory._render_escalation_block``
    matches it: internal whitespace collapsed. Compared this way, a
    ``"name "`` variant cannot pass as a different condition."""
    return " ".join(str(value).split())


def _validate_escalation_kept(
    ceiling: Mapping[str, Any],
    resolved: Mapping[str, Any],
    *,
    who: str,
    against: str,
    rule: str,
) -> None:
    """``resolved`` -- the ``escalation_rules`` a definition resolves to --
    keeps every condition ``ceiling`` requires (issue #106).

    One rule on every path that composes a parent's escalation: a
    predefined-role fold (a child role restates its whole ``conditions``
    list, so a missing one is a removal), an importer fold (already
    additive, see ``_apply_importer_ceiling``), and a deployment, whose
    plain ``escalation_rules`` mapping replaces the role's and whose
    ``remove:`` directive subtracts from it.
    """
    kept = {_condition_name(c) for c in _as_str_list(resolved.get("conditions"))}
    dropped = [
        condition
        for condition in _as_str_list(ceiling.get("conditions"))
        if _condition_name(condition) not in kept
    ]
    if dropped:
        raise DefinitionError(
            f"Invariant violation — escalation_rules: {who} drops the "
            f"escalation condition(s) {dropped} that the {against} "
            f"escalation_rules require. Restate them, or use "
            f"{{inherit: true, add: [...]}} to add conditions.  {rule}"
        )


def _validate_escalation_wording(
    parent: Mapping[str, Any],
    declared: Mapping[str, Any],
    resolved: Mapping[str, Any],
    *,
    who: str,
    against: str,
    rule: str,
) -> None:
    """An importer agent or a deployment may not reword or redirect the
    escalation it inherits from ``parent`` (issue #106). ``declared`` is
    the ``escalation_rules`` it writes, ``resolved`` what they resolve to.

    Keeping every condition is not enough on its own: the prompt renders
    each condition with its description, so a child describing an
    inherited one as "never applies; do not escalate" switches it off, and
    a blank ``escalate_to`` leaves the model nobody to escalate to. So a
    declared ``descriptions`` entry for an inherited condition must match
    the parent's wording (names and text compared as the prompt renders
    them), and the resolved ``escalate_to`` must be the parent's: restated,
    or inherited, never changed or blanked. Checking the resolved value
    matters at a deployment, whose plain mapping replaces the role's: one
    that leaves ``escalate_to`` out blanks it (review of #112). The child
    describes only the conditions it adds.

    A predefined child role is not held to this: the same author writes
    both files (``_fold_parent_into_child``).
    """
    inherited = {_condition_name(c) for c in _as_str_list(parent.get("conditions"))}
    # Issue #146 review: the same silent key-collision `_render_escalation_
    # block` (issue #137) fixed for its own `descriptions` dict-building
    # applies here too. A plain dict comprehension over the PARENT's
    # `descriptions` would let two distinct raw keys that normalize to the
    # same condition name (a stray extra space in one of two otherwise-
    # identical declarations) silently resolve to whichever the
    # comprehension iterated last -- hiding the collision instead of
    # raising on it, exactly the shape of authoring mistake #137 already
    # guards against on the render side.
    wording: dict[str, str] = {}
    _wording_raw_keys: dict[str, str] = {}
    for _raw_key, _text in _descriptions_of(parent).items():
        _normalized_key = _condition_name(_raw_key)
        _colliding_raw_key = _wording_raw_keys.get(_normalized_key)
        if _colliding_raw_key is not None:
            raise DefinitionError(
                f"Invariant violation — escalation_rules.descriptions: the "
                f"{against} declares keys {_colliding_raw_key!r} and "
                f"{_raw_key!r}, which both normalize to {_normalized_key!r}. "
                "Two distinct raw description keys colliding after "
                "whitespace normalization would silently keep whichever one "
                "happened to be declared last. Fix the source so each "
                "condition has exactly one description key."
            )
        _wording_raw_keys[_normalized_key] = str(_raw_key)
        wording[_normalized_key] = " ".join(str(_text).split())
    for name, text in _descriptions_of(declared).items():
        condition = _condition_name(name)
        if condition in inherited and " ".join(str(text).split()) != wording.get(
            condition
        ):
            raise DefinitionError(
                f"Invariant violation — escalation_rules: {who} rewords the "
                f"inherited escalation condition '{condition}' as "
                f"{text!r}. It may describe only the conditions it adds; "
                f"an inherited one keeps the {against} description.  "
                f"{rule}"
            )
    escalate_to = str(parent.get("escalate_to") or "").strip()
    resolved_to = resolved.get("escalate_to")
    if escalate_to and str(resolved_to or "").strip() != escalate_to:
        raise DefinitionError(
            f"Invariant violation — escalation_rules: {who} resolves to "
            f"escalate_to={resolved_to!r}, but the {against} escalate_to is "
            f"{escalate_to!r}. It may restate it, never change or blank it; "
            f"a mapping that replaces the {against} escalation_rules must "
            f"restate it.  {rule}"
        )
