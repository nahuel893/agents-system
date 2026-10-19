# Línea de comandos

> **Llega en el próximo release.** Está en `main`, no en `v0.7.2`.

Instalar el paquete instala un comando, `agents`, con un alias, `agents-system`. Los dos ejecutan el mismo programa, así que el alias sigue funcionando cuando otro paquete del mismo entorno también instala un comando `agents`. `python -m agents_system` también lo ejecuta. Versión en inglés: [`docs/platform/cli.md`](../platform/cli.md).

```bash
agents check ./my-agent --grant read:catalog
agents chat ./my-agent --provider anthropic --model claude-haiku-5-5
agents serve --agents-root ./agents --port 8000
agents db upgrade
```

El comando no agrega ninguna dependencia al núcleo. `check` y `chat` funcionan con una instalación simple. `serve` necesita el extra `server` y `db` el extra `postgres`; sin él, el comando falla con el error que nombra el extra a instalar (ver [Extras](library-usage.md#extras)).

La salida propia del comando va a stdout. Los logs del runtime van a stderr, y solo se muestran los errores; `agents -v <comando>` los muestra todos. Un comando que falla imprime una línea, `agents: error: ...`, y termina con estado 1. Un error de uso termina con estado 2.

## Cómo nombrar un agente

`check` y `chat` reciben el agente como primer argumento:

- **Una carpeta**: `./my-agent`, la ruta a una carpeta con `role.md`, `manifest.md` y `policy.md`. Se carga con `Agent.from_folder`, igual que desde Python. Un agente de referencia copiado de `examples/agents/` es una carpeta como cualquier otra.
- **Un nombre de rol**: `agent`, o un rol predefinido. Nombrar uno de los ocho roles predefinidos concretos está deprecado (ADR-010); `agents check` imprime el aviso.

## Herramientas y permisos otorgados

`check` y `chat` construyen el runtime del agente con `build_runtime`, igual que quien usa la biblioteca. Dos flags reemplazan lo que pasa tu código:

- `--grant PERM`, repetible: un permiso que otorga quien llama, como en `granted_permissions=[...]`. Sin él, no se otorga nada.
- `--registry MODULO:ATRIBUTO`: tu `ToolRegistry`. `ATRIBUTO` es el registro mismo, o algo invocable que lo devuelve: una función sin argumentos, o la fábrica de registro de la app servida, que se llama con `settings`, `embedder` y `bi_engine` en `None`. El módulo se importa desde el directorio donde corrés el comando. Sin el flag, el comando usa las herramientas de referencia de la plataforma (`knowledge_retrieval`, `conversation_summarizer`, `escalation_notifier`, `order_writer`, las de memoria por usuario y de tareas programadas, y la de reportes cuando el extra `postgres` está instalado). No están conectadas: dicen qué herramientas existen y, si se las llama, fallan cerradas.

Una herramienta llega al modelo solo cuando el agente declara su permiso y quien llama lo otorga. Los `permissions` del agente son un techo, y un permiso otorgado fuera de ese techo no equipa nada.

## `agents check <agente>`

Resuelve el agente e imprime a qué resuelve, sin ejecutarlo:

- la cadena que extiende, de la hoja a la raíz: `shop -> agent -> base`;
- sus permisos declarados, el registro usado y los permisos otorgados;
- su autonomía, si lee entrada no confiable y su modelo declarado;
- cada herramienta declarada, **equipped** (equipada) o **denied** (denegada) con el motivo;
- sus límites de ejecución efectivos (los suyos sobre los de la plataforma) y su política de memoria;
- cualquier aviso que haya dado resolverlo, como un rol predefinido deprecado.

```text
$ agents check ./shop --registry my_app.tools:registry --grant read:catalog
Agent: shop (version 1.0)
  extends:          shop -> agent -> base
  permissions:      send:escalation, write:orders
  registry:         my_app.tools:registry
  granted:          read:catalog
  ...

Tools:
  escalation_notifier  denied: send:escalation is not granted by the caller
  catalog_search       denied: read:catalog is not in the agent's permissions
  order_writer         denied: write:orders is not granted by the caller
  inventory_sync       denied: not in the registry
```

Los motivos de una herramienta denegada:

| Motivo | Qué significa | Qué cambiar |
|---|---|---|
| `PERM is not in the agent's permissions` | La herramienta necesita `PERM` y el agente no lo declara. Otorgarlo no alcanza. | Declararlo: `permissions:` en `manifest.md`, o `Agent(permissions=[...])`. |
| `PERM is not granted by the caller` | El agente declara `PERM` y quien llama no lo otorga. | Otorgarlo: `--grant PERM` acá, `granted_permissions=[...]` en código, `DEPLOY_GRANTS` en la app servida. |
| `not in the registry` | No hay ninguna herramienta registrada con ese nombre, y ningún default integrado la cubre. Una construcción desde la biblioteca lanza `InjectionError`, y la app servida no arranca. | Registrar un `ToolSpec` con ese nombre, o pasar `--registry`. |
| `tier T3 tools are never granted to an untrusted_input role` | El agente lee entrada no confiable y la herramienta ejecuta comandos en el host. | Ningún permiso lo cambia. Ver [Política](policy.md). |

Una herramienta equipada que es el default integrado de la biblioteca, como el `escalation_notifier` del rol base cuando tu registro no tiene uno, se muestra como `equipped (built-in default)`. No llega a ningún humano (ver [Uso como biblioteca](library-usage.md)).

`check` termina con estado 1 cuando el agente no resuelve (una carpeta que no existe, un rol desconocido, una definición inválida), cuando no se puede cargar `--registry`, o cuando `--grant` nombra un permiso que nadie registra. Una herramienta denegada no es un fallo: para eso está `check`.

## `agents chat <agente>`

Un bucle de turnos interactivo en la terminal. Cada turno envía toda la conversación hasta ese momento, con las llamadas a herramientas y sus resultados, como lo hace quien llama a la app servida. El comando muestra cada llamada a herramienta y su resultado a medida que ocurren, y después el texto de la respuesta (nunca sus bloques de contenido crudos). Para salir, escribí `exit` o `quit`, o apretá Ctrl-D.

```text
$ agents chat ./counter --registry my_app.tools:registry --provider anthropic
Chatting with counter on claude-haiku-5-5. Tools: word_count. Type exit or press Ctrl-D to quit.
  (denied: escalation_notifier: missing permissions: send:escalation; see `agents check`)
you> How many words are in 'the quick brown fox'?
  -> word_count({"text": "the quick brown fox"})
  <- word_count: {"words": 4}
agent> There are 4 words.
you> exit
```

Recibe `--grant` y `--registry` igual que `check`, y elige su modelo en este orden:

1. `--provider` (`anthropic`, `openai_compatible`, `groq` u `ollama`), con `--model` para un modelo distinto del predeterminado del proveedor.
2. El modelo que declara el agente (`model:` en `policy.md`, o `Agent(model=...)`). `--model` reemplaza el id del modelo.
3. `ADAPTER_PROVIDER`, del entorno o del `.env` del directorio actual, con `--model` como arriba.

Sin ninguno de ellos, el comando falla y lo dice. Las credenciales nunca son flags: vienen del entorno y del `.env`, igual que para `build_agent_model` (`ANTHROPIC_API_KEY`, `GROQ_API_KEY`, `OPENAI_COMPATIBLE_*`, `OLLAMA_*`). El extra del proveedor tiene que estar instalado. El encabezado nombra el modelo con el que corre la sesión.

La sesión no guarda historial cuando termina, y no registra eventos de auditoría salvo que tu módulo de registro registre un `AuditSink`.

## `agents serve`

Ejecuta la app servida (`agents_system.server`) con uvicorn. Necesita el extra `server`, más el extra del proveedor con el que está configurada la app.

| Flag | Predeterminado | Qué hace |
|---|---|---|
| `--agents-root DIR` | sin definir | La carpeta de la que se leen los registros `folder:<nombre>`. Define `AGENTS_ROOT` con la ruta absoluta de la carpeta. |
| `--registry MODULO:ATRIBUTO` | sin definir | La fábrica de registro con la que la app construye sus herramientas. Define `REGISTRY_FACTORY`. |
| `--host` | `127.0.0.1` | La dirección donde escuchar. En un contenedor, usá `0.0.0.0`. |
| `--port` | `8000` | El puerto donde escuchar. |
| `--workers` | el de uvicorn (uno) | Procesos worker. |
| `--reload` | apagado | Reinicia cuando cambia el código. Para desarrollo. |

Todo lo demás que necesita la app (su base de datos, `AGENT_REGISTRATIONS`, `DEPLOY_GRANTS`, el proveedor) viene del entorno y del `.env`, igual que con `uvicorn agents_system.server:app`. Ver [Deployment](deployment.md).

## `agents db upgrade` y `agents db current`

Las migraciones de la plataforma vienen dentro del paquete, así que un wheel instalado puede aplicarlas sin un checkout y sin `alembic.ini`. La revisión `007`, las tablas del checkpointer de conversaciones, tiene que estar aplicada antes de que arranque la app servida. Necesita el extra `postgres`.

```bash
agents db upgrade --database-url postgresql+asyncpg://user:pw@host:5432/agents
agents db current --database-url postgresql+asyncpg://user:pw@host:5432/agents
```

- `upgrade` aplica todas las migraciones pendientes e informa cada una por stderr. Correrlo sobre una base que ya está en la última revisión no cambia nada.
- `current` imprime la revisión aplicada, por ejemplo `013 (head)`.
- `--database-url` indica la base para este comando. Sin él, el comando usa `DATABASE_URL`, y si no, la base configurada de la app servida (variables `DB_*` o `.env`).

En un checkout del código, `alembic upgrade head` sigue funcionando: el `alembic.ini` del repositorio apunta a las mismas migraciones, en `src/agents_system/migrations/`.
