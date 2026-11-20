"""What the definition loader raises and warns with."""

from __future__ import annotations


class DefinitionError(Exception):
    """Raised when an agent definition violates a structural invariant."""


class PredefinedRoleDeprecationWarning(DeprecationWarning):
    """A concrete predefined role was resolved by name (ADR-010, #222).

    The eight concrete roles became reference agents under
    ``examples/agents/``: examples to copy, not supported API. Naming one
    still resolves it from ``platform/roles/`` until the next breaking
    release removes it. ``base`` and ``agent`` stay in the core and never
    warn.
    """
