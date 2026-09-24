# Pipeline de evaluación en vivo (live-eval)

ADR-002 E.18 (`docs/architecture/adr-002-agent-model-and-capabilities.md`).
Las pruebas deterministas por PR (`build_test_registry`, modelos fake)
prueban que el *mecanismo* funciona: una llamada denegada se deniega, una
superficie de herramientas resuelve como fue declarada. No pueden probar que
el *comportamiento* de un rol bajo un modelo real coincida con lo que su
prompt y su política pretenden. El pipeline de live-eval en
`src/agentsys/evals/` cierra esa brecha: corre el escenario de un rol N veces
contra un modelo local real y reporta una tasa de éxito, nunca un único
pass/fail — la corrección de un sistema probabilístico es una tasa, no un
booleano.

Este pipeline es **manual o nocturno, nunca por PR** (las llamadas a un
modelo real son lentas, cuestan dinero en un modelo alojado, y no son
totalmente deterministas — la forma equivocada para un gate que bloquea cada
PR).

## Cómo correrlo

```bash
pytest -m live
```

`live` es un marcador de pytest, deseleccionado por defecto igual que
`integration` (el `addopts` de `pyproject.toml`). Necesitás un Ollama local
corriendo con el modelo configurado ya descargado:

```bash
ollama serve
ollama pull qwen2.5:3b   # o el que nombre OLLAMA_MODEL
```

Apuntá el pipeline a otro modelo u otro host con los mismos campos de
`Settings` que `_build_chat_model` ya lee para cualquier otro rol:

```bash
export OLLAMA_MODEL=qwen3:8b
export OLLAMA_BASE_URL=http://localhost:11434   # vacío/sin definir = la resolución por defecto de ChatOllama
pytest -m live
```

Ambos por defecto valen lo mismo que estaba hardcodeado antes de #169
(`qwen2.5:3b`, la resolución de host propia de ChatOllama) — una corrida sin
configurar no cambia.

## Cambiar de proveedor (EVAL_PROVIDER)

El proveedor propio del eval es un switch separado del `ADAPTER_PROVIDER` de
la aplicación en ejecución: `EVAL_PROVIDER` (`Settings.eval_provider`, por
defecto `"ollama"`), leído por `agentsys.evals.provider.build_eval_model()`.
Elegir otro modelo para una corrida de eval puntual nunca debe cambiar lo que
la app misma sirve, así que ambos nunca comparten un campo. Acepta los mismos
valores que despacha `main._build_chat_model`: `ollama`, `groq`, `anthropic`,
`openai_compatible`.

`openai_compatible` lee exactamente los mismos settings
`OPENAI_COMPATIBLE_BASE_URL` / `OPENAI_COMPATIBLE_MODEL` /
`OPENAI_COMPATIBLE_API_KEY` que ya usa cualquier otro rol — funciona
cualquier host compatible con OpenAI, incluyendo un router alojado.
OpenRouter, por ejemplo:

```bash
export EVAL_PROVIDER=openai_compatible
export OPENAI_COMPATIBLE_BASE_URL=https://openrouter.ai/api/v1
export OPENAI_COMPATIBLE_MODEL=deepseek/deepseek-v4-flash
export OPENAI_COMPATIBLE_API_KEY=...   # tu propia clave -- nunca la commitees
pytest -m live
```

(O guardá estos valores en tu `.env` — `Settings` lo carga automáticamente;
nunca imprimas ni commitees ese archivo.) El nombre de modelo que reporta
`ScenarioResult` se lee de vuelta del modelo ya construido
(`agentsys.evals.provider.model_display_name`): las clases de la familia
ChatOpenAI (`groq`, `openai_compatible`) lo exponen como `model_name`, no
como `model` — reportar un string hardcodeado acá se desincronizaría en
silencio de lo que el proveedor realmente usó.

## Esquema de escenario

Un escenario es un archivo YAML: un rol, sus turnos de entrada, y
aserciones de comportamiento — **nunca una aserción sobre el texto exacto
generado por el modelo**. Los modelos reales no son lo bastante deterministas
para que una comparación de igualdad de texto sea un chequeo significativo o
estable; todas las aserciones acá son de comportamiento (qué herramienta se
llamó, si una llamada fue denegada, si un escalamiento se completó con
éxito).

```yaml
role: sales-agent            # obligatorio — el nombre de la carpeta del rol
name: sales_agent_smoke      # opcional — por defecto, el nombre del archivo sin extensión
description: >               # opcional, texto libre
  Un cliente pregunta por un artículo del catálogo.
client: acme                 # opcional — resolver bajo un deployment; omitir para el rol genérico
turns:                       # obligatorio, no vacío — mensajes del usuario, en orden
  - "Hola, ¿tenés Item Alpha en stock y cuánto sale?"
assertions:
  tools_called: [catalog_search]                    # cada nombre acá DEBE haber sido llamado
  tools_not_called: [order_writer, escalation_notifier]  # ninguno de estos puede llamarse
  permission_denied: false                          # true/false: si alguna llamada fue bloqueada por el interceptor de Layer 2
  escalation_expected: false                         # true: exige una llamada exitosa a escalation_notifier
                                                       # false: escalation_notifier nunca debe intentarse
granted_permissions: [read:catalog]  # opcional — por defecto, los permisos que el rol ya declara
```

Cada campo de `assertions` es opcional; uno omitido no afirma nada.
`tools_called`/`tools_not_called` chequean si el modelo *intentó* la llamada,
sin importar el resultado. `escalation_expected: true` es más estricto —
además exige que la llamada haya tenido éxito (sin `error_kind` en el
resultado JSON de la herramienta), porque "el modelo intentó escalar pero el
canal no estaba configurado" no es la misma afirmación que "el cliente
efectivamente fue escalado a un humano". `permission_denied` coincide
específicamente con un bloqueo de `PolicyViolation` de Layer 2 (el mensaje
`"Tool call blocked: ..."` de `agent/graph.py`) — un timeout es una falla
distinta y no cuenta.

Cada turno corre a través de `AgentRuntime.run_turn` exactamente como lo
haría un caller real — las llamadas a herramientas dentro de un turno las
resuelve automáticamente el grafo; `turns` son solo los mensajes propios del
usuario, enviados de a uno con el historial acumulado del runtime
retroalimentado.

Cargá un archivo con `agentsys.evals.schema.load_scenario(path)`, o todos
los `*.yaml`/`*.yml` de un directorio con `load_scenarios(directory)`.
Cualquiera de los dos lanza `ScenarioError`, nombrando el archivo
problemático, ante cualquier problema estructural (`role` faltante, `turns`
vacío, un `escalation_expected` que no es booleano, ...).

## Dónde viven los archivos de escenario

`evals/scenarios/*.yaml` en la raíz del repositorio — versionados en git, un
archivo por escenario. `evals/scenarios/sales_agent_smoke.yaml` es el
escenario de humo propio del pipeline (#169): prueba que `resolve()` →
`build_runtime()` → `AgentRuntime.run_turn` funciona de punta a punta contra
un modelo real. Deliberadamente no es una suite de corrección para
`sales-agent` — la cobertura de escenarios por rol queda fuera del alcance de
#169 y le corresponde a los issues hermanos bajo #52.

## Dónde van los resultados

`evals/results/` — **ignorado por git** (la entrada `evals/results/` del
`.gitignore`; `evals/scenarios/` es un hermano, no un padre, y sigue
versionado). Cada corrida de
`agentsys.evals.reporting.write_results(...)` escribe dos archivos con marca
de tiempo:

- `evals/results/<timestamp UTC>.json` — una entrada por escenario: rol,
  modelo, cantidad de corridas, cantidad de éxitos, tasa de éxito, y el
  detalle de fallas por corrida.
- `evals/results/<timestamp UTC>.md` — una tabla markdown corta (escenario,
  rol, modelo, corridas, tasa de éxito) para una lectura rápida.

## Correrlo como pipeline propio de un rol

`agentsys.evals.runner.run_scenario(scenario, *, model, model_name,
registry, roots=None, runs=1)` resuelve el rol del escenario por el mismo
camino `resolve()`/`build_runtime()` que usa cualquier otro consumidor (con
los backends de referencia del rol conectados de la misma forma que
`build_test_registry` de `tests/conftest.py` lo hace para las pruebas), lo
corre `runs` veces, y devuelve un `ScenarioResult` con el resultado de cada
corrida más la `success_rate` agregada.
`agentsys.evals.runner.evaluate_assertions(assertions, messages)` es el
motor de aserciones en sí, invocable directamente contra cualquier
transcripción de mensajes acumulada — esto es lo que ejercitan las pruebas
unitarias offline en `tests/test_eval_runner.py` con un modelo fake,
probando la lógica propia del runner (carga de escenarios, evaluación de
aserciones, agregación de tasas, una aserción que falla contada como corrida
fallida) sin ninguna llamada de red.

`run_scenario` también registra un `AuditSink` real para la corrida (por
defecto, un `_CapturingAuditSink` en memoria) para que el cableado de
auditoría normal de la plataforma efectivamente entregue eventos en vez de
que cada uno se descarte en silencio — un eval en vivo no tiene un lifespan
de FastAPI que construya el sink propio de la app respaldado por base de
datos. `ScenarioResult.audit_events_captured` reporta cuántos llegaron;
cualquier sink que estuviera registrado antes de la llamada, si había alguno,
se restaura después. Esto es solo diagnóstico — la aserción
`permission_denied` sigue leyendo el `ToolMessage` síncrono que devuelve el
interceptor, no el sink, ya que el drenador propio del sink agrupa en una
ventana de 100ms y solo es eventualmente consistente para cuando termina una
corrida.

## Nota sobre el hardware local

Una AMD RX 5700 XT (Navi10, `gfx1010`) necesita específicamente el paquete
`ollama-vulkan` — ROCm no soporta oficialmente `gfx1010`. `qwen2.5:3b` es
demasiado chico para un tool-calling confiable en la práctica, pero se
mantiene como un caso de piso/regresión deliberado: un modelo que falla en
una fracción de las tareas de tool-calling es una señal útil de que el
harness está discriminando correctamente, no un bug del pipeline.

## Referencias cruzadas

- ADR-002 E.18: `docs/architecture/adr-002-agent-model-and-capabilities.md`
- Backends de referencia que el eval runner conecta: `docs/platform_es/reference-backends.md`
- Código del runner: `src/agentsys/evals/{schema,runner,reporting,provider}.py`
- Pruebas offline: `tests/test_eval_schema.py`, `tests/test_eval_runner.py`,
  `tests/test_eval_reporting.py`, `tests/test_eval_provider.py`
- Prueba de humo en vivo: `tests/test_live_eval_sales_agent.py`
