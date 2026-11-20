# Plan de refactor: partir `loader.py`, `graph.py` y `main.py`

Plan para resolver el problema P1 de [la revisión de arquitectura](revision-arquitectura-2026-11.md#p1--tres-archivos-gigantes-concentran-todo). Reemplaza a la mejora M4 de ese documento, que suponía que bastaba con mover código y re-exportar; la verificación mostró que no alcanza.

**Dónde está cada cosa.**

- Diagnóstico y números medidos: [revisión de arquitectura, P1](revision-arquitectura-2026-11.md#p1--tres-archivos-gigantes-concentran-todo).
- Seguimiento: issue paraguas `#337 — Split the three oversized modules: loader, graph and main`, con una fase por issue (`#333`, `#334`, `#335`, `#336`).
- Bitácora de trabajo, en inglés: `odd/tasks/337-module-split.md` (specs, tareas y log con las decisiones).

**Objetivo.** Que ningún módulo concentre una capa entera: fronteras explícitas, dependencias declaradas y un único lugar para cada decisión. El comportamiento observable no cambia, salvo en los arreglos marcados.

## Reglas

- **Un PR mueve o arregla, nunca las dos cosas.** Los PRs de movimiento no cambian comportamiento y se verifican con la misma suite antes y después.
- **Los nombres públicos conservan su ruta de import.** `loader.py`, `graph.py` y `main.py` quedan como fachadas finas que re-exportan lo público.
- **Los privados no se re-exportan.** Cada mudanza migra sus imports y sus parches de tests en el mismo PR. Si un parche apuntara a un nombre re-exportado, el test pasaría sin probar nada.
- **Dependencias explícitas.** Lo que un módulo necesita le llega por argumento, no por una bolsa global ni por un parche.
- **Tope de tamaño verificado en CI:** 500 líneas por módulo, con una lista de excepciones que solo puede achicarse.
- **Sin dependencias nuevas** salvo necesidad de corrección. Las guardas de estructura se escriben como tests propios.
- **Sin features.** Los cambios de API (`Deployment`, políticas tipadas) van después y se apoyan en este refactor.

## Estado

| Fase | Qué | PRs | Estado |
|---|---|---|---|
| 0 (`#333`) | Red de seguridad | 1 | Lista, integrada en `main` con el PR #338 |
| 1 (`#334`) | `loader.py` → `harness/definition/` | 5 | Movimientos integrados (#340, #341, #344). Falta el arreglo 1.4 |
| 2 (`#335`) | `graph.py` → módulos de `agent/` | 5 | Movimientos integrados (#342). Faltan los arreglos 2.4 y 2.5 |
| 3 (`#336`) | `main.py` → paquete `serving/` | 6 | Movimientos de funciones auxiliares integrados (#343). Faltan los arreglos 3.1 y 3.2, y la división de `lifespan` y `create_app` |

### Avance al 2026-11-20

| Módulo | Antes | Ahora |
|---|---|---|
| `harness/loader.py` | 4.031 líneas | 282 |
| `agent/graph.py` | 2.047 | 51 |
| `main.py` | 2.079 | 1.156 |

Lo que cambió respecto del plan original:

- **Orden de la fase 1.** El bloque de comandos no podía salir primero: usa el modelo de datos. Salieron antes `errors.py`, `roots.py` y `model.py`. El paquete quedó en 17 módulos, ninguno sobre 500 líneas, y `resolve` se queda en `loader.py`, porque los tests lo parchean en esa ruta.
- **Nombre del paquete de la fase 3.** Es `serving/` y no `server/`: `agents_system/server.py` ya existe y es una ruta de import pública.
- **Lo que un movimiento no puede dividir.** `AgentRuntime` es una clase de unas 820 líneas; `lifespan` y `create_app` son dos funciones que suman unas 980. Quedan enteras en `agent/runtime.py` (techo 1.050) y `main.py` (techo 1.200) hasta los arreglos de cada fase.
- **Arreglo 1.5, parcial.** Trece nombres que otros paquetes importan ya son públicos. Quedan dos, `_effective_limits` y `_build_chat_model`: cada uno choca con un nombre que ya existe y necesita un nombre nuevo.
- **Una guarda que no alcanza.** La guarda de destino de parches no detecta este caso: un test parchea un nombre en el módulo viejo, el módulo viejo todavía lo usa, pero la función que el test quería interceptar se mudó y lee el nombre en otro módulo. Una lectura independiente encontró un test así; se corrigió en #343.

Pendiente, todo cambios de diseño o de comportamiento:

- **1.4** Constructor único de `AgentDefinition`. Espera la decisión sobre `merge()`.
- **2.4 y 2.5** `RuntimeOptions` y el `TurnContext` explícito. Toca concurrencia y el chequeo de permisos.
- **3.1 y 3.2** `AppServices` e inyección de settings, engine y modelo de chat; después, dividir `lifespan` y `create_app`.

## Fase 0 — Red de seguridad

- [x] **0.1 Guardas automáticas.** Tres tests de estructura:
  - tope de líneas por módulo, con lista de excepciones;
  - contratos de capas: qué paquete puede importar a cuál;
  - destino de los parches: falla si un test parchea, por ruta de módulo, un nombre que ese módulo no define.
- [x] **0.2 Tests de caracterización** donde hoy falta red:
  - la definición resuelta de cada rol predefinido y agente de referencia, serializada y comparada;
  - el prompt final de cada uno;
  - los atributos de `app.state` tras el arranque.

## Fase 1 — `loader.py` → paquete `harness/definition/`

Tiene 17 parches de tests encima; sirve para fijar el método.

- [ ] **1.1 Piezas sueltas:** `command_tools.py` (DSL de comandos), `fsread.py` (lectura acotada), `prompt.py`, `frontmatter.py`.
- [ ] **1.2 Modelo y rutas:** `model.py`, `roots.py`, `locator.py`.
- [ ] **1.3 Núcleo enredado, junto:** `directives.py`, `validators.py`, `fold.py`, `merge.py`, `resolve.py`.
- [ ] **1.4 Arreglo:** un único constructor de `AgentDefinition`, para que `merge()` y `resolve` devuelvan el mismo prompt. **Decisión pendiente del owner:** `merge()` pasa a incluir el contrato base, o sale de la API pública.
- [ ] **1.5 Arreglo:** API pública para los 14 nombres que hoy `factory.py`, `spec.py` y `main.py` importan como privados.

## Fase 2 — `graph.py`

- [ ] **2.1** `usage.py` y `limits.py`; `_effective_limits` pasa a ser público, porque `main.py` lo usa.
- [ ] **2.2** `thread.py`, con el estado `_ABANDONED_CALLS` y la política de timeout del checkpointer.
- [ ] **2.3** `nodes/model.py`, `nodes/tools.py` y `builder.py`.
- [ ] **2.4** `runtime.py` con `AgentRuntime`; los 14 parámetros pasan a un `RuntimeOptions`, manteniendo la firma actual.
- [ ] **2.5 Arreglo:** un `TurnContext` explícito con los datos del turno, para construir y compilar el grafo una vez por runtime. Se eliminan `self._runtime`, `current_permissions` y el segundo cálculo de esquemas. Toca concurrencia: va solo y con verificación independiente.

## Fase 3 — `main.py` → paquete `server/`

Va última: tiene 207 parches. Los dos primeros PRs crean las costuras; después se mueve. Conviene quitar Redis antes (parte de M1): elimina 52 parches que si no habría que migrar.

- [ ] **3.1 Arreglo:** un objeto tipado `AppServices` en lugar de los 31 atributos sueltos de `app.state`.
- [ ] **3.2 Arreglo:** inyectar settings, engine y modelo de chat en vez de parchearlos (124 de los 207 parches).
- [ ] **3.3** `runtimes.py`: el armado de runtimes sale del lifespan.
- [ ] **3.4** `registrations.py`, `boot_plan.py`, `channels.py`, `stores.py`.
- [ ] **3.5** `health.py` y `auth.py` como routers, en lugar de closures dentro de `create_app`.
- [ ] **3.6** `lifespan.py` y `app.py`; `main.py` queda como fachada.

## Verificación por PR

- **Offline:** `ruff check .`, `ruff format --check .`, `mypy src/`, `pytest -q`, con el mismo conteo de tests antes y después.
- **Live**, según la tabla de `AGENTS.md`:
  - fase 1: `tests/test_live_eval_phase4.py`;
  - fase 2: suite de mecanismos, `tests/test_live_eval_capabilities.py` y `tests/test_live_eval_guardrails.py`;
  - fase 3: `tests/test_live_eval_phase1.py` y `tests/test_live_smoke.py`.
- La fase 0 es solo tests: exenta de live.

## Fuera de alcance

- El objeto `Deployment` (M3) y las políticas tipadas (M5).
- Reagrupar `services/` y mover el código de referencia (M6).
