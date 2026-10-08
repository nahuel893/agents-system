# Entrega saliente proactiva

Un adaptador de canal normalmente solo responde a un turno entrante verificado. La entrega proactiva es la otra dirección: un mensaje a un usuario final autorizado sin turno entrante, como un reporte programado, un recordatorio, un mensaje de bienvenida o un aviso cuando termina un trabajo largo. Corre dentro de la identidad, los permisos, la auditoría y los límites de la plataforma, en lugar de un script ad hoc contra la API del proveedor. Diseño: [ADR-011](../architecture_es/adr-011-end-user-scope.md) parte 5.

**No es una herramienta para el modelo.** Los primeros llamadores son los turnos programados (#175) y los comandos de operador; el modelo nunca elige un destinatario ni una dirección.

## Las piezas

```python
from agents_system.proactive import (
    ProactiveDispatcher,
    ProactivePolicy,
    ProactiveSender,
    StaticRecipientDirectory,
)
from agents_system.services.proactive_postgres import PostgresProactiveQueue
from agents_system.services.scope_postgres import PostgresScopeProvider
from agents_system.subject import TurnActor, TurnSubject

queue = PostgresProactiveQueue(PostgresScopeProvider(engine))
directory = MyDirectory()  # tu RecipientDirectory, ver abajo

sender = ProactiveSender(
    queue, directory, deploy_grants={"support": {"send:proactive"}}
)
await sender.submit(
    "support",  # id de runtime
    TurnSubject.for_participant(42),
    "Tu reporte está listo.",
    actor=TurnActor(kind="scheduler", id="nightly-report"),
    permissions=current_permissions,  # los permisos del llamador, validados ahora
)

dispatcher = ProactiveDispatcher(
    queue,
    directory,
    {"whatsapp": whatsapp_channel},  # nombre de canal a ProactiveChannel
    deploy_grants={"support": {"send:proactive"}},  # el mismo techo
    policy=ProactivePolicy(per_recipient=(3, 86400), per_runtime=(500, 3600)),
)
while True:  # en cada proceso worker
    outcome = await dispatcher.run_once(worker_id)
```

- **El destinatario es un sujeto, nunca una dirección.** `RecipientDirectory.authorize(runtime_id, subject)` es un puerto que implementa el despliegue (una lista permitida, una base de datos, un CRM). Devuelve el sujeto con su `channel` y su `address`, o `None` para rechazarlo. `StaticRecipientDirectory` es la referencia en proceso. El directorio se consulta dos veces: en `submit` (un destinatario no autorizado lanza `RecipientNotAuthorizedError` y no se encola nada) y de nuevo al despachar, de modo que un destinatario revocado entretanto se rechaza antes de cualquier llamada al proveedor. La cola no guarda la dirección; se resuelve al enviar, así que un número reasignado nunca recibe el mensaje.
- **`send:proactive`.** Un permiso T2 nuevo, hermano de `send:message` y `send:escalation`: un permiso `send:message` existente nunca lo cubre. `submit` lo revalida en el momento de la llamada contra el techo del despliegue **del runtime id que recibe** y los permisos actuales del llamador, y lanza `ProactivePermissionError` si falta. `deploy_grants` es un mapa de runtime id a nombres de permiso (la forma de `DEPLOY_GRANTS`); un runtime sin entrada no tiene permiso. El despachador revalida al enviar: vuelve a comprobar el techo del runtime del mensaje y, si pasas `current_permissions=async (runtime_id, actor_label) -> nombres de permiso`, los permisos actuales del llamador. Un mensaje cuyo permiso se revocó o cuyo techo se redujo mientras esperaba (cola durable, horario silencioso, demora de un 429) termina `failed` con motivo `permission_revoked` y nunca llega al proveedor. Sin `current_permissions` la cola no guarda los permisos del llamador, así que solo se revalida el techo.
- **Canales.** Un canal que puede enviar sin que se le escriba implementa `ProactiveChannel.send_proactive(to, body)`. El de WhatsApp lo hace: un `429` pasa a ser `ProviderRateLimited(retry_after_s)` con la demora de `Retry-After` (segundos o una fecha HTTP, acotada a 24 horas; un valor ausente, inválido o pasado cae a 60 segundos), cualquier otro `4xx` pasa a ser `ProviderRejected`, y un `5xx` o un error de transporte se deja tal cual. Fuera de la ventana de atención de 24 horas de Meta solo se aceptan plantillas aprobadas; el adaptador envía texto plano, así que úsalo con usuarios que escribieron hace poco, o implementa el envío de plantillas en tu propio adaptador.
- **Durable, una vez.** `PostgresProactiveQueue` guarda los mensajes en `proactive_outbox` (migración `010`), una tabla por usuario con clave (`runtime_id`, `subject_key`, `id`) y seguridad a nivel de fila mediante el handle de alcance. Un worker reclama un mensaje con `FOR UPDATE SKIP LOCKED` y un lease, la misma disciplina que el outbox del webhook, así que un mensaje sale una vez entre workers y reinicios. El reclamo del despachador corre con el ajuste de worker de la tabla activado, porque atiende al sujeto que toque; encolar, exportar y borrar quedan acotados por sujeto. La tabla es un `SubjectDataStore` registrado (`proactive`): la exportación y el borrado del operador la cubren, y `SUBJECT_DATA_RETENTION='{"proactive": 2592000}'` purga las filas terminadas. Construir `PostgresProactiveQueue(provider)` liga ese almacén a la misma base, y la app servida lo registra y lo liga al arrancar cuando se configura `create_app(scope_provider_factory=...)`, así que exportar, borrar y la retención alcanzan los cuerpos encolados en ambos casos.
- **Modo librería.** `InMemoryProactiveQueue` es el mismo contrato en un solo proceso, sin persistencia.

## A lo sumo una vez

El camino de respuesta nunca reenvía un envío ambiguo, y este tampoco. Una vez reclamado el mensaje:

| Qué pasó | Resultado |
|---|---|
| El proveedor lo aceptó | `sent` |
| `429` con una demora | Vuelve a `queued`, para después de `Retry-After`. `run_once` devuelve `rate_limited` con `retry_after_s`; nada duerme ni itera, y el próximo reclamo espera la hora debida |
| Un rechazo definitivo (`4xx`) | `failed`, sin reintento |
| Un timeout, un `5xx` o cualquier error de resultado desconocido | `ambiguous`, sin reintento |
| El worker murió tras reclamar (vence el lease) | `ambiguous`, sin reintento; el próximo `run_once` de cualquier worker lo audita una vez (motivo `lease_expired`) |

Un mensaje `ambiguous` o `failed` queda en la tabla (con `last_error`) para que el operador lo vea; nada lo reenvía.

## Salvaguardas

`ProactivePolicy` posterga un mensaje en lugar de descartarlo:

- `per_recipient=(count, window_s)` y `per_runtime=(count, window_s)`: un mensaje que excede el límite vuelve a la cola y se reevalúa tras `recheck_s` (60 por defecto).
- `quiet_hours=(start, end)` con `tz`: dentro de la ventana (puede cruzar la medianoche) el mensaje espera a que termine.

## Auditoría

Cada paso registra un evento `proactive_delivery` (`queued`, `sent`, `failed`, `ambiguous`, `rate_limited`, `deferred`) con el **actor** que lo disparó (`scheduler:<id>`, `operator:<id>`, `library:<id>`) en `actor` y la clave del sujeto en `subject`. El payload lleva el id de runtime, el id del mensaje, el nombre del canal y un código de motivo; nunca la dirección ni el texto. El vencimiento de un lease lo audita el worker que lo ve primero, una vez por mensaje; si ese worker muere entre el cambio de estado y la escritura de auditoría, el evento se pierde (la auditoría es de mejor esfuerzo), pero la fila conserva `ambiguous` y `last_error`, que la exportación del operador muestra.
