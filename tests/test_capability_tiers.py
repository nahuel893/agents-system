"""Tests for capability tiers on ToolSpec (ADR-002 C.10, issue #109).

Strict TDD: written before `Tier`/`ToolSpec.tier` exist in
`agentsys.harness.registry`. Covers:

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


def _connector(_input: Any) -> str:
    return "ok"


# ---------------------------------------------------------------------------
# 1. `tier` is a required ToolSpec field
# ---------------------------------------------------------------------------


def test_toolspec_tier_is_required() -> None:
    """Omitting `tier` must fail construction, not silently default to a tier."""
    from agentsys.harness.registry import ToolSpec

    with pytest.raises(TypeError):
        ToolSpec(  # type: ignore[call-arg]
            name="untiered_tool",
            required_permissions=("read:catalog",),
            connector=_connector,
        )


def test_tier_enum_has_exactly_t0_through_t3() -> None:
    from agentsys.harness.registry import Tier

    assert {member.value for member in Tier} == {"T0", "T1", "T2", "T3"}


def test_toolspec_accepts_explicit_tier() -> None:
    from agentsys.harness.registry import Tier, ToolSpec

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
    from agentsys.connectors.operator import build_operator_tool_specs
    from agentsys.connectors.order_connector import build_order_writer_tool_spec
    from agentsys.connectors.platform_connectors import (
        build_conversation_summarizer_tool_spec,
        build_escalation_notifier_tool_spec,
        build_knowledge_retrieval_tool_spec,
    )
    from agentsys.connectors.report_connector import build_report_tool_spec

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
    from agentsys.harness.registry import Tier

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
    from agentsys.harness.registry import Tier

    specs = _production_specs()
    assert specs[tool_name].tier is Tier(expected_tier)


# ---------------------------------------------------------------------------
# 3. Interceptor Layer 2 revalidates T2/T3 independent of permission prefix
# ---------------------------------------------------------------------------


async def test_t3_tool_revalidated_even_under_read_permission_name() -> None:
    """The exact gap C.10 closes: a T3 tool named `read:x` (no `exec:`
    prefix) must still be revalidated at call time."""
    from agentsys.harness.factory import EquippedRuntime
    from agentsys.harness.interceptor import PolicyViolation, intercept
    from agentsys.harness.registry import Tier, ToolSpec

    spec = ToolSpec(
        name="disguised_t3_tool",
        required_permissions=("read:innocuous",),  # deliberately NOT exec:*
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
    from agentsys.harness.factory import EquippedRuntime
    from agentsys.harness.interceptor import intercept
    from agentsys.harness.registry import Tier, ToolSpec

    spec = ToolSpec(
        name="plain_read_tool",
        required_permissions=("read:innocuous",),
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
    from agentsys.harness.factory import EquippedRuntime
    from agentsys.harness.interceptor import PolicyViolation, intercept

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
    from agentsys.harness.registry import Tier, ToolSpec

    with pytest.raises(ValueError, match="write:orders"):
        ToolSpec(
            name="bad_write_tool",
            required_permissions=("write:orders",),
            connector=_connector,
            tier=Tier.T1,
        )


def test_send_permission_with_t0_tier_raises() -> None:
    from agentsys.harness.registry import Tier, ToolSpec

    with pytest.raises(ValueError, match="send:message"):
        ToolSpec(
            name="bad_send_tool",
            required_permissions=("send:message",),
            connector=_connector,
            tier=Tier.T0,
        )


def test_exec_permission_with_t2_tier_raises() -> None:
    """exec:* strictly requires T3 — T2 is not sufficient, unlike write:/send:."""
    from agentsys.harness.registry import Tier, ToolSpec

    with pytest.raises(ValueError, match="EXEC:shell"):
        ToolSpec(
            name="bad_exec_tool",
            required_permissions=("EXEC:shell",),  # case variant, deliberately
            connector=_connector,
            tier=Tier.T2,
        )


def test_whitespace_variant_write_permission_still_caught() -> None:
    """Mirrors loader._is_exec_permission's own whitespace/case tolerance —
    a stray leading/trailing space or case variant must not disarm the guard."""
    from agentsys.harness.registry import Tier, ToolSpec

    with pytest.raises(ValueError):
        ToolSpec(
            name="bad_whitespace_tool",
            required_permissions=(" Write:Orders ",),
            connector=_connector,
            tier=Tier.T1,
        )


@pytest.mark.parametrize(
    "perm,tier_name",
    [
        ("write:orders", "T2"),
        ("write:orders", "T3"),
        ("send:message", "T2"),
        ("send:message", "T3"),
        ("exec:shell", "T3"),
        ("read:catalog", "T0"),
        ("read:catalog", "T1"),
    ],
)
def test_valid_permission_tier_combinations_construct_cleanly(
    perm: str, tier_name: str
) -> None:
    from agentsys.harness.registry import Tier, ToolSpec

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
    from agentsys.harness.factory import EquippedRuntime
    from agentsys.harness.interceptor import PolicyViolation, intercept

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
