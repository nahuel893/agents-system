# Revisión de arquitectura — noviembre de 2026

Documento de referencia para ir resolviendo punto por punto. Documentos relacionados: [criterios de arquitectura de agentes](criterios-arquitectura-agentes.md) y [plan de refactor de módulos](plan-refactor-modulos.md). Cada punto tiene un identificador estable (`P1`…`P9` para los problemas, `M1`…`M6` para las mejoras) para poder citarlo en issues y PRs.

**Alcance y método.** Análisis de lectura sobre `main` en `829a399`. No se ejecutó nada. Se verificaron a mano tres hallazgos: el import de `Tier` en `permissions/base.py:12`, la llamada a `setup_logging()` en `main.py:1852` y los 19 argumentos de `create_app`. El resto surge de la lectura del código; la causa de la fragilidad de orden en los tests (P3) y la pérdida de eventos de auditoría (P4) son inferencias sin reproducir. Los números de línea corresponden a ese commit.

## Estado

| Mejora | Qué | Rompe compatibilidad | Estado |
|---|---|---|---|
| M1 | Arreglos chicos y seguros | No | Pendiente |
| M2 | Renovación del lease y apagado ordenado del worker | No | Pendiente |
| M3 | Objeto `Deployment` con un `RuntimeSpec` por runtime | No (se agrega al lado) | Pendiente |
| M4 | Partir `loader.py`, `main.py` y `graph.py`. Plan: [plan-refactor-modulos.md](plan-refactor-modulos.md), issue `#337` | No en lo público; los privados se migran | En curso |
| M5 | Tipar las políticas y exportar los tipos de turno | Sí | Pendiente |
| M6 | Sacar del paquete el código de dominio y el harness live | Sí (con deprecación) | Pendiente |

## Lo que está bien y no se toca

- **Permisos en dos capas con una sola regla.** La capa que arma las herramientas y la que intercepta cada llamada usan el mismo predicado, y el techo de permisos del deployment impide que la segunda se abra de más.
- **Arranque que falla cerrado.** Una configuración insegura se rechaza antes de servir nada.
- **Outbox bien diseñado.** Acepta cada mensaje una sola vez, procesa cada conversación en orden y guarda la respuesta antes de enviarla.
- **Evals honestos.** Un guardrail que nunca se ejercitó cuenta como "no prueba nada", no como aprobado.
- **Valores inmutables** (`Agent`, `AgentDefinition`, `EquippedRuntime`), métricas inyectables, exports perezosos en la raíz y extras opcionales con imports prolijos.
- **Guard de SQL en capas.**

## Problemas, por impacto

### P1 — Tres archivos gigantes concentran todo

Verificado el 2026-11-12 por una segunda lectura independiente, con conteos medidos. Los números de abajo reemplazan a los del primer análisis.

**`harness/loader.py` — 4.031 líneas, unos 15 bloques de responsabilidad.**

- Bloques: raíces y rutas (72-152), constantes de autonomía y límites (154-190), modelo de datos (192-467), frontmatter (469-650), DSL de herramientas de comando (652-1100), armado del prompt (1158-1296), localizador y `extends` (1298-1350), lectura acotada de archivos (1353-1594), carga de archivos del rol (1595-2034), plegado de la cadena (2035-2470), `load_generic`/`load_override` (2471-2662), directivas (2664-2901), validadores y techo del importador (2903-3611), `merge` (3613-3870) y `resolve` (3873-4031).
- Separables sin esfuerzo: el DSL de comandos (dos puntos de entrada, en 1955 y 3955), el armado del prompt y la lectura de archivos.
- Enredados: las directivas y los validadores, que comparten el plegado, el techo del importador y el merge. `_as_str_list` tiene 16 usos.
- `AgentDefinition` se construye en dos lugares: `loader.py:3844` (`_merge_validated`) y `loader.py:4008` (`resolve`). El camino sin override corre menos validaciones, lo cual es legítimo porque no hay contra qué comparar, pero el armado de campos está duplicado.
- El `merge()` público devuelve una definición **sin** el contrato base del prompt; solo `resolve` lo agrega (3973-3976). Llamar `merge()` directo da un prompt distinto.
- Privados importados desde afuera: `harness/factory.py:55-67` importa 7, `agent/spec.py:29-41` importa 6 y `main.py:65` importa 1.

**`main.py` — 2.079 líneas.**

- `create_app` (1711-2079) recibe 21 argumentos. Las rutas de health, ready y metrics son closures anidadas dentro (1933-2077), así que no se pueden mover sin cambiar código.
- El lifespan (338-951) ocupa unas 614 líneas; unas 390 son armado de runtimes escrito en línea (456-845).
- `app.state` funciona como localizador de servicios: 31 atributos distintos, escritos entre el lifespan y `create_app`, y leídos también desde `openai_adapter.py`, `operator_api.py`, `webhook.py` y `evals/live_harness.py`.

**`agent/graph.py` — 2.047 líneas.**

- `AgentRuntime.__init__` recibe 14 parámetros. `_run_turn_scoped` solo ocupa unas 237 líneas.
- `self._runtime` (1373) está muerto: nunca se lee, y `self._equipped` guarda el mismo objeto.
- `AgentState.current_permissions` se escribe en el estado inicial (1791) pero ningún nodo lo lee; los permisos llegan al nodo de herramientas por closure.
- Los esquemas de herramientas se calculan en `__init__` (1377) y otra vez en cada turno (919). El segundo cálculo es redundante.
- El grafo se reconstruye en cada turno (`_build_graph`, 893) y se compila hasta dos veces por turno cuando hay hilo (1773 y 1824). Se reconstruye porque los nodos capturan valores del turno: permisos efectivos, control de presupuesto, texto no confiable y contexto confiable.

**Riesgo para partir los archivos (afecta a M4).**

- Los tests parchean nombres por ruta de módulo: 207 referencias a `agents_system.main.*` en 13 archivos, 17 a `harness.loader` y 15 a `agent.graph`, más parches directos sobre el objeto módulo.
- Si una función se muda y el módulo viejo solo la re-exporta, esos parches cambian el nombre viejo mientras el código mudado sigue llamando al nuevo: los tests pasarían sin probar nada. Un movimiento puro con re-export **no** es seguro; cada mudanza tiene que migrar sus parches.
- Otros obstáculos: imports diferidos que rompen ciclos reales (`loader.py:704, 3089, 3632, 3937`), el estado mutable `_ABANDONED_CALLS` (`graph.py:968`), y `main.py` importando el privado `_effective_limits` de `graph.py`.

### P2 — Configurar el server es difícil de razonar

- `create_app` (`main.py:1707-2054`) recibe 19 argumentos. Siete son diccionarios separados por id de runtime: `grants`, `clients`, `budgets`, `execution_limits`, `timeout_reply`, `tool_error_reply` y `outbox_retry`. La configuración de un runtime queda repartida en siete lugares.
- Cada uno tiene además su variable de entorno (`AGENT_REGISTRATIONS`, `DEPLOY_GRANTS`, `USER_BUDGETS`, `EXECUTION_LIMITS`, `TURN_TIMEOUT_REPLY`, `TOOL_ERROR_REPLY`, `OUTBOX_*`), y la precedencia entre argumento y variable no es la misma en todos.
- `Settings` tiene unos 90 campos planos. Varios no se usan: `meta_phone_number_id`, `meta_access_token`, `slack_webhook_url`, `redis_url`. `meta_webhook_secret` se exige siempre, aunque no haya canal de WhatsApp.
- `REGISTRY_FACTORY` se lee con `os.getenv` en `server.py:124`, por fuera de `Settings`.
- Un server configurado solo por entorno no puede proveer `participant_directory`.
- La elección de backend se hace con `isinstance(scope_provider, InMemoryScopeProvider)` en cuatro lugares (`main.py:251, 270, 286, 327`).

### P3 — Estado global compartido

- Son globales del proceso: el registro de permisos (que además se muta al resolver una definición, `loader.py:682-707`), el destino de auditoría (`AuditSink`), `DEFAULT_METRICS`, `_pending_emits`, `_warned_no_audit_sink`, `_ABANDONED_CALLS`, la caché de `get_provider_settings` y `PLATFORM_DEFAULT_LIMITS`, que es público y mutable.
- Dos runtimes en un mismo proceso no pueden tener auditoría separada.
- `create_app` reconfigura structlog para todo el proceso (`main.py:1852`). Es la causa más probable de la fragilidad de orden en los tests: `tests/test_health.py:591-601` configura structlog contra un `StringIO` y no lo restaura, y `tests/test_agent_runtime.py:1318-1327, 2318-2326` verifican la salida de consola por defecto. Sin reproducir.
- Es el mismo origen del ruido de logs al usar la librería: sin configurar nada, structlog imprime en stdout.
- Los stores de Postgres registran singletons al importar el módulo.

### P4 — Eventos de auditoría que se pierden sin aviso

- `_emit` resuelve el recorder por nombre con `getattr` y, si no hay event loop, descarta el evento con un log a nivel debug (`harness/injector.py:54-55, 86-93`). Pasa, por ejemplo, al llamar `build_runtime` desde código síncrono. Los eventos descartados no se cuentan. Sin reproducir.
- El runner de evals reemplaza el destino global de auditoría mientras corre (`evals/runner.py:1229-1231`), así que no es seguro usarlo en paralelo ni dentro de un server.

### P5 — Cada deploy puede frenar conversaciones hasta 10 minutos

- El lease del outbox es fijo, de 600 segundos y sin renovación.
- `DeferredWebhookWorker.stop()` cancela los turnos en curso de golpe. Como cada conversación se procesa en orden, queda esperando a que venza el lease.
- Si se pierde el lease, el turno se vuelve a ejecutar y sus herramientas vuelven a escribir. `send_key` nunca llega al adapter, así que no hay clave de idempotencia en el envío.
- El turno se registra antes de guardar la respuesta.
- La cola proactiva duplica la lógica de lease.

### P6 — El núcleo depende de cosas del server

- Ciclo de imports: `Tier` vive en `harness/registry.py:9` y `permissions/base.py:12` lo importa, lo que obliga a imports diferidos en varios archivos.
- El harness del núcleo importa conectores y servicios. `RegistryFactory(settings, embedder, bi_engine)` está en el núcleo.
- El contrato de una herramienta (`ToolSpec`) recibe una `AsyncSession` de SQLAlchemy.
- Las constantes del lease del outbox limitan los turnos de quien usa solo la librería (`execution_limits.py:26-37`).
- `ModelPrice` se exporta desde el `config` del server.
- Imports de privados entre módulos: `_emit`, y siete privados del loader usados en `spec.py`.
- Redis solo se usa para un ping de salud, pero está en el extra `server` y hace que `/ready` devuelva 503.
- El chequeo del proveedor del modelo también condiciona `/ready`: si el proveedor se cae, todas las réplicas quedan "no listas" y se corta la entrada de mensajes, que es lo que el outbox debía evitar.
- `channels/whatsapp.py` lanza `HTTPException` de FastAPI desde la capa de canal.

### P7 — Código que no debería ir en el paquete

- Van dentro de la librería un catálogo de reportes de ventas (`connectors/sales_reports.py`), backends de una empresa de demostración (`services/reference.py`) y un motor de warehouse (`medallion_*`).
- `connectors/operator.py` se incluye sin un extra que lo habilite.
- `evals/live_harness.py` va en el wheel pero necesita rutas que solo existen en el checkout (`_REPO_ROOT = parents[3]`).
- `services/` es un cajón con 25 módulos, y hay módulos por usuario en la raíz del paquete.

### P8 — API pública incompleta y poco tipada

- Las políticas (`memory_policy`, `execution_limits`, `model`) son `Mapping[str, Any]` y se vuelven a parsear en cada uso.
- El bucle de la regla R3 está duplicado en tres lugares (`injector.py:136-149`, `interceptor.py:183-196`, `graph.py:1602-1612`). Los límites pertenecen a tres módulos y el chequeo de presupuesto en USD se hace dos veces.
- No hay una excepción raíz de la librería.
- No se exportan tipos que `run_turn` devuelve o usa: `TurnResult`, `TurnUsage`, `UntrustedData`, `PolicyViolation`, `BudgetCaps`, `UsageLedger`, `ScopeProvider`, `UserMemoryLimits`.
- `harness` exporta `load_generic`, `load_override` y `merge`; un docstring enseña a usar `_to_locator()`; el parámetro `role_type` acepta un `Agent`; el constructor expone `_folder`.
- Los tipos de LangChain forman parte del contrato.
- No hay streaming ni puntos de extensión alrededor del modelo o las herramientas.
- Relacionado: `#325 — Library ergonomics: one-call agents, string and sync turns, a @tool decorator, quiet logs`.

### P9 — Red de calidad más floja de lo que parece

- ruff corre sin `select`, solo con las reglas por defecto. No hay cobertura de ramas, y el umbral de 94 % de líneas cubre solo tests unitarios.
- Los docs en inglés y español divergieron: `deployment.md` tiene 21 títulos en inglés y 31 en español; `interceptor.md` y `analysis.md` no tienen gemelo. Nada verifica la paridad.
- Los evals usan 3 corridas contra un umbral de 0,8: un solo fallo de muestreo tumba el gate. El chequeo `audit_event` depende de polling.
- 30 archivos de test definen su propio modelo de chat falso; 42 tocan privados; hay tests nombrados por número de issue.
- No hay `pytest-timeout`. La CI instala torch 10 veces, prueba solo Python 3.12 y no corre ni el smoke del wheel ni pre-commit.
- `AGENTS.md:70` quedó desactualizado. El pin de ruff del extra `dev` entra en conflicto.
- No hay OpenTelemetry.

### Otros hallazgos del server, sin priorizar

- El bypass de RLS es un ajuste de sesión.
- Hay una única clave compartida para `/v1`.
- El motor de BI se conecta aunque no se haya verificado que la conexión es de solo lectura.
- Los límites son por proceso, no por despliegue.
- El scheduler y el dispatcher proactivo no arrancan con la app servida (`#275`).
- `whatsapp_checkpointer_enabled` es global, no por runtime.
- El ADR-001 quedó desactualizado.

## Mejoras, en el orden propuesto

### M1 — Arreglos chicos y seguros

Resuelve parte de P3, P4 y P6. No rompe nada.

- [ ] Fixture `autouse` que restaure la configuración de structlog y limpie cachés entre tests; usar `capture_logs()`; sumar `pytest-randomly`.
- [ ] Hacer que `setup_logging` sea opt-in, para que la librería no imprima en stdout por defecto.
- [ ] Contar y avisar los eventos de auditoría perdidos.
- [ ] Mover `Tier` a un módulo sin dependencias para romper el ciclo.
- [ ] Sacar Redis y el chequeo del proveedor del gate de `/ready`.
- [ ] Reemplazar el swap global del `AuditSink` en el runner de evals por un override con `ContextVar`.

### M2 — Renovación del lease y apagado ordenado del worker

Resuelve P5. Es el único punto que afecta a usuarios finales en cada deploy.

- [ ] Heartbeat que renueve el lease mientras el turno sigue vivo.
- [ ] `stop()` que espere a los turnos en curso antes de cancelar.
- [ ] Pasar `send_key` al adapter como clave de idempotencia.
- [ ] Unificar la lógica de lease de la cola proactiva con la del outbox.

### M3 — Objeto `Deployment` con un `RuntimeSpec` por runtime

Resuelve P2. Se agrega al lado de la firma actual de `create_app`.

- [ ] `Deployment` y `RuntimeSpec` congelados, que reemplacen los 19 argumentos y los siete diccionarios.
- [ ] `Deployment.from_settings`: las variables de entorno pasan a ser solo otra forma de construirlo, con una única regla de precedencia.
- [ ] `Settings` anidado por área; eliminar los campos sin uso.
- [ ] Puerto `PersistenceBackend` en lugar de los `isinstance`.

### M4 — Partir `loader.py`, `main.py` y `graph.py`

Resuelve P1. El plan detallado y su estado están en [plan-refactor-modulos.md](plan-refactor-modulos.md); el seguimiento, en el issue `#337`. La lista de abajo es el planteo original y quedó reemplazada por ese plan.

- [ ] `harness/definition/` como paquete, con un único `_finalize` que construya `AgentDefinition`.
- [ ] Del grafo: `agent/usage.py`, `thread.py`, `nodes.py`, `budget.py` y un `RuntimeOptions`.
- [ ] Del server: `server/boot_plan.py`, `runtimes.py`, `health.py`, `auth.py`, `lifespan.py`.
- [ ] Reagrupar `services/` en `delivery/`, `enduser/`, `ports/`, `sql/`, `retrieval/`.

### M5 — Tipar las políticas y exportar los tipos de turno

Resuelve P8, junto con `#325`. Rompe a quien lea las políticas como diccionario.

- [ ] Dataclasses tipadas para las políticas, parseadas una sola vez.
- [ ] Un único bucle para R3 y un único dueño de los límites.
- [ ] `AgentsSystemError` como excepción raíz.
- [ ] Exportar los tipos de turno; renombrar `role_type` a `agent`.
- [ ] `AuditEvent` tipado; registros inyectables en lugar de globales.
- [ ] `ToolContext` en lugar de la `AsyncSession` en el contrato de herramientas.
- [ ] `stream_turn`, y puntos de extensión `ToolMiddleware` y `ModelHook`.

### M6 — Sacar del paquete el código de dominio y el harness live

Resuelve P7. Rompe rutas de import; va con aviso de deprecación.

- [ ] Mover el código de referencia a `examples/`.
- [ ] Sacar `evals/live_harness.py` del wheel o hacerlo independiente del checkout.
- [ ] Poner `connectors/operator.py` detrás de un extra.

### Calidad, en paralelo con lo anterior

Resuelve P9. Ninguno rompe compatibilidad.

- [ ] `select` explícito en ruff y cobertura de ramas.
- [ ] Test de paridad entre docs EN y ES.
- [ ] Evals con al menos 5 corridas e intervalo de Wilson en lugar del umbral fijo.
- [ ] Un modelo de chat falso compartido para los tests.
- [ ] `pytest-timeout`; smoke del wheel y pre-commit en la CI; caché de torch; más de una versión de Python.
