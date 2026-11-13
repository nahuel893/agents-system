# 337 — Split the three oversized modules: loader, graph and main

Objective: put boundaries inside `harness/loader.py`, `agent/graph.py` and `main.py` so no module holds a whole layer, without changing observable behavior except in the fixes named below.
Issues: #337 (umbrella), #333 (phase 0), #334 (phase 1), #335 (phase 2), #336 (phase 3). Branch for phase 0: `test/333-structure-guards` from 829a399.
Diagnosis and owner-facing plan (Spanish): `docs/architecture_es/revision-arquitectura-2026-11.md` (problem P1) and `docs/architecture_es/plan-refactor-modulos.md`.
Delivery strategy: `stacked-to-main`, one PR per task. Test runner: `uv run pytest -q`.

## Specs

- S1. "Un PR mueve o arregla, nunca las dos cosas." Move PRs change no behavior and are verified by the same suite before and after.
- S2. "Los nombres públicos conservan su ruta de import." `loader.py`, `graph.py` and `main.py` stay as thin modules that re-export the public names.
- S3. "Los privados no se re-exportan." Each move migrates its imports and its test patches in the same PR.
- S4. "Dependencias explícitas." What a module needs arrives as an argument, not through a global bag or a patch.
- S5. "Tope de tamaño verificado en CI: 500 líneas por módulo, con una lista de excepciones que solo puede achicarse."
- S6. Phase 0.1, structure guards as tests, no new dependency: the size cap (S5); contracts stating which packages may import which, today's violations recorded as exceptions that can only shrink; a patch-target guard that fails when a test patches, by module path, a name the target module binds but never uses (a re-export), or does not bind at all.
- S7. Phase 0.2, characterization tests: the resolved definition and final prompt of every predefined role and reference agent; the set of `app.state` attributes after boot.
- S8. Phase 1 (#334): `loader.py` into `harness/definition/`; one constructor for `AgentDefinition`; a public home for the 14 private names imported from other modules. Open owner decision: `merge()` gains the base prompt contract, or leaves the public API.
- S9. Phase 2 (#335): `graph.py` into `usage.py`, `limits.py`, `thread.py`, `nodes/`, `builder.py`, `runtime.py`; an explicit turn context so the graph is built once per runtime; remove `self._runtime`, `AgentState.current_permissions` and the second schema build.
- S10. Phase 3 (#336): `main.py` into a `server/` package; a typed services object instead of 31 `app.state` attributes; settings, engine and chat model injected instead of patched.
- S11. Verification per PR: `ruff check .`, `ruff format --check .`, `mypy src/`, `pytest -q` with the same test count before and after a move; live modules per the `AGENTS.md` table. Phase 0 is tests-only and exempt from live runs.

## Tasks

- [x] T0.1 — S5, S6 — inline — structure guards (`tests/test_structure_guards.py`) — 850e96f, plus the unreadable-target guard in the follow-up commit
- [x] T0.2 — S7 — inline — characterization tests (`tests/test_module_split_characterization.py`, `tests/snapshots/module_split.json`) — 8e48928
- [x] T1.1 — S8 — inline — `harness/definition/` with `errors.py`, `roots.py`, `model.py` and `command_tools.py` moved out of `loader.py` (4,031 to 3,312 lines) — 39dd667
- [ ] T1.2 … T1.5 — S8 — planned in #334
- [ ] T2.1 … T2.5 — S9 — planned in #335
- [ ] T3.1 … T3.6 — S10 — planned in #336

## Log

- L1 (2026-11-12, owner): "primero creo que tener estos archivos de mas de 2mil lineas es un error de diseño importante. Esto implica un refactor  que fixee los problemas que presentas y ademas aplique criterios altos de diseño de codigo y arquitectura. Arma un plan para aplicar estas correcciones"
- L2 (2026-11-12, owner, after the plan): "perfecto, escribi el plan en un documento. Y arranca"
- L3 (2026-11-12): an independent read measured the three modules and found that tests patch names by module path 207 times on `main`, 17 on `loader` and 15 on `graph`; a move with a plain re-export would leave those tests passing while testing nothing. Hence S3 and the patch-target guard in S6.
- L4 (2026-11-12): layering contracts are written as plain AST tests instead of adding `import-linter`, to avoid a new dependency.
- L5 (2026-11-12): open PRs that touch the three modules (#323, #317, and the local audio branch for #320) should merge before the phase that splits the module they touch, or they will conflict.
- L6 (2026-11-12): T0.1 and T0.2 done. The patch-target guard reads 491 patch sites and finds no bad one today; 7 layering violations and 19 oversized modules are listed. The snapshot pins 10 predefined roles (the abstract `base` by its refusal), 8 reference agents, and `app.state` with 24 attributes after `create_app` and 31 inside the lifespan. Full offline suite on 850e96f plus the guards: 4015 passed, 17 xfailed.
- L7 (2026-11-12): native review of 829a399..8e48928, reliability lens, owner consent granted: approved and acknowledged, six non-blocking findings. Taken in a follow-up commit: a guard for patch targets that cannot be read statically (4 listed, all in `tests/test_channel_port.py`) and a `TypeError` instead of `repr()` for an unknown type in the snapshot. Left as later work: exact size ceilings (the round ceilings leave room for the open PRs that touch these modules; tighten each one when its phase starts), environment isolation of the `app.state` test, pruning stale snapshot keys on regeneration, and star-import and `__getattr__` bindings in `binding_of`.
- L8 (2026-11-13): #338 merged phase 0 into `main`; #339 added the two Spanish planning documents.
- L9 (2026-11-13): T1.1 changed order against the plan. The command-tool block cannot leave `loader.py` first: it uses `RawDefinition`, `CommandToolDeclaration` and `DefinitionError`, and `loader.py` would import it back. So the leaves went first, in dependency order: `errors.py`, then `roots.py` with `RootConfig`, then `model.py`, then `command_tools.py`. `frontmatter.py`, `fsread.py` and `prompt.py` follow in T1.2. `loader.py` now declares `__all__`: under `mypy --strict` an imported name is only re-exported when listed, so public names keep their import path and a private one must be imported from the module that defines it. Moved with the code: the test patches on the root constants, one `shutil.which` patch and the imports of two private command-tool functions. The path constants in `roots.py` gained one hop, since the file sits one directory deeper. Offline on the result: ruff and format clean, `mypy src/` clean on 111 files, `pytest -q` 4039 passed and 17 xfailed, the same count as before the move, with the characterization snapshot unchanged. The loader ceiling in the size guard went from 4100 to 3400.
- L10 (2026-11-13): live run of `tests/test_live_eval_phase4.py` not obtained. The reference provider answers 401 to the configured key, and the module fails the same way on `main`, so the failure says nothing about this change. Pending until a working key is configured.
- L11 (2026-11-13): native review of 39dd667, readability and reliability lenses, owner consent granted: approved and acknowledged, five non-blocking findings. Taken in a follow-up: a test that pins the root paths to the package directory, the stale "below" reference in `model.py`, the ADR-002 mention of where the command-tool types live, and the hash on T1.1. Left as is: private names imported by `loader.py` stay reachable as its attributes, which is how Python imports work; `__all__` and the patch-target guard are what keep them from being used through it.
