# Entrega de webhooks: persistencia durable, recuperación y el worker diferido

W1 agrega una primitiva de persistencia en PostgreSQL para mensajes entrantes de Meta y sus futuros registros de trabajo. W2b2 (ver más abajo) conecta esa primitiva a la ruta activa: `POST /webhook` ahora persiste de forma durable antes de reconocer, y un worker en segundo plano —no el request— ejecuta el turno y envía la respuesta.

## Decisión y modelo de datos de W1

La migración `002` crea dos nuevas tablas de PostgreSQL:

| Tabla | Responsabilidad en W1 |
|---|---|
| `webhook_inbox` | Almacena el ID único del mensaje de Meta, el payload recibido y la marca de tiempo de recepción. |
| `outbox_work` | Almacena una fila durable de trabajo futuro para un mensaje entrante, incluidos los campos de disponibilidad, lease y finalización. |

`outbox_work.inbound_message_id` es único, por lo que existe una fila de trabajo por cada identidad de mensaje de Meta aceptada. El ID único de Meta en el inbox también hace que la entrega repetida de esa identidad converja en la fila ya aceptada, sin crear otra fila de trabajo.

## Aceptación atómica

`accept_inbound_message` crea una fila de inbox y su fila de trabajo en una única sesión de base de datos y luego las confirma juntas. Por lo tanto, una aceptación exitosa persiste ambos registros; si falla el commit, revierte la sesión. Un duplicado solo se reconoce para la restricción de unicidad del ID de Meta en el inbox después del rollback; los demás errores de commit se propagan.

## Límite de migración y downgrade

La migración `002` es aditiva: crea tablas e índices nuevos, inicialmente vacíos, y no modifica los datos de auditoría existentes. Por eso es seguro aplicarla antes de conectar la ruta de W2.

Su downgrade es deliberadamente destructivo solo después de controles de seguridad:

1. Se niega a ejecutarse mientras alguna fila de `outbox_work` esté incompleta (`completed_at IS NULL`), incluido trabajo pendiente o con lease.
2. Cuando ya no hay filas incompletas, elimina `outbox_work` y `webhook_inbox`.
3. Esa eliminación borra también el historial de trabajo completado.

Ejecutar la prueba de migración solo contra una base PostgreSQL nueva y desechable. La prueba hace un downgrade y falla si falta `OUTBOX_TEST_DATABASE_URL`. Nunca apuntarla a una base de aplicación o una base de pruebas compartida:

```bash
OUTBOX_TEST_DATABASE_URL="$ISOLATED_OUTBOX_DATABASE_URL" \
  PYTHONPATH=/home/nh/wt-44-webhook-worker/src \
  /home/nh/agents-system/.venv/bin/python -m pytest -q -m integration tests/test_outbox_migration_integration.py
```

El operador debe crear primero la base desechable, definir `ISOLATED_OUTBOX_DATABASE_URL` con su URL asyncpg y eliminar la base al terminar.

## Estado recuperable del outbox en W2a (solo almacenamiento)

La migración `003` agrega estado recuperable sin cambiar la ruta activa del webhook:

| Campo | Responsabilidad |
|---|---|
| `lease_owner`, `lease_expires_at` | Identidad del worker y su claim acotado ya confirmado. |
| `attempt_count`, `last_error`, `available_at` | Historial de intentos y momento del próximo reintento. |
| `failed_at` | Falla terminal y visible que ya no puede ser reclamada. |
| `outbound_body`, `outbound_send_key` | Respuesta generada y clave estable **interna** de replay, confirmadas antes de un futuro envío al proveedor. |

`claim_available_outbox_work` lee `clock_timestamp()` de PostgreSQL, selecciona filas listas con `FOR UPDATE SKIP LOCKED`, escribe un lease e incrementa el contador de intentos, y hace commit antes de devolver cualquier fila. Por eso un worker concurrente saltea un lease activo; un lease vencido queda habilitado para recuperación. Una fila vencida que ya llegó a `MAX_OUTBOX_ATTEMPTS` se terminaliza con su evento de auditoría bajo ese lock y no recibe un intento adicional. El resultado del claim ya confirmado devuelve esas filas terminalizadas por separado de los claims activos, para que un worker invoque un notificador propio del deployment solo después de que exista la señal durable de auditoría/acción de operador. W2a no procesa filas, no llama a Meta ni ejecuta un loop de workers.

Ante una falla no terminal, se limpia el lease y se confirma un backoff exponencial acotado con full jitter. Se conservan la respuesta saliente y la clave interna: un resultado ambiguo del proveedor debe reintentarse, no deduplicarse con esa clave. Esto es entrega al-menos-una-vez, no exactamente-una-vez. La clave no es un campo ni header de idempotencia de Meta Graph API.

Al llegar a `MAX_OUTBOX_ATTEMPTS`, W2a limpia el lease, fija `failed_at`, conserva el error e inserta un `audit_event` en el **mismo commit de la sesión de base de datos**. `audit_event` tiene una partición DEFAULT, por lo que esta inserción directa sigue siendo segura más allá de las particiones mensuales iniciales. El evento durable incluye `operator_action: required`; su resultado `alert_required` es una señal accionable, no una afirmación de que una persona haya sido notificada. W2b debe conectar esa señal con el puerto de escalamiento humano propio del deployment o con monitoreo.

La migración `003` es aditiva: agrega columnas e índices de recuperabilidad y conserva los índices de W1. Su downgrade se niega si existe cualquier estado W2a, en vez de descartar datos de recuperación o una respuesta persistida. Como en W1, su prueba de integración solo debe ejecutarse con `OUTBOX_TEST_DATABASE_URL` apuntando a una base PostgreSQL aislada y descartable.

## Procesador diferido de W2b1

W2b1 agrega `DeferredWebhookWorker`, un procesador testeable directamente para claims ya confirmados. W2b2 (más abajo) conecta su ciclo de vida `start`/`stop` a la aplicación activa; la lógica de procesamiento descrita en esta sección no cambia por esa conexión. Busca el ID de Meta correspondiente en el sobre firmado persistido y falla cerrado si falta el directorio o el participante es desconocido o está inactivo: no ejecuta un turno ni envía una respuesta. La ausencia del directorio o un error inesperado de normalización se reintenta y puede terminar como falla visible; una dirección inválida (`ValueError`), un participante desconocido/inactivo, un envío exitoso o una respuesta vacía usa finalización cercada bajo el lease vivo del worker según la hora de la base. Un mensaje que no es de texto (imagen, audio, ubicación, documento, ...) se acepta de forma durable y se completa de la misma manera, sin turno y sin respuesta, y su `type` de Meta queda registrado explícitamente (`webhook_worker.non_text_message`); responder a un mensaje que no es de texto es un seguimiento de producto, no algo que este worker intente.

Las fallas de consulta a la base, runtime, turno, persistencia de intent, finalización y envío al proveedor usan la transición cercada `record_outbox_failure` de W2a. Perder el lease en cualquier transición cercada no modifica el claim obsoleto. En el arranque, el `total_execution_timeout_s` **efectivo** de cada runtime vinculado a WhatsApp más 60 segundos de margen no correspondiente al turno (lookup, persistencia de la respuesta, envío al proveedor y finalización) debe quedar **estrictamente por debajo** de `DEFAULT_LEASE_DURATION` (600 segundos): se aceptan 300 y 539 segundos; se rechazan 540 segundos. Los límites parciales o ausentes usan sus valores efectivos predeterminados. El control se aplica solo al runtime elegido por `whatsapp_runtime_id`; los runtimes exclusivos del adapter no corren bajo un lease del outbox. El control reduce recuperaciones previsibles de un lease vivo, pero no crea ejecución exactamente una vez.

Antes de llamar al proveedor, una respuesta generada se confirma como `outbound_body` junto con una clave interna estable derivada del ID de mensaje de Meta. Un claim posterior que tenga esa respuesta persistida envía el mismo cuerpo sin volver a ejecutar el agente. La clave no se coloca intencionalmente en la solicitud de Meta Graph API: la entrega saliente es al-menos-una-vez y un resultado ambiguo del proveedor puede duplicar un mensaje.

El registro de conversación sigue siendo best effort en una sesión independiente. Un deployment puede inyectar un callback asíncrono para fallas terminales. W2b1 lo invoca solo después de que W2a confirma la fila terminal y la señal de auditoría/acción de operador en la misma transacción, incluso si la terminalización se descubrió durante la recuperación del claim; si falta o falla el callback, no se afirma notificación humana y la señal durable se conserva. El esquema actual no guarda de forma durable los intentos de notificación, por lo que no puede garantizar el reintento del callback humano.

## W2b2 la ruta activa: persistir antes de reconocer, nunca procesar en el request

`POST /webhook` (`integration/webhook.py`) ahora hace exactamente cuatro cosas, en este orden: aplicar un límite de tamaño al cuerpo del request, verificar la firma HMAC sobre el cuerpo crudo, recorrer el payload parseado en busca de cada mensaje de Meta válido, y persistir cada uno de forma durable mediante `accept_inbound_message` antes de devolver `{"status": "ok"}`. Nunca ejecuta un turno, nunca llama al directorio de participantes ni al grabador de conversación, y nunca toca el cliente de WhatsApp: todo eso se movió a `DeferredWebhookWorker`, completamente fuera del request.

**Límite de tamaño del body, antes del HMAC (#140).** `read_bounded_body` (`integration/body_limits.py`) aplica `settings.webhook_max_body_bytes` (1 MiB por defecto -- un límite conservador muy por encima de cualquier payload real de la Cloud API de Meta, que se mantiene en pocos KB incluso en batch, porque los medios se obtienen por URL en vez de embeberse) antes de la verificación HMAC y antes de `json.loads`. Un request cuyo `Content-Length` ya supera el límite se rechaza con `HTTPException(413)` sin leer el body; un body ausente, incorrecto o con transfer-encoding chunked igual queda acotado porque el límite también se aplica contra los bytes reales leídos de `request.stream()`. Cualquiera de los dos caminos aborta antes de la persistencia, así que un request sobredimensionado no cuesta ni la CPU del hashing ni la memoria de retener el body completo. Esta primitiva es compartida, no local a esta ruta: `POST /v1/chat/completions` (#37) la reutiliza con su propio límite `adapter_max_body_bytes`, ya que esa ruta puede alcanzarse completamente sin autenticar cuando `adapter_api_key` está sin definir -- ver la sección "Adaptador de OpenAI" en `adr-002-agent-model-and-capabilities.md`.

El 413 es una señal distinta del 403 por HMAC inválido descripto más abajo: significa "demasiado grande para siquiera considerarlo", no "firma inválida". Meta no envía payloads ni cerca del límite por defecto, así que una entrega legítima nunca se ve afectada por esto en la práctica; el setting existe para acotar abuso y errores de configuración, no para condicionar el tráfico normal.

Un "mensaje de Meta válido" es cualquier entrada alcanzable en `entry[].changes[].value.messages[]` que tenga un `id` no vacío. Un evento de actualización de estado, un array `messages` vacío o ausente, o un mensaje sin id no se persisten, igual que el manejo siempre-200 que la ruta anterior a W2b2 ya les daba. El payload parseado **completo** se guarda junto a cada id de mensaje encontrado en él (no solo los campos de ese mensaje), porque un batch puede traer varios mensajes bajo una misma firma de Meta y el worker luego tiene que encontrar el mensaje exacto al que se refiere un id dado dentro de ese sobre compartido (`webhook_worker._extract_inbound_turn`, de W2b1).

**HMAC primero, sin condiciones.** `verify_signature` corre antes de `json.loads` y antes de cualquier llamada de persistencia. Una firma forjada o ausente se rechaza con 403 y nunca llega a la base de datos, sin importar la forma del payload.

**Persistir cada mensaje válido; 503 ante cualquier falla.** Los mensajes de un batch se persisten en orden de entrega, con una llamada a `accept_inbound_message` por cada uno. El primero cuyo commit falle aborta el request con `HTTPException(503)` — Meta reintenta todo el sobre ante un 5xx, y este es el único caso en esta ruta donde eso es exactamente el resultado deseado (una falla transitoria de base de datos), no el loop de mensajes envenenados que el contrato siempre-200 de la ruta anterior (AD-2) existía para evitar ante *contenido* controlado por un atacante. Los mensajes ya confirmados **antes** de esa falla quedan confirmados: no se revierten ni se reintentan en el mismo request. En el reintento de Meta sobre el mismo sobre, `accept_inbound_message` reconoce cada id ya confirmado (`webhook_inbox.meta_message_id` es único) y devuelve `duplicate=True` en lugar de fallar, así que el reintento retoma naturalmente en el mensaje que realmente falló, en vez de repetir trabajo o fallar sobre los que ya habían tenido éxito.

**Un body malformado o controlado por un atacante nunca persiste nada y nunca da 500.** Las fallas de `json.loads` y cada discrepancia de forma del payload que la ruta anterior ya defendía (`entry` que no es una lista, `changes` que no es una lista, `value` que no es un diccionario, etc.) cortan directo a `{"status": "ok"}` antes de cualquier llamada a la base de datos, igual que antes.

## Conexión del worker en el lifespan de W2b2: arrancar después de las dependencias, detener antes del teardown

El `lifespan` de `main.py` construye un `DeferredWebhookWorker` por proceso y arranca su loop de sondeo solo después de que cada dependencia que necesita ya existe en `app.state`: el engine, el cliente de WhatsApp, el caché de runtimes resuelto, y el directorio de participantes / grabador de conversación que `create_app` guarda ahí. Se omite —con una advertencia `webhook_worker.no_runtime_resolved`, no con una falla de arranque— cuando `whatsapp_runtime_id` no está configurado o no resuelve a nada en el caché de runtimes **y WhatsApp no está configurado de ningún otro modo para recibir y responder**; el trabajo entrante durable igual se acumula y simplemente espera a que un operador corrija la configuración, nunca se descarta. Ver "#141 — arranque fail-closed cuando WhatsApp está configurado pero ningún runtime resuelve" más abajo para cuándo esto se convierte en una falla de arranque.

### #141 — arranque fail-closed cuando WhatsApp está configurado pero ningún runtime resuelve

`POST /webhook` (`integration/webhook.py`) se monta **sin condiciones** — acepta de forma durable cada mensaje entrante firmado sin importar si algún worker llegará a procesarlo. Antes de #141, un `whatsapp_runtime_id` vacío o malformado (sin el separador `{deployment}__{role}`) dejaba esa brecha en silencio: la app arrancaba, la ruta seguía aceptando y guardando mensajes, y la única señal era la advertencia `webhook_worker.no_runtime_resolved` de arriba — fácil de pasar por alto, y nada la volvía a emitir a medida que crecía el backlog.

Ahora el lifespan falla cerrado en su lugar, pero solo cuando el operador configuró demostrablemente WhatsApp para recibir y responder: `WHATSAPP_TOKEN` y `WHATSAPP_PHONE_NUMBER_ID` están ambos configurados. En ese caso, no resolver ningún runtime de webhook (`whatsapp_runtime_id` vacío, malformado, o de otro modo ausente del caché de runtimes construido) lanza `DefinitionError` y la app se niega a arrancar, con un mensaje que nombra la corrección exacta (configurar `WHATSAPP_RUNTIME_ID`, o quitar las credenciales de WhatsApp si este despliegue no usa WhatsApp). Un despliegue sin ninguna de las dos credenciales configuradas mantiene el comportamiento previo a #141 de advertir y arrancar — no hay nada configurado para recibir y responder, así que no hay nada que fallar cerrado. Un despliegue con solo una de las dos credenciales configuradas (una configuración genuinamente parcial, por ejemplo en medio de una migración) también mantiene el camino de solo advertencia; la verificación es deliberadamente conservadora sobre cuándo se dispara — "ambas credenciales configuradas" es la única combinación que revisa, y no verifica de forma independiente que el runtime id resuelva a un rol que sea seguro para el input no confiable de WhatsApp, lo cual es responsabilidad de ADR-002 C.13 (justo arriba de esta verificación en `main.py`, y sin condiciones siempre que `whatsapp_runtime_id` coincida con un runtime construido, no solo cuando falla en hacerlo).

`GET /health` (más abajo) cubre el caso complementario, ya en ejecución: un worker que deja de correr (crash, apagado inesperado) después de un arranque exitoso.

## GET /health — actividad del worker de webhook y profundidad del backlog del outbox (#141)

Antes de #141, nada más que la advertencia de arranque de arriba señalaba un backlog estancado: si el worker moría después de un arranque limpio, o nunca corrió por una configuración que la verificación fail-closed de arriba no cubre (por ejemplo, WhatsApp no configurado en absoluto, a propósito), el trabajo entrante durable podía acumularse indefinidamente sin ninguna señal operativa más allá de una consulta a la base de datos que alguien tendría que pensar en correr.

La respuesta de `GET /health` ahora lleva un objeto `webhook_worker`:

```json
{
  "status": "ok",
  "webhook_worker": {
    "running": true,
    "outbox_pending": 0,
    "outbox_leased": 0,
    "outbox_leased_expired": 0
  }
}
```

- `running` lee `DeferredWebhookWorker.is_running` desde `app.state.webhook_worker` (`false` cuando nunca se arrancó ningún worker, o cuando su tarea del poll-loop murió sin pasar por `stop()` — seguimiento de revisión #141 — no es en sí mismo un signo de mala salud para un despliegue sin runtime de WhatsApp configurado).
- `outbox_pending` / `outbox_leased` / `outbox_leased_expired` vienen de `services/outbox.py::count_outbox_backlog`, tres consultas `COUNT(*)` contra `outbox_work` filtradas a filas no terminales (`completed_at IS NULL AND failed_at IS NULL`): `pending` (sin lease), `leased` (cualquier lease, vigente o vencida y sin reclamar), y `leased_expired` (el subconjunto de `leased` cuya lease ya venció según el reloj de la base de datos — filas abandonadas por un worker caído). Las tres usan los mismos índices parciales de los que ya depende `pending_outbox_statement`, así que esto no agrega ningún índice nuevo ni un table scan.
- **La consulta del backlog está acotada al mismo presupuesto de 3s que las verificaciones de postgres/redis de arriba** (seguimiento de revisión #141, BLOCKER): `outbox_work` se consulta a través del mismo engine, y sin su propio timeout una tabla trabada o lenta colgaría todo este endpoint — y cualquier sonda de liveness que lo lea — incluso mientras `postgres`/`redis` arriba responden bien. Ante un timeout, un error de base de datos, o cualquier otra falla, los contadores se reportan como `null` (no `0` — un backlog inalcanzable es un hecho distinto de uno confirmado-vacío). Esto es independiente de `postgres`: una tabla `outbox_work` lenta o trabada puede producir un backlog `null` incluso cuando la verificación simple `SELECT 1` de postgres tiene éxito, así que `postgres: "ok"` por sí solo no significa que los contadores del backlog sean confiables.
- **El `status` general se degrada** cuando el worker no está corriendo **y** `outbox_pending` u `outbox_leased_expired` es un número conocido distinto de cero — la condición de aceptación que pide #141, extendida (seguimiento de revisión #141, BLOCKER) para cubrir leases que un worker caído abandonó a mitad de camino, no solo trabajo que todavía nadie reclamó. `outbox_leased` por sí solo no se usa para esto: también cuenta leases que un worker actualmente en ejecución todavía sostiene legítimamente. Un backlog distinto de cero con un worker vivo drenándolo no está degradado; un valor desconocido (`null`) nunca degrada esta verificación por sí solo (solo duplicaría una falla de postgres/timeout que ya produjo ese `null`).
- **`GET /health` sigue respondiendo HTTP 200 mientras `status` es `"degraded"`** (preexistente, sin cambios por #141) — `status` en el cuerpo, no el código de estado HTTP, es lo que quien llama debe revisar. Una sonda de liveness de infraestructura que solo mira el código de estado HTTP no verá un backlog degradado; una que inspeccione el cuerpo sí.

Esto complementa la verificación de arranque de arriba en lugar de reemplazarla: la verificación de arranque atrapa un despliegue de WhatsApp que nunca pudo haber arrancado el worker; `/health` atrapa uno que sí pudo, y después se detuvo.

> Espejo en inglés: `docs/architecture/webhook-delivery.md` (mantener ambos al día juntos).

## W2b2 (continuación) — mecánica del ciclo de vida del worker

El resto de esta sección describe `DeferredWebhookWorker.start()`/`.stop()` en sí, independientemente de las adiciones de arranque/salud de #141 de arriba.

`webhook_worker.stop` se apila al final en el `AsyncExitStack` del lifespan, así que su teardown LIFO ejecuta el stop del worker **primero**, antes de que se destruya el engine, se cierre el cliente de WhatsApp o se cierre el pool de Redis — al worker nunca se le retira una dependencia debajo de una iteración en curso.

`DeferredWebhookWorker.start()`/`.stop()` replican la misma forma de tarea en segundo plano que ya usa `AuditSink`: `start()` lanza una `asyncio.Task` que ejecuta `process_available` a intervalo fijo (`webhook_worker_poll_interval_s`, 1s por defecto; tamaño de lote `webhook_worker_claim_limit`, 10 por defecto — ambos en `Settings`), y `stop()` **cancela de forma dura** esa tarea en lugar de drenarla. Esto es deliberado, no un atajo: un claim cancelado a mitad de camino (a mitad de turno, a mitad de envío) deja su lease exactamente como W2a la confirmó, así que un claim nuevo —este worker reiniciando, o cualquier otro— la recupera una vez que esa lease vence. `process_claimed_work` ya deja propagar `asyncio.CancelledError` en lugar de registrar una falla (es una `BaseException`, no la captura el `except Exception` amplio alrededor de cada paso de procesamiento), así que un loop cancelado de forma dura nunca escribe una fila de falla espuria; la recuperación es la primitiva de vencimiento de lease de W2a, reutilizada tal cual, no reimplementada acá.

## Decisión de dedup: PostgreSQL es la única autoridad activa

Se eliminaron `services/dedup.py` y su guarda Redis `SET NX EX` con TTL. La restricción de unicidad `webhook_inbox.meta_message_id` del inbox durable, aplicada transaccionalmente por `accept_inbound_message`, es la única autoridad activa para entregas duplicadas de Meta. No está acotada por un TTL que un reintento lento pueda superar. Si falla la persistencia, la ruta devuelve 503 para que Meta reintente; las identidades ya confirmadas convergen sobre la fila existente del inbox. Esta guarda durable de duplicados no modifica la semántica del proveedor saliente: un resultado del proveedor todavía puede ser ambiguo, por lo que el envío sigue siendo al-menos-una-vez y puede duplicar un mensaje.

## W3 control de admisión acotado (#46, ADR-001 D-033)

Nada acotaba cuántos turnos podían correr a la vez. `DeferredWebhookWorker.process_available` reclamaba hasta `webhook_worker_claim_limit` filas y las procesaba de a una, secuencialmente — un accidente de la forma del loop, no un límite diseñado, y no decía nada sobre `POST /v1/chat/completions` (`integration/openai_adapter.py`), que llama a `AgentRuntime.run_turn` directamente, una vez por request HTTP, sin ningún límite. Una ráfaga de conversaciones entrantes por cualquiera de las dos rutas podía abrir un turno por conversación y agotar el pool de base de datos sin importar la cantidad de procesos.

El lifespan de `main.py` ahora construye exactamente un `TurnAdmissionLimiter` (`services/admission.py`), dimensionado desde `Settings.max_concurrent_turns`, y lo comparte —vía `app.state.turn_admission_limiter`— entre ambos puntos de entrada: el worker del webhook acota en él el procesamiento de cada ítem reclamado, y el adapter envuelve su llamada a `run_turn` con él también. El límite es entonces de todo el proceso, no por punto de entrada.

**La decisión aprobada es backpressure, nunca rechazo.** En capacidad máxima el worker del webhook no reclama nada: `process_available` acota el reclamo a los slots libres del limitador (`min(limit, available)`) y retorna temprano cuando eso es cero, así que un mensaje ya persistido de forma durable simplemente espera, sin reclamar, en el outbox — no se toma ningún lease ni se gasta ningún intento en trabajo que todavía no puede empezar. Se reclama en un poll posterior una vez que se libera un slot. Por eso el limitador es una primitiva propia y no un `asyncio.Semaphore` directo: el worker necesita saber cuántos slots están libres *antes* de reclamar, algo que un semáforo no expone. Una vez reclamados, los ítems se procesan de forma concurrente (`asyncio.gather`), cada uno acotado por el mismo limitador, así que un worker que antes corría un turno a la vez ahora puede correr varios genuinamente.

El valor por defecto, `10`, coincide con el objetivo de lanzamiento de la Etapa A de ADR-001 ("10 conversaciones concurrentes: un solo proceso") y queda por debajo del techo por defecto del pool de un solo engine (`pool_size=5, max_overflow=10`). Corrección a partir de una revisión independiente: un turno **no** sostiene una conexión solo "por operación puntual" — `agent/graph.py::_execute_tools` abre una `AsyncSession` y la sostiene durante **todas** las llamadas a herramientas de una ronda (secuencial, nunca concurrente: una `AsyncSession` compartida no debe usarse desde más de una corrutina a la vez), así que una ronda con varias llamadas a herramientas lentas y secuenciales puede sostener esa única conexión durante la latencia acumulada completa de la ronda, no la de una sola consulta. La contabilidad propia de este worker (`_load_inbound`, `_resolve_participant`, `persist_outbound_intent`, `_complete_or_retry`, `_record_failure`) sí es sesión por llamada. De cualquier forma, un turno sostiene **como máximo una** conexión en un instante dado —nunca una por llamada a herramienta, porque el loop es secuencial— así que `max_concurrent_turns` turnos concurrentes implican como máximo esa cantidad de conexiones sostenidas desde esta ruta a la vez, todavía por debajo del techo del pool; eso no dice nada sobre cuánto tiempo se sostiene cada una. Dimensionar el pool en sí para la concurrencia de producción es responsabilidad de #45, no de este cambio.

## Relación con ADR-002 G.4/G.22

Este documento usa la abreviatura G.4/G.22 solicitada. En el ADR actual, G.22 trata el historial de chat durable y remite a A.4, “Memoria de trabajo durable”. W1 elige PostgreSQL como fuente de verdad para el traspaso entrante mediante inbox y outbox. No decide el backend de historial de chat durable.

## Límite de reversión

La migración `003` se niega a eliminar estado W2a no predeterminado. La migración `002` todavía se niega a eliminar trabajo incompleto. Una vez completado todo el trabajo y sin estado W2a, los downgrades quitan las tablas nuevas y el historial completado; la tabla `audit_event` existente permanece intacta.
