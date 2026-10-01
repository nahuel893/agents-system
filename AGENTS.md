# agents-system — Agent Instructions

Shared instructions for every AI agent working on this repository (Claude Code, Antigravity, OpenCode).

## Project

`agents-system` is a development kit for building AI agents. You describe an agent, hand it your own tools, and the kit assembles a permission-bound runtime around it; the smallest path is the [Quickstart](docs/platform/library-usage.md#quickstart), an agent with one tool in about ten lines. The server, the channels (WhatsApp, for one), the predefined roles and the reference backends are optional pieces of the kit, not the product. A plain install is the core; the server, the channels and the reference backends are extras (`agents-system[server]`, `[postgres]`, `[embeddings]`, ...; see "Extras" in `docs/platform/library-usage.md`), and the predefined roles ship with the core. Architecture and specs live in `docs/`. Start with `docs/architecture/agent-platform.md`.

## Golden rule — CodeGraph before you read code

This repository is indexed by CodeGraph. **Before** grepping, globbing, reading source files to understand something, or delegating an exploration to a sub-agent, call `codegraph_explore` (MCP tool) or `codegraph explore "<question>"` (shell). One call returns the verbatim, line-numbered source of the relevant symbols, the call path between them, and the blast radius — what depends on the thing you are about to change.

This is not a style preference. A grep-and-read exploration costs dozens of calls and a sub-agent's whole context to reproduce an index that already exists. Delegating the lookup does not avoid the cost — it moves it.

**The trap that makes this rule necessary.** `.codegraph/` is listed in `.gitignore`, and `fd`/`rg` respect `.gitignore` by default — including with `-H`. So:

```bash
fd -H -t d '^\.codegraph$'      # finds NOTHING. Not evidence of anything.
fd -H -I -t d '^\.codegraph$'   # -I / --no-ignore: finds it
```

An agent once concluded "no index here, skip CodeGraph" from the first command and spent a sub-agent reading twelve files by hand. **Do not probe for the index — just call the tool.** If it is genuinely unavailable it will say so, which is a real answer; a silent `fd` miss is not.

Skip CodeGraph only when you already know the exact file and line you need.

## Work tracking — GitHub Issues

Tasks live in **GitHub Issues**, not in a file. `delegations.md` is gone: it
was a ledger for coordinating several agents in parallel, and that is no longer
the situation.

Refer to a task as **`#44 — order_writer does not persist orders`**: the number
so it can be linked, the title so it reads without opening it. Never the bare
number alone.

```bash
gh issue list --label priority:high     # what is urgent
gh issue view 44                        # the full context
gh issue create --title "..." --label "priority:high,reliability"
```

**Link the PR to its issue.** A PR body containing `Closes #44` closes that
issue on merge — the state lives in the platform, so nobody edits a shared file
and the whole class of ledger merge conflicts disappears.

Labels carry what the old table's columns did: `priority:high|medium|low` for
urgency, and `security` / `reliability` / `infra` / `platform` / `process` for
what the task is about. Dependencies are stated in the body ("blocked by #49"),
which GitHub renders as a live link.

Branch naming keeps the issue number so branch, PR and issue are one thread:
`fix/44-real-order-writer`, `feat/49-deploy-units`.

## SDD flow — how it maps onto issues

Gentle AI already installs the SDD skills and Strict TDD in your config — you know the flow. What is **project-specific** (and was being skipped) is how it maps onto our delegation model:

| SDD phase | Owner |
|---|---|
| explore → propose → spec → design → tasks (planning) | The issue body **is** the resulting spec/design/tasks |
| apply (Strict TDD) + verify | Run them on the issue's scope |
| archive | At merge — `Closes #NN` |

So: treat the issue as the contract, run apply + verify against it, and do not close it until every acceptance criterion passes. If the issue is underspecified, say so on the issue rather than guessing — see the Definition of Ready in the delivery flow.

## Definition of done — what every PR proves before it merges

Offline tests prove the code does what the tests say. Only a real model proves an agent still behaves, so a change that can alter agent behavior is not done until a live run says so. Record the **observed** result of every step below in the PR body; a step you skip needs its reason there too.

1. **Offline checks**, from the repository root: `ruff check .`, `ruff format --check .`, `mypy src/`, `pytest -q`. While GitHub Actions cannot run, these local results are the verification of record.
2. **Live scenarios for every behavior change.** A PR that touches the runtime, an agent or role prompt, a tool or connector, a channel, the harness or the permission model runs the live modules that cover what it touched, before merge, and reports per-scenario pass rates. Docs-only, tests-only and CI-only PRs are exempt; say so in the body. If a review fix changes a prompt or behavior after the live run, re-run the affected scenarios.

   | You changed | Run at least |
   |---|---|
   | A role or agent prompt, escalation, no-fabrication | `tests/test_live_eval_roles.py` (plus `tests/test_live_eval_sales_agent.py` for the sales role) |
   | The permission model, the interceptor, guards or execution limits | `tests/test_live_eval_guardrails.py` |
   | The SQL tool or parameterized reports | `tests/test_live_eval_sql_query.py` |
   | The runtime, conversation memory, the served app, a channel, or a tool end to end | `tests/test_live_eval_phase1.py` and `tests/test_live_smoke.py` |
   | The `Agent` API, `extends`, skills or the importer | `tests/test_live_eval_phase4.py` |
   | Concurrency, the webhook worker, the outbox or connection pools | `tests/test_live_load_phase3.py` |

   Always force the remote reference model; local inference is never used:

   ```bash
   set -a; . .env; set +a
   EVAL_PROVIDER=openai_compatible OPENAI_COMPATIBLE_MODEL=openai/gpt-6-sol \
   MODEL_PRICES='{"openai/gpt-6-sol": {"input_per_million": 2.0, "output_per_million": 10.0}}' \
   EVAL_RUNS=3 pytest -m live <module> -k "<scenarios>" -rA
   ```

   Check the model name in the output before trusting a result. Never print `.env`.
3. **Docs in the same PR.** Every behavior or configuration change updates its page under `docs/` and the twin under the matching `docs/*_es/` directory, and its row in `docs/platform/features.md` (EN + ES), labeled as coming in the next release.
4. **Commit identity.** Commits use the GitHub noreply address: run `git config user.email 63928063+nahuel893@users.noreply.github.com` in every checkout and worktree. No AI attribution trailers. Before pushing, check `git log --format='%ae %ce' origin/main..HEAD`.
5. **Nothing private leaves the machine.** No customer or company names, personal emails, secrets or absolute local paths in committed files, PR bodies or issue comments.

## Release gate — what must pass before a tag

A release is cut only with the owner's go-ahead, through `scripts/release.sh pr|tag`. On the release branch, before merging the release PR:

1. **The full live suite passes:** `scripts/run-live-suite.sh` (all suites, `EVAL_RUNS=3`) with the reference model forced as above. Put its summary in the release PR body.
2. **The installed wheel works:** `scripts/smoke-installed-wheel.sh`.
3. **The feature page is current:** in `docs/platform/features.md` and its ES twin, the rows marked "coming in the next release" now belong to this version, and the latest-release line names it.

After tagging, install the release from its tag in a clean project (the core, plus every extra the release touched) and build an agent with it. The full procedure is in `docs/operations/release-process.md`.

## Engram persistent memory

Engram is always active. Save decisions, bugfixes, discoveries, and conventions proactively. Search it before starting work that may have been done before.
