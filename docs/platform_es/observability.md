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
herramienta alucinado). `blocked` es una falla de revalidación Layer-2
sobre una herramienta que SÍ estaba equipada (`reason` en
`{"revalidation_required", "permission_revoked"}` --
`harness/interceptor.py`). `timeout` es `tool_call_timeout_s`
disparándose para esa llamada. `error` es cualquier otra cosa que el
propio connector lance -- se registra y se vuelve a lanzar sin cambios,
igual que el outcome `error` a nivel turno.

**Regla de honestidad para tokens/costo** -- mismo contrato que el
`TurnUsage` del Slice 1: `agent_tokens_total`/`agent_cost_usd_total` sólo
se incrementan cuando el valor es realmente conocido. Un turno que
expiró por timeout o falló incrementa
`agent_turns_total`/`agent_turn_duration_seconds` pero nunca una muestra
de token o costo adivinada en `0`.

## Higiene de labels

Los labels son enums acotados y definidos en código, o identificadores
configurados por el operador -- **nunca** un id de usuario, número de
teléfono, id de correlación/request, o texto de mensaje. Concretamente:
`runtime_id` (ver abajo), `outcome`/`limit`/`direction` (enums de string
fijos), `tool` (un nombre de connector del `ToolRegistry` finito, inyectado
por el operador). No hay ningún parámetro en `record_turn`/
`record_tool_call`/`record_limit_trip` por el cual un valor no acotado
pudiera llegar a un label -- `tests/test_observability_metrics.py` y
`tests/test_agent_runtime.py` lo fijan: un test pinea el esquema exacto de
nombres de labels, otro corre un turno real con un `session_id` con forma
de número de teléfono y verifica que nunca aparece en ningún lado del
texto exportado (`session_id` ni siquiera es un label -- no es parámetro
de ninguna función de registro).

## `runtime_id`

`AgentRuntime.__init__` acepta un `runtime_id: str | None` opcional.
`None` (el default) cae al id de modelo del proveedor derivado por este
propio runtime (`model_display_name(model)` -- el mismo default que ya usa
el override `model_id` de precios del Slice 1), así que cada sitio de
construcción existente (`evals/runner.py`, uso directo de la librería vía
`agents_system.AgentRuntime`) queda sin afectar. El lifespan de `main.py`
-- el único caller con un id más significativo a mano -- pasa
explícitamente el `runtime_id` propio del operador
(`"{deployment}__{role}"`), así que las métricas de un despliegue real
quedan etiquetadas con el id que el operador configuró, no con qué modelo
subyacente resulta estar sirviéndolo.

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
`AgentRuntime(..., metrics=mis_metrics)` para verificar exactamente los
valores que produjeron los turnos de un test.

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
