"""Live smoke test for the #78 Phase 0 Slice 3 harness (opt-in, `-m live`).

Proves `boot_live_harness` end to end against a REAL model, a real throwaway
Postgres database, and the real Postgres checkpointer: one turn through the HTTP
API and one turn through the WhatsApp webhook -> deferred worker path, each
asserted the way the live-test plan's Principle demands -- against what the
SYSTEM did (audit rows, `/metrics` counters, the message the fake WhatsApp
server actually received), never the model's own wording.

Requires (see `docs/platform/live-eval.md`):
  - a reachable Postgres able to `CREATE DATABASE` (docker-compose's
    `postgres` service by default);
  - a reachable Redis (docker-compose's `redis` service by default);
  - a real model reachable through `EVAL_PROVIDER` (and its provider-specific
    env vars, e.g. `OPENAI_COMPATIBLE_*` for OpenRouter).

Run with: `pytest -m live tests/test_live_smoke.py -v`.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest

from agents_system.evals.live_harness import (
    LiveHarness,
    boot_live_harness,
    metric_value,
)

pytestmark = pytest.mark.live


@pytest.fixture
async def live_harness() -> AsyncIterator[LiveHarness]:
    async with boot_live_harness() as harness:
        yield harness


async def test_smoke_api_turn_and_webhook_turn(live_harness: LiveHarness) -> None:
    metrics_before = await live_harness.scrape_metrics()
    turns_before = (
        metric_value(
            metrics_before,
            "agent_turns_total",
            runtime_id=live_harness.runtime_id,
            outcome="ok",
        )
        or 0.0
    )

    # --- Turn 1: the HTTP API (POST /v1/chat/completions) -----------------
    api_response = await live_harness.send_chat_completion(
        "This is an automated harness check. Call the catalog_search tool "
        'with q="fernet". Then reply with one short sentence '
        "confirming you searched for it. You must actually call the tool, not just "
        "describe it."
    )
    assert api_response.status_code == 200, api_response.text
    correlation_id = api_response.headers["x-request-id"]
    api_body = api_response.json()
    assert api_body["choices"][0]["message"]["content"]

    api_audit_rows = await live_harness.audit_rows(correlation_id=correlation_id)
    assert any(
        row.event_type == "tool_call_attempted" and row.tool_name == "catalog_search"
        for row in api_audit_rows
    ), (
        "expected a catalog_search tool_call_attempted audit row for the API "
        f"turn's correlation id {correlation_id!r}; got "
        f"{[(row.event_type, row.tool_name) for row in api_audit_rows]}"
    )

    metrics_after_api = await live_harness.scrape_metrics()
    turns_after_api = metric_value(
        metrics_after_api,
        "agent_turns_total",
        runtime_id=live_harness.runtime_id,
        outcome="ok",
    )
    assert turns_after_api is not None
    assert turns_after_api >= turns_before + 1

    # --- Turn 2: the WhatsApp webhook -> deferred worker -------------------
    # A per-run phone number (mirroring the uuid4-based session_id above),
    # not a fixed one (#113 review): a fixed thread_id would let a stale
    # checkpoint from an earlier, possibly-broken run satisfy this run's own
    # `read_thread` assertion below, on top of `boot_live_harness` now also
    # deleting each run's own checkpoint threads at teardown.
    phone = f"+1555{uuid.uuid4().int % 10_000_000:07d}"
    # Captured BEFORE the request, not after (#113 review follow-up): the
    # API turn above already wrote a `role="sales-agent"`,
    # `event_type="tool_call_attempted"`, `tool_name="catalog_search"` audit
    # row, which is otherwise indistinguishable from a row the webhook turn
    # itself should write -- `occurred_after` is what actually scopes the
    # assertion below to this turn, per this module's own docstring design
    # ("role/event_type/occurred_after").
    webhook_turn_started_at = datetime.now(UTC)
    webhook_response = await live_harness.deliver_webhook(
        phone=phone,
        text=(
            "This is an automated harness check. Call the catalog_search "
            'tool with q="fernet". Then reply with one short '
            "sentence confirming you searched for it. You must actually call the "
            "tool, not just describe it."
        ),
    )
    assert webhook_response.status_code == 200, webhook_response.text
    assert webhook_response.json() == {"status": "ok"}

    await live_harness.wait_until(
        lambda: len(live_harness.outbound.sent) >= 1, timeout=60.0
    )

    sent = live_harness.outbound.sent[-1]
    assert sent["to"] == phone
    assert sent["text"]["body"]

    webhook_audit_rows = await live_harness.audit_rows(
        role="sales-agent",
        event_type="tool_call_attempted",
        tool_name="catalog_search",
        occurred_after=webhook_turn_started_at,
    )
    assert webhook_audit_rows, (
        "expected at least one catalog_search tool_call_attempted audit row "
        "for the sales-agent role (webhook turns carry no per-request "
        "correlation id -- see live_harness.py's module docstring)"
    )

    metrics_after_webhook = await live_harness.scrape_metrics()
    turns_after_webhook = metric_value(
        metrics_after_webhook,
        "agent_turns_total",
        runtime_id=live_harness.runtime_id,
        outcome="ok",
    )
    assert turns_after_webhook is not None
    assert turns_after_webhook >= turns_after_api + 1

    # The webhook worker only enables the checkpointer's thread_id when
    # `whatsapp_checkpointer_enabled` (the harness's default) -- confirms
    # the real Postgres checkpointer, not just the fake outbound send.
    thread = await live_harness.read_thread(phone)
    assert thread is not None
