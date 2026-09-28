# Uso como biblioteca

`agents_system` no es solo el runtime de ACME: es una biblioteca importable. El
paquete entrega la plataforma: el harness (loader → injector → factory) y los
roles de agente PREDEFINIDOS (`platform/roles/`). **No** entrega las tools de
ningún cliente ni los overrides de deployment de ningún cliente. La aplicación
que lo consume aporta ambas cosas por su cuenta, en runtime, por inyección — en
palabras del propio usuario, el requisito que este documento existe para
cumplir: *"el cliente debe armar su propia tool registry e inyectarlo al
agente."*

Este documento es para quien integra `agents_system` en su propia aplicación,
no para quien trabaja en la plataforma. Para la arquitectura de la plataforma,
leé primero `docs/platform_es/harness.md` y `docs/platform_es/deployment.md`.

Versión en inglés: `docs/platform/library-usage.md`.

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
instalado fuera del repositorio. Cualquier modelo de chat de LangChain sirve
en lugar de `ChatAnthropic`.

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
| Los roles predefinidos de la plataforma (`platform/roles/*/{role,manifest,policy}.md`) — lo que un `sales-agent`, `orchestrator`, `data-agent` o `summary-agent` *tiene permitido hacer* | Tus propios overrides de deployment (`deployments/{tu-cliente}/`), si querés prompts, skills o una política más estricta por cliente |
| El enforcement de RBAC (`InjectionError`, `FactoryError`, `DefinitionError`) | Los permisos concedidos a tu caller/identidad, y los `granted_permissions` que le pasás a `build_runtime` |
| Un `AgentRuntime` sobre LangGraph que convierte un runtime equipado en algo sobre lo que podés llamar `run_turn` | Cualquier `BaseChatModel` de LangChain al que enlazarlo |

La plataforma nunca tiene lógica de negocio específica de un cliente
incorporada — incluida ACME, el primer deployment de la plataforma. Si
encontrás código de plataforma que asume ACME, es un bug, no una
característica.

---

## Usarla desde otro repositorio

El paquete no está publicado en PyPI. Instalalo desde un tag de release con
uv:

```bash
uv add "agents-system @ git+ssh://git@github.com/nahuel893/agents-system@vX.Y.Z"
```

Esto funciona mientras el repositorio es privado, para cualquiera cuya clave
SSH tenga acceso; cuando vuelva a ser público,
`git+https://github.com/nahuel893/agents-system@vX.Y.Z` funciona sin
credenciales. Fijá un tag y no una rama, así cada actualización es un cambio
deliberado en tu `pyproject.toml`; uv registra el commit resuelto en tu
`uv.lock`.

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

El paquete depende de `torch` para los embeddings locales. Este repositorio lo
dirige al índice de PyTorch solo-CPU en su propio `[tool.uv.sources]`; agregá
las mismas entradas `[tool.uv.sources]` y `[[tool.uv.index]]` al
`pyproject.toml` de tu aplicación si querés lo mismo.

En ambos casos el nombre de importación es `agents_system` (con guion bajo);
solo el nombre de distribución en el índice lleva guion.

Instalar el paquete alcanza para tener los roles predefinidos de la
plataforma: viajan dentro del wheel (`platform/roles/**` se empaqueta como
`agents_system/platform` vía `[tool.hatch.build.targets.wheel.force-include]`
en `pyproject.toml`). Tus propios overrides de deployment nunca forman parte
de la instalación; los mantenés en el repositorio de tu aplicación y apuntás
`RootConfig` hacia ellos.

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
| `Agent` | `agents_system.agent.spec` | Un agente propio definido en Python (`Agent(...)`), desde una carpeta (`Agent.from_folder(path)`), o ambos; pasalo donde va un nombre de rol (`resolve`, `build_runtime`). Ver `docs/platform_es/role.md` |
| `ModelPrice` | `agents_system.config` | El precio de un modelo por millón de tokens de entrada/salida, para `AgentRuntime(model_prices=...)` |
| `AuditSink` | `agents_system.audit.sink` | El sink de eventos de auditoría; registrá uno con `AuditSink.set_current(sink)` para registrar eventos (ver "Correr sin el servidor") |

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


# 2. Registrá TODAS las tools que declara el rol. sales-agent declara cinco,
#    una de ellas (escalation_notifier) heredada de su rol padre; build_runtime
#    lanza InjectionError por cualquier tool declarada que falte en el
#    registro, salvo escalation_notifier, que recibe un default incorporado.
#    Registrar una tool no es concederla: una tool registrada pero no
#    concedida se deniega al construir y nunca llega al modelo, así que alcanza
#    con un stub para las que esta aplicación no usa.
def not_wired(inputs: dict, /) -> dict:
    return {"error": "this tool is not wired in this application"}


for name, permission, tier in [
    ("client_lookup", "read:client_registry", agents_system.Tier.T1),
    ("message_sender", "send:message", agents_system.Tier.T2),
    ("order_writer", "write:orders", agents_system.Tier.T2),
    ("escalation_notifier", "send:escalation", agents_system.Tier.T2),
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

Tres cosas que el ejemplo muestra a propósito:

- **Hay que registrar todas las tools que declara el rol** — incluidas las que
  el rol hereda (`sales-agent` recibe `escalation_notifier` de su padre,
  `platform/roles/agent`). `build_runtime` lanza `InjectionError: Unknown
  tool: ...` por cualquier tool declarada que falte en tu registro; la única
  excepción es `escalation_notifier` (ver "Escalamiento: el default
  incorporado"). Registrar
  una tool no es concederla: una tool cuyo permiso no pasás en
  `granted_permissions` se deniega al construir y nunca llega al modelo; por
  eso alcanza con un connector stub para las tools que esta aplicación no usa.
- **`granted_permissions` es la única concesión.** Acá el caller tiene
  `read:catalog`, así que `catalog_search` es la única tool equipada.
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

Para registrarlos, construí el sink sobre una session factory asíncrona de
SQLAlchemy cuya base de datos tenga las tablas de auditoría de la plataforma
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

### Escalamiento: el default incorporado

Todo agente definido por rol hereda `escalation_notifier` (`send:escalation`,
tier T2) de `platform/roles/agent`. Cuando tu registro no tiene ninguno,
`build_runtime` lo completa con un default incorporado para que el agente se
construya igual. Ese default:

- **no llega a ningún humano.** Le responde al modelo con el texto de la
  plataforma para "no hay canal de escalamiento configurado", así el modelo le
  dice al usuario que no se contactó a nadie en lugar de prometerle que lo van
  a llamar;
- en cada llamada registra un WARNING, `escalation.not_delivered` (rol,
  motivo, detalles), y un evento de auditoría `escalation_not_delivered`;
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

---

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

- Pipeline del harness y orden de inyección: `docs/platform_es/harness.md`
- Semántica de rol predefinido vs. override de deployment: `docs/platform_es/deployment.md`
- Esquema de archivos de rol/manifest/policy: `docs/platform_es/role.md`, `docs/platform_es/policy.md`
- Contrato de tools (`ToolSpec`, firma del connector): `docs/platform_es/tool.md`
- Sink, eventos y almacenamiento de auditoría: `docs/platform_es/audit.md`
- Uso de tokens y costo (`MODEL_PRICES`): `docs/platform_es/live-eval.md`
- Backends de referencia para los cuatro puertos genéricos de la plataforma (conocimiento, resumen, escalamiento, escritor de pedidos): `docs/platform_es/reference-backends.md`
- Código fuente: `src/agents_system/harness/{registry,loader,injector,factory,defaults}.py`, `src/agents_system/agent/graph.py`, `src/agents_system/audit/sink.py`, `src/agents_system/__init__.py`
