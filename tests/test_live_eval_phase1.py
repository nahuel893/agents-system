"""Live-eval: Phase 1 -- the agent works end to end, through the real HTTP
API and the WhatsApp webhook, never the direct-runtime shortcut (#79,
live-test plan Phase 1, `docs/delivery/live-test-plan.md`).

Every scenario here boots the REAL application via
`agents_system.evals.live_harness.boot_live_harness` (#78 Phase 0 Slice 3)
and drives it through `LiveHarness.send_chat_completion` (`POST
/v1/chat/completions`) or `LiveHarness.deliver_webhook` (`POST /webhook` ->
`DeferredWebhookWorker`) -- never `AgentRuntime.run_turn` directly the way
`evals/runner.py::run_scenario` does. Every assertion targets what the
SYSTEM did (audit rows in the real `audit_event` table, a tool's own
recorded arguments/result, `/metrics` counters, the fake WhatsApp server's
recorded sends, the real Postgres checkpointer's thread state) -- the model's
own final wording is at most a soft signal, consistent with the live-test
plan's own Principle.

Requires (see `docs/platform/live-eval.md` and `live_harness.py`'s own
module docstring):
  - a reachable Postgres able to `CREATE DATABASE` (docker-compose's
    `postgres` service by default);
  - a reachable Redis (docker-compose's `redis` service by default);
  - a real model reachable through `EVAL_PROVIDER` (and its provider-specific
    env vars, e.g. `OPENAI_COMPATIBLE_*` for OpenRouter).

Gating (#81): each scenario below runs `EVAL_RUNS` times (default 3) and
passes as a whole when at least `DEFAULT_HAPPY_PATH_THRESHOLD` (80%) of runs
pass -- these are all happy-path scenarios, never guardrail ones (#76/#81's
100%-of-exercised-runs rule is for Phase 2, not this file).

Run with: `pytest -m live tests/test_live_eval_phase1.py -v -s`.
"""

from __future__ import annotations

import dataclasses
import os
import pathlib
import secrets
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from agents_system.agent.spec import Agent
from agents_system.connectors.operator import SandboxPolicy, TerminalPolicy
from agents_system.connectors.sql_query_connector import (
    SqlQueryConfig,
    build_sql_query_tool_spec,
)
from agents_system.evals import live_harness as lh
from agents_system.evals.live_harness import (
    SALES_AGENT_PERMISSIONS,
    LiveHarness,
    boot_live_harness,
    provision_sql_readonly_fixture,
)
from agents_system.evals.schema import DEFAULT_HAPPY_PATH_THRESHOLD
from agents_system.harness.registry import ToolSpec
from agents_system.models.base import get_engine

pytestmark = pytest.mark.live

_RUNS = int(os.environ.get("EVAL_RUNS", "3"))

#: A demo client id guaranteed to exist regardless of how many
#: `demo/company/03_seed.sql` seeds (it seeds 12; see the module's own
#: `padron_clientes` insert) -- id 1 is always in range.
_DEMO_CLIENT_PHONE = "+5491100000001"
#: A demo client id guaranteed to NOT exist -- `padron_clientes` only ever
#: seeds ids 1-12 (`1 + (i % 12)` over `facturas`), so 99 is safely outside.
_UNKNOWN_CLIENT_PHONE = "+5491100000099"
#: A demo SKU guaranteed to exist (`articulos` seeds `ART-001..ART-0NN`).
_DEMO_SKU = "ART-001"


# ---------------------------------------------------------------------------
# Gating helper (#81's vocabulary: a happy-path scenario passes at
# `DEFAULT_HAPPY_PATH_THRESHOLD` of its runs, not necessarily all of them --
# a real model is not perfectly deterministic).
# ---------------------------------------------------------------------------


async def _run_gated(
    label: str,
    attempt: Callable[[], Awaitable[None]],
    *,
    runs: int = _RUNS,
    threshold: float = DEFAULT_HAPPY_PATH_THRESHOLD,
) -> float:
    """Run *attempt* up to *runs* times. An `AssertionError` counts as a
    failed run (#81's own gating vocabulary); the scenario as a whole passes
    when at least *threshold* of the runs succeed. Returns the observed
    success rate and prints a per-run report so a failure is diagnosable
    from `pytest -s` output alone, mirroring
    `tests/test_live_eval_sql_query.py`'s own reporting style."""
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
    """Wrap *spec*'s connector to append `(inputs, result)` to *sink* before
    returning the real result -- the only way to inspect a tool's actual
    arguments/result live, since `ToolCallAttempted.payload` (the audit
    trail) never carries either, only name/timing/outcome."""
    original = spec.connector

    async def recording_connector(
        inputs: dict[str, object], *, session: object = None
    ) -> object:
        result = await original(inputs, session=session)
        sink.append((dict(inputs), result))
        return result

    return dataclasses.replace(spec, connector=recording_connector)


async def _turn_text(harness: LiveHarness, prompt: str) -> str:
    """POST one turn to `/v1/chat/completions` and return the assistant's
    final text -- a soft signal at most; every scenario below asserts on
    system-level evidence FIRST and uses this only as a secondary check."""
    response = await harness.send_chat_completion(prompt)
    assert response.status_code == 200, response.text
    body = response.json()
    return str(body["choices"][0]["message"]["content"])


async def _assert_tool_attempted(
    harness: LiveHarness, *, role: str, tool_name: str
) -> None:
    """Assert the real `audit_event` table records at least one EXECUTED
    call to *tool_name* for *role* -- proof the tool actually ran, not just
    that the model described running it (acceptance criterion: "an audit
    event reaches the real audit_event Postgres table")."""
    rows = await harness.audit_rows(
        role=role, event_type="tool_call_attempted", tool_name=tool_name
    )
    # `executed` lives inside the JSONB `payload`, not as its own ORM
    # column -- `audit/recorder.py::record_tool_call_attempted` writes it
    # into `raw_payload`, never onto `AuditEvent` itself.
    executed = [row for row in rows if row.payload.get("executed")]
    assert executed, (
        f"expected an executed '{tool_name}' tool_call_attempted audit row "
        f"for role={role!r}; got "
        f"{[(r.tool_name, r.payload.get('executed')) for r in rows]}"
    )


# ---------------------------------------------------------------------------
# data-agent: catalog_search (RAG-shaped lexical search), client_lookup,
# run_report ("top products by units" ordering), knowledge_retrieval.
# One boot (seed_demo=True): all four read from the SAME seeded demo company.
# ---------------------------------------------------------------------------

_DATA_AGENT_PERMISSIONS = (
    "read:catalog",
    "read:client_registry",
    "read:knowledge_base",
    "read:reports",
    "read:session",
)


async def test_data_agent_reads_the_real_seeded_demo_database() -> None:
    catalog_calls: list[tuple[dict[str, object], object]] = []
    client_calls: list[tuple[dict[str, object], object]] = []
    report_calls: list[tuple[dict[str, object], object]] = []

    async with boot_live_harness(
        role="data-agent",
        runtime_id="phase1-data-agent",
        permissions=_DATA_AGENT_PERMISSIONS,
        seed_demo=True,
        bind_webhook_to_role=False,
        tool_wrappers={
            "catalog_search": lambda spec: _wrap_recorder(spec, catalog_calls),
            "client_lookup": lambda spec: _wrap_recorder(spec, client_calls),
            "run_report": lambda spec: _wrap_recorder(spec, report_calls),
        },
    ) as harness:

        async def catalog_search_hit() -> None:
            catalog_calls.clear()
            await _turn_text(
                harness,
                f"Call catalog_search with q='{_DEMO_SKU}', then tell me the "
                "product's description in one short sentence. You must "
                "actually call the tool, not just answer from memory.",
            )
            await _assert_tool_attempted(
                harness, role="data-agent", tool_name="catalog_search"
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
            await _turn_text(
                harness,
                f"Call client_lookup with phone='{_DEMO_CLIENT_PHONE}', then "
                "tell me the client's name. You must actually call the tool.",
            )
            await _assert_tool_attempted(
                harness, role="data-agent", tool_name="client_lookup"
            )
            assert client_calls, "client_lookup was not recorded by the wrapper"
            _, result = client_calls[-1]
            assert isinstance(result, dict)
            assert result.get("client_id") is not None, (
                f"client_lookup for {_DEMO_CLIENT_PHONE!r} found no client: {result!r}"
            )

        await _run_gated("data-agent.client_lookup", client_lookup_hit)

        async def knowledge_retrieval_hit() -> None:
            await _turn_text(
                harness,
                "Use knowledge_retrieval to find our shipping and delivery "
                "policy, then summarize it in one sentence. You must "
                "actually call the tool.",
            )
            await _assert_tool_attempted(
                harness, role="data-agent", tool_name="knowledge_retrieval"
            )

        await _run_gated("data-agent.knowledge_retrieval", knowledge_retrieval_hit)

        async def top_products_by_units() -> None:
            report_calls.clear()
            await _turn_text(
                harness,
                "Call run_report with report='top_products' and "
                "params={'order_by': 'units', 'months_back': 24, 'limit': "
                "5}. You must call it with EXACTLY those parameters, then "
                "name the single top product by units sold.",
            )
            await _assert_tool_attempted(
                harness, role="data-agent", tool_name="run_report"
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


# ---------------------------------------------------------------------------
# sales-agent: order_writer, message_sender, escalation
# (customer_not_registered), and conversation memory across two webhook
# turns via the real Postgres checkpointer. One boot (seed_demo=True).
# ---------------------------------------------------------------------------


async def test_sales_agent_orders_messages_escalates_and_remembers() -> None:
    async with boot_live_harness(
        role="sales-agent",
        runtime_id="phase1-sales-agent",
        permissions=(*SALES_AGENT_PERMISSIONS, "send:escalation"),
        seed_demo=True,
    ) as harness:

        async def places_an_order() -> None:
            # Even directed not to, the model still calls catalog_search
            # first -- the reference backend now reports a real confidence
            # signal for the exact SKU (#117: `classification: "direct"`,
            # `similarity: 1.0`), so this no longer routes the model into
            # `confidence_below_threshold`.
            await _turn_text(
                harness,
                f"This is {_DEMO_CLIENT_PHONE}. I confirm I want to order 2 "
                f"units of the exact catalog SKU '{_DEMO_SKU}' -- this code "
                "is already verified and confirmed, so you do not need to "
                "call catalog_search for it. Call order_writer now to place "
                "this order; do not escalate and do not ask again, the "
                "product match is certain.",
            )
            await _assert_tool_attempted(
                harness, role="sales-agent", tool_name="order_writer"
            )

        # #117 fixed the root cause (the reference `catalog_search` backend's
        # constant `classification: "ambiguous"` / `similarity: None`, even
        # for an exact SKU match). Re-verified live (EVAL_RUNS=3): 3/3
        # `order_writer` calls, up from the earlier 1/6 across two real
        # attempts -- no longer a documented xfail (formerly
        # `_run_gated_xfail`, tracked at #79/#117).
        await _run_gated("sales-agent.order_writer", places_an_order)

        async def sends_a_message() -> None:
            await _turn_text(
                harness,
                f"Use message_sender to send '{_DEMO_CLIENT_PHONE}' a short "
                "message confirming their order is ready. You must "
                "actually call message_sender.",
            )
            await _assert_tool_attempted(
                harness, role="sales-agent", tool_name="message_sender"
            )

        await _run_gated("sales-agent.message_sender", sends_a_message)

        async def escalates_for_an_unregistered_customer() -> None:
            await _turn_text(
                harness,
                f"Hi, this is {_UNKNOWN_CLIENT_PHONE} -- I'd like to order "
                "5 units of Item Alpha.",
            )
            await _assert_tool_attempted(
                harness, role="sales-agent", tool_name="client_lookup"
            )
            await _assert_tool_attempted(
                harness, role="sales-agent", tool_name="escalation_notifier"
            )

        await _run_gated(
            "sales-agent.escalation_customer_not_registered",
            escalates_for_an_unregistered_customer,
        )

        async def remembers_across_a_second_webhook_turn() -> None:
            phone = f"+1555{uuid.uuid4().int % 10_000_000:07d}"

            # Only this conversation's replies: `outbound.sent` is shared by
            # every scenario and run of this boot, so counting all of it let
            # an earlier run's late reply stand in for this one's (#24).
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
                text=(
                    "Without searching again, what item code did I just ask you about?"
                ),
            )
            assert second.status_code == 200, second.text
            await harness.wait_until(lambda: len(replies()) >= 2, timeout=60.0)

            reply = replies()[-1]
            assert _DEMO_SKU in reply, (
                f"second turn's reply did not recall {_DEMO_SKU!r}: {reply!r}"
            )

            thread = await harness.read_thread(phone)
            assert thread is not None, (
                "no checkpointed thread found for the conversation's phone "
                "number -- the Postgres checkpointer did not persist it"
            )

        await _run_gated(
            "sales-agent.conversation_memory_two_webhook_turns",
            remembers_across_a_second_webhook_turn,
        )


# ---------------------------------------------------------------------------
# summary-agent: conversation_summarizer, over the reference backend's own
# pre-seeded transcript (`evals.live_registry.DEMO_SUMMARY_SESSION_ID`) --
# no demo-company seeding needed.
# ---------------------------------------------------------------------------

_SUMMARY_AGENT_PERMISSIONS = (
    "read:conversation_logs",
    "read:knowledge_base",
    "read:session",
    "write:summary_output",
)


async def test_summary_agent_summarizes_a_seeded_conversation() -> None:
    from agents_system.evals.live_registry import DEMO_SUMMARY_SESSION_ID

    async with boot_live_harness(
        role="summary-agent",
        runtime_id="phase1-summary-agent",
        permissions=_SUMMARY_AGENT_PERMISSIONS,
        bind_webhook_to_role=False,
    ) as harness:

        async def summarizes() -> None:
            await _turn_text(
                harness,
                f"Use conversation_summarizer on session_id="
                f"'{DEMO_SUMMARY_SESSION_ID}' and give me the summary. You "
                "must actually call the tool.",
            )
            await _assert_tool_attempted(
                harness,
                role="summary-agent",
                tool_name="conversation_summarizer",
            )

        await _run_gated("summary-agent.conversation_summarizer", summarizes)


# ---------------------------------------------------------------------------
# operator-agent: use_term and read_file, against a REAL bubblewrap sandbox
# (never the always-refusing default `terminal_policy=None`).
# ---------------------------------------------------------------------------

_OPERATOR_AGENT_PERMISSIONS = ("exec:command", "read:files")
_SANDBOX_NOTE_CONTENT = "Sprint status: green. No blockers."


@pytest.fixture
def operator_workspace(tmp_path: pathlib.Path) -> pathlib.Path:
    """A dedicated, disposable sandbox root -- never the process's own cwd
    or `$HOME` (`TerminalPolicy.__post_init__` refuses both), mirroring
    `tests/test_live_eval_roles.py`'s own fixture for the same two tools."""
    (tmp_path / "notes.txt").write_text(_SANDBOX_NOTE_CONTENT, encoding="utf-8")
    return tmp_path


async def test_operator_agent_runs_sandboxed_commands(
    operator_workspace: pathlib.Path,
) -> None:
    use_term_calls: list[tuple[dict[str, object], object]] = []
    read_file_calls: list[tuple[dict[str, object], object]] = []
    policy = TerminalPolicy(
        root=operator_workspace,
        allowed_commands=frozenset({"ls", "cat"}),
        sandbox=SandboxPolicy(),
    )

    async with boot_live_harness(
        role="operator-agent",
        runtime_id="phase1-operator-agent",
        permissions=_OPERATOR_AGENT_PERMISSIONS,
        terminal_policy=policy,
        bind_webhook_to_role=False,
        tool_wrappers={
            "use_term": lambda spec: _wrap_recorder(spec, use_term_calls),
            "read_file": lambda spec: _wrap_recorder(spec, read_file_calls),
        },
    ) as harness:

        async def runs_ls_via_use_term() -> None:
            use_term_calls.clear()
            await _turn_text(
                harness,
                "Use the use_term tool to run `ls` (argv=['ls']) in the "
                "workspace root, then tell me what files you see. You must "
                "actually call the tool.",
            )
            await _assert_tool_attempted(
                harness, role="operator-agent", tool_name="use_term"
            )
            assert use_term_calls, "use_term was not recorded by the wrapper"
            _, result = use_term_calls[-1]
            assert isinstance(result, dict)
            assert result.get("exit_code") == 0, f"use_term failed: {result!r}"
            assert "notes.txt" in result.get("stdout", ""), (
                f"the real sandboxed `ls` did not see notes.txt: {result!r}"
            )

        await _run_gated("operator-agent.use_term", runs_ls_via_use_term)

        async def reads_notes_via_read_file() -> None:
            read_file_calls.clear()
            await _turn_text(
                harness,
                "Use the read_file tool to read 'notes.txt', then tell me "
                "what it says. You must actually call the tool.",
            )
            await _assert_tool_attempted(
                harness, role="operator-agent", tool_name="read_file"
            )
            assert read_file_calls, "read_file was not recorded by the wrapper"
            _, result = read_file_calls[-1]
            assert isinstance(result, dict)
            assert _SANDBOX_NOTE_CONTENT in result.get("content", ""), (
                f"read_file did not return the real file's content: {result!r}"
            )

        await _run_gated("operator-agent.read_file", reads_notes_via_read_file)


# ---------------------------------------------------------------------------
# A test agent built with the Agent API (`Agent.from_folder`), declaring one
# `command_tools:` entry -- no predefined platform role ships one live.
# Reuses the exact fixture role `harness/loader.py`'s own tests already
# validate (`tests/fixtures/agents/command_tools/roles/cmdtool-role`),
# wrapping `/usr/bin/echo` under the sandboxed command-tool path.
# ---------------------------------------------------------------------------

_CMDTOOL_ROLE_PATH = (
    pathlib.Path(__file__).resolve().parent
    / "fixtures"
    / "agents"
    / "command_tools"
    / "roles"
    / "cmdtool-role"
)


async def test_command_tools_agent_runs_a_declared_command_tool() -> None:
    # `check_stock` is a declarative `command_tools:` entry
    # (`harness/injector.py::build_command_tool_specs`), injected directly
    # from the resolved role definition -- NEVER through the `ToolRegistry`
    # `tool_wrappers`/`extra_tool_specs` operate on (confirmed against a
    # real run: the wrapper's sink stayed empty while the audit row for the
    # SAME call showed `executed: true`). Its actual stdout is therefore not
    # inspectable the way a registry-backed tool's is; the audit row (proof
    # it genuinely ran, sandboxed) plus the model's own final text (a soft
    # signal, per the live-test plan's Principle) are what this scenario can
    # assert on.
    async with boot_live_harness(
        role=Agent.from_folder(_CMDTOOL_ROLE_PATH),
        runtime_id="phase1-cmdtool-agent",
        permissions=("run:check_stock",),
        bind_webhook_to_role=False,
    ) as harness:

        async def runs_check_stock() -> None:
            reply = await _turn_text(
                harness,
                f"Call the check_stock tool with sku='{_DEMO_SKU}', then "
                "tell me exactly what it printed. You must actually call "
                "the tool.",
            )
            await _assert_tool_attempted(
                harness, role="cmdtool-role", tool_name="check_stock"
            )
            assert _DEMO_SKU in reply, (
                f"the model's reply did not mention {_DEMO_SKU!r}: {reply!r}"
            )

        await _run_gated("cmdtool-role.check_stock", runs_check_stock)


# ---------------------------------------------------------------------------
# A test agent built with the Agent API (`Agent.from_folder`), equipping the
# read-only SQL tool (#80, ADR-007) -- no predefined role does. Provisions
# `sql_readonly` (`scripts/provision_sql_readonly.sql`) against ITS OWN
# throwaway database, independent of `boot_live_harness`'s own one (see
# `provision_sql_readonly_fixture`'s docstring for why).
# ---------------------------------------------------------------------------

_SQL_ROLE = "sql-analyst-agent"


def _write_sql_analyst_role(folder: pathlib.Path) -> None:
    (folder / "role.md").write_text(
        f"---\nname: {_SQL_ROLE}\n---\n\n"
        "You answer questions about sales data. Every number you give comes "
        "from a query you ran with the sql_query tool in this conversation; "
        "never estimate or invent one. You only read data: you never change "
        "it, and you say so when asked to.\n",
        encoding="utf-8",
    )
    (folder / "manifest.md").write_text(
        f"---\nrole: {_SQL_ROLE}\ntools:\n  - sql_query\nskills: []\n"
        "context: {}\npermissions:\n  - query:sql\n---\n\n"
        "Test-only role for #79 Phase 1 (mirrors #80's own test role).\n",
        encoding="utf-8",
    )
    (folder / "policy.md").write_text(
        f"---\nrole: {_SQL_ROLE}\nautonomy: supervised\n"
        "untrusted_input: false\nexecution_limits: null\n---\n\n"
        "Test-only policy.\n",
        encoding="utf-8",
    )


@pytest.fixture
async def sql_fixture_database_url() -> AsyncIterator[str]:
    """A throwaway database, independent of any `boot_live_harness`
    database, holding `tests/fixtures/sql_query_fixture.sql` and the
    provisioned `sql_readonly` role -- see `provision_sql_readonly_fixture`'s
    own docstring for why this must exist and be provisioned BEFORE
    `boot_live_harness` is even called."""
    admin_url = os.environ.get(
        lh._ADMIN_DATABASE_URL_ENV, lh.DEFAULT_ADMIN_DATABASE_URL
    )
    db_name = f"phase1_sql_{uuid.uuid4().hex[:12]}"
    database_url = lh._with_database(admin_url, db_name)
    await lh._create_database(admin_url, db_name)
    try:
        yield database_url
    finally:
        await lh._drop_database(admin_url, db_name)


async def test_sql_query_agent_answers_an_ad_hoc_question(
    tmp_path: pathlib.Path, sql_fixture_database_url: str
) -> None:
    password = secrets.token_hex(16)
    sql_readonly_url = await provision_sql_readonly_fixture(
        sql_fixture_database_url, password=password
    )
    sql_engine: AsyncEngine = get_engine(sql_readonly_url)
    calls: list[tuple[dict[str, object], object]] = []
    try:
        sql_tool_spec = build_sql_query_tool_spec(
            sql_engine,
            SqlQueryConfig(
                views={
                    "sql_tool_fixture.sales_v": (
                        "One row per sale. Columns: id (integer), product "
                        "(text, e.g. 'product-0'), amount (numeric, "
                        "currency), sold_on (date)."
                    )
                }
            ),
        )
        wrapped_sql_tool_spec = _wrap_recorder(sql_tool_spec, calls)

        _write_sql_analyst_role(tmp_path)

        async with boot_live_harness(
            role=Agent.from_folder(tmp_path, name=_SQL_ROLE),
            runtime_id="phase1-sql-analyst-agent",
            permissions=("query:sql",),
            bind_webhook_to_role=False,
            extra_tool_specs=(wrapped_sql_tool_spec,),
        ) as harness:

            async def answers_via_sql_query() -> None:
                calls.clear()
                await _turn_text(
                    harness,
                    "Which product had the highest total sales amount, and "
                    "what was that total? Answer using the sql_query tool "
                    "over sql_tool_fixture.sales_v -- you must actually run "
                    "a query, never estimate.",
                )
                await _assert_tool_attempted(
                    harness, role=_SQL_ROLE, tool_name="sql_query"
                )
                assert calls, "sql_query was not recorded by the wrapper"
                _, result = calls[-1]
                assert isinstance(result, dict)
                assert "error" not in result, f"sql_query returned an error: {result!r}"
                assert result.get("rows"), f"sql_query returned no rows: {result!r}"

            await _run_gated("sql-analyst-agent.sql_query", answers_via_sql_query)
    finally:
        await sql_engine.dispose()


# ---------------------------------------------------------------------------
# The read-only role really is read-only against this exact fixture -- a
# single deterministic check (#80's own established pattern), not gated:
# whatever the model does, the rows must still be there afterwards.
# ---------------------------------------------------------------------------


async def test_sql_readonly_role_cannot_change_the_fixture_data(
    sql_fixture_database_url: str,
) -> None:
    password = secrets.token_hex(16)
    sql_readonly_url = await provision_sql_readonly_fixture(
        sql_fixture_database_url, password=password
    )
    admin_engine: AsyncEngine = get_engine(sql_fixture_database_url)
    sql_engine: AsyncEngine = get_engine(sql_readonly_url)
    try:
        async with sql_engine.connect() as conn:
            with pytest.raises(Exception):  # noqa: B017 - any DBAPI refusal is correct
                await conn.execute(
                    text("DELETE FROM sql_tool_fixture.sales WHERE id = 1")
                )
        async with admin_engine.connect() as conn:
            remaining = (
                await conn.execute(text("SELECT count(*) FROM sql_tool_fixture.sales"))
            ).scalar()
        assert remaining == 40
    finally:
        await sql_engine.dispose()
        await admin_engine.dispose()
