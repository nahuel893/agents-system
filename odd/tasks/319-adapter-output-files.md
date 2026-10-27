# 319 — Return files a turn produces through the OpenAI-compatible adapter

Issue: #319 — Return files a turn produces through the OpenAI-compatible adapter (inline image or signed expiring link).
Branch: `feat/319-adapter-output-files`, stacked on `feat/249-adapter-request-context` (0dd6330). It rebases onto main once #249 merges.

## Objective

An application can hand the adapter the files a turn produced. The adapter appends them to the assistant message as Markdown a standard client renders: a small image inline as a `data:` URI, anything else as a link to a short-lived download URL. With nothing configured, behavior is exactly today's.

## Why

Through Open WebUI an agent can only answer text: `_extract_assistant_text` drops everything else, so a spreadsheet or chart the agent produced never reaches the user. Channels do not have this limit because the application delivers files itself. #249 supplies the per-request identity (`AdapterRequestInfo`, with the resolved subject) the application needs to find whose files they are.

## Scope (authorized by the owner on 2026-10-27, "resolvé el resto en el orden propuesto")

- The contribution point, registered on `create_app`, request-local.
- Rendering into `message.content` (inline image under a size limit, link otherwise).
- A download route by unguessable, expiring token, with no bearer key; unknown or expired answers 404 without revealing whether the file existed.
- A small storage port with a local-directory default and TTL cleanup.
- Configurable inline limit, file size limit and TTL; a file over the limit is left out and logged, and the turn still answers.
- Tests offline (TDD), docs EN + ES, the `features.md` row (EN + ES).

Not in scope: streaming responses (#314), inbound media, audio (#320), object-storage backends.

## Tasks

- [x] T1 — Design and implement with TDD, docs EN + ES, features row (route: delegated writer; trigger: 2+ non-trivial files plus design). Commit `162180e`.
- [x] T2 — Offline checks: `ruff check .`, `ruff format --check .`, `mypy src/`, `pytest -q`.
- [ ] T3 — Live: composition scenarios through the harness, `tests/test_live_eval_phase1.py`, `tests/test_live_smoke.py`, on Haiku.
- [ ] T4 — Push and PR with `Closes #319`, based on #249's branch until it merges.

## Acceptance criteria (from #319)

- An application registers the contribution point when building the app and gets its files into the adapter response without modifying the library.
- Concurrent requests never see each other's files (parallel-requests test).
- Images render in a standard client: Markdown image with an inline `data:` URI below the limit, a link above it.
- Download tokens expire, cannot be guessed or enumerated; an expired or unknown token answers 404 without leaking whether the file ever existed.
- Size and TTL limits are configurable; a file over the limit is left out and logged, and the turn still answers.
- No behavior change when nothing is configured; existing tests unaffected.
- Documented in the adapter docs, including that download URLs are bearer-free capability links and should stay short-lived.

## Progress

- 2026-10-27: worktree and feature document created.
- 2026-10-27: T1 done in `162180e` (delegated writer). Design: `create_app(output_files=OutputFiles(hook, store, inline_image_max_bytes, max_file_bytes, ttl_s, public_base_url))` in `integration/output_files.py`; the hook runs after the turn, outside its capacity slot, with the request's `AdapterRequestInfo` and the turn's messages; `GET /v1/files/{token}` lives on a separate `output_files_router` mounted only when configured, with no bearer key. Tokens are `secrets.token_urlsafe(32)`, stored as their SHA-256 under server-chosen names by `LocalDirectoryFileStore`; unknown, expired and malformed tokens answer FastAPI's own `404 Not Found`. A failing hook answers the text alone and logs `adapter.output_files_failed`, since the turn's cost is already spent; an oversize, unreadable or store-refused file logs `adapter.output_file_skipped`. Added beyond the brief: inline `data:` images in earlier assistant messages are replaced by `[image: label]` before the model sees the history, only when output files are configured, so resent base64 never fills the context. TDD: RED observed (collection error, module missing), then 34 new tests green.
- 2026-10-27: T2 observed on the `162180e` tree: `ruff check .` all checks passed; `ruff format --check .` 623 files already formatted; `mypy src/` no issues in 107 source files; `pytest -q` 4035 passed, 262 deselected, 17 xfailed; forbidden-terms scan exit 1; no absolute home paths. Open: the request-id middleware logs the request path, so download tokens appear in application logs; the docs name logs as a reason to keep the TTL short.
- Next: T3 live scenarios, then T4 push and PR.
