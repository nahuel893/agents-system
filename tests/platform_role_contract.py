"""Pinned contract for the roles under ``platform/roles/``.

Two things live here so both platform-tool test files share ONE source of
truth instead of each carrying its own hardcoded role tuple:

``discover_platform_roles``
    Walks ``platform/roles/`` on disk through the loader's own ``RootConfig``,
    so a newly added role is picked up by the boot guards automatically. A
    hardcoded tuple cannot cover role #5 — and role #5 naming a tool the
    registry does not hold is exactly the ``InjectionError`` that keeps a role
    from booting.

``EXPECTED_ROLE_TOOLS``
    Each role's expected tool surface, written out as a literal. Grading a
    resolved surface against ``set(definition.tools)`` grades the manifest
    against itself: dropping a tool from a manifest leaves that assertion
    green. These literals are the independent expectation, so a manifest edit
    has to be a deliberate, reviewed change to this file too.
"""
from __future__ import annotations

import pathlib


def platform_roles_dir() -> pathlib.Path:
    """The on-disk ``platform/roles/`` directory the loader reads from."""
    from agentsys.harness.loader import RootConfig

    return RootConfig().platform_root / "roles"


def discover_platform_roles() -> tuple[str, ...]:
    """Every role folder present under ``platform/roles/``, sorted.

    Deliberately unfiltered beyond "is a directory": an incomplete role folder
    must fail the boot guards loudly rather than be silently skipped.
    """
    return tuple(
        sorted(
            path.name
            for path in platform_roles_dir().iterdir()
            if path.is_dir() and not path.name.startswith((".", "_"))
        )
    )


#: Independent expectation of each role's tool surface. Update deliberately —
#: a change here is a change to what a platform role is allowed to do.
EXPECTED_ROLE_TOOLS: dict[str, frozenset[str]] = {
    "data-agent": frozenset(
        {"catalog_search", "client_lookup", "knowledge_retrieval", "session_state"}
    ),
    "orchestrator": frozenset(
        {"client_lookup", "session_state", "escalation_notifier"}
    ),
    "sales-agent": frozenset(
        {
            "catalog_search",
            "client_lookup",
            "message_sender",
            "order_writer",
            "session_state",
        }
    ),
    "summary-agent": frozenset(
        {"conversation_summarizer", "knowledge_retrieval", "session_state"}
    ),
}

PINNED_ROLES: tuple[str, ...] = tuple(sorted(EXPECTED_ROLE_TOOLS))
