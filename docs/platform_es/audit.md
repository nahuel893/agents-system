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
| Drainer | Agrupa eventos en lotes y emite un INSERT por lote |
| `drain()` | Espera a que la cola se vacíe. **No** cubre el lote en vuelo del drainer |

Descartar bajo presión es deliberado. La alternativa — bloquear a quien llama
hasta que la escritura de auditoría termine — convierte a la auditoría en una
dependencia de latencia del camino de la petición, que es exactamente lo que el
contrato *fire-and-forget* existe para evitar. `dropped_count` y la advertencia
`audit.event_dropped` son la señal de que hay que subir el tamaño de la cola.

### Hueco conocido: el apagado pierde la cola final

`stop()` setea `_shutdown` y acto seguido cancela la task del drainer. El flush
de cierre del drainer vive *después* de su bucle `while not self._shutdown`, pero
la cancelación lanza `CancelledError` dentro del `queue.get()` esperado, así que
ese flush nunca se alcanza. Todo evento que siga encolado, más el lote en vuelo
del drainer, se descarta en un apagado ordenado.

Está fijado como xfail con `strict=True` en `tests/test_audit_wiring.py`.
Arreglarlo implica cambiar el handshake de apagado — `stop()` debe esperar la
salida del propio drainer en lugar de cancelarlo — y eso pertenece al trabajo de
despliegue de W4. Como el marcador es estricto, la suite se pone en rojo en
cuanto alguien arregle el código sin quitarlo.

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

`create_orm_owned_tables()` respeta esa bandera, y tanto `scripts/init_db.py`
como las fixtures de test pasan por ahí. El descubrimiento es por bandera y no
por una lista de nombres, así que una segunda tabla particionada hereda el
comportamiento sin cambiar código.

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

1. Agregar el sub-modelo en `src/agentsys/audit/events.py` y registrarlo en el
   mapa de dispatch. Los campos van en `payload`, no en el sub-modelo, salvo que
   necesiten indexarse.
2. Agregar una corrutina `record_*` en `src/agentsys/audit/recorder.py`. Debe
   construir el payload y pasarlo por `_build_and_redact` — nunca construir el
   evento directamente, o la PII saltea el redactor.
3. Llamarla desde el harness mediante `_emit("record_tu_evento", ...)`. Nunca
   `_emit_async`.
4. Verificar **entrega**, no invocación. Un test que comprueba que `_emit` fue
   llamado pasa igual contra una corrutina descartada. Ejercitar un llamador real
   y verificar que el evento llega a `_flush_batch`; `tests/test_audit_wiring.py`
   tiene el patrón `CapturingSink` para eso.

## Señales operativas

| Evento de log | Significado | Acción |
|---|---|---|
| `audit.event_dropped` | Cola llena, evento perdido | Subir `maxsize`, o investigar demoras del drainer |
| `audit.emit_failed` | El camino de emisión lanzó una excepción | Leer `exc_info` — la petición en sí no se vio afectada |
| `audit.emit_skipped_no_loop` | No hay loop de eventos en ejecución | Esperado en tests síncronos y entrypoints de CLI |

## Implementación

- `src/agentsys/audit/events.py` — la unión discriminada
- `src/agentsys/audit/redactor.py` — política default-deny de PII
- `src/agentsys/audit/recorder.py` — constructores de eventos, uno por tipo
- `src/agentsys/audit/sink.py` — cola, drainer, escrituras por lote
- `src/agentsys/harness/injector.py` — `_emit` / `_emit_async`
- `src/agentsys/models/audit_event.py` — modelo ORM, propiedad de Alembic
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
