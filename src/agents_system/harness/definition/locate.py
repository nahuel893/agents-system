"""Locating a definition: role and deployment folders, the target of an ``extends:`` value, and how a locator is named in messages."""

from __future__ import annotations

import os
import pathlib
import re
import warnings
from typing import Any

import structlog

from agents_system.harness.definition.errors import (
    DefinitionError,
    PredefinedRoleDeprecationWarning,
)
from agents_system.harness.definition.frontmatter import (
    _split_frontmatter,
)
from agents_system.harness.definition.fsread import (
    _is_absolute_value,
    _read_within_root,
    _resolve_within_root,
)
from agents_system.harness.definition.model import (
    FolderLocator,
    InlineLocator,
    RoleLocator,
)
from agents_system.harness.definition.roots import (
    RootConfig,
    _require_platform_root,
)

logger = structlog.get_logger()


#: ADR-010 Migration step 1 (#222): the concrete predefined roles, each
#: copied to ``examples/agents/<name>/`` as a reference agent. Resolving one
#: by name warns (``_warn_deprecated_role``).
_DEPRECATED_PREDEFINED_ROLES = frozenset(
    {
        "accountant-agent",
        "data-agent",
        "developer-agent",
        "operator-agent",
        "orchestrator",
        "sales-agent",
        "summary-agent",
        "support-agent",
    }
)


#: Where the reference agents live, relative to the repository root. They are
#: not in the wheel: an adopter copies one into their own project.
_REFERENCE_AGENTS_FOLDER = "examples/agents"


#: This package's directory. A deprecation warning is attributed to the first
#: frame outside it, so it points at the caller's own line.
_PACKAGE_DIR = str(pathlib.Path(__file__).parent.parent.parent) + os.sep


def _warn_deprecated_role(role_type: str) -> None:
    """Warn that ``role_type``, a concrete predefined role named by the
    caller, is deprecated, and name the reference agent to copy.

    Also logged: Python's default filters show a ``DeprecationWarning`` only
    when it is attributed to ``__main__``, so a served app registering the
    role by name would otherwise never show it.
    """
    copy_from = f"{_REFERENCE_AGENTS_FOLDER}/{role_type}/"
    logger.warning("role.deprecated", role=role_type, copy_from=copy_from)
    warnings.warn(
        f"Predefined role '{role_type}' is deprecated and will be removed from "
        f"platform/roles/ in the next breaking release (ADR-010). Copy the "
        f"reference agent {copy_from} from the agents-system repository into "
        "your project and load it with Agent.from_folder(path) instead of "
        "naming the role.",
        PredefinedRoleDeprecationWarning,
        skip_file_prefixes=(_PACKAGE_DIR,),
    )


# A role or client name is a single directory name, nothing else. Anything
# with a separator, a dot segment, or a leading dash is rejected before it can
# reach the filesystem.
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def _validate_segment(value: str, *, kind: str) -> str:
    """Reject any name that could escape the root it is joined to.

    These names are joined onto ``platform_root``/``deployments_root`` to build
    filesystem paths, and since D-024 they arrive from consuming applications —
    a tenant slug, a user-selected agent type. A traversing ``role_type`` does
    not *override* a role, it REPLACES the generic one, so none of ``merge``'s
    subset invariants apply: the traversed manifest becomes the parent and can
    declare any tools, any permissions and ``autonomy: full``.

    Validate here rather than in the callers so every path-building site is
    covered by construction.
    """
    if not _SAFE_SEGMENT.fullmatch(value):
        raise DefinitionError(
            f"Invalid {kind} {value!r}. Must match {_SAFE_SEGMENT.pattern} — "
            "a single directory name, with no path separators or dot segments."
        )
    return value


def _role_folder(platform_root: pathlib.Path, role_type: str) -> pathlib.Path:
    return platform_root / "roles" / _validate_segment(role_type, kind="role_type")


def _deployment_folder(
    deployments_root: pathlib.Path, client: str, role_type: str
) -> pathlib.Path:
    return (
        deployments_root
        / _validate_segment(client, kind="client")
        / _validate_segment(role_type, kind="role_type")
    )


_PLATFORM_ROLE_PREFIX = "platform/roles/"


_EXTENDS_RULE = (
    "A bare name or 'platform/roles/<name>' resolves only in the predefined "
    "role tree; any other value is a folder path relative to the declaring "
    "agent's own folder and must stay inside its importer root."
)


def _platform_role_name_or_none(value: str) -> str | None:
    """The role name when ``value`` is one of the two platform forms every
    shipped manifest writes — a bare segment or ``platform/roles/<segment>``
    — else ``None``. Anything else is never a platform-role reference."""
    name = value.removeprefix(_PLATFORM_ROLE_PREFIX)
    return name if _SAFE_SEGMENT.fullmatch(name) else None


def _shown_path(root: pathlib.Path, path: pathlib.Path) -> str:
    """``path`` relative to ``root``, for an error message (issue #75).

    Loader errors can reach whoever supplied the agent folder, so they name
    a file by where it sits in its root, never by its host path. Falls back
    to the last segment when ``path`` is not under ``root`` at all.
    """
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        pass
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, RuntimeError, ValueError):
        return path.name


def _describe_locator(locator: RoleLocator) -> str:
    """Name the agent declaring an ``extends:`` value, for error messages."""
    if isinstance(locator, FolderLocator):
        return f"agent folder '{_shown_path(locator.root, locator.path)}'"
    if isinstance(locator, InlineLocator):
        return f"inline agent '{locator.raw.role_name}'"
    return f"role '{locator}'"


def _contained_folder(locator: FolderLocator) -> pathlib.Path:
    """The real path of ``locator.path``, which must be an existing folder
    strictly inside ``locator.root`` once ``..`` and symlinks are followed
    (issue #75). ``Agent.from_folder(path)`` sets ``root=path.parent``, and
    an ``extends:`` target already passed ``_extends_target``; a
    ``FolderLocator`` built by hand is held to the same rule."""
    label = _describe_locator(locator)
    folder = _resolve_within_root(locator.root, locator.path, ".")
    if folder is None:
        raise DefinitionError(
            f"Invariant violation — containment: {label} is not inside its "
            "importer root: after following '..' and symlinks, an agent "
            "folder must land strictly inside the root it was given."
        )
    # Issue #127 (PR 97 review follow-up) — `is_dir()` calls `os.stat()`,
    # which needs search permission on every ANCESTOR of `folder`, never on
    # `folder` itself: a directory ABOVE it that momentarily lacks search
    # permission (e.g. a chmod-000 sibling-of-root folder) raises
    # `PermissionError` here rather than returning `False`. Unguarded, that
    # escapes as a raw `PermissionError` instead of the loader's own
    # `DefinitionError` contract.
    try:
        is_folder = folder.is_dir()
    except OSError as exc:
        raise DefinitionError(
            f"Invariant violation — containment: {label} could not be "
            f"checked ({exc.strerror}): an ancestor directory may lack "
            "search permission."
        ) from exc
    if not is_folder:
        raise DefinitionError(f"{label} does not exist or is not a folder.")
    return folder


def _read_contained_md(
    locator: FolderLocator, folder: pathlib.Path, filename: str, *, shown: pathlib.Path
) -> tuple[dict[str, Any], str]:
    """``_read_md`` for an importer folder: ``folder/filename`` must resolve
    inside ``locator.root`` (symlinks followed first, as for ``extends:``),
    and is then read through ``_read_within_root``. Issue #75: ``role.md``,
    ``manifest.md`` and ``policy.md`` used to be read wherever a symlink
    pointed."""
    label = _describe_locator(locator)
    target = _resolve_within_root(locator.root, folder, filename)
    if target is None:
        raise DefinitionError(
            f"Invariant violation — containment: {label} has a {filename} that "
            "resolves outside its importer root after following '..' and "
            "symlinks. Every file an agent folder supplies must stay inside "
            "that root."
        )
    try:
        text = _read_within_root(locator.root, target)
    except FileNotFoundError:
        raise DefinitionError(
            f"Required definition file not found: {(shown / filename).as_posix()}"
        ) from None
    except OSError as exc:
        raise DefinitionError(
            f"Invariant violation — containment: {label} has a {filename} that "
            f"could not be read inside its importer root ({exc.strerror}). A "
            "path that became a symlink after it was checked, or anything but "
            "a regular file, is refused."
        ) from exc
    return _split_frontmatter(text)


def _extends_target(
    raw: Any,
    *,
    current: RoleLocator,
    roots: RootConfig,
    declared_by: str | None = None,
) -> RoleLocator:
    """Place an ``extends:`` value declared by ``current`` in exactly one
    locator space, or raise ``DefinitionError`` (design.md D2).

    - bare name / ``platform/roles/<name>``: the predefined role tree only,
      never an importer folder of the same name;
    - anything else: a path relative to ``current``'s own folder, accepted
      only when ``current`` is a ``FolderLocator`` and the fully resolved
      path is an existing folder strictly inside ``current.root``. The
      returned locator carries the resolved real path, so later reads use
      what was checked instead of re-walking symlinks.

    Absolute values are rejected before any filesystem access. Nothing is
    ever collapsed to its last segment (the pre-ADR-004 behavior), which is
    what let ``some/importer/path/agent`` silently extend ``agent``. Spec:
    agent-definition-locator, "`extends:` fails loudly when unplaceable in
    either locator space".

    ``declared_by`` names where the value came from in error messages when
    it was not ``current``'s own ``extends:`` (an ``Agent.from_folder``
    override); it changes nothing else.
    """
    source = declared_by or _describe_locator(current)

    def fail(detail: str) -> DefinitionError:
        return DefinitionError(
            f"Invariant violation — extends: {source} "
            f"declares {detail}. {_EXTENDS_RULE}"
        )

    if raw is None:
        raise fail("'extends:' with no value (omit the key when there is no parent)")
    if not isinstance(raw, str):
        raise fail(f"an 'extends:' value of type {type(raw).__name__}, not a string")
    value = raw.strip()
    if not value:
        raise fail("an empty 'extends:' value")
    if _is_absolute_value(value):
        raise fail(f"'extends: {value}', which is an absolute path")
    value = value.rstrip("/")

    platform_name = _platform_role_name_or_none(value)
    if platform_name is not None:
        folder = _role_folder(
            _require_platform_root(roots.platform_root), platform_name
        )
        # Issue #127 — see `_contained_folder`'s matching comment: an
        # ancestor directory lacking search permission raises `OSError` from
        # `is_dir()` itself, which must not escape past `DefinitionError`.
        try:
            platform_folder_exists = folder.is_dir()
        except OSError as exc:
            raise fail(
                f"'extends: {value}', which names predefined role "
                f"'{platform_name}', whose folder could not be checked "
                f"({exc.strerror})"
            ) from exc
        if platform_folder_exists:
            return platform_name
        raise fail(
            f"'extends: {value}', which names predefined role "
            f"'{platform_name}', but the platform root has no "
            f"roles/{platform_name}"
        )

    if not isinstance(current, FolderLocator):
        raise fail(
            f"'extends: {value}', a folder path, but it was not loaded from a "
            "folder, so it has no importer root to resolve the path in"
        )
    resolved = _resolve_within_root(current.root, current.path, value)
    if resolved is None:
        raise fail(
            f"'extends: {value}', which escapes the importer root: after "
            "following '..' and symlinks it must land strictly inside that root"
        )
    if resolved == current.path.resolve():
        raise fail(f"'extends: {value}', which is the declaring agent's own folder")
    # Issue #127 — same guard as the platform branch above: an ancestor of
    # `resolved` lacking search permission raises `OSError` from `is_dir()`.
    try:
        resolved_is_dir = resolved.is_dir()
    except OSError as exc:
        raise fail(
            f"'extends: {value}', whose target could not be checked ({exc.strerror})"
        ) from exc
    if not resolved_is_dir:
        raise fail(
            f"'extends: {value}', which is not an existing folder inside the "
            "importer root"
        )
    return FolderLocator(path=resolved, root=current.root)


def _extends_override_target(locator: FolderLocator, roots: RootConfig) -> RoleLocator:
    """The parent an ``Agent.from_folder(path, extends=...)`` override asks
    for, used in place of the folder manifest's own ``extends:`` (design.md
    D3: an explicit parameter replaces the folder's value).

    An ``Agent`` value arrives already turned into its locator by
    ``Agent._to_locator()`` and is used as is. Anything else is placed by
    ``_extends_target`` exactly like a manifest value, relative to this
    folder, so it fails loud on the same inputs (``None``, empty, absolute,
    escaping the importer root, the folder itself).

    This override used to be accepted and then dropped, which resolved the
    agent under the folder's own parent, or none: it lost the requested
    parent's ``untrusted_input: true`` (the R4 T3 barrier) and its tighter
    execution limits. The chain walk now holds the folder to that parent's
    ceiling like any importer-authored child.
    """
    value = locator.overrides["extends"]
    if isinstance(value, FolderLocator | InlineLocator):
        return value
    return _extends_target(
        value,
        current=locator,
        roots=roots,
        declared_by=(
            f"the Agent.from_folder extends= override for {_describe_locator(locator)}"
        ),
    )


def _locator_key(locator: RoleLocator) -> str:
    """Stable cycle-detection identity for a ``RoleLocator`` (design.md D2
    case #7).

    A platform-role name and an importer-folder path can never collide
    (distinct prefixes), and two inline definitions are distinguished by
    Python object identity — the same ``InlineLocator.raw`` object
    reappearing in a walked chain is the only way an inline definition can
    cycle, since it carries no path or name of its own to compare by value.
    """
    if isinstance(locator, FolderLocator):
        return f"folder:{locator.path.resolve()}"
    if isinstance(locator, InlineLocator):
        return f"inline:{id(locator.raw)}"
    return f"platform:{locator}"


def _locator_label(locator: RoleLocator) -> str:
    """``_locator_key``'s shape for error messages (issue #75): a folder is
    named relative to its importer root, not by host path, and an inline
    definition by its role name."""
    if isinstance(locator, FolderLocator):
        return f"folder:{_shown_path(locator.root, locator.path)}"
    if isinstance(locator, InlineLocator):
        return f"inline:{locator.raw.role_name}"
    return f"platform:{locator}"
