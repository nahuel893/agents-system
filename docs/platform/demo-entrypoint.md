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

## 3. Run it

```bash
uv run python -m agents_system.demo
```

## 4. Call the OpenAI-compatible API

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
