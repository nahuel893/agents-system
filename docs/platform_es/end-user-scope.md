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

Las herramientas todavía no reciben el sujeto: el handle acotado y los almacenes por usuario llegan con la parte 3 del ADR-011.
