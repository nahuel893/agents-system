# Turnos programados

*Llega en el próximo release.* Un usuario final le pide al agente por un canal: "mandame las ventas semanales todos los lunes a las 8". El agente crea una programación para ese usuario; más tarde la plataforma la ejecuta como un turno **para ese usuario** y le entrega la respuesta. Diseño: [ADR-011](../architecture_es/adr-011-end-user-scope.md) (sujeto y actor del turno, handle de alcance, entrega proactiva); issue #175.

El modelo nunca elige para quién es la programación ni quién la recibe. Ambos los pone la plataforma: el dueño es el sujeto del turno y la entrega va solo a ese dueño por la [entrega proactiva](proactive.md).

## Las herramientas

Tres herramientas con alcance de sujeto (ver [Alcance del usuario final](end-user-scope.md#herramientas-acotadas-al-sujeto-y-el-handle-de-alcance)), así que cada una recibe un handle ya atado al usuario final actual y no tiene ningún id de usuario:

| Herramienta | Hace |
|---|---|
| `create_schedule(prompt, frequency, time?, weekday?, every_hours?, timezone?)` | Guarda un pedido recurrente para este usuario |
| `list_schedules()` | Lista las programaciones de este usuario, con ids y próxima ejecución |
| `delete_schedule(id)` | Borra una programación de este usuario |

`prompt` es el pedido tal como debe hacerse más tarde, con las palabras del usuario; la conversación ya no existirá. `frequency` es `daily`, `weekly` (con un `weekday`, de `monday` a `sunday`) o `interval` (`every_hours`); `time` es una hora de reloj de 24 horas `HH:MM` en `timezone` (un nombre IANA, por defecto `SCHEDULE_TIMEZONE`). No hay argumento de destinatario, dirección ni usuario.

Las tres necesitan el nuevo permiso **`write:schedule`**. Es T2: se revalida en cada llamada, así que el despliegue debe listarlo en `DEPLOY_GRANTS`, y sin él las herramientas ni siquiera se equipan. Se registran con `build_schedule_tool_specs(limits)`; el registro por defecto de la app servida ya lo hace, así que a un rol le basta nombrarlas en su `tools:`. Una llamada rechazada devuelve un `error_kind` que el modelo puede informar: `invalid_prompt`, `invalid_schedule`, `interval_too_short`, `quiet_hours`, `limit_reached`, `scheduled_turn`, y `not_found` al borrar un id que no es del usuario.

## Límites

Los fija el despliegue, nunca la definición del agente:

| Ajuste | Por defecto | Significado |
|---|---|---|
| `SCHEDULE_MAX_PER_USER` | `5` | Programaciones que puede tener un usuario; se aplica dentro de la sección crítica del almacén, así que creaciones concurrentes no pueden pasarse |
| `SCHEDULE_MIN_INTERVAL_S` | `3600` | Intervalo mínimo entre dos ejecuciones; una programación diaria o semanal siempre lo cumple |
| `SCHEDULE_QUIET_START`, `SCHEDULE_QUIET_END` | sin definir | Límites `HH:MM`, definidos juntos, en `SCHEDULE_TIMEZONE`; ninguna ejecución empieza dentro de ellos |
| `SCHEDULE_TIMEZONE` | `UTC` | Zona de las horas de silencio y zona por defecto de una programación |
| `SCHEDULE_MAX_PROMPT_CHARS` | `500` | Pedido más largo permitido |
| `SCHEDULE_MISSED_GRACE_S` | `1800` | Una ejecución reclamada más tarde que esto después de su ventana se omite, no se ejecuta tarde |

Una programación cuya hora cae dentro de las horas de silencio se rechaza al crearla. Un turno programado no puede crear programaciones.

## Ejecutarlas

```python
from agents_system.schedules import ScheduledTurnRunner
from agents_system.services.schedules_postgres import PostgresScheduleStore

store = PostgresScheduleStore(scope_provider)  # o InMemoryScheduleStore(provider)
runner = ScheduledTurnRunner(
    store,
    {"sales": agent_runtime},  # id de runtime -> AgentRuntime
    sender,  # un ProactiveSender (ver Entrega proactiva)
    directory,  # el RecipientDirectory que usa el sender
    deploy_grants={"sales": {"write:schedule", "send:proactive"}},
    limits=limits,
)
while True:  # en cada proceso worker
    outcome = await runner.run_once()
    if outcome.status == "idle":
        await asyncio.sleep(30)
```

Igual que el despachador proactivo, el runner es un bucle que el despliegue corre en sus procesos worker; la app servida no arranca uno. `run_once` reclama una programación vencida y la ejecuta:

- **El dueño como sujeto, el planificador como actor.** El turno corre con el dueño de la programación como sujeto del turno (mismo rol, mismo alcance de datos por usuario, memoria y presupuesto) y `scheduler:<id de la programación>` como actor, bajo los grants de despliegue del propio runtime. La auditoría dice "el planificador, en nombre de este usuario".
- **Solo texto, solo al dueño.** La respuesta se encola por `ProactiveSender` para el dueño. El directorio de destinatarios autoriza al dueño antes de que empiece el turno (un dueño no autorizado no cuesta ninguna llamada al modelo; `failed:recipient_not_authorized`, o `failed:recipient_lookup_failed` si falla el propio directorio) y otra vez al enviar, y `send:proactive` debe estar concedido. Una falla de la cola al enviar registra `failed:delivery_failed`.
- **Una vez por ventana.** El reclamo guarda la próxima ventana en la misma transacción (`FOR UPDATE SKIP LOCKED` en Postgres), así que una ventana corre como máximo una vez entre workers y reinicios. Una ejecución que muere después del reclamo no se repite, y una ventana reclamada que falla (una caída del directorio de destinatarios o de la cola, un turno fallido) se registra como `failed` y no se reintenta: el bucle del worker sobrevive y la programación pasa a su próxima ventana.
- **Las ventanas perdidas se omiten.** Una ventana reclamada después de `SCHEDULE_MISSED_GRACE_S`, o dentro de las horas de silencio, no corre; la programación pasa a su próxima ventana y registra `skipped:missed_window` o `skipped:quiet_hours`. Una programación por intervalo conserva su cadencia: la próxima ventana es la primera de su grilla original posterior al reclamo, así que un reclamo tardío nunca desplaza un reporte cada hora.
- **Presupuesto.** El uso cuenta contra el [presupuesto](end-user-scope.md#uso-y-presupuestos-por-usuario-final) del dueño. Un turno que el presupuesto rechaza no entrega nada (`skipped:budget_refused`).
- **Sigue permitido.** El runner verifica que el despliegue siga concediendo `write:schedule` al runtime antes de cada ejecución (`skipped:not_permitted`).

`run_once` devuelve un `RunOutcome` (`idle`, `delivered`, `skipped` o `failed`, con un motivo); el último también queda guardado en la programación como `last_status`. "Entregado" significa encolado: el [despachador](proactive.md) lo envía con sus propias garantías (límites de tasa, horas de silencio, como máximo una vez).

## Almacenamiento, exportación y borrado

`subject_schedules` (migración `013`) tiene clave (`runtime_id`, `subject_key`, `id`) y está cercada por seguridad a nivel de fila, como toda tabla por usuario. Crear, listar y borrar pasan por el handle de alcance; el reclamo del runner fija un ajuste de worker local a la transacción, lo único además del par de sujeto que la política deja pasar. La fila guarda el pedido con las palabras del usuario y ninguna dirección.

El almacén es un `SubjectDataStore` registrado con el nombre `schedules`: la exportación del operador lista las programaciones de un usuario y el borrado del operador las elimina, así que **una programación termina cuando se la borra o cuando se borra a su dueño.** No tiene ajuste de retención. Sin Postgres, `InMemoryScheduleStore` sobre `InMemoryScopeProvider` es el backend de referencia.

## Evals

La capacidad `schedules` trae un agente de prueba (`schedules-agent`) y tres escenarios: `schedule_create` (un pedido semanal se vuelve la programación correcta), `schedule_cancel` (se busca y se borra una programación) y `schedule_limit` (una programación rechazada por demasiado frecuente se informa y se crea la permitida). Ver [Evals en vivo](live-eval.md).
