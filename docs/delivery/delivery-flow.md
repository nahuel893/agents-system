# Delivery flow — the standard this repository follows

**Status:** Directive · **Date:** 2026-08-29 · Supersedes ad-hoc practice.

> **The portable standard now lives in
> [`engineering-workflow.md`](engineering-workflow.md).** That document is the
> complete workflow — spec-driven development, strict TDD, design discipline,
> delivery, release and agent collaboration — written to be copied into any
> repository unchanged.
>
> **This document holds the compliance record for *this* repository** — the
> read-back enforcement table and the ordered gap list at the end. It also
> still restates much of the standard itself, which is duplication the two
> documents carry until it is trimmed. Where they disagree, the portable one
> is authoritative on the rule and this one on what the platform actually
> enforces here.

This is the delivery process the repository follows from now on. It is the
industry-standard flow, not a description of what we happened to be doing.
Where the two differ, this document wins and the gap is a task.

## The one thing that separates a real process from a good habit

Everything below is common practice. What makes it a *process* rather than a
set of intentions is that **the platform enforces it**. A rule that depends on
someone remembering is not a rule; it is a preference with good PR.

Measured on 2026-08-29, this repository had the practices and none of the
enforcement:

| Control | Standard | Here, before this directive |
|---|---|---|
| Branch protection on `main` | Required checks, required review, no direct push | **None.** `GET /branches/main/protection` → 404 |
| CODEOWNERS | Routes reviews automatically | Does not exist |
| PR template | Every PR answers the same questions | Does not exist |
| Versioning | SemVer, tags, CHANGELOG | `0.1.0` static, 0 tags, 0 releases |
| Environments | dev → staging → production, with promotion | 0 configured |
| Rollback | A defined path back, per deploy | Undefined |
| Delivery metrics | DORA four keys | Not measured |

Nothing in that table is exotic. All of it was missing when this document was
written. One line has since changed: branch protection landed on 2026-08-30,
with one criterion deliberately unmet. The closing section records exactly what
is and is not enforced today.

## The flow

### Where work is tracked

**GitHub Issues.** One issue per task, referred to as `#44 — order_writer does
not persist orders`: the number to link it, the title to read it.

A PR body containing `Closes #44` closes the issue on merge, so status lives in
the platform rather than in a versioned file. That is not a convenience — a
shared status file is edited by every branch, and the three rebases in this
repository's last integration all conflicted on exactly that file.

Labels carry priority (`priority:high|medium|low`) and area (`security`,
`reliability`, `infra`, `platform`, `process`). Dependencies go in the body as
`blocked by #49`, which GitHub renders as a live link.

### 0. Definition of Ready — before work starts

A task may not be picked up until it states:

- **The problem, not the solution.** "Orders are not persisted" — not "add an
  ORM call to order_writer". The solution is the implementer's job.
- **Acceptance criteria that can be checked.** Someone other than the author
  must be able to decide whether it is met. "Works well" is not a criterion.
- **Dependencies.** What must land first, and what breaks if this lands alone.
- **Scope.** Which files or modules are in play. Stating it up front is what
  keeps a task from quietly growing into a refactor halfway through.
- **Size.** If it obviously exceeds ~400 changed lines, it is split *before*
  work starts, not after the PR is opened.

A task that fails any of these is not started. It is sent back or marked
blocked. Starting an underspecified task is how scope creep enters.

### 1. Branch — short-lived, off `main`

Trunk-based: branch from `main`, live **one to two days**, merge back. A branch
that lives a week has stopped being a branch and become a fork.

Naming carries the issue number so branch, PR and issue are one thread:
`fix/39-real-order-writer`, `feat/43-webhook-ack`.

### 2. Commits — atomic, conventional

Conventional Commits (`feat:`, `fix:`, `chore:`, `docs:`, `refactor:`,
`test:`). This is not cosmetic: the prefixes drive SemVer and CHANGELOG
generation, so a wrong prefix produces a wrong version number.

One commit does one thing. The body explains **why**; the diff already shows
what.

### 3. Pull request — small, and it answers a fixed set of questions

**Hard ceiling: 400 changed lines.** Above it, review quality collapses — the
reviewer approves without reading, and the gate stops existing. A large change
is not rejected; it is split into a chain where each link is independently
reviewable and merges in order.

Every PR body answers, without exception:

- What problem does this solve, and how do you know it is solved?
- How was it verified? (Commands, output, or a failing test that now passes.)
- What is the blast radius — what else could this break?
- **How is it rolled back?**

### 4. CI — required, not advisory

Lint, type check, unit tests, and any integration job the change touches. CI
must be a **required status check** in branch protection: a red build makes the
merge button unavailable rather than merely discouraged.

A tolerated red build is a dead build. Once the team learns that `main` is
sometimes red, CI has stopped being a signal.

CI runs on every pull request and on every push to `main`: once per commit,
not twice. A newer push to a pull request cancels the run it supersedes; runs
on `main` are never cancelled. A branch without a pull request gets no CI
until it has one.

Formatting is enforced: the `ci` job runs `ruff format --check .` and fails on
drift, and the `ruff-format` pre-commit hook formats what a commit touches.

Coverage is measured, not just tests run: the `ci` job's `Test` step runs the
default unit suite with `pytest --cov=agentsys` and fails the build under a
threshold set at the measured baseline (94, the floor of 94.87 % measured
when the gate was added), then publishes `coverage.xml` and `htmlcov/` as the
`coverage-report` job artifact on every run. Shell scripts are linted too: a
separate `shellcheck` job runs a pinned shellcheck release over every tracked
`*.sh` file, `.claude/hooks/guard-main.sh` included, so a script that would
fail silently in bash (unquoted expansion, word splitting) fails loudly here
instead (ADR-002 H.32, H.33).

### 5. Review — required, and by the right person

At least one approval from someone who is not the author. **CODEOWNERS routes
it**: whoever owns that path is requested automatically, so review assignment
is not a social negotiation.

Review is not primarily defect detection — CI is better at that. Its value is
that a second person now understands that code. That is why "LGTM" on a
600-line diff is worse than no review: it produces the appearance of shared
understanding without the fact.

**The author never approves their own work.** In this repository that applies
to agents literally: an agent that wrote a change may not be the one that
clears it.

> **This one is not enforced yet.** `required_approving_review_count` on `main`
> is `0`. Every pull request here is authored by the single account that owns
> the repository — the account whose token a coding agent also uses — and
> GitHub refuses self-approval, so at `1` nothing could ever merge. Until a
> second reviewing identity exists (#59), an agent *can* merge work it wrote.
> Read the rule above as the standard being aimed at, not a guarantee the
> platform makes.

### 6. Merge — with the branch deleted

`main` stays deployable at every commit. If `main` is broken, everyone is
blocked, so a broken `main` is reverted first and diagnosed second.

Delete the branch on merge. A stale branch list is how people lose track of
what has actually shipped.

### 7. Release — versioned and tagged

SemVer from the commit prefixes. A tag per release, a generated CHANGELOG, and
a version that actually moves. `0.1.0` sitting still across 200 commits means
nobody can say what is deployed.

### 8. Deploy — through environments, never straight to production

`staging` automatically on merge; production behind an explicit approval.
Deploy and release are separate: **feature flags** let code ship dark and be
switched on independently, which is also what makes rollback instant.

### 9. Verify after deploying — the deploy is not the finish line

Smoke tests against the deployed environment, and the health signals watched
for a defined window. This repository already learned the cost of skipping it:
`/health` returned `"ok"` while 100% of audit writes were being rejected.

### 10. Definition of Done

A task is done when **all** of the following hold. Not most.

- Merged to `main`, branch deleted
- CI green, including the integration jobs the change touches
- Reviewed and approved by someone other than the author
- Documentation updated in the same PR, not "later"
- Deployed and verified in a real environment
- Any deferred work has its own ticket — never an undocumented TODO

## How the process knows whether it is working

Four measurements (DORA). They describe the delivery system, and they are used
to find bottlenecks, never to rank people:

| Metric | Question |
|---|---|
| Deployment frequency | How often does work reach users? |
| Lead time for changes | Commit → production, elapsed |
| Change failure rate | What share of deploys cause a problem? |
| Time to restore | How fast is a bad deploy undone? |

The pair that matters most is **change failure rate and time to restore**.
Shipping often is only a virtue if breakage is rare and recovery is fast.

## When something goes wrong

**Blameless postmortem.** The output is a system change, never a person's name.
A process that produces blame produces hidden incidents, and a hidden incident
cannot be fixed.

The question is never "who pushed it" but "what let it through" — which
control was missing, unenforced, or misleading. When a hardcoded token sat in
this public repository for three months, the finding was not that someone
pasted it: it was that secret scanning was enabled for provider patterns only,
and that the hook we added to catch it matched PEM headers exclusively.

## What is enforced, and what is not

### Landed — branch protection on `main` (2026-08-30, #54)

Read back from the API rather than trusted because a call returned `200`:

| Setting | Value |
|---|---|
| Pull request required | yes — a direct `push` to `main` is refused |
| Required status checks | `ci`, `bi-readonly`, `audit-migration` |
| Branch must be up to date before merge | yes |
| Stale approvals dismissed on new commits | yes |
| Applies to administrators | yes |
| Force pushes, branch deletion | both refused |
| **Required approving reviews** | **`0`** |

That last row is the one deviation and it is deliberate. Every pull request in
this repository is authored by the single account that owns it, including the
ones a coding agent opens, because the agent uses that account's token — and
GitHub does not permit self-approval. At `1`, no pull request could ever be
merged. Raising it is tracked as #59, and what it waits on is a second
reviewing identity, not a settings change.

So CI is now genuinely blocking and `main` cannot be pushed to directly. The
second pair of eyes does not exist yet.

### Still missing, ordered by how much each one buys

1. **PR template** (#55) — makes the four questions in step 3 unavoidable. The
   pull request that introduced this very document answered none of them, which
   is the argument for the template rather than against it.
2. **CODEOWNERS** (#56) — routes review instead of leaving it to chance.
3. **Versioning and CHANGELOG** (#57) — SemVer driven by the commit prefixes we
   already write. `agentsys` is consumed as a library, so until tags exist a
   downstream project has no version it can pin.
4. **Required approving reviews at `1`** (#59) — blocked on a second identity.
5. **Environments and deploy pipeline** (#49) — the deployment work itself.
6. **DORA measurement** (#58) — meaningful only once deploys actually happen.

Items 1 through 3 are configuration and cost hours. Item 4 needs a second
account to exist. Items 5 and 6 need the deployment work first.
