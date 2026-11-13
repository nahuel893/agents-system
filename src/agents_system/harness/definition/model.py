"""The data an agent definition is made of, raw and resolved."""

from __future__ import annotations

import dataclasses
import pathlib
from collections.abc import Mapping
from typing import Any

from agents_system.harness.registry import Tier


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
    #: ADR-002 C.11. Always a concrete bool on a resolved AgentDefinition —
    #: defaults to False when no role in the chain declared it.
    untrusted_input: bool = False
    #: ADR-002 C.12. Fully resolved declarative command tools this role may
    #: build a `ToolSpec` for — see `CommandToolDeclaration`. Empty for every
    #: role that declares none, which is the overwhelming majority.
    command_tools: tuple[CommandToolDeclaration, ...] = ()
    #: design.md D4 (PR3). An importer agent's own `skills/` subdirectory
    #: (`Agent.from_folder(path)`'s `path/skills/`) — always a
    #: `FolderLocator.path` derivative, never a caller-suppliable separate
    #: value. `None` for a platform role or an inline-only agent with no
    #: folder of its own.
    skills_folder: pathlib.Path | None = None
    #: design.md D4 (PR3). `{name: content}` supplied directly as Python
    #: parameters (`Agent(skill_contents=...)`). Empty for a platform role
    #: or a folder-only agent with no inline content of its own.
    inline_skills: Mapping[str, str] = dataclasses.field(default_factory=dict)
    #: Issue #75. The `FolderLocator.root` that `skills_folder` belongs to:
    #: a skill file must resolve inside both, and `_load_skills` reads it
    #: walking from this root (`_read_within_root`). `None` exactly when
    #: `skills_folder` is.
    importer_root: pathlib.Path | None = None
    #: D-037 (issue #13, ADR-004). `None` means this runtime uses the
    #: global `ADAPTER_PROVIDER`-driven default (`main._build_chat_model`,
    #: unchanged) -- the overwhelming majority of resolved definitions.
    #: Otherwise a fully validated `{"provider": ..., "model": ...,
    #: "temperature"?: ..., "max_tokens"?: ...}` mapping (never a
    #: credential -- see `_validate_model_config`), which
    #: `main._resolve_model_for_definition` builds a dedicated chat model
    #: from at boot, never at first request.
    model: Mapping[str, Any] | None = None


@dataclasses.dataclass(frozen=True)
class CommandToolParam:
    """One typed, pattern-validated placeholder in a command tool's `argv`
    template (ADR-002 C.12).

    `type` is the only field every param must declare; `pattern`,
    `max_length` and `enum` are optional call-time constraints layered on
    top of it. Validated against an actual call value by
    `connectors.command_tools`, never here — this dataclass is pure data.
    """

    type: str
    pattern: str | None = None
    max_length: int | None = None
    enum: tuple[str, ...] | None = None


@dataclasses.dataclass(frozen=True)
class CommandToolDeclaration:
    """One declarative command tool from a manifest's `command_tools:` list
    (ADR-002 C.12).

    Fully validated at LOAD time by `_parse_command_tools` below: `argv[0]`
    is always an absolute path (resolved once at load, never re-looked-up on
    `$PATH` at call time) and every `{param}` placeholder in `argv` occupies
    a WHOLE element — never a fragment of one, which is exactly how option
    injection (`--flag={x}` → `--flag=--evil`) sneaks past a naive template.
    `permission` always starts with `run:` — a family distinct from
    `exec:*`, so an `untrusted_input` role can hold one without tripping
    C.11's mutual-exclusion invariant.
    """

    name: str
    argv: tuple[str, ...]
    params: Mapping[str, CommandToolParam]
    tier: Tier
    permission: str


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
    #: ADR-002 C.11. `None` means "not declared" — inherit from the parent
    #: role or, on an override, from the resolved generic definition. Only
    #: an explicit `True`/`False` is a declaration.
    untrusted_input: bool | None = None
    #: ADR-002 C.12. NAMES only (same shape as `tools`) — a role's manifest
    #: names its own command tools here (`command_tool_declarations` below
    #: carries the full entries); a deployment override names the SUBSET it
    #: keeps. Absent means `[]`, exactly like `tools` — see `merge()`.
    command_tools: list[str] = dataclasses.field(default_factory=list)
    #: ADR-002 C.12. `{name: full declaration}`, populated ONLY by a platform
    #: role's own manifest (`_load_role_files`) — a deployment override never
    #: originates a new declaration, it only narrows `command_tools` above.
    command_tool_declarations: dict[str, CommandToolDeclaration] = dataclasses.field(
        default_factory=dict
    )
    #: design.md D4 (PR3). Set only by `_load_role_files`'s `FolderLocator`
    #: branch, to that folder's own `skills/` subdirectory — `None` for a
    #: platform role and for an inline definition with no folder of its own.
    skills_folder: pathlib.Path | None = None
    #: design.md D4 (PR3). `{name: content}`, populated only by an
    #: `InlineLocator`'s own `raw` (`Agent._to_locator()`'s `skill_contents`).
    #: Empty for a platform role or a folder-only definition.
    inline_skills: dict[str, str] = dataclasses.field(default_factory=dict)
    #: Issue #75. Set with `skills_folder`, to the `FolderLocator.root` it
    #: belongs to; see `AgentDefinition.importer_root`.
    importer_root: pathlib.Path | None = None
    #: D-037 (issue #13, ADR-004) -- the raw `model:` declaration: `None`
    #: (not declared -- inherits the parent's, or falls back to the global
    #: `ADAPTER_PROVIDER`-driven default), the string `"inherit"`, or a
    #: mapping with `provider`/`model` and optional `temperature`/
    #: `max_tokens`. Shape/value-checked by `_validate_model_config` only at
    #: the two `AgentDefinition`-construction sites (`_merge_validated`,
    #: `resolve`'s no-override branch) -- exactly the same validation depth
    #: `execution_limits` gets, which this field otherwise mirrors. Never a
    #: capability or safety ceiling: an override may name ANY supported
    #: provider+model, so there is no `_apply_importer_ceiling` involvement.
    model: Mapping[str, Any] | str | None = None


@dataclasses.dataclass(frozen=True)
class FolderLocator:
    """An importer-supplied folder, read the same way `_load_role_files`
    reads a platform role folder today (role.md/manifest.md/policy.md).

    `root` bounds the importer-space `extends:` search (design.md D2): a
    relative `extends:` value found while resolving *this* locator's chain
    may reference a sibling/descendant folder under `root`, never outside
    it. `overrides` carries `Agent.from_folder(path, **overrides)`'s Python
    params, applied (field-replace, not merge — see design.md D3) after the
    folder is read. Its `extends` value, when present, replaces the
    manifest's `extends:`: a string placed like one, or the parent `Agent`'s
    own locator.
    """

    path: pathlib.Path
    root: pathlib.Path
    overrides: Mapping[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class InlineLocator:
    """An already-built `RawDefinition` — skips disk entirely.

    `parent` is the (not yet resolved) `extends:` value: a bare/prefixed
    platform-role string (resolved exactly like a manifest's own `extends:`
    frontmatter, lazily, at chain-walk time via design.md D2's algorithm),
    or `None`. An `Agent(extends=<another Agent>)` is resolved eagerly at
    `Agent.__init__` time instead (object identity, no string parsing
    needed — see design.md D3), so `parent` here is only ever `str | None`
    in practice, but is typed `RoleLocator | None` for uniformity with
    `_resolve_role_chain`'s walk.
    """

    raw: RawDefinition
    parent: RoleLocator | None = None


#: design.md D1 — a small discriminated union, not a new wrapper class: a
#: bare `str` (predefined-role name, today's only locator kind) needs no
#: wrapping to keep meaning what it already means, and every existing call
#: site passing one keeps working unchanged.
RoleLocator = str | FolderLocator | InlineLocator
