# agents-system — Agent Instructions

Shared instructions for every AI agent working on this repository (Claude Code, Antigravity, OpenCode).

## Project

`agents-system` is a reusable AI agent platform (formerly `agents-badie`). The first delivery is a WhatsApp sales bot for Distribuidora BADIE S.A. Architecture and specs live in `docs/`. Start with `docs/architecture/agent-platform.md`.

## You are working under a multi-agent protocol

Multiple agents work this repo **in parallel**. There is no direct communication between you — coordination is asynchronous through **git + Engram + `delegations.md`**. Full methodology: `docs/delivery/delegation-protocol.md`. Read it before starting delegated work.

### Standing rules

1. **Your task lives in `delegations.md`.** Scan the active wave table for the row(s) where the **Agent** column matches your identity (`claude-code`, `antigravity`, `opencode`) and the status is `todo`. Those are yours — do only that slice. No one needs to hand you an ID.
2. **Isolation.** Work in your own git worktree + branch (`feat/D-00X-<slug>`, worktree `../agents-system-D-00X`). A separate terminal is **not** isolation — the **directory** is: `cd` into your worktree and stay there, never operate in the main checkout, never `git checkout`/`git reset` shared history. Touch only files inside your task's declared **Scope (files)**. Need something out of scope? Stop and set your row to `blocked` with a note — never expand scope silently.
3. **Load context first.** `mem_search` Engram for `delegations/<task-id>` and any referenced topics. Load the skill paths your task lists.
4. **Use the Gentle AI SDD flow** for the work itself (see the section below — it is mandatory, not optional).
5. **On finish:** commit on your branch (conventional commits, **no AI attribution**) → save to Engram under `topic_key: delegations/<task-id>` (what you built, decisions, gotchas, files) → set your row to `in_review` and fill the **Result** note → tell the human. **Do not merge to `main`** — the Lead integrates.

### Roles are slots, not fixed agents

The `Agent roster` table at the top of `delegations.md` maps slot → agent → model and is edited freely. Any agent can take the **Lead** role by reading Engram (`methodology/multi-agent-delegation` + `delegations/*`) and the ledger. The human switches the model per task based on each task's complexity tier.

## SDD flow — how it maps onto this ledger

Gentle AI already installs the SDD skills and Strict TDD in your config — you know the flow. What is **project-specific** (and was being skipped) is how it maps onto our delegation model:

| SDD phase | Owner |
|---|---|
| explore → propose → spec → design → tasks (planning) | Lead — your ledger task block **is** the resulting spec/design/tasks |
| apply (Strict TDD) + verify | **You (worker)** — run them on your slice |
| archive | Lead, at integration |

So: treat your task block as the contract, run apply + verify on it, and do not mark `in_review` until verify passes **every** acceptance criterion. If the task is underspecified, set it `blocked` — don't re-plan or expand scope.

## Engram persistent memory

Engram is always active. Save decisions, bugfixes, discoveries, and conventions proactively. Search it before starting work that may have been done before.
