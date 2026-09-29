# Memoria de conversación: el checkpointer durable

Un hilo de conversación es el historial de mensajes que un agente conserva
entre turnos: lo que escribió el cliente, lo que respondió el agente y cada
llamada a herramienta con su resultado. La app servida guarda cada hilo en
PostgreSQL, así que un reinicio, un `docker compose down` o la recreación de
un contenedor ya no pierden una conversación (issue #24, ADR-002 A.4 y G.22).

| | |
|---|---|
| **Quién lo usa** | El webhook de WhatsApp, y cada canal registrado con `create_app(channels=...)`, cuando `WHATSAPP_CHECKPOINTER_ENABLED=true` (el valor por defecto). El id del hilo es la dirección normalizada del cliente; en WhatsApp, el número de teléfono. `POST /v1/chat/completions` sigue sin estado: quien llama envía todo el historial. |
| **Dónde vive** | En la propia base PostgreSQL de la app (`DATABASE_URL`), en cuatro tablas que pertenecen a `langgraph-checkpoint-postgres`: `checkpoints`, `checkpoint_blobs`, `checkpoint_writes`, `checkpoint_migrations`. |
| **Por cuánto tiempo** | Mientras la conversación está activa, y durante `CHECKPOINTER_RETENTION_S` (por defecto 24 horas) después de su último turno. Pasado ese plazo, una purga periódica la borra. |
| **Qué hace una falla** | El turno igual se responde. Un hilo que no se puede leer se responde sin su historial; un turno que no se puede guardar se responde y no se recuerda. |

## Por qué PostgreSQL

La plataforma ya eligió PostgreSQL para el estado durable. Ahí viven el inbox
y el outbox de webhooks que hacen durable la entrega de mensajes (archive
#43, #44 y #46; revisiones de Alembic 002 a 004; `services/outbox.py`) y el
registro de auditoría. La memoria de conversación reutiliza esa decisión en
vez de apartarse de ella:

- **Un solo almacenamiento durable para operar, respaldar y restaurar.** Redis
  con AOF y un volumen habría sido un segundo, con su propia configuración de
  persistencia, sus respaldos y sus modos de falla.
- **El mismo dominio de falla que el resto del turno.** Un turno ya necesita
  la base: para su fila del inbox y para sus eventos de auditoría.
- **Sin una build especial.** El checkpointer de Redis necesitaba Redis 8 con
  el motor de consultas (RediSearch); el de Postgres necesita el
  `postgres:16` que el stack ya corre.

Se quita el checkpointer de Redis. Redis queda en el stack solo para la
sonda de `GET /health`.

## Cómo lo usa un turno

El grafo del agente siempre corre sin checkpointer. El runtime lee el
historial del hilo una vez antes del turno y guarda los mensajes nuevos del
turno una vez después, como un único checkpoint. Por eso:

- **Un hilo solo contiene turnos completos.** Un proceso que muere en medio
  de un turno deja el hilo exactamente donde terminó el turno anterior. El
  outbox durable vuelve a entregar el mensaje, y el turno se ejecuta de nuevo
  desde ahí.
- **Un turno que agotó su tiempo se guarda tal como lo recibió el cliente:**
  las llamadas a herramientas que el timeout interrumpió quedan respondidas
  con un error, seguidas por la respuesta de timeout. El turno siguiente
  continúa desde la conversación que vio el cliente.
- **El presupuesto por turno (`total_execution_timeout_s`) acota el trabajo
  del propio agente.** Leer el hilo y guardar el turno tienen cada uno su
  propio tope, `CHECKPOINTER_TIMEOUT_S`, que el runtime aplica del lado del
  cliente. Una base que deja de responder sin rechazar nada, como un servidor
  congelado o una partición de red, se abandona pasado ese tiempo y se trata
  como una lectura o un guardado fallidos. No puede retener el turno, su lugar
  de admisión ni su lease del outbox. La llamada abandonada se cancela en
  segundo plano.

## La política ante fallas

La política depende de dónde falló el checkpointer, nunca de qué librería
cliente lanzó el error. Es la misma para cualquier saver de LangGraph que un
usuario de la librería pase a `AgentRuntime(checkpointer=...)`.

| Dónde falla | Qué recibe el cliente | Qué pasa con el hilo |
|---|---|---|
| Al leer el hilo: la base está caída o no responde dentro de `CHECKPOINTER_TIMEOUT_S`, el pool está agotado, o el checkpoint guardado de este hilo no se puede cargar | Una respuesta a este turno, sin el historial anterior | No se guarda nada. Un hilo que no se pudo leer nunca se sobrescribe con uno al que le faltan sus turnos anteriores. |
| Al guardar el turno, o el guardado no responde dentro de `CHECKPOINTER_TIMEOUT_S` | La respuesta terminada, tal cual | Queda en su turno anterior. No se vuelve a ejecutar nada: no se repite ninguna llamada al modelo ni se reejecuta ninguna herramienta. Un guardado abandonado por su tope todavía puede completarse si la base ya lo estaba escribiendo; el hilo queda entonces con el turno, completo. |

Ambos casos se registran como `runtime.checkpointer_degraded`, con
`operation` (`read` o `write`), `thread_id`, `error_type` y `error`, y se
cuentan en `agent_checkpointer_degraded_total{operation}` (ver
`docs/platform_es/observability.md`). El turno mismo igual informa `ok`, así
que ese contador es lo que muestra un checkpointer que falla en cada turno. El
uso de tokens del turno sigue siendo exacto, porque nunca se vuelve a
ejecutar nada.

Esto reemplaza la política que tenía el checkpointer de Redis. Esa política
atrapaba solo los errores propios de Redis y reintentaba el turno entero sin
checkpointer. Un guardado que fallaba después de ejecutar una herramienta
volvía a ejecutarla, el uso del turno se informaba como desconocido, y el
error de cualquier otro backend hacía fallar el turno.

**Un hilo roto nunca hace fallar todos los turnos.** Un hilo cuyos datos
guardados están dañados se sigue respondiendo, sin su memoria, y ningún otro
hilo se ve afectado. Como no se vuelve a escribir, queda inactivo, y la purga
de retención lo borra pasado `CHECKPOINTER_RETENTION_S`. El mensaje siguiente
empieza entonces un hilo nuevo. Para limpiarlo antes, borrá el hilo (ver
"Borrar la conversación de una persona" más abajo).

**Una conexión caída se recupera.** El checkpointer tiene su propio pool de
conexiones. Una conexión rota por un reinicio de la base o un corte de red se
reemplaza la próxima vez que un turno necesita una. Mientras la base está
caída, los turnos se responden sin memoria en vez de fallar.

## Retención: un valor separado y configurado

La durabilidad decide si un reinicio pierde una conversación. La retención
decide cuánto tiempo se conserva una conversación inactiva. Son settings
separados, y cambiar dónde se guardan las conversaciones no cambia cuánto
tiempo se conservan.

| Setting | Por defecto | Significado |
|---|---|---|
| `CHECKPOINTER_RETENTION_S` | `86400` (24 h) | Cuánto tiempo se conserva una conversación después de su último turno. `never` las conserva sin límite. Debe ser positivo. |
| `CHECKPOINTER_PURGE_INTERVAL_S` | `3600` | Cada cuánto corre la purga. También corre una vez al arrancar. |
| `CHECKPOINTER_POOL_SIZE` | `5` | Las conexiones propias del checkpointer. Se suman a los demás pools de la app frente al `max_connections` de PostgreSQL. |
| `CHECKPOINTER_TIMEOUT_S` | `5.0` | Tope para una lectura del hilo y para un guardado, que aplica el runtime. También acota obtener una conexión del pool, abrir una (`connect_timeout`, al menos 2 segundos), cada sentencia (`statement_timeout`) y los datos de red sin confirmar (`tcp_user_timeout`). |

El valor por defecto conserva la ventana de 24 horas que tenía el TTL de
Redis. Es un valor por defecto de la plataforma, no una política: cuánto
tiempo conserva un negocio las conversaciones de sus clientes es decisión de
ese negocio, así que configuralo explícitamente.

La purga corre dentro del proceso de la app:

1. Borra cada hilo cuyo último turno es anterior a la retención: todas sus
   filas, en las tres tablas.
2. Poda cada hilo hasta dejar solo su último checkpoint. Cada guardado
   escribe una copia completa del historial, y la plataforma nunca lee una
   anterior, así que conservarlas haría crecer el almacenamiento con el
   cuadrado del largo de una conversación. El último checkpoint sigue
   conteniendo todo el historial.

Una conversación inactiva por más tiempo que la retención se borra, entonces,
dentro de `CHECKPOINTER_RETENTION_S + CHECKPOINTER_PURGE_INTERVAL_S`. Cada
corrida registra `checkpointer.purged` solo con conteos (`threads_deleted`,
`checkpoints_pruned`, `retention_s`), nunca un id de hilo. Una corrida fallida
registra `checkpointer.purge_failed` con `consecutive_failures`, y el
intervalo siguiente lo reintenta. Cada corrida se cuenta en
`agent_checkpoint_purges_total{outcome}`: corridas que siguen fallando
significan conversaciones que sobreviven a su retención, así que alertá
sobre ellas.

## Datos personales

Un hilo es un dato personal. Contiene cada mensaje que envió el cliente y cada
respuesta, resultados de herramientas como detalles de pedidos, nombres o
direcciones, y su clave es el número de teléfono del cliente.

- **Qué significa la ventana.** Una conversación se conserva completa
  mientras está activa, y durante `CHECKPOINTER_RETENTION_S` después de su
  último turno, más hasta un intervalo de purga. Un cliente que escribe todos
  los días conserva un único hilo que crece.
- **`never` es una elección deliberada.** Conservar las conversaciones sin
  límite requiere una razón y una base legal documentadas en el deployment que
  lo configura.
- **Durable ahora significa durable también en los respaldos.** Redis perdía
  las conversaciones al reiniciar. PostgreSQL las conserva, así que también
  llegan a los respaldos de la base, los archivos WAL y las réplicas, bajo la
  retención propia de esos sistemas. Revisá la retención de los respaldos al
  fijar `CHECKPOINTER_RETENTION_S`: una conversación purgada sigue en un
  respaldo tomado antes de la purga.
- **Borrado significa un `DELETE` de SQL.** El espacio lo recupera el vacuum
  de PostgreSQL. Los logs son aparte: `runtime.checkpointer_degraded` nombra
  el id del hilo, así que la retención de los logs también se aplica a los
  números de teléfono.
- **Los demás almacenamientos tienen su propia retención.** El registro de
  auditoría (`audit_event`, ver `docs/platform_es/audit.md`) y el grabador de
  conversaciones de un deployment son almacenamientos separados. Borrar un
  hilo no los toca.

### Borrar la conversación de una persona

Ante un pedido de supresión, borrá el hilo por su dirección normalizada, por
ejemplo `+5491100000000`:

```sql
DELETE FROM checkpoint_writes WHERE thread_id = '+5491100000000';
DELETE FROM checkpoint_blobs  WHERE thread_id = '+5491100000000';
DELETE FROM checkpoints       WHERE thread_id = '+5491100000000';
```

Desde Python, `await saver.adelete_thread("+5491100000000")` hace lo mismo.
En un canal que no es WhatsApp, el id del hilo lleva como prefijo el nombre
del canal (`services.outbox.channel_scoped_key`).

## Esquema y migraciones

Las tablas pertenecen a `langgraph-checkpoint-postgres`, que versiona su
propio esquema en `checkpoint_migrations`. La revisión 007 de Alembic las crea
ejecutando las migraciones propias del saver, no una copia de su DDL. Por eso:

- las tablas se crean en el paso de migración del deployment
  (`alembic upgrade head`), con el rol de migración, como todas las demás;
- la app nunca ejecuta DDL al arrancar, y se niega a arrancar cuando el
  esquema falta o es más viejo que el que necesita el saver instalado: el
  error indica ejecutar `alembic upgrade head`;
- la app también se niega a arrancar cuando su propio rol de base no puede
  guardar hilos en esas tablas. Cuando la app se conecta con un rol distinto
  del rol de migración, otorgale `SELECT, INSERT, UPDATE, DELETE` sobre
  `checkpoints`, `checkpoint_blobs` y `checkpoint_writes`, y `SELECT` sobre
  `checkpoint_migrations`. Un rol que solo pudiera leer arrancaría, y después
  olvidaría cada conversación y nunca purgaría ninguna. Una conexión de solo
  lectura (`default_transaction_read_only`, o un servidor standby) se rechaza
  por la misma razón. El error nombra cada privilegio que falta;
- `alembic revision --autogenerate` ignora estas tablas (`alembic/env.py`).

Cuando una versión nueva de `langgraph-checkpoint-postgres` trae migraciones
nuevas, una base que ya está en la revisión 007 no las recibe. La suite
offline falla primero (`tests/test_checkpointer_service.py`): agregá una
revisión de Alembic nueva que llame a
`services.checkpointer.apply_checkpoint_migrations`, y después subí
`CHECKPOINT_SCHEMA_VERSION`.

Bajar de la revisión 007 elimina las cuatro tablas y con ellas todas las
conversaciones.

## Actualizar desde el checkpointer de Redis

1. **Desplegá la versión nueva y ejecutá `alembic upgrade head`.** El paso
   migrate de `docker-compose.prod.yml` y la unidad de migración de systemd ya
   lo hacen.
2. **Renombrá `CHECKPOINTER_TTL_S` a `CHECKPOINTER_RETENTION_S`.** El nombre
   viejo se sigue leyendo, como la retención que siempre fue, y el arranque
   registra `checkpointer.deprecated_setting`. Configurar ambos se rechaza al
   arrancar.
3. **Esperá un comienzo nuevo por conversación.** Los hilos guardados en Redis
   no se copian. El próximo mensaje de cada cliente empieza un hilo nuevo. Con
   el TTL anterior de 24 horas, esos hilos habrían vencido dentro de un día de
   todos modos. Una vez que corre la versión nueva, las claves viejas de Redis
   se pueden borrar o dejar vencer.
4. **Revisá el presupuesto de conexiones.** El pool del checkpointer suma
   hasta `CHECKPOINTER_POOL_SIZE` conexiones por proceso (ver
   `docs/operations_es/deployment.md`).

Volver a la versión anterior: su código usa Redis otra vez. Las
conversaciones guardadas en PostgreSQL quedan en sus tablas hasta que
`alembic downgrade 006` las elimina.

**Usuarios de la librería.** El extra `server` ya no instala
`langgraph-checkpoint-redis`; el checkpointer de Postgres viene con el extra
`postgres`, que `server` incluye. `AgentRuntime(checkpointer=...)` acepta
cualquier saver de LangGraph, y la política ante fallas de arriba se aplica a
todos. Cada lectura y cada guardado quedan acotados por
`AgentRuntime(checkpointer_timeout_s=...)`, 5 segundos por defecto. Para
guardar los hilos en Redis, instalá ese paquete vos mismo y pasá su saver.
Para usar PostgreSQL fuera de la app servida,
`services.checkpointer.open_postgres_checkpointer(database_url,
pool_size=..., timeout_s=...)` abre el mismo saver con pool. Creá sus tablas
con `apply_checkpoint_migrations(database_url)` o con el `setup()` propio del
saver.

## Evidencia

- `tests/test_checkpointer_failure_policy.py`: la política ante fallas con
  excepciones simples y sin backend (una lectura fallida, un guardado
  fallido, un hilo roto, un guardado por turno, un turno que agotó su tiempo,
  un checkpoint que se decodifica en algo que no es una lista de mensajes,
  una lectura o un guardado que nunca responden, y un driver lento para
  cancelar que no retiene el turno).
- `tests/test_checkpointer_service.py`: el string de conexión, los timeouts
  de conexión, sobre qué se niega a arrancar la app, la alarma de versión del
  esquema, el ciclo de purga, y la ausencia de un `DeltaChannel`
  que haría insegura la poda.
- `tests/test_checkpointer_integration.py`, contra un PostgreSQL real (job de
  CI `checkpointer-integration`, `CHECKPOINTER_TEST_DATABASE_URL`): la
  revisión 007 y su downgrade, una base sin migrar rechazada, una
  conversación que sobrevive a un reinicio del proceso (cada turno en su
  propio proceso de Python), un rol que no puede guardar ni purgar hilos y un
  rol de solo lectura, ambos rechazados, la purga de hilos inactivos y de checkpoints
  reemplazados, un hilo corrupto que se degrada solo, y una conexión caída que
  se recupera en el turno siguiente.
