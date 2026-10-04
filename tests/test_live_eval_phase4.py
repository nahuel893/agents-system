"""Live-eval: Phase 4 -- the full custom-agent lifecycle, through the real
HTTP API and the WhatsApp webhook (#84, live-test plan Phase 4,
`docs/delivery/live-test-plan.md`).

One live test walks the whole lifecycle as ONE scenario, through
`agents_system.evals.live_harness.boot_live_harness` (#78 Phase 0 Slice 3),
never `AgentRuntime.run_turn` directly:

1. Define a custom agent with the public `Agent` API
   (`Agent.from_folder`, `tests/fixtures/agents/phase4_lifecycle/roles/restock-helper`)
   that extends the reference `sales-agent` (`examples/agents/sales-agent`,
   supplied as `Agent.from_folder(..., extends=reference_agent("sales-agent"))`,
   #225 -- never the predefined role by name), adds a skill
   (`restock_alert`) and a tool (the declarative `check_stock` command
   tool, ADR-002 C.12), and tightens `max_tool_calls` to 5.
2. Register it with `create_app(agents={id: Agent}, grants=...)` under a
   chosen runtime id -- exactly what `boot_live_harness(role=...)` does.
3. Serve it: `GET /v1/models` lists it.
4. Converse with it over the real HTTP API and the real WhatsApp webhook.
5. Confirm its tool calls land in the real `audit_event` table under its own
   role name (`restock-helper`) -- the same taxonomy/redaction any agent's
   rows use, since it inherits `sales-agent`'s own `audit_policy`
   unchanged.
6. Update it: a new definition with a changed skill (an inline
   `skill_contents` override) and one fewer permission (`{"inherit": True,
   "remove": ["run:check_stock"]}`), re-registered under the SAME runtime id
   after a fresh `boot_live_harness` call -- a new application process, i.e.
   an "app restart". Prove the change took effect (deterministically, via
   `resolve()`, never only via the model's wording) and that the removed
   permission now blocks the tool: a `tool_denied` audit row lands at BOOT
   time (`harness/injector.py::resolve_command_tool_surface`), before any
   conversation happens, and no further `check_stock` call ever executes.

The one lifecycle assertion that needs no model -- that a variant which
raises a limit or loosens `untrusted_input` fails at boot with a clear
`DefinitionError` -- lives instead in
`tests/test_agent_lifecycle_safety_ceiling.py`, which needs no live
infrastructure and runs in the default offline suite.

Every assertion targets what the SYSTEM did (a real audit row, `/v1/models`'s
own response, the resolved `AgentDefinition`), per the live-test plan's own
Principle -- the model's final wording is at most a soft signal.

Requires the same live infra as `tests/test_live_eval_phase1.py` (see that
file's own module docstring and `docs/platform/live-eval.md`).

Run with: `pytest -m live tests/test_live_eval_phase4.py -v -s`.
"""

from __future__ import annotations

import os
import pathlib
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import pytest

from agents_system.agent.spec import Agent
from agents_system.evals.live_harness import (
    SALES_AGENT_PERMISSIONS,
    LiveHarness,
    boot_live_harness,
    reference_agent,
)
from agents_system.evals.schema import DEFAULT_HAPPY_PATH_THRESHOLD
from agents_system.harness.loader import RootConfig, resolve

pytestmark = pytest.mark.live

_RUNS = int(os.environ.get("EVAL_RUNS", "3"))

_RESTOCK_HELPER_PATH = (
    pathlib.Path(__file__).resolve().parent
    / "fixtures"
    / "agents"
    / "phase4_lifecycle"
    / "roles"
    / "restock-helper"
)

#: `restock-helper`'s full deploy-time grant: the reference `sales-agent`'s own six
#: permissions, `send:escalation` (inherited from the base `agent` role,
#: which declares `escalation_notifier` -- otherwise denied at boot for the
#: same reason `test_live_eval_phase1.py` grants it explicitly for its own
#: escalation scenario), and `run:check_stock`, `restock-helper`'s own
#: addition.
_RESTOCK_HELPER_GRANT = (*SALES_AGENT_PERMISSIONS, "send:escalation", "run:check_stock")

#: An arbitrary SKU matching `check_stock`'s declared param pattern
#: (`^[A-Za-z0-9_-]{1,32}$`) -- this fixture needs no seeded demo data,
#: exactly like `tests/fixtures/agents/command_tools/roles/cmdtool-role`.
_TEST_SKU = "SKU-4711"

#: v2's replacement skill content (an inline `skill_contents` override, never
#: written to the fixture folder) -- a genuinely different instruction whose
#: presence in the composed system prompt is this test's deterministic proof
#: that "update the definition" took effect.
_V2_SKILL_TEXT = (
    "After checking stock with check_stock, always add one sentence "
    "recommending the customer confirm the order soon if the SKU is "
    "running low."
)


# ---------------------------------------------------------------------------
# Gating helper (#81's vocabulary), copied from `test_live_eval_phase1.py` --
# each live test file is self-contained, matching that file's own convention.
# ---------------------------------------------------------------------------


async def _run_gated(
    label: str,
    attempt: Callable[[], Awaitable[None]],
    *,
    runs: int = _RUNS,
    threshold: float = DEFAULT_HAPPY_PATH_THRESHOLD,
) -> float:
    failures: list[str] = []
    passed = 0
    for index in range(runs):
        try:
            await attempt()
            passed += 1
            print(f"  {label} run {index}: pass")
        except AssertionError as error:
            failures.append(f"run {index}: {error}")
            print(f"  {label} run {index}: fail ({error})")
    rate = passed / runs
    print(f"{label}: {passed}/{runs} passed ({rate:.0%}, threshold {threshold:.0%})")
    assert rate >= threshold, (
        f"{label}: only {passed}/{runs} runs passed ({rate:.0%} < "
        f"{threshold:.0%}); failures: {'; '.join(failures)}"
    )
    return rate


async def _turn_text(harness: LiveHarness, prompt: str) -> str:
    response = await harness.send_chat_completion(prompt)
    assert response.status_code == 200, response.text
    body = response.json()
    return str(body["choices"][0]["message"]["content"])


async def _assert_tool_attempted(
    harness: LiveHarness,
    *,
    role: str,
    tool_name: str,
    occurred_after: datetime | None = None,
) -> None:
    """Assert the real `audit_event` table records at least one EXECUTED
    call to *tool_name* for *role*, optionally restricted to rows recorded
    after *occurred_after* (so a repeated call in the same boot cannot be
    mistaken for the one this particular turn made)."""
    rows = await harness.audit_rows(
        role=role,
        event_type="tool_call_attempted",
        tool_name=tool_name,
        occurred_after=occurred_after,
    )
    executed = [row for row in rows if row.payload.get("executed")]
    assert executed, (
        f"expected an executed '{tool_name}' tool_call_attempted audit row "
        f"for role={role!r}; got "
        f"{[(r.tool_name, r.payload.get('executed')) for r in rows]}"
    )


# ---------------------------------------------------------------------------
# The lifecycle, as one live test (issue #84).
# ---------------------------------------------------------------------------


async def test_custom_agent_full_lifecycle() -> None:
    # --- Step 1: define a custom agent with the public Agent API ---------
    # Its parent, the reference `sales-agent`, is supplied in Python: the
    # fixture's own `extends: ../sales-agent` names a sibling a copied
    # project would have, which this fixture folder does not (#225).
    v1 = Agent.from_folder(_RESTOCK_HELPER_PATH, extends=reference_agent("sales-agent"))
    v1_definition = resolve(v1._to_locator(), roots=RootConfig())
    assert v1_definition.untrusted_input is True, (
        "restock-helper must inherit sales-agent's untrusted_input: true "
        "to bind directly to the WhatsApp webhook below"
    )
    assert v1_definition.execution_limits is not None
    assert v1_definition.execution_limits["max_tool_calls"] == 5, (
        "the tightened limit did not resolve as expected"
    )
    assert "run:check_stock" in v1_definition.permissions
    assert any(tool.name == "check_stock" for tool in v1_definition.command_tools)

    # v2 -- the "update" -- is built now too, and its deterministic
    # definition-level checks run BEFORE any `boot_live_harness` call below:
    # `build_runtime` (offline, against the local test registry
    # `test_agent_safety_ceiling.py` already uses) fires the same
    # fire-and-forget audit `_emit` calls a real boot does, and running it
    # while a live harness's own real `AuditSink` is mid-shutdown between
    # the two boots below would let those calls race that shutdown for no
    # reason -- there is nothing about v2 that depends on v1 having booted.
    v2 = Agent.from_folder(
        _RESTOCK_HELPER_PATH,
        extends=reference_agent("sales-agent"),
        skill_contents={"restock_alert": _V2_SKILL_TEXT},
        permissions={"inherit": True, "remove": ["run:check_stock"]},
    )
    v2_definition = resolve(v2._to_locator(), roots=RootConfig())
    assert "run:check_stock" not in v2_definition.permissions, (
        "v2 must declare one fewer permission than v1"
    )
    assert any(tool.name == "check_stock" for tool in v2_definition.command_tools)
    assert v2_definition.execution_limits is not None
    assert v2_definition.execution_limits["max_tool_calls"] == 5

    # The skill's CONTENT is composed into the final prompt only once the
    # runtime is fully equipped (`harness/factory.py::_compose_prompt`) --
    # `AgentDefinition.system_prompt` (bare `resolve()`) never carries it, so
    # this deterministic proof calls `build_runtime` itself, offline.
    from conftest import build_test_registry

    from agents_system.harness.factory import build_runtime

    offline_registry = build_test_registry()
    v1_equipped = build_runtime(
        v1._to_locator(),
        offline_registry,
        granted_permissions=_RESTOCK_HELPER_GRANT,
        roots=RootConfig(),
    )
    v2_equipped = build_runtime(
        v2._to_locator(),
        offline_registry,
        granted_permissions=_RESTOCK_HELPER_GRANT,
        roots=RootConfig(),
    )
    assert _V2_SKILL_TEXT.strip() in v2_equipped.system_prompt, (
        "v2's changed skill did not reach the composed system prompt"
    )
    assert v1_equipped.system_prompt != v2_equipped.system_prompt

    runtime_id = f"phase4-restock-helper-{uuid.uuid4().hex[:8]}"

    # --- Steps 2-5: register, serve, converse, audit ----------------------
    async with boot_live_harness(
        role=v1,
        runtime_id=runtime_id,
        permissions=_RESTOCK_HELPER_GRANT,
        bind_webhook_to_role=True,  # safe: untrusted_input is True
    ) as harness:
        models_response = await harness.client.get("/v1/models")
        assert models_response.status_code == 200, models_response.text
        model_ids = {entry["id"] for entry in models_response.json()["data"]}
        assert runtime_id in model_ids, (
            f"/v1/models did not list {runtime_id!r}: {model_ids!r}"
        )

        async def checks_stock_via_the_api() -> None:
            checked_after = datetime.now(UTC)
            reply = await _turn_text(
                harness,
                f"Call the check_stock tool with sku='{_TEST_SKU}', then "
                "tell me exactly what it printed. You must actually call "
                "the tool.",
            )
            await _assert_tool_attempted(
                harness,
                role="restock-helper",
                tool_name="check_stock",
                occurred_after=checked_after,
            )
            assert _TEST_SKU in reply, (
                f"the model's reply did not mention {_TEST_SKU!r}: {reply!r}"
            )

        await _run_gated("restock-helper.check_stock.api", checks_stock_via_the_api)

        async def checks_stock_via_the_webhook() -> None:
            checked_after = datetime.now(UTC)
            phone = f"+1555{uuid.uuid4().int % 10_000_000:07d}"
            sent_before = len(harness.outbound.sent)
            response = await harness.deliver_webhook(
                phone=phone,
                text=(
                    f"Call the check_stock tool with sku='{_TEST_SKU}', then "
                    "tell me exactly what it printed. You must actually "
                    "call the tool."
                ),
            )
            assert response.status_code == 200, response.text
            await harness.wait_until(
                lambda: len(harness.outbound.sent) > sent_before, timeout=60.0
            )
            reply = harness.outbound.sent[-1]["text"]["body"]
            await _assert_tool_attempted(
                harness,
                role="restock-helper",
                tool_name="check_stock",
                occurred_after=checked_after,
            )
            assert _TEST_SKU in reply, (
                f"the webhook reply did not mention {_TEST_SKU!r}: {reply!r}"
            )

        await _run_gated(
            "restock-helper.check_stock.webhook", checks_stock_via_the_webhook
        )

    # --- Step 6: update the definition, "restart", prove the effect ------
    # v2 was already built and deterministically checked above, before v1
    # ever booted. A fresh `boot_live_harness` call is a fresh application
    # process -- the "app restart" the update is re-registered after. The
    # deploy-time grant is left UNCHANGED on purpose: the block below must
    # come from v2's own definition declaring one fewer permission, never
    # from a narrower deploy grant.
    async with boot_live_harness(
        role=v2,
        runtime_id=runtime_id,
        permissions=_RESTOCK_HELPER_GRANT,
        bind_webhook_to_role=True,
    ) as harness_v2:
        models_response = await harness_v2.client.get("/v1/models")
        assert models_response.status_code == 200, models_response.text
        model_ids = {entry["id"] for entry in models_response.json()["data"]}
        assert runtime_id in model_ids, (
            "v2 was not re-registered under the same runtime id after restart"
        )

        # The removed permission blocks the tool at BOOT time -- before any
        # conversation happens (`harness/injector.py::resolve_command_tool_surface`
        # denies it while building the equipped runtime), so this needs no
        # model at all. `record_tool_denied` is fire-and-forget onto the real
        # `AuditSink` (`audit/sink.py`: batches up to 50 events per 100ms
        # tick), scheduled as a bare asyncio task at boot -- polling (each
        # iteration a real awaited DB round trip) gives the event loop the
        # turns it needs to actually run that task and the drainer's next
        # tick, which a single immediate query right after boot cannot
        # guarantee.
        denied_rows: list[object] = []

        async def _denied_row_landed() -> bool:
            nonlocal denied_rows
            denied_rows = await harness_v2.audit_rows(
                role="restock-helper",
                event_type="tool_denied",
                tool_name="check_stock",
            )
            return bool(denied_rows)

        await harness_v2.wait_until(_denied_row_landed, timeout=10.0, interval=0.2)
        assert denied_rows, (
            "expected a tool_denied audit row for check_stock once "
            "run:check_stock was removed from restock-helper's own definition"
        )
        assert any(
            "run:check_stock" in row.payload.get("reason", "") for row in denied_rows
        ), f"tool_denied reason did not name the removed permission: {denied_rows!r}"

        # A single deterministic check, not gated (like
        # `test_live_eval_phase1.py::test_sql_readonly_role_cannot_change_the_fixture_data`):
        # whatever the model does, check_stock must never execute again,
        # because it is no longer bound to the runtime at all.
        checked_after = datetime.now(UTC)
        await _turn_text(
            harness_v2,
            f"Call the check_stock tool with sku='{_TEST_SKU}', then tell "
            "me exactly what it printed.",
        )
        post_update_rows = await harness_v2.audit_rows(
            role="restock-helper",
            event_type="tool_call_attempted",
            tool_name="check_stock",
            occurred_after=checked_after,
        )
        executed = [row for row in post_update_rows if row.payload.get("executed")]
        assert not executed, (
            f"check_stock executed after its permission was removed: {executed!r}"
        )
