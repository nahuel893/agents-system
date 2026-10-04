# Feature reference

Every feature of the kit on one page, with the name to reach it and the page that explains it. Spanish twin: [`docs/platform_es/features.md`](../platform_es/features.md).

**agents-system** is a development kit for building AI agents. You describe an agent (a system prompt, the tools it may call, the policy that bounds it), hand it your own tools, and the kit assembles a permission-bound runtime around it. The smallest path to a first agent is one tool of your own in about ten lines: see the [Quickstart](library-usage.md#quickstart).

## How to read this page

- Each table row is one feature. **How to use it** names a class, function, setting or file that exists on `main`.
- **Extra needed** is the install extra (see [Extras](library-usage.md#extras)). `None` means the light core is enough.
- A feature marked **coming in the next release** is merged on `main` but not in the latest release, `v0.4.0`. Everything else is in `v0.4.0`.
- Settings are environment variables. `CHECKPOINTER_RETENTION_S` is the setting `checkpointer_retention_s`, and so on.
- Things the kit does not do yet are listed at the end, in [Not in the kit yet](#not-in-the-kit-yet). Nothing there is available.

## Defining agents

| Feature | What it does | How to use it | Extra needed | Docs |
|---|---|---|---|---|
| Agents in Python | Defines an agent as a frozen object: name, tools, permissions, skills, policy fields, model. Checked when built. | `Agent(name=..., extends=..., tools=[...], permissions=[...])` | None | [Role](role.md), [Library usage](library-usage.md) |
| Agents in Markdown folders | Defines an agent as a folder with `role.md` (prompt), `manifest.md` (tools, permissions, skills) and `policy.md` (autonomy, escalation, limits). | `Agent.from_folder(path, **overrides)`; `resolve(...)` returns the `AgentDefinition` | None | [Role](role.md), [Policy](policy.md) |
| Predefined roles | Ships reusable roles: `sales-agent`, `support-agent`, `data-agent`, `summary-agent`, `accountant-agent`, `orchestrator`, `developer-agent`, `operator-agent`. The `base` and `agent` roles are their parents. **Coming in the next release:** naming one of the eight concrete roles is deprecated and emits a `PredefinedRoleDeprecationWarning` that names its reference agent; `base` and `agent` stay. | `Agent(extends="sales-agent")`, or `build_runtime("sales-agent", ...)`; files in `platform/roles/` | None | [Role](role.md#reference-agents), [Library usage](library-usage.md) |
| Reference agents (**coming in the next release**) | The eight concrete roles as agent folders to copy: examples, not supported API, and not in the wheel. Each resolves like your own folder, within the rules every agent folder follows; five differ from their role to fit them. | Copy `examples/agents/<name>/` into your project, then `Agent.from_folder(path)` | None | [Role](role.md#reference-agents), [ADR-010](../architecture/adr-010-capability-evals-and-reference-agents.md) |
| Serve an agent folder from the environment (**coming in the next release**) | An `AGENT_REGISTRATIONS` value `"folder:<name>"` serves an agent folder under a configured root, such as a copy of a reference agent. An unset root, an absolute path, `..` or a symlink out of the root fails boot. The demo registers the reference `sales-agent`. | `AGENTS_ROOT`, `AGENT_REGISTRATIONS={"id": "folder:<name>"}` | `server` plus a provider extra | [Deployment](deployment.md#serving-an-agent-folder-agents_root-coming-in-the-next-release), [Demo](demo-entrypoint.md) |
| Inheritance | A role extends a parent or another `Agent`. Capability adds up; safety can only tighten. | `extends=` in `Agent`, or `extends:` in `role.md` | None | [Role](role.md) |
| Client overrides | A client deployment overrides a role's prompt, tools, skills and policy without touching the core. An override can only narrow permissions and limits. | `build_runtime(..., client="my-client", roots=RootConfig(deployments_root=...))`; folder `deployments/<client>/<role>/` | None | [Deployment](deployment.md), [Library usage](library-usage.md) |
| Skills | Adds reusable instructions to a role, from files or inline text. | `skills:` in `manifest.md`; `Agent(skills=[...], skill_contents={...})` | None | [Skill](skill.md) |
| Model per agent | An agent can declare its own provider and model, checked at build time. | `Agent(model={"provider": ..., "model": ...})`, or `model:` in `policy.md` | The provider extra: `openai`, `anthropic`, `groq` or `ollama` | [Policy](policy.md), [Library usage](library-usage.md) |
| Build the declared model in-process (**coming in the next release**) | Builds the chat model an agent declares from settings and the environment, with no server. `AgentRuntime` uses it when you pass no model. | `agents_system.build_agent_model(definition)` | The provider extra | [Library usage](library-usage.md) |

## Tools and connectors

| Feature | What it does | How to use it | Extra needed | Docs |
|---|---|---|---|---|
| Your own tools | A tool is a `ToolSpec` (name, required permissions, tier, input schema, async connector) held in a registry. Your code runs the connector: database, API, anything. | `ToolRegistry`, `ToolSpec`, `Tier` | None | [Tool](tool.md), [Library usage](library-usage.md) |
| Capability tiers | Every tool declares how dangerous it is: T0 inherent, T1 scoped read, T2 write, send or model-written query, T3 host execution. A tier that understates a permission is rejected. | `ToolSpec(tier=Tier.T2, ...)` | None | [Tool](tool.md) |
| Declarative command tools | Declares a fixed command line in a manifest. The model fills declared parameters only, each checked against a pattern or a list. The command runs without a shell, inside a bubblewrap sandbox. | `command_tools:` in `manifest.md` (permission `run:<name>`, tier T2); `build_command_tool_specs` | None (host needs `bwrap`) | [Tool](tool.md), [Sandbox](../operations/sandbox-bwrap.md) |
| Read-only SQL tool | Lets the model write a read-only query over an allowlist of views (or, on an opt-in, base tables). A guard rejects anything else, and a database role enforces the limits. | `build_sql_query_tool_spec(engine, SqlQueryConfig(...))`; permission `query:sql`; `scripts/provision_sql_readonly.sql` | `sql` | [ADR-007](../architecture/adr-007-read-only-sql-tool.md), [Tool](tool.md) |
| Operator tools | A shell and a file reader for the `operator-agent` role, under a sandbox and terminal policy. Host execution (T3). | `build_operator_tool_specs`, `TerminalPolicy`, `SandboxPolicy` | None (host needs `bwrap`) | [Sandbox](../operations/sandbox-bwrap.md), [Tool](tool.md) |
| Reference backends | Opt-in implementations of the tools the predefined roles declare: catalog search, client lookup, sales reports, message sender, in-memory knowledge base, order writer, summarizer. | `ReferenceBackends` and its `*_tool_spec()` methods; `InMemoryKnowledgeBase`, `InMemoryOrderWriter` | `postgres` (the whole module needs it, in-memory classes included) | [Reference backends](reference-backends.md) |
| Catalog retrieval | Search over a product catalog you provide: vectors when an embedder is available, keywords otherwise. | `build_catalog_rag_connector(embedder, settings, source)` with your `CatalogSource`; `search_catalog`; `EMBEDDING_PROVIDER` | `postgres`, plus `embeddings` for vectors | [Library usage](library-usage.md#extras) |
| Built-in escalation | In-process, a role that inherits `escalation_notifier` gets a default that reaches no human, logs the attempt and records an audit event, so a first agent builds without wiring one. The served app never uses it: boot fails until you register your own. | `build_runtime(..., builtin_defaults=False)` to turn it off; `EquippedRuntime.builtin_defaults` | None | [Library usage](library-usage.md), [Audit](audit.md) |

## Permissions and security

| Feature | What it does | How to use it | Extra needed | Docs |
|---|---|---|---|---|
| Permission families | Permissions are `family:resource` strings: `read`, `write`, `send`, `exec`, `run`, `spawn`, `query`. Each family has a tier. | `agents_system.permissions.builtins` (`Read`, `Write`, `Send`, `Exec`, `Run`, `Spawn`, `Query`) | None | [Permission model](../architecture/permission-model.md), [Tool](tool.md) |
| Grants (build-time barrier) | A tool reaches the model only if the role declares its permission and the caller holds it: `role permissions ∩ grants`. Denied tools never reach the model. | `build_runtime(role, registry, granted_permissions=[...])` | None | [Harness](harness.md), [Policy](policy.md) |
| Call-time interceptor | Revalidates each call with the current permissions before the connector runs. All T2 and T3 tools are always revalidated. | `intercept(...)`, `PolicyViolation`; `always_revalidate` on `ToolSpec` | None | [Interceptor](interceptor.md), [Policy](policy.md) |
| Untrusted input | Marks a role as reading input from outside. It can never hold `exec:*` or receive a T3 tool. Once true, it stays true down the chain. | `untrusted_input: true` in `policy.md`; `Agent(untrusted_input=True)` | None | [Policy](policy.md) |
| Deploy grants | A served runtime gets an explicit permission ceiling. There is no automatic grant, and boot fails without one. | `DEPLOY_GRANTS`; `create_app(grants=...)` | `server` | [Deployment](deployment.md) |
| Fail-closed defaults | The adapter and `/metrics` refuse to boot open unless you opt in to an insecure mode. Webhook bodies are size-capped and signature-checked. | `ADAPTER_API_KEY`, `METRICS_API_KEY`, `ALLOW_INSECURE`, `META_WEBHOOK_SECRET`, `WEBHOOK_MAX_BODY_BYTES` | `server` | [Observability](observability.md), [Channel](channel.md) |
| Audit redaction | Audit events pass through a redactor before they are stored. | `Redactor` | `postgres` (to store) | [Audit](audit.md) |

## Runtime

| Feature | What it does | How to use it | Extra needed | Docs |
|---|---|---|---|---|
| Agent runtime | Runs a turn: model call, permitted tool calls, final answer. Built on LangGraph. Works in your process, with no server. | `AgentRuntime(equipped, model=...)`, `await runtime.run_turn(messages, session_id=...)` | None | [Library usage](library-usage.md), [Harness](harness.md) |
| Execution limits | Caps tool calls, per-call time and total turn time. A timed-out turn reports the timeout and keeps its usage. Limits can only be tightened by overrides. | `execution_limits:` in `policy.md`; `tool_call_timeout_s`, `total_execution_timeout_s`, `max_tool_calls` | None | [Policy](policy.md) |
| Turn usage and cost | Returns token usage and a cost estimate for each turn, from a price table you supply. | `run_turn_with_usage`, `TurnUsage`; `ModelPrice`, `AgentRuntime(model_prices=...)`, `MODEL_PRICES` | None | [Library usage](library-usage.md), [Live eval](live-eval.md) |
| Tool-call repair (**coming in the next release**) | After a turn stops mid-tool (a timeout), the unanswered tool calls are closed in the history the model sees, so the next turn is valid. | Automatic in `AgentRuntime.run_turn` | None | [Context size](context-size.md) |
| Escalation to a human | Roles declare when to escalate. The `escalation_notifier` tool (permission `send:escalation`) hands the case to a channel you provide. | `escalation_rules` in `policy.md`; `EscalationChannel` protocol, `LoggingEscalationChannel` (reference) | None for the protocol; `postgres` for `LoggingEscalationChannel` | [Policy](policy.md), [Reference backends](reference-backends.md) |

## Conversation memory and context

| Feature | What it does | How to use it | Extra needed | Docs |
|---|---|---|---|---|
| Durable conversation memory (**coming in the next release**) | Keeps each conversation in PostgreSQL, so it survives restarts. Replaces the Redis checkpointer. | `open_postgres_checkpointer`, `AgentRuntime(checkpointer=...)` plus `run_turn(..., thread_id=...)`: without a `thread_id` a turn keeps no history; tables from migration `007` | `postgres` | [Conversation memory](conversation-memory.md) |
| Retention and purge (**coming in the next release**) | Deletes conversations idle past a window, on a schedule. The window is its own setting; `never` keeps them. | `CHECKPOINTER_RETENTION_S`, `CHECKPOINTER_PURGE_INTERVAL_S`; `purge_checkpoints`, `CheckpointPurger` | `postgres` | [Conversation memory](conversation-memory.md) |
| Failure policy (**coming in the next release**) | If the store cannot answer in time, the turn degrades instead of stalling, and a metric counts it. | `CHECKPOINTER_TIMEOUT_S`, `CHECKPOINTER_POOL_SIZE` | `postgres` | [Conversation memory](conversation-memory.md) |
| Context budget (**coming in the next release**) | Trims the oldest history when a model call would pass a token budget. Records an audit event and a metric. | `max_context_tokens` in `execution_limits`; `trim_history` | None | [Context size](context-size.md) |
| Conversation isolation | A turn is stateless unless you pass `thread_id`. With a checkpointer, `thread_id` keys the saved history and threads share no state. `session_id` is only a label carried in the turn state. | `AgentRuntime(checkpointer=...)`, `run_turn(..., thread_id=...)` | None (the durable checkpointer needs `postgres`) | [Library usage](library-usage.md), [Conversation memory](conversation-memory.md) |

## Serving and channels

| Feature | What it does | How to use it | Extra needed | Docs |
|---|---|---|---|---|
| Serve agents over HTTP | Builds the FastAPI app for the agents you register. | `create_app(registry_factory=..., agents={...}, grants={...})`; `agents_system.server:app` with `AGENT_REGISTRATIONS`, `REGISTRY_FACTORY` | `server` plus a provider extra | [Deployment](deployment.md), [Operations](../operations/deployment.md) |
| OpenAI-compatible API | Exposes registered agents as models of an OpenAI-style API. | `GET /v1/models`, `POST /v1/chat/completions`; `ADAPTER_RUNTIMES`, `ADAPTER_API_KEY` | `server` | [Deployment](deployment.md) |
| Channel port | Adds a messaging channel with one adapter: it verifies, parses and sends. The kit supplies the route, the inbox, the worker and the audit. | `Channel`, `ChannelAdapter`; `create_app(channels={"name": Channel(...)})`; `POST /webhook/<name>` | `server` | [Channel](channel.md) |
| WhatsApp | A built-in channel with signature check, verification handshake and reply sending. | `WhatsAppChannel`; `WHATSAPP_RUNTIME_ID`, `META_WEBHOOK_SECRET`, `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID` | `server` | [Channel](channel.md) |
| Durable inbox and outbox | Stores each inbound message once, answers the provider at once and processes it later. Work survives crashes and is retried. | `accept_inbound_message`, `DeferredWebhookWorker`; `WEBHOOK_WORKER_POLL_INTERVAL_S`; migrations `002` to `006` | `server` | [Webhook delivery](../architecture/webhook-delivery.md), [Channel](channel.md) |
| Turn admission | Caps how many turns run at once in one process. The worker claims durable work only when a slot is free. | `TurnAdmissionLimiter`; `MAX_CONCURRENT_TURNS`, `ADMISSION_WAIT_TIMEOUT_S` | `server` | [Deployment](deployment.md) |
| Participant identity | Maps a channel sender to a person you know. Identity is per channel, so one channel cannot pose as another. | `ParticipantDirectory`, `ChannelParticipantDirectory`; `create_app(participant_directory=...)` | `server` | [Channel](channel.md) |
| Conversation recorder | A hook for storing the transcript in your own store. The kit ships the protocol only, no production implementation (see #27 below). | `ConversationRecorder` protocol; `create_app(conversation_recorder=...)` | `server` | [ADR-002](../architecture/adr-002-agent-model-and-capabilities.md) |
| Chat providers | Anthropic, Groq, Ollama or any OpenAI-compatible endpoint. | `ADAPTER_PROVIDER`; `OPENAI_COMPATIBLE_BASE_URL`, `OPENAI_COMPATIBLE_API_KEY`, `OPENAI_COMPATIBLE_MODEL` | `anthropic`, `groq`, `ollama` or `openai` | [Library usage](library-usage.md) |

## Observability

| Feature | What it does | How to use it | Extra needed | Docs |
|---|---|---|---|---|
| Liveness and readiness | `/health` always answers 200 with component status. `/ready` answers 503 when a core dependency is down or the outbox has work and no worker. | `GET /health`, `GET /ready`; `scripts/healthcheck.sh` | `server` | [Observability](observability.md), [Operations](../operations/deployment.md) |
| Prometheus metrics | Turn and tool-call counts and durations, limit trips, process memory and CPU. Off unless you enable it. | `GET /metrics`; `METRICS_ENABLED`, `METRICS_API_KEY` | `server` | [Observability](observability.md) |
| Provider metrics | Counts and times the HTTP calls to the model provider, with retries and status classes. | `MetricsHTTPTransport` | `server` | [Observability](observability.md) |
| Request IDs and logs | A request ID on every request and structured JSON logs. | `RequestIdMiddleware`, `setup_logging`; `LOG_LEVEL` | `server` | [Observability](observability.md) |
| Audit trail | Records grants, denials, tool calls, limit trips and lifecycle events in a partitioned PostgreSQL table, in order per conversation. | `AuditSink.set_current(sink)`; migrations `001` and `005` | `postgres` | [Audit](audit.md) |

## Evaluation

| Feature | What it does | How to use it | Extra needed | Docs |
|---|---|---|---|---|
| YAML scenarios | Describes a conversation and what must be true of the result: tools called, text, permission outcomes. | Files in `evals/scenarios/`; `load_scenario`, `load_scenarios`, `Scenario` | `postgres` | [Live eval](live-eval.md) |
| Scenario targets (**coming in the next release**) | A scenario can run against a predefined role, an agent folder under a configured root, or an `Agent` passed from Python. Results and reports carry the agent identifier and kind. | `Scenario(agent_folder=...)`, YAML `agent:`; `run_scenario(agent=..., agents_root=...)` | `postgres` and the provider extra | [Live eval](live-eval.md) |
| Live eval against a real model | Runs scenarios against a real provider, opt-in and never on offline test runs. | `pytest -m live`; `EVAL_PROVIDER`; `build_eval_model` | `postgres` and the provider extra | [Live eval](live-eval.md) |
| Whole suite in one command | Runs every phase and writes logs and a summary. | `scripts/run-live-suite.sh` (`--quick` for one run each) | `postgres` and the provider extra | [Live eval](live-eval.md) |
| Success-rate gates | Repeats each scenario and gates on the share that passes. | `EVAL_RUNS`; `ScenarioGate` | `postgres` and the provider extra | [Live eval](live-eval.md) |
| Guardrail assertions | Checks that a boundary held: a call was blocked, a tool was not called, a limit fired, an audit event was recorded. | `ScenarioAssertions`; `evaluate_assertions` | `postgres` and the provider extra | [Live eval](live-eval.md) |
| Offline mechanism suite (**coming in the next release**) | Proves the interceptor, the execution limits, audit and the sandbox on every offline run with a scripted model that always attempts the guarded call, so a guard cannot go untested. Each check has a twin that disables the guard and must fail. | `evals/scenarios/mechanism/`; `tests/test_mechanism_suite.py` | None | [Live eval](live-eval.md) |
| Load scenarios | Drives concurrent turns through the real app with an injected model. | `pytest -m load`; `create_app(chat_model=...)` | `server` | [Live eval](live-eval.md) |
| Result files | Writes results, tokens and cost per run. | `write_results` | `postgres` | [Live eval](live-eval.md) |

## Installation and packaging

| Feature | What it does | How to use it | Extra needed | Docs |
|---|---|---|---|---|
| Light core (**coming in the next release**) | A plain install is the core, about 76 MB, enough for the Quickstart. Up to `v0.4.0` a plain install brought everything. | `uv add "agents-system @ git+https://github.com/nahuel893/agents-system@<tag>"` | None | [Library usage](library-usage.md#extras) |
| Extras (**coming in the next release**) | Server, database, SQL tool, embeddings and each chat provider are opt-in. | `agents-system[server]`, `[postgres]`, `[sql]`, `[embeddings]`, `[openai]`, `[anthropic]`, `[groq]`, `[ollama]`, `[all]` | as named | [Library usage](library-usage.md#extras) |
| Missing-extra errors (**coming in the next release**) | Using a feature without its extra fails at once and names the extra to install. | `MissingExtraError`; `EXTRA_MODULES` in `agents_system.extras` | None | [Library usage](library-usage.md#extras) |
| Roles inside the wheel | The predefined roles ship in the wheel, so a library user needs no checkout. | Wheel built with `uv build --wheel`; `scripts/smoke-installed-wheel.sh` | None | [Library usage](library-usage.md), [Release process](../operations/release-process.md) |
| Releases | Versions and the changelog come from conventional commits. | `release-please-config.json`; `CHANGELOG.md` | None | [Release process](../operations/release-process.md) |

## Operations

| Feature | What it does | How to use it | Extra needed | Docs |
|---|---|---|---|---|
| Docker deployment | A production Compose stack: database, Redis, migrations, then the app. Ports bind to localhost. | `docker-compose.prod.yml`, `Dockerfile` | None | [Deployment](../operations/deployment.md) |
| systemd deployment | Units for a Linux host: a one-shot migration, then the app. | `deploy/systemd/` | None | [Deployment](../operations/deployment.md) |
| Migrations | Schema changes with Alembic, run before the app starts. | `alembic upgrade head`; `alembic/versions/` | `postgres` | [Deployment](../operations/deployment.md) |
| Sandboxed execution | Runs the built-in operator tools and the declarative command tools in a bubblewrap sandbox on the host. A custom T3 tool of your own runs in the host process, so its connector must sandbox itself. | `bwrap` on the host; `SandboxPolicy` | None | [Sandbox](../operations/sandbox-bwrap.md) |
| Database provisioning | Creates the read-only roles the report and SQL tools rely on. | `scripts/provision_bi_readonly.sql`, `scripts/provision_sql_readonly.sql` | `postgres` | [ADR-007](../architecture/adr-007-read-only-sql-tool.md), [Reference backends](reference-backends.md) |
| Demo | A fake distributor with data and a runnable app, to see the pieces working. | `examples/demo/app.py`; `demo/README.md` | `server` | [Demo entrypoint](demo-entrypoint.md) |

## Not in the kit yet

These are open on GitHub. Each is planned or under discussion. No dates are promised, and none of them is available today.

- [#173 Per-agent metrics built into every agent (AgentMetrics)](https://github.com/nahuel893/agents-system/issues/173)
- [#169 MCP client, phase 1 (ADR-008): stdio servers as T2 tools](https://github.com/nahuel893/agents-system/issues/169)
- [#170 ADR-006: agent-to-agent delegation](https://github.com/nahuel893/agents-system/issues/170). The `orchestrator` role declares `spawn:*` permissions and a delegation policy, but no runtime mechanism spawns a child agent.
- [#185 AgentRouter: route each task to a low-cost or a high-cost agent by its complexity](https://github.com/nahuel893/agents-system/issues/185)
- [#189 ADR-009: agent versioning and a development lifecycle per agent](https://github.com/nahuel893/agents-system/issues/189)
- Wave 3, agent model and persistence ([#31](https://github.com/nahuel893/agents-system/issues/31)):
  - [#23 Agent model: durable identity + memory with ephemeral activation (virtual actor)](https://github.com/nahuel893/agents-system/issues/23)
  - [#25 Three memory types with platform-enforced isolation by agent_id](https://github.com/nahuel893/agents-system/issues/25)
  - [#26 Enforce memory_policy (read_scope/write_scope/persist_conversation)](https://github.com/nahuel893/agents-system/issues/26). The `memory_policy` field is parsed but not enforced.
  - [#27 Production ConversationRecorder implementation (Postgres-backed)](https://github.com/nahuel893/agents-system/issues/27)
  - [#29 Agent-own memory: remember/recall tools](https://github.com/nahuel893/agents-system/issues/29)
  - [#30 Reasoning persistence: explicit decision (persist or discard)](https://github.com/nahuel893/agents-system/issues/30)
