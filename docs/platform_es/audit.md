# Registro de auditoría (*Audit Trail*)

`docs/platform_es/harness.md` establece *qué* debe poder reconstruirse desde el
registro de auditoría. Este documento describe *cómo* está implementado: el
camino de emisión, la política de redacción, el sink, y el almacén particionado
que hay detrás.

El subsistema tiene una propiedad no negociable: **la auditoría nunca rompe la
petición que está auditando.** Todo punto de emisión es *fire-and-forget*, y toda
falla se absorbe en el límite del subsistema. Y como una falla absorbida que no
deja rastro es indistinguible de una feature que no hace nada, toda absorción se
registra en el log.

## Pipeline

```
Sitio de llamada del harness (síncrono o asíncrono)
  → _emit(recorder_name, **kwargs)        agenda en el loop en ejecución
    → _emit_async()                       resuelve el recorder, captura todo
      → recorder.record_*()               construye el evento tipado
        → Redactor.redact()               elimina PII, devuelve (payload, pii_keys)
          → AuditSink.record()            put_nowait en una cola acotada — retorna ya
            → _drainer_loop()             agrupa en lotes
              → _flush_batch()            un INSERT por lote
                → audit_event_YYYY_MM     PostgreSQL, particionado RANGE por occurred_at
```

El hilo de control de quien llama termina en `record()`. Todo lo posterior a la
cola ocurre en la task del drainer.

### Por qué `_emit` es síncrono

`_emit_async` es una corrutina. Tres de los cuatro módulos que emiten
(`resolve_tool_surface`, `build_runtime`, `_load_skills`) son funciones síncronas
y no pueden esperarla. Llamar a una función corrutina sin `await` construye un
objeto corrutina y lo descarta: sin excepción, sin log, solo un `RuntimeWarning`
que no hace fallar una corrida de tests.

Por eso `_emit` es un `def` común que agenda la corrutina en el loop en ejecución
y **retiene una referencia fuerte a la task**. `asyncio.create_task` guarda solo
una referencia débil; sin el conjunto que la retiene, una task puede ser
recolectada en pleno vuelo. Cuando no hay ningún loop en ejecución — tests
unitarios síncronos, entrypoints de CLI — registra `audit.emit_skipped_no_loop` y
retorna. Eso no es un error.

Nunca llamar a `_emit_async` directamente desde un sitio de llamada. Usar `_emit`.

## Eventos

Diez tipos de evento, modelados como unión discriminada de Pydantic v2 sobre
`event_type`. Todos comparten una base común:

| Campo | Tipo | Significado |
|---|---|---|
| `event_id` | `UUID` | Único por evento |
| `occurred_at` | `datetime` | UTC. También es la clave de partición |
| `correlation_id` | `str` | Agrupa todos los eventos de una ejecución |
| `sequence` | `int` | Monótono dentro de un `correlation_id` |
| `role` | `str` | La definición de agente que estaba activa |
| `deployment` | `str \| None` | Qué deployment restringió ese rol |
| `actor` | `str \| None` | Identidad del disparador |
| `payload` | `dict` | Datos específicos del evento, almacenados como JSONB |
| `pii_keys` | `list[str]` | Claves de primer nivel del payload cuyos valores se redactaron |

`pii_keys` es lo que vuelve auditable a la redacción: el registro declara qué
campos se eliminaron, de modo que un operador puede distinguir "no había ningún
teléfono" de "había un teléfono y se eliminó".

La fila del ORM no es un espejo uno a uno de este modelo. Cuatro campos se
promueven fuera de `payload` a columnas reales — `event_type`, `tool_name`,
`policy_decision`, `policy_reason` — porque son sobre lo que filtra una consulta
de incidente, y filtrar JSONB es más lento que filtrar una columna. El resto
permanece en `payload`.

### Tipos de evento y dónde se emiten

| Evento | Emitido desde | Significado |
|---|---|---|
| `tool_granted` | `harness/injector.py` | Una herramienta entró en la superficie inyectada (Capa 1) |
| `tool_denied` | `harness/injector.py` | Una herramienta se retuvo de la superficie |
| `unknown_tool` | `harness/injector.py` | Un rol nombró una herramienta que no está en el registry |
| `skill_loaded` | `harness/factory.py` | Una skill se adjuntó al runtime |
| `skill_missing` | `harness/factory.py` | Una skill nombrada no pudo resolverse |
| `runtime_built` | `harness/factory.py` | Se ensambló un `EquippedRuntime` |
| `runtime_initialized` | `agent/graph.py` | El grafo aceptó un turno |
| `runtime_timeout` | `agent/graph.py` | El turno excedió `total_execution_timeout_s` |
| `tool_call_attempted` | `harness/interceptor.py` | Una llamada alcanzó la Capa 2 |
| `tool_call_blocked` | `harness/interceptor.py` | La Capa 2 rechazó la llamada |

Doce sitios de llamada para diez tipos de evento. `tool_call_blocked` se emite
desde tres ramas distintas del interceptor — `not_in_surface`,
`revalidation_required` y `permission_revoked` — y por eso los conteos difieren.

Conviene notar que `unknown_tool` es un evento de **tiempo de construcción**. Se
dispara en el injector cuando un manifiesto de rol nombra una herramienta que el
registry no tiene, inmediatamente antes de lanzar `InjectionError`; el runtime
nunca arranca. No es el caso `not_in_surface` del interceptor, que es un *modelo*
nombrando una herramienta en tiempo de llamada contra un runtime que sí se
construyó.

## Redacción

`Redactor.redact(payload, audit_policy)` devuelve `(payload_redactado, pii_keys)`.
La política es **default-deny**: un valor se conserva solo si se sabe que es
seguro.

| Categoría | Comportamiento | Marcador |
|---|---|---|
| Números de teléfono | Siempre redactados, detectados con `phonenumbers` | `[REDACTED:phone]` |
| Direcciones de correo | Siempre redactadas, detectadas por regex | `[REDACTED:email]` |
| Claves de texto libre (`message`, `body`, `text`, `email`) | Redactadas **salvo** que `audit_policy.capture_tool_input` sea `True` | `[REDACTED:body]` |
| Claves listadas en `audit_policy.redact_keys` | Siempre redactadas | `[REDACTED:custom]` |

Los teléfonos y correos se eliminan incluso con `capture_tool_input` activado:
optar por capturar texto libre no es optar por almacenar identificadores de
clientes.

La detección corre sobre los *valores*, no solo sobre los nombres de clave, así
que un teléfono embebido en el mensaje de error de un driver o en el motivo de
una denegación de política queda cubierto. Esto importa: el payload de auditoría
es el único lugar donde se persiste textualmente la cadena de una excepción
proveniente de un conector externo.

## Sink

`AuditSink` es un singleton de proceso (`AuditSink.current()`), construido en el
lifespan de FastAPI y al que se le entrega la session factory respaldada por el
`AsyncEngine` de la aplicación.

| Propiedad | Comportamiento |
|---|---|
| `record(event)` | `put_nowait` en una cola acotada. Retorna en ~5 ms. **Nunca lanza excepción.** |
| Cola llena | Incrementa `dropped_count`, registra `audit.event_dropped`, descarta el evento |
| `record()` antes de `start()` | Lanza `RuntimeError` — es una mala configuración, no una condición de runtime |
| `record()` con el sink ya cerrado | Se cuenta en `dropped_count`, registra `audit.event_dropped_shutdown`, nunca lanza excepción. El sink se cierra en el barrido final del drainer; hasta entonces — incluso mientras `stop()` espera — los eventos se siguen aceptando y escribiendo |
| Drainer | Agrupa eventos en lotes y emite un INSERT por lote |
| `drain()` | Espera a que la cola se vacíe. **No** cubre el lote en vuelo del drainer |

Descartar bajo presión es deliberado. La alternativa — bloquear a quien llama
hasta que la escritura de auditoría termine — convierte a la auditoría en una
dependencia de latencia del camino de la petición, que es exactamente lo que el
contrato *fire-and-forget* existe para evitar. `dropped_count` y la advertencia
`audit.event_dropped` son la señal de que hay que subir el tamaño de la cola.

### El apagado drena la cola final (arreglado — #8)

`stop()` seteaba `_shutdown` y acto seguido cancelaba la task del drainer. El
flush de cierre del drainer vive *después* de su bucle `while not
self._shutdown`, pero la cancelación lanzaba `CancelledError` dentro del
`queue.get()` esperado, así que ese flush nunca se alcanzaba — todo evento que
siguiera encolado, más el lote en vuelo del drainer, se descartaba en un
apagado ordenado.

`stop(timeout: float = 5.0)` ahora **espera** la task del drainer en vez de
cancelarla, que es lo que permite que su flush de cierre corra de verdad y
vacíe lo que haya quedado. El `timeout` acota esto para que un drainer trabado
(por ejemplo, una llamada a la DB colgada) no cuelgue el apagado para siempre:
al vencer el timeout, `asyncio.wait_for` cancela el drainer como último
recurso y `stop()` registra `audit.shutdown_timeout`. El valor por defecto es
compatible hacia atrás con cada sitio de llamada existente `await
sink.stop()` sin argumentos.

**Todo evento entregado al sink se escribe o se cuenta** (revisión del PR
#72). `dropped_count` es la única señal agregada de pérdida (ADR-001 D-046),
así que:

- `stop()` cuenta el lote que un drainer cancelado estaba volcando
  (`in_flight_events_dropped`) y lo que sigue encolado porque ningún drainer
  lo barrió (`queued_events_dropped`) — tras un timeout, o cuando el drainer
  murió antes de `stop()` (en ese caso registra
  `audit.shutdown_events_dropped`). Tras una salida limpia ambos son cero y no
  se registra nada.
- El drainer cierra el sink inmediatamente antes de su barrido final, sin
  ningún `await` en el medio: todo `record()` aceptado antes de ese punto está
  en el lote final, y todo `record()` posterior se rechaza y se cuenta
  (`audit.event_dropped_shutdown`) en vez de quedar en una cola que nadie lee.
- Un flush fallido (`audit.drain_failed`) suma su lote entero.

Esto estaba fijado como xfail con `strict=True` en `tests/test_audit_wiring.py`
(`test_stop_does_not_lose_queued_events`); el marcador ya fue quitado y el test
pasa de verdad.

## Almacenamiento

`audit_event` está particionada por rango sobre `occurred_at`, mensualmente, más
una partición `DEFAULT`. Dos consecuencias que conviene conocer antes de tocar el
modelo:

**Todas las claves son compuestas.** PostgreSQL exige la clave de partición en
toda restricción `PRIMARY KEY` y `UNIQUE` de una tabla particionada, lo que
reforma ambas:

| Restricción | Columnas |
|---|---|
| `pk_audit_event` | `(occurred_at, id)` — `id` es `BIGINT GENERATED ALWAYS AS IDENTITY` |
| `uq_audit_event_correlation_sequence` | `(occurred_at, correlation_id, sequence)` |

Por lo tanto `event_id` (el UUID que lleva el evento Pydantic) *no* es una
columna de clave primaria: tiene su propia restricción `UNIQUE`. El orden dentro
de una ejecución sale de `(correlation_id, sequence)`, no de `id`.

SQLite no puede expresar autoincremento sobre una clave primaria compuesta, que
es la razón inmediata por la que ninguna fixture de SQLite puede crear esta tabla.

**El ORM no debe crearla.** SQLAlchemy no puede expresar particionado. En SQLite
lanza error; en PostgreSQL produce en silencio una tabla plana, sin particionar,
distinta de la que construye la migración — que es peor, porque nada lo reporta.
Por eso la tabla declara su propiedad y la creación la saltea:

```python
class AuditEvent(Base):
    __tablename__ = "audit_event"
    __table_args__ = {"info": {ALEMBIC_OWNED: True}}
```

`alembic_owned_tables()` descubre esa bandera; nada en `Base.metadata` puede
crearse en bloque desde metadata del ORM si aparece ahí. El descubrimiento es
por bandera y no por una lista de nombres, así que una segunda tabla
particionada hereda el comportamiento sin cambiar código. `audit_event` es
hoy la única tabla que declara el ORM (#70 eliminó las tablas propias del
cliente y el stack de pgvector), así que no queda nada que el ORM deba
crear — `alembic upgrade head` es dueño de todas las tablas.

### La partición `DEFAULT` es un resguardo de disponibilidad, no una comodidad

Sin ella, un insert cuyo `occurred_at` cae fuera de todas las particiones
declaradas es rechazado de plano:

```
ERROR:  no partition of relation "audit_event" found for row
```

Eso detendría la auditoría en una fecha del calendario, sin cambio de código y
sin nada en un diff a lo que culpar, volviendo al job mensual de particiones una
dependencia dura de disponibilidad. Con la partición `DEFAULT`, las filas siempre
aterrizan, y agregar particiones mensuales vuelve a ser lo que debería ser: una
optimización de podado de consultas y retención.

Las particiones se llaman `audit_event_YYYY_MM`, con el nombre calculado desde
los mismos límites que su rango y con fronteras UTC explícitas. `occurred_at` es
`TIMESTAMPTZ`, y un literal de fecha sin zona se interpreta en el `TimeZone` de
la sesión, lo que partiría el mes distinto según quién corriera la migración.

## Agregar un tipo de evento nuevo

1. Agregar el sub-modelo en `src/agents_system/audit/events.py` y registrarlo en el
   mapa de dispatch. Los campos van en `payload`, no en el sub-modelo, salvo que
   necesiten indexarse.
2. Agregar una corrutina `record_*` en `src/agents_system/audit/recorder.py`. Debe
   construir el payload y pasarlo por `_build_and_redact` — nunca construir el
   evento directamente, o la PII saltea el redactor.
3. Llamarla desde el harness mediante `_emit("record_tu_evento", ...)`. Nunca
   `_emit_async`.
4. Verificar **entrega**, no invocación. Un test que comprueba que `_emit` fue
   llamado pasa igual contra una corrutina descartada. Ejercitar un llamador real
   y verificar que el evento llega a `_flush_batch`; `tests/test_audit_wiring.py`
   tiene el patrón `CapturingSink` para eso.

## Secuenciado

`sequence` es monótono dentro de un `correlation_id`, y `(occurred_at,
correlation_id, sequence)` es una restricción UNIQUE — así que el contador es lo
que garantiza que los eventos de una ejecución puedan ordenarse después, y un
duplicado hace fallar el INSERT de todo el lote.

### La secuencia se asigna en la base de datos (arreglado — #9)

El asignador solía ser un diccionario a nivel de módulo detrás de un
`asyncio.Lock` en `audit/recorder.py` — correcto dentro de un proceso,
incorrecto entre N: cada proceso contaba el fallback `"none"` de
`correlation_id` (usado cuando no hay contexto de request) desde 1 de forma
independiente, así que dos workers emitiendo un evento sin contexto en el
mismo instante podían asignar la misma secuencia y violar la UNIQUE, perdiendo
el lote entero. El diccionario tampoco se podaba nunca: ganaba una entrada por
`correlation_id` durante toda la vida del proceso.

`_allocate_sequence` y `_seq_counter` ya no existen. Cada helper `record_*`
ahora escribe `PLACEHOLDER_SEQUENCE` (`0`) — un valor que nunca se persiste —
y la secuencia real y autoritativa se reserva atómicamente en el momento del
flush, en `AuditSink._flush_batch`: un upsert por cada `correlation_id`
**distinto** del lote, en orden **ordenado**, cada uno reservando de una vez
el rango contiguo entero de esa correlación:

```sql
INSERT INTO audit_sequence (correlation_id, next_seq)
VALUES (:correlation_id, :count)
ON CONFLICT (correlation_id)
DO UPDATE SET next_seq = audit_sequence.next_seq + EXCLUDED.next_seq
RETURNING next_seq
```

`audit_sequence` (migración `005_audit_sequence`) es una tabla chica y sin
particionar — una fila por `correlation_id`. `next_seq` es el **último** valor
entregado, así que un resultado `r` significa que el lote es dueño de
`r - count + 1 .. r`, repartido en el orden de la cola para que los eventos de
una correlación conserven su orden. El upsert corre en la MISMA sesión a la
que se agregan y con la que se commitean los eventos del lote, así que un lote
fallido revierte su reserva gratis. Lo que hace que esto sobreviva a N
procesos worker, algo que el contador en memoria nunca pudo: el propio row
lock de PostgreSQL sobre cada fila de `audit_sequence` serializa a los workers
concurrentes de *cualquier* proceso, no solo corrutinas que comparten uno.

### Por qué ordenado, y un upsert por correlación (revisión del PR #72)

El row lock de cada upsert se mantiene hasta que el lote commitea, así que el
orden en que un flush emite sus upserts es el orden en que toma los locks. La
primera versión emitía uno por evento, en el orden de la cola: un worker que
tenía `c1` y esperaba `c2` mientras otro tenía `c2` y esperaba `c1` entraba en
deadlock, PostgreSQL abortaba una de las transacciones y el drainer perdía ese
lote entero (18 de 20 corridas en una prueba). Ahora todo flush de todo
proceso toma los locks en un único orden global — `correlation_id` ordenado —
así que un flush posterior hace cola detrás de uno anterior en vez de cerrar
un ciclo. Un upsert por correlación además bloquea cada fila una sola vez por
flush, y cuesta un viaje a la base por correlación distinta en vez de uno por
evento.

`tests/test_audit_migration_integration.py::TestAuditSequenceAllocationIsProcessSafe`
lo prueba contra una instancia real de PostgreSQL, con engines separados:
flushes concurrentes de una misma correlación quedan sin huecos ni
duplicados; lotes en orden opuesto, frenados por una compuerta hasta que cada
uno tiene su primer lock, entran en deadlock en cada corrida sin el
ordenamiento y nunca con él; y lotes mezclados de varias correlaciones desde
tres workers no pierden nada y conservan el orden de la cola. Un test basado
en mocks no puede: los mocks comparten la memoria de un solo proceso y no
toman row locks.

### La migración 005 siembra los contadores

`upgrade()` siembra `audit_sequence` con `MAX(sequence)` por cada
`correlation_id` que ya existe en `audit_event`, así ningún contador vuelve a
1 bajo números que la historia ya usó — sobre todo el fallback compartido
`"none"`. `MAX`, no `count(*)`: los viejos contadores por proceso reiniciaban
en cada arranque, así que la historia tiene huecos y repeticiones.

El costo: ningún índice empieza por `correlation_id`, así que la siembra es un
escaneo secuencial de todas las particiones, `DEFAULT` incluida, dentro de la
transacción de la migración, y escribe una fila de `audit_sequence` por cada
correlation_id histórico distinto. Solo toma `ACCESS SHARE`, así que no
bloquea inserts concurrentes, pero con un `audit_event` grande el upgrade dura
lo que una lectura de la tabla completa — correrlo en una ventana de
mantenimiento. Reiniciar todos los workers después: el código anterior a 005
sigue numerando con su contador en memoria.

### Un segundo escritor independiente: el outbox

`services/outbox.py:_terminalize_work` también escribe en `audit_event` — el
evento `webhook_delivery_terminal_failure` — directamente en su propia sesión,
con `sequence = attempt_count` y `correlation_id = "outbox:<id del trabajo>"`.
No pasa por `audit_sequence`, a propósito:

- **Los espacios de nombres no pueden solaparse.** Todo lo que escribe el sink
  lleva el request id (`uuid4().hex[:8]`, vinculado en
  `observability/middleware.py`: ocho caracteres hexadecimales, nunca dos
  puntos) o `"none"`. Ninguno puede ser igual a `"outbox:<uuid>"`.
- **Su propio espacio de nombres no puede colisionar.** Solo corre en la
  transición terminal, bajo el lock `FOR UPDATE` de la fila con lease vivo, y
  setea `failed_at`, que excluye esa fila de todo lock posterior — así que
  cada `"outbox:<id>"` recibe exactamente un evento, y `attempt_count` es el
  número con sentido para guardar ahí.
- **Pasarlo por el asignador** agregaría una fila de `audit_sequence` por cada
  trabajo fallido y un segundo row lock a la transacción terminal del outbox,
  para prevenir una colisión que no puede ocurrir.

El invariante a mantener: un escritor que comparte un correlation_id con
cualquier otro escritor tiene que reservar a través de `audit_sequence` —
incluido `_terminalize_work`, el día que cualquier otra cosa escriba bajo
`"outbox:*"`.

## Señales operativas

| Evento de log | Significado | Acción |
|---|---|---|
| `audit.event_dropped` | Cola llena, evento perdido | Subir `maxsize`, o investigar demoras del drainer |
| `audit.emit_failed` | El camino de emisión lanzó una excepción | Leer `exc_info` — la petición en sí no se vio afectada |
| `audit.drain_failed` | Falló el INSERT de un lote; esos eventos se perdieron | Leer `error` y `exc_info`. Lleva `batch_size`; el lote se suma a `dropped_count` |
| `audit.emit_skipped_no_loop` | No hay loop de eventos en ejecución | Esperado en tests síncronos y entrypoints de CLI |
| `audit.shutdown_timeout` | El `timeout` de `stop()` venció antes de que el drainer saliera solo; se lo canceló como último recurso | Lleva `queued_events_dropped` e `in_flight_events_dropped`, ambos sumados a `dropped_count`. Investigar qué trabó al drainer (usualmente una llamada a la DB colgada); subir `timeout` solo si el drainer es lento, no si está trabado |
| `audit.shutdown_events_dropped` | `stop()` encontró eventos que ningún drainer iba a volcar — el drainer había muerto antes de `stop()` | Mismos campos que arriba. Averiguar por qué la task del drainer terminó antes de tiempo |
| `audit.event_dropped_shutdown` | `record()` con el sink ya cerrado; el evento se contó, no se escribió | Algo emite eventos de auditoría después de empezado el apagado — encontrar al llamador (usualmente una task en segundo plano que sobrevive al lifespan) |

## Implementación

- `src/agents_system/audit/events.py` — la unión discriminada
- `src/agents_system/audit/redactor.py` — política default-deny de PII
- `src/agents_system/audit/recorder.py` — constructores de eventos, uno por tipo
- `src/agents_system/audit/sink.py` — cola, drainer, escrituras por lote
- `src/agents_system/harness/injector.py` — `_emit` / `_emit_async`
- `src/agents_system/models/audit_event.py` — modelo ORM, propiedad de Alembic
- `alembic/versions/` — tabla, particiones y la partición `DEFAULT`
- `tests/test_audit_wiring.py` — entrega punta a punta, verificada por mutación
- `tests/test_audit_sink.py`, `tests/test_audit_redactor.py`,
  `tests/test_audit_recorder.py` — cobertura unitaria

El comportamiento de particionado está cubierto por tests de integración contra
PostgreSQL real en el job de CI `audit-migration`. No puede testearse
unitariamente: el modelo ORM no lleva particiones, así que ningún test en memoria
puede saber si una fila aterrizó en una.

## Referencias cruzadas

- Requisitos del registro de auditoría: `docs/platform_es/harness.md` (sección de auditoría)
- Control de Capa 2, que emite cuatro de los diez eventos: `docs/platform/interceptor.md`
- Campos de `audit_policy` (`retention_days`, `capture_tool_input`, `redact_keys`): `docs/platform_es/policy.md`
- Modelo de permisos y RBAC: `docs/architecture/permission-model.md`
