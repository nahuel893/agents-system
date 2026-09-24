"""Tests for capability tiers on ToolSpec (ADR-002 C.10, issue #109).

Strict TDD: written before `Tier`/`ToolSpec.tier` exist in
`agents_system.harness.registry`. Covers:

1. `ToolSpec.tier` is a required field (fail closed — no default tier that
   silently grants; a tool author who forgets to classify a tool gets a
   TypeError at construction, not a silently-safe default).
2. Every tool the platform actually registers (the seven public
   `build_*_tool_spec`/`build_operator_tool_specs` builders) is classified
   into the ADR-002 C.10 table's tier.
3. Interceptor Layer 2 revalidates T2/T3 tools independent of permission
   prefix — the exact gap C.10 closes (a T3 tool named under a `read:*`
   permission was previously invisible to the write:/send: heuristic).
"""

from __future__ import annotations

from typing import Any

import pytest

from agents_system.permissions import Run, UnknownPermissionNameError, resolve, resource


#: `Run` has no shipped resource-scoped wire name (PR1's registration table
#: covers only Read/Write/Send/Exec/Spawn) -- declarative command-tool
#: permissions are registered dynamically, one dedicated subclass per wire
#: name (a class holds exactly one canonical name -- spec: "Unique wire
#: name per class"), the same way `harness.loader._parse_command_tools`
#: registers one per manifest-declared command tool. These two names
#: mirror `tests/test_command_tools_*.py`'s own fixtures so the lower-level
#: ToolSpec-construction tests below can exercise Run without going
#: through the loader. `resource()` itself is NOT idempotent (it always
#: builds a fresh class), so check-then-create here to converge safely
#: with any other module that registers the same wire name first.
def _ensure_run_permission_registered(wire_name: str) -> None:
    try:
        resolve(wire_name)
    except UnknownPermissionNameError:
        resource(Run, wire_name)


_ensure_run_permission_registered("run:check_stock")
_ensure_run_permission_registered("run:dangerous_tool")


def _connector(_input: Any) -> str:
    return "ok"


# ---------------------------------------------------------------------------
# 1. `tier` is a required ToolSpec field
# ---------------------------------------------------------------------------


def test_toolspec_tier_is_required() -> None:
    """Omitting `tier` must fail construction, not silently default to a tier."""
    from agents_system.harness.registry import ToolSpec

    with pytest.raises(TypeError):
        ToolSpec(  # type: ignore[call-arg]
            name="untiered_tool",
            required_permissions=("read:catalog",),
            connector=_connector,
        )


def test_tier_enum_has_exactly_t0_through_t3() -> None:
    from agents_system.harness.registry import Tier

    assert {member.value for member in Tier} == {"T0", "T1", "T2", "T3"}


def test_toolspec_accepts_explicit_tier() -> None:
    from agents_system.harness.registry import Tier, ToolSpec

    spec = ToolSpec(
        name="catalog_search",
        required_permissions=("read:catalog",),
        connector=_connector,
        tier=Tier.T1,
    )

    assert spec.tier is Tier.T1


# ---------------------------------------------------------------------------
# 2. Every production tool is classified per the ADR-002 C.10 table
# ---------------------------------------------------------------------------


def _production_specs() -> dict[str, Any]:
    """Every ToolSpec the platform's own public builders can produce today."""
    from agents_system.connectors.operator import build_operator_tool_specs
    from agents_system.connectors.order_connector import build_order_writer_tool_spec
    from agents_system.connectors.platform_connectors import (
        build_conversation_summarizer_tool_spec,
        build_escalation_notifier_tool_spec,
        build_knowledge_retrieval_tool_spec,
    )
    from agents_system.connectors.report_connector import build_report_tool_spec

    specs: dict[str, Any] = {}
    for spec in build_operator_tool_specs(None):
        specs[spec.name] = spec
    specs["order_writer"] = build_order_writer_tool_spec(None)
    specs["run_report"] = build_report_tool_spec(None, {})
    specs["knowledge_retrieval"] = build_knowledge_retrieval_tool_spec(None)
    specs["conversation_summarizer"] = build_conversation_summarizer_tool_spec(None)
    specs["escalation_notifier"] = build_escalation_notifier_tool_spec(None)
    return specs


def test_every_registered_platform_tool_has_a_tier() -> None:
    """Fail-closed audit: every tool the platform registers must be tiered.

    Iterates the real public builders rather than a hand-maintained list, so
    a future tool that forgets a tier fails this test instead of shipping
    with an implicit, unreviewed classification.
    """
    from agents_system.harness.registry import Tier

    specs = _production_specs()
    assert specs, "no production tool builders found — check the import list"
    for name, spec in specs.items():
        assert isinstance(spec.tier, Tier), f"{name} has no valid tier: {spec.tier!r}"


@pytest.mark.parametrize(
    "tool_name,expected_tier",
    [
        ("use_term", "T3"),
        ("read_file", "T3"),
        ("order_writer", "T2"),
        ("escalation_notifier", "T2"),
        ("run_report", "T1"),
        ("knowledge_retrieval", "T1"),
        ("conversation_summarizer", "T1"),
    ],
)
def test_production_tool_tier_matches_adr_table(
    tool_name: str, expected_tier: str
) -> None:
    from agents_system.harness.registry import Tier

    specs = _production_specs()
    assert specs[tool_name].tier is Tier(expected_tier)


# ---------------------------------------------------------------------------
# 3. Interceptor Layer 2 revalidates T2/T3 independent of permission prefix
# ---------------------------------------------------------------------------


async def test_t3_tool_revalidated_even_under_read_permission_name() -> None:
    """The exact gap C.10 closes: a T3 tool named `read:x` (no `exec:`
    prefix) must still be revalidated at call time. `read:files` is the
    real, registered instance of this shape (`ReadFiles(Read)` at T3, PR1
    Resolved Decision 1) -- no synthetic unregistered name needed."""
    from agents_system.harness.factory import EquippedRuntime
    from agents_system.harness.interceptor import PolicyViolation, intercept
    from agents_system.harness.registry import Tier, ToolSpec

    spec = ToolSpec(
        name="disguised_t3_tool",
        required_permissions=("read:files",),  # deliberately NOT exec:*
        connector=_connector,
        tier=Tier.T3,
    )
    runtime = EquippedRuntime(
        definition=None,  # type: ignore[arg-type]
        system_prompt="",
        tools=(spec,),
        denied_tools=(),
        skills=(),
    )

    # No current_permissions supplied — a sensitive tool must demand them.
    with pytest.raises(PolicyViolation) as exc_info:
        await intercept("disguised_t3_tool", {}, runtime)

    assert exc_info.value.tool_name == "disguised_t3_tool"
    assert exc_info.value.reason == "revalidation_required"


async def test_t1_tool_under_read_permission_is_not_revalidated() -> None:
    """Regression guard: an ordinary T1 read tool stays non-sensitive."""
    from agents_system.harness.factory import EquippedRuntime
    from agents_system.harness.interceptor import intercept
    from agents_system.harness.registry import Tier, ToolSpec

    spec = ToolSpec(
        name="plain_read_tool",
        required_permissions=("read:catalog",),
        connector=_connector,
        tier=Tier.T1,
    )
    runtime = EquippedRuntime(
        definition=None,  # type: ignore[arg-type]
        system_prompt="",
        tools=(spec,),
        denied_tools=(),
        skills=(),
    )

    result = await intercept("plain_read_tool", {}, runtime)

    assert result.revalidated is False


@pytest.mark.parametrize("tool_name", ["order_writer", "escalation_notifier"])
async def test_every_previously_write_send_tool_still_revalidated(
    tool_name: str,
) -> None:
    """Regression: every tool the old write:/send: prefix heuristic revalidated
    must still be revalidated once tier replaces that heuristic."""
    from agents_system.harness.factory import EquippedRuntime
    from agents_system.harness.interceptor import PolicyViolation, intercept

    specs = _production_specs()
    spec = specs[tool_name]
    runtime = EquippedRuntime(
        definition=None,  # type: ignore[arg-type]
        system_prompt="",
        tools=(spec,),
        denied_tools=(),
        skills=(),
    )

    with pytest.raises(PolicyViolation) as exc_info:
        await intercept(tool_name, {}, runtime)  # no current_permissions

    assert exc_info.value.reason == "revalidation_required"


# ---------------------------------------------------------------------------
# 4. Fail-closed at construction: a write:/send:/exec: permission cannot be
#    paired with a tier that would leave it unrevalidated (review follow-up
#    on PR #142 — replacing the prefix heuristic must not turn the
#    write:/send:/exec: guarantee into something every future ToolSpec
#    author has to remember to self-classify correctly).
# ---------------------------------------------------------------------------


def test_write_permission_with_t1_tier_raises() -> None:
    from agents_system.harness.registry import Tier, ToolSpec
    from agents_system.permissions import PermissionTierMismatchError

    with pytest.raises(PermissionTierMismatchError, match="write:orders"):
        ToolSpec(
            name="bad_write_tool",
            required_permissions=("write:orders",),
            connector=_connector,
            tier=Tier.T1,
        )


def test_send_permission_with_t0_tier_raises() -> None:
    from agents_system.harness.registry import Tier, ToolSpec
    from agents_system.permissions import PermissionTierMismatchError

    with pytest.raises(PermissionTierMismatchError, match="send:message"):
        ToolSpec(
            name="bad_send_tool",
            required_permissions=("send:message",),
            connector=_connector,
            tier=Tier.T0,
        )


def test_exec_permission_with_t2_tier_raises() -> None:
    """exec:* strictly requires T3 — T2 is not sufficient, unlike write:/send:."""
    from agents_system.harness.registry import Tier, ToolSpec
    from agents_system.permissions import PermissionTierMismatchError

    with pytest.raises(PermissionTierMismatchError, match="exec:command"):
        ToolSpec(
            name="bad_exec_tool",
            required_permissions=("exec:command",),
            connector=_connector,
            tier=Tier.T2,
        )


def test_whitespace_variant_write_permission_still_caught() -> None:
    """Registry resolution is exact-string, not prefix-tolerant (R2a) — no
    case/whitespace tolerance requirement carries over from the old prefix
    heuristic (see `test_unregistered_permission_name_fails_closed` below
    for what a typo does instead). Repurposed into a second registered
    Write-family permission failing the ceiling, at a different tier than
    its sibling test above."""
    from agents_system.harness.registry import Tier, ToolSpec
    from agents_system.permissions import PermissionTierMismatchError

    with pytest.raises(PermissionTierMismatchError, match="write:order_items"):
        ToolSpec(
            name="bad_whitespace_tool",
            required_permissions=("write:order_items",),
            connector=_connector,
            tier=Tier.T0,
        )


def test_unregistered_permission_name_fails_closed() -> None:
    """The old prefix heuristic's own guarantee — a case/whitespace typo
    must not silently disarm the guard — still holds, just via a different
    mechanism: a registered-name lookup treats the typo as a distinct,
    unregistered string and fails closed instead of matching it loosely."""
    from agents_system.harness.registry import Tier, ToolSpec
    from agents_system.permissions import UnknownPermissionNameError

    with pytest.raises(UnknownPermissionNameError):
        ToolSpec(
            name="bad_typo_tool",
            required_permissions=(" Write:Orders ",),
            connector=_connector,
            tier=Tier.T1,
        )


# ---------------------------------------------------------------------------
# 4b. R2b (floor): a T2/T3 tool's required permissions must include at
#    least one that reaches the tool's own tier — R2a's ceiling alone would
#    let a T2/T3 tool hide behind a cheap, low-tier permission.
# ---------------------------------------------------------------------------


def test_t3_tool_requiring_only_low_tier_permission_fails_floor() -> None:
    """R2a alone would pass (0<=3); R2b's floor still rejects it."""
    from agents_system.harness.registry import Tier, ToolSpec
    from agents_system.permissions import PermissionFloorViolationError

    with pytest.raises(PermissionFloorViolationError, match="read:catalog"):
        ToolSpec(
            name="bad_floor_tool",
            required_permissions=("read:catalog",),
            connector=_connector,
            tier=Tier.T3,
        )


def test_t1_tool_has_no_floor_requirement() -> None:
    from agents_system.harness.registry import Tier, ToolSpec

    spec = ToolSpec(
        name="t1_floor_exempt",
        required_permissions=("read:catalog",),
        connector=_connector,
        tier=Tier.T1,
    )

    assert spec.tier is Tier.T1


def test_t2_tool_with_mixed_tier_permissions_passes_floor() -> None:
    """`max(0, 2) >= 2` — one required permission reaching the tool's own
    tier is enough, even alongside a lower-tier one."""
    from agents_system.harness.registry import Tier, ToolSpec

    spec = ToolSpec(
        name="mixed_floor_tool",
        required_permissions=("read:catalog", "write:orders"),
        connector=_connector,
        tier=Tier.T2,
    )

    assert spec.tier is Tier.T2


def test_t0_tool_with_no_required_permissions_has_no_floor_requirement() -> None:
    from agents_system.harness.registry import Tier, ToolSpec

    spec = ToolSpec(
        name="t0_no_perms", required_permissions=(), connector=_connector, tier=Tier.T0
    )

    assert spec.required_permissions == ()


def test_order_writer_shaped_tool_satisfies_both_r2a_and_r2b() -> None:
    """`order_writer`'s own two Write-T2 permissions independently satisfy
    the ceiling (2<=2 both) and the floor (max(2,2)>=2)."""
    from agents_system.harness.registry import Tier, ToolSpec

    spec = ToolSpec(
        name="order_writer_shaped",
        required_permissions=("write:orders", "write:order_items"),
        connector=_connector,
        tier=Tier.T2,
    )

    assert spec.tier is Tier.T2


def test_read_file_shaped_tool_now_passes_r2a_and_r2b() -> None:
    """Resolved Decision 1 end-to-end: `read:files` resolves to `ReadFiles`
    (`Read` escalated to T3, shipped in PR1), so a `read_file`-shaped
    ToolSpec (tier=T3, required_permissions=("read:files",)) now succeeds
    under both R2a (3>=3) and R2b (max(3)>=3) — previously the sole R2b
    exception the spec's compatibility audit found."""
    from agents_system.harness.registry import Tier, ToolSpec

    spec = ToolSpec(
        name="read_file_shaped",
        required_permissions=("read:files",),
        connector=_connector,
        tier=Tier.T3,
    )

    assert spec.tier is Tier.T3


@pytest.mark.parametrize(
    "perm,tier_name",
    [
        ("write:orders", "T2"),
        ("send:message", "T2"),
        # ("write:orders", "T3") and ("send:message", "T3") are deliberately
        # NOT here: R2b (the floor, added once PR2-T2 lands) rejects a T3
        # tool whose only required permission is a T2-tier one (max(2)>=3 is
        # false) — the same gap `read_file` hit before PR1's `ReadFiles`
        # escalation. These two single-permission T3 combos are no longer
        # "construct cleanly" cases; see test_capability_tiers.py's R2b
        # section for the floor-violation coverage instead.
        ("exec:command", "T3"),
        ("read:catalog", "T0"),
        ("read:catalog", "T1"),
    ],
)
def test_valid_permission_tier_combinations_construct_cleanly(
    perm: str, tier_name: str
) -> None:
    from agents_system.harness.registry import Tier, ToolSpec

    spec = ToolSpec(
        name="ok_tool",
        required_permissions=(perm,),
        connector=_connector,
        tier=Tier(tier_name),
    )
    assert spec.tier is Tier(tier_name)


async def test_always_revalidate_t1_tool_still_revalidated() -> None:
    """Regression: `always_revalidate=True` keeps revalidating a T1 tool
    (run_report) even though T1 alone would not trigger it."""
    from agents_system.harness.factory import EquippedRuntime
    from agents_system.harness.interceptor import PolicyViolation, intercept

    specs = _production_specs()
    spec = specs["run_report"]
    assert spec.always_revalidate is True

    runtime = EquippedRuntime(
        definition=None,  # type: ignore[arg-type]
        system_prompt="",
        tools=(spec,),
        denied_tools=(),
        skills=(),
    )

    with pytest.raises(PolicyViolation) as exc_info:
        await intercept("run_report", {}, runtime)

    assert exc_info.value.reason == "revalidation_required"


# ---------------------------------------------------------------------------
# 5. Fail-closed at construction: a run:* permission (ADR-002 C.12 command
#    tools) cannot be paired with a tier below T2 — PR #147 review follow-up.
#    A T0/T1 `run:*` tool is never revalidated by `interceptor._is_sensitive`
#    (which only checks tier in {T2, T3} or `always_revalidate`), so it would
#    reach an untrusted_input role with no Layer-2 check at all — the exact
#    gap the reviewer proved live with a T0 command tool running
#    `/usr/bin/id` unrevalidated. Mirrors the write:/send:/exec: guards above.
# ---------------------------------------------------------------------------


def test_run_permission_with_t0_tier_raises() -> None:
    from agents_system.harness.registry import Tier, ToolSpec
    from agents_system.permissions import PermissionTierMismatchError

    with pytest.raises(PermissionTierMismatchError, match="run:check_stock"):
        ToolSpec(
            name="bad_run_tool_t0",
            required_permissions=("run:check_stock",),
            connector=_connector,
            tier=Tier.T0,
        )


def test_run_permission_with_t1_tier_raises() -> None:
    from agents_system.harness.registry import Tier, ToolSpec
    from agents_system.permissions import PermissionTierMismatchError

    with pytest.raises(PermissionTierMismatchError, match="run:check_stock"):
        ToolSpec(
            name="bad_run_tool_t1",
            required_permissions=("run:check_stock",),
            connector=_connector,
            tier=Tier.T1,
        )


def test_run_permission_with_t2_tier_is_accepted() -> None:
    """T2 stays allowed — ADR-002 C.12 deliberately lets an untrusted_input
    role hold a narrow T2 command tool; only the room BELOW T2 is closed."""
    from agents_system.harness.registry import Tier, ToolSpec

    spec = ToolSpec(
        name="ok_run_tool_t2",
        required_permissions=("run:check_stock",),
        connector=_connector,
        tier=Tier.T2,
    )

    assert spec.tier is Tier.T2


def test_run_permission_with_t3_tier_now_fails_the_floor() -> None:
    """Spec's own documented scenario ("The declarative command-tool
    factory can produce a floor-failing spec"): `Run` is T2, so a T3-tier
    tool requiring only a `run:*` permission fails R2b's floor
    (`max(2) >= 3` is false) — this used to construct cleanly under the old
    prefix heuristic (which only demanded tier in {T2, T3}). Resolved
    Decision 3 (PR2-T5) additionally narrows `_COMMAND_TOOL_MIN_TIER` so no
    T3 declarative command tool can even reach this point via the loader;
    this test proves the ToolSpec-level guard holds independent of that."""
    from agents_system.harness.registry import Tier, ToolSpec
    from agents_system.permissions import PermissionFloorViolationError

    with pytest.raises(PermissionFloorViolationError, match="run:dangerous_tool"):
        ToolSpec(
            name="bad_run_tool_t3",
            required_permissions=("run:dangerous_tool",),
            connector=_connector,
            tier=Tier.T3,
        )


def test_run_permission_case_and_whitespace_variant_still_caught() -> None:
    """Repurposed like the write:/send: sibling above — registry lookups are
    exact, so this now exercises a second Run-family ceiling violation using
    a distinct registered name; the unregistered-typo guarantee lives in
    `test_unregistered_permission_name_fails_closed`."""
    from agents_system.harness.registry import Tier, ToolSpec
    from agents_system.permissions import PermissionTierMismatchError

    with pytest.raises(PermissionTierMismatchError, match="run:dangerous_tool"):
        ToolSpec(
            name="bad_run_whitespace",
            required_permissions=("run:dangerous_tool",),
            connector=_connector,
            tier=Tier.T0,
        )
