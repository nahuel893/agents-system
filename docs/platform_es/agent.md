# Agente (Agent)

Este documento es la definición de referencia de la plataforma para
"agente". Todo otro documento que use la palabra — `manifesto.md`,
`role.md`, `policy.md`, ADR-002 — remite a este. Donde este archivo y un
documento más antiguo no coincidan, este archivo prevalece; el desacuerdo
queda registrado como una corrección en ADR-002.

---

## Definición general

> Un **agente** es un modelo que decide sus propias acciones en un bucle
> (*loop*): elige qué hacer, actúa a través de herramientas (*tools*),
> observa el resultado y repite hasta que se cumple el objetivo o se
> alcanza un límite.

El peso definitorio recae en "decide sus propias acciones". Un sistema que
ejecuta una secuencia fija de pasos — incluso si un LLM redacta el texto en
cada paso — no es un agente bajo esta definición, a menos que el *siguiente
paso en sí* sea una decisión que el modelo toma a partir del estado actual,
y no un paso que un humano o un grafo estático ya eligió por él.

## Chatbot vs. flujo de trabajo con LLM vs. agente

| | Quién decide el siguiente paso | Actúa sobre el mundo |
|---|---|---|
| **Chatbot** | El humano, en cada turno. El modelo solo responde. | No. La salida es texto de vuelta al usuario; no se llama, escribe ni envía nada. |
| **Flujo de trabajo con LLM** (*LLM workflow*) | El autor del flujo, en tiempo de diseño. Un pipeline fijo (p. ej. clasificar → recuperar → resumir) puede invocar a un LLM en cada etapa, pero la *secuencia* de etapas está fijada de antemano, no la elige el modelo en tiempo de ejecución. | A veces — una etapa del flujo puede invocar una herramienta, pero qué etapa corre a continuación no es una decisión del modelo. |
| **Agente** | El modelo, en tiempo de ejecución, a partir del estado actual. Elige qué herramienta invocar a continuación (o ninguna), dado lo que observó hasta el momento. | Sí — ese es el punto. Las llamadas a herramientas son cómo actúa, y el bucle existe justamente para que pueda observar el efecto y decidir de nuevo. |

La propia capa de conectores de la plataforma (`harness/injector.py`,
`harness/interceptor.py`) existe específicamente para acotar *qué* pueden
tocar las decisiones de un agente en tiempo de ejecución — no cambia el
hecho de que es el modelo, y no el autor de un flujo de trabajo, quien
decide.

## Cinco componentes necesarios

| # | Componente | Sin él | ESTADO |
|---|---|---|---|
| 1 | **Objetivo e identidad** | El modelo no tiene nada hacia qué decidir. El propósito/alcance de `role.md` de un rol es el objetivo; ver "Rol = clase" más abajo para identidad. | ✅ implementado (rol); ❌ no implementado (identidad por principal — ver ADR-002 §A.2) |
| 2 | **Un modelo que decide** | Nada elige la siguiente acción; se tiene un script, no un agente. `AgentRuntime` vincula un `BaseChatModel` y deja que elija las llamadas a herramientas (`src/agentsys/agent/graph.py:344-375`). | ✅ implementado |
| 3 | **Herramientas** (*tools*) | Sin herramientas el bucle igual puede correr, pero el modelo solo puede hablar — no puede actuar sobre el mundo. Este es un caso límite real y soportado en esta plataforma, no hipotético: `AgentRuntime.__init__` solo invoca `model.bind_tools(...)` cuando la superficie de herramientas otorgada no está vacía (`agent/graph.py:373-375` — "Only call bind_tools when there are tools to bind — some fake models raise NotImplementedError for bind_tools even with an empty list"). Un rol resuelto con cero herramientas otorgadas degrada a chatbot en tiempo de ejecución, silenciosamente, por este mismo camino de código. | ✅ implementado, incluyendo el caso límite sin herramientas |
| 4 | **Un bucle con observación** | Una única llamada al modelo sin posibilidad de ver el resultado de su propia acción no puede corregir el rumbo. `_build_graph` conecta `_call_model → _execute_tools → _call_model → …` hasta que el modelo deja de solicitar herramientas o se dispara un límite. | ✅ implementado |
| 5 | **Límites** | Un bucle sin límites es un riesgo de costo descontrolado y de seguridad. `PLATFORM_DEFAULT_LIMITS` (`harness/loader.py:139-145`): `tool_call_timeout_s=10`, `total_execution_timeout_s=60`, `max_tool_calls=20`, `max_delegation_depth=2`, `max_clarification_attempts=3`. `_effective_limits` (`agent/graph.py:58-73`) mezcla las sobreescrituras de un rol sobre estos valores, campo por campo; `_ENFORCED_LIMIT_KEYS` (`agent/graph.py:51-55`) es el subconjunto que el bucle realmente lee hoy — `max_tool_calls`, `total_execution_timeout_s`, `tool_call_timeout_s`. | ⚠️ parcial — las tres claves aplicadas funcionan; `max_delegation_depth` y `max_clarification_attempts` están declaradas y mezcladas pero no las lee el bucle (`agent/graph.py:50` lo dice explícitamente: "not yet read here") |

## Fórmula de la plataforma

> **Agente = Rol + Identidad + Modelo, ejecutado en un bucle acotado y
> auditado.**

El rol aporta el objetivo, la superficie de herramientas y la política. La
identidad aporta a nombre de quién actúa el agente (ver "Rol = clase" más
abajo). El modelo aporta la toma de decisiones. "Acotado" es el componente
5; "auditado" es el pipeline de `audit_event` (`docs/platform_es/audit.md`)
que registra cada otorgamiento/denegación/llamada de herramienta.

| Término de la fórmula | Mecanismo de la plataforma | ESTADO |
|---|---|---|
| Rol | `AgentDefinition`, resuelto desde `platform/roles/<rol>/{role.md,manifest.md,policy.md}` por `resolve()` (`harness/loader.py:1035`) | ✅ implementado |
| + Identidad | Parámetro `granted_permissions` de `build_runtime` — ver ADR-002 §A.2 sobre por qué hoy son los permisos propios del rol, no los de un principal | ⚠️ parcial — el mecanismo existe, ningún principal lo alimenta |
| + Modelo | Cualquier instancia de `langchain_core.BaseChatModel`, vinculada a la superficie de herramientas otorgada en `AgentRuntime.__init__` (`agent/graph.py:363-375`) | ✅ implementado |
| , ejecutado en un | `EquippedRuntime` (`harness/factory.py:68-83`) es la especificación ensamblada, aún no viva; `AgentRuntime.run_turn` (`agent/graph.py:394-442`) es la ejecución | ✅ implementado |
| bucle acotado | `PLATFORM_DEFAULT_LIMITS` + `_effective_limits` (arriba) | ⚠️ parcial (ver componente 5) |
| y auditado | El injector de la Capa 1 emite `tool_granted`/`tool_denied`/`unknown_tool` (`harness/injector.py:77-144`); el interceptor de la Capa 2 emite `tool_call_attempted`/`tool_call_blocked`; ambos vía `audit/events.py` | ✅ implementado |
| bucle | `_build_graph` / `_call_model` / `_execute_tools` (`agent/graph.py`) | ✅ implementado |

## Distinción clave: rol = clase, agente = instancia

Un **rol** es un plano (*blueprint*): `platform/roles/sales-agent/` declara
qué tiene permitido ser y hacer cualquier agente instanciado con ese rol. No
está ejecutando nada y no actúa a nombre de nadie. `docs/platform_es/role.md`
ya lo expresa correctamente: "La definición del agente define qué tiene
permitido ser y hacer el rol. El runtime decide cómo y cuándo se instancia."

Un **agente** es una instancia de un rol actuando a nombre de un principal
específico (un cliente, un empleado, una identidad de servicio). Dos
empleados distintos hablando cada uno con su propio "agente empleado"
(`docs/architecture/agent-platform.md:154-163`, ya lo describe: "one main
employee agent... scoped to the employee identity and permissions") son dos
agentes distintos, ambos instancias del mismo rol.

Hoy la plataforma resuelve roles correctamente pero no propaga una identidad
de principal hasta el runtime (ADR-002 §A.2): `granted_permissions` al
arrancar es `definition.permissions` — los permisos propios y declarados del
rol, no los de ningún empleado o cliente (`src/agentsys/main.py:326-333`,
específicamente la línea 329). En concreto: hoy, cada cliente de WhatsApp
que habla con el rol `sales-agent` es atendido por el *mismo* objeto
`AgentRuntime`, construido una única vez al arrancar (`main.py:334-336`) y
guardado en caché en `app.state.runtimes`, con el mismo permiso otorgado. No
existe ninguna representación en código de "este agente, para este cliente"
distinta de "este rol". La distinción clase/instancia está implementada
para roles y todavía no para agentes.

## Modelo objetivo: el agente como actor virtual

La plataforma todavía no implementa esta sección — es la dirección que fija
ADR-002, inspirada en el patrón de actor virtual (*virtual actor*, como en
Microsoft Orleans, o los Durable Objects de Cloudflare): una unidad
direccionable de identidad + estado que el runtime activa bajo demanda y que
nunca hace falta pensar como "un proceso".

> **Un agente siempre existe en almacenamiento durable; se activa en
> memoria solo mientras atiende, y cada paso se persiste.**

En concreto, esto implica separar lo durable de lo efímero:

- **Durable** (vive en almacenamiento persistente, sobrevive a cada reinicio
  de proceso): la identidad del agente — `agent_id`, `role`, `principal` —
  y su espacio de memoria (memoria de trabajo, memoria propia del agente;
  ver abajo). Este es el estado autoritativo. Un proceso que lo pierde no
  "reinició un agente", destruyó uno.
- **Efímero** (existe únicamente mientras atiende una solicitud): el objeto
  Python `AgentRuntime` en sí. Se *activa* — se construye a partir de la
  identidad durable más el `EquippedRuntime` actual de ese rol — cuando
  llega un mensaje, se usa para exactamente un `run_turn`, y luego se
  descarta. El objeto en memoria nunca es la fuente de verdad de nada; si
  desaparece entre turnos, no se pierde nada, porque todo paso relevante ya
  fue persistido antes de que el objeto desapareciera.

### Por qué falla un objeto literal, de larga vida, dentro del proceso

La alternativa tentadora — mantener un objeto `AgentRuntime` vivo por
usuario, mientras esté activo, en memoria del proceso — se rompe bajo
condiciones que esta plataforma ya tiene o busca explícitamente:

- **Un reinicio del proceso pierde el objeto y todo lo que retenía en
  memoria que no se haya persistido por separado.** ADR-001 ya establece
  que la memoria de trabajo (el checkpointer de Redis) es la única pieza de
  estado que hoy sí sobrevive a un reinicio, precisamente *porque* vive
  fuera del proceso. Todo lo que solo esté en el objeto Python — incluyendo
  "qué instancia de `AgentRuntime` pertenece a este usuario" — no sobrevive.
- **Múltiples workers de uvicorn o múltiples procesos servidor dividen al
  mismo usuario entre procesos.** El §2 de ADR-001 ("Etapa B — objetivo, 100
  conversaciones concurrentes: múltiples procesos") es el disparador
  concreto: en el momento en que hay más de un proceso, "el objeto en
  proceso para el usuario X" deja de estar bien definido — ¿qué proceso lo
  tiene? Un balanceador de carga no lo sabe ni le importa, y fijar un
  usuario a un proceso (*sticky sessions*) reintroduce exactamente el tipo
  de acoplamiento compartido y con estado por proceso que todo el análisis
  de ADR-001 buscó eliminar.
- **1.000 usuarios inactivos significarían 1.000 objetos vivos sentados en
  memoria sin hacer nada** — exactamente el "enjambre siempre activo" que
  el manifiesto rechaza explícitamente ("No es un enjambre... Se instancian
  bajo demanda... No hay un enjambre permanente por defecto",
  `docs/platform_es/manifesto.md:20,40`). Una identidad durable con una
  activación efímera logra la misma direccionabilidad (cualquier solicitud
  para el agente X puede encontrar al agente X) sin pagar el costo de
  mantener 1.000 objetos Python inactivos residentes.

### La activación es barata

Dos agentes con el mismo rol y el mismo conjunto efectivo de permisos pueden
compartir el mismo `EquippedRuntime` *sin estado*: la superficie de
herramientas ensamblada, el prompt de sistema y el conjunto de habilidades
son idénticos para ambos, porque nada de eso depende de qué principal está
preguntando. Solo la identidad (`agent_id`, `principal`) y la memoria son
por agente. Por eso la activación puede ser barata: construir un
`AgentRuntime` alrededor de un `EquippedRuntime` ya construido es vincular
un modelo a un esquema de herramientas existente (`agent/graph.py:373-375`),
no volver a resolver un rol desde disco. El propio principio de rendimiento
del manifiesto ya anticipa esto: "La instanciación del agente (inicio en
frío) debe ser lo suficientemente barata como para que la creación bajo
demanda sea el comportamiento por defecto" (`docs/platform_es/manifesto.md:69`)
— un costo bajo por activación es lo que hace viable el par
identidad-durable-más-activación-efímera, en vez de convertirlo en un
lastre de rendimiento.

## Tres tipos de memoria

| Tipo | Qué guarda | Aislamiento | ESTADO |
|---|---|---|---|
| **Memoria de trabajo** | El historial de mensajes de la conversación actual — lo que permite que un intercambio multi-turno se mantenga coherente dentro de un mismo hilo. | Por `thread_id` | ⚠️ parcial — el checkpointer de Redis de LangGraph guarda esto, indexado por `thread_id` (el número de teléfono de un cliente en WhatsApp, `webhook.py:246`, cuando `whatsapp_checkpointer_enabled`). Ver ADR-002 §D.4/G para su falta de durabilidad (no hay persistencia detrás de Redis) y su TTL. |
| **Memoria propia de largo plazo del agente** | Hechos durables *sobre el principal de este agente*, acumulados y curados a través de conversaciones — no la transcripción cruda, sino lo que el agente decidió que valía la pena retener ("este cliente prefiere entregas los martes"). | Por `agent_id` | ⏳ pendiente — no existe. No hay herramienta `remember`/`recall`, ni almacenamiento, ni esquema. Ver ADR-002 §G.25. |
| **Conocimiento compartido de la organización** | Hechos que la organización posee y que no son memoria privada de ningún agente en particular — el catálogo de productos, documentos de política, una wiki. No es propiedad del agente; el agente la *consulta* mediante una herramienta. | Por organización, filtrado por los permisos del agente que consulta | ⚠️ parcial — `KnowledgeBase` (`services/knowledge.py:23`) es un puerto `Protocol` sin implementación concreta en ninguna parte del repositorio, ni siquiera un doble de prueba; `platform_connectors.py` la vincula como una herramienta que falla en modo cerrado ("no knowledge base is available on this deployment") cuando no hay nada conectado. |

**El aislamiento por `agent_id` es una frontera impuesta por la plataforma,
nunca algo que el modelo elige.** Esto refleja la propia regla del modelo de
permisos para el acceso a herramientas (`docs/architecture/permission-model.md`
— el filtrado ocurre en el momento de la inyección, no porque el modelo
decida respetar una frontera) y es la razón por la que el aislamiento de
memoria, una vez implementado, debe controlarse de la misma manera en que
`memory_policy.read_scope`/`write_scope` ya declaran la intención de
controlarlo (ADR-002 §A.5): de forma declarativa, en la capa de la
plataforma, no pidiéndole al modelo que se comporte bien mediante el prompt.

## Referencias cruzadas

- Esquema de definición de roles: `docs/platform_es/role.md`
- Pipeline del runtime (disparador → ejecución de herramientas): `docs/platform_es/harness.md`
- Política (autonomía, límites, capas de aplicación): `docs/platform_es/policy.md`
- Modelo de permisos y RBAC: `docs/architecture/permission-model.md`
- Las correcciones de arquitectura en que se fundamenta esta definición: `docs/architecture_es/adr-002-agent-model-and-capabilities.md`
