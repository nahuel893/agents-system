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

A fourth thing lives here for Guard 1 (design.md D8; the
``predefined-agent-governance`` spec): ``RoleGovernanceSnapshot`` and
``EXPECTED_ROLE_SURFACE`` are ``EXPECTED_ROLE_TOOLS``'s own pattern extended
to also pin each role's ``permissions`` and ``version`` — an independent,
reviewed snapshot a manifest change must never update by itself.
``check_role_governance_snapshot`` is the comparison: a resolved
tools/permissions divergence from the snapshot must carry both a MAJOR
``version:`` bump and a ``CHANGELOG.md`` entry naming the role, or it fails,
naming exactly what diverged and which of the two conditions is unmet. See
``tests/test_role_governance_contract.py`` for the fixture-scoped RED/GREEN
suite and the real per-role contract test this feeds.
"""

from __future__ import annotations

import dataclasses
import pathlib
from typing import Any


@dataclasses.dataclass(frozen=True)
class RoleGovernanceSnapshot:
    """Guard 1 (design.md D8; ``predefined-agent-governance`` spec): a
    versioned, independent record of one predefined role's ``tools``/
    ``permissions`` surface, written and maintained separately from that
    role's own manifest — a manifest edit MUST NOT, by itself, update this.
    See ``check_role_governance_snapshot`` for what compares against it.
    """

    tools: frozenset[str]
    permissions: frozenset[str]
    #: "MAJOR.MINOR", the role's frontmatter ``version:`` as of this
    #: reviewed snapshot.
    version: str


def platform_roles_dir() -> pathlib.Path:
    """The on-disk ``platform/roles/`` directory the loader reads from."""
    from agents_system.harness.loader import RootConfig

    return RootConfig().platform_root / "roles"


def discover_platform_roles() -> tuple[str, ...]:
    """Every predefined role folder present under ``platform/roles/``, sorted.

    Deliberately unfiltered beyond "is a directory": an incomplete predefined
    role folder must fail the boot guards loudly rather than be silently
    skipped.
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
    from agents_system.harness.loader import _read_md

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
            "escalation_notifier",
            "conversation_summarizer",
            "knowledge_retrieval",
            "session_state",
        }
    ),
}

PINNED_ROLES: tuple[str, ...] = tuple(sorted(EXPECTED_ROLE_TOOLS))


#: Guard 1 (design.md D8). Independent, versioned expectation of every
#: predefined role's (tools, permissions, version) surface — seeded from the
#: CURRENT resolved surface of each PINNED_ROLES member at this PR's merge
#: time (a reviewed, deliberate copy, same philosophy EXPECTED_ROLE_TOOLS
#: documents above). Update this dict ONLY in the same PR that also bumps
#: the affected role's manifest `version:` MAJOR component and adds a
#: CHANGELOG.md entry naming it — see `check_role_governance_snapshot`.
EXPECTED_ROLE_SURFACE: dict[str, RoleGovernanceSnapshot] = {
    "agent": RoleGovernanceSnapshot(
        tools=frozenset({"session_state", "escalation_notifier"}),
        permissions=frozenset({"read:session", "send:escalation"}),
        version="1.0",
    ),
    "operator-agent": RoleGovernanceSnapshot(
        tools=frozenset(
            {"session_state", "escalation_notifier", "use_term", "read_file"}
        ),
        permissions=frozenset(
            {"read:session", "send:escalation", "exec:command", "read:files"}
        ),
        version="1.0",
    ),
    "support-agent": RoleGovernanceSnapshot(
        tools=frozenset(
            {
                "session_state",
                "escalation_notifier",
                "knowledge_retrieval",
                "conversation_summarizer",
                "client_lookup",
                "message_sender",
            }
        ),
        permissions=frozenset(
            {
                "read:session",
                "send:escalation",
                "read:knowledge_base",
                "read:conversation_logs",
                "read:client_registry",
                "send:message",
            }
        ),
        version="1.0",
    ),
    "developer-agent": RoleGovernanceSnapshot(
        tools=frozenset(
            {
                "session_state",
                "escalation_notifier",
                "use_term",
                "read_file",
                "knowledge_retrieval",
            }
        ),
        permissions=frozenset(
            {
                "read:session",
                "send:escalation",
                "exec:command",
                "read:files",
                "read:knowledge_base",
            }
        ),
        version="1.0",
    ),
    "accountant-agent": RoleGovernanceSnapshot(
        tools=frozenset(
            {
                "session_state",
                "escalation_notifier",
                "run_report",
                "knowledge_retrieval",
            }
        ),
        permissions=frozenset(
            {
                "read:session",
                "send:escalation",
                "read:reports",
                "read:knowledge_base",
            }
        ),
        version="1.0",
    ),
    "data-agent": RoleGovernanceSnapshot(
        tools=frozenset(
            {
                "escalation_notifier",
                "catalog_search",
                "client_lookup",
                "knowledge_retrieval",
                "run_report",
                "session_state",
            }
        ),
        permissions=frozenset(
            {
                "read:session",
                "send:escalation",
                "read:catalog",
                "read:client_registry",
                "read:knowledge_base",
                "read:reports",
            }
        ),
        version="1.1",
    ),
    "orchestrator": RoleGovernanceSnapshot(
        tools=frozenset({"client_lookup", "session_state", "escalation_notifier"}),
        permissions=frozenset(
            {
                "read:session",
                "send:escalation",
                "read:client_registry",
                "write:session",
                "spawn:data-agent",
                "spawn:sales-agent",
                "spawn:summary-agent",
            }
        ),
        version="1.0",
    ),
    "sales-agent": RoleGovernanceSnapshot(
        tools=frozenset(
            {
                "escalation_notifier",
                "catalog_search",
                "client_lookup",
                "message_sender",
                "order_writer",
                "session_state",
            }
        ),
        permissions=frozenset(
            {
                "read:session",
                "send:escalation",
                "read:catalog",
                "read:client_registry",
                "write:orders",
                "write:order_items",
                "read:price_lists",
                "send:message",
            }
        ),
        version="1.0",
    ),
    "summary-agent": RoleGovernanceSnapshot(
        tools=frozenset(
            {
                "escalation_notifier",
                "conversation_summarizer",
                "knowledge_retrieval",
                "session_state",
            }
        ),
        permissions=frozenset(
            {
                "read:session",
                "send:escalation",
                "read:conversation_logs",
                "read:knowledge_base",
                "write:summary_output",
            }
        ),
        version="1.0",
    ),
}


def _guard1_major_version(value: str) -> int:
    """The MAJOR component of a role's `MAJOR.MINOR`-shaped `version:`
    string. Guard 1 compares this component only — MINOR is free to move
    for any reason (a role's own minor documentation/tuning revision)."""
    try:
        return int(str(value).split(".")[0])
    except (ValueError, IndexError):
        return 0


def check_role_governance_snapshot(
    role: str,
    resolved_tools: frozenset[str],
    resolved_permissions: frozenset[str],
    resolved_version: str,
    snapshot: RoleGovernanceSnapshot,
    changelog_text: str,
) -> None:
    """Guard 1 (design.md D8; ``predefined-agent-governance`` spec): a
    predefined role's ``tools``/``permissions`` surface may diverge from its
    reviewed *snapshot* only when BOTH (a) *resolved_version*'s MAJOR
    component is strictly greater than *snapshot.version*'s, and (b)
    *changelog_text* names *role* somewhere. Neither condition alone is
    sufficient (spec's own two "still fails" scenarios), and no divergence
    at all (e.g. a ``role.md``-only prose edit) requires nothing.

    *changelog_text* is always a caller-supplied string — normally
    ``CHANGELOG.md``'s real content, a plain stand-in in tests — this
    function never reads the filesystem itself.

    *resolved_tools*/*resolved_permissions*/*resolved_version* are always
    independent arguments, never derived from *snapshot*: this function can
    never grade a role against itself.
    """
    added_tools = sorted(resolved_tools - snapshot.tools)
    removed_tools = sorted(snapshot.tools - resolved_tools)
    added_permissions = sorted(resolved_permissions - snapshot.permissions)
    removed_permissions = sorted(snapshot.permissions - resolved_permissions)
    if not (added_tools or removed_tools or added_permissions or removed_permissions):
        return

    version_bumped = _guard1_major_version(resolved_version) > _guard1_major_version(
        snapshot.version
    )
    changelog_ok = role in changelog_text

    if version_bumped and changelog_ok:
        return

    divergence_parts = []
    if added_tools:
        divergence_parts.append(f"added tools {added_tools}")
    if removed_tools:
        divergence_parts.append(f"removed tools {removed_tools}")
    if added_permissions:
        divergence_parts.append(f"added permissions {added_permissions}")
    if removed_permissions:
        divergence_parts.append(f"removed permissions {removed_permissions}")

    missing = []
    if not version_bumped:
        missing.append(
            "a MAJOR version bump (snapshot records "
            f"'{snapshot.version}', resolved is '{resolved_version}')"
        )
    if not changelog_ok:
        missing.append("a CHANGELOG.md entry naming this role")

    raise AssertionError(
        f"Guard 1: predefined role '{role}' diverged from its governance "
        f"snapshot ({'; '.join(divergence_parts)}) without "
        f"{' and '.join(missing)}. Update EXPECTED_ROLE_SURFACE in the same "
        "PR that satisfies whichever condition above is missing."
    )


def role_chain(role: str) -> tuple[str, ...]:
    """The ``extends:`` chain from *role* to its root, leaf-first.

    Walks ``_load_role_files`` the same way ``_resolve_role_chain`` (the
    loader's own fold) does, so a role's ancestry here can never drift from
    what actually gets resolved -- never a hardcoded map.
    """
    from agents_system.harness.loader import RootConfig, _load_role_files

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


def check_escalation_conditions_have_descriptions(definition: Any) -> None:
    """Issue #88: every condition a predefined role declares must resolve to
    a non-empty description, so the composed prompt renders `- name —
    description` and never just the bare name.

    A bare name alone is exactly the shape #82 traced the failure to:
    `accountant-agent`'s `figure_requested_outside_report_catalog` reached
    the model with no explanation of what it meant or what to do about it.
    This is enforced for every PREDEFINED role only — an importer agent
    (`FolderLocator`/`InlineLocator`) that supplies none is a deliberately
    tolerated fallback (bare name, no failure), see
    `harness/factory.py::_render_escalation_block`'s own docstring.
    """
    from agents_system.harness.loader import _as_str_list

    conditions = _as_str_list(definition.escalation_rules.get("conditions"))
    descriptions = definition.escalation_rules.get("descriptions") or {}
    missing = [
        condition
        for condition in conditions
        if not str(descriptions.get(condition) or "").strip()
    ]
    assert not missing, (
        f"'{definition.role_name}': escalation condition(s) {missing} have "
        "no description — add a `- `name` — description` bullet to this "
        "role's own `policy.md` prose (or an ancestor's, for an inherited "
        "condition)"
    )
