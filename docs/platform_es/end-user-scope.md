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
