"""Declarative ``command_tools`` (ADR-002 C.12): parsing and validation."""

from __future__ import annotations

import dataclasses
import os
import pathlib
import re
import shutil
from collections.abc import Iterable, Mapping
from typing import Any

from agents_system.harness.definition.errors import DefinitionError
from agents_system.harness.definition.model import (
    CommandToolDeclaration,
    CommandToolParam,
    RawDefinition,
)
from agents_system.harness.registry import Tier

#: A `{name}` placeholder — the ENTIRE element must match this, never a
#: fragment of it (see `_validate_argv_template`).
_PLACEHOLDER_WHOLE = re.compile(r"^\{([A-Za-z_][A-Za-z0-9_]*)\}$")
#: Any `{...}`-shaped fragment anywhere in an element, used only to detect a
#: PARTIAL placeholder (`--flag={x}`) so the error names the attempt.
_PLACEHOLDER_ANY = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")
_COMMAND_TOOL_PARAM_TYPES = ("string", "integer")
#: ADR-002 C.12 deliberately allows an untrusted_input role to hold a NARROW
#: T2 command tool — "narrow" is enforced, not aspirational: a run:* tool
#: below this floor is never revalidated at call time
#: (`interceptor._is_sensitive` only checks tier in {T2, T3} or
#: `always_revalidate`), so a T0/T1 command tool would reach an
#: untrusted_input role with no Layer-2 check at all (PR #147 review
#: follow-up: a T0 command tool was proven to run unrevalidated). Narrowed
#: to T2-only by permission-model Resolved Decision 3: `Run` is a single T2
#: family with no T3-floor sibling, so a T3 command tool would fail R2b's
#: floor at `ToolSpec` construction anyway (`max(2) >= 3` is false) —
#: rejecting it here, at load, gives an actionable error instead of a
#: construction-time crash deep in the injector.
_COMMAND_TOOL_MIN_TIER = (Tier.T2,)
#: A `max_length` beyond this is not "narrow" — it stops being a meaningful
#: constraint on what an untrusted model can smuggle through the param, so
#: it is capped rather than left to an author's judgement (PR #147 review
#: follow-up).
_COMMAND_TOOL_PARAM_MAX_LENGTH_CAP = 4096


def _ensure_command_tool_permission_registered(wire_name: str) -> None:
    """Register one manifest-declared command-tool permission to `Run`, the
    first time this exact wire name is seen.

    Unlike the 19 shipped wire names `permissions/builtins.py` registers at
    import time, a command tool's `permission` is authored per-manifest
    (design.md: "`declaration.permission`, which always resolves to `Run`
    (T2)") — there is no fixed table to register ahead of time.
    `ensure_resource_registered` does the get-or-create atomically (one
    `PermissionRegistry` lock acquisition), so two concurrent loads of the
    same not-yet-seen manifest can't race each other into a spurious
    collision or a lost registration.

    Deferred import: `permissions.base` imports `Tier` from
    `harness.registry`, which initializes the `agents_system.harness`
    PACKAGE (`harness/__init__.py`) first, which imports `loader.py` and, through it, this module — a
    module-level `from agents_system.permissions import ...` here would
    recurse into `agents_system.permissions` while it is still
    initializing, whenever `agents_system.permissions` is the first thing a
    fresh interpreter imports (see `tests/test_public_api.py::
    test_import_agents_system_permissions_standalone_succeeds`).
    """
    from agents_system.permissions import Run, ensure_resource_registered

    ensure_resource_registered(Run, wire_name)


def _resolve_argv0(raw: str, *, tool_name: str, source: pathlib.Path) -> str:
    """Resolve `argv[0]` to an absolute path ONCE, at load time (ADR-002 C.12).

    An already-absolute path is trusted as given. A bare name is looked up
    on `$PATH` here and only here — the resolved absolute path is what gets
    stored and, later, what gets executed; nothing re-resolves it against
    `$PATH` at call time, which removes the `$PATH`-manipulation vector
    `operator.py`'s own `_child_env` already closes for `use_term`.
    """
    candidate = pathlib.Path(raw)
    if candidate.is_absolute():
        return str(candidate)
    resolved = shutil.which(raw)
    if resolved is None:
        raise DefinitionError(
            f"Invariant violation — command_tools: {source} declares command "
            f"tool '{tool_name}' with argv[0]={raw!r}, which is not an "
            f"absolute path and could not be resolved on $PATH at load "
            f"time. ADR-002 C.12 requires argv[0] to resolve to an absolute "
            f"path at load time, not $PATH lookup at call time — give an "
            f"absolute path or make the binary available on $PATH now."
        )
    # Issue #127 — `shutil.which` itself can return a RELATIVE path when
    # `$PATH` contains a relative entry (e.g. `.` or `bin`). Storing that
    # verbatim would break ADR-002 C.12's "resolved once, absolute, at load
    # time" guarantee: a relative argv[0] still needs `$PATH`/cwd context to
    # mean anything at call time, exactly what this function exists to
    # avoid. Fail loudly instead of silently coercing it with
    # `os.path.abspath`, which would anchor it to THIS process's cwd — not
    # necessarily what the relative `$PATH` entry meant.
    if not os.path.isabs(resolved):
        raise DefinitionError(
            f"Invariant violation — command_tools: {source} declares command "
            f"tool '{tool_name}' with argv[0]={raw!r}, which resolved via "
            f"$PATH to a relative path ({resolved!r}) because $PATH contains "
            f"a relative entry. ADR-002 C.12 requires argv[0] to resolve to "
            f"an absolute path at load time — remove the relative entry "
            f"from $PATH or give an absolute path directly."
        )
    return resolved


def _validate_argv_template(
    raw_argv: Any,
    params: Mapping[str, CommandToolParam],
    *,
    tool_name: str,
    source: pathlib.Path,
) -> tuple[str, ...]:
    """Validate and resolve one command tool's `argv` template.

    Enforces, at LOAD time: `argv` is a non-empty list of strings; `argv[0]`
    is literal (never a placeholder) and gets resolved to an absolute path;
    every OTHER element is either a literal or a placeholder that occupies
    the WHOLE element — `--flag={x}` is rejected here, because a
    partial-element placeholder is how option injection sneaks in
    (`--flag=--evil-flag` smuggling a second flag through what looks like a
    value); every placeholder names a declared param, and every declared
    param is used by at least one placeholder.
    """
    _check_argv_shape(raw_argv, tool_name=tool_name, source=source)
    program, *rest = raw_argv
    _check_argv0_literal(program, tool_name=tool_name, source=source)
    resolved = _resolve_argv0(program, tool_name=tool_name, source=source)
    rest_elements = _check_argv_rest(rest, params, tool_name=tool_name, source=source)
    return (resolved, *rest_elements)


def _check_argv_shape(argv: Any, *, tool_name: str, source: pathlib.Path | str) -> None:
    """``argv`` is a non-empty list (as YAML parses it) or tuple (as a built
    ``CommandToolDeclaration`` holds it) of strings."""
    if (
        not isinstance(argv, list | tuple)
        or not argv
        or not all(isinstance(element, str) for element in argv)
    ):
        raise DefinitionError(
            f"Invariant violation — command_tools: {source} declares command "
            f"tool '{tool_name}' with argv={argv!r}, which must be a "
            f"non-empty list of strings."
        )


def _check_argv0_literal(
    program: str, *, tool_name: str, source: pathlib.Path | str
) -> None:
    if _PLACEHOLDER_ANY.search(program):
        raise DefinitionError(
            f"Invariant violation — command_tools: {source} declares command "
            f"tool '{tool_name}' with a placeholder in argv[0] ({program!r}). "
            f"The program path is fixed by the manifest author and may never "
            f"vary by param."
        )


def _check_argv_rest(
    rest: Iterable[str],
    params: Mapping[str, CommandToolParam],
    *,
    tool_name: str,
    source: pathlib.Path | str,
) -> tuple[str, ...]:
    """Every argv element after ``argv[0]``: a literal, or a placeholder that
    is the WHOLE element and names a declared param; every declared param
    is used. Returns the elements unchanged."""
    elements: list[str] = []
    used: set[str] = set()
    for element in rest:
        whole_match = _PLACEHOLDER_WHOLE.match(element)
        if whole_match is not None:
            name = whole_match.group(1)
            if name not in params:
                raise DefinitionError(
                    f"Invariant violation — command_tools: {source} declares "
                    f"command tool '{tool_name}' with placeholder "
                    f"'{{{name}}}' in argv, but no matching param is "
                    f"declared. Declared params: {sorted(params)}."
                )
            used.add(name)
            elements.append(element)
            continue

        if _PLACEHOLDER_ANY.search(element):
            raise DefinitionError(
                f"Invariant violation — command_tools: {source} declares "
                f"command tool '{tool_name}' with argv element {element!r}, "
                f"which embeds a placeholder inside a larger string. A "
                f"placeholder must occupy a WHOLE argv element (e.g. "
                f"'{{sku}}', never '--flag={{sku}}') — a partial-element "
                f"placeholder is how option injection sneaks in (ADR-002 "
                f"C.12)."
            )
        elements.append(element)

    unused = set(params) - used
    if unused:
        raise DefinitionError(
            f"Invariant violation — command_tools: {source} declares command "
            f"tool '{tool_name}' with param(s) {sorted(unused)} that never "
            f"appear as a placeholder in argv. Every declared param must be "
            f"used."
        )
    return tuple(elements)


def _parse_command_tool_param(
    raw: Any, *, tool_name: str, param_name: str, source: pathlib.Path | str
) -> CommandToolParam:
    if not isinstance(raw, dict):
        raise DefinitionError(
            f"Invariant violation — command_tools: {source} declares command "
            f"tool '{tool_name}' param '{param_name}' as {raw!r}, which must "
            f"be a mapping with at least a 'type' key."
        )
    ptype = raw.get("type")
    if ptype not in _COMMAND_TOOL_PARAM_TYPES:
        raise DefinitionError(
            f"Invariant violation — command_tools: {source} declares command "
            f"tool '{tool_name}' param '{param_name}' with type={ptype!r}. "
            f"Valid types: {list(_COMMAND_TOOL_PARAM_TYPES)}."
        )
    pattern = raw.get("pattern")
    if pattern is not None and not isinstance(pattern, str):
        raise DefinitionError(
            f"Invariant violation — command_tools: {source} declares command "
            f"tool '{tool_name}' param '{param_name}' with a non-string "
            f"pattern."
        )
    max_length = raw.get("max_length")
    if max_length is not None and (
        isinstance(max_length, bool) or not isinstance(max_length, int)
    ):
        raise DefinitionError(
            f"Invariant violation — command_tools: {source} declares command "
            f"tool '{tool_name}' param '{param_name}' with a non-integer "
            f"max_length."
        )
    if max_length is not None and max_length > _COMMAND_TOOL_PARAM_MAX_LENGTH_CAP:
        raise DefinitionError(
            f"Invariant violation — command_tools: {source} declares command "
            f"tool '{tool_name}' param '{param_name}' with "
            f"max_length={max_length}, which exceeds the platform cap of "
            f"{_COMMAND_TOOL_PARAM_MAX_LENGTH_CAP} (ADR-002 C.12 — a command "
            f"tool's params must stay narrow; max_length is optional but not "
            f"unbounded)."
        )
    enum_raw = raw.get("enum")
    enum = tuple(str(v) for v in enum_raw) if enum_raw is not None else None

    # ADR-002 C.12 deliberately lets an untrusted_input role hold a narrow T2
    # command tool — "narrow" is what makes that safe, and is enforced here,
    # not left to an author's judgement (PR #147 review follow-up: a
    # `type: string` param with no `pattern` let a value read `/etc/hostname`
    # straight through). `enum` is an equally valid narrowing — either is
    # accepted, but at least one is required for every string param.
    if ptype == "string" and pattern is None and enum is None:
        raise DefinitionError(
            f"Invariant violation — command_tools: {source} declares command "
            f"tool '{tool_name}' param '{param_name}' with type=string but no "
            f"'pattern' or 'enum'. An unconstrained string param can carry "
            f"arbitrary text into the command's argv — declare 'pattern' or "
            f"'enum' to narrow what it may contain (ADR-002 C.12)."
        )

    return CommandToolParam(
        type=ptype, pattern=pattern, max_length=max_length, enum=enum
    )


def _parse_command_tools(
    manifest_fm: dict[str, Any], *, source: pathlib.Path
) -> list[CommandToolDeclaration]:
    """Parse and fully validate a role manifest's `command_tools:` list.

    Only a platform ROLE's own `manifest.md` calls this — a deployment
    override's `command_tools:` is a bare list of NAMES (parsed with
    `_as_str_list`, like `tools:`), never full entries; see `merge()`.
    """
    raw_list = manifest_fm.get("command_tools")
    if raw_list is None:
        return []
    if not isinstance(raw_list, list):
        raise DefinitionError(
            f"Invariant violation — command_tools: {source} declares "
            f"command_tools={raw_list!r}, which must be a list."
        )

    declarations: list[CommandToolDeclaration] = []
    seen_names: set[str] = set()
    for entry in raw_list:
        if not isinstance(entry, dict):
            raise DefinitionError(
                f"Invariant violation — command_tools: {source} declares a "
                f"command_tools entry {entry!r}, which must be a mapping."
            )
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise DefinitionError(
                f"Invariant violation — command_tools: {source} declares a "
                f"command_tools entry with a missing or invalid 'name'."
            )
        if name in seen_names:
            raise DefinitionError(
                f"Invariant violation — command_tools: {source} declares "
                f"command tool '{name}' more than once."
            )
        seen_names.add(name)

        params_raw = entry.get("params") or {}
        if not isinstance(params_raw, dict):
            raise DefinitionError(
                f"Invariant violation — command_tools: {source} declares "
                f"command tool '{name}' with a non-mapping 'params'."
            )
        params = {
            pname: _parse_command_tool_param(
                praw, tool_name=name, param_name=pname, source=source
            )
            for pname, praw in params_raw.items()
        }

        argv = _validate_argv_template(
            entry.get("argv"), params, tool_name=name, source=source
        )

        tier = _checked_command_tool_tier(entry.get("tier"), name=name, source=source)
        permission = _checked_command_tool_permission(
            entry.get("permission"), name=name, source=source
        )
        _ensure_command_tool_permission_registered(permission)

        declarations.append(
            CommandToolDeclaration(
                name=name, argv=argv, params=params, tier=tier, permission=permission
            )
        )
    return declarations


def _checked_command_tool_tier(
    tier_raw: Any, *, name: str, source: pathlib.Path | str
) -> Tier:
    try:
        tier = Tier(tier_raw)
    except ValueError:
        raise DefinitionError(
            f"Invariant violation — command_tools: {source} declares "
            f"command tool '{name}' with tier={tier_raw!r}. Valid "
            f"tiers: {[t.value for t in Tier]}."
        ) from None

    if tier not in _COMMAND_TOOL_MIN_TIER:
        raise DefinitionError(
            f"Invariant violation — command_tools: {source} declares "
            f"command tool '{name}' with tier={tier.value}, but "
            f"declarative command tools require tier=T2 exactly "
            f"(permission-model Resolved Decision 3). A T0/T1 tool is "
            f"never revalidated at call time (interceptor._is_sensitive) "
            f"and would let an untrusted_input role reach an "
            f"unrevalidated host command; a T3 tool would fail R2b's "
            f"floor at ToolSpec construction, since a command tool's "
            f"permission is always in the run:* family (`Run`, T2), "
            f"with no T3-floor sibling. Use tier: T2."
        )
    return tier


def _checked_command_tool_permission(
    permission: Any, *, name: str, source: pathlib.Path | str
) -> str:
    if not isinstance(permission, str) or not permission.startswith("run:"):
        raise DefinitionError(
            f"Invariant violation — command_tools: {source} declares "
            f"command tool '{name}' with permission={permission!r}, "
            f"which must start with 'run:' — command tools are a "
            f"distinct permission family from exec:*, so an "
            f"untrusted_input role can safely hold one without "
            f"tripping C.11's invariant (ADR-002 C.12)."
        )
    return permission


def _revalidate_command_tools(definition: RawDefinition) -> None:
    """Hold every command tool a resolved definition carries to the
    manifest parser's rules again (issue #75): T2 only, a ``run:``
    permission, an absolute literal ``argv[0]``, whole-element placeholders
    that name declared params, and narrow params.

    ``_parse_command_tools`` only runs on a manifest. An
    ``InlineLocator(raw=RawDefinition(...))`` hands the loader built
    ``CommandToolDeclaration`` objects directly, so without this nothing
    checked them. ``resolve()`` calls it on the fully folded definition, so
    it covers every locator kind and every hop of the chain. It changes
    nothing: a declaration that passes is used exactly as given. ``argv[0]``
    must already be absolute here: ``$PATH`` is only looked up while a
    manifest is parsed, never later.
    """
    who = f"agent '{definition.role_name}'"
    declarations = definition.command_tool_declarations
    for name in definition.command_tools:
        if name not in declarations:
            raise DefinitionError(
                f"Invariant violation — command_tools: {who} names command "
                f"tool '{name}' but declares no entry for it."
            )
    for key, declaration in declarations.items():
        if not isinstance(declaration, CommandToolDeclaration):
            raise DefinitionError(
                f"Invariant violation — command_tools: {who} declares command "
                f"tool '{key}' as a {type(declaration).__name__}, not a "
                f"CommandToolDeclaration."
            )
        if declaration.name != key:
            raise DefinitionError(
                f"Invariant violation — command_tools: {who} files command "
                f"tool '{declaration.name}' under the name '{key}'. The key "
                f"must be the declaration's own name."
            )
        _check_argv_shape(declaration.argv, tool_name=key, source=who)
        params = declaration.params
        if not isinstance(params, Mapping):
            raise DefinitionError(
                f"Invariant violation — command_tools: {who} declares command "
                f"tool '{key}' with non-mapping params."
            )
        for param_name, param in params.items():
            if not isinstance(param, CommandToolParam):
                raise DefinitionError(
                    f"Invariant violation — command_tools: {who} declares "
                    f"command tool '{key}' param '{param_name}' as a "
                    f"{type(param).__name__}, not a CommandToolParam."
                )
            _parse_command_tool_param(
                dataclasses.asdict(param),
                tool_name=key,
                param_name=param_name,
                source=who,
            )
        program, *rest = declaration.argv
        _check_argv0_literal(program, tool_name=key, source=who)
        if not pathlib.Path(program).is_absolute():
            raise DefinitionError(
                f"Invariant violation — command_tools: {who} declares command "
                f"tool '{key}' with argv[0]={program!r}, which is not an "
                f"absolute path. ADR-002 C.12 fixes argv[0] to an absolute "
                f"path when the definition is built; nothing looks it up on "
                f"$PATH later."
            )
        _check_argv_rest(rest, params, tool_name=key, source=who)
        _checked_command_tool_tier(declaration.tier, name=key, source=who)
        _checked_command_tool_permission(declaration.permission, name=key, source=who)
