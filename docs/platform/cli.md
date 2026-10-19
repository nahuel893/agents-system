# Command line

> **Coming in the next release.** Merged on `main`, not in `v0.7.2`.

Installing the package installs a command, `agents`, with an alias, `agents-system`. Both run the same program, so the alias keeps working when another package in the same environment also installs an `agents` command. `python -m agents_system` runs it too. Spanish twin: [`docs/platform_es/cli.md`](../platform_es/cli.md).

```bash
agents check ./my-agent --grant read:catalog
agents chat ./my-agent --provider anthropic --model claude-haiku-5-5
agents serve --agents-root ./agents --port 8000
agents db upgrade
```

The command adds no dependency to the core. `check` and `chat` run on a plain install. `serve` needs the `server` extra and `db` the `postgres` extra; without it, the command fails with the error that names the extra to install (see [Extras](library-usage.md#extras)).

The command's own output goes to stdout. The runtime's logs go to stderr, and only errors are shown; `agents -v <command>` shows all of them. A command that fails prints one line, `agents: error: ...`, and exits with status 1. A usage error exits with status 2.

## Naming an agent

`check` and `chat` take the agent as their first argument:

- **A folder**: `./my-agent`, a path to a folder with `role.md`, `manifest.md` and `policy.md`. It is loaded with `Agent.from_folder`, the same as in Python. A reference agent copied from `examples/agents/` is a folder like any other.
- **A role name**: `agent`, or a predefined role. Naming one of the eight concrete predefined roles is deprecated (ADR-010); `agents check` prints the warning.

## Tools and grants

`check` and `chat` build the agent's runtime with `build_runtime`, exactly as a library caller does. Two flags stand in for what your code passes:

- `--grant PERM`, repeatable: a permission the caller grants, as in `granted_permissions=[...]`. Without it, nothing is granted.
- `--registry MODULE:ATTR`: your `ToolRegistry`. `ATTR` is the registry itself, or a callable that returns one: a function with no arguments, or the served app's registry factory, which is called with `settings`, `embedder` and `bi_engine` all set to `None`. The module is imported from the directory you run the command in. Without the flag, the command uses the platform's reference tools (`knowledge_retrieval`, `conversation_summarizer`, `escalation_notifier`, `order_writer`, the per-user memory and schedule tools, and the report tool when the `postgres` extra is installed). These are unbound: they say which tools exist, and they fail closed if called.

A tool reaches the model only when the agent declares its permission and the caller grants it. The agent's `permissions` are a ceiling, and a grant outside that ceiling equips nothing.

## `agents check <agent>`

Resolves the agent and prints what it resolves to, without running it:

- the chain it extends, leaf first: `shop -> agent -> base`;
- its declared permissions, the registry used and the grants passed;
- its autonomy, whether it reads untrusted input, and its declared model;
- each declared tool, **equipped** or **denied** with the reason;
- its effective execution limits (its own over the platform defaults) and its memory policy;
- any warning resolving it raised, such as a deprecated predefined role.

```text
$ agents check ./shop --registry my_app.tools:registry --grant read:catalog
Agent: shop (version 1.0)
  extends:          shop -> agent -> base
  permissions:      send:escalation, write:orders
  registry:         my_app.tools:registry
  granted:          read:catalog
  ...

Tools:
  escalation_notifier  denied: send:escalation is not granted by the caller
  catalog_search       denied: read:catalog is not in the agent's permissions
  order_writer         denied: write:orders is not granted by the caller
  inventory_sync       denied: not in the registry
```

The reasons for a denied tool:

| Reason | What it means | What to change |
|---|---|---|
| `PERM is not in the agent's permissions` | The tool needs `PERM`, and the agent does not declare it. A grant cannot add it. | Declare it: `permissions:` in `manifest.md`, or `Agent(permissions=[...])`. |
| `PERM is not granted by the caller` | The agent declares `PERM`, and the caller does not grant it. | Grant it: `--grant PERM` here, `granted_permissions=[...]` in code, `DEPLOY_GRANTS` in the served app. |
| `not in the registry` | No tool by that name is registered, and no built-in default fills it. A library build raises `InjectionError` on it, and the served app fails boot. | Register a `ToolSpec` under that name, or pass `--registry`. |
| `tier T3 tools are never granted to an untrusted_input role` | The agent reads untrusted input, and the tool runs commands on the host. | No grant can change it. See [Policy](policy.md). |

An equipped tool that is the library's built-in default, such as the base role's `escalation_notifier` when your registry has none, is shown as `equipped (built-in default)`. It reaches no human (see [Library usage](library-usage.md)).

`check` exits with status 1 when the agent does not resolve (a missing folder, an unknown role, an invalid definition), when `--registry` cannot be loaded, or when `--grant` names a permission nothing registers. A denied tool is not a failure: it is what `check` is for.

## `agents chat <agent>`

An interactive turn loop in the terminal. Each turn sends the whole conversation so far, tool calls and results included, the way a caller of the served app does. The command shows each tool call and its result as they happen, then the reply's text (never its raw content blocks). Type `exit` or `quit`, or press Ctrl-D, to leave.

```text
$ agents chat ./counter --registry my_app.tools:registry --provider anthropic
Chatting with counter on claude-haiku-5-5. Tools: word_count. Type exit or press Ctrl-D to quit.
  (denied: escalation_notifier: missing permissions: send:escalation; see `agents check`)
you> How many words are in 'the quick brown fox'?
  -> word_count({"text": "the quick brown fox"})
  <- word_count: {"words": 4}
agent> There are 4 words.
you> exit
```

It takes `--grant` and `--registry` like `check`, and chooses its model in this order:

1. `--provider` (`anthropic`, `openai_compatible`, `groq` or `ollama`), with `--model` for a model id other than the provider's default.
2. The model the agent declares (`model:` in `policy.md`, or `Agent(model=...)`). `--model` replaces its model id.
3. `ADAPTER_PROVIDER`, from the environment or from `.env` in the current directory, with `--model` as above.

With none of them, the command fails and says so. Credentials are never flags: they come from the environment and `.env`, as for `build_agent_model` (`ANTHROPIC_API_KEY`, `GROQ_API_KEY`, `OPENAI_COMPATIBLE_*`, `OLLAMA_*`). The provider's extra must be installed. The banner names the model the session runs on.

The session keeps no history after it ends, and it records no audit events unless your registry module registers an `AuditSink`.

## `agents serve`

Runs the served app (`agents_system.server`) on uvicorn. Needs the `server` extra, plus the extra of the provider the app is configured with.

| Flag | Default | What it does |
|---|---|---|
| `--agents-root DIR` | unset | The folder that `folder:<name>` registrations are read from. Sets `AGENTS_ROOT` to the folder's absolute path. |
| `--registry MODULE:ATTR` | unset | The registry factory the app builds its tools with. Sets `REGISTRY_FACTORY`. |
| `--host` | `127.0.0.1` | The address to bind. Use `0.0.0.0` in a container. |
| `--port` | `8000` | The port to bind. |
| `--workers` | uvicorn's (one) | Worker processes. |
| `--reload` | off | Restart when the code changes. For development. |

Everything else the app needs (its database, `AGENT_REGISTRATIONS`, `DEPLOY_GRANTS`, the provider) comes from the environment and `.env`, as with `uvicorn agents_system.server:app`. See [Deployment](deployment.md).

## `agents db upgrade` and `agents db current`

The platform's migrations ship inside the package, so an installed wheel can apply them with no checkout and no `alembic.ini`. Revision `007`, the conversation checkpointer's tables, must be applied before the served app boots. Needs the `postgres` extra.

```bash
agents db upgrade --database-url postgresql+asyncpg://user:pw@host:5432/agents
agents db current --database-url postgresql+asyncpg://user:pw@host:5432/agents
```

- `upgrade` applies every pending migration and reports each one on stderr. Running it on a database already at the latest revision changes nothing.
- `current` prints the applied revision, such as `013 (head)`.
- `--database-url` names the database for this command. Without it, the command uses `DATABASE_URL`, and then the served app's configured database (`DB_*` variables or `.env`).

In a source checkout, `alembic upgrade head` keeps working: the repository's `alembic.ini` points at the same migrations, in `src/agents_system/migrations/`.
