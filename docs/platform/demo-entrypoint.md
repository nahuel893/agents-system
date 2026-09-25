# Demo API entrypoint

`agents_system.demo` serves the fake company database through the platform's
OpenAI-compatible adapter. It is the **serve it** half of
[`demo/load_demo_company.py`](../../demo/load_demo_company.py): load the
repeatable demo data first, then start an API against it. This is a manual
local demonstration, not a deployment configuration.

## 1. Load the demo database

Start the local Postgres service and create the scratch database if needed,
then load its deterministic schema, views, and data:

```bash
uv run python demo/load_demo_company.py
```

The loader refuses non-demo database objects and is deliberately destructive
only to a database it created. See [`demo/README.md`](../../demo/README.md) for
the database setup and safety details.

## 2. Configure the environment

All entrypoint configuration comes from environment variables (or the project's
`.env` file). The demo database URL is separate from the app's `DATABASE_URL`:

| Variable | Default / requirement | Description |
|----------|-----------------------|-------------|
| `DEMO_DATABASE_URL` | `postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/agents_system_demo` | Connection pool for the loaded demo database. |
| `EVAL_PROVIDER` | `ollama` | Chat-model provider: `ollama`, `groq`, `anthropic`, or `openai_compatible`. It is intentionally separate from `ADAPTER_PROVIDER`. |
| `OLLAMA_MODEL` | `qwen2.5:3b` | Ollama model when `EVAL_PROVIDER=ollama`. |
| `OLLAMA_BASE_URL` | optional | Ollama server URL; unset uses the provider default. |
| `GROQ_API_KEY` | required for Groq | Credential used when `EVAL_PROVIDER=groq`. |
| `ANTHROPIC_API_KEY` | required for Anthropic | Credential used when `EVAL_PROVIDER=anthropic`. |
| `OPENAI_COMPATIBLE_BASE_URL` | required for `openai_compatible` | OpenAI-compatible chat endpoint base URL. |
| `OPENAI_COMPATIBLE_MODEL` | required for `openai_compatible` | Model identifier requested from that endpoint. |
| `OPENAI_COMPATIBLE_API_KEY` | optional | Credential for that endpoint; omit for keyless local servers. |
| `DEMO_HOST` | `127.0.0.1` | HTTP bind host. |
| `DEMO_PORT` | `8000` | HTTP bind port. |
| `ADAPTER_RUNTIMES` | required to expose a role | JSON list of published runtime IDs, such as `["_generic__sales-agent"]`. Empty (the default) exposes no `/v1/*` models. |
| `ADAPTER_API_KEY` | required when publishing a role | Bearer token required by `/v1/*`. |

For example, choose one provider and set its variables, then publish a generic
role without putting any credential values in source control:

```bash
export EVAL_PROVIDER=openai_compatible
export OPENAI_COMPATIBLE_BASE_URL=https://your-compatible-endpoint.example/v1
export OPENAI_COMPATIBLE_MODEL=your-model-id
export OPENAI_COMPATIBLE_API_KEY="$YOUR_PROVIDER_API_KEY"
export ADAPTER_RUNTIMES='["_generic__sales-agent"]'
export ADAPTER_API_KEY="$YOUR_DEMO_ADAPTER_API_KEY"
```

`ADAPTER_RUNTIMES` and `ADAPTER_API_KEY` are what actually publish and protect
a role on `/v1/*`; this entrypoint does not set them. The in-progress permission
model will additionally require explicit `DEPLOY_GRANTS` once it lands. Do not
preconfigure that future shape here.

## 3. Satisfy the two startup security checks

`uv run python -m agents_system.demo` boots through `create_app`, and refuses
to start unless two things hold. Both are enforced fail-closed:

1. **A non-empty webhook secret.** `create_app`'s lifespan runs
   `Settings.validate_security_fail_closed` at startup, which raises if
   `META_WEBHOOK_SECRET` is empty — an empty HMAC key makes webhook
   signatures forgeable. Set it to any non-empty value for a local demo run;
   it does not need to be a real Meta secret, since this entrypoint never
   receives WhatsApp webhooks:

   ```bash
   export META_WEBHOOK_SECRET=local-demo-not-a-real-secret
   ```

2. **A genuinely read-only `DEMO_DATABASE_URL` role.** Before serving
   anything, `main()` verifies that the role behind `DEMO_DATABASE_URL` has
   `default_transaction_read_only = on` — the same check the platform runs
   for `BI_DATABASE_URL`. A role that can write is refused. Create a
   dedicated read-only role for the demo database:

   ```sql
   CREATE ROLE agents_system_demo_ro LOGIN PASSWORD 'change-me';
   GRANT CONNECT ON DATABASE agents_system_demo TO agents_system_demo_ro;
   GRANT USAGE ON SCHEMA public TO agents_system_demo_ro;
   GRANT SELECT ON agents_system_customers, agents_system_sales,
     agents_system_sale_items, agents_system_stock
     TO agents_system_demo_ro;
   ALTER ROLE agents_system_demo_ro SET default_transaction_read_only = on;
   ```

   Then point `DEMO_DATABASE_URL` at that role instead of the default
   `postgres` superuser connection, e.g.
   `postgresql+asyncpg://agents_system_demo_ro:change-me@127.0.0.1:5432/agents_system_demo`.

`ALLOW_INSECURE=true` bypasses both checks, but it is the wider hammer: it
*also* allows `ADAPTER_RUNTIMES` to be configured without `ADAPTER_API_KEY`
(an open, unauthenticated `/v1/*`). Prefer `META_WEBHOOK_SECRET` plus a
genuinely read-only role for a demo run, and keep `ALLOW_INSECURE=true` as
the local-dev fallback when that is inconvenient:

```bash
export ALLOW_INSECURE=true
```

## 4. Run it

```bash
uv run python -m agents_system.demo
```

## 5. Call the OpenAI-compatible API

With a role published and the server running, list the available models:

```bash
curl http://127.0.0.1:8000/v1/models \
  -H "Authorization: Bearer $ADAPTER_API_KEY"
```

Then send a chat completion to one of those model IDs:

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $ADAPTER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "_generic__sales-agent",
    "messages": [{"role": "user", "content": "What items are available?"}]
  }'
```

The model provider and the demo database are local operator choices. Do not run
this walkthrough against a production database or expose the local server to an
untrusted network.
