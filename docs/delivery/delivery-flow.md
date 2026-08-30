# Delivery flow — the standard this repository follows

**Status:** Directive · **Date:** 2026-08-29 · Supersedes ad-hoc practice.

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

Nothing in that table is exotic. All of it is missing.

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

## What this repository must add to comply

Ordered by how much each one buys:

1. **Branch protection on `main`** (#54) — required checks, required review,
   no direct pushes. Without this every rule above is voluntary, and it is the
   single highest-value change in the list.
2. **PR template** (#55) — makes the four questions in step 3 unavoidable.
3. **CODEOWNERS** (#56) — routes review instead of leaving it to chance.
4. **Versioning and CHANGELOG** (#57) — SemVer driven by the commit prefixes we
   already write.
5. **Environments and deploy pipeline** (#49) — the deployment work itself.
6. **DORA measurement** (#58) — meaningful only once deploys actually happen.

Items 1 through 4 are configuration and cost hours. Items 5 and 6 need the
deployment work to exist first.
