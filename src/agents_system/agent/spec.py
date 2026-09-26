"""``Agent`` — the library-first, Python-native way to define a custom agent
(ADR-004, design.md D3/D6).

An ``Agent`` is a frozen, immutable value object. It never touches disk (or,
for a folder-backed one, never touches disk until something actually resolves
it — see ``from_folder``): it only builds a ``RoleLocator``
(``harness.loader.RoleLocator``, design.md D1) via ``_to_locator()``, which
``harness.loader.resolve()``/``harness.factory.build_runtime()`` then read,
merge, and validate through the exact same pipeline a predefined platform
role already goes through — see the ``agent-definition-locator`` spec's
"Agent(...) Python-parameter construction resolves without disk",
"Agent.from_folder produces the loader's AgentDefinition shape", and "Folder
content and Python parameters compose, with parameters as the override
layer" requirements. Placed at ``agent/spec.py``, not a sibling ``agent.py``
module, because ``agent/`` is already a package (``agent/graph.py`` etc. —
design.md D6); importing ``Agent`` pulls in only this module and
``harness.loader``, never ``agent.graph`` (and therefore never LangGraph).
"""

from __future__ import annotations

import dataclasses
import pathlib
import types
from collections.abc import Mapping
from typing import Any

from agents_system.harness.loader import (
    DefinitionError,
    FolderLocator,
    InlineLocator,
    RawDefinition,
    RoleLocator,
)


def _freeze(value: Any) -> Any:
    """A deep, immutable copy of ``value``: every mapping becomes a
    ``MappingProxyType`` over a fresh ``dict``, every list or tuple a
    ``tuple``, every set a ``frozenset``, nested values included. Anything
    else (a ``str``, a number, a path, another ``Agent``) is already
    immutable and is returned as is.

    ``frozen=True`` only blocks reassigning a field. Without this copy, a
    list or dict the caller passed in stays shared with the caller, and
    mutating it afterwards changes every later ``_to_locator()`` -- including
    one reached through another ``Agent``'s ``extends=``."""
    if isinstance(value, Mapping):
        return types.MappingProxyType(
            {key: _freeze(item) for key, item in value.items()}
        )
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set | frozenset):
        return frozenset(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    """The inverse of ``_freeze``, as a fresh copy: mappings become plain
    ``dict``s and tuples plain ``list``s, nested values included -- the
    shapes the loader parses out of YAML and checks for (a tuple where it
    expects a list, or a ``MappingProxyType`` where it expects a ``dict``,
    would be skipped or misread). Each call returns new containers, so
    nothing the loader does to them can reach back into the ``Agent``."""
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


@dataclasses.dataclass(frozen=True)
class Agent:
    """A custom agent definition, built entirely in Python, from a folder, or
    both (design.md D3).

    ``extends`` accepts a bare/prefixed predefined-role name (resolved
    lazily, at chain-walk time, by the same fail-loud algorithm the
    ``agent-definition-locator`` spec's "extends:" requirements describe —
    design.md D2) or another already-built ``Agent`` (resolved eagerly, at
    ``_to_locator()`` call time, since the parent object already exists in
    memory — no I/O, and cycle-free by construction because Python cannot
    build an object before its own dependencies exist).

    ``skill_contents`` carries inline skill text (consumed by the skills
    capability — PR3); it is validated here (every key must also be listed in
    ``skills``) but not yet threaded into a resolved ``AgentDefinition`` in
    this module, per design.md's own Testing Strategy note for this PR ("no
    ``_load_skills`` wiring here").
    """

    name: str
    extends: str | Agent = "platform/roles/agent"
    tools: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    skill_contents: Mapping[str, str] = dataclasses.field(default_factory=dict)
    context: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    autonomy: str = ""
    escalation_rules: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    delegation_policy: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    memory_policy: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    audit_policy: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    execution_limits: Mapping[str, Any] | str | None = None
    untrusted_input: bool | None = None
    system_prompt: str = ""
    version: str = "1.0"
    _folder: pathlib.Path | None = dataclasses.field(default=None, repr=False)
    _folder_overrides: Mapping[str, Any] = dataclasses.field(
        default_factory=dict, repr=False
    )

    def __post_init__(self) -> None:
        # Deep-immutable, not just frozen (see `_freeze`). Every field goes
        # through it, so a container field added later is covered too.
        for field in dataclasses.fields(self):
            object.__setattr__(self, field.name, _freeze(getattr(self, field.name)))

        unlisted = self.skill_contents.keys() - set(self.skills)
        if unlisted:
            raise DefinitionError(
                "Agent: skill_contents names a skill not listed in `skills`: "
                f"{sorted(unlisted)}"
            )
        if self._folder is not None:
            unknown = set(self._folder_overrides) - _AGENT_OVERRIDABLE_FIELDS
            if unknown:
                raise DefinitionError(
                    f"Agent.from_folder: unknown override(s) {sorted(unknown)}"
                )

    @classmethod
    def from_folder(cls, path: str | pathlib.Path, /, **overrides: Any) -> Agent:
        """Build an ``Agent`` lazily from ``path``'s ``role.md``/
        ``manifest.md``/``policy.md`` (the same three-file contract a
        platform role uses).

        No filesystem access happens here, matching ``RootConfig``'s own
        "validate at first use" philosophy — ``path`` is read the first time
        this ``Agent``'s locator is actually resolved.

        ``overrides`` REPLACES (never merges with) the folder's own field,
        applied after the read (design.md D3's explicit non-merge choice) — a
        caller wanting additive tools writes ``tools=[*folder_tools,
        "extra"]`` explicitly, so the direction of the merge is never a
        hidden per-field guess.

        ``extends=`` replaces the manifest's ``extends:`` the same way: a
        string is placed like the manifest value (relative to ``path``), and
        another ``Agent`` is used as the parent directly. The agent is then
        held to that parent's safety ceiling and ``untrusted_input``.
        """
        folder_path = pathlib.Path(path)
        return cls(
            name=str(overrides.get("name", folder_path.name)),
            _folder=folder_path,
            _folder_overrides=dict(overrides),
        )

    def _to_locator(self) -> RoleLocator:
        # Every container handed to the loader is a fresh, plain copy
        # (`_thaw`): the loader's shapes, and nothing it can mutate back into
        # this frozen `Agent`.
        if self._folder is not None:
            overrides = _thaw(self._folder_overrides)
            if isinstance(overrides.get("extends"), Agent):
                # Same eager rule as the inline branch below: the loader
                # gets the parent's locator and uses it in place of the
                # manifest's own `extends:` (see `_load_role_files`).
                overrides["extends"] = overrides["extends"]._to_locator()
            return FolderLocator(
                path=self._folder,
                root=self._folder.parent,
                overrides=overrides,
            )
        raw = RawDefinition(
            role_name=self.name,
            version=self.version,
            deployment=None,
            system_prompt=self.system_prompt,
            tools=list(self.tools),
            skills=list(self.skills),
            context=_thaw(self.context),
            permissions=list(self.permissions),
            autonomy=self.autonomy,
            escalation_rules=_thaw(self.escalation_rules),
            delegation_policy=_thaw(self.delegation_policy),
            memory_policy=_thaw(self.memory_policy),
            audit_policy=_thaw(self.audit_policy),
            execution_limits=_thaw(self.execution_limits),
            untrusted_input=self.untrusted_input,
        )
        parent: RoleLocator | None
        if isinstance(self.extends, Agent):
            # Eager — the parent object already exists, so resolving it to a
            # locator is pure Python, no I/O, no cycle risk.
            parent = self.extends._to_locator()
        else:
            # Lazy — a bare/prefixed platform-role name (or, reachable only
            # through a folder-based chain, an importer-root-relative path),
            # walked by design.md D2's algorithm at chain-walk time.
            parent = self.extends
        return InlineLocator(raw=raw, parent=parent)


#: design.md D3 — the set of `Agent`'s own dataclass field names an
#: `Agent.from_folder(path, **overrides)` call may legally name. Computed
#: from the dataclass itself (never hand-duplicated) so a new field is
#: automatically overridable without a second edit here.
_AGENT_OVERRIDABLE_FIELDS: frozenset[str] = frozenset(
    field.name for field in dataclasses.fields(Agent)
) - {"_folder", "_folder_overrides"}
