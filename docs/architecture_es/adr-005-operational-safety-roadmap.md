# ADR-005 — Hoja de ruta de seguridad operacional y gobernanza

**Estado:** Propuesto · **Fecha:** 2026-09-25

## Resumen

Esta es una hoja de ruta, no un diseño: un relevamiento de nueve áreas de
seguridad operacional contra el código tal como está hoy, cada una con lo que
existe (con referencias file:line, verificadas contra el árbol actual), el
vacío, una dirección propuesta breve y una prioridad. No propone
implementación ni crea issues. Donde una capacidad no existe, esto lo dice
llanamente en vez de asumirla.

## Contexto

ADR-003 cerró el modelo de tiers de permisos. ADR-004 (reservado) definirá el
contrato de library-first-agents — roles predefinidos que un proyecto
downstream instala y no puede reformar silenciosamente. Entre esos dos,
varias cuestiones operacionales quedaron diferidas en vez de decididas: qué
detiene realmente a un agente antes de actuar, qué acota un turno
descontrolado, qué se registra y por cuánto tiempo, y qué ve un humano antes
de que salga una versión MAJOR. Este ADR mapea esa superficie una vez, para
que los cambios de seguimiento tengan una referencia compartida en vez de
redescubrir el mismo código cada uno por su cuenta.

## Hoja de ruta

### 1. Seguridad

**Qué existe:** una jerarquía de tiers explícita `T0`–`T3`
(`harness/registry.py:9-33`); la barrera `untrusted_input` que rechaza
grants T3 para roles no confiables (`harness/loader.py:1580` al cargar, `harness/factory.py:349-361` al otorgar, ADR-003
R4). Toda llamada T3 corre dentro de un sandbox `bwrap` con límites vía
`prlimit` y sin fallback sin sandbox (`docs/operations/sandbox-bwrap.md`,
ADR-002 C.14). Los secretos se cargan vía `pydantic-settings`
(`config.py:55-91`) y el arranque falla cerrado ante un `meta_webhook_secret`
vacío o un adapter expuesto sin `adapter_api_key` (`config.py:219-245`). La
autenticación del webhook es HMAC (`integration/meta_signature.py:14`,
invocada en `integration/webhook.py:130`); la del adapter es un chequeo
Bearer de tiempo constante (`integration/openai_adapter.py:82-108`). Las CVE
de dependencias se rastrean como los issues #32–#35, contenidas por el job de
CI `dependency-audit` con líneas `--ignore-vuln` por paquete y PRs semanales
de Dependabot (`.github/dependabot.yml`).

**Vacío:** no existe un documento de modelo de amenazas consolidado.
`untrusted_input` tiene un test de invariante estructural
(`tests/test_untrusted_input_invariant.py`) pero ningún corpus de evals
adversariales de prompt-injection. Los issues #32–#35 siguen abiertos a la
espera de migraciones mayores coordinadas.

**Dirección:** agregar un documento breve de modelo de amenazas que nombre
los límites de confianza, más un set mínimo de evals adversariales de
prompt-injection.

**Prioridad:** P1 · **Dependencia:** ninguna para el documento; #32 bloquea
el cierre completo de CVE hasta la migración de LangChain/LangGraph a 1.x.

### 2. Guardas

**Qué existe:** Layer-1 excluye herramientas no otorgadas en tiempo de
construcción (`harness/injector.py:122`). El `intercept()` de Layer-2
revalida herramientas sensibles contra el `deploy_grant_ceiling` persistido,
no contra el conjunto completo declarado por el rol
(`harness/interceptor.py:77-149`; el techo se construye en
`harness/factory.py:380` y se lee en `agent/graph.py:478`).
`escalation_rules` se renderiza en el system prompt solo como guía dirigida
al modelo (`harness/factory.py:199-227`) — no es una compuerta impuesta por
código. `EscalationChannel` (`services/escalation.py:24`) y `OrderWriter`
(`services/orders.py:24`) son ambos `Protocol` vacíos sin implementación de
referencia.

**Vacío:** no existe ninguna interrupción con humano en el circuito antes de
una acción T2/T3; hoy nada detiene efectivamente la escritura de un pedido a
la espera de confirmación. `intercept()` valida solo `tool_name` y permisos —
los *argumentos* de la herramienta llegan al conector sin chequeo más allá
del tipado propio de la llamada del modelo.

**Dirección:** agregar un paso de confirmación real que bloquee las
escrituras T2 detrás de `EscalationChannel`, y un chequeo declarativo de
política sobre argumentos evaluado en `intercept()` antes del despacho.

**Prioridad:** P1 · **Dependencia:** una implementación de referencia mínima
de `OrderWriter` para tener algo concreto que bloquear.

### 3. Límites de ejecución

**Qué existe:** `_validate_execution_limits` (`harness/loader.py:1611-1645`)
impone que un override solo puede ajustar `_PLATFORM_DEFAULT_LIMITS` hacia
abajo (`harness/loader.py:156-162`: `tool_call_timeout_s=10`,
`total_execution_timeout_s=60`, `max_tool_calls=20`,
`max_delegation_depth=2`, `max_clarification_attempts=3`).
`_effective_limits` (`agent/graph.py:61-74`) mezcla por clave sobre esos
valores por defecto; el grafo impone `max_tool_calls` y ambos timeouts, pero
no `max_delegation_depth`/`max_clarification_attempts` (señalado
directamente en `agent/graph.py:53-54`). El `lifespan` de `main.py` construye
un único `TurnAdmissionLimiter` de alcance de proceso
(`services/admission.py:48-56`, cableado en `main.py:112`), que acota solo
los turnos concurrentes.

**Vacío:** no hay presupuesto de tokens/costo por turno ni por conversación,
no hay rate limit por principal, no hay detección de loop/repetición más
allá del techo plano de `max_tool_calls`, no hay circuit breaker ante fallos
repetidos de herramienta o proveedor.

**Dirección:** agregar contabilidad de tokens/costo al estado del turno, un
limitador por principal junto a `TurnAdmissionLimiter`, y un circuit breaker
por fallos consecutivos por herramienta.

**Prioridad:** P1 · **Dependencia:** ninguna; extiende directamente los
puntos de extensión ya existentes de `_effective_limits`/
`TurnAdmissionLimiter`.

### 4. Gobernanza de datos

**Qué existe:** `Redactor` siempre elimina teléfonos y emails de los
payloads de auditoría, y redacta `message`/`body`/`text` salvo que
`audit_policy.capture_tool_input` esté activo (`audit/redactor.py:24-45`).
`audit_event` es una tabla particionada mensualmente por rango, con una
partición DEFAULT para que las escrituras nunca fallen cerradas ante una
partición faltante (`alembic/versions/001_add_audit_event.py`); el estado
durable del outbox vive en `models/outbox.py`/`services/outbox.py`.
`role_is_read_only` (`services/db_role.py:19`) le pregunta directamente a
Postgres si el rol BI puede escribir, verificado en CI por el job
`bi-readonly`.

**Vacío:** no hay política de retención o borrado para filas de
`audit_event`/outbox — el particionado existe para poda de queries, no para
retención automatizada. No hay clasificación de datos más allá del `Tier` de
peligrosidad de herramientas. `deployments/` está vacío en este repo público
(solo README), por lo que el aislamiento de datos por deployment es una
convención, no código que lo imponga.

**Dirección:** agregar un job programado de drop de particiones/retención
con un período documentado, y una nota breve de clasificación de datos que
separe PII de datos operacionales.

**Prioridad:** P2 · **Dependencia:** ninguna.

### 5. Logging

**Qué existe:** `structlog` configurado para salida JSON con timestamps ISO
(`observability/logging.py:16-27`); los eventos siguen la convención
`componente.nombre_evento` (`loader.invariant_violation`,
`interceptor.call_blocked`, `health.postgres_error`). El `request_id` se
genera por cada request HTTP entrante y se liga a los contextvars de
structlog vía middleware (`observability/middleware.py:22-23`);
`audit/recorder.py` lo relee como el id de correlación de auditoría, con
`"none"` por defecto cuando está ausente (`audit/recorder.py:60-62`).
`Redactor` es el mismo camino de redacción para los eventos de auditoría.

**Vacío:** `services/webhook_worker.py` nunca liga `request_id` (ni ningún
id de conversación) al contexto de structlog, así que un turno que el
worker del outbox ejecuta más tarde loguea y audita con id de correlación
`"none"` — la cadena webhook → outbox → turno hoy no está realmente
correlacionada, solo lo está el manejo del request HTTP inicial.

**Dirección:** ligar un id estable de conversación/fila de outbox a los
contextvars de structlog al inicio del loop de claim del worker, y
propagarlo a cada llamada `record_*` de ese turno.

**Prioridad:** P1 · **Dependencia:** ninguna.

### 6. Observabilidad

**Qué existe:** `langsmith` está presente solo como dependencia transitiva
de `langchain-core` vía `langgraph` (`uv.lock:956`) — no es una dependencia
directa de `pyproject.toml`. No existe ningún import de `opentelemetry` ni
de `langsmith`/`LANGCHAIN_TRACING` en ningún lugar de `src/`. No existe
librería de métricas ni endpoint `/metrics` en `main.py`.
`evals/reporting.py::write_results` escribe los resultados de evals a un
archivo; nada en `src/` vuelve a leer esa salida hacia una señal en tiempo
de ejecución.

**Vacío:** cero trazas, cero métricas; los resultados de evals son un
artefacto aislado, desconectado del monitoreo en tiempo de ejecución.

**Dirección:** cablear el tracing de LangSmith detrás de un flag explícito
de settings primero (la dependencia ya está resuelta), y luego agregar un
set mínimo de contadores (llamadas a herramientas, denegaciones, límites
excedidos) antes de cualquier despliegue más amplio de OpenTelemetry.

**Prioridad:** P3 · **Dependencia:** ninguna bloqueante; los contadores que
producirían las áreas 3 y 2 son lo que consumirían las alertas del área 7.

### 7. Monitoreo y alertas

**Qué existe:** `GET /health` (`main.py:641-720`) sondea Postgres y Redis
con timeout de 3s, informa `webhook_worker.running` y los conteos
`pending`/`leased`/`leased_expired` del outbox, y degrada — no solo informa —
cuando hay trabajo pendiente o con lease vencido y ningún worker corriendo
(`main.py:686-706`, issue #141).

**Vacío:** no existe documento de SLO. Nada sondea `/health` y alerta sobre
él — es pull-only; no existe ningún camino de alertas push (email, Slack,
PagerDuty o similar) en ningún lugar del repo. No hay condiciones de alerta
definidas para picos de denegación de herramientas, límites excedidos, ni
errores de proveedor/anomalías de costo, porque las áreas 3 y 6 todavía no
emiten esas señales. Una vez que exista el área 9, hay otras cuatro
condiciones sin ningún hook: saturación sostenida de admisión (`in_flight`
en `max_concurrent_turns`, `services/admission.py:62-77`), antigüedad del
backlog del outbox más allá de su sola cuenta (`count_outbox_backlog` solo
cuenta, `services/outbox.py:217-264`), conteos de disparo de cuota por
participante, y tokens/costo por turno.

**Dirección:** definir 2–3 SLO iniciales (latencia de procesamiento del
webhook, tiempo en estado degradado) y cablear `/health` a una herramienta de
alertas como primer camino; agregar alertas de pico de denegación y de
límite excedido una vez que las áreas 3 y 6 estén resueltas, y alertas de
saturación de admisión, antigüedad de backlog, disparo por participante y
costo por turno una vez que aterrice el área 9.

**Prioridad:** P2 · **Dependencia:** las áreas 3, 6 y 9 para las condiciones
de alerta más ricas; el SLO basado en `/health` puede arrancar de forma
independiente.

### 8. Gobernanza de agentes

**Qué existe:** el SemVer/CHANGELOG está automatizado vía release-please
(`.github/workflows/release-please.yml`, `release-please-config.json`); el
issue #18 está cerrado y la versión ahora se deriva de los prefijos de
Conventional Commits (`docs/operations/release-process.md`). Los eventos
`ToolGranted`/`ToolDenied` se registran en tiempo de inyección
(`audit/events.py:50-63`, `audit/recorder.py:179-236`) — existe un rastro de
grant/denegación a nivel de herramienta. No existe `CONTRIBUTING.md` ni
template de PR; la revisión corre por el review de PR habitual de GitHub más
los checks requeridos `ci`/`secret-scan`/`dependency-audit`.

**Vacío:** nada inspecciona un diff y fuerza un bump MAJOR
(`feat!:`/`fix!:`) cuando los `tools:`/`permissions:` de un rol predefinido
se amplían — un cambio de manifiesto podría salir hoy bajo un commit
`feat:`/`fix:` ordinario. Esto es exactamente la preocupación de
library-first-agents (ADR-004).

**Dirección:** agregar un check de CI que compare las listas de
tools/permissions de `platform/roles/**/manifest.md` contra `main` y exija
un marcador de breaking change cuando se amplían; que aterrice junto con
ADR-004.

**Prioridad:** P2 · **Dependencia:** ADR-004 define el contrato de
rol/versión que este check impondría.

### 9. Concurrencia y escala

**Qué existe:** los mensajes entrantes de WhatsApp se persisten de forma
durable en el outbox de Postgres y el webhook responde de inmediato
(`services/outbox.py:88-143`); el worker reclama solo tantas filas como
slots libres de `TurnAdmissionLimiter` haya
(`services/webhook_worker.py:194-198`) — backpressure, nunca rechazo — y
`pending_outbox_statement` (`services/outbox.py:164-214`) impone un orden
estricto por `conversation_key`, así que los mensajes de un mismo remitente
siempre corren de a uno. `TurnAdmissionLimiter`
(`services/admission.py:45,48-111`, `DEFAULT_MAX_CONCURRENT_TURNS = 10`) se
construye una sola vez por proceso (`main.py:112-114`) y se comparte, vía
`app.state`, entre el worker del webhook y el adapter de OpenAI. El
historial por conversación vive en un checkpointer `AsyncRedisSaver` de
LangGraph, con clave el número de teléfono normalizado
(`services/webhook_worker.py:299`), gobernado solo por un TTL temporal
(`checkpointer_ttl_s = 86400`, `refresh_on_read: True`, `config.py:106`,
`main.py:41-47,69-71`) — no por un límite de tamaño o de tokens.
`AgentRuntime` liga los schemas de herramientas una sola vez en su
construcción (`agent/graph.py:379-382`), pero cada llamada a
`bound_model.ainvoke()` igual transmite el system prompt, esos schemas y el
`state["messages"]` acumulado completo, de nuevo, por la red
(`agent/graph.py:97`).

**Vacío:** al examinar esto a escala de 100 remitentes concurrentes
aparecen cuatro riesgos que se potencian entre sí. (1) La admisión se
impone por *proceso* vía un contador en memoria
(`services/admission.py:56`): N réplicas permiten cada una hasta 10 turnos
concurrentes y hasta 15 conexiones a la base
(`pool_size=5 + max_overflow=10`, `models/base.py:86-88`, valores por
defecto de SQLAlchemy), sin nada que acote la suma entre réplicas. (2) No
existe cuota por remitente ni por deployment — un remitente insistente o
abusivo consume slots de admisión exactamente igual que cualquier otro,
porque tanto `TurnAdmissionLimiter` como `pending_outbox_statement` son
ciegos al principal. (3) `AgentState.messages` usa el reducer
`add_messages` de LangGraph sin ningún tope (`agent/state.py:11`);
combinado con el `refresh_on_read` del TTL, el historial checkpointeado de
una conversación activa nunca se recorta, ventanea ni resume — crece sin
límite mientras la conversación siga activa, elevando costo, latencia y el
riesgo eventual de error por ventana de contexto agotada. (4) Ese contexto
sin límite y sin cachear se reenvía en cada llamada al modelo, así que el
costo y la latencia por turno crecen tanto con la edad de la conversación
como con el volumen concurrente; el #59 ya recortó una fuente de esto para
la tool `run_report` (`connectors/report_connector.py:116-148`, cierra el
#56), pero no existe caching ni medición más amplios.

**Dirección:** (1) un limitador de admisión compartido y distribuido
(respaldado en Postgres o Redis), o un techo global al momento del claim
entre réplicas. (2) cuotas por participante y por deployment impuestas al
momento del claim, con una señal visible para el operador cuando una cuota
se dispara. (3) una política de ventaneo o resumen por rol, aplicada antes
de que `_call_model` arme `model_input`. (4) prompt caching del lado del
proveedor para el prefijo estático, más recorte de tool schemas, y medir
tokens por turno como una métrica real.

**Prioridad:** P1 · **Dependencia:** ninguna; cada punto extiende un punto
de extensión ya existente (`TurnAdmissionLimiter`, `pending_outbox_statement`,
`_effective_limits`) directamente.

## Secuenciación

1. IDs de correlación de punta a punta — ligar un id de conversación/outbox
   al contexto de structlog del webhook worker (área 5).
2. Documento de modelo de amenazas + corpus de evals de prompt-injection —
   sin cambio de código, informa la revisión de todo lo que sigue (área 1).
3. Confirmación con humano en el circuito para acciones T2/T3, más chequeos
   de política sobre argumentos en `intercept()` (área 2).
4. Presupuestos de tokens y costo por turno/conversación, rate limiting por
   principal, y un circuit breaker (área 3).
5. Limitador de admisión distribuido, cuotas por participante/deployment,
   ventaneo del historial de conversación, y reducción de costo por
   llamada al modelo (área 9).
6. Seguir cerrando #32–#35 a medida que aterrizan sus migraciones
   coordinadas (área 1, ya rastreado).
7. Job de retención de audit/outbox y una nota breve de clasificación de
   datos (área 4).
8. Flag de tracing de LangSmith y un set mínimo de contadores en `/metrics`
   (área 6).
9. SLOs y alertas basadas en `/health`, extendidas con los contadores de 4,
   7 y 9 (área 7).
10. Check de CI que imponga un bump MAJOR ante cambios de tools/permissions
    de rol, junto con ADR-004 (área 8).
