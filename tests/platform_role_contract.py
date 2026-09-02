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


def is_abstract(role: str) -> bool:
    """Whether ``role``'s manifest declares ``abstract: true``."""
    from agentsys.harness.loader import _read_md

    manifest_fm, _ = _read_md(platform_roles_dir() / role / "manifest.md")
    return bool(manifest_fm.get("abstract", False))


def discover_concrete_platform_roles() -> tuple[str, ...]:
    """The roles a boot guard can actually build.

    An abstract role is not a broken one, and the two must not be conflated:
    `resolve` refuses it BY DESIGN, so feeding it to a boot guard would report
    a working feature as a failure. The filter is on the declaration, not on
    a name convention, so it cannot drift from what the loader enforces.

    Every abstract role still has to earn its place — see
    `test_every_abstract_role_has_a_concrete_descendant`.
    """
    return tuple(r for r in discover_platform_roles() if not is_abstract(r))


#: Independent expectation of each role's tool surface. Update deliberately —
#: a change here is a change to what a platform role is allowed to do.
EXPECTED_ROLE_TOOLS: dict[str, frozenset[str]] = {
    # The taxonomy root that can be built. Everything below it inherits
    # these two, which is why three roles gained `escalation_notifier`
    # when they were re-parented: a base exists to stop each descendant
    # restating what they all need.
    "agent": frozenset({"session_state", "escalation_notifier"}),
    "data-agent": frozenset(
        {
            "escalation_notifier",
            "catalog_search",
            "client_lookup",
            "knowledge_retrieval",
            "run_report",
            "session_state",
        }
    ),
    "orchestrator": frozenset(
        {"client_lookup", "session_state", "escalation_notifier"}
    ),
    "sales-agent": frozenset(
        {
            "escalation_notifier",
            "catalog_search",
            "client_lookup",
            "message_sender",
            "order_writer",
            "session_state",
        }
    ),
    "summary-agent": frozenset(
        {
            "escalation_notifier","conversation_summarizer", "knowledge_retrieval", "session_state"}
    ),
}

PINNED_ROLES: tuple[str, ...] = tuple(sorted(EXPECTED_ROLE_TOOLS))
