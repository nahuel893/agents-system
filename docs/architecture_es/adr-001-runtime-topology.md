# ADR-001 — Topología de runtime

**Estado:** Aceptado · **Fecha:** 2026-08-27 · **Tarea:** D-029
**Bloquea a:** D-030, D-031, D-032, D-033, D-034, D-036, D-041, D-042

## Contexto

Hasta ahora el roadmap preguntaba "¿un proceso o N workers?" y no podía
responder, porque a la pregunta le faltaba su insumo: cuántas conversaciones
concurrentes tiene que sostener el sistema. Ese número ya está definido.

- **10 conversaciones concurrentes** al lanzamiento (ACME, primer cliente).
- **100 conversaciones concurrentes** como objetivo permanente.

Esos dos números no tienen la misma respuesta, y la distancia entre ellos no se
salva agregando workers. Medido sobre la máquina de desarrollo:

| Medición | Valor | Origen |
|---|---|---|
| RAM del host | 15.877 MB totales | `free -m` |
| CPUs | 12 | `nproc` |
| Modelo BGE-M3 en disco | 4,3 GB | `du -sh ~/.cache/huggingface/hub/models--BAAI--bge-m3` |
| `max_connections` de PostgreSQL | 100 | `show max_connections` |
| `superuser_reserved_connections` | 3 | `show superuser_reserved_connections` |
| `shared_buffers` | 128 MB (default de fábrica, sin tunear) | `show shared_buffers` |
| Pool de conexiones | **sin configurar** → default de SQLAlchemy 5 + 10 = 15 por engine | `models/base.py:65` |
| Engines por proceso | hasta 3 (operacional, BI, medallion) | `main.py:122,193` · `services/medallion.py:19` |
| Default de `embedding_provider` | `"local"` | `config.py` |

Dos de esos números imponen techos duros, y ambos son costos **por proceso**.

**El embedder se resuelve una vez por registry, no por llamada.** `main.py:171`
construye el registry dentro del lifespan y captura el embedder en el closure
del conector (`rag_connector.py:112` lo documenta: "BGE-M3 es pesado — cargar
una vez por registry, no por llamada"). Ese diseño es correcto, y significa que
cada proceso worker sostiene su propia copia de 4,3 GB del modelo.

**Las conexiones a la base también son por proceso.** Con el pool en el default
de SQLAlchemy (`pool_size=5, max_overflow=10`), cada engine puede abrir 15
conexiones, y un proceso sostiene entre 2 y 3 engines:

| Procesos | Conexiones (2 engines) | Conexiones (3 engines) | Contra 97 utilizables |
|---|---|---|---|
| 1 | 30 | 45 | entra |
| 2 | 60 | 90 | ajustado |
| 3 | 90 | 135 | **agota** |
| 4 | 120 | 180 | **agota** |

Es decir: con los defaults de hoy, PostgreSQL se queda sin conexiones al tercer
proceso worker — antes de que cualquier otra cosa del sistema se vuelva el
cuello de botella.

## Decisión

La decisión de topología está **encadenada**, no es una sola. La elección del
embedder define si múltiples procesos son físicamente posibles; recién después
la cantidad de workers se vuelve una pregunta con sentido.

### 1. El embedder es remoto cuando el objetivo supera ~10 concurrentes

`LocalBGEEmbeddingProvider` (`services/embeddings.py`) hace dos cosas que no
sobreviven al escalado horizontal:

- sostiene 4,3 GB de modelo **por proceso**, así que N workers cuestan
  N × 4,3 GB contra 15,8 GB de RAM del host — dos procesos ya son casi toda la
  máquina, tres no entran;
- `embed()` despacha a `loop.run_in_executor(None, ...)`, el thread pool
  compartido por defecto, lo que vuelve a la búsqueda de catálogo
  **CPU-bound sobre 12 cores compartidos con todo lo demás que hace el
  proceso**. El proveedor de OpenAI es I/O puro y no agrega CPU ni RAM por
  request.

Por lo tanto:

- **`embedding_provider="local"` sigue siendo el default de desarrollo** —
  offline, sin API key, sin costo por token, y una máquina de desarrollo corre
  un solo proceso de todos modos.
- **Producción, en el objetivo de 100 conversaciones, usa un proveedor
  remoto.** Los embeddings de consulta son cortos y baratos; el costo
  arquitectónico de mantenerlos locales (un proceso, para siempre) es mucho
  mayor que el costo en tokens de mandarlos afuera.

Queda deliberadamente abierta una tercera opción, sin elegirla ahora: un
**servicio de embeddings dedicado** — un proceso carga BGE-M3 una vez y los
workers le pegan por HTTP. Eso mantiene los embeddings locales (no sale ningún
dato, sin costo por token) y permite escalar la capa de API. Cuesta una unidad
desplegable extra. Retomarlo si mandar consultas de catálogo a un tercero se
vuelve inaceptable; no construirlo especulativamente.

### 2. La cantidad de procesos sigue al objetivo, en dos etapas

**Etapa A — lanzamiento, 10 conversaciones concurrentes: un solo proceso.**

Diez turnos concurrentes están dominados por la latencia del LLM, que es I/O.
Un solo event loop lo maneja cómodo. Un proceso único no requiere ningún
trabajo de estado compartido: `_seq_counter` es correcto dentro de un proceso,
una sola cola de auditoría significa un solo `dropped_count`, y 30–45
conexiones entran en `max_connections` sin tocar nada.

Esto significa que la configuración de lanzamiento es **el código actual más
D-030 y D-033** — no una re-arquitectura. La Etapa A es alcanzable ya.

**Etapa B — objetivo, 100 conversaciones concurrentes: múltiples procesos.**

Con 100 turnos concurrentes un solo event loop deja de alcanzar — no por las
llamadas al LLM, que siguen siendo I/O, sino porque la serialización JSON, la
validación de Pydantic, el mapeo del ORM y (si sigue siendo local) el embedding
compiten todos por un core. Hacen falta múltiples procesos, y cada ítem de la
sección siguiente pasa de ser una mejora a ser un prerrequisito.

### 3. Qué tiene que salir del proceso antes de N > 1

Inventariado leyendo el runtime, no supuesto:

| Estado | Ubicación | ¿Sobrevive a N procesos? |
|---|---|---|
| `_seq_counter` | dict de módulo, `audit/recorder.py:44` | **No — D-042.** Cada proceso cuenta el fallback `"none"` desde 1, así que dos workers emitiendo un evento sin contexto en el mismo instante violan `uq_audit_event_correlation_sequence` y pierden el lote entero. |
| Cola de auditoría + `dropped_count` | en memoria, `audit/sink.py` | **Parcialmente.** Cada proceso tiene su propia cola de 1000 y su propio contador; nada los agrega, así que la pérdida de auditoría se vuelve N números invisibles en lugar de uno. Necesita D-046. |
| Pools de conexiones | SQLAlchemy, sin configurar | **No — D-032.** 3 procesos agotan `max_connections` con los defaults actuales. |
| Embedder BGE-M3 local | RAM del proceso | **No.** N × 4,3 GB. Resuelto por la decisión 1. |
| Control de admisión | *no existe* | **N/A — D-033.** No hay semáforo ni límite de turnos concurrentes en ninguna parte del código. La advertencia del roadmap de que "el techo real es 4× lo que escribiste" todavía no aplica, porque no hay nada escrito. |
| `app.state.runtimes` | cacheado al boot | Sí — solo lectura después de que el lifespan lo construye. |
| `_pending_emits` | set de módulo, `injector.py:37` | Sí — es por proceso por diseño, y está bien así. |
| `@lru_cache` de `get_settings` | `config.py:217` | Sí — solo lectura. |
| Checkpointer (estado de conversación) | Redis | Sí — ya es compartido. |
| Marcas de dedup | Redis, TTL de 300s | Sí — ya es compartido. |
| Cliente de WhatsApp | httpx | Sí — sin estado. |

### 4. Qué pierde un reinicio

- **Hasta 1000 eventos de auditoría encolados por proceso**, más lo que el
  drainer tuviera a mitad de lote. Hoy es peor de lo necesario:
  `AuditSink.stop()` cancela el drainer antes de que su flush de shutdown pueda
  correr (**D-041**, pineado como xfail estricto), así que incluso un apagado
  ordenado pierde la cola.
- **Nada más.** El estado de conversación vive en el checkpointer de Redis y
  las marcas de dedup viven en Redis con TTL de 300 s, así que ambos sobreviven
  a un reinicio. Los turnos en vuelo se pierden, y por eso la ejecución en
  background de D-030 tiene que ir junto con el claim/release de dedup de
  D-031 — si no, un reinicio a mitad de turno se traga el mensaje del cliente
  en silencio.

## Consecuencias

Ordenadas como deben hacerse, con lo que cambió en cada una:

| Tarea | Era | Ahora |
|---|---|---|
| **D-033** control de admisión | medium | **Requerido para la Etapa A.** Hoy nada acota los turnos concurrentes. Sin límite, 100 conversaciones que llegan abren 100 turnos y agotan el pool sin importar la cantidad de procesos. Es la protección más barata de la lista y hace falta con 10, no solo con 100. |
| **D-030** el webhook devuelve 200 antes del turno | high | Sin cambios, y ahora desbloqueada. Requerida para la Etapa A. |
| **D-031** claim/release de dedup | medium | Tiene que aterrizar junto con D-030, por lo dicho arriba. |
| **D-032** dimensionamiento del pool | **low** | **Elevada.** Es la restricción que ata a partir de 3 procesos, así que es prerrequisito de la Etapa B, no una optimización. Configurar `pool_size`/`max_overflow` desde settings en ambos engines, dimensionados como `max_connections` ÷ procesos esperados, con margen. |
| **D-042** `_seq_counter` | medium | Ahora es decidible. Como la Etapa B es un objetivo real, el arreglo tiene que sobrevivir a N procesos: mover la secuencia a la base de datos (una secuencia por correlación, o una expresión en el INSERT), no un contador en memoria más astuto. Limpiar por request no alcanza. |
| **D-041** `stop()` pierde la cola | medium | Sin cambios, y más importante en la Etapa B, donde N procesos pierden cada uno su cola en cada despliegue. |
| **D-046** exponer `dropped_count` | medium | Sube de valor: en la Etapa B es la única forma de ver la pérdida agregada de auditoría. |
| **D-036** provisioning del servidor | high | Ya tiene su insumo: la Etapa A despliega una unidad; la Etapa B necesita la cantidad de unidades parametrizada y el pool dimensionado en consecuencia. |

## Lo que deliberadamente no se decide acá

- **La cantidad exacta de workers de la Etapa B.** Depende del tiempo de CPU
  por turno, que no está medido. La secuencia correcta es: llegar a la Etapa A,
  medir latencia real y CPU bajo carga, y recién ahí dimensionar. Elegir un
  número ahora sería una adivinanza disfrazada de decisión.
- **Si conviene correr un servicio de embeddings dedicado** en lugar de un
  proveedor remoto. Ver decisión 1.
- **`shared_buffers` y el tuning de PostgreSQL.** 128 MB es el default de
  fábrica y es casi seguro incorrecto para el objetivo, pero tunear antes de
  tener carga que medir es prematuro.

## Todavía sin medir

Registrado para que nadie confunda ausencia con cero:

- Tiempo de CPU por turno y latencia real bajo carga concurrente.
- Latencia de `LocalBGEEmbeddingProvider.embed()` para una consulta corta — el
  número que diría cuánto margen tiene realmente el camino local con 10
  concurrentes.
- Memoria residente de un BGE-M3 cargado (4,3 GB es el tamaño en disco).
- Si `max_connections=100` es lo que va a correr el PostgreSQL de producción; es
  lo que reporta el contenedor de desarrollo.
