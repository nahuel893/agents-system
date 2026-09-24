# Feature: demo entrypoint (issue #46)

Worktree: /home/nh/agents-system-worktrees/46-demo-entrypoint
Branch: feat/46-demo-entrypoint
Issue: nahuel893/agents-system#46 — No documented entrypoint to serve the
OpenAI-compatible adapter over the demo database

## Acceptance criteria (from the issue)

- [ ] Documented entrypoint (`python -m agents_system.demo`) builds the demo
      `RegistryFactory` via `build_live_registry_factory` and serves it
      through `create_app`, configured entirely from env vars (no hardcoded
      credentials or business data).
- [ ] README.md (or a linked docs/ page) documents: what it needs
      configured, the one command that starts it, what it serves.
- [ ] A smoke test exercises the entrypoint end-to-end: the app it produces
      boots and responds on /health.

## Design (resolved from exploration, do not re-derive)

- New module `src/agents_system/demo.py` (not the existing top-level
  `demo/` dir, which stays script-only for `load_demo_company.py`).
- Reuse `agents_system.evals.provider.build_eval_model()` for the chat model
  (EVAL_PROVIDER switch, already dispatches through `main._build_chat_model`
  incl. the `openai_compatible` branch). Do NOT invent new provider code.
- Reuse `agents_system.evals.live_registry.build_live_registry_factory`.
- `_demo_registry_factory(engine, model)` adapts the zero-arg closure to the
  `RegistryFactory` Protocol shape `create_app` expects
  (`settings, embedder=None, bi_engine=None`).
- `build_app(*, engine, model) -> FastAPI` — pure, testable, no env reads.
  `main()` does env var resolution (`DEMO_DATABASE_URL`, default matches
  `demo/load_demo_company.py`'s `_DEFAULT_URL`; `DEMO_HOST` default
  `127.0.0.1`; `DEMO_PORT` default `8000`) + `uvicorn.run(...)`. Import-safe:
  nothing runs at import time, guarded by `if __name__ == "__main__"`.
- Smoke test: `build_app(engine=MagicMock(), model=MagicMock())`, then set
  `app.state.engine` to a mocked engine and patch
  `agents_system.main.get_redis_client` — same pattern
  `tests/test_health.py` already uses (ASGITransport `AsyncClient`, no real
  lifespan run, no network, no Postgres/Redis/GPU). Hit GET /health, assert
  200 / status ok.
- Docs: short "Run the demo API" section in README.md linking to
  `docs/platform/demo-entrypoint.md` (+ `docs/platform_es/demo-entrypoint.md`
  twin, since `docs/platform_es/` mirrors `docs/platform/*.md` 1:1 today).
  Cover: load the demo DB (`demo/load_demo_company.py`), env vars (incl.
  `ADAPTER_RUNTIMES`/`ADAPTER_API_KEY` to actually publish a role on `/v1`,
  and a forward note that the in-progress permission-model change will add
  `DEPLOY_GRANTS`), the one command, curl examples for `/v1/models` and
  `/v1/chat/completions`.

## Tasks

1. [x] RED: write `tests/test_demo_entrypoint.py` (fails: module doesn't exist)
2. [x] GREEN: add `src/agents_system/demo.py`
3. [x] Docs: README "Run the demo API" section + `docs/platform/demo-entrypoint.md`
       + `docs/platform_es/demo-entrypoint.md`
4. [x] Verify: targeted tests, full `pytest -q`, `ruff check .`,
       `ruff format --check .`, `mypy src/` — all green
5. [ ] Commit (conventional, no AI attribution), push, open PR, watch CI

## Evidence log

- Tasks 1-3 delegated to gentle-ai-worker. RED: `ModuleNotFoundError: No module
  named 'agents_system.demo'`. GREEN: `tests/test_demo_entrypoint.py` 1 passed;
  `tests/test_health.py` regression check 14 passed. Files: src/agents_system/demo.py,
  tests/test_demo_entrypoint.py, README.md (+30 lines), docs/platform/demo-entrypoint.md,
  docs/platform_es/demo-entrypoint.md.
