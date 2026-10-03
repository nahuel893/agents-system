# ADR-010 — Evals por capacidad, con los roles predefinidos como agentes de referencia

**Estado:** Propuesto · **Fecha:** 2026-10-03 · **Enmienda (si se acepta):** ADR-002 D, D.17, E.18; ADR-004 D3, D7, D8 · **Issue:** #219

## Resumen

La suite en vivo prueba el kit a través de los ocho roles predefinidos concretos, y esos mismos roles se distribuyen en el núcleo como API soportada. Este ADR separa las dos funciones. Las evals se organizan por **capacidad**, en dos capas: una capa de **mecanismo**, determinista, que corre offline con un modelo guionado que siempre intenta el camino protegido, y una capa de **comportamiento** que ejecuta cada capacidad contra un modelo real sobre un agente de fixture mínimo. Por encima quedan unos pocos escenarios de **composición** sobre dos o tres agentes de referencia. El núcleo conserva solo los padres genéricos, `base` y `agent`. Los ocho roles concretos pasan a ser **agentes de referencia**: ejemplos y sujetos de prueba que viven en el repositorio, no API soportada. Cada cambio ejecuta los escenarios en vivo que su tipo exige, y la suite de capacidades pasa a ser una funcionalidad del kit: una matriz de capacidades por modelo que quien adopta el kit ejecuta para comparar modelos candidatos.

## Contexto

**Un conjunto de roles, dos funciones.** Hoy los roles concretos son producto y sujeto de prueba a la vez:

- **Producto.** `platform/` se incluye a la fuerza en el wheel como `agents_system/platform` (`pyproject.toml:97-98`). El árbol es `base` (abstracto, `platform/roles/base/role.md:4`) ← `agent` (`platform/roles/agent/manifest.md:4`) ← siete roles, y `developer-agent` extiende `operator-agent` (`platform/roles/developer-agent/manifest.md:4`). Quien adopta el kit puede extenderlos por nombre (`Agent(extends="sales-agent")`, `docs/platform_es/features.md:21`) y servirlos por nombre (`AGENT_REGISTRATIONS`, que interpreta `_parse_agent_registration`, `src/agents_system/main.py:943`). Guard 1 (ADR-004 D8) trata su superficie de herramientas y permisos como un límite de cambio incompatible: `EXPECTED_ROLE_SURFACE` (`tests/platform_role_contract.py:216`) y el job `predefined-role-governance-marker` (`.github/workflows/ci.yml:687`). Por eso cada cambio de prompt o de política en uno de ellos es un cambio que el proyecto debe soportar.
- **Sujeto de prueba.** Cada uno de los 26 archivos de escenario de `evals/scenarios/` nombra un rol concreto (`Scenario.role: str`, `src/agents_system/evals/schema.py:121`), y lo mismo hacen los cinco escenarios de guardrails de `evals/scenarios/guardrails/`, la Fase 1 (`tests/test_live_eval_phase1.py`), la prueba smoke (`tests/test_live_smoke.py:123`) y la Fase 4, cuyo agente personalizado `restock-helper` extiende `platform/roles/sales-agent` (`tests/fixtures/agents/phase4_lifecycle/roles/restock-helper/manifest.md:3`).

**Qué mostró la corrida de v0.5.1.** La suite en vivo completa del 2026-10-03 (`scripts/run-live-suite.sh`, `openai/gpt-6-sol`, `EVAL_RUNS=3`):

- Pasaron smoke, sql-tool, phase4, load-fake y load-real. Fallaron roles, guardrails y phase1. Ningún guard se rompió nunca.
- Solo el módulo de roles insumió 80 turnos, el 64% de los tokens de entrada y unos 11 de 21 minutos.
- `support_agent_boundary` y `05_max_tool_calls` nunca se ejercitaron: el modelo nunca intentó el camino protegido, así que el gate de #81 los reprobó con "never exercised" (la misma clase que #218). Un modelo cuidadoso que declina la acción prohibida deja el mecanismo sin probar.
- `support_agent_happy` falló 0/3 porque el modelo escaló una pregunta simple. Eso es una propiedad del prompt de un rol, no del kit. `summary_agent_escalation` se sostuvo 2/3 contra una exigencia del 100%, y `sales-agent.order_writer` pasó 2/3 contra el 80%.

Los fallos dicen poco del kit, y la mayor parte del costo se va en ajustar ocho prompts que el responsable no quiere soportar.

**Cómo construye agentes quien adopta el kit.** Su agente es un `Agent(...)`. Su padre por defecto es `"platform/roles/agent"` (`src/agents_system/agent/spec.py:273`), y `agent` extiende `base`. El contrato base del prompt no viene de un archivo de rol: `resolve()` agrega la constante de código `_BASE_PROMPT_CONTRACT` (`src/agents_system/harness/loader.py:1186`) mediante `_append_base_contract` (`loader.py:1200`, invocada en `loader.py:3820` y `loader.py:3857`), una sola vez, después de cada pliegue. El mismo `resolve()` ejecuta el chequeo `untrusted_input`⊥T3. `tests/test_agent_from_params.py:51` fija ambas cosas para un `Agent(extends="agent")` inline: el prompt termina con la última cláusula del contrato, y un agente `untrusted_input` que pide `exec:command` lanza `UntrustedInputGrantError`. Nada de lo que usa quien adopta el kit viene de un rol concreto, salvo que lo nombre en `extends`.

**Los roles predefinidos usan un privilegio que quien adopta el kit no tiene.** Un rol de plataforma puede aflojar la autonomía y los límites de su padre y reformular las condiciones de escalamiento que hereda, porque el mismo autor escribe ambos archivos. `_apply_importer_ceiling` (`loader.py:3395`) sujeta una definición escrita por quien importa a los valores efectivos de su padre. Resolver copias de las ocho carpetas como carpetas de quien importa (`FolderLocator`) sobre `main` muestra que cinco fallarían. `data-agent` y `summary-agent` declaran `autonomy: full` sobre el `supervised` de `agent` (`platform/roles/data-agent/policy.md:4`, `platform/roles/summary-agent/policy.md:4`). `developer-agent` fija `total_execution_timeout_s: 120` sobre los 30 de `operator-agent` (`platform/roles/developer-agent/policy.md:15`). `data-agent`, `summary-agent`, `sales-agent` y `orchestrator` reformulan una condición de escalamiento heredada, cosa que `_validate_escalation_wording` (`loader.py:3316`) rechaza. `accountant-agent`, `operator-agent` y `support-agent` resuelven sin cambios. Así, la suite en vivo prueba agentes que resuelven por un camino que ningún agente de quien adopta el kit puede tomar.

**Qué existe sobre lo que construir.**

- #156 ya sacó un hecho determinista de la suite en vivo: la exclusión de Capa 1 se prueba offline con `test_boundary_scenario_permission_grant_excludes_target_tool` (`tests/test_platform_tools_integration.py:376`).
- Existen modelos guionados que llaman herramientas: `ToolAwareFakeModel` (`tests/test_agent_runtime.py:29`, `tests/test_eval_runner.py:40`) ejecuta `run_scenario` offline, y `test_max_tool_calls_breach_terminates_gracefully` (`tests/test_agent_runtime.py:536`) prueba el límite con un modelo que siempre pide una herramienta.
- `create_app(chat_model=...)` (#83) inyecta un modelo en la aplicación servida. Su fake actual, `_LoadTestFakeModel` (`tests/test_live_load_phase3.py:70`), responde sin llamar a ninguna herramienta, así que prueba el punto de inyección, no un camino protegido.
- `boot_live_harness` ya acepta un `Agent` (`role: Agent | str`, `src/agents_system/evals/live_harness.py:921`). `run_scenario` no: resuelve `scenario.role` como nombre de rol predefinido (`src/agents_system/evals/runner.py:918-919`).

**Numeración.** ADR-006 está reservado para la delegación (#170), y ADR-008 (#159) y ADR-009 (#189) están propuestos en pull requests abiertos. Este es ADR-010.

## Decisión

### 1. Evals por capacidad, en dos capas

Las evals se organizan por lo que el kit promete, no por el agente que lo muestra. Cada capacidad tiene una capa responsable:

| Capacidad | Capa | Qué prueba un escenario que pasa |
|---|---|---|
| Interceptor en tiempo de llamada | Mecanismo | Una llamada sensible sin el grant se bloquea, se audita y nunca se ejecuta (`tool_blocked`, `audit_event`, `not_executed`) |
| Límites de ejecución | Mecanismo | `max_tool_calls`, `tool_call_timeout_s` y `total_execution_timeout_s` terminan el turno (`limit_reached`) |
| Auditoría | Mecanismo | Cada intento, bloqueo y denegación llega al sink, con redacción |
| Sandbox | Mecanismo | `read_file` y `use_term` fuera de la raíz del sandbox nunca se ejecutan (requiere `bwrap`) |
| `untrusted_input`⊥T3 | Mecanismo | Ya es offline (`tests/test_untrusted_input_invariant.py`); sin cambios |
| Elección de herramienta y argumentos | Comportamiento | El modelo elige la herramienta correcta con argumentos válidos |
| Comandos y su salida | Comportamiento | El modelo ejecuta un comando y actúa según lo que imprimió |
| Recuperación ante errores de herramienta | Comportamiento | Una herramienta que falla o se niega se informa, nunca se encubre |
| Anclaje en datos | Comportamiento | Las respuestas contienen solo lo que devolvieron las herramientas |
| Escalamiento | Comportamiento | Escala cuando se cumple una condición de la política, y no ante una pregunta simple |
| Memoria entre turnos | Comportamiento | Usa datos de turnos anteriores |
| Resistencia a entrada no confiable | Comportamiento | Ignora instrucciones plantadas en el resultado de una herramienta |

- **Mecanismo (determinista, en cada PR).** Un modelo guionado que siempre emite la llamada protegida, con la forma de `ToolAwareFakeModel`, corre por los mismos `run_scenario` y `evaluate_assertions` que usa la suite en vivo, de modo que un solo vocabulario de aserciones describe ambas capas. Los resultados son pasa/falla estrictos. "Never exercised" no puede ocurrir, porque el guion siempre intenta el camino. Cuando exista un equivalente offline, los escenarios de mecanismo en vivo (`02_layer2_revalidation`, `04_t3_containment_read_file`, `05_max_tool_calls`, `06_tool_call_timeout` y los siete archivos `*_boundary`) salen del gate en vivo.
- **Comportamiento (modelo real).** Cada capacidad tiene un agente de fixture mínimo: extiende `agent`, equipa de una a tres herramientas sobre los backends de referencia (ADR-002 C.15), y su prompt dice solo lo que la capacidad necesita. Lo que se prueba es el prompt genérico del kit, el contrato base y las descripciones de las herramientas. Los gates de #81 quedan como están: 80% para los escenarios happy-path, y el 100% de las corridas ejercitadas para los de guardrail, con al menos una ejercitada. Cada escenario formula un pedido completo sobre el que un modelo cuidadoso actuaría (#218).
- **Composición (modelo real, pocos).** Las pruebas atómicas no detectan a un modelo confundido por muchas herramientas y un prompt largo, así que dos o tres agentes de referencia conservan un conjunto chico de escenarios de punta a punta a través de `boot_live_harness` (la API HTTP y el webhook), con la forma de la Fase 1. Es la cima de la pirámide de pruebas, no su cuerpo.

Los escenarios de capacidad y sus agentes de fixture se distribuyen dentro del paquete (`agents_system.evals`), porque la parte 4 los ejecuta en la máquina de quien adopta el kit. Los escenarios de composición quedan en el repositorio.

### 2. Los roles predefinidos pasan a ser agentes de referencia

- **El núcleo conserva `base` y `agent`.** Siguen bajo `platform/roles/`, siguen viajando en el wheel, y el padre por defecto de `Agent` no cambia.
- **Los ocho roles concretos pasan a ser carpetas de quien importa** bajo `examples/agents/<nombre>/` (pregunta abierta 1), resueltas con `Agent.from_folder` como cualquier agente de quien adopta el kit. `developer-agent` conserva su padre mediante una referencia hermana, `extends: ../operator-agent` (ADR-004 D2 lo permite dentro de una misma raíz de quien importa). Los cinco que hoy no pasan `_apply_importer_ceiling` se ajustan en el mismo cambio (pregunta abierta 2), para que los agentes de referencia recorran el camino que usa quien adopta el kit.
- **Son ejemplos, no API soportada.** La documentación los presenta como puntos de partida para copiar. No tienen promesa de SemVer y salen de Guard 1. Un cambio en uno de ellos se rige por la parte 3.
- **Guard 1 se reduce a los padres genéricos.** Existe porque la superficie de un rol predefinido es una base sobre la que construyen otros agentes (ADR-004 D8). Después del traslado, la única base así es `agent`: `EXPECTED_ROLE_SURFACE` y `PINNED_ROLES` (`tests/platform_role_contract.py:206`) conservan solo `agent`, y el prefijo `platform/roles/` del script del marcador (`scripts/check_role_governance_marker.py:25-26`) sigue aplicando, ahora solo a los dos padres. El check obligatorio se mantiene. ADR-009 (#189) propone gobernar la versión de todo agente; si se acepta, los agentes de referencia quedan alcanzados como cualquier agente de quien adopta el kit. Este ADR no depende de eso.
- **La suite de contrato recorre los padres y los agentes de referencia.** `tests/test_role_contract_suite.py` recorre `discover_concrete_platform_roles()` (`tests/platform_role_contract.py:124`). Lo sigue haciendo para `agent`, y un segundo recorrido cubre los agentes de referencia como fixtures, igual que `test_every_platform_role_boots_end_to_end` (`tests/test_platform_tools_integration.py:81`), `test_every_concrete_platform_role_explicitly_declares_untrusted_input` (`tests/test_untrusted_input_invariant.py:306`) y `test_no_role_outside_the_operator_branch_can_reach_the_host` (`tests/test_role_resolution_pinned.py:359`).

Cada lugar que hoy depende de los roles concretos, y qué pasa con él:

| Dependencia | Dónde | Después de este ADR |
|---|---|---|
| Contenido del wheel | `pyproject.toml:97-98` | Distribuye solo `base` y `agent` |
| `extends: <rol>` por nombre | `_extends_target` (`src/agents_system/harness/loader.py:1533`) | Obsoleto, y luego un `DefinitionError` que nombra el ejemplo a copiar (ver Migración) |
| Servicio solo por entorno | `_parse_agent_registration` (`main.py:943`); `deploy/systemd/agents-system.env.example:89` | Solo nombra `agent`, salvo que la pregunta abierta 4 agregue un valor de carpeta |
| Overrides de cliente | `resolve(..., client=)` acepta solo un nombre de rol predefinido (`loader.py:3787`) | Siguen funcionando para `agent`; quien adopta el kit edita su propia carpeta copiada |
| Permisos de spawn integrados | `spawn:sales-agent`, `spawn:data-agent`, `spawn:summary-agent` (`src/agents_system/permissions/builtins.py:148-150`) | Siguen registrados hasta que ADR-006 (#170) decida la delegación |
| Default del harness en vivo | `role="sales-agent"` y `SALES_AGENT_PERMISSIONS` (`evals/live_harness.py:921`, `:995`, `:148`) | Recibe un agente de referencia como `Agent`, con un grant explícito |
| Runner de evals | `Scenario.role: str` (`evals/schema.py:121`); `resolve(scenario.role, ...)` (`evals/runner.py:918-919`) | Acepta además una carpeta o un `Agent` |
| Escenarios | 26 archivos en `evals/scenarios/`, 5 en `evals/scenarios/guardrails/` | Se dividen en escenarios de capacidad y escenarios de composición de agentes de referencia |
| Pruebas en vivo | `tests/test_live_eval_{roles,sales_agent,guardrails,phase1,phase4}.py`, `tests/test_live_smoke.py`, `tests/test_live_load_phase3.py` | Se reapuntan; `restock-helper` extiende el `sales-agent` de referencia mediante `Agent.from_folder(..., extends=...)` |
| Guard 1 | `tests/platform_role_contract.py`, `tests/test_role_governance_contract.py`, `scripts/check_role_governance_marker.py`, `.github/workflows/ci.yml:687`, `openspec/specs/predefined-agent-governance/` | Reducido a `agent` (arriba) |
| Otras pruebas offline | 69 archivos bajo `tests/` nombran un rol concreto; la mayoría de las menciones están en `test_main.py`, `test_role_resolution_pinned.py`, `test_loader_fail_closed.py`, `test_openai_adapter.py` y `test_harness_factory.py` | Usan `agent`, un `Agent` inline o un agente de referencia como fixture |
| Fundamento de conectores | `src/agents_system/connectors/order_connector.py:9` y `report_connector.py:54` explican que la herramienta siempre existe porque un rol la nombra | Comportamiento sin cambios; los comentarios nombran al agente de referencia |
| Docstring público | `src/agents_system/__init__.py:68` (`extends="sales-agent"`) | Muestra `extends="agent"` |
| Demo | `examples/demo/app.py` sirve mediante `AGENT_REGISTRATIONS`; `docs/platform_es/demo-entrypoint.md` registra `sales-agent` | Registra agentes de referencia con `create_app(agents=...)` |
| Documentación | 43 archivos bajo `docs/` (EN y ES), encabezados por `features.md:21`, `role.md`, `library-usage.md`, `deployment.md`, `live-eval.md`, `demo-entrypoint.md`; las secciones library-first y Guard 1 del README | Se reescriben en los mismos cambios, EN + ES |
| Specs | `openspec/specs/{agent-definition-locator,agent-registration-serving,permission-hierarchy,predefined-agent-governance}/` | Se enmiendan; el término "rol predefinido" se retira |

### 3. Qué escenarios en vivo ejecuta cada cambio

Un cambio de runtime ejecuta los escenarios de capacidad. Un cambio en un agente de referencia ejecuta ese agente y todo agente que lo extienda, según el grafo de `extends:`. Esta tabla se propone para reemplazar la tabla de la Definition of done de `AGENTS.md` cuando se acepte este ADR; este PR no edita `AGENTS.md`.

| Si cambió | Ejecute como mínimo |
|---|---|
| El runtime, el grafo, el interceptor, el modelo de permisos, los límites, la auditoría o el sandbox | La suite de mecanismo (parte de `pytest -q`) y todos los escenarios de capacidad de comportamiento |
| `base`, `agent` o el contrato base del prompt | Todos los escenarios de capacidad de comportamiento y todos los de composición |
| La descripción o el esquema de una herramienta, o un conector | Los escenarios de capacidad que la equipan, y los de composición de cada agente de referencia que la declara |
| El prompt, la política o el manifiesto de un agente de referencia | Los escenarios de composición de ese agente y los de cada agente que lo extiende (`operator-agent` → `developer-agent`) |
| Un canal, la aplicación servida o la memoria de conversación | Los escenarios de composición a través del harness, y `tests/test_live_smoke.py` |
| La API de `Agent`, `extends`, las skills o el importador | `tests/test_live_eval_phase4.py` |
| La concurrencia, el worker del webhook, el outbox o los pools de conexión | `tests/test_live_load_phase3.py` |
| La herramienta SQL o los reportes parametrizados | `tests/test_live_eval_sql_query.py` |
| El runner de evals o la suite de capacidades | Los escenarios de capacidad sobre el modelo de referencia |

El modelo de referencia, la regla de forzarlo y el gate de release (`scripts/run-live-suite.sh`, ahora agrupado en mecanismo, comportamiento y composición) no cambian.

### 4. Una matriz de capacidades por modelo

La suite de capacidades es una funcionalidad del kit. Dados unos modelos candidatos y las capacidades que necesitan los agentes de quien adopta el kit (todas por defecto), ejecuta cada escenario de capacidad `EVAL_RUNS` veces por modelo e informa una tabla modelo × capacidad: tasa de éxito frente al gate, corridas ejercitadas, tokens, costo y duración. Cada uno de esos números ya está en `ScenarioResult` (`evals/runner.py:193`), y `write_results` ya escribe JSON y Markdown en `evals/results/`, que git ignora. El punto de entrada es una función de Python en `agents_system.evals` con un script delgado alrededor, detrás de los extras que ya necesitan las funcionalidades de evaluación (`postgres` y el extra del proveedor, `docs/platform_es/features.md`). Quien adopta el kit puede sumar a la misma matriz sus propios escenarios sobre sus propios agentes, cuando el runner acepte un `Agent`. La documentación aclara qué significa una celda: una tasa para un modelo sobre el prompt genérico del kit y un fixture mínimo, no un veredicto sobre el agente propio de quien adopta el kit.

## Migración

1. **Release N (la próxima minor; `bump-minor-pre-major` está activo, `release-please-config.json:3`).** Los agentes de referencia llegan a `examples/agents/`, ya dentro del techo de quien importa. Los ocho nombres siguen resolviendo desde `platform/roles/`, y resolver uno emite un `DeprecationWarning` que nombra la carpeta de ejemplo a copiar.
2. **Release N+1 (incompatible, `feat!`).** Las ocho carpetas salen de `platform/roles/`. `extends: sales-agent` y un valor de `AGENT_REGISTRATIONS` que nombre un rol concreto fallan al cargar o al arrancar con un `DefinitionError` que nombra la carpeta de ejemplo, del mismo modo que ADR-004 D2 falla de forma ruidosa hoy.
3. **Qué hace quien adopta el kit.** Copia `examples/agents/<nombre>/` en su proyecto y lo carga con `Agent.from_folder(path)`. Reemplaza `extends="sales-agent"` por la copia (un `extends:` hermano dentro de la misma raíz de quien importa, o `extends=Agent.from_folder(...)` en Python). Pasa un override de cliente de un rol concreto a ediciones sobre la copia, porque los overrides de despliegue aplican solo a roles de plataforma (ADR-004 D5). Registra la copia con `create_app(agents=...)`. Los nombres de permisos y `DEPLOY_GRANTS` no cambian. Quien copie uno de los cinco agentes ajustados recibe la política ajustada, que las notas de la release enumeran.

## Consecuencias

- Las corridas en vivo se abaratan y sus fallos se vuelven legibles: un fallo de capacidad apunta al kit, uno de composición a un ejemplo. El módulo de roles, que insumió el 64% de los tokens de entrada, se reduce a unos pocos escenarios de composición.
- Los mecanismos se prueban en cada PR con un pasa/falla estricto, y un modelo cuidadoso ya no puede dejar un guard sin probar.
- Los agentes de referencia resuelven por el camino de quien importa, así que la suite en vivo prueba lo que ejecuta quien adopta el kit, incluido `_apply_importer_ceiling`.
- Es incompatible para quien extiende o sirve un rol concreto por nombre, después de una release de obsolescencia. Quien instala con `pip install` encuentra los ejemplos en el repositorio, no en el wheel.
- Cinco agentes de referencia cambian su política para entrar en el techo de quien importa, y sus escenarios se vuelven a medir.
- Los overrides de `deployments/<cliente>/<rol>/` conservan solo `agent` como sujeto, y la suite de contrato D.17 cubre un rol de plataforma en lugar de nueve.
- ADR-002 E.18 mide por capacidad y por modelo en lugar de por rol y por modelo; el árbol de ADR-002 D conserva solo `base` y `agent`; el `extends` por nombre de rol predefinido de ADR-004 D3, el registro de la demo de D7 y el alcance de Guard 1 de D8 cambian como se describe arriba. Cada uno recibe un puntero al aceptarse, como hizo ADR-004 con ADR-002.

## Preguntas abiertas para el responsable

1. **Dónde viven los agentes de referencia.** `examples/agents/` es visible, está documentado y queda fuera del wheel; `tests/fixtures/` también queda fuera del wheel pero se lee como algo interno, así que nadie lo encuentra como ejemplo; dejarlos en el paquete los mantiene soportados de hecho. **Recomendación:** `examples/agents/`. La suite en vivo corre desde un checkout, así que quedar fuera del wheel no le cuesta nada.
2. **Cómo entran los cinco agentes que fallan en el techo de quien importa.** O se ajustan (`data-agent` y `summary-agent` pasan a `supervised`; los cuatro que reformulan una condición heredada mueven esa redacción a condiciones nuevas, propias del rol; `developer-agent` quita su override de 120 s y hereda 30 s, con `operator-agent` elevado al default de plataforma de 60 s, `loader.py:171`, si sus escenarios necesitan más; 120 s también supera ese default), o se les da a los agentes de referencia una raíz privilegiada propia. **Recomendación:** ajustarlos. Una raíz privilegiada probaría un camino que quien adopta el kit no puede usar.
3. **Ventana de obsolescencia.** **Recomendación:** una release minor con un `DeprecationWarning`, y la eliminación en la siguiente release incompatible.
4. **Servicio solo por entorno.** Sin cambios, `AGENT_REGISTRATIONS` solo puede servir `agent`. **Recomendación:** aceptar un valor de carpeta resuelto bajo una raíz de agentes configurada, con la misma contención que `FolderLocator`. De lo contrario, los despliegues que usan solo el entorno deben pasar a `create_app(agents=...)`.
5. **Qué agentes de referencia llevan escenarios de composición.** **Recomendación:** `sales-agent` (entrada no confiable, escrituras, el webhook), `developer-agent` con `operator-agent` (el sandbox, una cadena de dos niveles) y `data-agent` (reportes). Los otros cinco conservan solo pruebas de contrato offline, sin escenarios en vivo.
6. **Publicación de la matriz.** **Recomendación:** cada PR de release lleva la matriz del modelo de referencia y de los dos modelos de segunda opinión (`docs/platform_es/live-eval.md`, "Modelo de referencia"), en el cuerpo del PR, sin commitearla.

## Seguimientos

Issues a abrir cuando se acepte este ADR (ninguno se abre ahora):

1. Suite de mecanismo offline: un modelo guionado para cada escenario de guardrail y `*_boundary`, y luego su salida del gate en vivo.
2. Runner de evals: `Scenario` y `run_scenario` aceptan una carpeta o un `Agent`, no solo un nombre de rol predefinido.
3. Agentes de fixture de capacidad y escenarios de comportamiento, uno por capacidad, distribuidos en `agents_system.evals`.
4. Agentes de referencia en `examples/agents/`, ajustados al techo de quien importa, con el aviso de obsolescencia (release N).
5. Eliminación de `platform/roles/` (release N+1): Guard 1 reducido, la suite de contrato y las pruebas en vivo reapuntadas, el default del harness en vivo reemplazado.
6. Aplicación servida y demo: la demo registra agentes de referencia; el valor de carpeta de `AGENT_REGISTRATIONS` si se acepta la pregunta abierta 4; los ejemplos de entorno actualizados.
7. Documentación y specs enumeradas en la parte 2, EN + ES, y los punteros en ADR-002 y ADR-004.
8. La tabla de la Definition of done en `AGENTS.md`, la agrupación de suites en `scripts/run-live-suite.sh` y una utilidad que enumere los agentes que extienden un agente de referencia.
9. La matriz de capacidades: API, script y documentación.
