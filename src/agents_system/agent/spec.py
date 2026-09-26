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
import os
import pathlib
from collections.abc import Mapping
from typing import Any, NoReturn, Self

from agents_system.harness.loader import (
    DefinitionError,
    FolderLocator,
    InlineLocator,
    RawDefinition,
    RoleLocator,
    _as_str_list,
    _parse_untrusted_input,
    _validate_inline_skills,
)


class _FrozenDict(dict[str, Any]):
    """A ``dict`` that cannot change once built: every mutator raises
    ``TypeError``.

    Issue #93: a ``MappingProxyType`` is read-only too, but it cannot be
    pickled or deep-copied, so neither could any ``Agent`` (every one has
    at least an empty ``skill_contents``), and ``dataclasses.asdict``
    failed with it. A ``dict`` subclass pickles through ``__reduce__``,
    deep-copies, and ``asdict`` rebuilds it like any other ``dict``.

    The content is set in ``__new__``, and ``__init__`` does nothing:
    ``dict.__init__`` merges into an existing instance, so calling it a
    second time would otherwise be a way around the blocked mutators.
    """

    __slots__ = ()

    def __new__(cls, *args: Any, **kwargs: Any) -> Self:
        self = super().__new__(cls)
        dict.update(self, *args, **kwargs)
        return self

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def __reduce__(self) -> tuple[Any, ...]:
        return (type(self), (dict(self),))

    def _immutable(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise TypeError(f"'{type(self).__name__}' object is immutable")

    __setitem__ = __delitem__ = _immutable
    clear = pop = popitem = setdefault = update = __ior__ = _immutable


def _freeze(value: Any) -> Any:
    """A deep, immutable copy of ``value``: every mapping becomes a
    ``_FrozenDict``, every list or tuple a ``tuple``, every set a
    ``frozenset``, nested values included. Anything else (a ``str``, a
    number, a path, another ``Agent``) is already immutable and is returned
    as is.

    ``frozen=True`` only blocks reassigning a field. Without this copy, a
    list or dict the caller passed in stays shared with the caller, and
    mutating it afterwards changes every later ``_to_locator()`` -- including
    one reached through another ``Agent``'s ``extends=``."""
    if isinstance(value, Mapping):
        return _FrozenDict({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set | frozenset):
        return frozenset(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    """The inverse of ``_freeze``, as a fresh copy: mappings become plain
    ``dict``s and tuples plain ``list``s, nested values included -- the
    shapes the loader parses out of YAML and checks for (a tuple where it
    expects a list would be skipped or misread). Each call returns new
    containers, so nothing the loader does to them can reach back into the
    ``Agent``."""
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


# ---------------------------------------------------------------------------
# Parameter checks (issue #93): the rules the YAML path applies, reused
# ---------------------------------------------------------------------------

_STR_LIST_PARAMS = frozenset({"tools", "skills"})
_MAPPING_PARAMS = frozenset(
    {
        "skill_contents",
        "context",
        "escalation_rules",
        "delegation_policy",
        "memory_policy",
        "audit_policy",
    }
)
#: The two shapes `_resolve_list_directive` actually honours. Any other
#: mapping resolves to the parent's full set, so a typo would keep a
#: permission it meant to remove.
_INHERIT_DIRECTIVE_KEYS = frozenset({"inherit", "add", "remove"})


def _str_list(who: str, name: str, value: Any) -> tuple[str, ...]:
    """A list parameter, read as the loader reads the YAML field
    (``_as_str_list``): ``None`` is empty, and a bare string is a one-item
    list -- it used to be split into its characters. Anything but a string or
    a list/tuple of strings raises, rather than being stringified into a
    name nothing matches."""
    if not (
        value is None
        or isinstance(value, str)
        or (isinstance(value, list | tuple) and all(isinstance(v, str) for v in value))
    ):
        raise DefinitionError(
            f"Invariant violation — {name}: {who} declares {name}={value!r} "
            f"({type(value).__name__}), which is not a list of strings. A "
            f"single string counts as a one-item list, as in YAML."
        )
    return tuple(_as_str_list(value))


def _permissions(who: str, value: Any) -> Any:
    """``permissions`` takes the three shapes a manifest does: a list, the
    string ``"inherit"``, or a directive mapping. A mapping is passed through
    to the loader's ``_resolve_list_directive``, like the folder path does;
    it used to be iterated, which turned its keys into the permissions
    ``'inherit'`` and ``'remove'`` and ignored the removal."""
    if isinstance(value, Mapping):
        keys = set(value)
        if not (
            (value.get("inherit") is True and keys <= _INHERIT_DIRECTIVE_KEYS)
            or keys == {"override"}
        ):
            raise DefinitionError(
                f"Invariant violation — permissions: {who} declares the "
                f"directive {dict(value)!r}, which the loader does not "
                f"recognise and would resolve to the parent's full set. Use "
                f"{{'inherit': True, 'add': [...], 'remove': [...]}} or "
                f"{{'override': [...]}}."
            )
        return {
            key: item
            if key == "inherit"
            else _str_list(who, f"permissions.{key}", item)
            for key, item in value.items()
        }
    if value == "inherit":
        return value
    return _str_list(who, "permissions", value)


def _mapping(who: str, name: str, value: Any) -> Mapping[str, Any]:
    """A mapping parameter: ``None`` is empty, as ``dict(field or {})`` reads
    a YAML ``null``, and anything else that is not a mapping raises."""
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise DefinitionError(
            f"Invariant violation — {name}: {who} declares {name}={value!r} "
            f"({type(value).__name__}), which is not a mapping."
        )
    if name == "skill_contents" and not all(
        isinstance(key, str) and isinstance(text, str) for key, text in value.items()
    ):
        raise DefinitionError(
            f"Invariant violation — skill_contents: {who} declares "
            f"skill_contents={dict(value)!r}, which does not map each skill "
            f"name to its text (both strings)."
        )
    return value


def _checked(who: str, params: Mapping[str, Any]) -> dict[str, Any]:
    """``params`` with every field the loader parses checked and brought to
    the shape the YAML path gives it; any other key is passed through.

    One function for both forms, so ``Agent(...)`` and
    ``Agent.from_folder(path, **overrides)`` enforce the same rules.
    ``untrusted_input`` goes through the loader's own
    ``_parse_untrusted_input``: only a real bool is accepted (``'false'``
    and ``'no'`` used to resolve to True), and a key present with ``None``
    is ambiguous with not passing it, exactly like ``untrusted_input: null``
    in ``policy.md``.
    """
    checked = dict(params)
    for name, value in params.items():
        if name in _STR_LIST_PARAMS:
            checked[name] = _str_list(who, name, value)
        elif name in _MAPPING_PARAMS:
            checked[name] = _mapping(who, name, value)
        elif name == "permissions":
            checked[name] = _permissions(who, value)
    _parse_untrusted_input(params, source=who)
    return checked


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

    ``skill_contents`` carries inline skill text (design.md D4, PR3): it is
    validated here (every key must also be listed in ``skills``) and, at
    ``_to_locator()`` time, threaded onto the built ``RawDefinition``'s
    ``inline_skills`` — the carrier ``_load_skills`` (``harness/factory.py``)
    checks first, ahead of an importer's own folder and any deployment
    override.

    Parameters are checked at construction the way the loader checks the
    same fields in YAML (issue #93, ``_checked``), for ``Agent(...)`` and for
    ``from_folder``'s overrides alike: ``untrusted_input`` must be a real
    bool, a bare string for ``tools``/``skills``/``permissions`` is a one-item
    list, and ``permissions`` also takes ``"inherit"`` or a directive
    mapping (``{"inherit": True, "remove": [...]}``, ``{"override": [...]}``).
    A bad value raises ``DefinitionError`` naming the agent, the field and
    the reason.
    """

    name: str
    extends: str | Agent = "platform/roles/agent"
    tools: tuple[str, ...] = ()
    permissions: tuple[str, ...] | Mapping[str, Any] | str = ()
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
        who = f"agent '{self.name}'"
        own = {name: getattr(self, name) for name in _CHECKED_FIELDS}
        if own["untrusted_input"] is None:
            # The default, meaning "not declared": only an explicit value is
            # parsed. A `from_folder` override of None still is (see below).
            del own["untrusted_input"]
        for name, value in _checked(who, own).items():
            object.__setattr__(self, name, value)
        _validate_inline_skills(self.name, self.skills, self.skill_contents)

        if self._folder is not None:
            unknown = set(self._folder_overrides) - _AGENT_OVERRIDABLE_FIELDS
            if unknown:
                raise DefinitionError(
                    f"Agent.from_folder: unknown override(s) {sorted(unknown)}"
                )
            overrides = _checked(who, self._folder_overrides)
            if "skill_contents" in overrides and "skills" in overrides:
                # Both sides are known without reading the folder. Otherwise
                # the folder's own `skills` decide, and the loader runs the
                # same check once it has read them
                # (`_apply_agent_folder_overrides`).
                _validate_inline_skills(
                    self.name, overrides["skills"], overrides["skill_contents"]
                )
            object.__setattr__(self, "_folder_overrides", overrides)

        # Deep-immutable, not just frozen (see `_freeze`). Every field goes
        # through it, so a container field added later is covered too.
        for field in dataclasses.fields(self):
            object.__setattr__(self, field.name, _freeze(getattr(self, field.name)))

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
            # The importer root is the folder's parent (issue #75 checks the
            # folder against it). Taken from the lexically absolute path:
            # `Path(".").parent` is `Path(".")`, which would make "." its
            # own root.
            return FolderLocator(
                path=self._folder,
                root=pathlib.Path(os.path.abspath(self._folder)).parent,
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
            # A list, "inherit", or a directive dict: `_fold_parent_into_child`
            # resolves the last two with `_resolve_list_directive`.
            permissions=_thaw(self.permissions),
            autonomy=self.autonomy,
            escalation_rules=_thaw(self.escalation_rules),
            delegation_policy=_thaw(self.delegation_policy),
            memory_policy=_thaw(self.memory_policy),
            audit_policy=_thaw(self.audit_policy),
            execution_limits=_thaw(self.execution_limits),
            untrusted_input=self.untrusted_input,
            inline_skills=dict(self.skill_contents),
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

#: The `Agent` fields `_checked` has a rule for.
_CHECKED_FIELDS: frozenset[str] = (
    _STR_LIST_PARAMS | _MAPPING_PARAMS | {"permissions", "untrusted_input"}
)
