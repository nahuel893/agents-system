"""Live harness for issue #78 Phase 0 Slice 3.

Boots the REAL application -- the HTTP API, the WhatsApp webhook route and
its deferred worker, a real (throwaway) Postgres database holding the real
checkpointer's conversation threads (#24), and audit persisted to the real
`audit_event` table -- instead
of calling `AgentRuntime.run_turn` directly the way `evals/runner.py` does.
Phase 1 (#79) and Phase 2 (extends #76) are meant to build their own live
scenarios on top of `boot_live_harness` below.

Design decisions
-----------------
- **In-process ASGI transport, real lifespan** (not a subprocess `uvicorn`):
  `create_app`'s `lifespan` context manager is entered directly via
  `app.router.lifespan_context(app)` -- the same mechanism Starlette's own
  `TestClient` uses -- so every resource the real deployment boots (the
  engine, the audit sink, the checkpointer, the runtime cache, the deferred
  webhook worker) is genuinely constructed and torn down, with no second
  process, no port-polling race, and no extra dependency
  (`asgi-lifespan` was considered and rejected: it adds a dependency for
  exactly what `app.router.lifespan_context` already does). Requests go
  through `httpx.ASGITransport`, a real ASGI call into the app -- not a
  direct Python call into a handler function.
- **A throwaway Postgres database**, created fresh and migrated to
  `alembic upgrade head` (the same pattern the CI `audit-migration` /
  `outbox-migration` jobs use for a disposable schema), dropped at teardown.
  Never the operator's own `agents_system` database.
- **The real checkpointer** (#24): LangGraph's Postgres saver on the
  throwaway database, whose checkpoint tables `alembic upgrade head` creates
  (revision 007). It runs for real (`WHATSAPP_CHECKPOINTER_ENABLED` defaults
  `True`; nothing here disables it), so `read_thread` reads an actual
  persisted LangGraph checkpoint. Every thread this run writes lives in that
  database and is dropped with it at teardown, so a checkpoint left behind by
  one run can never be mistaken for proof that a LATER run's checkpointing
  actually worked (#113 review). `used_thread_ids` still records which
  threads `deliver_webhook` drove.
- **One `boot_live_harness` at a time per process.** Booting mutates the
  process-global `os.environ` and the process-global `get_settings()`
  `lru_cache` -- there is no per-instance `Settings` isolation. A whole
  `boot_live_harness()` lifetime (boot through teardown) is therefore
  serialized on a module-level `asyncio.Lock`: a second concurrent call
  blocks until the first's context manager fully exits. Do NOT
  `asyncio.gather()` two `boot_live_harness()` calls expecting them to run
  side by side -- they will run one after the other, and while one is
  active its env/settings are the only ones in effect process-wide (#113
  review; Phase 1 #79 and Phase 2 #76-extends build on this harness and
  must not reach for concurrent boots without first giving each
  `LiveHarness` its own isolated `Settings` instance instead of mutating
  the shared process globals).
- **Outbound WhatsApp never reaches Meta.** `WhatsAppClient` posts to
  `Settings.whatsapp_graph_api_url` -- an existing configuration seam, no
  code change needed -- pointed here at `FakeWhatsAppServer`, a real local
  HTTP server that records every send instead of a mocked transport, since
  `WhatsAppClient` owns a real `httpx.AsyncClient` with no injectable
  transport of its own.
- **`catalog_search` is the smoke scenario's tool of choice**, not
  `order_writer`. `order_writer` is a write tool that creates orders;
  `catalog_search` is a safe read tool that every `sales-agent` declares,
  needing no seeded data, so a smoke scenario proving the HARNESS's wiring can
  use it cleanly.
- **Correlation id caveat**: `AuditEvent.correlation_id` is read from
  `structlog`'s `request_id` contextvar (`audit/recorder.py::
  _correlation_id_from_context`), which only `RequestIdMiddleware` binds --
  only for an HTTP request. A turn run by `DeferredWebhookWorker` (no
  in-flight HTTP request of its own) always records `correlation_id="none"`.
  `LiveHarness.audit_rows` therefore filters the webhook turn's rows by
  `role`/`event_type`/`occurred_after` instead of by correlation id -- this
  is an existing platform gap, not something this slice changes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import socket
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import structlog
from langchain_core.language_models import BaseChatModel
from prometheus_client.parser import text_string_to_metric_families

from agents_system.extras import raise_missing_extra

try:
    import uvicorn
    from fastapi import FastAPI
    from sqlalchemy import Select, select, text
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
except ImportError as exc:
    raise_missing_extra("server", "The live evaluation harness", exc)

from agents_system.agent.spec import Agent
from agents_system.config import get_settings
from agents_system.connectors.operator import TerminalPolicy
from agents_system.evals.live_registry import build_live_registry_factory
from agents_system.evals.provider import build_eval_model
from agents_system.harness.registry import RegistryFactory, ToolRegistry, ToolSpec
from agents_system.integration.meta_signature import sign_payload
from agents_system.main import create_app
from agents_system.models.audit_event import AuditEvent
from agents_system.models.base import get_session_factory
from agents_system.models.outbox import OutboxWork

logger = structlog.get_logger()

#: This module's own directory is `src/agents_system/evals/`; the repo root
#: is three parents up. Shared by `_run_migrations` (already used it inline)
#: and Phase 1 (#79)'s `_seed_demo_company`/`render_provision_sql_readonly`
#: below, all of which locate a checked-in file relative to the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[3]

#: Overridable so a developer whose docker-compose stack uses different
#: credentials/ports is not stuck with the checked-in defaults
#: (`docker-compose.yml`'s own `postgres`/`redis` services).
_ADMIN_DATABASE_URL_ENV = "LIVE_HARNESS_ADMIN_DATABASE_URL"
_REDIS_URL_ENV = "LIVE_HARNESS_REDIS_URL"
#: Connects to Postgres's own always-present `postgres` maintenance
#: database, not `docker-compose.yml`'s `agents_system` -- `CREATE DATABASE`
#: cannot run against the database being created, and `postgres` needs no
#: assumption about what else has (or has not) provisioned the app's own
#: database name on a given host.
DEFAULT_ADMIN_DATABASE_URL = (
    "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/postgres"
)
DEFAULT_REDIS_URL = "redis://127.0.0.1:6379/0"

#: Serializes `boot_live_harness`'s whole lifetime (see the module docstring's
#: "One `boot_live_harness` at a time per process" note, #113 review): booting
#: mutates the process-global `os.environ` and `get_settings()` cache with no
#: per-instance isolation, so two overlapping boots would silently invalidate
#: each other's already-issued credentials.
_boot_lock = asyncio.Lock()

#: The reference agents (ADR-010, #222): the eight example agent folders in
#: the repository's `examples/agents/`, outside the wheel. The live suite runs
#: from a checkout (like `_run_migrations` and `_seed_demo_company` above), so
#: it loads them from there with `Agent.from_folder`, the path an adopter's
#: own folder takes -- never a concrete predefined role by name (#225).
REFERENCE_AGENTS_ROOT = _REPO_ROOT / "examples" / "agents"


def reference_agent(name: str) -> Agent:
    """The reference agent *name* (`"sales-agent"`, `"developer-agent"`, ...),
    loaded from `REFERENCE_AGENTS_ROOT` as an `Agent` (#225).

    *name* must be the name of one folder directly under the root: anything
    else (a path, an unknown name) raises `ValueError` listing the names
    there are, before anything is booted.
    """
    available = sorted(
        path.name for path in REFERENCE_AGENTS_ROOT.iterdir() if path.is_dir()
    )
    if name not in available:
        raise ValueError(
            f"{name!r} is not a reference agent; the reference agents under "
            f"examples/agents/ are: {', '.join(available)}"
        )
    return Agent.from_folder(REFERENCE_AGENTS_ROOT / name)


#: `examples/agents/sales-agent/manifest.md`'s own `permissions:` list,
#: repeated here (not imported) because the manifest is data, not code --
#: this is the deploy-time GRANT `create_app(grants=...)` requires per
#: registered runtime id, independent of what the agent happens to declare.
#: `boot_live_harness` grants it to its default agent, the reference
#: `sales-agent`; `tests/test_live_harness.py` checks it against the manifest.
SALES_AGENT_PERMISSIONS: tuple[str, ...] = (
    "read:catalog",
    "read:client_registry",
    "write:orders",
    "write:order_items",
    "read:price_lists",
    "send:message",
)

#: Runtime id for `boot_live_harness(bind_webhook_to_role=False)`'s
#: WhatsApp-bound placeholder (#79, Phase 1) -- see its own docstring.
_WEBHOOK_PLACEHOLDER_RUNTIME_ID = "live-harness-webhook-placeholder"


def _webhook_placeholder_agent() -> Agent:
    """A minimal, always-boot-safe agent for `WHATSAPP_RUNTIME_ID` when a
    scenario's own *role* does not itself resolve `untrusted_input: true`
    (most reference agents, and a bare `Agent(...)`'s inherited
    default, do not) and never calls `deliver_webhook` -- see
    `boot_live_harness`'s own `bind_webhook_to_role` docstring. Explicit
    `untrusted_input=True` overrides whatever its own `extends:` chain
    resolves, exactly like any other `Agent(...)` field."""
    return Agent(name=_WEBHOOK_PLACEHOLDER_RUNTIME_ID, untrusted_input=True)


def _free_port() -> int:
    """Bind an ephemeral port, close it immediately, and return its number.

    A small, accepted race (something else could grab the port before
    `FakeWhatsAppServer.start` binds it) -- adequate for a local, opt-in test
    harness, and the same shortcut `demo/app.py`-adjacent tooling elsewhere
    in this repo already takes for "pick a free local port".
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def build_meta_webhook_envelope(
    *, phone: str, text: str, message_id: str
) -> dict[str, Any]:
    """One Meta WhatsApp Cloud API inbound-text webhook envelope.

    Shaped exactly as the WhatsApp channel adapter
    (`channels/whatsapp.py`: `read_inbound` at the route, `extract_turn` in
    the deferred worker) reads it: a single text message inside one
    `entry`/`changes`/`value.messages` batch.
    """
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "live-harness-waba",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "messages": [
                                {
                                    "id": message_id,
                                    "from": phone,
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        },
                        "field": "messages",
                    }
                ],
            }
        ],
    }


@dataclass
class FakeWhatsAppServer:
    """Records outbound WhatsApp sends instead of hitting Meta (#78 slice 3).

    `WhatsAppClient` owns a real `httpx.AsyncClient` with no injectable
    transport, so intercepting its call needs a real HTTP endpoint, not a
    mocked transport -- this runs a genuine local ASGI server (uvicorn, bound
    to 127.0.0.1) and points `Settings.whatsapp_graph_api_url` at it, the
    exact seam `WhatsAppClient.__init__`/`main.py`'s lifespan already read
    from `Settings`. No `src/` code changes for this fake to work.
    """

    sent: list[dict[str, Any]] = field(default_factory=list)
    _server: uvicorn.Server | None = field(default=None, repr=False)
    _task: asyncio.Task[None] | None = field(default=None, repr=False)
    port: int = 0
    #: Recipient phone -> remaining failed sends owed before it starts
    #: succeeding (#83, Phase 3). Lets a load scenario deterministically
    #: induce a bounded number of outbox retries for one recipient (a small
    #: count) or drive it past `MAX_OUTBOX_ATTEMPTS` into a terminal failure
    #: (a count at or above it), without affecting any other concurrent
    #: conversation's sends. Empty (the default) never fails anything --
    #: #78's original always-succeeds behaviour.
    fail_remaining: dict[str, int] = field(default_factory=dict)

    def _consume_failure(self, to: str) -> bool:
        """True (and decrements) exactly when *to* still owes a failed send.

        A pure, synchronous method -- no server, no I/O -- so it is unit-
        testable offline (`tests/test_live_harness.py`) independently of
        `_build_app`'s Starlette route below, which is the only caller in a
        real run.
        """
        remaining = self.fail_remaining.get(to, 0)
        if remaining <= 0:
            return False
        self.fail_remaining[to] = remaining - 1
        return True

    def _build_app(self) -> Any:
        from starlette.applications import Starlette
        from starlette.requests import Request as StarletteRequest
        from starlette.responses import JSONResponse
        from starlette.routing import Route

        async def send_message(request: StarletteRequest) -> JSONResponse:
            payload = await request.json()
            to = payload.get("to")
            if isinstance(to, str) and self._consume_failure(to):
                # Mirrors a real Meta Graph API error envelope closely enough
                # for `WhatsAppClient.send_text`'s `raise_for_status()` to
                # raise `httpx.HTTPStatusError` -- the same exception path a
                # real outage would take, so `webhook_worker.py`'s
                # `_record_failure` -> `record_outbox_failure` retry/terminal
                # logic is exercised for real, not simulated (#83).
                return JSONResponse(
                    {"error": {"message": "induced failure (#83 load test)"}},
                    status_code=500,
                )
            self.sent.append(payload)
            return JSONResponse(
                {
                    "messaging_product": "whatsapp",
                    "messages": [{"id": f"wamid.fake-{len(self.sent):04d}"}],
                }
            )

        return Starlette(
            routes=[
                Route("/{phone_number_id}/messages", send_message, methods=["POST"])
            ]
        )

    async def start(self) -> None:
        """Bind a free local port and start serving. Idempotent-unsafe by
        design -- call once per instance."""
        self.port = _free_port()
        config = uvicorn.Config(
            self._build_app(),
            host="127.0.0.1",
            port=self.port,
            log_level="warning",
            lifespan="off",
        )
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve())
        for _ in range(200):
            if self._server.started:
                return
            await asyncio.sleep(0.02)
        raise RuntimeError("FakeWhatsAppServer did not start within 4s")

    async def stop(self) -> None:
        """Never raises (#113 review): `boot_live_harness`'s teardown
        `finally` calls this before `_drop_database` -- an exception escaping
        here would abort that `finally` block before the throwaway database
        is dropped, masking whatever error actually failed the boot. Mirrors
        `_drop_database`'s own never-raise teardown contract."""
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.warning("live_harness.fake_whatsapp_server_stop_failed")

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


@dataclass(frozen=True)
class _FakeParticipant:
    """Deterministic, always-active participant (#78 slice 3).

    The webhook smoke turn is not testing client-registry resolution --
    that is Phase 1 (#79)'s job. This directory only needs to resolve ANY
    phone-shaped address to one active participant so
    `DeferredWebhookWorker` proceeds to run the turn at all.
    """

    id: str = "live-harness-participant"
    active: bool = True


class AcceptAllParticipantDirectory:
    """`ParticipantDirectory` that resolves every well-formed address to one
    fixed, always-active `_FakeParticipant` -- see its docstring for why."""

    def normalize_address(self, raw: str) -> str:
        cleaned = (raw or "").strip()
        if not cleaned:
            raise ValueError("address is empty")
        return cleaned if cleaned.startswith("+") else f"+{cleaned}"

    async def resolve(self, session: Any, address: str) -> Any:
        return _FakeParticipant()


def live_registry_factory_adapter(
    engine: AsyncEngine,
    model: Any,
    *,
    terminal_policy: TerminalPolicy | None = None,
    extra_tool_specs: Sequence[ToolSpec] = (),
    tool_wrappers: Mapping[str, Callable[[ToolSpec], ToolSpec]] | None = None,
) -> RegistryFactory:
    """Adapt `evals.live_registry.build_live_registry_factory`'s zero-arg
    closure to the `(settings, embedder, bi_engine)` signature `create_app`'s
    lifespan calls its `registry_factory` with -- the same shape
    `examples/demo/app.py::_demo_registry_factory` already uses.

    *terminal_policy* (#79, Phase 1) is forwarded to `build_live_registry_factory`
    so an operator/developer-shaped scenario can bind `use_term`/`read_file`
    to a real sandbox instead of the always-refusing default -- `None`
    keeps #78's original behaviour (both tools refuse every call).

    *extra_tool_specs* (#79, Phase 1) are registered into the SAME registry
    as the reference backends, REPLACING a base tool of the same name rather
    than raising `ToolRegistry.register`'s own duplicate-name error -- e.g.
    the read-only SQL tool (`sql_query_connector.build_sql_query_tool_spec`),
    a genuinely new name, built and provisioned by the caller BEFORE
    booting -- see `provision_sql_readonly_fixture`'s own docstring for why
    the registry factory runs during `create_app`'s lifespan startup, before
    a harness exists to provision against.

    *tool_wrappers* (#79, Phase 1) map a base tool's name to a function that
    receives that tool's OWN `ToolSpec` -- already bound to this harness's
    throwaway database, which *extra_tool_specs* has no way to pre-build --
    and returns the `ToolSpec` to register in its place: a call-recording
    wrapper around `run_report`, say, for a scenario that needs to inspect
    the tool's actual arguments and result, which the audit trail alone does
    not carry (`ToolCallAttempted.payload` never includes a tool's arguments
    or return value -- only name/timing/outcome)."""
    build_registry = build_live_registry_factory(
        engine, model, terminal_policy=terminal_policy
    )

    def factory(
        settings: Any = None, embedder: Any = None, bi_engine: Any = None
    ) -> ToolRegistry:
        base = build_registry()
        wrappers = tool_wrappers or {}
        # An extra spec REPLACES a base one of the same name (`ToolRegistry.
        # register` itself raises on a duplicate) -- e.g. #80's read-only SQL
        # tool is a genuinely new name, added alongside every reference
        # backend.
        extra_names = {spec.name for spec in extra_tool_specs}
        registry = ToolRegistry()
        for name in base.names():
            if name in extra_names:
                continue
            spec = base.get(name)
            wrap = wrappers.get(name)
            registry.register(wrap(spec) if wrap is not None else spec)
        for spec in extra_tool_specs:
            registry.register(spec)
        return registry

    return factory


def audit_rows_statement(
    *,
    correlation_id: str | None = None,
    role: str | None = None,
    event_type: str | None = None,
    tool_name: str | None = None,
    occurred_after: datetime | None = None,
) -> Select[tuple[AuditEvent]]:
    """Build the `SELECT` `LiveHarness.audit_rows` executes -- a pure
    function so it is unit-testable (by compiling it) without a real
    database; see `tests/test_live_harness.py`."""
    stmt: Select[tuple[AuditEvent]] = select(AuditEvent).order_by(
        AuditEvent.occurred_at, AuditEvent.id
    )
    if correlation_id is not None:
        stmt = stmt.where(AuditEvent.correlation_id == correlation_id)
    if role is not None:
        stmt = stmt.where(AuditEvent.role == role)
    if event_type is not None:
        stmt = stmt.where(AuditEvent.event_type == event_type)
    if tool_name is not None:
        stmt = stmt.where(AuditEvent.tool_name == tool_name)
    if occurred_after is not None:
        stmt = stmt.where(AuditEvent.occurred_at > occurred_after)
    return stmt


def outbox_rows_statement(conversation_key: str) -> Select[tuple[OutboxWork]]:
    """Build the `SELECT` `LiveHarness.outbox_rows` executes -- every
    `outbox_work` row for *conversation_key*, oldest enqueued first (#83,
    Phase 3).

    Lets a load scenario check per-conversation ordering directly against the
    durable rows a real run left behind, rather than against reply content
    (the fake model used by the deterministic load scenarios never varies
    it): the same `(enqueued_at, id)` order `pending_outbox_statement` itself
    enforces at claim time. A pure function so it is unit-testable (by
    compiling it) without a real database, mirroring `audit_rows_statement`
    above; see `tests/test_live_harness.py`.
    """
    return (
        select(OutboxWork)
        .where(OutboxWork.conversation_key == conversation_key)
        .order_by(OutboxWork.enqueued_at, OutboxWork.id)
    )


def metric_value(metrics_text: str, metric_name: str, **labels: str) -> float | None:
    """Read one sample's value out of a `GET /metrics` Prometheus text body.

    Matches on the exact exposed sample name (e.g. `"agent_turns_total"`,
    including a counter's `_total` suffix) and an exact match on every given
    label -- extra labels the sample carries but *labels* does not name are
    ignored, so a caller only names the labels it cares about. Every sample
    that matches is summed: a label the caller does not name can split one
    count into several series (`agent_turns_total` gained `actor_kind` in
    #252, so an API turn and a channel turn of one runtime are two series),
    and the caller asked for the total over them. `None` when no sample
    matches, which a caller should treat as honestly unknown rather than `0`
    (the harness's own honesty-rule convention, per issue #78 Slice 1's
    `TurnUsage`).
    """
    matched = [
        sample.value
        for family in text_string_to_metric_families(metrics_text)
        for sample in family.samples
        if sample.name == metric_name
        and all(sample.labels.get(key) == value for key, value in labels.items())
    ]
    return sum(matched) if matched else None


def _with_database(url: str, database: str) -> str:
    """Return *url* with its path replaced by *database* -- used to derive
    the throwaway database's own connection URL from the admin URL."""
    return make_url(url).set(database=database).render_as_string(hide_password=False)


async def _create_database(admin_url: str, name: str) -> None:
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        await engine.dispose()


async def _drop_database(admin_url: str, name: str) -> None:
    """Best-effort drop: terminates other backends on *name* first (a stray
    pooled connection from this same process can otherwise block `DROP
    DATABASE`), then drops it. Never raises -- a teardown that fails must not
    hide the test's own result."""
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :name AND pid <> pg_backend_pid()"
                ),
                {"name": name},
            )
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
    except Exception:
        logger.warning("live_harness.drop_database_failed", database=name)
    finally:
        await engine.dispose()


def _run_migrations(database_url: str, *, repo_root: Path) -> None:
    """`alembic upgrade head` against *database_url*, mirroring the CI
    `audit-migration`/`outbox-migration` jobs' own throwaway-database
    pattern. `migrations/env.py` reads `DATABASE_URL` from the environment in
    preference to `Settings` (see its own module docstring), which is
    exactly why only the env var needs to change here.

    The subprocess `env` is built from scratch (`PATH` + `DATABASE_URL`),
    never `{**os.environ, ...}` (#113 review follow-up): a full copy of the
    process environment also copies any real model-provider secret a
    developer exported to run the live harness against a real model into
    this function's own locals, where a migration failure combined with any
    verbose-traceback tool (`pytest -l`/`--showlocals`, rich tracebacks, a
    notebook/IDE debugger) could print it verbatim. `alembic upgrade head`
    genuinely only needs `DATABASE_URL`; `PATH` is kept so the interpreter
    and any tool it shells out to can still be resolved."""
    import subprocess
    import sys

    env = {"PATH": os.environ.get("PATH", ""), "DATABASE_URL": database_url}
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=repo_root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


#: Bound on `_seed_demo_company`'s own subprocess (#119 review, medium
#: finding): without an explicit `timeout=`, an unreachable or misconfigured
#: demo database hangs `subprocess.run` indefinitely -- and that call runs
#: inside `boot_live_harness`'s process-global `_boot_lock` (held boot-through
#: -teardown, #113 review), so a hang here also starves every other
#: `boot_live_harness()` caller in the same process. 60s comfortably covers
#: `demo/load_demo_company.py`'s own normal runtime against a reachable
#: throwaway database.
_SEED_DEMO_COMPANY_TIMEOUT_S = 60.0


def _seed_demo_company(
    database_url: str,
    *,
    repo_root: Path,
    timeout: float = _SEED_DEMO_COMPANY_TIMEOUT_S,
) -> None:
    """Load `demo/load_demo_company.py`'s fixture data (catalog, clients,
    sales) into *database_url* (#79, Phase 1) -- the SAME script an operator
    runs by hand, reused here rather than reimplemented so the harness's
    data matches exactly what the script documents and
    `demo/verify_contract.py`'s own contract check already covers. Safe
    against a fresh throwaway database: the script only refuses to run
    against one that already holds unrecognized objects (`classify_target`),
    which an empty, just-migrated database never does.

    *timeout* (#119 review, medium finding) bounds the subprocess so a
    broken/unreachable demo database fails fast with a clear `RuntimeError`
    instead of hanging this call -- and, transitively, every other
    `boot_live_harness()` caller blocked on the same `_boot_lock` -- forever.
    """
    import subprocess
    import sys

    env = {**os.environ, "DEMO_DATABASE_URL": database_url}
    try:
        subprocess.run(
            [sys.executable, str(repo_root / "demo" / "load_demo_company.py")],
            cwd=repo_root,
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            "_seed_demo_company: demo/load_demo_company.py did not finish "
            f"within {timeout}s -- the demo database is likely unreachable "
            "or misconfigured (LIVE_HARNESS_ADMIN_DATABASE_URL)"
        ) from exc


async def _execute_sql_script(database_url: str, sql_text: str) -> None:
    """Run *sql_text* -- which may hold several statements, including `DO
    $$ ... $$` blocks -- against *database_url* over ONE raw asyncpg
    connection (#79, Phase 1).

    Mirrors `demo/load_demo_company.py::_run_sql_files`'s own technique, and
    for the same reason: every SQLAlchemy `execute()` path sends a PREPARED
    statement, and PostgreSQL rejects more than one command in one of those
    ("cannot insert multiple commands into a prepared statement"). asyncpg's
    own `execute()` with no bind parameters uses the simple query protocol,
    which accepts them.
    """
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as conn:
            raw = await conn.get_raw_connection()
            driver_conn = raw.driver_connection
            if driver_conn is None:
                raise RuntimeError(
                    "no raw asyncpg connection available; sql_text was NOT run"
                )
            await driver_conn.execute(sql_text)
    finally:
        await engine.dispose()


def _quote_sql_literal(value: str) -> str:
    """A single-quoted SQL string literal, with every embedded `'` doubled
    -- the same escaping rule PostgreSQL's own lexer applies."""
    return "'" + value.replace("'", "''") + "'"


def _quote_sql_identifier(value: str) -> str:
    """A double-quoted SQL identifier, with every embedded `"` doubled."""
    return '"' + value.replace('"', '""') + '"'


#: The psql-only meta-command prefixes `scripts/provision_sql_readonly.sql`
#: uses purely to supply a DEFAULT for `sql_temp_file_limit` and
#: `sql_allow_tables` when the caller's `-v` omits them.
#: `render_provision_sql_readonly` always supplies a value itself (see its own
#: *temp_file_limit* and *allow_base_tables* parameters), so the whole `\if
#: ... \endif` block -- along with the unrelated `\set ON_ERROR_STOP on`
#: line -- is dropped rather than interpreted; asyncpg's simple query
#: protocol has no psql meta-command support at all, so leaving one in would
#: fail as invalid SQL.
_PSQL_META_COMMAND_PREFIX = "\\"


def render_provision_sql_readonly(
    *,
    database: str,
    password: str,
    views: Sequence[str],
    temp_file_limit: str = "256MB",
    allow_base_tables: bool = False,
) -> str:
    """Render the checked-in `scripts/provision_sql_readonly.sql` (#80,
    ADR-007's read-only SQL tool role) into plain SQL runnable over one
    asyncpg connection (#79, Phase 1's own SQL-tool scenario provisions this
    role against the harness's throwaway database with this function).

    Renders the CHECKED-IN script -- rather than a parallel
    reimplementation -- by substituting its four psql variables
    (`sql_password`, `sql_views`, `sql_temp_file_limit`, `sql_allow_tables`,
    the last one off unless *allow_base_tables* is set) and its one
    automatic psql variable (`DBNAME`) and dropping its psql-only
    meta-commands, so a live Phase 1 run provisions the tool's role through
    the EXACT same runbook an operator follows by hand -- see
    `tests/test_live_harness.py` for unit tests of the substitution itself.
    """
    raw = (_REPO_ROOT / "scripts" / "provision_sql_readonly.sql").read_text()
    rendered = "\n".join(
        line
        for line in raw.splitlines()
        if not line.lstrip().startswith(_PSQL_META_COMMAND_PREFIX)
    )
    rendered = rendered.replace(":'sql_password'", _quote_sql_literal(password))
    rendered = rendered.replace(":'sql_views'", _quote_sql_literal(",".join(views)))
    rendered = rendered.replace(
        ":'sql_temp_file_limit'", _quote_sql_literal(temp_file_limit)
    )
    rendered = rendered.replace(
        ":'sql_allow_tables'", _quote_sql_literal("on" if allow_base_tables else "off")
    )
    rendered = rendered.replace(':"DBNAME"', _quote_sql_identifier(database))
    return rendered


async def provision_sql_readonly_fixture(database_url: str, *, password: str) -> str:
    """Load `tests/fixtures/sql_query_fixture.sql` and provision the
    `sql_readonly` role (`scripts/provision_sql_readonly.sql`) against
    *database_url* (#79, Phase 1's SQL-tool scenario), granting it `SELECT`
    on exactly `sql_tool_fixture.sales_v` -- the same fixture
    `tests/test_sql_query_integration.py` already uses.

    *database_url* must be a SUPERUSER-credentialed connection string (the
    same shape `DEFAULT_ADMIN_DATABASE_URL`/`_with_database` produce): a
    dedicated throwaway database the caller creates and drops itself,
    independent of any `boot_live_harness` database -- this must run and
    complete BEFORE the `ToolSpec` it provisions for can be handed to
    `boot_live_harness(extra_tool_specs=...)`, which is itself built and
    bound while `create_app`'s lifespan is starting, before a harness exists
    to provision against.

    Returns the `sql_readonly` role's own connection URL: same host and
    database as *database_url*, different user/password -- a live scenario
    passes this to
    `agents_system.connectors.sql_query_connector.build_sql_query_tool_spec`
    exactly like a deployment's own `SQL_DATABASE_URL` would.
    """
    fixture_path = _REPO_ROOT / "tests" / "fixtures" / "sql_query_fixture.sql"
    fixture_sql = "\n".join(
        line
        for line in fixture_path.read_text().splitlines()
        if not line.lstrip().startswith(_PSQL_META_COMMAND_PREFIX)
    )
    await _execute_sql_script(database_url, fixture_sql)

    database = make_url(database_url).database
    assert database is not None, "database_url carries no database name"
    provision_sql = render_provision_sql_readonly(
        database=database,
        password=password,
        views=("sql_tool_fixture.sales_v",),
    )
    await _execute_sql_script(database_url, provision_sql)

    return (
        make_url(database_url)
        .set(username="sql_readonly", password=password)
        .render_as_string(hide_password=False)
    )


@dataclass
class LiveHarness:
    """Handle to a booted real application (#78 Phase 0 Slice 3), exposing
    the helpers Phase 1 (#79) and Phase 2 (extends #76) build their own live
    scenarios on top of."""

    app: FastAPI
    client: httpx.AsyncClient
    engine: AsyncEngine
    redis_url: str
    outbound: FakeWhatsAppServer
    runtime_id: str
    model_name: str
    meta_webhook_secret: str
    metrics_api_key: str
    #: The exact superuser-credentialed connection string the app itself
    #: connects with, pointed at this run's throwaway database (#79, Phase
    #: 1) -- lets a scenario provision extra database-level fixtures (e.g.
    #: `provision_sql_readonly_fixture`) against the SAME database, without
    #: re-deriving it from `LIVE_HARNESS_ADMIN_DATABASE_URL` itself.
    database_url: str
    #: Every `thread_id` (normalized phone number) a `deliver_webhook` call
    #: has driven the checkpointer with (#113 review) -- never mutated by
    #: `send_chat_completion`, which never engages the checkpointer. They all
    #: live in this run's throwaway database (see this module's docstring).
    used_thread_ids: set[str] = field(default_factory=set)

    async def send_chat_completion(self, text: str) -> httpx.Response:
        """POST one turn to `/v1/chat/completions` against this harness's
        registered runtime id."""
        return await self.client.post(
            "/v1/chat/completions",
            json={
                "model": self.runtime_id,
                "messages": [{"role": "user", "content": text}],
                "stream": False,
            },
        )

    async def deliver_webhook(
        self, *, phone: str, text: str, message_id: str | None = None
    ) -> httpx.Response:
        """POST one signed inbound WhatsApp text message to `/webhook`, using
        the real HMAC-SHA256 signature scheme (`sign_payload`, the
        counterpart to the route's own `verify_signature`)."""
        message_id = message_id or f"wamid.smoke-{uuid.uuid4().hex[:12]}"
        envelope = build_meta_webhook_envelope(
            phone=phone, text=text, message_id=message_id
        )
        body = json.dumps(envelope).encode()
        signature = sign_payload(body, self.meta_webhook_secret)
        # Recorded BEFORE the request completes (not after), and normalized
        # the same way `DeferredWebhookWorker` will: it checkpoints under
        # `AcceptAllParticipantDirectory.normalize_address(turn.phone_number)`
        # (#113 review), which adds a leading `+` when *phone* is missing
        # one. Tracking the raw, unnormalized `phone` here would record the
        # wrong key whenever a caller passes a number without a leading
        # `+`, so the recorded key would not be the thread the worker wrote
        # (#113 review follow-up) -- regardless of whether the worker has
        # run yet by the time this call returns.
        normalized_phone = AcceptAllParticipantDirectory().normalize_address(phone)
        self.used_thread_ids.add(normalized_phone)
        return await self.client.post(
            "/webhook",
            content=body,
            headers={
                "content-type": "application/json",
                "x-hub-signature-256": signature,
            },
        )

    async def wait_until(
        self,
        predicate: Callable[[], Awaitable[bool] | bool],
        *,
        timeout: float = 20.0,
        interval: float = 0.25,
    ) -> None:
        """Poll *predicate* until it is truthy or *timeout* elapses (used to
        wait for `DeferredWebhookWorker`'s poll loop, which runs on its own
        `webhook_worker_poll_interval_s` cadence). Raises `TimeoutError`."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while True:
            outcome = predicate()
            if asyncio.iscoroutine(outcome):
                outcome = await outcome
            if outcome:
                return
            if loop.time() >= deadline:
                raise TimeoutError(
                    f"condition not met within {timeout}s (live harness wait_until)"
                )
            await asyncio.sleep(interval)

    async def audit_rows(self, **filters: Any) -> list[AuditEvent]:
        """Read matching rows from the real `audit_event` table. See
        `audit_rows_statement` for the accepted filters, and this module's
        docstring for the correlation-id caveat on webhook-originated turns.
        """
        session_factory = get_session_factory(self.engine)
        async with session_factory() as session:
            result = await session.scalars(audit_rows_statement(**filters))
            return list(result)

    async def outbox_rows(self, conversation_key: str) -> list[OutboxWork]:
        """Read every `outbox_work` row for *conversation_key*, oldest
        enqueued first. See `outbox_rows_statement`'s docstring for why (#83,
        Phase 3's per-conversation ordering check)."""
        session_factory = get_session_factory(self.engine)
        async with session_factory() as session:
            result = await session.scalars(outbox_rows_statement(conversation_key))
            return list(result)

    async def scrape_metrics(self) -> str:
        """`GET /metrics`, Bearer-authenticated with this harness's own
        `metrics_api_key`. Returns the raw Prometheus text body; see
        `metric_value` to read one sample out of it."""
        response = await self.client.get(
            "/metrics",
            headers={"Authorization": f"Bearer {self.metrics_api_key}"},
        )
        response.raise_for_status()
        return response.text

    async def read_thread(self, thread_id: str) -> Any:
        """Read the checkpointed LangGraph thread for *thread_id* (the
        normalized address `DeferredWebhookWorker` used) straight out of the
        real Postgres checkpointer's tables in this run's database -- `None`
        if nothing was ever checkpointed under it."""
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        from agents_system.services.checkpointer import psycopg_conninfo

        async with AsyncPostgresSaver.from_conn_string(
            psycopg_conninfo(self.database_url)
        ) as saver:
            return await saver.aget_tuple({"configurable": {"thread_id": thread_id}})


def _resolve_eval_model(
    model: BaseChatModel | None, model_name: str | None
) -> tuple[BaseChatModel, str]:
    """Return the `(model, model_name)` `boot_live_harness` should boot with.

    *model* overrides #78's original `build_eval_model()` selection (#83,
    Phase 3): a load scenario injects a fast, deterministic, zero-network
    fake model instead of depending on `EVAL_PROVIDER`/a real provider
    staying reachable under concurrent load. *model_name* is required
    whenever *model* is given -- falling back to e.g. `type(model).__name__`
    would silently mislabel `/metrics`' `runtime_id`-keyed series and audit
    rows with a name nobody chose. Leaving both unset keeps #78's original
    behaviour byte-for-byte: `build_eval_model()` is called exactly as
    before.

    A pure function so it is unit-testable (`tests/test_live_harness.py`)
    without booting anything, mirroring `audit_rows_statement`'s own
    pure-function-for-testability shape above.
    """
    if model is not None:
        if not model_name:
            raise ValueError(
                "boot_live_harness(): model_name must be given (non-empty) "
                "when model is -- otherwise metrics and audit rows would be "
                "labelled with a name nobody chose"
            )
        return model, model_name
    return build_eval_model()


@contextlib.asynccontextmanager
async def boot_live_harness(
    *,
    role: Agent | str | None = None,
    runtime_id: str = "live-harness",
    permissions: Sequence[str] | None = None,
    terminal_policy: TerminalPolicy | None = None,
    seed_demo: bool = False,
    extra_tool_specs: Sequence[ToolSpec] = (),
    tool_wrappers: Mapping[str, Callable[[ToolSpec], ToolSpec]] | None = None,
    bind_webhook_to_role: bool = True,
    model: BaseChatModel | None = None,
    model_name: str | None = None,
) -> AsyncIterator[LiveHarness]:
    """Boot the real application against a fresh throwaway database and
    yield a `LiveHarness`. See this module's docstring for the full design.

    Requires a reachable Postgres (`LIVE_HARNESS_ADMIN_DATABASE_URL`,
    default: `docker-compose.yml`'s own `postgres` service) able to
    `CREATE DATABASE`, a reachable Redis (`LIVE_HARNESS_REDIS_URL`, default:
    the `redis` service, probed by `GET /health`), and a real model reachable
    through whatever `EVAL_PROVIDER` is configured (see
    `docs/platform/live-eval.md`).

    *role* (#79, Phase 1) accepts a Python-built `Agent` (`agent.spec.Agent`,
    e.g. `reference_agent(...)`, `Agent.from_folder(...)` or a bare
    `Agent(...)`) or a role name, registered exactly as
    `create_app(agents=...)` already accepts both. Left unset, it serves the
    reference `sales-agent` (`reference_agent("sales-agent")`, #225: an agent
    folder, not the deprecated predefined role). *permissions* is the
    matching deploy-time grant; omitted, it is `SALES_AGENT_PERMISSIONS`,
    but ONLY when *role* is left unset -- for any *role* given, a
    `"sales-agent"` name included, omitting *permissions* raises
    `ValueError` instead of silently granting `SALES_AGENT_PERMISSIONS` to
    an agent it was never meant for (#119 review: a fail-open default on
    this exact deploy-grant trust boundary). Every caller that names its
    agent states its own grant explicitly. *terminal_policy*
    is forwarded to the
    live registry so `use_term`/`read_file` can be genuinely exercised
    instead of always refusing. *seed_demo* additionally creates a SECOND
    throwaway database and loads `demo/load_demo_company.py`'s fixture data
    (catalog, clients, sales) into it -- separate from *database_url* itself
    (the app's own alembic-managed operational schema, which the demo
    loader's own safety guard refuses to load into) -- and points the
    registry's reference-backend engine at it, needed by any scenario
    touching `catalog_search`, `client_lookup`, or `run_report`.
    *extra_tool_specs* are registered on
    top of the reference backends -- see `live_registry_factory_adapter`'s
    own docstring for why they must already be fully built (and, for a
    database-backed one, already provisioned) before this call.
    *tool_wrappers* replaces a base tool with the result of calling a
    function on it -- see `live_registry_factory_adapter`'s own docstring
    for when this is the right choice instead of *extra_tool_specs*.

    *bind_webhook_to_role* (#79, Phase 1): `True` (the default, #78's
    original behaviour) binds `WHATSAPP_RUNTIME_ID` to the SAME `runtime_id`
    as *role* itself, so `deliver_webhook` exercises *role* directly -- only
    correct when *role* resolves `untrusted_input: true` (ADR-002 C.11/C.13:
    `create_app` refuses to boot a WhatsApp-bound role that does not, since
    the channel delivers untrusted external input). Most reference agents do
    NOT (`developer-agent`, `data-agent`, ...), and neither does a bare
    `Agent(...)`'s inherited default -- pass `False` for any scenario that
    only drives *role* through `send_chat_completion`/the HTTP API and never
    calls `deliver_webhook`, which instead binds WHATSAPP_RUNTIME_ID to a
    separate, minimal, always-`untrusted_input: true` placeholder agent that
    nothing in the scenario ever talks to.

    *model*/*model_name* (#83, Phase 3): override `build_eval_model()`'s own
    `EVAL_PROVIDER`-driven selection -- see `_resolve_eval_model`'s docstring
    for why *model_name* is required whenever *model* is. Both default to
    unset, keeping #78's original behaviour unchanged for every existing
    caller.
    """
    admin_url = os.environ.get(_ADMIN_DATABASE_URL_ENV, DEFAULT_ADMIN_DATABASE_URL)
    redis_url = os.environ.get(_REDIS_URL_ENV, DEFAULT_REDIS_URL)
    db_name = f"live_harness_{uuid.uuid4().hex[:12]}"
    database_url = _with_database(admin_url, db_name)
    if role is not None and permissions is None:
        raise ValueError(
            "boot_live_harness(): permissions must be given explicitly when "
            f"role is (got role={role!r}) -- only the default reference "
            "sales-agent is granted SALES_AGENT_PERMISSIONS by default"
        )
    if role is None:
        role = reference_agent("sales-agent")
    grant_list = list(
        permissions if permissions is not None else SALES_AGENT_PERMISSIONS
    )
    agents: dict[str, Agent | str] = {runtime_id: role}
    grants: dict[str, list[str]] = {runtime_id: grant_list}
    whatsapp_runtime_id = runtime_id
    if not bind_webhook_to_role:
        whatsapp_runtime_id = _WEBHOOK_PLACEHOLDER_RUNTIME_ID
        agents[whatsapp_runtime_id] = _webhook_placeholder_agent()
        grants[whatsapp_runtime_id] = []

    # #113 review (medium finding) -- held for this call's ENTIRE lifetime
    # (boot through teardown), not just around the env mutation below: see
    # the module docstring's "One `boot_live_harness` at a time per process"
    # note. A second concurrent call blocks here until this one's context
    # manager fully exits, so two booted harnesses can never be up at once
    # invalidating each other's process-global env/`get_settings()` cache.
    # A SEPARATE throwaway database from *database_url* (#79, Phase 1
    # review): `_run_migrations` makes *database_url* the app's own
    # alembic-managed operational schema (`audit_event`, `outbox`, ...), and
    # `demo/load_demo_company.py`'s own safety guard (`classify_target`)
    # refuses to load demo data into a database that already holds objects
    # it does not recognise -- which those alembic tables always are. This
    # mirrors production's own separation between `DATABASE_URL` (the app)
    # and `BI_DATABASE_URL` (a dedicated reporting connection, `services/
    # db_role.py`'s "AD-3"): `registry_engine` below is pointed at whichever
    # one `catalog_search`/`client_lookup`/`run_report` actually need.
    demo_db_name = f"{db_name}_demo" if seed_demo else None
    demo_database_url = (
        _with_database(admin_url, demo_db_name) if demo_db_name is not None else None
    )

    async with _boot_lock:
        await _create_database(admin_url, db_name)
        outbound = FakeWhatsAppServer()
        registry_engine: AsyncEngine | None = None
        harness: LiveHarness | None = None
        try:
            # #119 review (medium finding): this second `_create_database`
            # call must stay INSIDE this `try` -- the `finally` below is what
            # drops *db_name* (already created above), so if this demo
            # create raises outside the try, *db_name* is never dropped and
            # leaks. `_drop_database` already tolerates a database that was
            # never created (`DROP DATABASE IF EXISTS`), so entering the
            # `finally` before this call succeeds is always safe.
            if demo_db_name is not None:
                await _create_database(admin_url, demo_db_name)
            await asyncio.to_thread(_run_migrations, database_url, repo_root=_REPO_ROOT)
            if demo_database_url is not None:
                await asyncio.to_thread(
                    _seed_demo_company, demo_database_url, repo_root=_REPO_ROOT
                )
            await outbound.start()

            metrics_api_key = secrets.token_hex(16)
            env_overrides = {
                "DATABASE_URL": database_url,
                "REDIS_URL": redis_url,
                "META_WEBHOOK_SECRET": secrets.token_hex(16),
                # #182 -- a served WhatsApp channel needs a non-empty verify
                # token, or Settings refuses to boot.
                "WHATSAPP_VERIFY_TOKEN": secrets.token_hex(16),
                "WHATSAPP_TOKEN": "live-harness-test-token",
                "WHATSAPP_PHONE_NUMBER_ID": "live-harness-test-phone-id",
                "WHATSAPP_GRAPH_API_URL": outbound.base_url,
                "WHATSAPP_RUNTIME_ID": whatsapp_runtime_id,
                "WHATSAPP_CHECKPOINTER_ENABLED": "true",
                "ADAPTER_RUNTIMES": json.dumps([runtime_id]),
                "ADAPTER_API_KEY": secrets.token_hex(16),
                "ADAPTER_PROVIDER": os.environ.get("EVAL_PROVIDER", "ollama"),
                "METRICS_ENABLED": "true",
                "METRICS_API_KEY": metrics_api_key,
            }
            previous_env = {key: os.environ.get(key) for key in env_overrides}
            os.environ.update(env_overrides)
            get_settings.cache_clear()
            try:
                settings = get_settings()
                resolved_model, resolved_model_name = _resolve_eval_model(
                    model, model_name
                )
                registry_engine = create_async_engine(demo_database_url or database_url)

                app = create_app(
                    agents=agents,
                    grants=grants,
                    registry_factory=live_registry_factory_adapter(
                        registry_engine,
                        resolved_model,
                        terminal_policy=terminal_policy,
                        extra_tool_specs=extra_tool_specs,
                        tool_wrappers=tool_wrappers,
                    ),
                    participant_directory=AcceptAllParticipantDirectory(),
                    title="agents_system live harness",
                    # #83, Phase 3: only when *model* was explicitly injected
                    # does the SAME instance also drive every registered
                    # AgentRuntime's actual turns (`create_app`'s own
                    # `chat_model` override) -- not just the registry
                    # factory's reference-backend connectors above. `None`
                    # (the overwhelming majority of existing callers) keeps
                    # #78's original behaviour byte-for-byte: the lifespan's
                    # own `_build_chat_model(settings.adapter_provider)`.
                    chat_model=resolved_model if model is not None else None,
                )

                async with app.router.lifespan_context(app):
                    transport = httpx.ASGITransport(app=app)
                    async with httpx.AsyncClient(
                        transport=transport,
                        base_url="http://live-harness.test",
                        headers={"Authorization": f"Bearer {settings.adapter_api_key}"},
                        timeout=httpx.Timeout(120.0),
                    ) as client:
                        harness = LiveHarness(
                            app=app,
                            client=client,
                            engine=app.state.engine,
                            redis_url=redis_url,
                            outbound=outbound,
                            runtime_id=runtime_id,
                            model_name=resolved_model_name,
                            meta_webhook_secret=settings.meta_webhook_secret,
                            metrics_api_key=metrics_api_key,
                            database_url=database_url,
                        )
                        yield harness
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                get_settings.cache_clear()
        finally:
            if registry_engine is not None:
                await registry_engine.dispose()
            await outbound.stop()
            await _drop_database(admin_url, db_name)
            if demo_db_name is not None:
                await _drop_database(admin_url, demo_db_name)
