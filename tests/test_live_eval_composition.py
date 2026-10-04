"""Live-eval: composition scenarios on the reference agents (#225, ADR-010
part 1, open question 5).

The behavior capability suite tests the kit's generic prompt on minimal
fixture agents, one capability at a time. Atomic tests miss a model confused
by many tools and a long prompt, so three reference agents from
`examples/agents/` keep a small set of end-to-end scenarios on top, in the
shape of Phase 1 (`tests/test_live_eval_phase1.py`):

- `sales-agent`: untrusted input, writes, the webhook. Places an order,
  sends a message, escalates for an unregistered customer, and remembers an
  earlier webhook turn through the real Postgres checkpointer.
- `developer-agent` over `operator-agent`: the sandbox and a two-level
  chain. Runs a command and reads a file in a real bubblewrap sandbox with
  the tools it inherits from `operator-agent`, and escalates when the
  sandbox's allowlist refuses a command: the condition comes from
  `operator-agent`'s policy, the instruction to escalate from
  `developer-agent`'s prompt, and the tool from `agent`.
- `data-agent`: reports. Searches the catalog, looks up a client, retrieves
  from the knowledge base, and ranks a report by units.

Every agent is loaded with `Agent.from_folder` (`reference_agent(name)`), the
path an adopter's own folder takes, never a predefined role by name, and is
served through `boot_live_harness`: the real HTTP API and WhatsApp webhook,
never `AgentRuntime.run_turn` directly. Each grant is stated explicitly and
checked against the agent's own declared permissions before booting.
Assertions target what the SYSTEM did (an executed row in the real
`audit_event` table, a tool's own recorded arguments and result, the
checkpointer's thread, the fake WhatsApp server's sends); the model's
wording is at most a soft signal.

One test per agent, so `-k developer_agent` runs exactly the scenarios a
change to `developer-agent` or `operator-agent` calls for (ADR-010 part 3).
Each test prints every turn's wall-clock time and the turns that hit the
agent's turn budget, the data `operator-agent`'s `total_execution_timeout_s`
is decided from.

Gating (#81): each scenario runs `EVAL_RUNS` times (default 3) and passes
when at least `DEFAULT_HAPPY_PATH_THRESHOLD` (80%) of runs pass.

Run with: `uv run --env-file .env pytest -m live tests/test_live_eval_composition.py -v -s`
(EVAL_PROVIDER comes from `.env`; see docs/platform/live-eval.md).
"""

from __future__ import annotations

import dataclasses
import os
import pathlib
import shutil
import statistics
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import pytest

from agents_system.agent.spec import Agent
from agents_system.connectors.operator import SandboxPolicy, TerminalPolicy
from agents_system.evals.live_harness import (
    SALES_AGENT_PERMISSIONS,
    LiveHarness,
    boot_live_harness,
    metric_value,
    reference_agent,
)
from agents_system.evals.schema import DEFAULT_HAPPY_PATH_THRESHOLD
from agents_system.harness.loader import AgentDefinition, RootConfig, resolve
from agents_system.harness.registry import ToolSpec

pytestmark = pytest.mark.live

_RUNS = int(os.environ.get("EVAL_RUNS", "3"))

#: A demo client guaranteed to exist (`demo/company/03_seed.sql` seeds ids
#: 1-12; id 1 is always in range).
_DEMO_CLIENT_PHONE = "+5491100000001"
#: A demo client guaranteed NOT to exist (ids stop at 12).
_UNKNOWN_CLIENT_PHONE = "+5491100000099"
#: A demo SKU guaranteed to exist (`articulos` seeds `ART-001..ART-0NN`).
_DEMO_SKU = "ART-001"

#: The explicit deploy-time grant for each agent: everything its folder
#: declares, plus `send:escalation` for `escalation_notifier`, which every
#: agent inherits from `agent`.
_SALES_AGENT_GRANT = (*SALES_AGENT_PERMISSIONS, "send:escalation")
_DEVELOPER_AGENT_GRANT = (
    "exec:command",
    "read:files",
    "read:knowledge_base",
    "send:escalation",
)
_DATA_AGENT_GRANT = (
    "read:catalog",
    "read:client_registry",
    "read:knowledge_base",
    "read:reports",
    "send:escalation",
)


# ---------------------------------------------------------------------------
# Helpers. Each live module is self-contained, matching Phase 1 and Phase 4.
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class _TurnClock:
    """Every turn's wall-clock seconds, measured at the HTTP client."""

    seconds: list[float] = dataclasses.field(default_factory=list)

    def report(self, harness: LiveHarness, label: str, metrics_text: str) -> None:
        timeouts = metric_value(
            metrics_text,
            "agent_turns_total",
            runtime_id=harness.runtime_id,
            outcome="timeout",
        )
        if not self.seconds:
            print(f"{label} turn durations: none recorded")
            return
        print(
            f"{label} model={harness.model_name} "
            f"turn durations: n={len(self.seconds)} "
            f"median={statistics.median(self.seconds):.1f}s "
            f"max={max(self.seconds):.1f}s "
            f"all={[round(value, 1) for value in self.seconds]} "
            f"timed_out_turns={int(timeouts or 0)}"
        )


async def _run_gated(
    label: str,
    attempt: Callable[[], Awaitable[None]],
    *,
    runs: int = _RUNS,
    threshold: float = DEFAULT_HAPPY_PATH_THRESHOLD,
) -> float:
    """Run *attempt* *runs* times. An `AssertionError` counts as a failed
    run (#81); the scenario passes when at least *threshold* of the runs
    succeed. Prints a per-run report so a failure is diagnosable from
    `pytest -s` output alone."""
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


def _wrap_recorder(
    spec: ToolSpec, sink: list[tuple[dict[str, object], object]]
) -> ToolSpec:
    """Wrap *spec*'s connector to append `(inputs, result)` to *sink*: the
    audit trail never carries a tool's arguments or result."""
    original = spec.connector

    async def recording_connector(
        inputs: dict[str, object], *, session: object = None
    ) -> object:
        result = await original(inputs, session=session)
        sink.append((dict(inputs), result))
        return result

    return dataclasses.replace(spec, connector=recording_connector)


async def _turn_text(harness: LiveHarness, clock: _TurnClock, prompt: str) -> str:
    """POST one turn to `/v1/chat/completions`, time it, and return the
    assistant's final text."""
    started = time.monotonic()
    response = await harness.send_chat_completion(prompt)
    clock.seconds.append(time.monotonic() - started)
    assert response.status_code == 200, response.text
    body = response.json()
    return str(body["choices"][0]["message"]["content"])


async def _assert_tool_executed(
    harness: LiveHarness, *, role: str, tool_name: str, since: datetime
) -> None:
    """Assert the real `audit_event` table records an EXECUTED call to
    *tool_name* for *role* after *since*, so an earlier run's call can never
    stand in for this one. Polls briefly: the audit sink writes in batches."""
    rows: list[object] = []

    async def landed() -> bool:
        nonlocal rows
        found = await harness.audit_rows(
            role=role,
            event_type="tool_call_attempted",
            tool_name=tool_name,
            occurred_after=since,
        )
        rows = [row for row in found if row.payload.get("executed")]
        return bool(rows)

    try:
        await harness.wait_until(landed, timeout=5.0, interval=0.2)
    except TimeoutError:
        raise AssertionError(
            f"no executed '{tool_name}' tool_call_attempted audit row for "
            f"role={role!r} in this turn"
        ) from None


def _resolved(agent: Agent) -> AgentDefinition:
    return resolve(agent._to_locator(), roots=RootConfig())


# ---------------------------------------------------------------------------
# sales-agent: untrusted input, writes, the webhook. One boot, seeded demo
# company, the webhook bound to the agent itself (`untrusted_input: true`).
# ---------------------------------------------------------------------------


async def test_sales_agent_composition() -> None:
    agent = reference_agent("sales-agent")
    definition = _resolved(agent)
    assert definition.untrusted_input is True
    assert set(_SALES_AGENT_GRANT) == set(definition.permissions)

    clock = _TurnClock()
    async with boot_live_harness(
        role=agent,
        runtime_id="composition-sales-agent",
        permissions=_SALES_AGENT_GRANT,
        seed_demo=True,
    ) as harness:
        print(f"\ncomposition: runtime={harness.runtime_id} model={harness.model_name}")

        async def places_an_order() -> None:
            since = datetime.now(UTC)
            # The product_id is stated outright (#225 re-baseline). The
            # reference catalog's results carry `sku` and `description` and
            # no `product_id`, and in this catalog the SKU IS the product id,
            # which nothing tells the model: asked for "SKU 'ART-001'" only,
            # `openai/gpt-6-sol` looked the client up, searched the catalog,
            # then declined to guess the id `order_writer` requires and
            # escalated, 3 of 3 runs. A careful model acts on a complete
            # request (#218); the assertion is unchanged.
            await _turn_text(
                harness,
                clock,
                f"This is {_DEMO_CLIENT_PHONE}. I confirm I want to order 2 "
                f"units of the exact catalog SKU '{_DEMO_SKU}' -- this code "
                "is already verified and confirmed, so you do not need to "
                "call catalog_search for it. In this catalog the SKU is the "
                f"product_id, so the order line is product_id '{_DEMO_SKU}', "
                "qty 2. Call order_writer now to place this order; do not "
                "escalate and do not ask again, the product match is certain.",
            )
            await _assert_tool_executed(
                harness, role="sales-agent", tool_name="order_writer", since=since
            )

        await _run_gated("sales-agent.order_writer", places_an_order)

        async def sends_a_message() -> None:
            since = datetime.now(UTC)
            # The exact text is given (#225 re-baseline). Asked to send "a
            # short message confirming their order is ready", a fact no tool
            # can verify, `openai/gpt-6-sol` sent it in 5 of 6 runs; in the
            # sixth it answered without calling any tool, and in another it
            # escalated first because the order status was unverifiable. A
            # careful model acts on a complete request (#218); the assertion
            # is unchanged.
            await _turn_text(
                harness,
                clock,
                f"Use message_sender to send '{_DEMO_CLIENT_PHONE}' exactly "
                "this text: 'Hello from the sales team, we received your "
                "message and will reply shortly.' You must actually call "
                "message_sender.",
            )
            await _assert_tool_executed(
                harness, role="sales-agent", tool_name="message_sender", since=since
            )

        await _run_gated("sales-agent.message_sender", sends_a_message)

        async def escalates_for_an_unregistered_customer() -> None:
            since = datetime.now(UTC)
            await _turn_text(
                harness,
                clock,
                f"Hi, this is {_UNKNOWN_CLIENT_PHONE} -- I'd like to order "
                "5 units of Item Alpha.",
            )
            await _assert_tool_executed(
                harness, role="sales-agent", tool_name="client_lookup", since=since
            )
            await _assert_tool_executed(
                harness,
                role="sales-agent",
                tool_name="escalation_notifier",
                since=since,
            )

        await _run_gated(
            "sales-agent.escalation_customer_not_registered",
            escalates_for_an_unregistered_customer,
        )

        async def remembers_across_a_second_webhook_turn() -> None:
            phone = f"+1555{uuid.uuid4().int % 10_000_000:07d}"

            # Only this conversation's replies: `outbound.sent` is shared by
            # every scenario and run of this boot (#24).
            def replies() -> list[str]:
                digits = "".join(filter(str.isdigit, phone))
                return [
                    entry["text"]["body"]
                    for entry in harness.outbound.sent
                    if "".join(filter(str.isdigit, entry.get("to", ""))) == digits
                ]

            first = await harness.deliver_webhook(
                phone=phone,
                text=(
                    f"Please check catalog_search for '{_DEMO_SKU}' and "
                    "tell me its description. You must call the tool."
                ),
            )
            assert first.status_code == 200, first.text
            await harness.wait_until(lambda: len(replies()) >= 1, timeout=60.0)

            second = await harness.deliver_webhook(
                phone=phone,
                text="Without searching again, what item code did I just ask you about?",
            )
            assert second.status_code == 200, second.text
            await harness.wait_until(lambda: len(replies()) >= 2, timeout=60.0)

            reply = replies()[-1]
            assert _DEMO_SKU in reply, (
                f"second turn's reply did not recall {_DEMO_SKU!r}: {reply!r}"
            )
            thread = await harness.read_thread(phone)
            assert thread is not None, (
                "no checkpointed thread for the conversation's phone number"
            )

        await _run_gated(
            "sales-agent.conversation_memory_two_webhook_turns",
            remembers_across_a_second_webhook_turn,
        )

        clock.report(harness, "sales-agent", await harness.scrape_metrics())


# ---------------------------------------------------------------------------
# developer-agent over operator-agent: the sandbox and a two-level chain.
# ---------------------------------------------------------------------------

_SANDBOX_NOTE_CONTENT = "Sprint status: green. No blockers."


@pytest.fixture
def developer_workspace(tmp_path: pathlib.Path) -> pathlib.Path:
    """A dedicated, disposable sandbox root -- never the process's own cwd
    or `$HOME` (`TerminalPolicy.__post_init__` refuses both)."""
    (tmp_path / "notes.txt").write_text(_SANDBOX_NOTE_CONTENT, encoding="utf-8")
    return tmp_path


async def test_developer_agent_composition(
    developer_workspace: pathlib.Path,
) -> None:
    if shutil.which("bwrap") is None:
        pytest.skip("developer-agent's sandboxed tools need bwrap")

    agent = reference_agent("developer-agent")
    definition = _resolved(agent)
    # The chain, deterministically: `use_term`/`read_file` and the limits
    # come from `operator-agent`, `escalation_notifier` from `agent`, and
    # `knowledge_retrieval` is the developer's own.
    operator = _resolved(reference_agent("operator-agent"))
    assert {"use_term", "read_file"} <= set(operator.tools)
    assert {
        "use_term",
        "read_file",
        "knowledge_retrieval",
        "escalation_notifier",
    } <= set(definition.tools)
    assert definition.execution_limits == operator.execution_limits
    assert "command_refused_by_policy" in definition.escalation_rules["conditions"]
    assert set(_DEVELOPER_AGENT_GRANT) == set(definition.permissions)
    assert definition.execution_limits is not None
    print(
        "developer-agent turn budget: total_execution_timeout_s="
        f"{definition.execution_limits['total_execution_timeout_s']}"
    )

    use_term_calls: list[tuple[dict[str, object], object]] = []
    read_file_calls: list[tuple[dict[str, object], object]] = []
    policy = TerminalPolicy(
        root=developer_workspace,
        allowed_commands=frozenset({"ls", "cat"}),
        sandbox=SandboxPolicy(),
    )

    clock = _TurnClock()
    async with boot_live_harness(
        role=agent,
        runtime_id="composition-developer-agent",
        permissions=_DEVELOPER_AGENT_GRANT,
        terminal_policy=policy,
        bind_webhook_to_role=False,
        tool_wrappers={
            "use_term": lambda spec: _wrap_recorder(spec, use_term_calls),
            "read_file": lambda spec: _wrap_recorder(spec, read_file_calls),
        },
    ) as harness:
        print(f"\ncomposition: runtime={harness.runtime_id} model={harness.model_name}")

        async def runs_ls_in_the_sandbox() -> None:
            use_term_calls.clear()
            since = datetime.now(UTC)
            await _turn_text(
                harness,
                clock,
                "Use the use_term tool to run `ls` (argv=['ls']) in the "
                "workspace root, then tell me what files you see. You must "
                "actually call the tool.",
            )
            await _assert_tool_executed(
                harness, role="developer-agent", tool_name="use_term", since=since
            )
            assert use_term_calls, "use_term was not recorded by the wrapper"
            _, result = use_term_calls[-1]
            assert isinstance(result, dict)
            assert result.get("exit_code") == 0, f"use_term failed: {result!r}"
            assert "notes.txt" in result.get("stdout", ""), (
                f"the real sandboxed `ls` did not see notes.txt: {result!r}"
            )

        await _run_gated("developer-agent.use_term", runs_ls_in_the_sandbox)

        async def reads_a_file_in_the_sandbox() -> None:
            read_file_calls.clear()
            since = datetime.now(UTC)
            await _turn_text(
                harness,
                clock,
                "Use the read_file tool to read 'notes.txt', then tell me "
                "what it says. You must actually call the tool.",
            )
            await _assert_tool_executed(
                harness, role="developer-agent", tool_name="read_file", since=since
            )
            assert read_file_calls, "read_file was not recorded by the wrapper"
            _, result = read_file_calls[-1]
            assert isinstance(result, dict)
            assert _SANDBOX_NOTE_CONTENT in result.get("content", ""), (
                f"read_file did not return the real file's content: {result!r}"
            )

        await _run_gated("developer-agent.read_file", reads_a_file_in_the_sandbox)

        async def escalates_a_command_the_sandbox_refuses() -> None:
            use_term_calls.clear()
            since = datetime.now(UTC)
            await _turn_text(
                harness,
                clock,
                "Use the use_term tool to run `uname -a` (argv=['uname', "
                "'-a']) and tell me which kernel this machine runs. You must "
                "actually call the tool.",
            )
            assert use_term_calls, "use_term was not recorded by the wrapper"
            refused = [
                result
                for inputs, result in use_term_calls
                if isinstance(result, dict)
                and result.get("error_kind") == "command_not_allowed"
            ]
            assert refused, (
                f"the sandbox never refused a command in this turn: {use_term_calls!r}"
            )
            await _assert_tool_executed(
                harness,
                role="developer-agent",
                tool_name="escalation_notifier",
                since=since,
            )

        await _run_gated(
            "developer-agent.escalation_command_refused_by_policy",
            escalates_a_command_the_sandbox_refuses,
        )

        clock.report(harness, "developer-agent", await harness.scrape_metrics())


# ---------------------------------------------------------------------------
# data-agent: reports. One boot over the seeded demo company.
# ---------------------------------------------------------------------------


async def test_data_agent_composition() -> None:
    agent = reference_agent("data-agent")
    definition = _resolved(agent)
    assert set(_DATA_AGENT_GRANT) == set(definition.permissions)

    catalog_calls: list[tuple[dict[str, object], object]] = []
    client_calls: list[tuple[dict[str, object], object]] = []
    report_calls: list[tuple[dict[str, object], object]] = []

    clock = _TurnClock()
    async with boot_live_harness(
        role=agent,
        runtime_id="composition-data-agent",
        permissions=_DATA_AGENT_GRANT,
        seed_demo=True,
        bind_webhook_to_role=False,
        tool_wrappers={
            "catalog_search": lambda spec: _wrap_recorder(spec, catalog_calls),
            "client_lookup": lambda spec: _wrap_recorder(spec, client_calls),
            "run_report": lambda spec: _wrap_recorder(spec, report_calls),
        },
    ) as harness:
        print(f"\ncomposition: runtime={harness.runtime_id} model={harness.model_name}")

        async def catalog_search_hit() -> None:
            catalog_calls.clear()
            since = datetime.now(UTC)
            await _turn_text(
                harness,
                clock,
                f"Call catalog_search with q='{_DEMO_SKU}', then tell me the "
                "product's description in one short sentence. You must "
                "actually call the tool, not just answer from memory.",
            )
            await _assert_tool_executed(
                harness, role="data-agent", tool_name="catalog_search", since=since
            )
            assert catalog_calls, "catalog_search was not recorded by the wrapper"
            _, result = catalog_calls[-1]
            assert isinstance(result, dict)
            assert result.get("classification") != "no_match", (
                f"catalog_search for {_DEMO_SKU!r} found no match: {result!r}"
            )
            assert result.get("results"), f"catalog_search returned no rows: {result!r}"

        await _run_gated("data-agent.catalog_search", catalog_search_hit)

        async def client_lookup_hit() -> None:
            client_calls.clear()
            since = datetime.now(UTC)
            await _turn_text(
                harness,
                clock,
                f"Call client_lookup with phone='{_DEMO_CLIENT_PHONE}', then "
                "tell me the client's name. You must actually call the tool.",
            )
            await _assert_tool_executed(
                harness, role="data-agent", tool_name="client_lookup", since=since
            )
            assert client_calls, "client_lookup was not recorded by the wrapper"
            _, result = client_calls[-1]
            assert isinstance(result, dict)
            assert result.get("client_id") is not None, (
                f"client_lookup for {_DEMO_CLIENT_PHONE!r} found no client: {result!r}"
            )

        await _run_gated("data-agent.client_lookup", client_lookup_hit)

        async def knowledge_retrieval_hit() -> None:
            since = datetime.now(UTC)
            await _turn_text(
                harness,
                clock,
                "Use knowledge_retrieval to find our shipping and delivery "
                "policy, then summarize it in one sentence. You must "
                "actually call the tool.",
            )
            await _assert_tool_executed(
                harness,
                role="data-agent",
                tool_name="knowledge_retrieval",
                since=since,
            )

        await _run_gated("data-agent.knowledge_retrieval", knowledge_retrieval_hit)

        async def top_products_by_units() -> None:
            report_calls.clear()
            since = datetime.now(UTC)
            await _turn_text(
                harness,
                clock,
                "Call run_report with report='top_products' and "
                "params={'order_by': 'units', 'months_back': 24, 'limit': "
                "5}. You must call it with EXACTLY those parameters, then "
                "name the single top product by units sold.",
            )
            await _assert_tool_executed(
                harness, role="data-agent", tool_name="run_report", since=since
            )
            assert report_calls, "run_report was not recorded by the wrapper"
            inputs, result = report_calls[-1]
            assert isinstance(result, dict)
            params = inputs.get("params")
            assert isinstance(params, dict) and params.get("order_by") == "units", (
                f"run_report was not called with order_by='units': {inputs!r}"
            )
            rows = result.get("rows")
            assert rows, f"run_report(top_products, units) returned no rows: {result!r}"
            quantities = [row["total_quantity"] for row in rows]
            assert quantities == sorted(quantities, reverse=True), (
                f"rows are not sorted by units descending: {quantities!r}"
            )

        await _run_gated("data-agent.top_products_by_units", top_products_by_units)

        clock.report(harness, "data-agent", await harness.scrape_metrics())
