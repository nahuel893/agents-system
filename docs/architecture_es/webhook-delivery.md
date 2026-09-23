# Entrega de webhooks: límite de persistencia durable de W1

W1 agrega una primitiva de persistencia en PostgreSQL para mensajes entrantes de Meta y sus futuros registros de trabajo. No modifica la ruta de webhook activa: sigue siendo sincrónica y respaldada por Redis hasta que W2 conecte una ruta y un worker durables.

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

`claim_available_outbox_work` selecciona filas listas con `FOR UPDATE SKIP LOCKED`, escribe un lease e incrementa el contador de intentos, y hace commit antes de devolver cualquier fila. Por eso un worker concurrente saltea un lease activo; un lease vencido queda habilitado para recuperación. W2a no procesa filas, no llama a Meta ni ejecuta un loop de workers.

Ante una falla no terminal, se limpia el lease y se confirma un backoff exponencial acotado con full jitter. Se conservan la respuesta saliente y la clave interna: un resultado ambiguo del proveedor debe reintentarse, no deduplicarse con esa clave. Esto es entrega al-menos-una-vez, no exactamente-una-vez. La clave no es un campo ni header de idempotencia de Meta Graph API.

Al llegar a `MAX_OUTBOX_ATTEMPTS`, W2a limpia el lease, fija `failed_at`, conserva el error e inserta un `audit_event` en el **mismo commit de la sesión de base de datos**. `audit_event` tiene una partición DEFAULT, por lo que esta inserción directa sigue siendo segura más allá de las particiones mensuales iniciales. El evento durable incluye `operator_action: required`; su resultado `alert_required` es una señal accionable, no una afirmación de que una persona haya sido notificada. W2b debe conectar esa señal con el puerto de escalamiento humano propio del deployment o con monitoreo.

La migración `003` es aditiva: agrega columnas e índices de recuperabilidad y conserva los índices de W1. Su downgrade se niega si existe cualquier estado W2a, en vez de descartar datos de recuperación o una respuesta persistida. Como en W1, su prueba de integración solo debe ejecutarse con `OUTBOX_TEST_DATABASE_URL` apuntando a una base PostgreSQL aislada y descartable.

## Diferido explícitamente a W2b

El webhook activo continúa siendo sincrónico y respaldado por Redis. W2a **no** implementa un worker activo, reconocimiento HTTP temprano, comportamiento HTTP 503, envíos al proveedor ni control de admisión. Solo persiste estado de claim y reintento; W2b debe procesar el claim, persistir una respuesta saliente generada antes de enviar, reintentar resultados ambiguos del proveedor al-menos-una-vez y conectar señales terminales `alert_required` con el escalamiento propio del deployment.

## Relación con ADR-002 G.4/G.22

Este documento usa la abreviatura G.4/G.22 solicitada. En el ADR actual, G.22 trata el historial de chat durable y remite a A.4, “Memoria de trabajo durable”. W1 elige PostgreSQL como fuente de verdad para el traspaso entrante mediante inbox y outbox. No decide el backend de historial de chat durable.

## Límite de reversión

La migración `003` se niega a eliminar estado W2a no predeterminado. La migración `002` todavía se niega a eliminar trabajo incompleto. Una vez completado todo el trabajo y sin estado W2a, los downgrades quitan las tablas nuevas y el historial completado; la tabla `audit_event` existente permanece intacta.
