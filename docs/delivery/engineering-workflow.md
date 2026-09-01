# Engineering workflow — the portable standard

**Status:** Directive · **Scope:** any project, not just this one

This is the complete working standard: how a change goes from an idea to
running in production, and what has to be true at each step. It is
**self-contained and copied into a repository unchanged** — it names no
project, links to no other file, and depends on nothing else being present.

Read it in order. The sections build: work tracking defines what a task is,
SDD defines what it means for a task to be understood, TDD defines what it
means for it to be built, design discipline defines what makes it survive, and
delivery defines how it reaches users.

### The one thing to add per project

This document states the standard. It cannot state how much of it *your*
repository actually enforces, and that gap is the only thing that matters on
any given day (§0).

So keep a second, short document beside it — a **compliance record** — and put
nothing in it but repository-specific truth:

- A table of each control in §10, read back from the platform's API, with its
  real value.
- Every deviation, with the reason and what it is waiting on.
- Nothing else. The moment it starts restating the standard, the two drift and
  the reader has to guess which one is current.

That document is the one that changes. This one should not.

---

## 0. The principle everything else rests on

**A rule the platform does not enforce is not a rule. It is a preference with
good PR.**

Every practice below is common. What turns a set of good intentions into a
*process* is that a machine refuses to proceed when one is skipped: branch
protection refuses the merge, CI refuses the green tick, a required review
refuses the button.

This has a corollary that matters when adopting the standard: **do the
enforcement before the documentation.** A written process with no enforcement
decays into folklore in about a month, and then the document is worse than
nothing because it describes a world that no longer exists.

The second corollary is about proof. **Verify by reading state back, never by
observing that a command succeeded.** An API returning `200` means the request
was accepted, not that the setting took effect the way you meant. A test that
passes proves nothing until you have seen it fail.

---

## 1. Where work lives

**One tracker, and it is the issue tracker — never a file in the repository.**

A shared status file is edited by every branch, so every integration conflicts
on it. The tracker is also the only place where state survives a rebase.

Refer to a task by **number and title**: `#44 — order_writer does not persist
orders`. The number links it; the title means a reader does not have to open
it. A bare number in a conversation is unreadable a week later.

| Carried by | What it holds |
|---|---|
| Labels | Priority (`priority:high|medium|low`) and area (`security`, `reliability`, `infra`, `platform`, `process`) |
| Body | Dependencies, as `blocked by #49` — a live link |
| PR body | `Closes #44`, so merging closes the issue and status lives in the platform |

Branch names carry the number too, so branch, PR and issue are one thread:
`fix/44-real-order-writer`.

### Definition of Ready

A task may not be started until it states all five:

- **The problem, not the solution.** "Orders are not persisted", not "add an
  ORM call". Choosing the solution is the implementer's job, and pre-deciding
  it in the ticket is how bad designs get laundered into requirements.
- **Acceptance criteria someone else can check.** Someone other than the
  author must be able to decide whether it is met. "Works well" is not a
  criterion.
- **Dependencies.** What must land first; what breaks if this lands alone.
- **Scope.** Which modules are in play. Stating it up front is what keeps a
  task from quietly becoming a refactor halfway through.
- **Size.** If it obviously exceeds the review ceiling (§5), it is split
  *before* work starts.

A task failing any of these is sent back or marked blocked. **Starting an
underspecified task is the single largest source of scope creep**, because the
scope gets decided silently, by whoever is typing, at the moment they hit the
ambiguity.

> Write an acceptance criterion the slice can actually satisfy. A criterion
> that depends on work outside the slice makes it impossible to close the
> issue honestly — you either lie or you leave it open. Amend the issue; do
> not close over it.

---

## 2. Before code — Spec-Driven Development

SDD is eight phases. Their point is that **the expensive mistakes are made
before any code exists**, and each phase produces a durable artifact that the
next one is checked against.

| Phase | Produces | The question it answers |
|---|---|---|
| **explore** | Findings | What is actually there? |
| **propose** | Proposal | What are we changing, and why now? |
| **spec** | Requirements + scenarios | What must be true when this is done? |
| **design** | Design doc | How, and what did we rule out? |
| **tasks** | Ordered checklist | In what order, in what slices? |
| **apply** | Code | Built under strict TDD (§3) |
| **verify** | Verification report | Does the code match the spec, not the plan? |
| **archive** | Merged specs | The delta becomes the new baseline |

### When SDD is worth it

**Not always, and using it always is its own failure mode.** Route by
ambiguity, never by size:

- **Direct** — the change is understood and mechanical. Just do it.
- **SDD** — durable proposal/spec/design/tasks would materially reduce
  substantial ambiguity, or several people (or sessions) must agree on the
  contract before code exists.

Size, file count and risk alone never select SDD. A 2,000-line mechanical
rename needs no spec. A 40-line change to an authorization boundary might.

For everything in between: **the issue body *is* the spec.** Definition of
Ready already demands problem, criteria, dependencies, scope and size — that
is a small spec. Treat the issue as the contract, build against it, and do not
close it until every criterion passes.

### The failure modes to watch for

These are not hypothetical; they are what actually goes wrong.

**Fabricated justification.** A proposal invents a business reason nobody
stated ("a second consumer needs this seam"). It then propagates into design
and tasks, and by the time anyone notices, three artifacts assert it. When you
find one, correct it *in the artifact*, with an explicit note, so downstream
phases cannot reintroduce it.

**Undercounted scope.** A design that plans slices by walking imports will
miss every module that depends on an *abstraction* rather than on data — and
those are exactly the modules a boundary change touches. Verify slice sizes
against the file system, not against the dependency graph.

**Planning gaps.** A module with no slice assigned is not "out of scope"; it
is a hole. Open an issue for it the moment you find it rather than discovering
it during apply.

---

## 3. Writing code — strict TDD

**RED → GREEN → refactor.** Write the failing test first. Watch it fail. Make
it pass with the smallest change. Then clean up with the test as your net.

The discipline is not about coverage. It is about three things:

### The test must be seen to fail

A test written after the code has never been observed failing, so nothing
proves it can. This is not pedantry — vacuous passes are common and invisible.

**When it matters, prove it by mutation:** break the guard the test names and
confirm the test goes red. If it still passes, the test is decoration.

A real example of why: a fail-closed test asserted that no turn ran when a
dependency was missing. It passed. It also passed when the guard was deleted —
because a *different* error path also skipped the turn. The test was
worthless, and only the mutation revealed it.

### Tests are design feedback, and monkeypatching is the loudest signal

**When a test has to patch a module attribute to run, the code has no seam.**

```python
# The test is rewriting the module under test to make it testable.
monkeypatch.setattr(rag.catalog, "search_vector", fake_search)
```

That line is not a testing technique; it is a design report. The module
imported its dependency instead of receiving it. After inverting the
dependency, the same test passes a stub — and the test file gets *smaller*.

Use this as a rule: **if making something testable requires reaching inside
it, fix the code, not the test.**

### Behaviour, not implementation

Assert against literals written independently, not against values derived from
the object under test. A test that calls a pure function twice and asserts the
results are equal cannot fail for any implementation.

---

## 4. Design discipline

This is the part most process documents omit, and it is the part that decides
whether the codebase is still workable in a year.

### Dependencies point one way: toward the abstract

A mechanism must not know its domain. The moment a general module imports a
specific one, the general module stops being reusable and the specific one
cannot be removed.

**The symptom is an import, and it is easy to grep for.** The fix is to invert
it: the caller supplies what the mechanism needs.

```python
# Before: the retrieval strategy knows one deployment's tables.
from myapp.services import catalog
async def search(session, q, *, settings): ...

# After: it receives them.
class CatalogSource(Protocol):
    async def search_vector(self, session, *, embedding, limit): ...
async def search(session, q, *, settings, source: CatalogSource): ...
```

Reuse the seam the codebase already has. If one module already defines a
`Protocol` for injection, the next one uses a `Protocol` too. **Two mechanisms
for one concept is the debt nobody unwinds later.**

### Composition roots belong to the application, never the library

The function that wires concrete things together — which connector serves
which tool, which adapter backs which port — is the *application's*. A library
that ships one, ships one consumer's decisions to every other consumer.

The test for whether something is a composition root: does it name a specific
deployment, product or customer? A function called `build_acme_registry`
inside a published package answers itself.

The consequence to plan for: **a composition root cannot be extracted before
the application is**, because moving it anywhere inside the package does not
remove it from the package. Sequence accordingly.

### Defaults carry identity, and silently

The most easily missed contamination is not a field; it is a value.

```python
db_name: str = "acme"                     # every consumer inherits this
runtime_id: str = "acme__sales-agent"     # and this
```

Those work for everyone, which is exactly the problem: they are quietly
someone else's topology. **A default that names a specific deployment is a
bug in a library, even though nothing fails.** Neutralize it, or remove it and
force the caller to decide.

Prefer *no default* over a plausible one when the platform genuinely cannot
know the answer. A missing required argument is a `TypeError` at the call
site; a wrong default is a mystery in production.

### Extension by data beats extension by class

When consumers must specialize behaviour, prefer declarative extension —
manifests with `extends:`, configuration with inheritance, a registry they
populate — over a class hierarchy in the library.

The reason is operational, not aesthetic: **with data, a new specialization is
a new file in the consumer's repo. With classes, it is a new release of
yours.**

### Delete, do not relocate, when there is no destination yet

If code must leave a repository but its new home does not exist, deleting it
is usually correct and moving it "somewhere temporary" is not. Version control
already preserves it. A temporary home becomes permanent, and an intermediate
state that nobody ships is a state nobody maintains.

---

## 5. Delivery

### Branch

Trunk-based, off the main line, **living one to two days**. A branch alive for
a week has stopped being a branch and become a fork.

Do not add a long-lived integration branch (`develop`) to get "somewhere safe
to integrate". That need is real; the answer is **feature flags plus a staging
environment**, which give you the same safety without holding work back from
the release line. A `release/x.y` branch cut on demand from a tag is a
different thing and is legitimate for a library that must patch old versions.

### Commit

Conventional Commits (`feat:`, `fix:`, `chore:`, `docs:`, `refactor:`,
`test:`). This is not cosmetic — the prefixes drive the version bump and the
changelog, so a wrong prefix produces a wrong version number.

One commit does one thing. **The body explains why; the diff already shows
what.** A body that restates the diff is wasted; a body that records the
alternative you rejected, or the constraint that forced the shape, is the most
valuable text in the repository six months later.

### Pull request

**A hard ceiling on changed lines** — pick one and enforce it. 400 is the
common figure; a team that reviews well may run higher. Above the ceiling,
review quality collapses: the reviewer approves without reading, and the gate
stops existing while still appearing to.

A large change is not rejected, it is **chained**: each link independently
reviewable, merging in order, each one green on its own.

Pure-deletion PRs are the honest exception. They are large and fast to review,
and splitting them creates broken intermediate states. Declare the exception
and say why.

Every PR body answers, without exception:

1. What problem does this solve, and how do you know it is solved?
2. How was it verified — commands, output, or a failing test that now passes?
3. What is the blast radius?
4. **How is it rolled back?**

A template makes these unavoidable. Relying on memory does not.

### CI

Lint, type check, unit tests, plus any integration job the change touches. CI
must be a **required status check**, so a red build removes the merge button
rather than merely discouraging it.

**A tolerated red build is a dead build.** Once people learn the main line is
sometimes red, CI has stopped being a signal and become noise with a spinner.

Check what CI actually covers. A pipeline that type-checks `src/` but not
`scripts/` will let a runtime break through in the part nobody types — and it
will look green doing it.

### Review

At least one approval from **someone who is not the author**, routed
automatically by a code-owners file so assignment is not a social negotiation.

Review's main value is not defect detection — CI is better at that. It is that
**a second person now understands that code**. Which is why "LGTM" on a
600-line diff is worse than no review at all: it manufactures the appearance
of shared understanding without the fact.

### Merge

The main line stays deployable at every commit. A broken main line blocks
everyone, so it is **reverted first and diagnosed second**.

Delete the branch on merge.

---

## 6. Release and deploy

**Release and deploy are different events**, and conflating them is what makes
rollback slow.

- **Release** — SemVer derived from the commit prefixes, a tag, a generated
  changelog, and a version number that actually moves. A version frozen across
  200 commits means nobody can say what is running.
- **Deploy** — staging automatically on merge; production behind an explicit
  approval.
- **Feature flags** let code ship dark and be switched on separately. That is
  also what makes rollback instant: a flag flip, not a redeploy.

For a library, one more rule: **you cannot version a surface that contains
someone else's code.** If the published package holds one consumer's domain,
every change to that consumer moves the library's version, and the SemVer
contract means nothing. Get the boundary right before the first tag — a
pre-release (`0.1.0-alpha.1`) is the tool for shipping while the surface is
still moving.

### After deploying

The deploy is not the finish line. Smoke tests against the deployed
environment, and health signals watched for a defined window.

Be specific about what "healthy" means. A health endpoint that reports `ok`
while every write is being rejected is worse than no endpoint, because it
converts an outage into a silent one.

### Definition of Done

All of these, not most:

- Merged, branch deleted
- CI green, including the integration jobs the change touches
- Reviewed and approved by someone other than the author
- Documentation updated **in the same PR**, not "later"
- Deployed and verified in a real environment
- Any deferred work has its own ticket — never an undocumented TODO

---

## 7. Knowing whether the process works

Four measurements (DORA). They describe the **delivery system**, and are used
to find bottlenecks — never to rank people. Used for ranking, they are gamed
within a quarter and then measure nothing.

| Metric | Question |
|---|---|
| Deployment frequency | How often does work reach users? |
| Lead time for changes | Commit → production, elapsed |
| Change failure rate | What share of deploys cause a problem? |
| Time to restore | How fast is a bad deploy undone? |

The pair that matters most is **change failure rate and time to restore**.
Shipping often is only a virtue if breakage is rare and recovery is fast.

---

## 8. When something goes wrong

**Blameless postmortem.** The output is a system change, never a person's
name. A process that produces blame produces hidden incidents, and a hidden
incident cannot be fixed.

The question is never "who pushed it" but **"what let it through"** — which
control was missing, unenforced, or misleading.

A worked example: when a credential sat exposed in a public repository for
three months, the finding was not that someone pasted it. It was that secret
scanning was enabled for provider patterns only, and the hook meant to catch
the rest matched PEM headers exclusively. Two controls existed; neither
covered the case. That is a systems finding, and it produces a fix.

---

## 9. Working with AI agents

Agents change the economics of this process, not its rules. Two things need
saying explicitly because they are easy to get wrong.

**The author never approves their own work — and for agents this is
literal.** An agent that wrote a change must not be the one that clears it. If
every pull request is authored by the same account (because the agents share a
token), the platform *cannot* enforce review: the host will refuse
self-approval, so a required-review count of `1` blocks every merge forever.
The honest configuration is `0`, plus a written record that the second pair of
eyes does not exist yet. Do not let a setting you had to disable quietly read
as a guarantee.

**The issue is the contract.** An agent given a well-formed issue produces
work you can check. An agent given an underspecified one produces work that
looks finished. The Definition of Ready is not bureaucracy here; it is the
input format.

Three further rules, learned the hard way:

- **Report failures faithfully.** If tests fail, say so with the output. If a
  step was skipped, say that. An agent that reports success it did not verify
  is worse than one that fails loudly.
- **Do not accept a subagent's result at face value.** Findings from a
  delegated task are input to be checked, not conclusions to be relayed.
- **Stop at decisions that are not yours.** Creating a repository, choosing a
  name, accepting downtime — an agent that invents these to avoid blocking has
  made a decision the human was supposed to make. Finish everything that does
  not depend on the answer, then ask.

---

## 10. Adopting this in a new project

Ordered by what each step buys, not by what is easiest. Steps 1–4 are hours of
configuration and remove entire classes of problem.

1. **Branch protection on the main line.** Required status checks, no direct
   push, no force push, applies to administrators. Read the settings back from
   the API afterwards — a `200` is not confirmation.
2. **CI as a required check**, covering lint, types and tests. Confirm what it
   actually covers; a directory outside the type-check path is a blind spot.
3. **A PR template** carrying the four questions from §5.
4. **Conventional Commits**, enforced by a hook. This is the input to
   versioning, so it has to be right from the first commit.
5. **A code-owners file** routing review automatically.
6. **SemVer, tags and a changelog**, generated from the commit prefixes.
7. **Required approving reviews at `1`** — the moment a second reviewing
   identity exists.
8. **Environments and a deploy pipeline**: staging on merge, production behind
   approval, a defined rollback per deploy.
9. **DORA measurement** — meaningful only once deploys actually happen.

Items 1 through 6 are configuration. Item 7 needs a second person or account.
Items 8 and 9 need the deployment work first.

**Do not adopt them in a different order to feel faster.** Measurement before
enforcement measures a process that is not running, and a written standard
before enforcement is a document about a place that does not exist.
