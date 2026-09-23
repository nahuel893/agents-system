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

Las fallas de consulta a la base, runtime, turno, persistencia de intent, finalización y envío al proveedor usan la transición cercada `record_outbox_failure` de W2a. Perder el lease en cualquier transición cercada no modifica el claim obsoleto. El lease predeterminado está acotado a diez minutos, por encima del timeout de turno configurado de menos de 300 segundos más el margen de envío; reduce recuperaciones prematuras, pero no crea ejecución exactamente una vez.

Antes de llamar al proveedor, una respuesta generada se confirma como `outbound_body` junto con una clave interna estable derivada del ID de mensaje de Meta. Un claim posterior que tenga esa respuesta persistida envía el mismo cuerpo sin volver a ejecutar el agente. La clave no se coloca intencionalmente en la solicitud de Meta Graph API: la entrega saliente es al-menos-una-vez y un resultado ambiguo del proveedor puede duplicar un mensaje.

El registro de conversación sigue siendo best effort en una sesión independiente. Un deployment puede inyectar un callback asíncrono para fallas terminales. W2b1 lo invoca solo después de que W2a confirma la fila terminal y la señal de auditoría/acción de operador en la misma transacción, incluso si la terminalización se descubrió durante la recuperación del claim; si falta o falla el callback, no se afirma notificación humana y la señal durable se conserva. El esquema actual no guarda de forma durable los intentos de notificación, por lo que no puede garantizar el reintento del callback humano.

## W2b2 la ruta activa: persistir antes de reconocer, nunca procesar en el request

`POST /webhook` (`integration/webhook.py`) ahora hace exactamente tres cosas, en este orden: verificar la firma HMAC sobre el cuerpo crudo, recorrer el payload parseado en busca de cada mensaje de Meta válido, y persistir cada uno de forma durable mediante `accept_inbound_message` antes de devolver `{"status": "ok"}`. Nunca ejecuta un turno, nunca llama al directorio de participantes ni al grabador de conversación, y nunca toca el cliente de WhatsApp: todo eso se movió a `DeferredWebhookWorker`, completamente fuera del request.

Un "mensaje de Meta válido" es cualquier entrada alcanzable en `entry[].changes[].value.messages[]` que tenga un `id` no vacío. Un evento de actualización de estado, un array `messages` vacío o ausente, o un mensaje sin id no se persisten, igual que el manejo siempre-200 que la ruta anterior a W2b2 ya les daba. El payload parseado **completo** se guarda junto a cada id de mensaje encontrado en él (no solo los campos de ese mensaje), porque un batch puede traer varios mensajes bajo una misma firma de Meta y el worker luego tiene que encontrar el mensaje exacto al que se refiere un id dado dentro de ese sobre compartido (`webhook_worker._extract_inbound_turn`, de W2b1).

**HMAC primero, sin condiciones.** `verify_signature` corre antes de `json.loads` y antes de cualquier llamada de persistencia. Una firma forjada o ausente se rechaza con 403 y nunca llega a la base de datos, sin importar la forma del payload.

**Persistir cada mensaje válido; 503 ante cualquier falla.** Los mensajes de un batch se persisten en orden de entrega, con una llamada a `accept_inbound_message` por cada uno. El primero cuyo commit falle aborta el request con `HTTPException(503)` — Meta reintenta todo el sobre ante un 5xx, y este es el único caso en esta ruta donde eso es exactamente el resultado deseado (una falla transitoria de base de datos), no el loop de mensajes envenenados que el contrato siempre-200 de la ruta anterior (AD-2) existía para evitar ante *contenido* controlado por un atacante. Los mensajes ya confirmados **antes** de esa falla quedan confirmados: no se revierten ni se reintentan en el mismo request. En el reintento de Meta sobre el mismo sobre, `accept_inbound_message` reconoce cada id ya confirmado (`webhook_inbox.meta_message_id` es único) y devuelve `duplicate=True` en lugar de fallar, así que el reintento retoma naturalmente en el mensaje que realmente falló, en vez de repetir trabajo o fallar sobre los que ya habían tenido éxito.

**Un body malformado o controlado por un atacante nunca persiste nada y nunca da 500.** Las fallas de `json.loads` y cada discrepancia de forma del payload que la ruta anterior ya defendía (`entry` que no es una lista, `changes` que no es una lista, `value` que no es un diccionario, etc.) cortan directo a `{"status": "ok"}` antes de cualquier llamada a la base de datos, igual que antes.

## Conexión del worker en el lifespan de W2b2: arrancar después de las dependencias, detener antes del teardown

El `lifespan` de `main.py` construye un `DeferredWebhookWorker` por proceso y arranca su loop de sondeo solo después de que cada dependencia que necesita ya existe en `app.state`: el engine, el cliente de WhatsApp, el caché de runtimes resuelto, y el directorio de participantes / grabador de conversación que `create_app` guarda ahí. Se omite —con una advertencia `webhook_worker.no_runtime_resolved`, no con una falla de arranque— cuando `whatsapp_runtime_id` no está configurado o no resuelve a nada en el caché de runtimes; el trabajo entrante durable igual se acumula y simplemente espera a que un operador corrija la configuración, nunca se descarta.

`webhook_worker.stop` se apila al final en el `AsyncExitStack` del lifespan, así que su teardown LIFO ejecuta el stop del worker **primero**, antes de que se destruya el engine, se cierre el cliente de WhatsApp o se cierre el pool de Redis — al worker nunca se le retira una dependencia debajo de una iteración en curso.

`DeferredWebhookWorker.start()`/`.stop()` replican la misma forma de tarea en segundo plano que ya usa `AuditSink`: `start()` lanza una `asyncio.Task` que ejecuta `process_available` a intervalo fijo (`webhook_worker_poll_interval_s`, 1s por defecto; tamaño de lote `webhook_worker_claim_limit`, 10 por defecto — ambos en `Settings`), y `stop()` **cancela de forma dura** esa tarea en lugar de drenarla. Esto es deliberado, no un atajo: un claim cancelado a mitad de camino (a mitad de turno, a mitad de envío) deja su lease exactamente como W2a la confirmó, así que un claim nuevo —este worker reiniciando, o cualquier otro— la recupera una vez que esa lease vence. `process_claimed_work` ya deja propagar `asyncio.CancelledError` en lugar de registrar una falla (es una `BaseException`, no la captura el `except Exception` amplio alrededor de cada paso de procesamiento), así que un loop cancelado de forma dura nunca escribe una fila de falla espuria; la recuperación es la primitiva de vencimiento de lease de W2a, reutilizada tal cual, no reimplementada acá.

## Decisión de dedup: el chequeo de Redis queda superado, no duplicado

Antes de W2b2, `POST /webhook` llamaba a `services.dedup.is_duplicate` (Redis `SET NX EX`, TTL de 5 minutos, **fail-open** ante un error de Redis) para saltear un `message_id` ya procesado. W2b2 quita esa llamada de la ruta. La restricción de unicidad `webhook_inbox.meta_message_id` del inbox durable —aplicada transaccionalmente por `accept_inbound_message`— es ahora la única guarda de entrega duplicada en la ruta activa, y domina estrictamente al chequeo de Redis: no está acotada por un TTL que un reintento lento pueda superar, y una falla de persistencia devuelve 503 (Meta reintenta) en vez de reprocesar en silencio como hacía un Redis caído. `services/dedup.py` y su constante `DEDUP_TTL_SECONDS` quedan en el árbol (ver el docstring de ese módulo) solo porque `main.py` todavía afirma una invariante estática no relacionada contra `DEDUP_TTL_SECONDS`, que ata el timeout de ejecución de turno por defecto de la plataforma a ese valor; esa invariante protegía a la ruta sincrónica anterior (un turno corriendo dentro del request por más tiempo que el TTL de dedup), un escenario que no puede volver a ocurrir una vez que ningún turno corre dentro del request. Desenredar esa invariante queda fuera del alcance de esta porción y se deja como seguimiento.

## Relación con ADR-002 G.4/G.22

Este documento usa la abreviatura G.4/G.22 solicitada. En el ADR actual, G.22 trata el historial de chat durable y remite a A.4, “Memoria de trabajo durable”. W1 elige PostgreSQL como fuente de verdad para el traspaso entrante mediante inbox y outbox. No decide el backend de historial de chat durable.

## Límite de reversión

La migración `003` se niega a eliminar estado W2a no predeterminado. La migración `002` todavía se niega a eliminar trabajo incompleto. Una vez completado todo el trabajo y sin estado W2a, los downgrades quitan las tablas nuevas y el historial completado; la tabla `audit_event` existente permanece intacta.
