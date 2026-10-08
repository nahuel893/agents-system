# Uso como biblioteca

`agents_system` es un kit de desarrollo para construir agentes, y este documento
es su punto de entrada como biblioteca: el servidor es solo una pieza opcional.
El paquete entrega el kit: el harness (loader → injector → factory) y los
roles de agente PREDEFINIDOS (`platform/roles/`). **No** entrega las tools de
ningún cliente ni los overrides de deployment de ningún cliente. La aplicación
que lo consume aporta ambas cosas por su cuenta, en runtime, por inyección — en
palabras del propio usuario, el requisito que este documento existe para
cumplir: *"el cliente debe armar su propia tool registry e inyectarlo al
agente."*

Este documento es para quien integra `agents_system` en su propia aplicación,
no para quien trabaja en la plataforma. Para la arquitectura de la plataforma,
leé primero `docs/platform_es/harness.md` y `docs/platform_es/deployment.md`.

Para ver todas las funcionalidades del kit en una sola página, mirá
[`docs/platform_es/features.md`](features.md).

---

## Inicio rápido

Un agente con una tool propia, corriendo un turno. No se registra nada más:
la tool de escalamiento que todo agente hereda recibe un default incorporado
(ver "Escalamiento: el default incorporado" más abajo).

<!-- runnable-example: quickstart -->
```python
import asyncio

from langchain_core.messages import HumanMessage

import agents_system


# Tu tool: una función de un dict de entradas a un dict.
def word_count(inputs: dict, /) -> dict:
    return {"words": len(inputs["text"].split())}


registry = agents_system.ToolRegistry()
registry.register(
    agents_system.ToolSpec(
        name="word_count",
        description="Count the words in a text.",
        input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
        required_permissions=(),  # cálculo puro: nada que conceder
        connector=word_count,
        tier=agents_system.Tier.T0,
    )
)
agent = agents_system.Agent(name="word-counter", tools=("word_count",))
equipped = agents_system.build_runtime(agent, registry, granted_permissions=[])


async def main(model):
    runtime = agents_system.AgentRuntime(equipped, model=model)
    return await runtime.run_turn(
        [HumanMessage("How many words are in 'the quick brown fox'?")],
        session_id="quickstart",
    )


if __name__ == "__main__":
    from langchain_anthropic import ChatAnthropic  # lee ANTHROPIC_API_KEY

    messages = asyncio.run(main(ChatAnthropic(model="claude-sonnet-4-5")))
    print(messages[-1].content)
```

`tests/test_quickstart_example.py` ejecuta este bloque tal como está escrito,
y `scripts/smoke-installed-wheel.sh` lo corre contra el wheel construido,
instalado fuera del repositorio, solo con el núcleo: sin ningún extra.
Cualquier modelo de chat de LangChain sirve en lugar de `ChatAnthropic`, que
viene de `langchain-anthropic` (el extra `anthropic`; ver "Extras" más abajo).

- `Agent(...)` extiende el rol básico `agent` (conversación más escalamiento)
  salvo que pases `extends=`. Pasalo directamente a `build_runtime` o a
  `resolve`.
- Una tool que lee o cambia tus datos declara un permiso (por ejemplo
  `read:catalog`) y un tier acorde, y el caller lo concede en
  `granted_permissions`. El ejemplo completo de más abajo lo muestra.

---

## Qué te da la biblioteca y qué traés vos

| Viene de `agents_system` | Lo traés vos |
|---|---|
| El harness: `ToolRegistry`, `RootConfig`, `resolve`, `build_runtime`, `AgentRuntime` | Tus propios `ToolSpec`s — los connectors que llaman a *tus* servicios |
| Los roles predefinidos de la plataforma (`platform/roles/*/{role,manifest,policy}.md`) — lo que un `sales-agent`, `orchestrator`, `data-agent` o `summary-agent` *tiene permitido hacer*. Los ocho concretos quedan obsoletos en el próximo release en favor de los agentes de referencia de `examples/agents/`, ejemplos para copiar (ver "Nombre de importación y roles de la plataforma") | Tus propios overrides de deployment (`deployments/{tu-cliente}/`), si querés prompts, skills o una política más estricta por cliente |
| El enforcement de RBAC (`InjectionError`, `FactoryError`, `DefinitionError`) | Los permisos concedidos a tu caller/identidad, y los `granted_permissions` que le pasás a `build_runtime` |
| Un `AgentRuntime` sobre LangGraph que convierte un runtime equipado en algo sobre lo que podés llamar `run_turn` | Cualquier `BaseChatModel` de LangChain al que enlazarlo, salvo que el agente declare su propio `model:` |

La plataforma nunca tiene lógica de negocio específica de un cliente
incorporada — incluida ACME, el primer deployment de la plataforma. Si
encontrás código de plataforma que asume ACME, es un bug, no una
característica.

---

## Usarla desde otro repositorio

El paquete no está publicado en PyPI. Instalalo desde un tag de release con
uv:

```bash
uv add "agents-system @ git+https://github.com/nahuel893/agents-system@v0.4.0"
```

Reemplazá `v0.4.0` por el release que quieras. Por HTTPS no hace falta
configurar SSH y funciona sin credenciales cuando el repositorio sea público;
mientras es privado, git debe poder autenticarse por HTTPS (un credential
helper, o `gh auth setup-git`). La alternativa, para quien tenga una clave SSH
con acceso al repositorio, es la forma SSH:

```bash
uv add "agents-system @ git+ssh://git@github.com/nahuel893/agents-system@v0.4.0"
```

Desde el primer release posterior a v0.4.0, eso es una instalación simple: el
núcleo liviano, de unos 76 MB, que es todo lo que necesita el Inicio rápido. El
servidor, la base de datos, la tool SQL, los embeddings y los proveedores de
modelos de chat son extras (ver "Extras" más abajo); hasta v0.4.0 el mismo
comando traía todo eso, alrededor de 1 GB más, casi todo `torch`.

Fijá un tag y no una rama, así cada actualización es un cambio
deliberado en tu `pyproject.toml`; uv registra el commit resuelto en tu
`uv.lock`. Si todavía ningún tag contiene un cambio que necesitás, fijá el
hash completo de su commit de la misma forma (`...agents-system@<commit-sha>`)
y pasá al tag cuando se publique.

Un build de Docker normalmente no tiene credenciales de git. Construí el
wheel donde sí las tenés, copialo a tu aplicación e instalá el archivo:

```bash
# en un checkout de agents-system, en el tag que quieras
uv build --wheel   # escribe dist/agents_system-X.Y.Z-py3-none-any.whl

# en tu aplicación
uv add ./wheels/agents_system-X.Y.Z-py3-none-any.whl
```

La imagen necesita entonces el wheel en su contexto de build antes de
instalar dependencias (por ejemplo `COPY wheels/ wheels/` antes de
`uv sync --frozen`).

### Extras

Una instalación simple es el **núcleo**: la API de agentes (`Agent`), el
harness (`ToolRegistry`, `ToolSpec`, `build_runtime`, el injector y el
interceptor), el runtime en proceso (`AgentRuntime`) y las interfaces de
auditoría y métricas. Es todo lo que usan el Inicio rápido y el ejemplo
completo de más abajo. No instala ningún servidor web, ningún driver de base
de datos, ningún proveedor de modelos de chat ni `torch`: un virtualenv solo
con el núcleo mide unos 76 MB, mientras que una instalación completa pasa de
1 GB, casi todo `torch`.

Todo lo demás es un extra. Nombrá los que uses en el requisito:

```bash
uv add "agents-system[anthropic] @ git+https://github.com/nahuel893/agents-system@vX.Y.Z"
uv add "agents-system[server,openai] @ git+https://github.com/nahuel893/agents-system@vX.Y.Z"
```

| Extra | Qué habilita | Qué instala |
|---|---|---|
| `server` | La app servida (`agents_system.main`, `agents_system.server`): HTTP, los canales y el adaptador compatible con OpenAI. Trae `postgres`, porque la app corre sobre su base de datos, que también guarda sus conversaciones. Un agente registrado también necesita un proveedor de modelos de chat; ver "Qué necesita la app servida" más abajo. | FastAPI, uvicorn, Redis (sondeado por `GET /health`) |
| `postgres` | `AuditSink` (registrar eventos de auditoría), las tablas de la plataforma y sus migraciones, los backends de referencia, y el checkpointer durable de conversaciones (`docs/platform_es/conversation-memory.md`) | SQLAlchemy, asyncpg, Alembic, langgraph-checkpoint-postgres, psycopg |
| `sql` | La tool de consultas SQL con guarda. Trae `postgres`. | sqlglot |
| `embeddings` | Embeddings para recuperación, locales o por la API de OpenAI | torch, sentence-transformers, openai |
| `openai` | El proveedor `openai_compatible` | langchain-openai |
| `anthropic` | El proveedor `anthropic` | langchain-anthropic |
| `groq` | El proveedor `groq` | langchain-groq |
| `ollama` | El proveedor `ollama` | langchain-ollama |
| `all` | Todos los extras de arriba. Es lo que instala la imagen Docker de la app servida. | |

Un extra de proveedor hace falta cuando el runtime construye el modelo que
declara un agente (`AgentRuntime(equipped)` sin `model=`, o
`build_agent_model`; ver "Elegir el modelo"). Un modelo que construís y pasás
vos necesita su propio paquete de integración, lo instales como lo instales.

**Qué necesita la app servida.** `server` solo arranca la app y corre un
agente registrado, con dos condiciones:

- **Un modelo de chat.** La app construye su modelo por defecto a partir de
  `ADAPTER_PROVIDER` (`ollama` si no lo configurás), así que instalá también
  el extra de ese proveedor, por ejemplo `agents-system[server,openai]` para
  `openai_compatible`. O pasá tu propio modelo a `create_app(chat_model=...)`,
  que no necesita ningún extra de proveedor.
- **Embeddings, solo si una tool tuya los usa.** La app le pasa a tu registry
  factory un embedder (`EMBEDDING_PROVIDER`, `local` por defecto). Con el
  extra `embeddings` instalado, se construye al arrancar, así que el modelo
  local se carga en ese momento y no dentro de un turno. Sin él, la app
  arranca igual y registra un warning `embeddings.unavailable`, y una tool que
  llama al embedder recibe un `MissingExtraError` que nombra `embeddings`. La
  recuperación de catálogo de la plataforma (`search_catalog`) cae a búsqueda
  por palabras clave ante cualquier error del embedder, este incluido, así que
  instalá `embeddings` si querés búsqueda vectorial.

`all` instala todo lo que la app servida puede usar.

Usar una funcionalidad sin su extra falla de inmediato, con un error que
nombra el extra en lugar de un simple `No module named 'fastapi'`:

```text
agents_system.extras.MissingExtraError: The served app needs the 'server' extra,
which is not installed (no module named 'fastapi'). Install the package with it:
agents-system[server]. The install section of docs/platform/library-usage.md
lists every extra.
```

`MissingExtraError` es un `ModuleNotFoundError`: su `name` es el módulo que
falta y su `extra`, el extra a instalar. `agents_system.extras.EXTRA_MODULES`
asocia cada extra con los módulos que provee.

Hasta v0.4.0 una instalación simple traía todo lo anterior. Una aplicación que
use el servidor, la base de datos, la tool SQL, los embeddings o el proveedor
del modelo que declara un agente nombra los extras correspondientes al
actualizar más allá de esa versión.

El extra `embeddings` instala `torch`. Este repositorio lo dirige al índice de
PyTorch solo-CPU en su propio `[tool.uv.sources]`; agregá las mismas entradas
`[tool.uv.sources]` y `[[tool.uv.index]]` al `pyproject.toml` de tu aplicación
si querés lo mismo.

### Nombre de importación y roles de la plataforma

Lo instales como lo instales, el nombre de importación es `agents_system` (con
guion bajo); solo el nombre de distribución en el índice lleva guion.

Instalar el paquete alcanza para tener los roles predefinidos de la
plataforma: viajan dentro del wheel (`platform/roles/**` se empaqueta como
`agents_system/platform` vía `[tool.hatch.build.targets.wheel.force-include]`
en `pyproject.toml`). Tus propios overrides de deployment nunca forman parte
de la instalación; los mantenés en el repositorio de tu aplicación y apuntás
`RootConfig` hacia ellos.

**Los roles concretos están obsoletos (llega en el próximo release).** Solo
los padres genéricos, `base` y `agent`, siguen siendo API soportada
(ADR-010). Los ocho roles concretos (`sales-agent`, `support-agent`,
`data-agent`, `summary-agent`, `accountant-agent`, `orchestrator`,
`developer-agent`, `operator-agent`) tienen copias en el repositorio bajo
`examples/agents/<nombre>/`: agentes de referencia, ejemplos para copiar que
no traen promesa de compatibilidad y no están en el wheel. Nombrar un rol
concreto (`build_runtime("sales-agent", ...)`, `extends="sales-agent"`,
`create_app(agents=...)`, `AGENT_REGISTRATIONS`) sigue funcionando y emite un
`PredefinedRoleDeprecationWarning` que nombra la carpeta a copiar; la próxima
versión incompatible los saca de `platform/roles/`. Copiá la carpeta a tu
proyecto y pasá `Agent.from_folder(path)` donde iba el nombre. Cinco de las
copias difieren de su rol para cumplir las reglas que sigue toda carpeta de
agente; `docs/platform_es/role.md`, "Agentes de referencia", lista cada
cambio.

---

## La API pública

Todo lo siguiente se alcanza directamente desde el paquete (`import
agents_system; agents_system.X`), de forma perezosa: acceder a un nombre
importa solo lo que ese nombre necesita, nada más:

| Nombre | Módulo de origen | Qué es |
|---|---|---|
| `ToolRegistry` | `agents_system.harness.registry` | El registro que construís y llenás con tus propias tools |
| `ToolSpec` | `agents_system.harness.registry` | El contrato de una tool: nombre, permisos requeridos, connector, esquema de entrada, `tier` de capacidad |
| `Tier` | `agents_system.harness.registry` | Enum del tier de capacidad (`T0`-`T3`, ADR-002 C.10) — obligatorio en cada `ToolSpec` |
| `ToolNotFoundError` | `agents_system.harness.registry` | Lo lanza `ToolRegistry.get()` para un nombre no registrado |
| `RootConfig` | `agents_system.harness.loader` | El par inyectable de rutas `platform_root` / `deployments_root` |
| `AgentDefinition` | `agents_system.harness.loader` | La definición de rol resuelta y congelada que devuelve `resolve()` |
| `resolve` | `agents_system.harness.loader` | Carga y fusiona una definición de rol (predefinida, opcionalmente + un override de cliente) |
| `DefinitionError` | `agents_system.harness.loader` | Se lanza cuando una definición o sus raíces son inválidas |
| `build_runtime` | `agents_system.harness.factory` | El único punto de paso: rol + tu registro + tus permisos concedidos → `EquippedRuntime` |
| `EquippedRuntime` | `agents_system.harness.factory` | La especificación completamente ensamblada — tools, prompt, definición — que devuelve `build_runtime` |
| `FactoryError` | `agents_system.harness.factory` | Se lanza cuando no se puede ensamblar un runtime (p. ej. falta un archivo de skill declarado) |
| `InjectionError` | `agents_system.harness.injector` | Se lanza cuando un rol declara una tool que tu registro no tiene |
| `AgentRuntime` | `agents_system.agent.graph` | Envuelve un `EquippedRuntime` + un modelo de chat en algo sobre lo que llamás `run_turn` |
| `build_agent_model` | `agents_system.agent.models` | Construye el modelo de chat que declara el `model:` resuelto de un agente, con credenciales solo de la configuración/entorno. `AgentRuntime(equipped)` lo llama cuando no pasás modelo. Ver "Elegir el modelo" |
| `Agent` | `agents_system.agent.spec` | Un agente propio definido en Python (`Agent(...)`), desde una carpeta (`Agent.from_folder(path)`), o ambos; pasalo donde va un nombre de rol (`resolve`, `build_runtime`). Ver `docs/platform_es/role.md` |
| `ModelPrice` | `agents_system.config` | El precio de un modelo por millón de tokens de entrada/salida, para `AgentRuntime(model_prices=...)` |
| `AuditSink` | `agents_system.audit.sink` | El sink de eventos de auditoría; registrá uno con `AuditSink.set_current(sink)` para registrar eventos (ver "Correr sin el servidor") |
| `TurnSubject` | `agents_system.subject` | El usuario final para quien es un turno; pasalo a `run_turn(..., subject=)` (ver [Alcance del usuario final](end-user-scope.md)) |
| `TurnActor` | `agents_system.subject` | Quién inició el turno (`kind` cerrado + `id`); pasalo a `run_turn(..., actor=)` |

También está disponible `agents_system.__version__`, resuelto desde los
metadatos del paquete instalado (con un valor provisorio en un checkout de
código fuente que nunca se instaló con `pip install`).

---

## Ejemplo completo

Este ejemplo corre tal como está escrito: `tests/test_readme_library_example.py`
lo ejecuta (con un modelo de chat guionado que se le pasa a `main`), y
`scripts/smoke-installed-wheel.sh` corre la copia del README contra el wheel
construido, instalado fuera del repositorio, con un entorno vacío. Es el mismo
ejemplo que el del README, con los comentarios traducidos.

<!-- runnable-example: library -->
```python
import asyncio

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AnyMessage, HumanMessage

import agents_system

# 1. Tus propias tools. Un connector es TU código: puede llamar a tu base de
#    datos, a tus APIs, a lo que sea. Este busca en un catálogo en memoria.
CATALOG = [
    {"id": "item-001", "name": "Item Alpha", "stock": 50},
    {"id": "item-002", "name": "Item Beta", "stock": 0},
]


def catalog_search(inputs: dict, /) -> dict:
    query = inputs.get("query", "").lower()
    return {"results": [item for item in CATALOG if query in item["name"].lower()]}


registry = agents_system.ToolRegistry()
registry.register(
    agents_system.ToolSpec(
        name="catalog_search",
        required_permissions=("read:catalog",),
        connector=catalog_search,
        tier=agents_system.Tier.T1,
        description="Search the product catalog by free-text query.",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    )
)


# 2. Registrá las demás tools que declara el rol. sales-agent declara cinco;
#    build_runtime lanza InjectionError por cualquier tool declarada que falte
#    en el registro, salvo escalation_notifier (heredada de su rol padre), que
#    recibe un default incorporado que no llega a ningún humano. Registrar una
#    tool no es concederla: una tool registrada pero no concedida se deniega
#    al construir y nunca llega al modelo, así que alcanza con un stub para
#    las que esta aplicación no usa.
def not_wired(inputs: dict, /) -> dict:
    return {"error": "this tool is not wired in this application"}


for name, permission, tier in [
    ("client_lookup", "read:client_registry", agents_system.Tier.T1),
    ("message_sender", "send:message", agents_system.Tier.T2),
    ("order_writer", "write:orders", agents_system.Tier.T2),
]:
    registry.register(
        agents_system.ToolSpec(
            name=name,
            required_permissions=(permission,),
            connector=not_wired,
            tier=tier,
        )
    )

# 3. Construí el runtime equipado: un rol predefinido, tu registro y los
#    permisos que tiene este caller. Solo se equipan las tools concedidas.
equipped = agents_system.build_runtime(
    "sales-agent",
    registry,
    granted_permissions=["read:catalog"],
)


# 4. Convertilo en un agente vivo con cualquier modelo de chat de LangChain y
#    corré un turno.
async def main(model: BaseChatModel) -> list[AnyMessage]:
    agent = agents_system.AgentRuntime(equipped, model=model)
    return await agent.run_turn(
        [HumanMessage(content="Do you have Item Alpha in stock?")],
        session_id="demo-session",
    )


if __name__ == "__main__":
    from langchain_anthropic import ChatAnthropic

    messages = asyncio.run(main(ChatAnthropic(model="claude-sonnet-4-5")))
    print(messages[-1].content)
```

Cuatro cosas que el ejemplo muestra a propósito:

- **Hay que registrar todas las tools que declara el rol**, incluidas las
  heredadas, con una excepción: `escalation_notifier`, que todo agente
  definido por rol hereda de `platform/roles/agent`, recibe un default
  incorporado cuando tu registro no lo tiene (ver "Escalamiento: el default
  incorporado"). `build_runtime` lanza `InjectionError: Unknown tool: ...`
  por cualquier otra tool declarada que falte en tu registro. Registrar una
  tool no es concederla: una tool cuyo permiso no pasás en
  `granted_permissions` se deniega al construir y nunca llega al modelo; por
  eso alcanza con un connector stub para las tools que esta aplicación no usa.
- **`granted_permissions` es la única concesión.** Acá el caller tiene
  `read:catalog`, así que `catalog_search` es la única tool equipada.
- **Un nombre obsoleto, por ahora.** `"sales-agent"` es un rol predefinido
  concreto, así que este ejemplo emite un `PredefinedRoleDeprecationWarning`
  en el próximo release (ver "Nombre de importación y roles de la
  plataforma"). Con una copia de `examples/agents/sales-agent/`, pasá
  `agents_system.Agent.from_folder(path)` en su lugar; el registro y las
  concesiones quedan iguales.
- **Sin override de deployment.** Sin `client=`, `build_runtime` usa el rol
  predefinido tal como se entrega, y no hace falta `RootConfig`. Para acotar
  el rol con tu propio deployment, agregá ambos — ver la Trampa 1 para saber
  por qué `deployments_root` tiene que ser explícito:

  ```python
  equipped = agents_system.build_runtime(
      "sales-agent",
      registry,
      granted_permissions=["read:catalog"],
      client="my-client",
      roots=agents_system.RootConfig(deployments_root=MY_APP_ROOT / "deployments"),
  )
  ```

---

## Elegir el modelo: el que declara el agente

Un agente puede declarar su propio modelo (`Agent(model={...})`, o un bloque
`model:` en su frontmatter, heredado por `extends`; ver
`docs/platform_es/policy.md`, "Configuración de modelo"). En uso como
biblioteca esa declaración se respeta: si no pasás modelo, `AgentRuntime` lo
construye.

```python
import agents_system

agent = agents_system.Agent(
    name="summarizer",
    model={
        "provider": "openai_compatible",
        "model": "vendor/model-id",
        "temperature": 0.2,
    },
)
equipped = agents_system.build_runtime(agent, registry, granted_permissions=[])

runtime = agents_system.AgentRuntime(equipped)  # sin model=: el declarado
```

`agents_system.build_agent_model(equipped.definition)` es ese mismo paso por
separado, para cuando querés el modelo de chat en sí (para envolverlo,
compartirlo o inspeccionarlo). Pasá `default_model=` para recibir un respaldo
de un agente que no declara nada.

La integración de cada proveedor es un extra: `openai` para
`openai_compatible`, y `anthropic`, `groq` y `ollama` para los proveedores de
esos nombres (ver "Extras"). Un modelo declarado cuyo extra no está instalado
falla al construirse el modelo, con un error que nombra el extra.

**Las credenciales vienen solo de la configuración y del entorno, nunca de la
definición del agente** (un bloque `model:` no puede llevarlas). El builder las
lee con un cargador estrecho, solo de proveedores, así que no hace falta
ninguna variable del servidor (`META_WEBHOOK_SECRET`, URL de base de datos, ...):

| Proveedor | Variables |
|---|---|
| `openai_compatible` | `OPENAI_COMPATIBLE_BASE_URL`, `OPENAI_COMPATIBLE_API_KEY` |
| `anthropic` | `ANTHROPIC_API_KEY` |
| `groq` | `GROQ_API_KEY` |
| `ollama` | `OLLAMA_BASE_URL` |

Una variable obligatoria faltante (`OPENAI_COMPATIBLE_BASE_URL`,
`ANTHROPIC_API_KEY`) falla al construir, con un error que nombra al agente y a
la variable. `OPENAI_COMPATIBLE_API_KEY` es opcional, para endpoints locales
sin clave.

### Cuando pasás un modelo Y el agente declara uno

La regla, a propósito la misma en todo el uso como biblioteca:

| Pasás `model=` | El agente declara `model:` | Resultado |
|---|---|---|
| no | sí | Se construye y se usa el modelo declarado. |
| no | no | `ValueError`: el agente no declara modelo; pasá `model=`. |
| sí | no | Se usa tu modelo; no se reporta nada. |
| sí | sí, mismo id de modelo y mismo proveedor | Se usa tu modelo; no se reporta nada. |
| sí | sí, mismo id de modelo, proveedor no identificable | Se usa tu modelo; un evento `runtime.model_provider_unverified` (nivel info) indica que no se pudo establecer el proveedor (no se lo da por equivalente ni por conflicto). |
| sí | sí, otro id de modelo, o el mismo id de otro proveedor | **Gana tu modelo** y se registra una advertencia `runtime.model_conflict` con el agente, los ids declarado y pasado, y los proveedores declarado y pasado. |

La comparación tiene límites. El proveedor del modelo pasado se lee de su clase
(`langchain_openai` = `openai_compatible`, `langchain_groq` = `groq`,
`langchain_anthropic` = `anthropic`, `langchain_ollama` = `ollama`), así que
distingue esos cuatro pero no dos hosts de protocolo OpenAI entre sí, y un doble
de test o un wrapper propio no tiene proveedor identificable. No se comparan
`temperature` ni `max_tokens`.

Gana el modelo explícito del caller porque pasarlo es un acto deliberado en el
sitio de la llamada (un doble de test, un cliente compartido, un override
local), y rechazarlo haría imposible sobrescribir la declaración en tests. La
advertencia hace visible el desacuerdo en lugar de dejarlo en silencio. El
modelo declarado no se construye cuando pierde, así que la advertencia no
necesita credenciales.

La app servida no cambia: construye al arrancar el modelo declarado de cada
runtime (allí la declaración tiene precedencia; el modelo global de
`ADAPTER_PROVIDER` es solo el default de los agentes que no declaran ninguno).
Ambos caminos llaman a una sola implementación, `agents_system.agent.models`,
así que no pueden divergir.

---

## Correr sin el servidor

`build_runtime` → `AgentRuntime(...)` → `run_turn` nunca construye los
`Settings` del servidor y no necesita ninguna de sus variables de entorno (ni
`.env`, ni `META_WEBHOOK_SECRET`, ni URL de base de datos). Dos
funcionalidades del servidor pasan a ser opcionales en lugar de implícitas:

### Costo: pasá `model_prices`

La app servida valoriza cada turno desde `MODEL_PRICES`. En uso como
biblioteca, el runtime no tiene tabla de precios salvo que le pases una, con
clave el id del modelo del proveedor (la misma clave que usa `MODEL_PRICES` —
ver `docs/platform_es/live-eval.md`):

```python
agent = agents_system.AgentRuntime(
    equipped,
    model=model,
    model_prices={
        "claude-sonnet-4-5": agents_system.ModelPrice(
            input_per_million=3.0, output_per_million=15.0
        ),
    },
)
result = await agent.run_turn_with_usage(messages, session_id="s1")
result.usage.cost_usd  # float, o None si el id de modelo no tiene precio
```

Sin `model_prices`, `usage.cost_usd` es `None` — nunca un número adivinado.
Los conteos de tokens se reportan en ambos casos.

### Auditoría: registrá un `AuditSink`

Los eventos de auditoría (tool concedida/denegada, intento/bloqueo de llamada
a tool, runtime construido, ...) van al `AuditSink` registrado en el proceso.
El lifespan de `create_app` registra uno sobre la base de datos de la
aplicación. En uso como biblioteca no hay ninguno registrado salvo que lo
registres vos, y el proceso emite una única advertencia estructurada,
`audit.sink_not_registered`, la primera vez que un evento no tiene adónde ir —
una vez por proceso, nunca un traceback por evento.

Para registrarlos (con el extra `postgres`, a través del cual escribe el sink),
construí el sink sobre una session factory asíncrona de SQLAlchemy cuya base de
datos tenga las tablas de auditoría de la plataforma
(creadas por las migraciones de Alembic de este repositorio — ver
`docs/platform_es/audit.md`), inicialo y registralo para todo el proceso:

```python
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

engine = create_async_engine(MY_DATABASE_URL)
sink = agents_system.AuditSink(
    session_factory=async_sessionmaker(engine, expire_on_commit=False)
)
await sink.start()
agents_system.AuditSink.set_current(sink)
try:
    ...  # construir runtimes y correr turnos
finally:
    await sink.stop()  # vuelca lo que todavía está en cola
    agents_system.AuditSink.clear_current(sink)
```

El registro es de todo el proceso: cada task registra en el mismo sink, sin
importar qué task lo registró.

### Memoria de conversación: pasá un `checkpointer`

Por defecto un runtime no tiene estado: enviás todo el historial en cada
`run_turn`. Para que conserve una conversación entre turnos, pasá cualquier
saver de LangGraph como `checkpointer=` y un `thread_id` por conversación;
cada turno lleva entonces solo su(s) mensaje(s) nuevo(s). El hilo se lee
antes del turno y se guarda una vez después, y un saver que falla degrada el
turno (respondido sin historial, o respondido y no recordado) en vez de
hacerlo fallar. Lo mismo pasa con uno que deja de responder: cada lectura y
cada guardado se abandonan pasado `checkpointer_timeout_s` (5 segundos por
defecto).

```python
from agents_system.services.checkpointer import open_postgres_checkpointer

async with open_postgres_checkpointer(
    MY_DATABASE_URL, pool_size=5, timeout_s=5.0
) as saver:
    agent = agents_system.AgentRuntime(equipped, model=model, checkpointer=saver)
    await agent.run_turn(
        [HumanMessage("Hola, soy Ana")], session_id="s1", thread_id="cliente-42"
    )
```

`open_postgres_checkpointer` es el saver durable de la app servida, que
instala el extra `postgres`; rechaza una base sin sus tablas, que crea `apply_checkpoint_migrations(url)` (o el
`setup()` propio del saver). Cualquier otro saver también sirve, como el
`InMemorySaver` de LangGraph para tests. Cuánto tiempo se conservan los
hilos, y qué implica eso para los datos personales, está en
`docs/platform_es/conversation-memory.md`.

### Escalamiento: el default incorporado

Todo agente definido por rol hereda `escalation_notifier` (`send:escalation`,
tier T2) de `platform/roles/agent`. Cuando tu registro no tiene ninguno,
`build_runtime` lo completa con un default incorporado para que el agente se
construya igual. Ese default:

- **no llega a ningún humano.** Le responde al modelo con el texto de la
  plataforma para "no hay canal de escalamiento configurado", así el modelo le
  dice al usuario que no se contactó a nadie en lugar de prometerle que lo van
  a llamar;
- en cada llamada registra un WARNING, `escalation.not_delivered`, y un
  evento de auditoría `escalation_not_delivered`. Ambos llevan el rol, el
  motivo (cortado a 200 caracteres y pasado por el redactor de tu política de
  auditoría) y el largo de los detalles. Ninguno guarda el texto de los
  detalles: es texto libre sobre el cliente, como nombres, documentos o
  direcciones, para el que el redactor no tiene patrón;
- solo se equipa si concedés `send:escalation`, y en ese caso registra un
  WARNING, `injector.builtin_default_granted`, al construir. El inicio rápido
  no concede nada, así que ahí se deniega y nunca llega al modelo;
- lo reemplaza cualquier `escalation_notifier` que registres. El puerto
  `EscalationChannel` detrás de la tool real se describe en
  `docs/platform_es/reference-backends.md`.

La app servida nunca lo usa. El lifespan de `create_app` construye con
`build_runtime(..., builtin_defaults=False)`, así que un rol servido sin su
propio `escalation_notifier` falla el arranque con `InjectionError: Unknown
tool: escalation_notifier`, y un registro que contiene el default (registrado
a mano) se rechaza al arrancar. Pasá `builtin_defaults=False` vos mismo para
tener el mismo comportamiento de falla cerrada en proceso.

**Por qué el default queda activado y cómo verlo (issue #182).** Los defaults
vienen activados para que el Quickstart corra en unas diez líneas sin
registrar nada. Es un trade-off deliberado, no un descuido, y la respuesta a
"es fácil olvidar que están activos" es una señal más fuerte en lugar de un
opt-in:

- el build registra `injector.builtin_default_granted` (arriba);
- `EquippedRuntime.builtin_defaults` lista los defaults incluidos otorgados del
  runtime (un default denegado no aparece), así que un despliegue puede
  afirmarlo en un test o en un chequeo de arranque;
- la auditoría los marca: `tool_granted`, `tool_call_attempted` y
  `runtime_built` llevan `builtin_default` (`runtime_built` lleva la lista
  `builtin_defaults`), ver `docs/platform_es/audit.md`.

---

## Bloques de datos no confiables

Los datos guardados o externos (los datos que se guardaron de un usuario, una transcripción) llegan al modelo solo como un bloque `UntrustedData(source, content)`, que se pasa a `run_turn` para ese turno. Nunca pegues esos datos en un mensaje ni en un prompt.

```python
from agents_system.agent.untrusted import UntrustedData

await runtime.run_turn(
    messages,
    session_id="s1",
    untrusted_data=[UntrustedData("user_facts", "Prefiere respuestas cortas.")],
)
```

- **Dónde va.** El grafo renderiza los bloques en un solo mensaje justo después del mensaje de sistema, al llamar al modelo. Nunca van dentro del mensaje de sistema ni se guardan en el estado de la conversación.
- **Delimitado.** Cada bloque nombra su `source` (letras, dígitos y `_ . : -`, hasta 64) y queda entre líneas `BEGIN <límite>` y `END <límite>`. El límite es aleatorio en cada turno y se vuelve a sortear hasta que no aparece en ningún bloque, así que el contenido no puede cerrar su propio bloque.
- **Nunca es política.** Las herramientas, los permisos, los límites y el prompt de sistema quedan fijos antes de renderizar un bloque. Ninguna de las dos capas de permisos lee un bloque, `untrusted_input: true` mantiene su prohibición de T3, y un bloque nunca se vuelve un prompt, una skill ni un argumento de herramienta que llene la plataforma. La cláusula 2 del contrato base le indica al modelo tratar los bloques como datos.
- **Acotado.** `max_chars` limita una fuente (2000 caracteres por defecto); un turno renderiza como máximo 8000 caracteres en total (preámbulo, delimitadores, nombres de fuente y marcadores incluidos) y como máximo 20 bloques, y un bloque que no entra se corta o se omite por nombre (los primeros cinco nombres, luego un conteo). El tope nunca supera el presupuesto de contexto (unos cuatro caracteres por token), así que los bloques no pueden dejar la solicitud fuera de presupuesto. Los bloques cuentan como tokens fijos contra `max_context_tokens`, así que se recorta el historial para hacerles lugar. Si el prompt de sistema, los esquemas de herramientas y el turno actual no dejan lugar para ellos, los bloques se omiten de esa solicitud, y se registra como `runtime.untrusted_blocks_omitted`, en lugar de dejarla fuera de presupuesto.
- **Siempre activo.** No hay ajuste para apagarlo ni para inyectar los mismos datos como texto plano.

Un escenario en vivo, `untrusted_input_block` en la suite de capacidades, planta una instrucción de pedido en un bloque y comprueba que el agente no hace ningún pedido.

## Dos trampas

### Trampa 1 — `deployments_root` apunta por defecto al checkout de desarrollo

Si construís `agents_system.RootConfig()` sin argumentos (u omitís
`deployments_root` en una llamada explícita a `RootConfig(...)`), apunta por
defecto al directorio `deployments/` del checkout git de `agents_system` desde
el que corre tu entorno de Python — o sea, `deployments/acme/` de **este**
repositorio, no el tuyo. `platform_root` tiene un valor por defecto real en el
paquete instalado (ver más abajo); `deployments_root` a propósito no lo tiene,
porque los deployments de un cliente nunca viajan dentro del paquete.

**Desde D-024 slice 2, olvidarlo ya no falla en silencio en el caso que más
importa**: si pedís un override de cliente (`resolve(role, client=...)` /
`build_runtime(..., client=...)`) y el directorio `deployments_root` resuelto
no existe (p. ej. `agents_system` instalado como dependencia, sin un
`deployments/` al lado), la biblioteca lanza un `DefinitionError` explícito
que nombra la ruta exacta que buscó, en lugar de caer en silencio al rol
predefinido. Un override de deployment solo puede *acotar* el rol predefinido,
nunca ampliarlo — así que caer en silencio habría ampliado
tools/autonomía/permisos más allá de lo que el override pedido (pero no
configurado) debía restringir; eso es una relajación de seguridad disfrazada
de valor por defecto seguro, y por eso ahora es una falla ruidosa en lugar de
una advertencia en el log.

Esto **no** vuelve seguro omitir `deployments_root`: el valor adivinado por
defecto solo coincide con un checkout de desarrollo de `agents_system` ubicado
al lado. Si tu aplicación corre desde un clon donde ese directorio existe por
una razón ajena, vas a recibir en silencio los overrides de ACME en lugar de
un error — la nueva guarda solo detecta el caso de *directorio inexistente*,
no el de *existe pero es el equivocado*. **Pasá siempre `deployments_root` de
forma explícita** cuando tengas tus propios overrides de deployment; no
confíes en que el valor adivinado funcione solo porque ya no falla en
silencio cuando falta.

`platform_root`, en cambio, se resuelve solo: `RootConfig()` prueba primero la
ubicación empaquetada (`platform/` junto al paquete `agents_system`
instalado), después la del checkout de desarrollo, y lanza `DefinitionError`
nombrando ambas rutas solo si no existe ninguna
(`src/agents_system/harness/loader.py`, `_default_platform_root`). Solo
necesitás pasar `platform_root` si entregás tu propio fork de los roles
predefinidos.

### Trampa 2 — `load_override` devuelve `None` en silencio ante un nombre de cliente mal escrito

`agents_system.harness.loader.load_override` (`loader.py:290`) devuelve `None`
cuando `deployments_root/{client}/{role_type}/` no existe en disco —
**en silencio, sin advertencia, sin error**:

```python
if not folder.exists():
    return None  # loader.py:303
```

`resolve(role_type, client=..., roots=...)` trata un override `None`
exactamente como "este cliente no tiene override" y cae a la definición del
rol predefinido. Eso significa que un error de tipeo en `client=`
(`"my-cilent"` en lugar de `"my-client"`) no lanza nada: te devuelve en
silencio el rol predefinido sin ninguno de los prompts, skills ni
restricciones de política de tu deployment. Este comportamiento no cambia con
este documento; se documenta acá para que sepas verificar `AgentDefinition.
deployment is not None` (o loguear `definition.deployment`) si necesitás
confirmar que un override efectivamente se aplicó.

---

## Referencias cruzadas

- Todas las funcionalidades del kit, con su punto de entrada y su doc: `docs/platform_es/features.md`
- Pipeline del harness y orden de inyección: `docs/platform_es/harness.md`
- Semántica de rol predefinido vs. override de deployment: `docs/platform_es/deployment.md`
- Esquema de archivos de rol/manifest/policy: `docs/platform_es/role.md`, `docs/platform_es/policy.md`
- Contrato de tools (`ToolSpec`, firma del connector): `docs/platform_es/tool.md`
- Sink, eventos y almacenamiento de auditoría: `docs/platform_es/audit.md`
- Memoria de conversación (checkpointer, retención, política ante fallas): `docs/platform_es/conversation-memory.md`
- Uso de tokens y costo (`MODEL_PRICES`): `docs/platform_es/live-eval.md`
- Backends de referencia para los cuatro puertos genéricos de la plataforma (conocimiento, resumen, escalamiento, escritor de pedidos): `docs/platform_es/reference-backends.md`
- Código fuente: `src/agents_system/harness/{registry,loader,injector,factory,defaults}.py`, `src/agents_system/agent/graph.py`, `src/agents_system/audit/sink.py`, `src/agents_system/__init__.py`
- Versión en inglés: `docs/platform/library-usage.md`
