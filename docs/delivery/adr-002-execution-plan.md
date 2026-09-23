# ADR-002 execution plan

How the work recorded in
[ADR-002](../architecture/adr-002-agent-model-and-capabilities.md) gets built:
in what order, in what slices, and what "done" means for each slice.

This document is the *plan*. The *contract* for each slice is its GitHub issue,
and the general rules (Definition of Ready, strict TDD, review, Definition of
Done) live in [engineering-workflow.md](engineering-workflow.md). Where this
plan and that standard disagree, the standard wins and this plan is wrong.

## Principles

- **The issue is the spec.** Every ADR-002 item maps to an issue labelled
  `adr-002`. The issue states the problem, the acceptance criteria, the tests
  and the dependencies. Work does not start on an issue that fails the
  Definition of Ready; the issue is amended first.
- **SDD only where the ambiguity is real.** Wave 3 (agent model and
  persistence) goes through the full SDD cycle, because identity, memory
  isolation and durable history change contracts that several components
  depend on and nobody has agreed on the shape yet. Every other wave routes
  direct: the ADR section plus the issue body already are the spec.
- **Security before evaluation, evaluation before the agent model.** A live
  evaluation of agents whose tools are not yet bounded measures the wrong
  thing; a new agent model built before the evaluation exists cannot be shown
  to be better.

## Waves

Each wave has an epic issue that lists its children in execution order.

| Wave | Goal | Items | Route |
|---|---|---|---|
| 0 — Hygiene (#125) | Make CI trustworthy and remove what misleads | H.31 → H.27 → H.28, F.19–21, B.8–9, H.34 (settings) | Direct |
| 1 — Security (#126) | Deterministic limits on what an agent can do | C.11 → C.10 → C.12 → C.14, C.11 → C.13, C.15, D.17 | Direct, independent review mandatory |
| 2 — Live evaluation (#127) | Test the platform's own agents end to end | E.18 | Direct |
| 3 — Agent model (#128) | Identity, memory and durable persistence | A.1–A.6, G.22–G.26 | Full SDD |

H.29–30 and H.32–33 have no dependencies on the waves and are interleaved
wherever a writer is free.

### Ordering rules inside a wave

- **Wave 0.** H.31 first: it halves CI time for everything that follows.
  B.8–9 must land before any live evaluation, because today the role's design
  prose is sent to the model as its system prompt.
- **Wave 1.** C.11 (`untrusted_input` and its invariant against `exec:*`) is
  the root: tiers (C.10) and channel enforcement (C.13) build on it, declarative
  `command_tools` (C.12) build on tiers, and the bubblewrap sandbox (C.14) wraps
  `command_tools`. C.15 (reference backends) is a prerequisite for Wave 2.
- **Wave 3.** Durable working memory and chat history (A.4 + G.22) go first
  and must either reuse the Postgres-as-source-of-truth decision taken for the
  webhook outbox (#43, #44, #46) or deviate from it explicitly in the design.
  Identity (A.2, #53) precedes agent-own memory (G.25); memory types (A.3)
  precede `memory_policy` enforcement (A.5).

## Slicing

- **One issue, one branch, one worktree, one pull request.** Branch names
  carry the issue number (`feat/NN-short-name`). Worktrees live under the home
  directory, never under `/tmp`.
- **Under ~400 changed lines per pull request.** An issue that cannot fit is
  split before work starts, not during review.
- **No stacked pull requests.** Each branch starts from `origin/main`. If a
  stack is unavoidable, the next pull request is retargeted to `main` *before*
  the one below it is merged; a squash merge of the lower branch otherwise
  strands the upper one.
- **Parallel work only on disjoint files.** At most two or three writers at a
  time, each in its own worktree, and never two writers on the same module.

## Roles

| Role | Who | Responsibility |
|---|---|---|
| Orchestrator | Main session | Picks the next issue, checks it is Ready, dispatches, verifies, merges |
| Writer | Sonnet subagent, one per issue | Red → green → refactor, docs, opens the pull request |
| Proposer / designer | Opus subagent | Wave 3 only: SDD propose and design |
| Reviewer | Fresh agent, not the writer | Adversarial review before merge on high-risk pull requests |

The writer never reviews its own work. A pull request is **high-risk** when it
touches a permission boundary, the tool interceptor, persistence, message
delivery or CI gates — in practice, every Wave 1 pull request and most of
Wave 3.

## Definition of Done for an ADR-002 slice

On top of the Definition of Done in
[engineering-workflow.md](engineering-workflow.md#definition-of-done):

- [ ] A failing test was written first and seen failing (strict TDD); a safety
      test is shown to fail when the guard it protects is removed
- [ ] Documentation updated in English **and** Spanish in the same pull request
- [ ] The item's row in the ADR-002 summary table set to ✅ in both languages
- [ ] Pull request body contains `Closes #NN`
- [ ] CI green, including the integration jobs the change touches
- [ ] High-risk: an independent review ran and its findings are resolved or
      ticketed

## Tracking

The ADR-002 summary table carries the issue number of every item in its
*Planned slice* column. The epics carry the execution order. When the two disagree,
the epic is updated; the ADR records decisions, not scheduling.
