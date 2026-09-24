# Engineering workflow — the portable standard

**Status:** Directive · **Scope:** any project

This is the complete working standard: how a change goes from an idea to
running in production, and what has to be true at each step. It is
**self-contained and copied into a repository unchanged** — it names no
project, links to no other file, and depends on nothing else being present.

Read it in order. The sections build: work tracking defines what a task is,
SDD defines what it means for a task to be understood, TDD defines what it
means for it to be built, design discipline defines what makes it survive,
review defines how it is checked, and delivery defines how it reaches users.

### The one thing to add per project

This document states the standard. It cannot state how much of it *your*
repository actually enforces, and that gap is the only thing that matters on
any given day (§0).

So keep a second, short document beside it — a **compliance record** — and put
nothing in it but repository-specific truth:

- A table of each control in §11, read back from the platform's API, with its
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

Two corollaries, and they are the ones people skip.

**Do the enforcement before the documentation.** A written process with no
enforcement decays into folklore in about a month, and then the document is
worse than nothing because it describes a world that no longer exists.

**Verify by reading state back, never by observing that a command succeeded.**
An API returning `200` means the request was accepted, not that the setting
took effect the way you meant. A script that prints "updated" proves it ran,
not that it changed anything — a find-and-replace whose pattern did not match
writes the file back unchanged and reports success. A test that passes proves
nothing until you have seen it fail.

---

## 1. Where work lives

**One tracker, and it is the issue tracker — never a file in the repository.**

A shared status file is edited by every branch, so every integration conflicts
on it — and a conflict in a status file is resolved by guessing, because there
is no test that can tell you which side was right. The tracker also gives every
task a stable identity that a branch, a commit and a pull request can all point
at.

Refer to a task by **number and title**: `#44 — order_writer does not persist
orders`. The number links it; the title means a reader does not have to open
it. A bare number in a conversation is unreadable a week later.

| Carried by | What it holds |
|---|---|
| Labels | Priority (`priority:high` / `priority:medium` / `priority:low`) and area (`security`, `reliability`, `infra`, `platform`, `process`) |
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
- **Size.** If it obviously exceeds the review ceiling (§6), it is split
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

**The artifacts disagreeing with each other.** A design document and its task
checklist are two halves of one decision. When the decision changes, change
both: a design that says "keep this module" beside a checklist that schedules
its deletion will be executed by whoever reads the checklist. Mark the
superseded decision in place, with the reasoning, rather than editing it away
— the next reader needs to know it was reconsidered, not just what won.

---

## 3. Writing code — strict TDD

**RED → GREEN → refactor.** Write the failing test first. Watch it fail. Make
it pass with the smallest change. Then clean up with the test as your net.

The discipline is not about coverage. It is about one thing: **a test you have
never seen fail proves nothing.**

### Prove it by mutation

When a test guards something that matters, break the guard on purpose and
confirm the test goes red. Restore it afterwards and verify the restore with a
diff.

This is the single highest-yield practice in this document. Every vacuous test
described below was found this way and by nothing else — including tests
written by careful people who believed they were testing what the name said.

Make it cheap enough to do routinely: a small script that applies a
find-and-replace to one production line, runs the suite, records the last line
of output, and restores the file. Run it against every guard you add.

### The catalogue of tests that cannot fail

Recognising these by shape is faster than mutating everything. Each is real
and each shipped in a repository whose author was paying attention.

**It asserts something every branch already satisfies.** A route that returns
`200 {"status": "ok"}` on success, on a duplicate, on an unknown user and on a
misconfiguration is not tested by asserting `200 {"status": "ok"}`. Assert the
first observable step of the path you mean.

**It grades the data, not the enforcement.** "Every deployment's tools are
within its role's allowance" passes because the deployments on disk are all
correct. Delete the validator and it stays green. To test enforcement you must
*construct the malformed input*.

**It compares the module against its own constant.** `assert result.root ==
module._DEFAULT_ROOT`, where `_DEFAULT_ROOT` is imported from the module under
test, can never catch that module computing the wrong value. Derive the
expectation independently.

**Its fixture already declares the value being defaulted.** A test for "an
omitted setting falls back to X" that uses a fixture explicitly declaring X
passes with the fallback deleted.

**It scans source text.** `assert "forbidden.module" not in source` fires on a
docstring reword and misses a transitive import, an aliased import, and a
function-local one. For import boundaries use a fresh-interpreter `sys.modules`
probe *and* an AST parse — the probe cannot see a function-local import and the
scan cannot see a transitive one.

**It measures a process-wide high-water mark.** `ru_maxrss` never decreases, so
`after - before` is zero once anything earlier in the run peaked higher. The
test degrades to vacuous silently as the suite grows. Measure the allocation
itself.

**Its threshold is looser than the bug.** A 500 ms wall-clock assertion cannot
detect a regression that costs 20 ms. Before shipping a threshold, measure the
broken version and confirm the gap is decisive.

**It contains `or True`, or an assertion with no operand under test.** Usually
introduced while making something "pass for now".

**It started passing for a different reason.** The most dangerous class,
because nothing announces it. When a change makes a test terminate earlier —
at a new guard, on a different exception — the test keeps passing while no
longer exercising what it names. **Tests that go red announce themselves;
tests that start passing for a new reason do not.** After changing a shared
code path, re-check the tests that still pass, not only the ones that broke.

### Tests are design feedback

**When a test has to patch a module attribute to run, the code has no seam.**

```python
# The test is rewriting the module under test to make it testable.
monkeypatch.setattr(search_module.storage, "query", fake_query)
```

That line is not a testing technique; it is a design report. The module
imported its dependency instead of receiving it. After inverting the
dependency, the same test passes a stub object instead of reaching inside the
module.

Be careful what you claim for that change. It does **not** reliably make the
test file shorter — an explicit stub is usually more lines than a
`monkeypatch` call. What it buys is that the test stops depending on the
module's internal structure: patching a name where it was imported *to* stops
applying the moment the import moves, and the test then passes without
exercising anything.

Use this as a rule: **if making something testable requires reaching inside
it, fix the code, not the test.**

### A safety test must not be dangerous

A test that proves a sandbox blocks `rm -rf /tmp` must not use `rm -rf /tmp`
as its payload. Under the exact regression it exists to catch, it deletes
`/tmp` — on a developer's machine, in CI — before it reports the failure.

**A safety test whose failure mode is destruction is not a safety test.** Use
a canary: create a marker inside a temporary directory and assert it does not
exist. Same proof, no blast radius.

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
# Before: the ranking strategy knows one deployment's tables.
from myapp.storage import product_table


async def rank(session, query, *, settings): ...


# After: it receives them.
class RecordSource(Protocol):
    async def fetch(self, session, *, query, limit) -> list[Record]: ...


async def rank(session, query, *, settings, source: RecordSource): ...
```

Reuse the seam the codebase already has. If one module already defines a
`Protocol` for injection, the next one uses a `Protocol` too. **Two mechanisms
for one concept is the debt nobody unwinds later.**

Watch where the edge lands after you invert it. An import that moves from a
mechanism into a *composition root* has gone to the right place; an import
that moves from one mechanism to another has only relocated the problem. Count
them before and after.

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
db_name: str = "acme"  # every consumer inherits this
runtime_id: str = "acme__sales-agent"  # and this
```

Those work for everyone, which is exactly the problem: they are quietly
someone else's topology. **A default that names a specific deployment is a bug
in a library, even though nothing fails.**

Prefer *no default* over a plausible one when the platform genuinely cannot
know the answer. A missing required argument is a `TypeError` at the call
site; a wrong default is a mystery in production.

### Fail closed, and check that the shipped object does

"Fails closed" is a property of the object that actually reaches production,
not of the type that describes it. A policy object with an empty allowlist and
a root defaulted to the process working directory is inert for the tool that
consults the allowlist and **wide open** for the tool that consults only the
root. Under a service manager whose default working directory is `/`, that is
the entire filesystem.

Two rules follow:

- **Every guarded capability needs its own guard.** A single switch that
  disables one of two tools has disabled one of two tools.
- **Unconfigured must mean "no working object", not "a working object pointed
  somewhere harmless".** There is no harmless default location.

And the escape hatch has to exist. If the documentation says "an application
that wants this configures it and registers its own", confirm that is possible
— a registry that rejects duplicate names and offers no replacement makes the
documented path raise, and the capability is then permanently off by accident
rather than by design.

### Extension by data beats extension by class

When consumers must specialize behaviour, prefer declarative extension —
manifests with `extends:`, configuration with inheritance, a registry they
populate — over a class hierarchy in the library.

The reason is operational, not aesthetic: **with data, a new specialization is
a new file in the consumer's repo. With classes, it is a new release of
yours.**

Whatever the mechanism, make the declaration real. A key that appears
authoritative in one place and is ignored in another is worse than one ignored
everywhere: a contradiction then reads as a decision and does nothing. Either
honour it or reject it, and prefer rejecting a lie to ignoring it.

### Additive and subtractive composition are different relations

When a hierarchy exists, be explicit about which direction each edge composes
in, and why:

- **Within one trust boundary** (a library's own roles, a base class and its
  subclasses, all authored by the same people) composition is **additive**. A
  child adds capability. Forbidding that makes the hierarchy useless.
- **Across a trust boundary** (a consumer's override of your definition)
  composition is **subtractive**. The consumer may only narrow.

Getting this backwards in either direction is a bug. Applying the subtractive
rule inside one trust boundary blocks legitimate design; applying the additive
rule across one is a privilege escalation.

Two fields resist that split and deserve care: **safety ceilings** — timeouts,
call budgets, supervision levels. Decide deliberately whether a child may
raise them, and note that "the same person writes both sides" is the argument
for allowing it, not an excuse to skip the decision.

### Merging structured values: substitute versus overlay

When a child supplies a partial structure — a limits dict, a policy mapping —
decide whether it **replaces** the parent's or **overlays** it. Replacement is
almost always wrong for ceilings:

```
parent: {timeout: 5, max_calls: 3}
child:  {timeout: 2}
substituted -> {timeout: 2}                 # max_calls ceiling GONE
overlaid    -> {timeout: 2, max_calls: 3}   # both kept
```

The substituted version then falls back to a global default for the missing
key — usually a *looser* one. So a child tightening one limit silently
loosened the rest, and the validator saw nothing wrong because every key the
child actually named really was stricter. **The escape is in the keys it did
not name.**

Two related traps in the same area:

- `dict.get(key, default)` returns the default only when the key is **absent**,
  never when its value is `None`. If `null` is an idiom in your configuration
  meaning "no opinion", a null-valued key bypasses the default and the ceiling
  disappears.
- A validator that skips keys missing from its baseline gives a free hand on
  every key the baseline happens not to mention. Fall back to a global
  default rather than to no bound at all.

### Delete, do not relocate, when there is no destination yet

If code must leave a repository but its new home does not exist, deleting it
is usually correct and moving it "somewhere temporary" is not. Version control
already preserves it. A temporary home becomes permanent, and an intermediate
state that nobody ships is a state nobody maintains.

---

## 5. Review

Review's main value is not defect detection — CI is better at that. It is that
**a second person now understands that code.** Which is why "LGTM" on a
600-line diff is worse than no review at all: it manufactures the appearance
of shared understanding without the fact.

At least one approval from **someone who is not the author**, routed
automatically by a code-owners file so assignment is not a social negotiation.

### What a reviewer actually does

Do **not** re-run the test suite. CI did that, and if you are repeating CI by
hand then CI is not doing its job. Spend the time on what a machine cannot
judge.

1. **Read the PR body before the diff.** Does it answer the four questions in
   §6? If not, send it back there — do not start reading code to compensate for
   a description that is missing.
2. **Read the tests before the implementation.** The tests state the claimed
   behaviour; reading the code first anchors you to what it does rather than
   what it should do.
3. **Then the diff, looking for one thing:** will this shape be a problem in
   six months? Bugs are CI's job. Structure is yours.
4. **Ask whether the rollback is real.** A plain revert, or is there a
   migration, a written row, a flag left on? If the PR does not say, that is
   your finding.

Fifteen minutes per pull request is a reasonable budget. If it routinely takes
more than thirty, the PRs are too large — which is information about the
process, not about you.

### The question that finds the most

**Could this test pass if the code were wrong?**

Look at each assertion and name the production line whose breakage would turn
it red. If you cannot name one, say so in the review. §3's catalogue is the
list of shapes worth suspecting.

### Verify fixes, not only features

A fix is more likely to contain a defect than the code it fixes, and this is
not a slogan — it is the most consistent observation in this document.
Reviewing a fix, hunt three specific failures:

1. **It does not fix the defect.** The symptom moved. The classic form is
   patching the reported *instance* and leaving the *class*: closing a hole on
   one code path and leaving the identical hole on the parallel one, or fixing
   a false claim in one file while the same claim stands in another.
2. **It introduces a new defect.** Widening an exception handler so it now
   swallows a programming error. Changing a public signature. Adding a lazy
   import that breaks at runtime. Bounding memory in a way that silently
   truncates output.
3. **The test that proves it is vacuous.** See above.

### Claims discipline

**Never write "verified" about something you did not run.** A commit message
saying "every guard was mutation-checked" is evidence to the next reader; if
it is false, it is worse than saying nothing, because it spends credibility
the reader has no way to audit.

The same applies to a document. An anecdote used to justify a rule must be
true — a rule argued from a measurement that did not happen is exactly the
fabricated-justification failure mode from §2, in the place where it does the
most damage.

If you catch a false claim of your own, correct it **everywhere it appears**.
Correcting the document and leaving the pull request body asserting the same
thing is the instance-not-class failure applied to prose.

---

## 6. Delivery

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

A breaking change needs its own signal, or every incompatible release ships as
a minor bump and the SemVer contract silently stops meaning anything:

```
feat!: drop the deprecated resolve() positional argument

BREAKING CHANGE: callers must pass roots= explicitly.
```

Either signal alone is enough — the specification treats `!` and the
`BREAKING CHANGE:` footer as equivalent triggers for a major bump. Write both
anyway: the `!` is visible in a one-line log, and the footer is the sentence
the changelog quotes.

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

Check what CI actually covers. A pipeline that type-checks one directory but
not another will let a runtime break through in the part nobody types — and it
will look green doing it.

Guard the *shape* of the suite too. A unit test that quietly needs network, or
downloads a model, or reaches a database, moves the whole suite into a
category it was built to stay out of. If you have a marker for slow or
external tests, a unit test must not opt back in by accident.

### Merge, and the check nobody runs

The main line stays deployable at every commit. A broken main line blocks
everyone, so it is **reverted first and diagnosed second**. Delete the branch
on merge.

**Before merging a batch of branches, merge them together and run the suite.**
Every PR's CI tests that branch against the main line — *none of them tests the
branches against each other*. A required keyword-only argument added in one
branch and a call site written in another will pass both suites separately and
fail the moment both land.

Do it in a throwaway worktree, in the intended merge order. It costs minutes
and it is the only check that sees this class at all. Expect two kinds of
finding:

- **Textual conflicts**, usually two branches appending to the end of the same
  test file. Mechanical, but read them: a conflict can hide a real
  interaction, such as an assertion that stops matching once another branch
  adds an argument.
- **Semantic breaks**, where nothing conflicts and the combined suite fails.
  These are the ones worth the exercise.

---

## 7. Release and deploy

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

Ship the types, too. A package with full annotations and no marker declaring
them (PEP 561's `py.typed`, or your ecosystem's equivalent) gives every
consumer `Any` at exactly the boundary where types matter most: the interfaces
they are meant to implement.

### After deploying

The deploy is not the finish line. Smoke tests against the deployed
environment, and health signals watched for a defined window.

Be specific about what "healthy" means. A health endpoint that reports `ok`
while every write is being rejected is worse than no endpoint, because it
converts an outage into a silent one.

### Keep the onboarding path true

The documented first-run path — copy the example configuration, start the
services, hit the endpoint — is executed by every new person and by nobody
else. It rots invisibly.

When you change a default, change **every** artifact that states it: the
README table, the example environment file, the container definitions, the
sample requests. Patching two of three moves the inconsistency rather than
removing it, and the example file usually *overrides* the default the README
documents — so a mismatch there beats the documentation silently.

### Definition of Done

All of these, not most:

- Merged, branch deleted
- CI green, including the integration jobs the change touches
- Reviewed and approved by someone other than the author
- Documentation updated **in the same PR**, not "later"
- Deployed and verified in a real environment
- Any deferred work has its own ticket — never an undocumented TODO

---

## 8. Knowing whether the process works

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

## 9. When something goes wrong

**Blameless postmortem.** The output is a system change, never a person's
name. A process that produces blame produces hidden incidents, and a hidden
incident cannot be fixed.

The question is never "who pushed it" but **"what let it through"** — which
control was missing, unenforced, or misleading.

A worked example: when a credential sat exposed in a public repository for
three months, the finding was not that someone pasted it. It was that secret
scanning was enabled for provider patterns only, and the hook meant to catch
the rest matched a single key format. Two controls existed; neither covered
the case. That is a systems finding, and it produces a fix.

Watch for the same shape in your own remediation. Deleting a failing test is
the loudest possible way to close a gap without fixing it — and it is easy to
do by accident while editing its neighbour. If a security test disappears in a
diff, that is a finding regardless of intent.

---

## 10. Working with AI agents

Agents change the economics of this process, not its rules. Four things need
saying explicitly.

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
*looks* finished. The Definition of Ready is not bureaucracy here; it is the
input format.

**Adversarial review by a fresh agent is the closest available substitute for
a second person** — and it works. Give the reviewer the diff and a mandate to
attack the claims rather than confirm them, name the places you yourself
suspect, and let it run and mutate code to prove a test can fail. Expect it to
find things in every pass, including in the fixes for what the last pass
found. Budget for that: a fix round is not the end of a review cycle, it is
the start of the next one.

**Three rules for the agent's own conduct:**

- **Report failures faithfully.** If tests fail, say so with the output. If a
  step was skipped, say that. An agent that reports success it did not verify
  is worse than one that fails loudly.
- **Do not accept a subagent's result at face value.** Findings from a
  delegated task are input to be checked, not conclusions to be relayed.
- **Stop at decisions that are not yours.** Creating a repository, choosing a
  name, accepting downtime, loosening a sandbox — an agent that invents these
  to avoid blocking has made a decision the human was supposed to make. Finish
  everything that does not depend on the answer, then ask. Where a
  conservative default exists, take it and say so, rather than blocking on a
  question that has a safe provisional answer.

---

## 11. Adopting this in a new project

Ordered by what each step buys, not by what is easiest. Steps 1–4 are hours of
configuration and remove entire classes of problem.

1. **Branch protection on the main line.** Required status checks, no direct
   push, no force push, applies to administrators. Read the settings back from
   the API afterwards — a `200` is not confirmation.
2. **CI as a required check**, covering lint, types and tests. Confirm what it
   actually covers; a directory outside the type-check path is a blind spot.
3. **A PR template** carrying the four questions from §6.
4. **Conventional Commits**, validated by a **required CI check**. A local
   hook is a convenience, not enforcement — it is bypassable and absent on
   every machine that has not installed it, which is exactly the "preference
   with good PR" §0 warns about. Add the hook too, for the fast feedback; just
   do not count it.
5. **SemVer, tags and a changelog**, generated from the commit prefixes.
6. **A code-owners file** routing review automatically. Listed after
   versioning because routing review buys nothing until there is a second
   identity to route it to (step 7); until then it is configuration that
   documents an intention.
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

---

## The one-page version

If you remember nothing else:

- A rule the platform does not enforce is not a rule.
- Verify by reading state back. A command that succeeded is not a state that
  changed.
- A test you have never seen fail proves nothing. Break the line on purpose.
- Tests that go red announce themselves; tests that start passing for a new
  reason do not.
- A fix is the most likely place for the next defect. Verify fixes, not only
  features.
- When you fix something, ask whether you fixed the instance or the class.
- Never claim you verified something you did not run.
- Merge the branches together before merging them one at a time.
