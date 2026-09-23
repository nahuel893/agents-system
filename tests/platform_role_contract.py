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

A third thing lives here for the same reason (ADR-002 D.17): ``role_chain``
and the ``check_*`` functions below it are the reusable, inherited-contract
checks the formalized suite in ``tests/test_role_contract_suite.py`` applies
to every role ``discover_concrete_platform_roles`` finds — one definition per
invariant, shared instead of re-asserted per role or copied per test file.
"""
from __future__ import annotations

import pathlib
from typing import Any


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
    # The sibling branch: everything `agent` has, plus the two tools that
    # reach the host. Deliberately not reachable from any other role.
    "operator-agent": frozenset(
        {"session_state", "escalation_notifier", "use_term", "read_file"}
    ),
    # Reads only, and every absence is deliberate: no order_writer, no
    # catalog_search. Selling is sales-agent's job.
    "support-agent": frozenset(
        {
            "session_state",
            "escalation_notifier",
            "knowledge_retrieval",
            "conversation_summarizer",
            "client_lookup",
            "message_sender",
        }
    ),
    # The only role descending from operator-agent, so the only one that
    # reaches the host. One tool declared, four inherited.
    "developer-agent": frozenset(
        {
            "session_state",
            "escalation_notifier",
            "use_term",
            "read_file",
            "knowledge_retrieval",
        }
    ),
    # Holds no write permission of any kind.
    "accountant-agent": frozenset(
        {
            "session_state",
            "escalation_notifier",
            "run_report",
            "knowledge_retrieval",
        }
    ),
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


def role_chain(role: str) -> tuple[str, ...]:
    """The ``extends:`` chain from *role* to its root, leaf-first.

    Walks ``_load_role_files`` the same way ``_resolve_role_chain`` (the
    loader's own fold) does, so a role's ancestry here can never drift from
    what actually gets resolved -- never a hardcoded map.
    """
    from agentsys.harness.loader import RootConfig, _load_role_files

    roots = RootConfig()
    chain: list[str] = []
    seen: set[str] = set()
    current: str | None = role
    while current is not None and current not in seen:
        chain.append(current)
        seen.add(current)
        _, parent, _ = _load_role_files(current, roots)
        current = parent
    return tuple(chain)


# ---------------------------------------------------------------------------
# ADR-002 D.17 -- reusable inherited-contract checks.
#
# Each function asserts ONE invariant against an already-RESOLVED value (an
# ``AgentDefinition``, or a computed tool-name set) -- never against disk.
# That is what makes each one independently mutation-testable: a synthetic,
# deliberately broken value can be built with ``dataclasses.replace`` on a
# real resolution -- no on-disk fixture role needed -- and handed straight
# to the check. See ``tests/test_role_contract_suite.py`` for both the
# tree-wide pass proof (every concrete role) and the synthetic-fixture fail
# proof (one deliberately broken value per check) for each one.
# ---------------------------------------------------------------------------

_DESIGN_NOTES_MARKER = "## design notes"
_BASE_CONTRACT_CLAUSE_MARKER = "never fabricate data"
_BASE_CONTRACT_LAST_CLAUSE = "Answer in the user's language."
_EXEC_PERMISSION_PREFIX = "exec:"


def check_no_design_notes_leak(definition: Any) -> None:
    """ADR-002 B.8: a ``## design notes`` heading must never reach a
    resolved prompt."""
    assert _DESIGN_NOTES_MARKER not in definition.system_prompt.lower(), (
        f"'{definition.role_name}': design-notes marker leaked into its "
        "resolved system prompt"
    )


def check_base_contract_present_once_and_last(definition: Any) -> None:
    """ADR-002 B.9: the six-clause base contract, exactly once, last block."""
    prompt = definition.system_prompt
    count = prompt.lower().count(_BASE_CONTRACT_CLAUSE_MARKER)
    assert count == 1, (
        f"'{definition.role_name}': base-contract clause appears {count} "
        "times in the resolved prompt, expected exactly 1"
    )
    assert prompt.rstrip().endswith(_BASE_CONTRACT_LAST_CLAUSE), (
        f"'{definition.role_name}': base contract is not the final block "
        "of the resolved system prompt"
    )


def check_untrusted_input_exec_exclusion(definition: Any) -> None:
    """ADR-002 C.11: ``untrusted_input=true`` and any ``exec:*`` permission
    are mutually exclusive."""
    if not definition.untrusted_input:
        return
    exec_perms = sorted(
        p
        for p in definition.permissions
        if p.strip().lower().startswith(_EXEC_PERMISSION_PREFIX)
    )
    assert not exec_perms, (
        f"'{definition.role_name}': untrusted_input=true but holds exec:* "
        f"permissions {exec_perms}"
    )


def check_permissions_and_tools_survive_inheritance(leaf: Any, ancestor: Any) -> None:
    """Role-to-role composition is additive (ADR-002 D): nothing a resolved
    ancestor grants may be missing from a resolved descendant."""
    missing_perms = set(ancestor.permissions) - set(leaf.permissions)
    missing_tools = set(ancestor.tools) - set(leaf.tools)
    assert not missing_perms, (
        f"'{leaf.role_name}': lost permissions {sorted(missing_perms)} that "
        f"its ancestor '{ancestor.role_name}' grants"
    )
    assert not missing_tools, (
        f"'{leaf.role_name}': lost tools {sorted(missing_tools)} that its "
        f"ancestor '{ancestor.role_name}' grants"
    )


def check_ungranted_tools_are_not_injected(
    definition: Any, registry: Any, granted_tool_names: frozenset[str]
) -> None:
    """A tool whose registry spec requires a permission must never appear
    in *granted_tool_names* when that permission was not granted (D-009's
    Layer-1 RBAC)."""
    for name in definition.tools:
        spec = registry.get(name)
        if spec.required_permissions:
            assert name not in granted_tool_names, (
                f"'{definition.role_name}': tool '{name}' requires "
                f"permissions {spec.required_permissions} but was granted "
                "with an empty permission set"
            )
