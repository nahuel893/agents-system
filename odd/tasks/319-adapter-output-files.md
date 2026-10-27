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
- 2026-10-27: `ce921c3` (parent) redacts the download token in the request middleware's `request.started` and `request.completed` events.
- 2026-10-27: independent security review of `0dd6330..ce921c3`; all findings fixed in `75320e9` (delegated writer, TDD: 11 new tests failed first, the FIFO and `/dev/zero` cases hung, and `strip_inline_images` took 1.2 s on 5k and 5.1 s on 10k `![` openers).
  - B1 ReDoS in history stripping: a match starts only at an unescaped bracket, labels exclude unescaped brackets and are bounded to 1000 characters, URLs stop at a bracket. Now linear: 200k openers in 0.06 s; regression tests on five hostile shapes stay under 1 s.
  - S1 token in uvicorn's access log: `setup_logging` adds `DownloadTokenFilter` to `uvicorn.access` once; it rewrites the path in `record.args`. `loggable_path` moved to `observability/logging.py`. Docs name what is redacted and that a reverse proxy logs the full URL.
  - S2 path reads: `O_NOFOLLOW | O_NONBLOCK`, `S_ISREG` required, read bounded to the limit plus one byte, in a thread. Symlink, FIFO and `/dev/zero` skip with `not_a_regular_file`; `/proc/self/status` (st_size 0) skips as too large. Docs: hooks resolve paths under a root they own.
  - S3 cleanup scope: only `<sha256>.json`/`.bin` and their stale `.tmp` files (`stale_tmp_s`, default the TTL); a foreign `package.json` survives.
  - S4 volume: `max_files_per_turn` (10) and `max_total_bytes` (50 MiB) skip with `too_many_files` and `turn_total_too_large`; `StoredFile` carries an open handle and downloads stream via `StreamingResponse` with `Content-Length`, same headers and 404s.
  - N1 tokens to the model: download links in assistant history become `[file: name]`, leftover `/v1/files/<token>` paths become `<redacted>`; `strip_inline_images` is now `strip_rendered_files`. User messages untouched.
  - N2 docs: `public_base_url` recommended in production because the link host otherwise comes from the `Host` header.
  - Checks on `75320e9`: `ruff check .` all checks passed; `ruff format --check .` 623 files already formatted; `mypy src/` no issues in 107 source files; `pytest -q` 4055 passed, 262 deselected, 17 xfailed; forbidden-terms scan exit 1; no absolute home paths.
- 2026-10-27: second security pass (independent reviewer, probes from the scratchpad): B1, S2, S3, S4 and N1 confirmed fixed; S1 partly fixed, because the redaction regex was anchored at `^/v1/files/` and missed paths behind `--root-path`. Fixed by the parent: the pattern is no longer anchored and keeps the query string; new test `test_loggable_path_redacts_the_token_under_any_root_path` (RED: 2 failed, GREEN). Adapter tests: 171 passed. Accepted residuals, documented: a symlinked parent directory is still followed (hooks resolve paths under a root they own); a disconnected download's handle closes on garbage collection; a user who pastes a live link back hands that token to the model.
- 2026-10-27: T3 done (delegated verification) on 6367656, `claude-haiku-5-5`, `EVAL_RUNS=3`: `tests/test_live_eval_phase1.py` 3 passed (`sql_query` 3/3); `tests/test_live_smoke.py` 2 passed; `tests/test_live_eval_composition.py` 3 passed, 11 scenarios at 3/3. The later commit 6492284 changes only the log-redaction pattern. Full suite on 6492284: `pytest -q` 4059 passed, 262 deselected, 17 xfailed; `mypy src/` clean; forbidden-terms exit 1.
- Next: push and PR, based on #321.
