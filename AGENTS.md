# agents-system — Agent Instructions

Shared instructions for every AI agent working on this repository (Claude Code, Antigravity, OpenCode).

## Project

`agents-system` is a reusable AI agent platform (formerly `agents-acme`). The first delivery is a WhatsApp sales bot for a regional beverage distributor Architecture and specs live in `docs/`. Start with `docs/architecture/agent-platform.md`.

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

## Engram persistent memory

Engram is always active. Save decisions, bugfixes, discoveries, and conventions proactively. Search it before starting work that may have been done before.
