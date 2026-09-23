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
  PYTHONPATH=/home/nh/wt-43-webhook-outbox/src \
  /home/nh/agents-system/.venv/bin/python -m pytest -q -m integration tests/test_outbox_migration_integration.py
```

El operador debe crear primero la base desechable, definir `ISOLATED_OUTBOX_DATABASE_URL` con su URL asyncpg y eliminar la base al terminar.

## Lo que W1 explícitamente no implementa

El webhook activo continúa siendo sincrónico y respaldado por Redis. W1 **no** implementa un worker activo, reconocimiento HTTP temprano, comportamiento HTTP 503, reintentos ni control de admisión. La consulta de selección de trabajo almacenada prepara W2; no toma, procesa ni envía trabajo.

## Relación con ADR-002 G.4/G.22

Este documento usa la abreviatura G.4/G.22 solicitada. En el ADR actual, G.22 trata el historial de chat durable y remite a A.4, “Memoria de trabajo durable”. W1 elige PostgreSQL como fuente de verdad para el traspaso entrante mediante inbox y outbox. No decide el backend de historial de chat durable.

## Límite de reversión

La migración `002` se niega a eliminar trabajo incompleto. Una vez completado todo el trabajo, su downgrade quita las dos tablas nuevas y el historial completado; la tabla `audit_event` existente permanece intacta.
