# Deployment Model

The platform separates predefined role definitions from client-specific implementations through a two-layer structure. Predefined roles define the behavioral template and capability boundaries. Deployments extend and specialize those roles for a specific client context.

This separation is the resolution of open decision #5: platform IP lives in `platform/roles/`. Client-specific implementation lives in `deployments/{client}/`.

---

## Folder structure

```
platform/
  roles/
    sales-agent/          ← predefined: what a sales agent is
      role.md
      manifest.md
      policy.md
    orchestrator/
      role.md
      manifest.md
      policy.md
    data-agent/
      role.md
      manifest.md
      policy.md
    summary-agent/
      role.md
      manifest.md
      policy.md

deployments/
  acme/
    sales-agent/          ← ACME override: preventista + DeW + colloquial AR Spanish
      role.md
      manifest.md
      policy.md
      skills/
        order_extraction.md
        colloquial_matching.md
        confirm_flow.md
  other-client/
    sales-agent/          ← different override: different language, tools, policy
      role.md
      manifest.md
```

---

## Merge semantics

When the harness instantiates an agent for a deployment, it builds the final definition in two steps:

1. Load the predefined role from `platform/roles/{role-type}/`
2. Merge the client override from `deployments/{client}/{role-type}/`
3. Build the runtime from the merged definition

The override follows these rules per file:

### `role.md` override

The deployment `role.md` extends the predefined role with client-specific context:
- Adds company name, domain, language, and business vocabulary
- Adds client-specific purpose statement on top of the predefined one
- Cannot remove or contradict the predefined role's scope boundaries

### `manifest.md` override

The deployment `manifest.md` can:
- Add tools from the approved platform registry (e.g., `dew_connector`, `app_preventas_writer`)
- Declare which skills from the deployment's `skills/` folder are active
- Restrict context scope (narrower than predefined)

The deployment `manifest.md` cannot:
- Add tools not present in the platform registry
- Elevate the permission requirements of any tool
- Expand context access beyond what the predefined manifest allows

### `policy.md` override

The deployment `policy.md` can:
- Restrict autonomy level (e.g., `supervised` → `confirm`)
- Add stricter escalation rules: `escalation_rules: {inherit: true, add: [...]}` adds conditions, and its `descriptions` describe the ones it adds
- Tighten audit redaction (add `redact_keys`)
- Reduce execution limits (shorter timeouts, fewer tool calls)
- Define client-specific human-in-the-loop thresholds

The deployment `policy.md` cannot:
- Elevate autonomy level beyond the predefined role's ceiling
- Remove an escalation condition defined in the predefined policy. A plain `escalation_rules` mapping replaces the role's, so it must restate every condition; a `remove:` directive may not name an inherited one
- Reword an inherited condition's description, or change or blank the role's `escalate_to`. A plain `escalation_rules` mapping must restate `escalate_to`, since leaving it out would blank it. It need not restate the role's descriptions: a deployment describes only the conditions it adds, so the role's descriptions carry over a plain mapping too. `descriptions` must be a mapping; a `null` (a bare `descriptions:` key), a list or a string raises `DefinitionError` instead of replacing the role's descriptions
- Loosen audit redaction: set `audit_policy.capture_tool_input` to a true value the role does not have, or leave out a key the role's `redact_keys` lists. A plain `audit_policy` mapping replaces the role's, so it must keep them
- Increase execution limits beyond platform defaults. A `null` value counts as the platform default and is checked like any other value; a value that is not a finite, non-negative number (`NaN`, infinity, a negative value, a boolean) raises `DefinitionError`

Breaking any of these raises `DefinitionError`, naming the deployment, the field and the reason, and the runtime is not built. A policy field the deployment leaves out, or leaves empty, keeps the role's value.

### Merge directives

`permissions` takes a list (the subset the deployment keeps), `inherit`, `{inherit: true, add: [...], remove: [...]}` or `{override: [...]}`. A policy mapping (`escalation_rules`, `delegation_policy`, `memory_policy`, `audit_policy`) is either a plain mapping, which replaces the role's, or `{inherit: true, ...}`, which keeps the role's keys and overlays its own. Only `escalation_rules` takes `add`/`remove`, which edit its `conditions`.

A directive the loader does not recognise raises `DefinitionError`: `{remove: [...]}` without `inherit: true`, a mistyped key, `inherit: 'yes'`, `inherit` mixed with `override`, or `add`/`remove` on a policy mapping other than `escalation_rules`. Such a directive used to resolve to the role's full set, so a permission the deployment meant to remove was kept. The same rule applies to predefined roles and custom agents (`docs/platform/role.md`).

### `skills/`

Skills are behavioral prompt modules that shape how the agent reasons about domain-specific tasks — they encode client vocabulary, business rules, and interaction patterns. Skill content resolves from three sources, checked in this order:

1. **Inline Python content** — a custom agent built with `Agent(skill_contents={...})` (or `Agent.from_folder(path, skill_contents={...})`) supplies skill content directly, with no file read.
2. **The custom agent's own folder** — `Agent.from_folder(path)`'s own `skills/<name>.md`, for a custom agent that ships its own default skill content.
3. **The deployment** — `deployments/{client}/{role}/skills/<name>.md`. This is the **only** source a predefined platform role ever resolves skills from; a predefined role has no folder or inline source of its own.

A declared skill that resolves from none of the three sources fails loud (`FactoryError`). A custom agent may combine its own two sources — e.g. ship a folder default and let an inline override (source 1) replace one skill by name. The deployment source is exclusively a predefined-role mechanism: pairing a `client` deployment override with a folder- or inline-sourced agent (any `Agent`/`Agent.from_folder` locator) raises `DefinitionError` at resolve time, since a custom agent has no deployment tree to look one up in.

Both file sources are contained: a skill file must resolve, after `..` and symlinks, inside its own `skills/` folder and inside that folder's root (the importer root for source 2, the deployments root for source 3). A skill or role name with `..` in it, or a symlink leading out, is treated as not found. See `role.md`, "Importer folders stay inside their root".

---

## Permission invariant

> A deployment can only restrict or specialize. It can never elevate.

The predefined role defines the maximum capability surface. No deployment override can exceed it. The Capability Injector enforces this at injection time: if a deployment manifest requests a tool or permission not present in the predefined role's allowed surface, the injection fails and the runtime is not created.

---

## The harness merge algorithm

```
function build_runtime(client, role_type, user_identity):
  generic = load_folder("platform/roles/{role_type}/")
  override = load_folder("deployments/{client}/{role_type}/")  # may not exist

  if override is None:
    definition = generic
  else:
    definition = merge(generic, override)
    assert definition.permissions ⊆ generic.permissions  # invariant
    assert definition.autonomy_level ≤ generic.autonomy_level  # invariant
    assert generic.escalation_conditions ⊆ definition.escalation_conditions  # invariant
    assert definition.audit_redaction ⊇ generic.audit_redaction  # invariant

  return AgentFactory.build(definition, user_identity)
```

If no deployment override exists for a given client and role, the platform uses the predefined definition as-is. This allows gradual specialization — a client can start with the predefined role and add overrides incrementally. This is `resolve()`/`load_override()`'s own behavior when called directly (a library caller with no client of its own); `create_app`'s boot path is stricter whenever a client is named explicitly — see the `clients` bullet below.

---

## Example: ACME sales-agent

### `platform/roles/sales-agent/role.md` (predefined)
```
name: sales-agent
purpose: >
  Assist customers in placing orders through a conversational interface.
  Understand product requests in natural language, match them against the
  available catalog, confirm the order, and persist it.
scope: order-taking, catalog-lookup, order-confirmation
```

### `deployments/acme/sales-agent/role.md` (override)
```
extends: platform/roles/sales-agent
company: a regional beverage distributor
language: es-AR (Rioplatense Spanish)
domain: beer and beverage distribution — Argentina
vocabulary:
  - "la rubia" → BrandA
  - "cajón" → case of 24 units
  - "preventista" → field sales representative
  - "punto de venta" → retail client (kiosk, bar, restaurant)
purpose_extension: >
  Handle orders from registered retail clients (puntos de venta) via
  WhatsApp Business API. Interpret colloquial Argentine product names
  and quantities, match them against the ACME catalog using RAG, and
  persist confirmed orders to the DeW / App Preventas system.
```

---

## Serving: registering runtimes with `create_app`

An application registers what it serves when it builds the app (ADR-004). Each runtime id is the application's own choice, and it is opaque: nothing parses it for a role or a deployment.

```python
app = create_app(
    registry_factory=build_registry,
    roots=RootConfig(deployments_root=Path("deployments")),
    agents={
        "acme-sales": "sales-agent",  # a predefined role
        "triage-bot": Agent.from_folder("agents/triage-bot"),  # a custom agent
    },
    clients={"acme-sales": "acme"},  # deployments/acme/sales-agent/ narrows it
    grants={
        "acme-sales": ["read:catalog", "write:orders"],
        "triage-bot": ["read:catalog"],
    },
)
```

- **Every registered id is built at boot.** One entry that fails to resolve or equip fails the whole boot; no runtime is served while another is dropped.
- **An id** is 1-64 letters, digits, `_` or `-`, starting with a letter or digit.
- **`clients`** is valid only for a predefined role registered by name. It fails boot for an `Agent` entry, for an id `agents` does not register, without `agents`, and for a value that is not a client name (a `str` of letters, digits, `_` or `-`, starting with a letter or digit): a client names a subtractive override, and a silently ignored one would serve the agent without its narrowing. A `None` value, such as an unset environment variable, is not "no client"; leave the id out of `clients` instead. A client override needs an explicit `RootConfig(deployments_root=...)`. **A named client whose deployment override folder does not exist also fails boot** — `deployments/{client}/{role_type}/` must exist once `clients` names it; a typo'd or removed folder would otherwise silently serve the role WITHOUT its narrowing, the same widening risk a missing grant entry guards against below.
- **`grants`** is the explicit deploy-time grant, one list per registered id, and it needs `agents=` to key it against — like `clients`, it fails boot without `agents=`. Nothing is granted automatically: a registered id with no entry fails boot, and so does a bare string in place of a list or a `grants` id that `agents` does not register. Without `grants=`, `DEPLOY_GRANTS` is the source, keyed by the same ids. See `docs/architecture/permission-model.md`.
- **Channels look ids up.** `WHATSAPP_RUNTIME_ID` and every `ADAPTER_RUNTIMES` id must be a registered id, or boot fails naming it. `/v1/models` lists only the `ADAPTER_RUNTIMES` ids, never every registered one.

### Without `agents=`: `AGENT_REGISTRATIONS`

Without `agents=`, `create_app` registers the entries of the `AGENT_REGISTRATIONS` environment variable instead, and builds them through the same rules. It maps each runtime id to a predefined role, as `"{role}"` or `"{role}@{client}"`:

```bash
AGENT_REGISTRATIONS='{"acme-sales": "sales-agent@acme", "support": "sales-agent"}'
ADAPTER_RUNTIMES='["acme-sales", "support"]'
DEPLOY_GRANTS='{"acme-sales": ["read:catalog", "write:orders"], "support": ["read:catalog"]}'
```

- **A value is strict.** At most one `@`; the role and the client each match the id rule above. A malformed id or value fails boot, naming it and `AGENT_REGISTRATIONS`. `__` has no meaning anywhere: it is part of the name.
- **`"{role}@{client}"`** is that id's `clients` entry, so it needs an explicit `RootConfig(deployments_root=...)`. `"{role}"` alone means no deployment override.
- **Predefined roles, or a folder.** An environment variable cannot carry an `Agent` object; a value is a predefined-role name, or `"folder:<name>"` (below). Anything else is registered with `agents=`.
- **`agents=` wins.** When `create_app` gets `agents=`, `AGENT_REGISTRATIONS` is ignored, never merged.

### Serving an agent folder: `AGENTS_ROOT`

A value of the form `"folder:<name>"` registers the agent folder `<name>` under the directory `AGENTS_ROOT` names, for example a copy of a [reference agent](../../examples/agents/README.md):

```bash
AGENTS_ROOT=/srv/agents
AGENT_REGISTRATIONS='{"support": "folder:support-agent"}'
ADAPTER_RUNTIMES='["support"]'
DEPLOY_GRANTS='{"support": ["read:catalog"]}'
```

`AGENTS_ROOT` is the operator's setting and a registration value is not trusted, so every escape fails boot, naming the id and `AGENT_REGISTRATIONS`:

- **Unset or relative root.** A folder value with `AGENTS_ROOT` unset (the default) or not an absolute path fails closed. Without a folder value, `AGENTS_ROOT` is not read.
- **One folder name.** `<name>` is a single folder directly under the root, matching the runtime-id character rule. An absolute path, a `..` segment, a separator or an `@` fails. A folder value takes no `@{client}`: deployment overrides apply to predefined roles only.
- **No symlinks out.** The folder must exist, and once symlinks are followed it must still lie strictly inside the root. A file inside the folder that is a symlink out of it fails the load, as for any agent folder.
- **Siblings stay inside.** The root is the importer root, so a sibling `extends: ../operator-agent` inside it works, and one that leaves it fails.

The folder is held to the importer ceiling like any `Agent.from_folder`. A predefined-role name keeps working, with its deprecation warning.

### Execution limits and the timeout reply

The deployment sets each runtime's limits and the text a timed-out turn answers, keyed by the same ids as `grants`:

```python
app = create_app(
    registry_factory=build_registry,
    agents={"reports": Agent.from_folder("agents/reports")},
    grants={"reports": ["read:catalog"]},
    execution_limits={
        "reports": {"total_execution_timeout_s": 240, "tool_call_timeout_s": 60}
    },
    timeout_reply={"reports": "Esto está tardando más de lo esperado. Probá de nuevo."},
    tool_error_reply={
        "reports": "No pude completar esto porque una herramienta falló. Probá de nuevo."
    },
)
```

Without those arguments, `EXECUTION_LIMITS`, `TURN_TIMEOUT_REPLY` and `TOOL_ERROR_REPLY` are the source, with the same shape:

```bash
EXECUTION_LIMITS='{"reports": {"total_execution_timeout_s": 240, "tool_call_timeout_s": 60}}'
TURN_TIMEOUT_REPLY='{"reports": "Esto está tardando más de lo esperado. Probá de nuevo."}'
TOOL_ERROR_REPLY='{"reports": "No pude completar esto porque una herramienta falló. Probá de nuevo."}'
```

- **Only the deployment raises.** A value replaces the platform default (60 s per turn, 10 s per tool call, 20 tool calls) as that runtime's ceiling; the agent may tighten it, never exceed it.
- **The lease bounds it.** A turn must be shorter than 540 s: the 600 s outbox lease minus the 60 s `LEASE_MARGIN_S` for the reply send and the checkpointer. A turn that does not fit, a tool call longer than its turn (checked again once combined with the agent's own limits), an unknown key, or an id the boot does not build fails boot, naming the runtime. The margin also holds the checkpointer's read and save, so with a channel and a checkpointer `CHECKPOINTER_TIMEOUT_S` may be at most 20 s.
- **The reply is plain text**, at most 1000 characters, sent as given. A runtime without one answers the default English text.
- **Tool errors** *(coming in the next release)*. A tool that raises reaches the model as an error result; `max_tool_errors` (default 3, settable like the other limits) ends the turn with `tool_error_reply` once that many tool calls have raised or timed out. See [Policy: tool errors](policy.md#tool-errors).

See [Policy: execution limits](policy.md#execution-limits).

### Retrying failed messages and the failure reply

A channel message that fails in the deferred worker is retried only when the failure can go away on its own. Each retry runs the whole turn again (model calls, SQL, code execution), so a failure that repeats with the same input is not worth it.

- **Transient failures are retried** with backoff, up to `max_attempts` (default 5): provider rate limits (429), 5xx and timeouts, network and connection errors, database connection errors, `TimeoutError` and `ConnectionError`. A wrapper raised `from` one of these counts too.
- **Everything else is permanent** and runs once: an exception from a tool or the turn logic, a bad input, a security abort. (With tool errors returned to the model, what is left here is mostly bugs and aborts.)
- **The user is told once.** When a message fails for good, whether permanent or out of retries, the user receives a fallback reply through the same channel. The reply is committed with the terminal state and sent after it, so a crash in between loses the notice and never repeats it, and a failed send is audited, not retried. It is not sent when the row already holds a reply (a timed-out turn answers the timeout reply as a normal reply).
- **It is visible.** The terminal audit event `webhook_delivery_terminal_failure` carries `failure_class`, `permanent` and `fallback_reply`; `webhook_fallback_reply` records whether the notice was `delivered`. A permanent failure is logged (`webhook_worker.permanent_failure`) and counted in `outbox_permanent_failures_total{channel, exception, reason}`, with the exception **class name** only, never its message.

```python
from agents_system.services.retry_policy import OutboxRetryPolicy

app = create_app(
    ...,
    outbox_retry={
        "reports": OutboxRetryPolicy(
            max_attempts=3,
            fallback_reply="Algo salió mal con tu mensaje. Probá de nuevo.",
        ),
        # only these exceptions are retried; add include_default=True to keep the built-in list
        "billing": OutboxRetryPolicy.retrying(MyBackendUnavailable, max_attempts=4),
    },
)
```

Without an entry a runtime keeps the defaults (5 attempts, the built-in classifier, an English fallback). `is_transient` takes any predicate over the exception. The environment carries the two plain values, keyed by runtime id; a policy in `create_app` replaces them whole:

```bash
OUTBOX_MAX_ATTEMPTS='{"reports": 3}'
OUTBOX_FALLBACK_REPLY='{"reports": "Algo salió mal con tu mensaje. Probá de nuevo."}'
```

- **The reply is plain text**, at most 1000 characters, sent as given and never templated.
- **Boot fails** for a policy that names a runtime it does not build, or an invalid value, naming the runtime.
- **A crash is not covered.** A row whose worker died on its last attempt is closed at the next claim with the terminal audit event but no reply: the platform does not know the user's address there.

### Per-request context for the OpenAI-compatible API

A channel turn can carry deterministic context (the channel, today's date in the deployment's timezone, output-format rules) because the application builds it. A `/v1/chat/completions` turn is built by the adapter, so the application passes a hook instead:

```python
from datetime import datetime
from zoneinfo import ZoneInfo

from agents_system.integration.request_context import AdapterRequestInfo


async def request_context(info: AdapterRequestInfo) -> str | None:
    today = datetime.now(ZoneInfo("America/New_York")).date()
    return f"Channel: {info.channel}. Today is {today.isoformat()}."


app = create_app(
    ...,
    request_context_hook=request_context,
    adapter_channel="web-chat",  # default "openai-adapter"
)
```

- **Input: platform metadata only.** The adapter awaits the hook once per request with a frozen `AdapterRequestInfo`: `runtime_id` (the requested model), `channel` (the `adapter_channel` label), `request_id` (the `x-request-id` response header), `session_id` (the turn's) and `subject` (the end user a `SubjectResolver` resolved, or `None`; see [End-user scope](end-user-scope.md)). Nothing from the request body reaches it.
- **Output: trusted context in a fixed position.** The text it returns is appended to the system message, after the agent's own prompt, under a fixed `## Context for this request` header. The agent's prompt stays first and keeps priority; the context never replaces it. Client `system` messages are still dropped. `None` or blank text attaches nothing.
- **Trusted versus untrusted.** Hook output is trusted: the application builds it, so it sits in the system message. User content stays untrusted, and so does any stored or external data. Never copy request text, a header value or a database row into the hook's output; pass that kind of data as an untrusted-data block (`run_turn(untrusted_data=...)`) instead.
- **Never persisted.** Like the system prompt, the context is added when the model is called and never stored in the conversation state. It counts against `max_context_tokens`.
- **Request-local.** Each request builds its own context and passes it to its own turn; concurrent requests never see each other's.
- **Fail-closed.** A hook that raises, or returns anything but text or `None`, fails the request with a `500` (`The request context could not be built.`), logs `adapter.request_context_failed` with the stack, and runs no turn. The hook's error never reaches the client. The hook runs before the turn takes a capacity slot.
- **No hook, no change.** Without `request_context_hook`, the adapter calls the runtime exactly as before.

A library caller that runs turns itself passes the same text directly: `run_turn(..., trusted_context="...")`.

### Files a turn produces, through the OpenAI-compatible API

A channel turn can deliver the files it produced (a spreadsheet, a chart) because the application sends them through the channel itself. Chat completions has no field for assistant files, so the adapter answers text only unless the application hands it the files:

```python
from agents_system.integration.output_files import (
    LocalDirectoryFileStore,
    OutputFile,
    OutputFiles,
)
from agents_system.integration.request_context import AdapterRequestInfo


async def turn_files(info: AdapterRequestInfo, messages) -> list[OutputFile]:
    # Find what this turn wrote to this user's workspace.
    return [
        OutputFile(filename="sales.csv", mime_type="text/csv", path=...),
        OutputFile(
            filename="chart.png",
            mime_type="image/png",
            data=png,
            caption="Sales by region",
        ),
    ]


app = create_app(
    ...,
    output_files=OutputFiles(
        hook=turn_files,
        store=LocalDirectoryFileStore("/var/lib/my-app/output-files"),
        inline_image_max_bytes=256 * 1024,  # the default
        max_file_bytes=20 * 1024 * 1024,  # the default
        ttl_s=3600,  # the default
        max_files_per_turn=10,  # the default
        max_total_bytes=50 * 1024 * 1024,  # the default
        public_base_url="https://chat.example.com",  # recommended in production
    ),
)
```

- **The hook runs after the turn.** The adapter awaits it once per request, after the turn finished and released its capacity slot, with the same `AdapterRequestInfo` the [per-request context hook](#per-request-context-for-the-openai-compatible-api) gets (so `info.subject` says whose turn it was) and the turn's messages. It returns `OutputFile`s: `filename`, `mime_type` (a bare `type/subtype`), exactly one of `data` (bytes) or `path`, and an optional `caption`. `None` or an empty list adds nothing. A `path` must be a regular file: a symlink (as the last component), a device, a FIFO or a socket is refused, and the read stops at the size limit whatever the file reports. The adapter does not confine paths to a directory, so the hook resolves every path under a root it owns and never builds one from model or user text.
- **Rendered as Markdown a client already shows.** Each file is appended to the assistant `content`, after the text, one per paragraph. An image at or below `inline_image_max_bytes` becomes `![caption](data:image/png;base64,...)`. A larger image becomes a Markdown image pointing to a download URL, and any other file a link, `[sales.csv](https://.../v1/files/<token>)`, followed by its caption. Filenames and captions are escaped, so they cannot break the Markdown.
- **Download URLs are bearer-free capability links.** `GET /v1/files/{token}` serves the file without the adapter's bearer key, so a browser can open it from the chat. The token is the only authority: 43 URL-safe characters from `secrets`, stored only as its SHA-256 hash, with no listing route. Keep the TTL short: anyone who sees a link (a shared screenshot, a forwarded message, a proxy or access log) can download the file until it expires. The application's request logs (`request.started`, `request.completed`) and uvicorn's access log, once `create_app` has run, record the path as `/v1/files/<redacted>`; a reverse proxy or load balancer in front of the app logs the full URL, token included, unless you configure it not to. Without `public_base_url` the link's host comes from the request's `Host` header, which the client controls; set `public_base_url` in production, and always when the adapter sits behind a proxy that rewrites the host.
- **Expired, unknown or malformed: the same 404.** A link stops working `ttl_s` seconds after the reply. Every miss answers the same `404 Not Found` a missing route answers, so a probe cannot tell whether a file ever existed. A download is streamed from disk as an attachment with its stored MIME type and length, a sanitized filename, `X-Content-Type-Options: nosniff`, `Cache-Control: private, no-store` and a sandboxing `Content-Security-Policy`.
- **Limits.** A file above `max_file_bytes` is left out and logged as `adapter.output_file_skipped` (`reason="too_large"`, its `size` and the `limit`); the other files and the text still answer. A turn renders at most `max_files_per_turn` files (`reason="too_many_files"` for the rest) and at most `max_total_bytes` across them (`reason="turn_total_too_large"` for a file that would cross it). A path that is not a regular file is skipped with `reason="not_a_regular_file"`, one the adapter cannot read with `reason="unreadable"`, and one the store refuses with `reason="store_failed"`.
- **A failing hook answers the text.** A hook that raises, or returns anything but a list of `OutputFile`, logs `adapter.output_files_failed` with the stack and the reply carries the text alone. This differs from the context hook on purpose: the turn already ran and its cost is spent, so failing the request would throw a good answer away.
- **Storage is a port.** `LocalDirectoryFileStore` keeps each file under a server-chosen name in one directory (never the client filename), deletes it once expired when looked up, and sweeps expired files at most once a minute when storing. The sweep touches only the store's own names (a SHA-256 followed by `.json`, `.bin` or a temp suffix), so anything else in the directory stays; give the store a directory of its own anyway. Several processes can share the directory. Another backend implements `OutputFileStore`: `put` returns a new token, `get` returns the file or `None`, `cleanup` deletes expired files.
- **Rendered files do not go back to the model.** A client resends the whole history, rendered files included. With `output_files` set, the adapter rewrites every earlier assistant message before the model sees it: an inline `data:` image becomes `[image: <caption>]`, a download link `[file: <name>]`, and any other `/v1/files/<token>` path `/v1/files/<redacted>`. Base64 never counts against the context, and live download tokens never reach the model provider. User messages are left as they are.
- **Request-local.** Each request passes its own `info` and messages to the hook and renders its own files; concurrent requests never see each other's.
- **Nothing configured, nothing changes.** Without `output_files`, responses are byte-identical to before and `/v1/files/{token}` is not mounted.

### Migrating from the old runtime ids

Before ADR-004, the Settings-driven boot read the role and the deployment out of the runtime id itself: `acme__sales-agent`, or `_generic__sales-agent` for no deployment. That scheme is gone. A runtime id is only a key, and a deployment still configured the old way fails boot, naming the unregistered id and pointing at `AGENT_REGISTRATIONS`. To migrate:

1. **Choose an id for each runtime.** Keeping the old string works only where it is a valid id: `acme__sales-agent` is one (now just a name), `_generic__sales-agent` is not (an id cannot start with `_`).
2. **Register it** in `AGENT_REGISTRATIONS`: what was `acme__sales-agent` becomes `"sales-agent@acme"`, what was `_generic__sales-agent` becomes `"sales-agent"`.
3. **Rename the id everywhere it is used**: `ADAPTER_RUNTIMES`, `WHATSAPP_RUNTIME_ID`, and every `DEPLOY_GRANTS` key. An old `DEPLOY_GRANTS` key is never matched to a renamed id: the runtime fails boot for lack of a grant.
4. **Update OpenAI-compatible clients** (e.g. Open WebUI): they send the id as `model`, and `/v1/models` now lists the new ids.

| Before | After |
|---|---|
| `ADAPTER_RUNTIMES='["_generic__sales-agent"]'` | `AGENT_REGISTRATIONS='{"sales": "sales-agent"}'` and `ADAPTER_RUNTIMES='["sales"]'` |
| `WHATSAPP_RUNTIME_ID=acme__sales-agent` | `AGENT_REGISTRATIONS='{"acme-sales": "sales-agent@acme"}'` and `WHATSAPP_RUNTIME_ID=acme-sales` |
| `DEPLOY_GRANTS='{"acme__sales-agent": [...]}'` | `DEPLOY_GRANTS='{"acme-sales": [...]}'` |

`MODEL_PRICES` needs no change: it is keyed by the provider model id, never by a runtime id. `agents_system.integration.openai_adapter.to_model_id` and `parse_model_id` are deleted with the scheme, with no replacement: code that imports them fails at import time. Register through `create_app(agents=...)` or `AGENT_REGISTRATIONS` instead.

---

## Memory scoping in deployments

The memory layer follows the same two-level scope:

| Scope | What it stores |
|---|---|
| `platform / {role_type}` | Predefined agent behavior learned over time across all deployments |
| `deployment / {client} / {role_type}` | Client-specific knowledge (catalog patterns, client preferences) |
| `deployment / {client} / {role_type} / {user_id}` | Individual user memory (order history, preferences, delivery notes) |

A deployment's memory is always isolated from other deployments. Cross-client memory access is never permitted.

---

## Cross-references

- Role definition schema: `docs/platform/role.md`
- Policy schema and enforcement: `docs/platform/policy.md`
- Capability injection and merge enforcement: `docs/platform/harness.md`
- Permission model and RBAC: `docs/architecture/permission-model.md`
- ACME delivery scope: `docs/delivery/acme-seller-ai.md`
