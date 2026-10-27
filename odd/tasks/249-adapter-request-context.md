# 249 — Per-request context hook for the OpenAI-compatible adapter

Issue: #249 — Add a per-request context hook to the OpenAI-compatible adapter.
Branch: `feat/249-adapter-request-context` from 98aa9e8.

## Objective

An application can register an optional async hook when building the app. The adapter calls it right before running each turn, with platform metadata only, and attaches the trusted context it returns to that turn. With no hook, behavior is exactly today's.

## Why

Channel turns can carry deterministic context (channel, date, format rules) because the application builds them. Adapter turns cannot, so the same agent behaves differently per entry point. #319 (output files through the adapter) builds on this: the application needs to know whose turn it is.

## Scope (authorized by the owner on 2026-10-27, "resolvé el resto en el orden propuesto")

- The hook: its input (runtime id, a configured channel label, request id, session id, the authenticated caller identity when available) and its output (trusted context attached in a fixed position, never replacing the agent's system prompt).
- Registration on `create_app` (or the adapter's settings), request-local, concurrency-safe.
- Tests offline (TDD), docs EN + ES, the `features.md` row (EN + ES) as coming in the next release.

Not in scope: #319 itself, channel turns, any business concept in the library.

## Constraints

- Client `system` messages stay dropped (AD#6). The agent's system prompt keeps priority over hook output.
- Hook output is trusted (built by the application); user content stays untrusted.
- A failing hook: decide fail-closed vs. skip, document it, test it.
- No behavior change and no test change when no hook is configured.

## Tasks

- [x] T1 — Explore the adapter turn path and the system-prompt assembly, design the hook, RED tests, implement GREEN, docs EN + ES, features row (route: delegated writer; trigger: 2+ non-trivial files plus design).
- [x] T2 — Offline checks: `ruff check .`, `ruff format --check .`, `mypy src/`, `pytest -q` (route: writer self-verification, parent spot check).
- [ ] T3 — Live: composition scenarios through the harness, `tests/test_live_eval_phase1.py`, `tests/test_live_smoke.py`, on Haiku (route: delegated verification).
- [ ] T4 — Native review per work-unit commit, push, PR with `Closes #249`.

## Acceptance criteria (from #249)

- An application registers a hook when building the app and attaches deterministic context to every adapter request without modifying the library.
- Context from concurrent requests never leaks (test with parallel requests).
- Client `system` messages are still filtered; the agent's system prompt keeps priority over hook output.
- Existing callers and tests are unaffected when no hook is configured.
- Documented in the adapter docs, including that hook output is trusted while user content stays untrusted.

## Progress

- 2026-10-27: worktree and feature document created.
- 2026-10-27: T1 done (delegated writer), commit 9514ac7. Design:
  - Hook module `src/agents_system/integration/request_context.py`: frozen `AdapterRequestInfo(runtime_id, channel, request_id, session_id, subject)`, `RequestContextHook = Callable[[AdapterRequestInfo], Awaitable[str | None]]`, `DEFAULT_ADAPTER_CHANNEL = "openai-adapter"`. No web-stack dependency.
  - Registration: `create_app(request_context_hook=..., adapter_channel=...)`, validated at build time and stashed on `app.state`.
  - Adapter: `_request_context` awaits the hook after subject resolution and before admission; the text is passed explicitly as `run_turn_with_usage(trusted_context=...)` only when non-blank, so with no hook the call is byte-identical. Concurrency safety comes from explicit per-request passing, no shared state.
  - Runtime: new `trusted_context` keyword on `run_turn` / `run_turn_with_usage`, rendered by `_call_model` into the single system message AFTER the agent prompt under a fixed `## Context for this request` header. Never in state (same path as the system prompt), counted in `max_context_tokens`. One system message keeps single-system-message providers (Anthropic) working; untrusted blocks stay a separate message after it.
  - Failure: fail-closed, the same convention as `_resolve_subject`: a raising hook or a non-str/None return answers 500 with a fixed message, logs `adapter.request_context_failed`, runs no turn.
  - TDD: RED observed (module missing, then 17 failed / 3 passed), GREEN 20 passed in `tests/test_openai_adapter_request_context.py`, including 20 parallel requests through a real `AgentRuntime` with an echo model.
  - Docs: `docs/platform/deployment.md` and `docs/platform_es/deployment.md` (new section), `features.md` row EN + ES marked as coming in the next release.
- 2026-10-27: T2 done. `uv run ruff check .`: all checks passed. `uv run ruff format --check .`: 620 files already formatted. `uv run mypy src/`: no issues in 106 source files. `uv run pytest -q`: 4001 passed, 262 deselected, 17 xfailed. Forbidden-terms scan over changed files: clean.
  - Note: the new test file was first named `tests/test_adapter_request_context.py` and broke two log-capture tests in `tests/test_agent_runtime.py` by ordering: any earlier test that calls `create_app` runs `setup_logging()` and switches structlog to JSON. Pre-existing; unchanged files reproduce it (`pytest tests/test_openai_adapter.py tests/test_agent_runtime.py` fails the same two tests). Renamed to sort after; the fragility itself is a follow-up candidate.
- 2026-10-27: parent spot check: `pytest -q tests/test_openai_adapter_request_context.py tests/test_openai_adapter.py`: 52 passed. Review assess over origin/main..0dd6330: risk medium, `slice_budget_reached`. The preflight STATUS stopped with `rdd_disabled`: receipt-driven development is off globally, so no native review runs; the medium tier is covered by the writer's self-verification (Opus) plus this spot check.
- 2026-10-27: T3 done (delegated verification), on `claude-haiku-5-5` with `EVAL_RUNS=3` (the reference model's key answers 401):
  - `tests/test_live_eval_phase1.py`: 3 passed (`sql_query` 3/3).
  - `tests/test_live_smoke.py`: 2 passed.
  - `tests/test_live_eval_composition.py`: 3 passed, 11 scenarios at 3/3.
  - `tests/test_live_eval_capabilities.py`: 17 passed, 1 failed. `escalation_builtin_default` held 2/3 against a 100% threshold, and fails the same way on base 98aa9e8, so it is pre-existing.
  - `tests/test_live_eval_guardrails.py`: 1 passed.
  - No live scenario configures a hook, so these runs show no regression when the hook is absent; the hook's own behavior is covered offline.
- Next: push and PR.
