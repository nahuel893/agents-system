# Observabilidad — métricas de proceso, turno y herramientas

Issue #78 Phase 0 Slice 2. Números reales sobre cuánto cuesta y cómo rinde
la aplicación corriendo: memoria residente y CPU del proceso, duración de
turnos y de llamadas a herramientas, y un endpoint `GET /metrics` en formato
de texto de Prometheus -- el "conjunto mínimo de contadores (tool calls,
denials, limit trips)" que pide ADR-005 sección 6
(`docs/architecture/adr-005-operational-safety-roadmap.md`) antes de
cualquier despliegue más amplio de OpenTelemetry. El Slice 1 (uso real de
tokens y costo por turno) está documentado en la sección "Tokens y costo"
de `docs/platform/live-eval.md`; esta página cubre todo lo demás que agrega
Phase 0.

## `GET /metrics`

Exposición en texto de Prometheus (`prometheus_client.generate_latest`) del
registro de métricas de todo el proceso. Apagado por defecto y ausente
(404), no meramente sin autenticar, hasta que un operador lo activa
explícitamente:

| Setting | Variable de entorno | Default | Efecto |
|---|---|---|---|
| `metrics_enabled` | `METRICS_ENABLED` | `False` | `GET /metrics` devuelve 404 mientras no esté seteado. |
| `metrics_api_key` | `METRICS_API_KEY` | `""` | Token Bearer requerido una vez que `metrics_enabled` está seteado. |

```bash
export METRICS_ENABLED=true
export METRICS_API_KEY=un-token-largo-y-aleatorio
curl -H "Authorization: Bearer $METRICS_API_KEY" http://localhost:8000/metrics
```

**Modelo de protección**: sigue exactamente el espíritu fail-closed de
`ADAPTER_API_KEY`/`ALLOW_INSECURE` de `integration/openai_adapter.py`
(`main.py::verify_metrics_access`, `Settings.validate_security_fail_closed`):

- `metrics_enabled=False` (default): la ruta existe en la app, pero la
  dependencia lanza 404 antes de hacer cualquier otra cosa -- la misma
  postura que si el endpoint no estuviera registrado.
- `metrics_enabled=True` + `metrics_api_key` seteado: se requiere
  `Authorization: Bearer <key>`, comparado con `secrets.compare_digest`
  (tiempo constante, el mismo mecanismo que `verify_bearer` del adapter de
  OpenAI). Token faltante o incorrecto -> 401.
- `metrics_enabled=True` + `metrics_api_key` vacío: `GET /metrics` queda
  abierto para cualquiera que pueda llegar al proceso.
  `Settings.validate_security_fail_closed` se niega a bootear esta
  combinación a menos que `ALLOW_INSECURE=true` también esté seteado
  explícitamente -- la misma vía de escape sólo-para-dev que usa el modo
  abierto del adapter. Un boot bajo esta combinación además loguea una
  advertencia `metrics.open_mode`.

## Salud y disponibilidad

`GET /health` es un endpoint de **liveness**: siempre devuelve HTTP 200 y el
`status` del cuerpo es `ok` o `degraded`. Informa PostgreSQL, Redis, el estado
del outbox de webhook, el proveedor de chat usado por runtimes configurados y
la pérdida de registros de auditoría. Si no hay un runtime configurado, el
estado del proveedor es `not_configured` y no se hace ninguna petición. De lo
contrario, la prueba del proveedor es un `HEAD` sin credenciales a su URL base,
acotado por un timeout total de un segundo y cacheado cinco segundos por
destino de proveedor configurado. Los cache misses concurrentes comparten una
única petición en vuelo, evitando ráfagas de probes al proveedor. Cualquier
respuesta HTTP (incluyendo errores de
autenticación o de ruta) prueba conectividad; un error de transporte o timeout
pone `provider.status` en `error` con la razón fija `"unreachable"`. No envía
tokens ni credenciales, y no expone mensajes de error del proveedor.

`GET /ready` devuelve el mismo cuerpo, pero es el endpoint de
**readiness**: sólo responde 200 cuando el status del cuerpo es `ok`; de lo
contrario devuelve 503. Configurá el liveness del orquestador con `/health` y
la compuerta de readiness/tráfico con `/ready`.

El campo `audit.dropped_count` de salud es el total local al proceso de eventos
de auditoría que el sink asíncrono no pudo persistir. No existe un contador de
pérdida silenciosa del outbox: su trabajo es durable y las fallas terminales
siguen visibles como estado de backlog/worker en vez de descartarse.

## Qué se mide

Se registra en los puntos de choque ya existentes del loop del agente
(`src/agents_system/agent/graph.py`) -- `AgentRuntime.run_turn_with_usage`
(vía su punto de retorno compartido `_finish_turn`), `_execute_tools`, y
`_limit_reached` -- nunca desparramado en llamadas adicionales. Todos los
objetos de métricas viven en `src/agents_system/observability/metrics.py`.

| Métrica | Tipo | Labels | Se registra cuando |
|---|---|---|---|
| `agent_turns_total` | Counter | `runtime_id`, `outcome` | Cada turno termina: `outcome` es `ok`, `timeout`, o `error`. |
| `agent_turn_duration_seconds` | Histogram | `runtime_id`, `outcome` | Igual que arriba -- duración real (wall-clock) de toda la llamada a `run_turn_with_usage`. |
| `agent_tokens_total` | Counter | `runtime_id`, `direction` (`input`/`output`) | El uso de un turno es conocido (ver la regla de honestidad abajo). |
| `agent_cost_usd_total` | Counter | `runtime_id` | El `cost_usd` de un turno es conocido (mismo lookup de `Settings.model_prices` que el Slice 1). |
| `agent_tool_calls_total` | Counter | `tool`, `outcome` | Cada intento de llamada a herramienta: `outcome` es `ok`, `denied`, `blocked`, `timeout`, o `error`. |
| `agent_tool_call_duration_seconds` | Histogram | `tool`, `outcome` | Igual que arriba. |
| `agent_limit_trips_total` | Counter | `limit` | Un límite de ejecución se dispara -- hoy sólo `"max_tool_calls"` (el nodo terminal `_limit_reached`; los "limit trips" de ADR-005 sección 6). |
| `agent_audit_events_dropped` | Gauge | ninguno | `AuditSink.dropped_count` actual y local al proceso, muestreado al servir `GET /metrics`. |
| `agent_provider_requests_total` | Counter | `provider`, `status` | Cada intento HTTP crudo que hace el cliente async del chat model (issue #145) -- ver "Observabilidad del proveedor, y la investigación del issue #145" más abajo. |
| `agent_provider_retries_total` | Counter | `provider` | Cada intento que el PROPIO loop de reintentos del SDK del proveedor reenvía después de que un intento anterior de la misma llamada falló (issue #145). |
| `process_resident_memory_bytes`, `process_cpu_seconds_total`, y el resto del `ProcessCollector` por defecto de `prometheus_client` | Gauge/Counter | ninguno | Siempre, para el proceso corriendo -- confirmado presente en Linux por `tests/test_observability_metrics.py`, no meramente asumido de la documentación de la librería. |

**Outcomes de turno**: `ok` también cubre un turno que terminó
legítimamente en el nodo `_limit_reached` -- eso es una finalización
normal, no una falla; el límite disparándose es su propia muestra de
`agent_limit_trips_total`. `timeout` es `total_execution_timeout_s`
disparándose. `error` es cualquier otra cosa que el turno lance (una falla
real del proveedor, un error inesperado del checkpointer) -- se registra y
luego se vuelve a lanzar sin cambios, así que esta métrica agrega
observabilidad sin cambiar ningún comportamiento de manejo de errores
existente.

**Outcomes de llamada a herramienta**: `denied` es un `PolicyViolation`
para una herramienta que nunca estuvo en la superficie equipada
(`reason="not_in_surface"` -- territorio Layer-1, p. ej. un nombre de
herramienta alucinado o inyectado por prompt injection). Como ese nombre
nunca se validó contra la superficie equipada, es texto no confiable
provisto por el modelo -- el label `tool` de una muestra `denied` es
siempre el placeholder fijo `_unrecognized_`, nunca el nombre de
herramienta que dio el modelo (ver "Higiene de labels" más abajo).
`blocked` es una falla de revalidación Layer-2 sobre una herramienta que SÍ
estaba equipada (`reason` en `{"revalidation_required",
"permission_revoked"}` -- `harness/interceptor.py`); su label `tool` es el
nombre real de esa herramienta equipada. `timeout` es `tool_call_timeout_s`
disparándose para esa llamada. `error` es cualquier otra cosa que el
propio connector lance -- se registra y se vuelve a lanzar sin cambios,
igual que el outcome `error` a nivel turno.

**Regla de honestidad para tokens/costo** -- mismo contrato que el
`TurnUsage` del Slice 1: `agent_tokens_total`/`agent_cost_usd_total` sólo
se incrementan cuando el valor es realmente conocido. Un turno que
expiró por timeout o falló incrementa
`agent_turns_total`/`agent_turn_duration_seconds` pero nunca una muestra
de token o costo adivinada en `0`.

## Observabilidad del proveedor, y la investigación del issue #145

El issue #145 ("con 20 conversaciones concurrentes contra un modelo real,
sólo 3/20 terminan dentro de 240s") sospechaba throttling del lado del
proveedor de chat: el síntoma reportado -- la mayoría de los turnos
fallando, "nunca vuelve ningún error" -- es exactamente lo que se vería si un
429 fuera absorbido en silencio por el propio loop de reintentos del SDK del
proveedor, dado que `agent_turns_total{outcome="error"}` sola no puede decir
*por qué* falló un turno. `MetricsHTTPTransport`
(`src/agents_system/observability/provider_http.py`) se construyó para hacer
eso visible: envuelve el cliente httpx async del chat model
`openai_compatible` para que cada intento HTTP crudo incremente
`agent_provider_requests_total`, etiquetado por `provider` (un valor acotado
y definido en código -- `openai_compatible` por ahora) y `status` -- uno de
`success`, `rate_limited` (un 429 crudo), `client_error`, `server_error`, o
`network_error` (un timeout de connect/read/write u otra falla de
transporte), clasificado por `classify_provider_status`. Cada intento que el
propio loop de reintentos del SDK reenvía (reconocido vía el header
`x-stainless-retry-count` que todo SDK generado con Stainless -- openai,
groq, anthropic -- estampa en los requests reintentados) también incrementa
`agent_provider_retries_total{provider}` -- un reintento que nunca lanza
excepción es, si no, invisible.

**Esa misma instrumentación fue lo que descartó la hipótesis.** Se reprodujo
contra una cuenta real con `EVAL_PROVIDER=openai_compatible` con 5, 10 y 20
turnos concurrentes contra un modelo real, por el camino completo webhook →
outbox → `AgentRuntime.run_turn`, ANTES del fix de abajo: una corrida de
confirmación fresca con concurrencia=20 completó 10/20 conversaciones en
240s (p50 84.96s, p95 158.20s), con `agent_provider_requests_total{
provider="openai_compatible", status="success"}` = 73 de 73 intentos HTTP
crudos -- cada request al proveedor tuvo éxito. Cero muestras `rate_limited`,
`client_error`, `server_error`, o `network_error`, a cualquier concurrencia
en toda la investigación. El proveedor nunca fue el cuello de botella.

Cruzar `agent_turns_total{outcome="error"}` contra
`agent_tool_calls_total{outcome="error",tool="catalog_search"}` para las
MISMAS corridas encontró una correspondencia exacta 1:1 cada vez (12/12,
63/63, 126/126 a lo largo de las tres rondas) -- cada turno fallido había
fallado DENTRO de la llamada a la herramienta `catalog_search`, nunca dentro
de la llamada al modelo. La causa: `catalog_search` de `sales-agent` consulta
una tabla demo `articulos` que `evals.live_harness.boot_live_harness` sólo
crea cuando se llama con `seed_demo=True`
(`docs/platform/reference-backends.md`) -- y el escenario real-model de Phase
3 (`tests/test_live_load_phase3.py`, del #83/PR #144, el escenario que el
propio issue #145 cita) nunca lo pasaba, aunque su propio prompt ("Do you
have Item Alpha in stock?") está diseñado para disparar esa herramienta. Cada
llamada real a `catalog_search` lanzaba
`asyncpg.exceptions.UndefinedTableError: relation "articulos" does not
exist`, una excepción no manejada que hacía fallar todo el turno y
alimentaba el loop de reintento-con-backoff de `services/outbox.py`
(`MAX_OUTBOX_ATTEMPTS=5`, `_RETRY_BASE_SECONDS * 2**attempt`) -- que nunca le
envía una respuesta al cliente mientras reintenta, calzando exactamente con
el síntoma de "nunca vuelve ningún error". El OTRO benchmark de la suite de
carga (100 conversaciones concurrentes con un modelo fake determinístico,
`agent_turn_duration_seconds` p95 13.8s, sin errores) nunca ejercita esto en
absoluto: ese modelo fake está hardcodeado para nunca llamar a una
herramienta (el propio docstring de
`tests/test_live_load_phase3.py::_LoadTestFakeModel`), así que
estructuralmente no puede tocar el bug de esquema faltante de
`catalog_search` -- lo cual explica por qué el número rápido/limpio de 100
concurrentes y el número lento/roto de 20 con modelo real nunca fueron
realmente comparables.

**El fix tiene dos partes, ambas "de nuestro lado", no del proveedor:**

1. El escenario real-model de `tests/test_live_load_phase3.py` ahora pasa
   `seed_demo=True`, igual que cualquier otro escenario de live-eval que
   toca `catalog_search`/`client_lookup`/`run_report`
   (`tests/test_live_eval_phase1.py`) -- así este benchmark mide de verdad
   la latencia y capacidad reales de un modelo real, no un bug de setup de
   datos demo.
2. `catalog_search`/`client_lookup` de `services/reference.py` (y, a través
   de él, el propio chequeo de destinatario de `message_sender`) ahora
   capturan un `sqlalchemy.exc.SQLAlchemyError` de la consulta en sí y se
   degradan a la MISMA respuesta `error_kind: "reference_data_not_configured"`
   que ya devuelve un `readonly_engine` sin setear, logueando una advertencia
   `reference_backends.demo_schema_unavailable` -- en vez de dejar que un
   esquema demo faltante o mal configurado rompa todo el turno. Esto es
   defensa en profundidad para un despliegue real que reutilice este backend
   de referencia sin haber migrado/sembrado realmente su esquema; ver la
   sección "Comportamiento observable de la demo" en
   `docs/platform/reference-backends.md`.

**Lo que sigue siendo útil aunque el proveedor no tuviera la culpa esta vez:**
los contadores de status/reintentos HTTP por llamada dan una visibilidad real
de 429/5xx/timeout que la plataforma nunca tuvo antes, y el timeout acotado
por llamada de abajo cierra un hueco real (un default sin límite de 600s) que
SÍ afectaría a una cuenta genuinamente limitada en el futuro. Re-corrida
después de ambos fixes, mismo escenario de 20 concurrentes:
**20/20 conversaciones completaron en 76.92s** (mín 18.88s, p50 59.28s, p95
76.67s, máx 76.91s -- latencia real de modelo, ahora libre del crash de
esquema), `agent_turns_total{outcome="ok"}` = 20 con cero muestras
`error`/`timeout`, `agent_tool_calls_total{outcome="ok",tool="catalog_search"}`
= 78 con cero errores, y `agent_provider_requests_total{
provider="openai_compatible",status="success"}` = 96 -- cada intento HTTP
crudo realizado, ninguno rate-limited ni fallido. Cero filas en
`outbox_work.last_error`. Comparar los números propios del issue original
(3/20 dentro de 240s, p50 95s, p95/máx 152s para las tres que completaron) y
la reproducción antes-del-fix de este mismo PR, justo arriba (10/20, p50
84.96s, p95 158.20s, 63 errores de tool-call) -- el fix no es una
optimización de latencia, es eliminar una excepción no manejada que estaba
descartando en silencio la mayoría de las conversaciones al loop de
reintentos del outbox.

**Timeout acotado por llamada.** El chat model `openai_compatible`
anteriormente dejaba `request_timeout` sin setear, lo cual dejaba en efecto
el default propio del SDK de openai: 5s para conectar, pero 600s (diez
minutos) para la respuesta en sí. Una llamada colgada podía retener un turno
en silencio hasta diez minutos antes de que cualquier límite a nivel
plataforma (`total_execution_timeout_s`, 60s por default) llegara a importar
en la práctica para los reintentos/backoff de esa misma llamada.
`Settings.openai_compatible_request_timeout_s` (env
`OPENAI_COMPATIBLE_REQUEST_TIMEOUT_S`, default `45.0`) ahora lo acota
explícitamente, cómodamente por debajo de `total_execution_timeout_s` -- una
única llamada colgada al proveedor ahora falla con un timeout tipado y
claro, en vez de esperar en silencio.

**El presupuesto del turno tiene que alojar las llamadas al proveedor (issue
#172).** `total_execution_timeout_s` acota el turno completo y
`OPENAI_COMPATIBLE_REQUEST_TIMEOUT_S` acota una llamada al proveedor, así que
los dos tienen que encajar:

```
total_execution_timeout_s  >=  llamadas al proveedor por turno x timeout de request  +  tiempo de herramientas
```

Un turno que usa una herramienta hace al menos dos llamadas al proveedor (una
para elegir la herramienta y otra para responder con su resultado). Con el
timeout de request de 45s son 90s de proveedor antes de que la herramienta
corra siquiera. Un presupuesto de 30s, que `operator-agent` fija a propósito,
no alcanza ni para una llamada lenta, y uno de 60s no alcanza para dos. Por eso
`developer-agent` sube su propio `total_execution_timeout_s` a 120s. No bajes
`OPENAI_COMPATIBLE_REQUEST_TIMEOUT_S` para compensar: un timeout de request más
corto convierte a un proveedor lento pero funcional en uno que falla.

Al arrancar, la app registra un WARNING `runtime.turn_budget_below_request_timeout`
por cada runtime sobre el proveedor `openai_compatible` cuyo presupuesto de turno
sea menor que el timeout de request. Nunca frena el arranque: un presupuesto
ajustado puede ser deliberado.

**Qué reporta un turno que agotó el tiempo.** El turno termina con la respuesta
fija "This is taking longer than expected. Please try again." y
`outcome="timeout"`. Sus mensajes conservan todo lo que el grafo había producido
antes del límite, y su uso cuenta las llamadas al modelo que completaron antes
(`TurnResult.usage`), así que `runtime.turn_usage` y las métricas de tokens y
costo también se registran. Una llamada cortada a mitad de vuelo todavía no tiene
uso, así que los totales son una cota inferior. Una llamada a herramienta que
el timeout interrumpió queda en los mensajes, respondida con un `ToolMessage` de
error, así que los mensajes devueltos son un historial válido para enviar de
vuelta como el del turno siguiente. Si el checkpointer se degradó y el
turno arrancó de nuevo, o nunca se produjo estado, el uso es desconocido
(`None`), nunca un valor inventado. `TurnResult.timed_out` y
`TurnResult.timeout_s` permiten distinguir el timeout de un turno normal.

**Guía de dimensionamiento de cuenta para operadores.** `max_concurrent_turns`
(default 10) acota cuántos turnos corre este proceso a la vez, pero NO acota
el límite de tasa propio de la cuenta del proveedor (requests/minuto y/o
tokens/minuto, según el proveedor). Ver crecer
`agent_provider_requests_total{status="rate_limited"}` y
`agent_provider_retries_total` bajo tráfico normal es la señal de que
`max_concurrent_turns`, o el plan de la cuenta, necesita bajar (o subir) para
calzar con lo que la cuenta del proveedor realmente sostiene -- dimensionar
el limitador de admisión es una decisión de soporte/costo que la plataforma
deja deliberadamente al operador, no algo que este fix pueda elegir por un
número correcto. Una cuenta con rate limit también significa que cada
reintento suma el propio backoff exponencial de `services/outbox.py`
(`_RETRY_BASE_SECONDS * 2**attempt`, acotado por `_RETRY_MAX_SECONDS`)
encima del propio delay del proveedor, así que una cuenta persistentemente
subdimensionada se ve como una latencia end-to-end que crece lentamente, no
que falla de inmediato.

## Higiene de labels

Los labels son enums acotados y definidos en código, o identificadores
configurados por el operador -- **nunca** un id de usuario, número de
teléfono, id de correlación/request, o texto de mensaje. Concretamente:
`runtime_id` (ver abajo), `outcome`/`limit`/`direction` (enums de string
fijos), `tool` (un nombre de connector del `ToolRegistry` finito, inyectado
por el operador -- **excepto** para una llamada `denied`, donde el nombre
de herramienta que dio el modelo nunca se validó contra ese registro:
`_execute_tools` (`agent/graph.py`) sustituye ese único caso por el
placeholder fijo `_unrecognized_`, así un nombre de herramienta atacante o
alucinado nunca puede llegar al almacén de labels), y `provider`/`status`
(`observability/metrics.PROVIDER_NAMES`/`PROVIDER_REQUEST_STATUSES` --
`agent_provider_requests_total`/`agent_provider_retries_total` del issue
#145: el código de status HTTP crudo y cualquier cuerpo de error del
proveedor nunca son labels en sí mismos, sólo el bucket fijo de
`classify_provider_status`). No hay ningún parámetro en
`record_turn`/`record_tool_call`/`record_limit_trip`/`record_provider_request`/`record_provider_retry`
por el cual un valor no acotado pudiera llegar a un label --
`tests/test_observability_metrics.py` y `tests/test_agent_runtime.py` lo
fijan: un test pinea el esquema exacto de nombres de labels, uno corre un
turno real con un `session_id` con forma de número de teléfono y verifica
que nunca aparece en ningún lado del texto exportado (`session_id` ni
siquiera es un label -- no es parámetro de ninguna función de registro), y
otro corre una llamada `denied` cuyo nombre es un payload con forma de
ataque (un número de teléfono más metacaracteres del formato de
exposición de Prometheus) y verifica que ni el payload ni su substring de
PII aparecen nunca en el texto exportado.

## `runtime_id`

`AgentRuntime.__init__` acepta un `runtime_id: str | None` opcional.
`None` (el default) cae al id de modelo del proveedor derivado por este
propio runtime (`model_display_name(model)` -- el mismo default que ya usa
el override `model_id` de precios del Slice 1), así que cada sitio de
construcción existente (`evals/runner.py`, uso directo de la librería vía
`agents_system.AgentRuntime`) queda sin afectar. El lifespan de `main.py`
-- el único caller con un id más significativo a mano -- pasa
explícitamente el runtime id registrado: la clave opaca con la que se
registró el runtime, en `create_app(agents=...)` o en
`AGENT_REGISTRATIONS`. Así, las métricas de un despliegue real quedan
etiquetadas con el id que el operador eligió, no con qué modelo subyacente
resulta estar sirviéndolo.

## Patrón de testing

Cada métrica se construye contra un `prometheus_client.CollectorRegistry`
explícito vía `observability.metrics.build_metrics(registry)` -- nunca
atada al registro ambiente por defecto de `prometheus_client`.
`DEFAULT_METRICS`/`DEFAULT_REGISTRY` son el conjunto de todo el proceso que
sirve `GET /metrics`, construido una vez al importar. Un test construye su
propio conjunto descartable -- `build_metrics(CollectorRegistry())` --
para que los tests nunca choquen entre sí (registrar el mismo nombre de
métrica dos veces en un registro lanza `ValueError`) ni muten estado de
todo el proceso que otro test pudiera observar. Pasá un conjunto propio a
`AgentRuntime(..., metrics=mis_metrics)` -- o, para los contadores del
proveedor, a `main._build_chat_model(..., metrics=mis_metricas)` (issue
#145) -- para verificar exactamente los valores que produjeron los turnos de
un test.

## Fuera de alcance en este slice

- El harness de vivo de la app real (issue #78 Slice 3).
- Alertas, SLOs, y notificación push (ADR-005 sección 7 -- desbloqueada por
  los contadores de este slice, no construida por él).
- OpenTelemetry / trazas distribuidas (el propio "despliegue más amplio" de
  ADR-005 sección 6, explícitamente diferido hasta después de este
  conjunto mínimo de contadores).
- Duración por llamada a herramienta en el reporte de live-eval
  (`evals/reporting.py`): sólo se agregó la duración de turno ahí (ver
  `docs/platform/live-eval.md`) -- meter la duración por llamada a
  herramienta en ese reporte de forma barata necesitaría que `TurnResult`
  también cargue una lista de duraciones de llamadas a herramientas, algo
  que este slice no agregó. Usá `agent_tool_call_duration_seconds` vía
  `/metrics` para eso.

## Referencias cruzadas

- ADR-005 sección 6 (Observability) / sección 7 (Monitoring and alerts):
  `docs/architecture/adr-005-operational-safety-roadmap.md`
- Tokens y costo (issue #78 Slice 1): `docs/platform/live-eval.md`
- Código de métricas: `src/agents_system/observability/metrics.py`
- Puntos de registro: `src/agents_system/agent/graph.py`
  (`_execute_tools`, `_limit_reached`, `_finish_turn`)
- Ruta `GET /metrics` + `verify_metrics_access`: `src/agents_system/main.py`
- Tests offline: `tests/test_observability_metrics.py`,
  `tests/test_metrics_endpoint.py`, `tests/test_agent_runtime.py` (sección
  de métricas), `tests/test_config.py` (validador fail-closed)
- Throttling/reintentos del proveedor (issue #145):
  `src/agents_system/observability/provider_http.py`
  (`MetricsHTTPTransport`, `classify_provider_status`), conectado en la
  rama `openai_compatible` de `main._build_chat_model`;
  `tests/test_provider_http_metrics.py`, `tests/test_main.py` (sección
  `#145`), `tests/test_config.py` (`openai_compatible_request_timeout_s`)
- La causa raíz real del #145 (un esquema demo faltante, no el proveedor):
  `src/agents_system/services/reference.py` (manejo de `SQLAlchemyError` en
  `_fetch_rows`), `tests/test_reference_backends.py` (sección `#145`), el
  fix `seed_demo=True` en `tests/test_live_load_phase3.py`; ver
  `docs/platform/reference-backends.md`
