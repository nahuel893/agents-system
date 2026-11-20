"""Agent registration parsing, validation and the boot plan data."""

from __future__ import annotations

import dataclasses
import os
import pathlib
from collections.abc import (
    Mapping,
)
from typing import Any

from agents_system.agent.spec import Agent
from agents_system.harness.definition.locate import _SAFE_SEGMENT
from agents_system.harness.loader import DefinitionError, RoleLocator
from agents_system.harness.registry import ToolRegistry
from agents_system.services.retry_policy import OutboxRetryPolicy

_MAX_RUNTIME_ID_LENGTH = 64


def _bounded_repr(value: str) -> str:
    """``repr(value)``, truncated to ``_MAX_RUNTIME_ID_LENGTH`` characters of
    *value* itself, plus a trailing character count, when longer.

    issue #128 item 2 -- every boot error that echoes a caller-supplied,
    not-yet-validated id back (an unregistered ``clients``/``grants`` key, an
    unmatched ``WHATSAPP_RUNTIME_ID``/``ADAPTER_RUNTIMES`` id) must go
    through this, never a bare ``!r}``: those ids reach the error message
    specifically BECAUSE they failed validation, so nothing has bounded
    their length yet either. Reused by ``_validate_runtime_id`` and
    ``_parse_agent_registration``, which used to each inline the same
    truncate-then-repr logic.
    """
    if len(value) <= _MAX_RUNTIME_ID_LENGTH:
        return repr(value)
    return f"{value[:_MAX_RUNTIME_ID_LENGTH]!r}... ({len(value)} characters)"


def _validate_runtime_id(runtime_id: str) -> str:
    """Reject a runtime id that is empty, longer than
    ``_MAX_RUNTIME_ID_LENGTH``, or contains anything other than letters,
    digits, underscore or hyphen (design.md D6).

    A runtime id is deployer-chosen (design.md D5) and becomes part of
    URLs (``/v1/models``), structured logs and metrics labels once
    registered -- the same class of user-supplied, filesystem-adjacent
    segment ``locate._SAFE_SEGMENT`` already validates elsewhere in this
    codebase (a role type, a client/deployment name), reused here for the
    same reason: consistency, and because an id built from it must be safe
    to embed in a URL path segment and a log line with no further escaping.
    Malformed ids fail here, at registration time, rather than surfacing as
    a confusing lookup miss at first request.
    """
    if not isinstance(runtime_id, str):
        raise DefinitionError(
            f"Invalid runtime id of type {type(runtime_id).__name__}: a "
            "runtime id must be a str."
        )
    if len(runtime_id) > _MAX_RUNTIME_ID_LENGTH:
        # Never echo the whole value: the length is the problem.
        raise DefinitionError(
            f"Invalid runtime id {_bounded_repr(runtime_id)}. A runtime id "
            f"is at most {_MAX_RUNTIME_ID_LENGTH} characters."
        )
    if not _SAFE_SEGMENT.fullmatch(runtime_id):
        raise DefinitionError(
            f"Invalid runtime id {runtime_id!r}. Must match "
            f"{_SAFE_SEGMENT.pattern} -- letters, digits, underscore and "
            "hyphen only, non-empty, not starting with '-' or '_'."
        )
    return runtime_id


def _validate_client(runtime_id: str, client: object) -> str:
    """Reject a ``clients`` value that is not a client name: a ``str``
    matching ``locate._SAFE_SEGMENT``, the rule the loader applies when it
    joins that name onto ``deployments_root``.

    A ``None`` (``os.environ.get(...)`` for an unset variable) would
    otherwise pass as "no client" and serve the role WITHOUT its
    subtractive override, silently. The error names the id, never the
    value.
    """
    if not isinstance(client, str):
        raise DefinitionError(
            f"clients[{runtime_id!r}] must be a deployment client name (str), "
            f"got {type(client).__name__}. Refusing to boot."
        )
    if not _SAFE_SEGMENT.fullmatch(client):
        raise DefinitionError(
            f"clients[{runtime_id!r}] is not a valid deployment client name. "
            f"It must match {_SAFE_SEGMENT.pattern} -- letters, digits, "
            "underscore and hyphen only, non-empty, not starting with '-' or "
            "'_'. Refusing to boot."
        )
    return client


def _parse_agent_registration(value: str) -> tuple[str, str | None]:
    """Read one ``AGENT_REGISTRATIONS`` value: ``"{role}"`` or
    ``"{role}@{client}"`` -> ``(role, client or None)`` (design.md D5/D6).

    ``main.py``'s own env-boot convention, not a public string format: a
    library caller registers ``Agent``/``str`` objects through
    ``create_app(agents=...)`` instead. Strict -- at most one ``@``, and
    both the role and the client must match ``locate._SAFE_SEGMENT`` AND be
    at most ``_MAX_RUNTIME_ID_LENGTH`` characters (issue #128 item 6: a
    segment made only of legal characters but far longer than any real role
    or client name previously passed straight through here, to overflow
    into a raw ``OSError`` -- ``ENAMETOOLONG`` -- once it reached a real
    filesystem join in ``locate._role_folder``/``_deployment_folder``,
    instead of this function's own bounded ``DefinitionError``); anything
    else fails boot naming the value, cut to ``_MAX_RUNTIME_ID_LENGTH``
    characters. Nothing else in the value has a meaning: ``__`` in
    particular is part of the role name, never a separator.
    """
    if not isinstance(value, str):
        raise DefinitionError(
            f"Invalid agent registration of type {type(value).__name__}: "
            'expected "{role}" or "{role}@{client}".'
        )
    role, separator, client = value.partition("@")
    role_or_client_too_long = len(role) > _MAX_RUNTIME_ID_LENGTH or (
        separator and len(client) > _MAX_RUNTIME_ID_LENGTH
    )
    if (
        role_or_client_too_long
        or not _SAFE_SEGMENT.fullmatch(role)
        or (separator and not _SAFE_SEGMENT.fullmatch(client))
    ):
        raise DefinitionError(
            f"Invalid agent registration {_bounded_repr(value)}. Expected "
            '"{role}" or "{role}@{client}", where the role and the client '
            f"each match {_SAFE_SEGMENT.pattern}, are each at most "
            f"{_MAX_RUNTIME_ID_LENGTH} characters, and '@' appears at most "
            "once."
        )
    return role, (client if separator else None)


@dataclasses.dataclass(frozen=True)
class _Registration:
    """One runtime the lifespan builds (design.md D5): what to resolve, and
    the deployment client (if any) to resolve it for."""

    locator: RoleLocator
    client: str | None
    subject: str
    """``role 'sales-agent'`` or ``agent 'triage-bot'`` -- names it in boot errors."""


@dataclasses.dataclass(frozen=True)
class _BootPlan:
    """Every runtime to build, keyed by runtime id, and where its grants come from."""

    registrations: dict[str, _Registration]
    grants: Mapping[str, Any]
    grant_source: str
    """``create_app(grants=...)``, or ``DEPLOY_GRANTS`` when it got none."""
    execution_limits: dict[str, dict[str, int | float]]
    """#278 -- each runtime's deployment-set limits, checked against the lease."""
    timeout_replies: dict[str, str]
    """#278 -- each runtime's timeout reply; absent means the default text."""
    tool_error_replies: dict[str, str]
    """#289 -- each runtime's reply when tool errors reach the cap; absent
    means the default text."""
    retry_policies: dict[str, OutboxRetryPolicy]
    """#290 -- each runtime's outbox retry policy; absent means the defaults."""


def _refuse_builtin_default_tools(registry: ToolRegistry) -> None:
    """Issue #168 -- never serve a built-in default tool.

    ``build_runtime`` fills a missing ``escalation_notifier`` for in-process
    use with a default that reaches no human. The lifespan builds with
    ``builtin_defaults=False``; this closes the other way in, the caller's
    registry holding the default because it was registered by hand. A
    customer asking for a person must never be answered by it while the
    operator believes escalation is wired.
    """
    from agents_system.harness.defaults import is_builtin_default

    served_defaults = sorted(
        name for name in registry.names() if is_builtin_default(registry.get(name))
    )
    if served_defaults:
        raise DefinitionError(
            f"The tool registry holds the built-in default for "
            f"{', '.join(served_defaults)}, which reaches no human. It is for "
            "in-process use only: register a ToolSpec that delivers to a real "
            "channel. Refusing to boot."
        )


def _build_registrations(
    agents: Mapping[str, Agent | str],
    clients: Mapping[str, str] | None,
) -> dict[str, _Registration]:
    """``create_app(agents=...)``, or ``AGENT_REGISTRATIONS`` read into the
    same shape: each id is opaque and validated, never parsed. A ``str`` is
    a predefined-role name; an ``Agent`` is its own locator."""
    clients = clients or {}
    # A client names a subtractive deployment override: one keyed by a
    # typo'd id would silently serve that agent WITHOUT its narrowing.
    # issue #128 item 2 -- `key` is exactly what failed the membership check
    # below, so it is not yet a validated runtime id and may be unbounded.
    unknown = [_bounded_repr(key) for key in clients if key not in agents]
    if unknown:
        raise DefinitionError(
            f"clients names runtime id(s) {', '.join(unknown)} that agents "
            "does not register. Refusing to boot."
        )
    registrations: dict[str, _Registration] = {}
    for runtime_id, agent in agents.items():
        _validate_runtime_id(runtime_id)
        if isinstance(agent, Agent):
            if runtime_id in clients:
                # design.md D5 / Q5: deployment overrides stay
                # predefined-role-only. Fail loud, not a silent no-op.
                raise DefinitionError(
                    f"clients[{runtime_id!r}] is set, but agents[{runtime_id!r}] "
                    f"is an Agent ({agent.name!r}). A client deployment "
                    "override applies only to a predefined role registered "
                    "by name (a str). Refusing to boot."
                )
            registrations[runtime_id] = _Registration(
                agent._to_locator(), None, f"agent {agent.name!r}"
            )
        elif isinstance(agent, str):
            client = (
                _validate_client(runtime_id, clients[runtime_id])
                if runtime_id in clients
                else None
            )
            registrations[runtime_id] = _Registration(agent, client, f"role {agent!r}")
        else:
            raise DefinitionError(
                f"agents[{runtime_id!r}] must be an Agent or a predefined role "
                f"name (str), got {type(agent).__name__}."
            )
    return registrations


#: ``AGENT_REGISTRATIONS`` value prefix naming an agent folder under
#: ``AGENTS_ROOT`` instead of a predefined role (ADR-010, #226).
_FOLDER_PREFIX = "folder:"


def _folder_registration(value: str, agents_root: str) -> Agent:
    """Read a ``"folder:<name>"`` registration into an ``Agent``.

    The root is the operator's (``AGENTS_ROOT``); the value is not trusted.
    It must be one folder name directly under the root -- an absolute path,
    a ``..`` segment or any separator fails the name rule -- and the folder,
    once symlinks are followed, must still lie strictly inside the root.
    An unset or relative root fails closed. The loader then holds every file
    the folder supplies (and a sibling ``extends:``) to the same root.
    """
    name = value[len(_FOLDER_PREFIX) :]
    if not agents_root or not os.path.isabs(agents_root):
        raise DefinitionError(
            "a folder registration needs AGENTS_ROOT set to an absolute "
            "directory path; it is unset or relative."
        )
    if len(name) > _MAX_RUNTIME_ID_LENGTH or not _SAFE_SEGMENT.fullmatch(name):
        raise DefinitionError(
            f"Invalid folder registration {_bounded_repr(value)}. Expected "
            f'"{_FOLDER_PREFIX}<name>", one folder name directly under '
            f"AGENTS_ROOT matching {_SAFE_SEGMENT.pattern} (no path "
            "separators, no '..', at most "
            f"{_MAX_RUNTIME_ID_LENGTH} characters)."
        )
    try:
        real_root = pathlib.Path(agents_root).resolve(strict=True)
        folder = (real_root / name).resolve(strict=True)
    except (OSError, RuntimeError):
        raise DefinitionError(
            f"folder {name!r} is not a directory under AGENTS_ROOT."
        ) from None
    if folder == real_root or not folder.is_relative_to(real_root):
        raise DefinitionError(
            f"folder {name!r} resolves outside AGENTS_ROOT (a symlink leads out of it)."
        )
    if not folder.is_dir():
        raise DefinitionError(f"folder {name!r} is not a directory under AGENTS_ROOT.")
    return Agent.from_folder(real_root / name)


def _settings_agents(
    agent_registrations: Mapping[str, str],
    agents_root: str = "",
) -> tuple[dict[str, Agent | str], dict[str, str]]:
    """The Settings-driven fallback, used when ``create_app`` got no
    ``agents``: ``AGENT_REGISTRATIONS`` read into the ``agents``/``clients``
    shape ``create_app`` takes, so both paths build through one loop
    (design.md D5). A malformed id or value fails boot, naming it."""
    agents: dict[str, Agent | str] = {}
    clients: dict[str, str] = {}
    for runtime_id, value in agent_registrations.items():
        try:
            _validate_runtime_id(runtime_id)
        except DefinitionError as exc:
            raise DefinitionError(
                f"AGENT_REGISTRATIONS: {exc} Refusing to boot."
            ) from exc
        if isinstance(value, str) and value.startswith(_FOLDER_PREFIX):
            try:
                agents[runtime_id] = _folder_registration(value, agents_root)
            except DefinitionError as exc:
                raise DefinitionError(
                    f"AGENT_REGISTRATIONS[{runtime_id!r}]: {exc} Refusing to boot."
                ) from exc
            continue
        try:
            role, client = _parse_agent_registration(value)
        except DefinitionError as exc:
            raise DefinitionError(
                f"AGENT_REGISTRATIONS[{runtime_id!r}]: {exc} Refusing to boot."
            ) from exc
        agents[runtime_id] = role
        if client is not None:
            clients[runtime_id] = client
    return agents, clients
