# ADR-001 — Runtime topology

**Status:** Accepted · **Date:** 2026-08-27 · **Task:** D-029
**Blocks:** D-030, D-031, D-032, D-033, D-034, D-036, D-041, D-042

## Context

Until now the roadmap asked "one process or N workers?" and could not answer,
because the question was missing its input: how many concurrent conversations
the system has to carry. That number is now set.

- **10 concurrent conversations** at launch (ACME, first client).
- **100 concurrent conversations** as the standing target.

Those two numbers do not have the same answer, and the gap between them is not
a matter of adding workers. Measured on the development host:

| Measurement | Value | Source |
|---|---|---|
| Host RAM | 15,877 MB total | `free -m` |
| CPUs | 12 | `nproc` |
| BGE-M3 model on disk | 4.3 GB | `du -sh ~/.cache/huggingface/hub/models--BAAI--bge-m3` |
| PostgreSQL `max_connections` | 100 | `show max_connections` |
| PostgreSQL `superuser_reserved_connections` | 3 | `show superuser_reserved_connections` |
| `shared_buffers` | 128 MB (factory default, untuned) | `show shared_buffers` |
| Connection pool | **unconfigured** → SQLAlchemy default 5 + 10 = 15 per engine | `models/base.py:65` |
| Engines per **server** process | **2** (operational + BI) | `main.py:122`, `main.py:193` |
| Engines per **sync script** process | 2 (operational + medallion) | `scripts/sync_articles.py:36-37` · `sync_clients.py:28-29` |
| `embedding_provider` default | `"local"` | `config.py` |

Two of those numbers set hard ceilings, and both are about *per-process* cost.

**The embedder is resolved once per registry, not per call.** `main.py:171`
builds the registry inside the lifespan and captures the embedder in the
connector closure (`rag_connector.py:108-109` documents this: "BGE-M3 is heavy —
load once per registry, not per call"). That is the right design, and it means
each worker process holds its own 4.3 GB copy of the model.

**Database connections are per-process too.** With the pool left at SQLAlchemy's
default of `pool_size=5, max_overflow=10` (verified against a real async engine,
not assumed from the sync defaults), each engine can open 15 connections.

A **server** process holds two: the operational engine, always, and the BI
engine when `adapter_runtimes` and `bi_database_url` are both set — which the
ACME deployment does, since `data-agent` needs it. The medallion engine is not
one of them: `main.py` never constructs it. It exists only in
`scripts/sync_articles.py` and `scripts/sync_clients.py`, which are separate,
short-lived processes that pair it with the operational engine.

| Server processes | Connections | Against 97 usable |
|---|---|---|
| 1 | 30 | fits |
| 2 | 60 | fits |
| 3 | 90 | fits, with 7 to spare |
| 4 | 120 | **exhausts** |

So with today's defaults the ceiling is **four** server processes.

The seven-connection margin at three processes is the part worth noticing. A
sync script opens its own pair of engines — up to 30 more connections — and
those scripts are exactly the kind of thing a cron job runs while the service
is up. Three server processes plus one running sync is 120 against 97: the
service starts failing to check out connections because a catalog sync fired.
Sizing the pool (D-032) has to account for the scripts, not just the workers.

## Decision

The topology decision is **chained**, not single. The embedder choice decides
whether multiple processes are physically possible; only then does worker count
become a meaningful question.

### 1. The embedder is remote whenever the target is above ~10 concurrent

`LocalBGEEmbeddingProvider` (`services/embeddings.py`) does two things that do
not survive horizontal scaling:

- it holds 4.3 GB of model **per process**, so N workers cost N × 4.3 GB against
  15.8 GB of host RAM — two processes is already most of the machine, three does
  not fit;
- `embed()` dispatches to `loop.run_in_executor(None, ...)`, the shared default
  thread pool, making catalog search **CPU-bound on 12 cores shared with
  everything else the process is doing**. The OpenAI provider is pure I/O and
  adds no CPU or RAM per request.

Therefore:

- **`embedding_provider="local"` stays the default for development** — offline,
  no API key, no per-token cost, and a single process is all a dev box runs.
- **Production at the 100-conversation target uses a remote provider.** Query
  embeddings are short and cheap; the architectural cost of keeping them local
  (one process, forever) is far larger than the token cost of sending them out.

A third option is deliberately left open rather than chosen now: a **dedicated
embedding service** — one process loads BGE-M3 once and workers call it over
HTTP. That keeps embeddings local (no data leaves, no per-token cost) while
letting the API layer scale. It costs an extra deployable unit. Revisit it if
sending catalog queries to a third party becomes unacceptable; do not build it
speculatively.

### 2. Process count follows the target, in two stages

**Stage A — launch, 10 concurrent conversations: a single process.**

Ten concurrent turns are dominated by LLM latency, which is I/O. One event loop
handles that comfortably. A single process needs no shared-state work at all:
`_seq_counter` is correct within one process, one audit queue means one
`dropped_count`, and 30 connections fit inside `max_connections` untouched.

This means the launch configuration is **the current code plus D-030 and D-033**
— not a re-architecture. Stage A is reachable now.

**Stage B — target, 100 concurrent conversations: multiple processes.**

At 100 concurrent turns a single event loop stops being enough — not because of
the LLM calls, which stay I/O, but because JSON serialization, Pydantic
validation, ORM mapping and (if still local) embedding all compete for one core.
Multiple processes are required, and every item in the next section becomes a
prerequisite rather than an improvement.

### 3. What must leave the process before N > 1

Inventoried by reading the runtime, not assumed:

| State | Location | Survives N processes? |
|---|---|---|
| `_seq_counter` | module dict, `audit/recorder.py:44` | **No — D-042.** Every process counts the `"none"` fallback correlation from 1, so two workers emitting a contextless event in the same instant violate `uq_audit_event_correlation_sequence` and lose the whole batch. |
| Audit queue + `dropped_count` | in-memory, `audit/sink.py` | **Partially.** Each process gets its own queue of 1000 and its own counter; nothing aggregates them, so audit loss becomes N invisible numbers instead of one. Needs D-046. |
| Connection pools | SQLAlchemy, unconfigured | **No — D-032.** 4 server processes exhaust `max_connections` at today's defaults, and 3 plus a running sync script already do. |
| Local BGE-M3 embedder | process RAM | **No.** N × 4.3 GB. Resolved by decision 1. |
| Admission control | *does not exist* | **N/A — D-033.** There is no semaphore and no bound on concurrent turns anywhere in the codebase. The roadmap's warning that "the real ceiling is 4× what you wrote" does not apply yet, because nothing is written. |
| `app.state.runtimes` | cached at boot | Yes — read-only after the lifespan builds it. |
| `_pending_emits` | module set, `injector.py:37` | Yes — per-process by design and correct that way. |
| `get_settings` `@lru_cache` | `config.py:217` | Yes — read-only. |
| Checkpointer (conversation state) | Redis | Yes — already shared. |
| Dedup marks | Redis, 300s TTL | Yes — already shared. |
| WhatsApp client | httpx | Yes — stateless. |

### 4. What a restart loses

- **Up to 1000 queued audit events per process**, plus whatever the drainer held
  mid-batch. Worse today than it needs to be: `AuditSink.stop()` cancels the
  drainer before its shutdown flush can run (**D-041**, pinned as a strict
  xfail), so even a graceful shutdown drops the tail.
- **Nothing else.** Conversation state lives in the Redis checkpointer and dedup
  marks live in Redis with a 300 s TTL, so both survive a restart. In-flight
  turns are lost, which is why D-030's background execution has to be paired
  with D-031's dedup claim/release — otherwise a restart mid-turn silently
  swallows the customer's message.

## Consequences

Ordered as they must be done, with what changed for each:

| Task | Was | Now |
|---|---|---|
| **D-033** admission control | medium | **Required for Stage A.** Nothing bounds concurrent turns today. With no limit, 100 arriving conversations open 100 turns and exhaust the pool regardless of process count. This is the cheapest protection in the list and it is needed at 10, not just at 100. |
| **D-030** webhook returns 200 before the turn | high | Unchanged, and now unblocked. Required for Stage A. |
| **D-031** dedup claim/release | medium | Must land with D-030, per above. |
| **D-032** pool sizing | **low** | **Raised.** It is the binding constraint at 4+ server processes, so it is a prerequisite for Stage B, not an optimization. Set `pool_size`/`max_overflow` from settings on both engines, and size them as `max_connections` ÷ expected processes — leaving room for the sync scripts, which open their own pair of engines and can push a 3-process deployment over the limit on their own. |
| **D-042** `_seq_counter` | medium | Now decidable. Since Stage B is a real target, the fix must be one that survives N processes: move the sequence to the database (a per-correlation sequence or an INSERT-time expression), not a smarter in-process counter. Per-request cleanup alone is not sufficient. |
| **D-041** `stop()` loses the queue tail | medium | Unchanged, and more important at Stage B, where N processes each lose a tail on every deploy. |
| **D-046** expose `dropped_count` | medium | Raised in value: at Stage B it is the only way to see aggregate audit loss. |
| **D-036** server provisioning | high | Now has its input: Stage A ships one unit; Stage B needs the unit count parameterised and the pool sized to match. |

## What is deliberately not decided here

- **The exact worker count for Stage B.** It depends on per-turn CPU time, which
  has not been measured. The right sequence is: reach Stage A, measure real turn
  latency and CPU under load, then size. Picking a number now would be a guess
  wearing a decision's clothes.
- **Whether to run a dedicated embedding service** instead of a remote provider.
  See decision 1.
- **`shared_buffers` and PostgreSQL tuning.** 128 MB is the factory default and
  almost certainly wrong for the target, but tuning before there is load to
  measure is premature.

## Not yet measured

Recorded so the next person does not mistake absence for zero:

- Per-turn CPU time and wall latency under concurrent load.
- `LocalBGEEmbeddingProvider.embed()` latency for a single short query — the
  number that would say how much headroom the local path actually has at 10
  concurrent.
- Resident memory of a loaded BGE-M3 model (4.3 GB is the on-disk size).
- Whether `max_connections=100` is what production PostgreSQL will run; it is
  what the development container reports.

## Corrections

- **2026-08-27, from review.** The first version of this ADR claimed up to 3
  engines per process (operational, BI, medallion) and concluded that
  PostgreSQL exhausts at three worker processes. Both were wrong: `main.py`
  never constructs the medallion engine (`rg -n medallion src/agentsys/main.py`
  → no hits), so a server process holds at most 2. The real ceiling is four
  processes, not three. The error made the constraint look tighter than it is,
  and it was found by fact-checking every claim against the code rather than
  by re-reading the document.
