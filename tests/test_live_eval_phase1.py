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

Since #225 (ADR-010) this module keeps the agents built with the `Agent` API
for one feature each: a declared command tool and the read-only SQL tool.
Its scenarios on the predefined roles moved to the composition scenarios on
the reference agents (`tests/test_live_eval_composition.py`): `data-agent`
and `sales-agent` as they were, and `operator-agent`'s sandboxed tools
through `developer-agent`, which inherits them. `summary-agent` keeps no
live scenario (ADR-010 open question 5).

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
from agents_system.connectors.sql_query_connector import (
    SqlQueryConfig,
    build_sql_query_tool_spec,
)
from agents_system.evals import live_harness as lh
from agents_system.evals.live_harness import (
    LiveHarness,
    boot_live_harness,
    provision_sql_readonly_fixture,
)
from agents_system.evals.schema import DEFAULT_HAPPY_PATH_THRESHOLD
from agents_system.harness.registry import ToolSpec
from agents_system.models.base import get_engine

pytestmark = pytest.mark.live

_RUNS = int(os.environ.get("EVAL_RUNS", "3"))

#: An arbitrary SKU `check_stock`'s declared pattern accepts (it is echoed
#: back, never looked up).
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
