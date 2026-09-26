"""Live harness for issue #78 Phase 0 Slice 3.

Boots the REAL application -- the HTTP API, the WhatsApp webhook route and
its deferred worker, a real (throwaway) Postgres database, a real Redis
checkpointer, and audit persisted to the real `audit_event` table -- instead
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
- **Real Redis** at the harness's configured `REDIS_URL` (docker-compose's
  `redis` service) -- the checkpointer runs for real
  (`WHATSAPP_CHECKPOINTER_ENABLED` defaults `True`; nothing here disables
  it), so `read_thread` reads an actual persisted LangGraph checkpoint.
  `boot_live_harness` tracks every `thread_id` its own `deliver_webhook`
  calls write (the normalized phone number `DeferredWebhookWorker` engages
  the checkpointer with) and deletes those threads at teardown, in the same
  `finally` that drops the throwaway database -- a checkpoint left behind by
  one run must never be mistaken for proof that a LATER run's checkpointing
  actually worked (#113 review).
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
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import structlog
import uvicorn
from fastapi import FastAPI
from prometheus_client.parser import text_string_to_metric_families
from sqlalchemy import Select, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

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

#: `platform/roles/sales-agent/manifest.md`'s own `permissions:` list,
#: repeated here (not imported) because the manifest is data, not code --
#: this is the deploy-time GRANT `create_app(grants=...)` requires per
#: registered runtime id, independent of what the role happens to declare.
SALES_AGENT_PERMISSIONS: tuple[str, ...] = (
    "read:catalog",
    "read:client_registry",
    "write:orders",
    "write:order_items",
    "read:price_lists",
    "send:message",
)


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

    Shaped exactly as `integration/webhook.py::_extract_meta_messages` and
    `services/webhook_worker.py::_extract_inbound_turn` read it: a single
    text message inside one `entry`/`changes`/`value.messages` batch.
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

    def _build_app(self) -> Any:
        from starlette.applications import Starlette
        from starlette.requests import Request as StarletteRequest
        from starlette.responses import JSONResponse
        from starlette.routing import Route

        async def send_message(request: StarletteRequest) -> JSONResponse:
            payload = await request.json()
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
        `finally` calls this before `_delete_redis_threads`/`_drop_database`
        -- an exception escaping here would abort that `finally` block
        before the throwaway database is dropped, masking whatever error
        actually failed the boot. Mirrors `_drop_database`'s/
        `_delete_redis_threads`'s own never-raise teardown contract."""
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
    the read-only SQL tool (`sql_query_connector.build_sql_query_tool_spec`,
    a genuinely new name, built and provisioned by the caller BEFORE
    booting -- see `provision_sql_readonly_fixture`'s own docstring for why
    the registry factory runs during `create_app`'s lifespan startup, before
    a harness exists to provision against), or a call-recording wrapper
    around an EXISTING connector (e.g. `run_report`) that a scenario needs
    to inspect the actual result of, which the audit trail alone does not
    carry (`ToolCallAttempted.payload` never includes a tool's arguments or
    return value -- only name/timing/outcome)."""
    build_registry = build_live_registry_factory(
        engine, model, terminal_policy=terminal_policy
    )

    def factory(
        settings: Any = None, embedder: Any = None, bi_engine: Any = None
    ) -> ToolRegistry:
        base = build_registry()
        # An extra spec REPLACES a base one of the same name (`ToolRegistry.
        # register` itself raises on a duplicate) -- e.g. #79 Phase 1's
        # `run_report`-call-recording wrapper around the same connector, kept
        # local to a test rather than a change to `build_live_registry` --
        # not just adds a genuinely new tool (e.g. `sql_query`).
        extra_names = {spec.name for spec in extra_tool_specs}
        registry = ToolRegistry()
        for name in base.names():
            if name not in extra_names:
                registry.register(base.get(name))
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


def metric_value(metrics_text: str, metric_name: str, **labels: str) -> float | None:
    """Read one sample's value out of a `GET /metrics` Prometheus text body.

    Matches on the exact exposed sample name (e.g. `"agent_turns_total"`,
    including a counter's `_total` suffix) and an exact match on every given
    label -- extra labels the sample carries but *labels* does not name are
    ignored, so a caller only names the labels it cares about. `None` when no
    sample matches, which a caller should treat as honestly unknown rather
    than `0` (the harness's own honesty-rule convention, per issue #78
    Slice 1's `TurnUsage`).
    """
    for family in text_string_to_metric_families(metrics_text):
        for sample in family.samples:
            if sample.name != metric_name:
                continue
            if all(sample.labels.get(key) == value for key, value in labels.items()):
                return sample.value
    return None


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


async def _delete_redis_threads(redis_url: str, thread_ids: set[str]) -> None:
    """Best-effort delete of every LangGraph checkpoint this harness run
    wrote (#113 review, high-severity finding): a stale checkpoint left by
    an earlier run under the same `thread_id` must never let a later run's
    `read_thread` assertion pass without the current run's checkpointer
    genuinely having written anything. Mirrors `_drop_database`'s own
    never-raise contract -- a teardown that fails must not hide the test's
    own result.
    """
    if not thread_ids:
        return
    from langgraph.checkpoint.redis import AsyncRedisSaver

    try:
        async with AsyncRedisSaver.from_conn_string(redis_url) as saver:
            for thread_id in thread_ids:
                await saver.adelete_thread(thread_id)
    except Exception:
        logger.warning(
            "live_harness.delete_redis_threads_failed",
            thread_ids=sorted(thread_ids),
        )


def _run_migrations(database_url: str, *, repo_root: Path) -> None:
    """`alembic upgrade head` against *database_url*, mirroring the CI
    `audit-migration`/`outbox-migration` jobs' own throwaway-database
    pattern. `alembic/env.py` reads `DATABASE_URL` from the environment in
    preference to `Settings` (see its own module docstring), which is
    exactly why only the env var needs to change here."""
    import subprocess
    import sys

    env = {**os.environ, "DATABASE_URL": database_url}
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=repo_root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


def _seed_demo_company(database_url: str, *, repo_root: Path) -> None:
    """Load `demo/load_demo_company.py`'s fixture data (catalog, clients,
    sales) into *database_url* (#79, Phase 1) -- the SAME script an operator
    runs by hand, reused here rather than reimplemented so the harness's
    data matches exactly what the script documents and
    `demo/verify_contract.py`'s own contract check already covers. Safe
    against a fresh throwaway database: the script only refuses to run
    against one that already holds unrecognized objects (`classify_target`),
    which an empty, just-migrated database never does.
    """
    import subprocess
    import sys

    env = {**os.environ, "DEMO_DATABASE_URL": database_url}
    subprocess.run(
        [sys.executable, str(repo_root / "demo" / "load_demo_company.py")],
        cwd=repo_root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


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


#: The four psql-only meta-command prefixes `scripts/provision_sql_readonly.sql`
#: uses purely to supply a DEFAULT for `sql_temp_file_limit` when the caller's
#: `-v` omits it. `render_provision_sql_readonly` always supplies a value
#: itself (see its own *temp_file_limit* parameter), so the whole `\if
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
) -> str:
    """Render the checked-in `scripts/provision_sql_readonly.sql` (#80,
    ADR-007's read-only SQL tool role) into plain SQL runnable over one
    asyncpg connection (#79, Phase 1's own SQL-tool scenario provisions this
    role against the harness's throwaway database with this function).

    Renders the CHECKED-IN script -- rather than a parallel
    reimplementation -- by substituting its three psql variables
    (`sql_password`, `sql_views`, `sql_temp_file_limit`) and its one
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
    #: has driven the checkpointer with, so `boot_live_harness`'s teardown
    #: knows exactly which Redis checkpoints are this run's own to delete
    #: (#113 review) -- never mutated by `send_chat_completion`, which never
    #: engages the checkpointer (see this module's docstring).
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
        # Recorded BEFORE the request completes (not after): `phone` is the
        # exact thread_id `DeferredWebhookWorker` engages the checkpointer
        # with (#113 review) once it processes this message, regardless of
        # whether the worker has run yet by the time this call returns --
        # teardown must still know to clean it up.
        self.used_thread_ids.add(phone)
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
        real Redis checkpointer -- `None` if nothing was ever checkpointed
        under it."""
        from langgraph.checkpoint.redis import AsyncRedisSaver

        async with AsyncRedisSaver.from_conn_string(self.redis_url) as saver:
            return await saver.aget_tuple({"configurable": {"thread_id": thread_id}})


@contextlib.asynccontextmanager
async def boot_live_harness(
    *,
    role: Agent | str = "sales-agent",
    runtime_id: str = "live-harness",
    permissions: Sequence[str] | None = None,
    terminal_policy: TerminalPolicy | None = None,
    seed_demo: bool = False,
    extra_tool_specs: Sequence[ToolSpec] = (),
) -> AsyncIterator[LiveHarness]:
    """Boot the real application against a fresh throwaway database and
    yield a `LiveHarness`. See this module's docstring for the full design.

    Requires a reachable Postgres (`LIVE_HARNESS_ADMIN_DATABASE_URL`,
    default: `docker-compose.yml`'s own `postgres` service) able to
    `CREATE DATABASE`, a reachable Redis (`LIVE_HARNESS_REDIS_URL`, default:
    the `redis` service), and a real model reachable through whatever
    `EVAL_PROVIDER` is configured (see `docs/platform/live-eval.md`).

    *role* (#79, Phase 1) accepts either a predefined platform-role name --
    #78's original `str` -- or a Python-built `Agent` (`agent.spec.Agent`,
    e.g. `Agent.from_folder(...)` or a bare `Agent(...)`), registered exactly
    as `create_app(agents=...)` already accepts both. *permissions* is the
    matching deploy-time grant; omitted, it keeps #78's original
    `SALES_AGENT_PERMISSIONS` default (correct only when *role* is left at
    its own `"sales-agent"` default). *terminal_policy* is forwarded to the
    live registry so `use_term`/`read_file` can be genuinely exercised
    instead of always refusing. *seed_demo* additionally loads
    `demo/load_demo_company.py`'s fixture data (catalog, clients, sales) into
    the throwaway database, needed by any scenario touching `catalog_search`,
    `client_lookup`, or `run_report`. *extra_tool_specs* are registered on
    top of the reference backends -- see `live_registry_factory_adapter`'s
    own docstring for why they must already be fully built (and, for a
    database-backed one, already provisioned) before this call.
    """
    admin_url = os.environ.get(_ADMIN_DATABASE_URL_ENV, DEFAULT_ADMIN_DATABASE_URL)
    redis_url = os.environ.get(_REDIS_URL_ENV, DEFAULT_REDIS_URL)
    db_name = f"live_harness_{uuid.uuid4().hex[:12]}"
    database_url = _with_database(admin_url, db_name)
    grant_list = (
        list(permissions) if permissions is not None else list(SALES_AGENT_PERMISSIONS)
    )

    # #113 review (medium finding) -- held for this call's ENTIRE lifetime
    # (boot through teardown), not just around the env mutation below: see
    # the module docstring's "One `boot_live_harness` at a time per process"
    # note. A second concurrent call blocks here until this one's context
    # manager fully exits, so two booted harnesses can never be up at once
    # invalidating each other's process-global env/`get_settings()` cache.
    async with _boot_lock:
        await _create_database(admin_url, db_name)
        outbound = FakeWhatsAppServer()
        registry_engine: AsyncEngine | None = None
        harness: LiveHarness | None = None
        try:
            await asyncio.to_thread(_run_migrations, database_url, repo_root=_REPO_ROOT)
            if seed_demo:
                await asyncio.to_thread(
                    _seed_demo_company, database_url, repo_root=_REPO_ROOT
                )
            await outbound.start()

            metrics_api_key = secrets.token_hex(16)
            env_overrides = {
                "DATABASE_URL": database_url,
                "REDIS_URL": redis_url,
                "META_WEBHOOK_SECRET": secrets.token_hex(16),
                "WHATSAPP_TOKEN": "live-harness-test-token",
                "WHATSAPP_PHONE_NUMBER_ID": "live-harness-test-phone-id",
                "WHATSAPP_GRAPH_API_URL": outbound.base_url,
                "WHATSAPP_RUNTIME_ID": runtime_id,
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
                model, model_name = build_eval_model()
                registry_engine = create_async_engine(database_url)

                app = create_app(
                    agents={runtime_id: role},
                    grants={runtime_id: grant_list},
                    registry_factory=live_registry_factory_adapter(
                        registry_engine,
                        model,
                        terminal_policy=terminal_policy,
                        extra_tool_specs=extra_tool_specs,
                    ),
                    participant_directory=AcceptAllParticipantDirectory(),
                    title="agents_system live harness",
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
                            model_name=model_name,
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
            if harness is not None:
                await _delete_redis_threads(redis_url, harness.used_thread_ids)
            await _drop_database(admin_url, db_name)
