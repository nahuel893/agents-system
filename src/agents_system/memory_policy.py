"""Enforcement of a role's `memory_policy` (#26, ADR-002 A.5).

`memory_policy` is declared in `policy.md` (`read_scope`, `write_scope`,
`persist_conversation`) and the loader carries it through every merge as a
plain mapping. This module turns that mapping into a decision the platform
makes, never the model:

- a tool that declares `ToolSpec.memory_access` is refused by the interceptor
  when its `memory_scope` is wider than the policy's `read_scope` or
  `write_scope` allows;
- `AgentRuntime` and the channel worker do not read or write a conversation
  thread when `persist_conversation` is false.

An undeclared policy means the schema's own defaults: `local` for both scopes
and a persisted conversation, which is today's behavior. A policy that does
not parse fails closed (`MemoryPolicyError`): the callers deny the memory
access and persist nothing, rather than guess.

This module has no dependency on the rest of the package.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any

#: Scopes from narrowest to widest; a policy allowing one allows the narrower.
SCOPES: tuple[str, ...] = ("local", "team", "org")

#: The shipped `policy.md` files (the base role and every reference agent)
#: spell the narrowest and widest scopes `session` and `deployment`. They are
#: accepted as spellings of `local` and `org`.
_ALIASES: Mapping[str, str] = {"session": "local", "deployment": "org"}

_FIELDS = frozenset({"read_scope", "write_scope", "persist_conversation"})


class MemoryPolicyError(ValueError):
    """The declared `memory_policy` does not match its schema."""


def normalize_scope(value: object) -> str | None:
    """The canonical scope for a spelling, or ``None`` when it is not one."""
    if not isinstance(value, str):
        return None
    value = _ALIASES.get(value, value)
    return value if value in SCOPES else None


def _scope(raw: Mapping[str, Any], field: str) -> str:
    scope = normalize_scope(raw.get(field, "local"))
    if scope is None:
        raise MemoryPolicyError(
            f"memory_policy.{field} must be one of {SCOPES} "
            f"(or the spellings {tuple(_ALIASES)})"
        )
    return scope


def _canonical(scope: str) -> str:
    canonical = normalize_scope(scope)
    if canonical is None:
        raise MemoryPolicyError(f"unknown memory scope {scope!r}")
    return canonical


@dataclasses.dataclass(frozen=True)
class MemoryPolicy:
    read_scope: str = "local"
    write_scope: str = "local"
    persist_conversation: bool = True

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> MemoryPolicy:
        raw = raw or {}
        unknown = sorted(set(raw) - _FIELDS)
        if unknown:
            raise MemoryPolicyError(f"unknown memory_policy fields: {unknown}")
        persist = raw.get("persist_conversation", True)
        if not isinstance(persist, bool):
            raise MemoryPolicyError("memory_policy.persist_conversation must be a bool")
        return cls(
            read_scope=_scope(raw, "read_scope"),
            write_scope=_scope(raw, "write_scope"),
            persist_conversation=persist,
        )

    def allows_read(self, scope: str) -> bool:
        return SCOPES.index(_canonical(scope)) <= SCOPES.index(self.read_scope)

    def allows_write(self, scope: str) -> bool:
        return SCOPES.index(_canonical(scope)) <= SCOPES.index(self.write_scope)


def persists_conversation(raw: Mapping[str, Any] | None) -> bool:
    """Whether a conversation may be durably persisted; invalid means no."""
    try:
        return MemoryPolicy.from_mapping(raw).persist_conversation
    except MemoryPolicyError:
        return False
