# ADR-009 — Versionado de agentes y un ciclo de desarrollo por agente

**Estado:** Propuesto · **Fecha:** 2026-09-29 · **Issue:** #189 · **Enmendaría:** ADR-004 D8 (alcance del Guard 1)

## Resumen

Todo agente ya tiene un `version`, y nada lo lee. Este ADR propone darle significado, en cinco decisiones que se apoyan entre sí. (1) Una **huella** (*fingerprint*), un hash de contenido sobre la definición *efectiva* (la definición resuelta más el contenido de los skills que carga), se registra junto a la versión en un archivo de lock revisado; el CI la compara con el lock de la rama base, y una huella que cambió sin subir la versión hace fallar el build, y un cambio en la autoridad del agente (herramientas, permisos, autonomía, delegación, `untrusted_input`) exige subir MAJOR. Es el Guard 1 (ADR-004 D8) extendido de los roles predefinidos a todo agente. (2) **Las evaluaciones son la compuerta de release:** cada agente lleva sus propios escenarios, y una versión se libera solo cuando las compuertas de #81 pasan para esa huella exacta y ese conjunto exacto de escenarios. (3) **Trazabilidad:** cada turno, evento de auditoría y métrica nombra al agente y su versión, de modo que dos versiones puedan compararse con datos. (4) **Despliegue y rollback:** un despliegue servido fija (*pin*) la versión y la huella que espera y se niega a arrancar si alguna no coincide, la misma postura fail-loud que `DEPLOY_GRANTS`; el rollback restaura la revisión anterior y ambos pins, y la huella prueba que es la misma definición. (5) **Estados del ciclo de vida y herramientas:** borrador, evaluado, liberado, obsoleto, manejados por `agents-system agent check | eval | release | diff`. Nada de esto está implementado; el dueño decide, y los issues de implementación siguen a la aceptación.

## Contexto

**Qué existe.** `Agent.version` (`src/agents_system/agent/spec.py`) tiene por defecto `"1.0"`. El loader lo lee del frontmatter de `role.md`, luego de `manifest.md`, y por último cae en `"1.0"` (`_load_role_files` y su gemela de deployment en `harness/loader.py`), como un `str` sin más: nada valida su forma. Se copia a `RawDefinition` y a `AgentDefinition`. En una cadena `extends:` gana el valor propio del hijo (`version=child.version` en `_fold_parent_into_child`), así que ni la versión del padre ni un cambio del padre llegan al hijo.

**Por qué es decorativo.** Fuera del loader, nada en `src/` lee `AgentDefinition.version`, por lo tanto:

- nada verifica que cambie cuando el agente cambia: un prompt, una herramienta o un modelo pueden cambiar con la versión intacta;
- las métricas se etiquetan solo por `runtime_id` (`observability/metrics.py`: `agent_turns`, `agent_turn_duration_seconds`, `agent_tokens`, `agent_cost_usd`), y `agent_tool_calls` por `tool`;
- los eventos de auditoría llevan `role`, `deployment` y un `actor` (`audit/events.py::_AuditEventBase`, completados por `_extract_role_deployment`), nunca una versión, aunque la definición que recibe esa función tiene una;
- la línea de log `runtime.turn_usage` tampoco lleva versión.

**Qué hace cumplir un cambio hoy.** El Guard 1 (ADR-004 D8) tiene dos mitades, ambas solo para roles predefinidos. Una mitad en pytest (`tests/platform_role_contract.py::check_role_governance_snapshot`) compara las `tools` y `permissions` resueltas de cada rol con un `EXPECTED_ROLE_SURFACE` mantenido a mano y falla ante una divergencia salvo que el componente MAJOR del rol haya crecido y `CHANGELOG.md` nombre al rol. Una mitad en CI (`scripts/check_role_governance_marker.py`) exige un marcador de cambio incompatible de conventional commits en un PR que toque `platform/roles/**/manifest.md`. ADR-004 D8 dice explícitamente que un `Agent` de un importador "nunca puede convertirse en sujeto de gobernanza mediante este mecanismo", porque no tiene nombre en `PINNED_ROLES`. El prompt, el modelo, la política y los límites quedan fuera del Guard 1 incluso para los roles predefinidos.

**Qué ya ofrece el harness.**

- *Superficie de registro y pin.* `create_app(agents=...)` y `AGENT_REGISTRATIONS` asocian un id opaco elegido por quien despliega con un `Agent` o con `"role"` / `"role@client"` (ADR-004 D5). Ese id es el `agent_id` natural; este ADR no introduce una segunda identidad.
- *Decisiones de despliegue explícitas y fail-loud.* `DEPLOY_GRANTS` exige una entrada por model id o el arranque falla (ADR-003). Un pin de versión es una decisión del mismo tipo.
- *Evaluaciones con compuertas.* Los `evals/scenarios/*.yaml` están sujetos a las compuertas de #81: un escenario de guardrail pasa solo con 100% de las corridas *ejercitadas* y al menos una ejercitada; uno de camino feliz con 80%, salvo que declare un umbral y su razón (`docs/platform_es/live-eval.md`, "Gating"). Los reportes llevan modelo, tokens y costo por escenario. Las corridas en vivo son opt-in y a propósito no son una compuerta por PR.
- *Prompt fuera del checkpoint.* `_call_model` antepone el prompt del sistema al momento de la llamada y nunca lo guarda en el estado (`agent/graph.py`). Un hilo retomado corre entonces con el prompt desplegado *ahora*; el hilo guarda mensajes, no al agente.

**Qué no existe.** No hay CLI (`pyproject.toml` no declara ningún script de consola). El runner de evaluaciones resuelve `scenario.role` por el árbol de roles de la plataforma, con un `client` de deployment opcional (`evals/runner.py`): hoy no puede apuntar a un `Agent` de un importador. No existe un checkpointer durable (#24) ni memoria propia de largo plazo del agente (ADR-002 A.3, G.25).

**Numeración.** ADR-004 son los agentes library-first, ADR-005 la hoja de ruta de seguridad operativa, ADR-006 está reservado tentativamente para delegación, ADR-007 es la herramienta SQL de solo lectura y ADR-008 la propuesta de MCP. Este es el ADR-009.

## Decisión

### D1 — Versión más huella, verificadas para todo agente

**La huella.** SHA-256 sobre una serialización canónica (claves ordenadas, tuplas como listas, UTF-8) de la definición *efectiva*: la `AgentDefinition` resuelta (después del pliegue de `extends:` y después de que la plataforma agrega sus cláusulas del contrato base) **más el contenido de los skills cargados para ella**. La definición resuelta sola no alcanza. Lleva los *nombres* de los skills y los lugares desde donde se leen (`skills`, `skills_folder`, `inline_skills`, `importer_root`), no el texto de un skill de carpeta o de despliegue, y su `system_prompt` es el cuerpo del rol más el contrato base, no lo que recibe el modelo. El texto que recibe el modelo se compone después, al equipar, a partir de ese cuerpo, los archivos de skill, el bloque de escalación y el contrato base (`harness/factory.py::_compose_prompt`, enviado por `_call_model` como `EquippedRuntime.system_prompt`). Por eso la huella se calcula después de la misma carga de skills (`_load_skills`, misma precedencia de fuentes, incluida la carpeta `skills/` del despliegue cuando se da un cliente), y la sección prompt hashea el prompt compuesto. `agent check` corre entonces ese cargador sin conexión; una verificación que hasheara solo la `AgentDefinition` no se movería al editar un archivo de skill. La huella se calcula por sección, y la huella del agente es el hash de los digests de las secciones:

| Sección | Campos | Incremento que exige |
|---|---|---|
| **autoridad** | `tools`, `permissions`, `command_tools`, `untrusted_input`, `autonomy`, `delegation_policy` | **MAJOR** |
| **prompt** | el `system_prompt` compuesto (cuerpo del rol, contenido de los skills cargados en orden, bloque de escalación, contrato base), `context` | MINOR |
| **modelo** | `model` (`provider`, `model`, `temperature`, `max_tokens`; nunca una credencial, ver `_validate_model_config`) | MINOR |
| **política** | `escalation_rules`, `memory_policy`, `audit_policy` | MINOR |
| **límites** | `execution_limits` | MINOR |

Excluidos a propósito: el propio `version`, la disposición de archivos, los comentarios y espacios fuera del prompt, las rutas propias de la máquina `skills_folder` e `importer_root` (lo que cuenta es el *contenido* de los skills, que entra por los skills cargados), `role_name` y `deployment` (identidad y procedencia: lo que cambia un override de despliegue llega a las secciones anteriores por los campos que restringe y los skills que aporta), y la configuración propia del despliegue (`DEPLOY_GRANTS`, el id de registro, el checkpointer). Los grants pertenecen al despliegue y ya son explícitos (ADR-003); meterlos en la huella del agente haría que la versión de un agente dependiera de dónde se despliega.

`model=None`, el caso común, significa "usar el proveedor por defecto global" (`ADAPTER_PROVIDER`). Ese valor por defecto no es parte de la definición, así que cambiarlo queda fuera de la huella. Es deliberado: es una decisión del despliegue como `DEPLOY_GRANTS`, no una edición del agente. No es invisible: el modelo queda registrado en la evidencia de evaluación (D2) y en los campos de trazabilidad (D3). Un agente cuyo comportamiento no deba seguir al valor por defecto debería declarar su propio `model`, que entonces sí está en la sección *modelo*.

Hashear la definición efectiva y no los archivos es el punto central. Un cambio en un rol padre o en el contrato base de la plataforma llega a un hijo por `extends:` mientras el `version:` propio del hijo (que no se hereda) queda quieto; un hash de los archivos del hijo no lo ve, y en cambio recoge ruido que la definición no contiene. La edición de un archivo de skill se detecta por otra razón: los archivos de skill se leen del disco al equipar, no por `extends:`, así que mueve la huella solo porque el contenido de los skills cargados es parte de su entrada. El precio es que un release de la plataforma que edite el contrato base mueve la sección *prompt* de todos los agentes. Eso exige un MINOR en cada uno, y es la señal honesta: su comportamiento puede cambiar.

**El registro revisado.** Un archivo de lock versionado junto a la definición del agente (`agent.lock.json`; para un agente armado en línea en Python, la ruta que su dueño pasa al CLI) guarda `version`, `fingerprint`, los digests de las secciones, las `tools` y `permissions` ordenadas (para que un `diff` pueda nombrar qué cambió), el estado del ciclo de vida (D5) y la última evaluación (D2). Cumple el papel que `EXPECTED_ROLE_SURFACE` cumple para los roles predefinidos: una instantánea independiente que la definición no puede actualizar por sí misma. Regenerarlo es un comando deliberado, y aparece en el mismo diff que la subida de versión que el revisor está mirando.

**La verificación** (`agents-system agent check`, y un helper de pytest con la misma lógica para equipos que validan en su propia suite) vuelve a resolver el agente sin conexión (cargando sus skills, como arriba) y aplica dos comparaciones.

- **Consistencia.** El lock de trabajo debe ser igual a lo que produce la definición de trabajo (huella, digests de secciones, digest de escenarios). Si no, el lock está desactualizado y la verificación falla: hay que regenerarlo.
- **Historia.** El lock de trabajo se compara con el lock de la rama base del PR (en CI, `git show <base>:<ruta>/agent.lock.json`, o el lock del último tag de release): un registro previo que el PR no puede reescribir. Un lock regenerado en el mismo PR que el cambio solo prueba que el PR concuerda consigo mismo, así que las reglas de versión de abajo se aplican a través de esa frontera, nunca contra el archivo que el PR acaba de escribir. Un agente sin lock en la base es nuevo y pasa como borrador. Donde no hay base disponible (una corrida local, un agente en línea sin historia), solo se verifica la consistencia y la salida dice que la regla de historia no se aplicó.

La comparación de historia rige tanto para agentes de importadores como para roles predefinidos; es la línea base independiente que a los agentes de importadores les faltaba bajo el Guard 1.

| Lock de trabajo vs lock base | Resultado |
|---|---|
| Misma huella | Pasa |
| Huella cambiada, versión sin cambio | **Falla:** "cambió sin subir la versión", nombrando las secciones que se movieron |
| Sección de autoridad cambiada, MAJOR sin aumentar | **Falla:** se exige MAJOR, nombrando las herramientas/permisos agregados o quitados |
| Cualquier otra sección cambiada, versión sin aumentar | **Falla** |
| Versión menor que la del lock | **Falla** |
| Versión aumentada, huella sin cambio | Pasa con una nota (un release re-emitido) |

La forma de la versión pasa a ser `MAJOR.MINOR` (solo dígitos), que es lo que el Guard 1 ya interpreta; hoy se acepta cualquier cosa que diga un manifiesto, así que validar la forma es un pequeño cambio de comportamiento (ver Preguntas abiertas).

**Guard 1 y los roles predefinidos.** De aceptarse, un rol predefinido recibe un lock como cualquier otro agente, y el `EXPECTED_ROLE_SURFACE` escrito a mano queda redundante. El job de CI del marcador de commit y su mitad de `CHANGELOG.md` se mantienen: sirven a los *consumidores* de un rol predefinido, que se enteran de un cambio incompatible por las notas de release, algo que una huella no puede escribir. Retirar la instantánea es un follow-up, no parte de esta decisión.

### D2 — Las evaluaciones son la compuerta de release

- **Dónde viven los escenarios.** Los escenarios de un agente están en una carpeta `evals/` dentro de su propia carpeta, con el esquema YAML existente, y apuntan a ese agente sin una línea `role:`. El `evals/scenarios/` del repositorio sigue sirviendo a los roles predefinidos. Esto exige que el runner acepte una carpeta de importador o un `Agent`, algo que hoy no puede.
- **Qué pasa.** Las compuertas de #81, sin cambios: 100% de las corridas ejercitadas para escenarios de guardrail, 80% (o un umbral declarado con su razón) para los de camino feliz. Aquí no se introduce ningún umbral nuevo.
- **Piso recomendado.** Una versión sin escenarios no puede liberarse, y un agente que tenga cualquier herramienta T2 o T3 (ADR-003) debe llevar al menos un escenario de guardrail. Sin eso, "evaluado" lo satisfaría un agente al que nunca se le pidió portarse mal.
- **La evidencia queda atada a la huella y a los escenarios.** El *digest de escenarios* es un SHA-256 sobre el inventario completo de los archivos de escenarios del agente (rutas ordenadas y contenidos canónicos, de modo que agregar, quitar o editar un escenario, sus aserciones o su umbral declarado lo mueve) más los parámetros de compuerta vigentes (los umbrales de guardrail y de camino feliz de #81 y la cantidad de corridas). `agent eval` corre los escenarios contra la definición actual y escribe el resultado en el lock: huella, digest de escenarios, modelo, veredictos de compuerta por escenario, fecha. `agent release` se niega salvo que la evaluación registrada coincida con *tanto la huella actual como el digest de escenarios actual*, y la evaluación se repite cuando cualquiera de los dos cambia. Agregar un escenario de guardrail que falla sin tocar el agente deja la huella igual pero mueve el digest, así que el resultado aprobado anterior deja de valer. La verificación offline recalcula el digest sin costo, de modo que un lock que declara `liberado` sobre escenarios que cambiaron desde entonces se informa como borrador. Eso es lo que hace que "evaluado" signifique algo.
- **Dónde corre.** La evaluación en vivo cuesta dinero y necesita una clave de proveedor, y #81 la mantiene fuera del camino por PR a propósito. Por eso la compuerta se divide: la verificación offline de la huella (D1) es gratis y corre en cada PR; la evaluación en vivo corre al liberar, por el dueño del agente o un job programado, sobre el modelo de referencia que nombra el documento de live-eval. Qué modelo debe usar la compuerta cuando el agente declara el suyo es una pregunta abierta.

### D3 — Trazabilidad: `agent_id`, versión y huella en todo

`agent_id` es el id de registro (`runtime_id` hoy); el nombre del rol sigue siendo lo que es, el *tipo* del agente. Se propone:

- **Eventos de auditoría:** `agent_id`, `agent_version` y `agent_fingerprint` pasan a ser campos de `_AuditEventBase`, completados donde ya se completan `role` y `deployment`. Son aditivos y anulables, así que las filas y sinks existentes no se ven afectados; si pasan a ser columnas o viven en `payload` es una decisión de implementación (las columnas se pueden consultar; `payload` no necesita migración).
- **Logs:** la línea `runtime.turn_usage` y los demás eventos `runtime.*` llevan los mismos tres valores.
- **Métricas:** `agent_version` pasa a ser una etiqueta de las cuatro métricas que ya llevan `runtime_id` (turnos, duración, tokens, costo), porque son las que comparan dos versiones. La huella *no* es una etiqueta: cambia en cada edición y las etiquetas de métricas se revisan para mantenerse acotadas. Va en un único gauge `agent_build_info{runtime_id, agent_version, fingerprint} 1`, la forma habitual de adjuntar metadatos de build sin multiplicar series. Las métricas por herramienta conservan su etiqueta `tool`; agregarles la versión multiplicaría series para una comparación que nadie necesita.
- **Checkpoints:** la versión que escribió un checkpoint se registra en su metadata (es la entrada de D4).

La parte de métricas es el contrato de etiquetas que #173 (métricas por agente) debería adoptar, para no hacer el trabajo dos veces. La cardinalidad sigue acotada porque un despliegue corre un puñado de versiones a la vez.

### D4 — Despliegue y rollback

**Pin.** Un registro en un despliegue servido debe indicar la versión que espera **y** la huella. Al arrancar, el lifespan resuelve cada agente (cargando sus skills, como en D1), compara ambas y se niega a iniciar ante cualquier discrepancia, nombrando el id y los valores fijado y resuelto. Un pin solo de versión no alcanza: acepta una definición cambiada que conservó su versión, que es justo la falla que este ADR busca cerrar. Es la misma postura que un id no registrado o una entrada faltante de `DEPLOY_GRANTS`: un despliegue no ejecuta en silencio algo distinto de lo que su operador dejó escrito. La única excepción es una exclusión explícita para desarrollo (un registro marcado sin pin a propósito, rechazado cuando el despliegue se declara producción, y registrado en el log al arrancar), para que el uso en proceso, los notebooks y los tests no estén obligados a mantener pins. Los pins obligatorios cambian el comportamiento de hoy, donde los registros no llevan ninguno, así que se entregan con una nota de release y un release de aviso antes de que el arranque falle. La sintaxis exacta para `AGENT_REGISTRATIONS` (hoy sus valores son strings), cómo un despliegue declara su modo y el período de aviso se dejan al issue de implementación. La salida de `release` del CLI imprime el pin para pegar.

**Rollback.** El rollback es volver a desplegar la revisión anterior *y* restaurar ambos pins anteriores, versión y huella. La verificación de arranque rechaza una revisión cuya huella no coincide con los pins, así que un rollback parcial falla con ruido en lugar de ejecutar algo intermedio. Una definición son archivos en git, así que una versión anterior es recuperable sin ninguna copia guardada en otro lado, y la huella fijada prueba que el redespliegue reprodujo la definición liberada exactamente, no una aproximación. La alternativa, una instantánea resuelta y congelada por release que pudiera cargarse sin el código viejo, se consideró y se difirió: necesita un serializador y un cargador de `AgentDefinition` (contenido de skills, command tools, modelo) que deben repetir todas las validaciones de carga, un segundo camino hacia los mismos invariantes por un beneficio que una revisión de git ya da.

**Hilos a través de un cambio de versión.** Hoy un hilo con checkpoint guarda solo mensajes y el prompt del sistema se aplica al momento de la llamada, así que una conversación retomada corre de inmediato bajo la definición nueva. Qué significa el cambio de versión para ella:

- **MINOR:** los hilos continúan. El mismo cliente mantiene la misma conversación.
- **MAJOR:** la recomendación es continuar también, y registrar la transición. La autoridad no se guarda en un hilo: la Capa 2 revalida cada llamada sensible contra el `deploy_grant_ceiling` *actual*, así que un hilo viejo no puede arrastrar un permiso quitado. Lo que puede salir mal es la coherencia: un historial que muestra llamadas a una herramienta que la versión nueva ya no tiene. Ese riesgo es real, depende del proveedor y no está probado aquí; debe ejercitarse con un test de implementación antes de llamarlo seguro.
- **La alternativa** (un espacio de nombres de hilo nuevo por cada MAJOR) es más limpia para el modelo y peor para el cliente: cada MAJOR reiniciaría todas las conversaciones abiertas. Si el dueño la prefiere, es una elección por agente (`continue` o `new_thread` en MAJOR), no un valor por defecto de la plataforma.
- **La memoria de largo plazo** (ADR-002 A.3, G.25) todavía no existe. Cuando exista se indexará por `agent_id`, no por versión, y un cambio en lo que una memoria *significa* es problema de esa funcionalidad. #24 es el momento de agregar la versión a la metadata del checkpoint, de modo que los datos para decidir ya estén.

### D5 — Estados del ciclo de vida y herramientas

```
borrador ──eval pasa para esta huella──▶ evaluado ──release──▶ liberado ──deprecate──▶ obsoleto
   ▲                                        │                     │
   └───── edición del agente o escenarios ──┴─────────────────────┘ (la definición editada es un borrador nuevo)
```

- **borrador:** la definición difiere del lock, o no tiene una evaluación aprobada para su huella y el digest de escenarios actual.
- **evaluado:** los escenarios pasaron sus compuertas para la huella actual.
- **liberado:** versión subida como exige D1, evaluada, con lock. Es el único estado que un pin de producción debería nombrar.
- **obsoleto:** todavía se puede resolver, para que un destino de rollback no desaparezca, pero un pin hacia él avisa al arrancar y se desaconseja un despliegue nuevo. Quitar una versión es trabajo de git.

El estado vive en el archivo de lock, no en una base de datos, de modo que viaja con el código que describe.

| Comando | Hace |
|---|---|
| `agents-system agent check` | La verificación offline de D1. Lo que corre CI en cada PR. |
| `agents-system agent eval` | Corre los escenarios del agente y registra el resultado contra la huella. |
| `agents-system agent release` | Exige una evaluación aprobada para la huella actual y el incremento que pide D1; escribe el lock. |
| `agents-system agent diff [A] [B]` | Diferencia sección por sección entre el lock y la definición de trabajo, o entre dos locks, con el incremento que exige cada diferencia. |
| `agents-system agent deprecate` | Marca obsoleta una versión liberada. |

`check` y `diff` son puros y offline, por lo que deben funcionar desde la instalación liviana (#188) sin los extras de live-eval ni de servidor. Esto también introduce el primer script de consola del paquete.

## Alternativas consideradas

| Alternativa | Por qué no |
|---|---|
| **Solo versión, garantizada por revisión** (hoy) | La razón de este ADR: nada falló cuando la versión quedó quieta. |
| **Un SHA de git como versión** | Excelente para trazabilidad, mudo sobre compatibilidad: no puede decir que un cambio amplió la autoridad, y cambia con commits no relacionados. Sirve como metadata *adicional*, no como la versión. |
| **Solo huella, sin versión humana** | Un hash no puede expresar MAJOR frente a MINOR, y nadie puede decir "volvamos a la 2.1" en un incidente. |
| **Hashear los archivos, no la definición resuelta** | No ve un cambio heredado por `extends:` o desde el contrato base, y reporta los espacios en blanco como un cambio. |
| **Un registro respaldado por base de datos con versiones guardadas y cambiadas en runtime** | Más flexible y un producto distinto: la definición deja de ser archivos revisados, y choca con el modelo de grants explícitos. Diferido hasta que haya demanda de cambio en caliente. |
| **Una etiqueta de versión en toda métrica, huella incluida** | Multiplica series, y la huella cambia en cada edición. El patrón de gauge informativo lleva la misma información a costo fijo. |

## Consecuencias

- De aceptarse, se enmienda la frase de ADR-004 D8 que excluye a los agentes de importadores del Guard 1: pasan a regirse por el mecanismo de lock. El texto original queda como historia, con un puntero, como hizo ADR-004 con ADR-002.
- Todo agente gana un archivo de lock y una disciplina de versión. El costo es una subida en cada cambio, que es la idea; la verificación nombra qué se movió para que la subida no sea una adivinanza.
- Una edición del contrato base de la plataforma fuerza un MINOR en todo agente que tenga lock. En lote, es un PR mecánico por consumidor; también es justo el momento en que un consumidor debería volver a correr sus evaluaciones.
- Los pins pasan a ser obligatorios en despliegues servidos: los registros existentes necesitarán versión y huella cuando esto se entregue, tras un release de aviso.
- Las filas de auditoría y las series de métricas crecen con los campos nuevos y la etiqueta de versión. Aditivo; ninguna consulta existente se rompe.
- El runner de evaluaciones gana un nuevo tipo de objetivo (una carpeta de agente), trabajo compartido con cualquier otra funcionalidad que quiera evaluar agentes de importadores.

## Preguntas abiertas para el dueño

1. **Forma de la versión.** ¿Exigir dígitos `MAJOR.MINOR`? Los agentes personalizados existentes con un `version:` libre fallarían en `check` hasta adoptarla.
2. **Qué modelo compuerta un release** cuando el agente declara el suyo: ¿el declarado (más fiel) o el de referencia (comparable entre agentes)?
3. **El piso de escenarios.** ¿"Al menos un escenario de guardrail para todo agente con una herramienta T2/T3" es el mínimo correcto, o demasiado estricto para un primer release?
4. **MAJOR con hilos vivos:** ¿continuar (recomendado) o un espacio de nombres nuevo, y es un valor por defecto de la plataforma o una configuración por agente?
5. **Pins:** este ADR propone que los registros servidos deban fijar versión y huella, con una exclusión explícita para desarrollo. ¿Se confirma, y cuánto dura el período de aviso antes de que el arranque falle?
6. **Roles predefinidos:** ¿retirar `EXPECTED_ROLE_SURFACE` a favor de locks, o mantener ambos?

## Follow-ups (solo tras la aceptación)

- El módulo de huella y lock, con `agent check`; el job de CI, que compara contra el lock de la rama base, y reemplaza la exclusión de importadores del Guard 1.
- Soporte del runner de evaluaciones para una carpeta de agente; `agent eval` y `agent release`.
- Versión y huella en eventos de auditoría, logs y métricas, alineado con #173.
- Sintaxis del pin (versión y huella, exclusión para desarrollo) y verificación al arrancar.
- Versión en la metadata del checkpoint, junto con #24.
- `agent diff` y `agent deprecate`; el retiro de la instantánea de roles mantenida a mano.
