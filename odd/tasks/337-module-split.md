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

- [ ] T0.1 — S5, S6 — inline — structure guards (`tests/test_structure_guards.py`)
- [ ] T0.2 — S7 — inline — characterization tests
- [ ] T1.1 … T1.5 — S8 — planned in #334
- [ ] T2.1 … T2.5 — S9 — planned in #335
- [ ] T3.1 … T3.6 — S10 — planned in #336

## Log

- L1 (2026-11-12, owner): "primero creo que tener estos archivos de mas de 2mil lineas es un error de diseño importante. Esto implica un refactor  que fixee los problemas que presentas y ademas aplique criterios altos de diseño de codigo y arquitectura. Arma un plan para aplicar estas correcciones"
- L2 (2026-11-12, owner, after the plan): "perfecto, escribi el plan en un documento. Y arranca"
- L3 (2026-11-12): an independent read measured the three modules and found that tests patch names by module path 207 times on `main`, 17 on `loader` and 15 on `graph`; a move with a plain re-export would leave those tests passing while testing nothing. Hence S3 and the patch-target guard in S6.
- L4 (2026-11-12): layering contracts are written as plain AST tests instead of adding `import-linter`, to avoid a new dependency.
- L5 (2026-11-12): open PRs that touch the three modules (#323, #317, and the local audio branch for #320) should merge before the phase that splits the module they touch, or they will conflict.
