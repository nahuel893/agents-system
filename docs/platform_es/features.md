# Referencia de funcionalidades

Todas las funcionalidades del kit en una sola página, con el nombre para llegar a cada una y la página que la explica. Gemelo en inglés: [`docs/platform/features.md`](../platform/features.md).

**agents-system** es un kit de desarrollo para construir agentes de IA. Se describe un agente (un prompt de sistema, las herramientas que puede llamar y la política que lo acota), se le entregan las herramientas propias y el kit arma alrededor un runtime acotado por permisos. El camino más corto hacia un primer agente es una herramienta propia en unas diez líneas: ver el [Inicio rápido](library-usage.md#inicio-rápido).

## Cómo leer esta página

- Cada fila de una tabla es una funcionalidad. **Cómo usarla** nombra una clase, función, setting o archivo que existe en `main`.
- **Extra necesario** es el extra de instalación (ver [Extras](library-usage.md#extras)). `Ninguno` significa que alcanza con el núcleo liviano.
- Una funcionalidad marcada **llega en el próximo release** ya está en `main`, pero no en el último release, `v0.4.0`. Todo lo demás está en `v0.4.0`.
- Los settings son variables de entorno. `CHECKPOINTER_RETENTION_S` es el setting `checkpointer_retention_s`, y así con el resto.
- Lo que el kit todavía no hace está al final, en [Todavía no está en el kit](#todavía-no-está-en-el-kit). Nada de eso está disponible.

## Definir agentes

| Funcionalidad | Qué hace | Cómo usarla | Extra necesario | Docs |
|---|---|---|---|---|
| Agentes en Python | Define un agente como un objeto inmutable: nombre, herramientas, permisos, skills, campos de política, modelo. Se valida al construirlo. | `Agent(name=..., extends=..., tools=[...], permissions=[...])` | Ninguno | [Rol](role.md), [Uso como biblioteca](library-usage.md) |
| Agentes en carpetas Markdown | Define un agente como una carpeta con `role.md` (prompt), `manifest.md` (herramientas, permisos, skills) y `policy.md` (autonomía, escalamiento, límites). | `Agent.from_folder(path, **overrides)`; `resolve(...)` devuelve el `AgentDefinition` | Ninguno | [Rol](role.md), [Política](policy.md) |
| Roles predefinidos | Incluye roles reutilizables: `sales-agent`, `support-agent`, `data-agent`, `summary-agent`, `accountant-agent`, `orchestrator`, `developer-agent`, `operator-agent`. Los roles `base` y `agent` son sus padres. **Llega en el próximo release:** nombrar uno de los ocho roles concretos está obsoleto y emite un `PredefinedRoleDeprecationWarning` que nombra su agente de referencia; `base` y `agent` se quedan. | `Agent(extends="sales-agent")`, o `build_runtime("sales-agent", ...)`; archivos en `platform/roles/` | Ninguno | [Rol](role.md#agentes-de-referencia), [Uso como biblioteca](library-usage.md) |
| Agentes de referencia (**llega en el próximo release**) | Los ocho roles concretos como carpetas de agente para copiar: ejemplos, no API soportada, y fuera del wheel. Cada uno se resuelve como tu propia carpeta, dentro de las reglas que sigue toda carpeta de agente; cinco difieren de su rol para cumplirlas. | Copiá `examples/agents/<nombre>/` a tu proyecto y usá `Agent.from_folder(path)` | Ninguno | [Rol](role.md#agentes-de-referencia), [ADR-010](../architecture_es/adr-010-capability-evals-and-reference-agents.md) |
| Servir una carpeta de agente desde el entorno (**llega en el próximo release**) | Un valor `"folder:<nombre>"` de `AGENT_REGISTRATIONS` sirve una carpeta de agente bajo una raíz configurada, como una copia de un agente de referencia. Una raíz sin definir, una ruta absoluta, `..` o un symlink fuera de la raíz hace fallar el arranque. La demo registra el `sales-agent` de referencia. | `AGENTS_ROOT`, `AGENT_REGISTRATIONS={"id": "folder:<nombre>"}` | `server` más un extra de proveedor | [Deployment](deployment.md#servir-una-carpeta-de-agente-agents_root-próxima-versión), [Demo](demo-entrypoint.md) |
| Herencia | Un rol extiende a un padre o a otro `Agent`. La capacidad se suma; la seguridad solo puede endurecerse. | `extends=` en `Agent`, o `extends:` en `role.md` | Ninguno | [Rol](role.md) |
| Overrides por cliente | Un deployment de cliente sobrescribe el prompt, las herramientas, las skills y la política de un rol sin tocar el núcleo. Un override solo puede acotar permisos y límites. | `build_runtime(..., client="my-client", roots=RootConfig(deployments_root=...))`; carpeta `deployments/<client>/<role>/` | Ninguno | [Deployment](deployment.md), [Uso como biblioteca](library-usage.md) |
| Skills | Agrega instrucciones reutilizables a un rol, desde archivos o texto en línea. | `skills:` en `manifest.md`; `Agent(skills=[...], skill_contents={...})` | Ninguno | [Skill](skill.md) |
| Modelo por agente | Un agente puede declarar su propio proveedor y modelo, validados al construirlo. | `Agent(model={"provider": ..., "model": ...})`, o `model:` en `policy.md` | El extra del proveedor: `openai`, `anthropic`, `groq` u `ollama` | [Política](policy.md), [Uso como biblioteca](library-usage.md) |
| Construir el modelo declarado en el mismo proceso (**llega en el próximo release**) | Construye el modelo de chat que declara un agente a partir de los settings y el entorno, sin servidor. `AgentRuntime` lo usa cuando no se le pasa un modelo. | `agents_system.build_agent_model(definition)` | El extra del proveedor | [Uso como biblioteca](library-usage.md) |

## Herramientas y conectores

| Funcionalidad | Qué hace | Cómo usarla | Extra necesario | Docs |
|---|---|---|---|---|
| Herramientas propias | Una herramienta es un `ToolSpec` (nombre, permisos requeridos, tier, esquema de entrada, conector async) guardado en un registro. El conector lo ejecuta tu código: base de datos, API, lo que sea. | `ToolRegistry`, `ToolSpec`, `Tier` | Ninguno | [Herramienta](tool.md), [Uso como biblioteca](library-usage.md) |
| Tiers de capacidad | Cada herramienta declara cuán peligrosa es: T0 inherente, T1 lectura acotada, T2 escritura, envío o consulta escrita por el modelo, T3 ejecución en el host. Un tier que subestima un permiso se rechaza. | `ToolSpec(tier=Tier.T2, ...)` | Ninguno | [Herramienta](tool.md) |
| Herramientas de comando declarativas | Declara una línea de comando fija en un manifiesto. El modelo completa solo los parámetros declarados, cada uno validado contra un patrón o una lista. El comando corre sin shell, dentro de un sandbox bubblewrap. | `command_tools:` en `manifest.md` (permiso `run:<name>`, tier T2); `build_command_tool_specs` | Ninguno (el host necesita `bwrap`) | [Herramienta](tool.md), [Sandbox](../operations_es/sandbox-bwrap.md) |
| Herramienta SQL de solo lectura | Permite al modelo escribir una consulta de solo lectura sobre una lista de vistas permitidas (o, con una opción, tablas base). Un guardia rechaza todo lo demás y un rol de base de datos hace cumplir los límites. | `build_sql_query_tool_spec(engine, SqlQueryConfig(...))`; permiso `query:sql`; `scripts/provision_sql_readonly.sql` | `sql` | [ADR-007](../architecture_es/adr-007-read-only-sql-tool.md), [Herramienta](tool.md) |
| Herramientas de operador | Una shell y un lector de archivos para el rol `operator-agent`, bajo una política de sandbox y de terminal. Ejecución en el host (T3). | `build_operator_tool_specs`, `TerminalPolicy`, `SandboxPolicy` | Ninguno (el host necesita `bwrap`) | [Sandbox](../operations_es/sandbox-bwrap.md), [Herramienta](tool.md) |
| Backends de referencia | Implementaciones opcionales de las herramientas que declaran los roles predefinidos: búsqueda en catálogo, consulta de clientes, reportes de ventas, envío de mensajes, base de conocimiento en memoria, escritor de pedidos, resumidor. | `ReferenceBackends` y sus métodos `*_tool_spec()`; `InMemoryKnowledgeBase`, `InMemoryOrderWriter` | `postgres` (todo el módulo lo necesita, incluidas las clases en memoria) | [Backends de referencia](reference-backends.md) |
| Recuperación de catálogo | Búsqueda sobre un catálogo de productos que aporta el usuario: por vectores cuando hay un embedder disponible, por palabras clave en caso contrario. | `build_catalog_rag_connector(embedder, settings, source)` con tu `CatalogSource`; `search_catalog`; `EMBEDDING_PROVIDER` | `postgres`, más `embeddings` para vectores | [Uso como biblioteca](library-usage.md#extras) |
| Escalamiento incorporado | En el mismo proceso, un rol que hereda `escalation_notifier` recibe un valor por defecto que no llega a ninguna persona, registra el intento y guarda un evento de auditoría, de modo que un primer agente se construye sin conectar nada. La app servida nunca lo usa: el arranque falla hasta que se registre el propio. | `build_runtime(..., builtin_defaults=False)` para desactivarlo; `EquippedRuntime.builtin_defaults` | Ninguno | [Uso como biblioteca](library-usage.md), [Auditoría](audit.md) |

## Permisos y seguridad

| Funcionalidad | Qué hace | Cómo usarla | Extra necesario | Docs |
|---|---|---|---|---|
| Familias de permisos | Los permisos son cadenas `familia:recurso`: `read`, `write`, `send`, `exec`, `run`, `spawn`, `query`. Cada familia tiene un tier. | `agents_system.permissions.builtins` (`Read`, `Write`, `Send`, `Exec`, `Run`, `Spawn`, `Query`) | Ninguno | [Modelo de permisos](../architecture_es/permission-model.md), [Herramienta](tool.md) |
| Grants (barrera en la construcción) | Una herramienta llega al modelo solo si el rol declara su permiso y quien llama lo tiene: `permisos del rol ∩ grants`. Las herramientas denegadas nunca llegan al modelo. | `build_runtime(role, registry, granted_permissions=[...])` | Ninguno | [Harness](harness.md), [Política](policy.md) |
| Interceptor en tiempo de llamada | Revalida cada llamada con los permisos vigentes antes de que corra el conector. Todas las herramientas T2 y T3 se revalidan siempre. | `intercept(...)`, `PolicyViolation`; `always_revalidate` en `ToolSpec` | Ninguno | [Interceptor](../platform/interceptor.md) (solo en inglés), [Política](policy.md) |
| Entrada no confiable | Marca un rol como lector de entrada externa. Nunca puede tener `exec:*` ni recibir una herramienta T3. Una vez en true, sigue en true en toda la cadena. | `untrusted_input: true` en `policy.md`; `Agent(untrusted_input=True)` | Ninguno | [Política](policy.md) |
| Grants de deploy | Un runtime servido recibe un techo de permisos explícito. No hay grant automático, y el arranque falla sin uno. | `DEPLOY_GRANTS`; `create_app(grants=...)` | `server` | [Deployment](deployment.md) |
| Valores por defecto cerrados | El adaptador y `/metrics` se niegan a arrancar abiertos salvo que se opte por un modo inseguro. Los cuerpos de webhook tienen tamaño máximo y se verifica su firma. | `ADAPTER_API_KEY`, `METRICS_API_KEY`, `ALLOW_INSECURE`, `META_WEBHOOK_SECRET`, `WEBHOOK_MAX_BODY_BYTES` | `server` | [Observabilidad](observability.md), [Canales](channel.md) |
| Redacción en la auditoría | Los eventos de auditoría pasan por un redactor antes de guardarse. | `Redactor` | `postgres` (para guardar) | [Auditoría](audit.md) |

## Runtime

| Funcionalidad | Qué hace | Cómo usarla | Extra necesario | Docs |
|---|---|---|---|---|
| Runtime del agente | Ejecuta un turno: llamada al modelo, llamadas permitidas a herramientas, respuesta final. Construido sobre LangGraph. Funciona en tu proceso, sin servidor. | `AgentRuntime(equipped, model=...)`, `await runtime.run_turn(messages, session_id=...)` | Ninguno | [Uso como biblioteca](library-usage.md), [Harness](harness.md) |
| Límites de ejecución | Acota las llamadas a herramientas, el tiempo por llamada y el tiempo total del turno. Un turno que agota el tiempo informa el timeout y conserva su uso. Los overrides solo pueden ajustar los límites hacia abajo. | `execution_limits:` en `policy.md`; `tool_call_timeout_s`, `total_execution_timeout_s`, `max_tool_calls` | Ninguno | [Política](policy.md) |
| Uso y costo por turno | Devuelve el uso de tokens y una estimación de costo por cada turno, a partir de una tabla de precios que aporta el usuario. | `run_turn_with_usage`, `TurnUsage`; `ModelPrice`, `AgentRuntime(model_prices=...)`, `MODEL_PRICES` | Ninguno | [Uso como biblioteca](library-usage.md), [Evaluación en vivo](live-eval.md) |
| Reparación de llamadas a herramientas (**llega en el próximo release**) | Cuando un turno se corta en medio de una herramienta (un timeout), las llamadas sin respuesta se cierran en el historial que ve el modelo, de modo que el turno siguiente sea válido. | Automática en `AgentRuntime.run_turn` | Ninguno | [Tamaño de contexto](context-size.md) |
| Escalamiento a una persona | Los roles declaran cuándo escalar. La herramienta `escalation_notifier` (permiso `send:escalation`) entrega el caso a un canal que aporta el usuario. | `escalation_rules` en `policy.md`; protocolo `EscalationChannel`, `LoggingEscalationChannel` (referencia) | Ninguno para el protocolo; `postgres` para `LoggingEscalationChannel` | [Política](policy.md), [Backends de referencia](reference-backends.md) |

## Memoria de conversación y contexto

| Funcionalidad | Qué hace | Cómo usarla | Extra necesario | Docs |
|---|---|---|---|---|
| Memoria de conversación durable (**llega en el próximo release**) | Guarda cada conversación en PostgreSQL, de modo que sobrevive a los reinicios. Reemplaza al checkpointer de Redis. | `open_postgres_checkpointer`, `AgentRuntime(checkpointer=...)` junto con `run_turn(..., thread_id=...)`: sin `thread_id` un turno no guarda historial; tablas de la migración `007` | `postgres` | [Memoria de conversación](conversation-memory.md) |
| Retención y purga (**llega en el próximo release**) | Borra, con una frecuencia configurada, las conversaciones inactivas más allá de una ventana. La ventana es un setting propio; `never` las conserva. | `CHECKPOINTER_RETENTION_S`, `CHECKPOINTER_PURGE_INTERVAL_S`; `purge_checkpoints`, `CheckpointPurger` | `postgres` | [Memoria de conversación](conversation-memory.md) |
| Política de fallas (**llega en el próximo release**) | Si el almacén no responde a tiempo, el turno se degrada en lugar de quedar detenido, y una métrica lo cuenta. | `CHECKPOINTER_TIMEOUT_S`, `CHECKPOINTER_POOL_SIZE` | `postgres` | [Memoria de conversación](conversation-memory.md) |
| Presupuesto de contexto (**llega en el próximo release**) | Recorta el historial más antiguo cuando una llamada al modelo superaría un presupuesto de tokens. Registra un evento de auditoría y una métrica. | `max_context_tokens` en `execution_limits`; `trim_history` | Ninguno | [Tamaño de contexto](context-size.md) |
| Aislamiento de conversaciones | Un turno no guarda estado salvo que pases `thread_id`. Con un checkpointer, `thread_id` identifica el historial guardado y los hilos no comparten estado. `session_id` es solo una etiqueta que viaja en el estado del turno. | `AgentRuntime(checkpointer=...)`, `run_turn(..., thread_id=...)` | Ninguno (el checkpointer durable necesita `postgres`) | [Uso como biblioteca](library-usage.md), [Memoria de conversación](conversation-memory.md) |

## Servicio y canales

| Funcionalidad | Qué hace | Cómo usarla | Extra necesario | Docs |
|---|---|---|---|---|
| Servir agentes por HTTP | Construye la app FastAPI para los agentes registrados. | `create_app(registry_factory=..., agents={...}, grants={...})`; `agents_system.server:app` con `AGENT_REGISTRATIONS`, `REGISTRY_FACTORY` | `server` más un extra de proveedor | [Deployment](deployment.md), [Operaciones](../operations_es/deployment.md) |
| API compatible con OpenAI | Expone los agentes registrados como modelos de una API estilo OpenAI. | `GET /v1/models`, `POST /v1/chat/completions`; `ADAPTER_RUNTIMES`, `ADAPTER_API_KEY` | `server` | [Deployment](deployment.md) |
| Puerto de canales | Agrega un canal de mensajería con un solo adaptador: verifica, interpreta y envía. El kit aporta la ruta, la bandeja de entrada, el worker y la auditoría. | `Channel`, `ChannelAdapter`; `create_app(channels={"name": Channel(...)})`; `POST /webhook/<name>` | `server` | [Canales](channel.md) |
| WhatsApp | Un canal incorporado con verificación de firma, handshake de verificación y envío de respuestas. | `WhatsAppChannel`; `WHATSAPP_RUNTIME_ID`, `META_WEBHOOK_SECRET`, `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID` | `server` | [Canales](channel.md) |
| Inbox y outbox durables | Guarda cada mensaje entrante una sola vez, responde al proveedor de inmediato y lo procesa después. El trabajo sobrevive a las caídas y se reintenta. | `accept_inbound_message`, `DeferredWebhookWorker`; `WEBHOOK_WORKER_POLL_INTERVAL_S`; migraciones `002` a `006` | `server` | [Entrega de webhooks](../architecture_es/webhook-delivery.md), [Canales](channel.md) |
| Admisión de turnos | Limita cuántos turnos corren a la vez en un proceso. El worker toma trabajo durable solo cuando hay un lugar libre. | `TurnAdmissionLimiter`; `MAX_CONCURRENT_TURNS`, `ADMISSION_WAIT_TIMEOUT_S` | `server` | [Deployment](deployment.md) |
| Identidad del participante | Asocia el remitente de un canal con una persona conocida. La identidad es por canal, de modo que un canal no puede hacerse pasar por otro. | `ParticipantDirectory`, `ChannelParticipantDirectory`; `create_app(participant_directory=...)` | `server` | [Canales](channel.md) |
| Registrador de conversaciones | Un gancho para guardar la transcripción en un almacén propio. El kit trae solo el protocolo, sin implementación de producción (ver #27 más abajo). | Protocolo `ConversationRecorder`; `create_app(conversation_recorder=...)` | `server` | [ADR-002](../architecture_es/adr-002-agent-model-and-capabilities.md) |
| Proveedores de chat | Anthropic, Groq, Ollama o cualquier endpoint compatible con OpenAI. | `ADAPTER_PROVIDER`; `OPENAI_COMPATIBLE_BASE_URL`, `OPENAI_COMPATIBLE_API_KEY`, `OPENAI_COMPATIBLE_MODEL` | `anthropic`, `groq`, `ollama` u `openai` | [Uso como biblioteca](library-usage.md) |

## Observabilidad

| Funcionalidad | Qué hace | Cómo usarla | Extra necesario | Docs |
|---|---|---|---|---|
| Liveness y readiness | `/health` siempre responde 200 con el estado de los componentes. `/ready` responde 503 cuando cae una dependencia central o el outbox tiene trabajo y ningún worker. | `GET /health`, `GET /ready`; `scripts/healthcheck.sh` | `server` | [Observabilidad](observability.md), [Operaciones](../operations_es/deployment.md) |
| Métricas Prometheus | Cantidad y duración de turnos y llamadas a herramientas, límites alcanzados, memoria y CPU del proceso. Desactivadas salvo que se las habilite. | `GET /metrics`; `METRICS_ENABLED`, `METRICS_API_KEY` | `server` | [Observabilidad](observability.md) |
| Métricas del proveedor | Cuenta y mide las llamadas HTTP al proveedor del modelo, con reintentos y clases de estado. | `MetricsHTTPTransport` | `server` | [Observabilidad](observability.md) |
| IDs de request y logs | Un ID de request en cada request y logs JSON estructurados. | `RequestIdMiddleware`, `setup_logging`; `LOG_LEVEL` | `server` | [Observabilidad](observability.md) |
| Registro de auditoría | Registra grants, denegaciones, llamadas a herramientas, límites alcanzados y eventos de ciclo de vida en una tabla PostgreSQL particionada, en orden por conversación. | `AuditSink.set_current(sink)`; migraciones `001` y `005` | `postgres` | [Auditoría](audit.md) |

## Evaluación

| Funcionalidad | Qué hace | Cómo usarla | Extra necesario | Docs |
|---|---|---|---|---|
| Escenarios YAML | Describe una conversación y lo que debe cumplirse en el resultado: herramientas llamadas, texto, resultados de permisos. | Archivos en `evals/scenarios/`; `load_scenario`, `load_scenarios`, `Scenario` | `postgres` | [Evaluación en vivo](live-eval.md) |
| Destinos de escenario (**llega en el próximo release**) | Un escenario puede correr contra un rol predefinido, una carpeta de agente bajo una raíz configurada, o un `Agent` pasado desde Python. Los resultados y reportes llevan el identificador y el tipo del agente. | `Scenario(agent_folder=...)`, YAML `agent:`; `run_scenario(agent=..., agents_root=...)` | `postgres` y el extra del proveedor | [Evaluación en vivo](live-eval.md) |
| Evaluación en vivo contra un modelo real | Ejecuta escenarios contra un proveedor real, de forma opcional y nunca en las corridas de tests offline. | `pytest -m live`; `EVAL_PROVIDER`; `build_eval_model` | `postgres` y el extra del proveedor | [Evaluación en vivo](live-eval.md) |
| Toda la suite con un comando | Ejecuta todas las fases y escribe logs y un resumen. | `scripts/run-live-suite.sh` (`--quick` para una sola corrida de cada una) | `postgres` y el extra del proveedor | [Evaluación en vivo](live-eval.md) |
| Umbrales de tasa de éxito | Repite cada escenario y aprueba según la proporción que pasa. | `EVAL_RUNS`; `ScenarioGate` | `postgres` y el extra del proveedor | [Evaluación en vivo](live-eval.md) |
| Aserciones de guardrails | Comprueba que un límite se mantuvo: una llamada fue bloqueada, una herramienta no se llamó, un límite se activó, se registró un evento de auditoría. | `ScenarioAssertions`; `evaluate_assertions` | `postgres` y el extra del proveedor | [Evaluación en vivo](live-eval.md) |
| Suite de mecanismos offline (**llega en el próximo release**) | Prueba el interceptor, los límites de ejecución, la auditoría y el sandbox en cada corrida offline con un modelo guionado que siempre intenta la llamada protegida, así una protección no puede quedar sin probar. Cada verificación tiene una gemela que desactiva la protección y debe fallar. | `evals/scenarios/mechanism/`; `tests/test_mechanism_suite.py` | Ninguno | [Evaluación en vivo](live-eval.md) |
| Escenarios de carga | Lanza turnos concurrentes contra la app real con un modelo inyectado. | `pytest -m load`; `create_app(chat_model=...)` | `server` | [Evaluación en vivo](live-eval.md) |
| Archivos de resultados | Escribe resultados, tokens y costo por corrida. | `write_results` | `postgres` | [Evaluación en vivo](live-eval.md) |

## Instalación y empaquetado

| Funcionalidad | Qué hace | Cómo usarla | Extra necesario | Docs |
|---|---|---|---|---|
| Núcleo liviano (**llega en el próximo release**) | Una instalación simple es el núcleo, de unos 76 MB, suficiente para el Inicio rápido. Hasta `v0.4.0`, una instalación simple traía todo. | `uv add "agents-system @ git+https://github.com/nahuel893/agents-system@<tag>"` | Ninguno | [Uso como biblioteca](library-usage.md#extras) |
| Extras (**llega en el próximo release**) | El servidor, la base de datos, la herramienta SQL, los embeddings y cada proveedor de chat son opcionales. | `agents-system[server]`, `[postgres]`, `[sql]`, `[embeddings]`, `[openai]`, `[anthropic]`, `[groq]`, `[ollama]`, `[all]` | según se nombra | [Uso como biblioteca](library-usage.md#extras) |
| Errores de extra faltante (**llega en el próximo release**) | Usar una funcionalidad sin su extra falla de inmediato y nombra el extra que hay que instalar. | `MissingExtraError`; `EXTRA_MODULES` en `agents_system.extras` | Ninguno | [Uso como biblioteca](library-usage.md#extras) |
| Roles dentro del wheel | Los roles predefinidos vienen en el wheel, de modo que quien usa la biblioteca no necesita un checkout. | Wheel construido con `uv build --wheel`; `scripts/smoke-installed-wheel.sh` | Ninguno | [Uso como biblioteca](library-usage.md), [Proceso de release](../operations_es/release-process.md) |
| Releases | Las versiones y el changelog salen de los commits convencionales. | `release-please-config.json`; `CHANGELOG.md` | Ninguno | [Proceso de release](../operations_es/release-process.md) |

## Operaciones

| Funcionalidad | Qué hace | Cómo usarla | Extra necesario | Docs |
|---|---|---|---|---|
| Deploy con Docker | Un stack de producción con Compose: base de datos, Redis, migraciones y luego la app. Los puertos se enlazan a localhost. | `docker-compose.prod.yml`, `Dockerfile` | Ninguno | [Deployment](../operations_es/deployment.md) |
| Deploy con systemd | Units para un host Linux: una migración de una sola ejecución y luego la app. | `deploy/systemd/` | Ninguno | [Deployment](../operations_es/deployment.md) |
| Migraciones | Cambios de esquema con Alembic, ejecutados antes de que arranque la app. | `alembic upgrade head`; `alembic/versions/` | `postgres` | [Deployment](../operations_es/deployment.md) |
| Ejecución en sandbox | Ejecuta las herramientas de operador integradas y las herramientas de comando declarativas en un sandbox bubblewrap en el host. Una herramienta T3 propia corre en el proceso del host, así que su conector debe aplicar su propio sandbox. | `bwrap` en el host; `SandboxPolicy` | Ninguno | [Sandbox](../operations_es/sandbox-bwrap.md) |
| Aprovisionamiento de base de datos | Crea los roles de solo lectura en los que se apoyan las herramientas de reportes y SQL. | `scripts/provision_bi_readonly.sql`, `scripts/provision_sql_readonly.sql` | `postgres` | [ADR-007](../architecture_es/adr-007-read-only-sql-tool.md), [Backends de referencia](reference-backends.md) |
| Demo | Una distribuidora ficticia con datos y una app ejecutable, para ver las piezas funcionando. | `examples/demo/app.py`; `demo/README.md` | `server` | [Entrypoint de demo](demo-entrypoint.md) |

## Todavía no está en el kit

Estos temas están abiertos en GitHub. Cada uno está planificado o en discusión. No se promete ninguna fecha, y ninguno está disponible hoy.

- [#173 Per-agent metrics built into every agent (AgentMetrics)](https://github.com/nahuel893/agents-system/issues/173)
- [#169 MCP client, phase 1 (ADR-008): stdio servers as T2 tools](https://github.com/nahuel893/agents-system/issues/169)
- [#170 ADR-006: agent-to-agent delegation](https://github.com/nahuel893/agents-system/issues/170). El rol `orchestrator` declara permisos `spawn:*` y una política de delegación, pero ningún mecanismo del runtime lanza un agente hijo.
- [#185 AgentRouter: route each task to a low-cost or a high-cost agent by its complexity](https://github.com/nahuel893/agents-system/issues/185)
- [#189 ADR-009: agent versioning and a development lifecycle per agent](https://github.com/nahuel893/agents-system/issues/189)
- Wave 3, modelo de agente y persistencia ([#31](https://github.com/nahuel893/agents-system/issues/31)):
  - [#23 Agent model: durable identity + memory with ephemeral activation (virtual actor)](https://github.com/nahuel893/agents-system/issues/23)
  - [#25 Three memory types with platform-enforced isolation by agent_id](https://github.com/nahuel893/agents-system/issues/25)
  - [#26 Enforce memory_policy (read_scope/write_scope/persist_conversation)](https://github.com/nahuel893/agents-system/issues/26). El campo `memory_policy` se lee, pero no se hace cumplir.
  - [#27 Production ConversationRecorder implementation (Postgres-backed)](https://github.com/nahuel893/agents-system/issues/27)
  - [#29 Agent-own memory: remember/recall tools](https://github.com/nahuel893/agents-system/issues/29)
  - [#30 Reasoning persistence: explicit decision (persist or discard)](https://github.com/nahuel893/agents-system/issues/30)
