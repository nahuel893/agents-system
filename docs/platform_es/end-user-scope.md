# Alcance del usuario final: sujeto y actor del turno

*Llegará en la próxima versión.* Decidido en [ADR-011](../architecture_es/adr-011-end-user-scope.md); esta página cubre la parte 1 (identidad vinculada por turno) y la parte 2 (auditoría y logs).

Un agente desplegado atiende a muchos usuarios finales con un solo runtime en caché. Cada turno lleva dos valores que vincula el punto de entrada y que el modelo nunca ve:

| Valor | Significado | Forma |
|---|---|---|
| `TurnSubject` | El usuario final para quien es el turno | `key` (`participant:<id>`), más `channel` y `address` solo para entrega |
| `TurnActor` | Quién inició el turno | `kind` (`channel`, `api`, `operator`, `scheduler`, `library`) e `id`; su etiqueta es `<kind>:<id>` |

```python
from agents_system import TurnActor, TurnSubject

subject = TurnSubject.for_participant(42, channel="whatsapp", address="+5491100000000")
await runtime.run_turn(
    messages,
    session_id="s1",
    subject=subject,
    actor=TurnActor(kind="library", id="job"),
)
```

Ambos son solo por palabra clave y valen `None` por defecto; un turno sin ellos se comporta como antes. La clave usa el id del participante y no la dirección, porque una dirección puede reasignarse a otra persona.

## Quién vincula qué

| Turno | Sujeto | Actor |
|---|---|---|
| Turno entrante de un canal | El participante que resuelve el worker | `channel:<canal>` |
| Turno de API | Solo desde un `SubjectResolver`; si no, ninguno | `api:<id de runtime>` |
| Llamada de biblioteca | Lo que pase quien llama | Lo que pase quien llama |

El adaptador compatible con OpenAI autentica una sola clave compartida, así que no conoce al usuario final. Un despliegue que sí lo conoce aporta un resolver:

```python
class MyResolver:
    async def resolve(self, request):  # -> Participant | None
        ...


app = create_app(registry_factory=..., subject_resolver=MyResolver())
```

`None` significa que la petición no tiene usuario final y el turno corre sin sujeto. Un participante inactivo se rechaza con 403 y no corre ningún turno. El campo `user` de la petición y el texto de los mensajes nunca son un sujeto.

## Qué alcanza

- **Auditoría:** cada evento del turno completa `actor` (`<kind>:<id>`) y la nueva columna nullable `subject` (migración `008`) con la clave del sujeto. La dirección nunca va en una columna. Ver [Audit](audit.md).
- **Logs del turno:** `runtime.turn_usage` y cada línea de log del turno llevan `subject` (la clave) y `actor`, nunca la dirección.
- **Ids de participante:** un `Participant.id` debe ser único en todos los canales que sirve el directorio. Ver [Channel](channel.md).

## Herramientas acotadas al sujeto y el handle de alcance

Una herramienta nunca tiene la clave del sujeto. Marcá una herramienta con `ToolSpec(..., subject_scoped=True)` y su conector **async** recibe un argumento `scope=`, un handle atado a (id de runtime, clave del sujeto) que construye el interceptor a partir del sujeto del turno. Los métodos del handle no reciben ninguno de los dos valores; el modelo nunca los ve ni los pasa.

```python
from agents_system.scope import InMemoryScopeProvider

async def remember(inputs, *, session=None, scope):
    scope.put("notes", inputs["id"], inputs["text"])      # backend de referencia
    return {"notes": scope.rows("notes")}                  # solo las filas de este sujeto

ToolSpec(name="remember", ..., connector=remember, subject_scoped=True)
runtime = AgentRuntime(equipped, scope_provider=InMemoryScopeProvider(), runtime_id="support")
```

- **Sin sujeto, no corre.** Una herramienta acotada al sujeto llamada en un turno sin sujeto, sin `scope_provider` o con un conector sync se rechaza (`subject_required`, `scope_unavailable`, `scope_requires_async_connector`) y se audita como llamada bloqueada. Nunca corre contra un valor por defecto.
- **Id de runtime explícito.** Un `AgentRuntime` con `scope_provider` (cualquier almacén por usuario) y sin `runtime_id` lanza `ValueError`: el valor por defecto es el nombre del modelo, que dos agentes comparten. Tratá el id de runtime como permanente una vez activas funciones por usuario; renombrarlo deja huérfanos los datos.
- **`memory_policy` lo controla (#26).** Una herramienta acotada al sujeto que lee o escribe memoria del agente declara `memory_access` (`read`, `write` o `readwrite`) y `memory_scope` en su `ToolSpec`. El interceptor los compara con la `memory_policy` del rol antes de que corra el conector y rechaza la llamada, auditada, con `memory_read_denied`, `memory_write_denied` o `memory_policy_invalid`. Ver [Definición de rol](role.md#aplicación).
- **Backend de referencia.** `InMemoryScopeProvider` guarda las filas en un dict con clave (id de runtime, clave del sujeto), así el modo biblioteca conserva el contrato sin Postgres.
- **Backend Postgres (extra `postgres`).** `PostgresScopeProvider(engine)` abre la transacción de cada handle con `app.runtime_id` y `app.subject_key` como ajustes locales a la transacción (`handle.transaction()` entrega la conexión). `create_subject_policy(conn, table)` protege una tabla por usuario (columnas `runtime_id`, `subject_key`) con una política de row-level security y `FORCE ROW LEVEL SECURITY`, instalada como política permisiva y también restrictiva (la restrictiva se combina con AND, así que una política permisiva previa de la tabla no puede ampliarla): una consulta que olvida su filtro igual devuelve solo las filas del sujeto actual, y una transacción sin alcance no devuelve ninguna.
- **El rol de la base importa.** Los superusuarios y los roles con `BYPASSRLS` ignoran row-level security incluso con FORCE. `PostgresScopeProvider.verify()` rechaza ese rol. La app servida lo llama al arrancar solo si pasás `create_app(scope_provider_factory=lambda engine: PostgresScopeProvider(engine))`; sin funciones por usuario, los despliegues existentes arrancan con cualquier rol.
- **Test estructural.** `await assert_subject_isolation(provider, write=..., read_all=...)` (de `agents_system.scope`) escribe como dos sujetos y ejecuta tu `read_all` deliberadamente sin filtrar como cada uno; falla si alguno ve filas del otro. Cada almacén por usuario trae uno. Probá el lado Postgres con un rol dedicado no superusuario, nunca con el superusuario de docker.

## Datos por usuario: registro, exportación, borrado y retención

Cada almacén que guarda datos por usuario final implementa `SubjectDataStore` y se registra, así una sola llamada del operador los alcanza a todos y ningún almacén se puede olvidar.

```python
from agents_system.subject_data import DEFAULT_REGISTRY, SubjectRef


class NotesStore:
    name = "notes"
    tables = frozenset({"notes"})  # cada tabla con una columna subject_key

    async def export(self, subject: SubjectRef) -> list[dict]: ...
    async def delete(self, subject: SubjectRef) -> int: ...
    async def purge(
        self, *, retention_s: int
    ) -> int: ...  # opcional: filas más viejas que la retención


DEFAULT_REGISTRY.register(NotesStore())  # o create_app(subject_data=registry)
```

- **Biblioteca.** `export_subject(registry, SubjectRef(runtime_id, subject_key), threads=..., thread_ids=[...])` y `delete_subject(..., actor=TurnActor(kind="operator", id="ops"))`. `delete_subject` intenta todos los almacenes y todos los hilos aunque uno falle y después lanza `SubjectDeletionError` con los almacenes fallidos, la cantidad de hilos fallidos y los conteos que sí se borraron.
- **Hilos de conversación.** Los hilos del checkpointer tienen como clave la conversación, no el sujeto, así que se nombran: el despliegue pasa `create_app(subject_threads=async_fn)` (referencia del sujeto a ids de hilo) y es la única autoridad sobre qué hilos pertenecen a un sujeto. El operador puede nombrar `thread_ids` en el pedido para acotar el conjunto, pero un id nombrado que el hook no devuelve para ese sujeto se rechaza con 403, y sin el hook las rutas fallan cerradas con 409 mientras exista un checkpointer. La exportación devuelve todos los checkpoints retenidos (todos los valores de canal, metadatos y escrituras pendientes), los mismos datos que borra el borrado. `CheckpointThreads(saver)` exporta y borra un hilo a pedido; el purgador sigue eliminando hilos inactivos por antigüedad.
- **Nada olvidado.** `await find_unregistered_subject_tables(engine, registry)` (Postgres, en `agents_system.services.scope_postgres`) devuelve las tablas con columna `subject_key` que ningún almacén registrado reclama. Verificá que esté vacío en el test de tu almacén.
- **Rutas del operador.** Con `OPERATOR_API_KEY` definida, la app servida monta `POST /operator/subjects/export` y `POST /operator/subjects/delete`. Sin la clave las rutas no existen (404). La clave debe ser distinta de `ADAPTER_API_KEY` y `METRICS_API_KEY`, y se compara en tiempo constante como la del adaptador.

  ```bash
  curl -X POST $HOST/operator/subjects/delete \
    -H "Authorization: Bearer $OPERATOR_API_KEY" -H "X-Operator-Id: ops-ana" \
    -H "Content-Type: application/json" \
    -d '{"runtime_id": "support", "subject_key": "participant:42"}'
  ```

  El sujeto viaja en el cuerpo, así que nunca queda en un log de acceso. Un fallo parcial responde 500 con los almacenes fallidos y lo que sí se borró.
- **Auditoría.** Un borrado registra `subject_data_deleted` con el operador como actor (`operator:<X-Operator-Id>`, por defecto `operator:operator`). El resultado incluye `audited`: es `false`, con una advertencia en el log, cuando no hay un sink de auditoría registrado o la escritura falló; el borrado nunca se deshace ni se oculta. El sink de la app servida es fire-and-forget, así que el evento es de mejor esfuerzo. Las filas de auditoría guardan solo la clave seudónima del sujeto y siguen su propia retención. El `ConversationRecorder` del consumidor es del despliegue, que borra de él.
- **Retención.** `SUBJECT_DATA_RETENTION='{"notes": 2592000, "usage": "never"}'` fija una retención por almacén, en segundos o `never`, con el mismo formato que `CHECKPOINTER_RETENTION_S`; un almacén que no figura conserva sus filas. Un único `SubjectDataPurger` corre cada `SUBJECT_DATA_PURGE_INTERVAL_S` segundos (3600 por defecto), registra un almacén que falla y sigue con el resto. Un nombre que ningún almacén registró hace fallar el arranque.
- **El id de runtime es permanente** una vez activas funciones por usuario: las filas se indexan con él, así que renombrarlo las deja huérfanas y las llamadas del operador ya no las encuentran.

- **Entrega proactiva.** La cola proactiva (`proactive_outbox`) es un almacén registrado con su propia retención; ver [Entrega proactiva](proactive.md).

## Uso y presupuestos por usuario final

*Llegará en la próxima versión.* Decidido en la parte 5 del [ADR-011](../architecture_es/adr-011-end-user-scope.md). El runtime registra el uso de cada turno contra el sujeto del turno y puede rechazar a un sujeto que alcanzó un tope.

- **El ledger.** Una fila por turno en `subject_usage` (migración `009`), con clave (`runtime_id`, `subject_key`, id): tokens de entrada, salida y total, `cost_usd`, id del modelo, llamadas al modelo, llamadas a herramientas, `timed_out`, la etiqueta del actor y la hora. Se escribe a través del handle de scope, así que Postgres lo cerca con seguridad a nivel de fila, y es un `SubjectDataStore` registrado (`usage`): la exportación y el borrado del operador lo alcanzan. Un proveedor de scope lo activa (`create_app(scope_provider_factory=...)`; `InMemoryUsageLedger(InMemoryScopeProvider())` para quien usa la librería, pasado como `AgentRuntime(usage_ledger=...)` con un `runtime_id` explícito).
- **Quién paga.** El uso cuenta contra el sujeto del turno, sea quien sea el actor: un turno programado contra su dueño, un turno de operador en nombre de un usuario contra ese usuario. Un turno sin sujeto no entra al ledger; cuenta solo contra el runtime, en las métricas.
- **Se registra donde se cotiza el turno**, incluida la cota inferior de un turno agotado por tiempo. Un modelo sin entrada en `MODEL_PRICES` se cuenta en tokens y se marca (`cost_usd` es nulo y los totales informan `unpriced_turns`); nunca se cotiza adivinando. Una escritura fallida del ledger se registra (`runtime.usage_record_failed`) y nunca hace fallar el turno.
- **Topes.** Los fija el despliegue por id de runtime, en USD o tokens, por día y mes UTC, en `create_app(budgets=...)` o `USER_BUDGETS` (con la forma de `DEPLOY_GRANTS`), o `AgentRuntime(budget=BudgetCaps(...), usage_ledger=...)`. Una definición de agente no puede subirlos. Un tope se alcanza cuando el total es igual o mayor.

  ```bash
  USER_BUDGETS='{"support": {"daily_usd": 1.0, "monthly_tokens": 500000, "message": "Llegaste al límite de hoy."}}'
  ```

- **Se revisa antes de cada llamada al modelo.** Un turno sobre el tope no llama al modelo: `run_turn_with_usage` devuelve un `TurnResult` con `budget_refused=True`, el `message` configurado como último mensaje y uso cero, y un evento de auditoría `budget_refused` (id de runtime, tope, límite, usado; el sujeto en la columna `subject`). Antes de cada llamada posterior del turno el tope se revisa de nuevo con el uso que el turno ya hizo, así que un bucle de herramientas se pasa como mucho una llamada (un rechazo ahí termina el turno con el mismo mensaje). Esa revisión también corre antes de la petición de reintento que sigue a una llamada a herramienta mal formada; el intento fallido pudo facturarse, así que los totales de tokens del turno se informan como desconocidos. Los demás sujetos no se ven afectados. Un turno rechazado antes de cualquier llamada al modelo no se guarda en el hilo de la conversación y no escribe fila en el ledger. Un turno rechazado a mitad de su bucle de herramientas ya hizo llamadas reales y ejecutó herramientas, así que se guarda en el hilo como un turno agotado por tiempo, escribe su fila en el ledger y cuenta como `outcome="budget_refused"`.
- **Chequeos al arrancar.** Un tope debe ser un número finito y positivo (NaN e infinito se rechazan, porque apagarían el tope) y cada entrada fija al menos un tope. Topes sin proveedor de scope, un presupuesto que nombra un id de runtime que no se construye y un tope en USD sobre un runtime cuyo modelo no tiene entrada en `MODEL_PRICES` hacen fallar el arranque. Un `model_id` por turno sin entrada en `MODEL_PRICES` se rechaza con un `ValueError` antes de cualquier llamada al modelo en un runtime con tope en USD.
- **Métricas.** `agent_turns_total`, `agent_tokens_total` y `agent_cost_usd_total` ganan una etiqueta acotada `actor_kind` (`channel`, `api`, `operator`, `scheduler`, `library` o `none`). No hay etiqueta de sujeto: las cifras por usuario salen del ledger. Los turnos rechazados aparecen como `outcome="budget_refused"`.
- **API de consulta.** `POST /operator/subjects/usage` con `{runtime_id, subject_key}` responde los totales del sujeto (tokens, `cost_usd`, `turns`, `unpriced_turns`) del día y el mes UTC. En la librería, `ledger.totals(...)` y `ledger.query(...)`.
- **Límites.** Las filas de uso no tienen purga: una purga entre sujetos no tiene scope donde correr bajo seguridad a nivel de fila, así que las filas quedan hasta que el operador borra al sujeto. Los tokens no informados cuentan como cero para un tope de tokens y el turno se marca en `unpriced_turns` solo cuando no tiene precio. Dos turnos concurrentes de un mismo sujeto pueden pasar ambos la revisión previa; los turnos por canal de una conversación ya corren de a uno.

## Memoria por usuario

*Llegará en la próxima versión.* Decidido en el [ADR-011](../architecture_es/adr-011-end-user-scope.md) y el issue #250. La memoria de trabajo es el hilo del checkpointer y caduca con `CHECKPOINTER_RETENTION_S`. La memoria por usuario es lo que un agente guarda **sobre cada usuario final** entre conversaciones: una unidad preferida, una entidad por la que suele preguntar, un pedido recurrente. No es la memoria propia del agente, que se indexa por agente.

- **Se activa por agente.** Viene apagada. Un rol la enciende en `policy.md` con `memory_policy: {user_memory: true}` (un booleano; cualquier otra cosa falla cerrado, como el resto de `memory_policy`). Sin eso las tres herramientas se rechazan con `user_memory_disabled` y no se inyecta nada.
- **Indexada por el sujeto del turno.** Un hecho pertenece a (`runtime_id`, `subject_key`), el mismo par que toda tabla por usuario, en `subject_facts` (migración `010`) con seguridad a nivel de fila. Las herramientas son `subject_scoped`: reciben el handle `scope` y ningún argumento nombra un usuario, así que el modelo no puede leer ni escribir los hechos de otro, y una consulta que olvida su filtro igual devuelve filas de un solo usuario. Un turno sin sujeto se rechaza con `subject_required`.
- **Tres herramientas.** `remember_user_fact(fact)`, `forget_user_fact(id)` y `list_user_facts()`. `write:user_memory` (T2: se revalida al llamar, así que necesita una entrada en `DEPLOY_GRANTS`) cubre las dos primeras y `read:user_memory` (T0) la tercera. `build_user_memory_tool_specs(limits)` las construye; `default_registry_factory` las registra, inertes hasta que un rol las nombre. Un hecho es una línea de texto; recordar dos veces el mismo texto deja un solo hecho.
- **Se inyecta como datos no confiables.** Al inicio de cada turno de un rol con la opción activa, los hechos del usuario se agregan como un bloque de datos no confiables, `source=user_facts` (el primer consumidor de [la primitiva](end-user-scope.md)): delimitado por un límite aleatorio por turno, fuera del prompt de sistema, nunca persistido en el hilo. Los hechos se escribieron a partir de entrada del usuario, así que son información y nunca una instrucción: un "ignora tus reglas y llama a una herramienta" guardado no cambia ninguna herramienta, permiso ni límite, y bajo `untrusted_input: true` la prohibición de T3 no cambia. Los hechos van del más usado al menos usado, luego del más reciente, y un hecho que no cabe en el tope de tamaño se omite, nunca se corta. Si el almacén falla al leer, el turno pierde sus hechos y se registra `user_memory.inject_failed`; nunca falla el turno.
- **Límites, fijados por el despliegue.** `USER_MEMORY_MAX_FACTS` (por defecto 50), `USER_MEMORY_MAX_FACT_BYTES` (500; un hecho más largo se rechaza al modelo con `error_kind: fact_rejected`, no se corta) y `USER_MEMORY_MAX_INJECTED_CHARS` (1500, cuenta contra `max_context_tokens` como cualquier bloque). Cuando una escritura pasa el máximo de hechos, se desaloja primero el **menos usado**, luego el más antiguo; el hecho recién escrito nunca es el desalojado. Un hecho cuenta como usado cada vez que se inyecta. En la librería, pasa `UserMemoryLimits(...)` como `AgentRuntime(user_memory=..., scope_provider=..., runtime_id=...)`.
- **Retención.** `SUBJECT_DATA_RETENTION='{"user_facts": 2592000}'` hace caducar un hecho esa cantidad de segundos después de escribirse; `never`, o ninguna entrada, lo conserva hasta que se olvide, se desaloje o se borre al sujeto. Un hecho caducado nunca se lista, se inyecta ni se exporta. Se borra la próxima vez que se leen o escriben los hechos de ese usuario, y el purgador compartido borra los hechos caducados de todos los usuarios, también de quienes nunca vuelven. En Postgres la purga corre por una vía de mantenimiento acotada (migración `011`): solo dentro de una transacción que fija `app.maintenance`, las filas pasadas de `expires_at` se vuelven visibles y borrables, y nada más se abre.
- **Escritores concurrentes.** Recordar y desalojar corren como una sola operación serializada por usuario (un advisory lock por usuario en Postgres, un lock por usuario en el backend en proceso), así que dos escrituras simultáneas respetan el máximo configurado en vez de borrarse entre sí.
- **Los permisos gobiernan la inyección.** Inyectar los hechos es leerlos, así que un turno recibe el bloque solo cuando los permisos del despliegue cubren `read:user_memory` (acotados por los permisos del turno); sin eso no se inyecta nada ni se cuenta ningún uso.
- **Exportar y borrar.** El almacén se registra como `user_facts` en el registro de datos, así que `export_subject` / `delete_subject` del operador (y sus rutas) cubren los hechos de un usuario junto con el resto de sus datos.
- **Modo librería.** `InMemoryScopeProvider` respalda el mismo handle con un dict (`InMemoryUserFactStore(provider)` es su almacén del registro), así que quien no usa Postgres obtiene el mismo contrato.
- **Evals.** Un escenario nombra un `end_user`, puede guardar `user_facts` antes de su primer turno y empezar una conversación nueva con `new_conversation_before: [1]`; ver [Evaluación en vivo](live-eval.md). La capacidad `user_memory` incluye un escenario de recuerdo y uno guardrail con una instrucción plantada.

```yaml
# policy.md
memory_policy:
  user_memory: true
```
