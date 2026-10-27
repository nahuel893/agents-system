# Modelo de Despliegue (Deployment Model)

La plataforma separa las definiciones de roles predefinidas de las implementaciones específicas de los clientes a través de una estructura de dos capas. Los roles predefinidos definen la plantilla de comportamiento y las fronteras de capacidades. Los despliegues (*deployments*) extienden y especializan esos roles para el contexto específico de cada cliente.

Esta separación es la resolución de la decisión abierta #5: la propiedad intelectual (IP) de la plataforma reside en `platform/roles/`, mientras que la implementación específica del cliente reside en `deployments/{client}/`.

---

## Estructura de carpetas

```
platform/
  roles/
    sales-agent/          ← predefinido: qué es un agente de ventas
      role.md
      manifest.md
      policy.md
    orchestrator/
      role.md
      manifest.md
      policy.md
    data-agent/
      role.md
      manifest.md
      policy.md
    summary-agent/
      role.md
      manifest.md
      policy.md

deployments/
  acme/
    sales-agent/          ← sobreescritura de ACME: preventista + DeW + español coloquial AR
      role.md
      manifest.md
      policy.md
      skills/
        order_extraction.md
        colloquial_matching.md
        confirm_flow.md
  other-client/
    sales-agent/          ← sobreescritura para otro cliente: idioma, herramientas y políticas distintas
      role.md
      manifest.md
```

---

## Semántica de mezcla (*Merge semantics*)

Cuando el harness instancia un agente para un despliegue, construye la definición final en tres pasos secuenciales:

1. Carga el rol predefinido desde `platform/roles/{role-type}/`
2. Mezcla (*merge*) la sobreescritura del cliente desde `deployments/{client}/{role-type}/`
3. Construye el runtime a partir de la definición ya mezclada y consolidada

La sobreescritura sigue estrictamente estas reglas por cada archivo:

### Sobreescritura de `role.md`

El archivo `role.md` del despliegue extiende el rol predefinido con contexto específico del cliente:
- Añade el nombre de la empresa, el dominio, el idioma y el vocabulario de negocio.
- Añade la declaración del propósito del cliente por encima de la definición predefinida.
- No puede eliminar ni contradecir las fronteras de alcance del rol predefinido.

### Sobreescritura de `manifest.md`

El archivo `manifest.md` del despliegue puede:
- Añadir herramientas desde el registro aprobado de la plataforma (ej. `dew_connector`, `app_preventas_writer`).
- Declarar cuáles habilidades de la carpeta `skills/` del despliegue están activas.
- Restringir el alcance del contexto (haciéndolo más estrecho que el predefinido).

El archivo `manifest.md` del despliegue no puede:
- Añadir herramientas que no estén presentes en el registro global de la plataforma.
- Elevar los requisitos de permisos de ninguna herramienta.
- Expandir el acceso al contexto más allá de lo que permite el manifiesto predefinido.

### Sobreescritura de `policy.md`

El archivo `policy.md` del despliegue puede:
- Restringir el nivel de autonomía (ej. `supervised` → `confirm`).
- Añadir reglas de escalamiento más estrictas: `escalation_rules: {inherit: true, add: [...]}` agrega condiciones, y sus `descriptions` describen las que agrega.
- Endurecer la redacción de auditoría (agregar `redact_keys`).
- Reducir los límites de ejecución (timeouts más cortos, menor número de llamadas a herramientas permitidas).
- Definir umbrales específicos del cliente para la intervención humana (*human-in-the-loop*).

El archivo `policy.md` del despliegue no puede:
- Elevar el nivel de autonomía por encima del límite máximo del rol predefinido.
- Eliminar una condición de escalamiento definida en la política predefinida. Un mapeo `escalation_rules` simple reemplaza el del rol, así que tiene que repetir cada condición; una directiva `remove:` no puede nombrar una heredada.
- Reescribir la descripción de una condición heredada, ni cambiar o dejar en blanco el `escalate_to` del rol. Un mapeo `escalation_rules` simple tiene que repetir `escalate_to`, porque omitirlo lo dejaría en blanco. No necesita repetir las descripciones del rol: un despliegue describe solo las condiciones que agrega, así que las descripciones del rol se conservan también bajo un mapeo simple. `descriptions` tiene que ser un mapeo; un `null` (una clave `descriptions:` sin valor), una lista o un texto lanza `DefinitionError` en lugar de reemplazar las descripciones del rol.
- Aflojar la redacción de auditoría: poner `audit_policy.capture_tool_input` en un valor verdadero que el rol no tiene, u omitir una clave que lista el `redact_keys` del rol. Un mapeo `audit_policy` simple reemplaza el del rol, así que tiene que conservarlas.
- Incrementar los límites de ejecución más allá de los valores por defecto de la plataforma.

Romper cualquiera de estas reglas lanza `DefinitionError`, que nombra el despliegue, el campo y el motivo, y el runtime no se construye. Un campo de política que el despliegue omite, o deja vacío, conserva el valor del rol.

### `skills/`

Las habilidades son módulos de prompts de comportamiento que definen cómo razona el agente sobre tareas específicas del dominio — codifican el vocabulario del cliente, las reglas de negocio y los patrones de interacción conversacional. El contenido de una habilidad se resuelve desde tres fuentes, revisadas en este orden:

1. **Contenido inline en Python** — un agente propio construido con `Agent(skill_contents={...})` (o `Agent.from_folder(path, skill_contents={...})`) provee el contenido de la habilidad directamente, sin leer ningún archivo.
2. **La carpeta propia del agente** — el `skills/<nombre>.md` de `Agent.from_folder(path)`, para un agente propio que trae su propio contenido por defecto.
3. **El despliegue** — `deployments/{cliente}/{rol}/skills/<nombre>.md`. Esta es la **única** fuente desde la que un rol predefinido de plataforma resuelve sus habilidades; un rol predefinido no tiene carpeta ni fuente inline propia.

Una habilidad declarada que no se resuelve en ninguna de las tres fuentes falla de forma explícita (`FactoryError`). Un agente propio puede combinar sus dos fuentes propias — por ejemplo, traer un contenido por defecto en su carpeta y dejar que un contenido inline (fuente 1) reemplace una habilidad puntual por nombre. La fuente de despliegue es exclusivamente un mecanismo de rol predefinido: combinar una sobreescritura de despliegue (`client`) con un agente propio de carpeta o inline (cualquier locator `Agent`/`Agent.from_folder`) lanza `DefinitionError` al resolver, ya que un agente propio no tiene un árbol de despliegue donde buscarla.

Las dos fuentes de archivo quedan contenidas: un archivo de habilidad tiene que resolverse, después de `..` y los enlaces simbólicos, dentro de su propia carpeta `skills/` y dentro de la raíz de esa carpeta (la raíz de importador para la fuente 2, la raíz de despliegues para la fuente 3). Un nombre de habilidad o de rol con `..`, o un enlace simbólico que sale, se trata como no encontrado. Ver `role.md`, "Las carpetas del importador no salen de su raíz".

---

## Directivas de mezcla en YAML (YAML Merge Directives)

El cargador de definiciones (`loader.py`) implementa un vocabulario preciso de directivas en YAML para regular cómo se mezclan las propiedades de la plantilla predefinida con las sobreescrituras del cliente. Esto evita redundancias y mantiene el control de versión limpio:

### 1. Comportamiento por defecto (Ausencia de campo)
Si un campo está definido en la plantilla predefinida (`platform/roles/`) pero está **completamente ausente** en la sobreescritura del despliegue (`deployments/{client}/`), el cargador lo hereda automáticamente sin modificaciones. Un mapeo de política vacío (`{}`) cuenta igual que uno ausente.

### 2. Heredar explícitamente (`inherit`)
Si un campo escalar se define con el valor literal `"inherit"` en la sobreescritura, toma el valor original del padre tal cual:
```yaml
permissions: inherit
```

### 3. Directivas para listas (List Directives)
`permissions` admite directivas estructuradas en lugar de una lista plana. `tools` y `skills` de un despliegue son siempre listas planas: el subconjunto que el despliegue conserva.
- **`add` (Agregar con deduplicación)**: Mantiene la lista del padre y le añade nuevos elementos, eliminando duplicados automáticamente y preservando el orden.
  ```yaml
  permissions:
    inherit: true
    add:
      - read:catalog
  ```
- **`remove` (Remover elementos)**: Mantiene la lista del padre pero filtra y elimina los elementos indicados.
  ```yaml
  permissions:
    inherit: true
    remove:
      - write:orders
  ```
- **`override` (Reemplazo total)**: Sobreescribe por completo la lista del padre por la nueva lista proporcionada (sujeto a las restricciones de los invariantes). Se puede declarar como una lista directa o usando la clave `override`:
  ```yaml
  permissions:
    override:
      - read:catalog
      - send:message
  ```

### 4. Directivas para diccionarios y mapeos (Mapping Directives)
Para campos estructurados como diccionarios (por ejemplo, `escalation_rules`, `delegation_policy`, `memory_policy` o `audit_policy`), se admite la mezcla incremental:
- **Mezcla con `inherit: true`**: Si el diccionario incluye `inherit: true`, el cargador toma todas las propiedades del padre como base y aplica selectivamente los campos declarados en el hijo. Solo en `escalation_rules`, `add` y `remove` editan su lista `conditions`:
  ```yaml
  escalation_rules:
    inherit: true
    add:
      - vip_complaint
    descriptions:
      vip_complaint: un cliente VIP presenta un reclamo.
  ```
- **Reemplazo absoluto**: Si el diccionario no contiene `inherit: true`, la sobreescritura reemplaza por completo la configuración del padre. En `escalation_rules` y `audit_policy` el resultado sigue sujeto al Invariante 5.

### 5. Directivas no reconocidas
Una directiva que el cargador no reconoce lanza `DefinitionError`: `{remove: [...]}` sin `inherit: true`, una clave mal escrita, `inherit: 'yes'`, `inherit` mezclado con `override`, o `add`/`remove` en un mapeo de política que no es `escalation_rules`. Una directiva así antes se resolvía al conjunto completo del padre, así que se conservaba un permiso que el despliegue quería quitar. La misma regla vale para los roles predefinidos y los agentes propios (`docs/platform_es/role.md`).

---

## Invariantes estructurales (Structural Invariants)

> **Regla de Oro de la Plataforma:** Un despliegue específico de cliente solo puede *restringir o especializar* el comportamiento. Jamás puede elevar capacidades, relajar límites ni saltarse controles de seguridad definidos en el rol predefinido.

Durante el proceso de mezcla (`merge`), el cargador de configuraciones (`loader.py`) valida de forma estricta los siguientes **cinco invariantes estructurales**. Si se viola cualquiera de ellos, el cargador aborta de inmediato levantando una excepción `DefinitionError`, bloqueando la instanciación del runtime:

### Invariante 1 — Restricción de Herramientas (`tools`)
Las herramientas declaradas por la sobreescritura del despliegue del cliente deben ser un subconjunto estricto de las herramientas declaradas en la plantilla predefinida del rol:
$$\text{set(override.tools)} \subseteq \text{set(parent.tools)}$$
Si la sobreescritura solicita una herramienta que no está aprobada en la plantilla predefinida del rol, la inyección falla.

### Invariante 2 — Restricción de Permisos (`permissions`)
Los permisos resueltos finales del despliegue deben ser un subconjunto de los permisos declarados en el rol predefinido:
$$\text{resolved\_permissions} \subseteq \text{parent.permissions}$$
Esto impide que una configuración de cliente intente elevar privilegios o solicitar accesos de seguridad no planificados para ese tipo de rol en la plataforma.

### Invariante 3 — Techo de Autonomía (`autonomy`)
El nivel de autonomía del despliegue debe ser menor o igual al nivel de autonomía declarado en el rol predefinido:
$$\text{autonomy\_rank(override)} \le \text{autonomy\_rank(parent)}$$
El cargador ordena los rangos de autonomía de menor a mayor (más restrictivo a más permisivo):
1. **`confirm` (Rango 0)** — El agente debe pedir confirmación humana antes de ejecutar cualquier herramienta.
2. **`supervised` (Rango 1)** — El agente actúa autónomamente en tareas seguras y escala en excepciones.
3. **`full` (Rango 2)** — El agente ejecuta libremente todas las herramientas de su superficie.

Si el rol predefinido está definido con autonomía `supervised`, un despliegue de cliente puede restringirlo a `confirm`, pero **jamás** elevarlo a `full`.

### Invariante 4 — Restricción de Límites de Ejecución (`execution_limits`)
Si la sobreescritura del cliente define límites de ejecución personalizados (`execution_limits`), cada valor numérico individual debe ser **más estricto o igual** (menor o igual) que el límite por defecto definido por el padre o por la plataforma:
$$\text{override\_limit} \le \text{platform\_default}$$
Esto aplica a:
- `tool_call_timeout_s` (por defecto: 10s)
- `total_execution_timeout_s` (por defecto: 60s)
- `max_tool_calls` (por defecto: 20)
- `max_delegation_depth` (por defecto: 2)
- `max_clarification_attempts` (por defecto: 3)

Por ejemplo, un cliente puede configurar `max_tool_calls: 10` para prevenir bucles de costes en su despliegue, pero si intenta poner `max_tool_calls: 30`, el cargador rechazará la configuración lanzando un `DefinitionError`.

Un valor `null` cuenta como el valor por defecto de la plataforma y se valida igual que cualquier otro. Un valor que no es un número finito y no negativo (`NaN`, infinito, un valor negativo, un booleano) también lanza `DefinitionError`.

### Invariante 5 — Escalamiento y redacción de auditoría (`escalation_rules`, `audit_policy`)
Las `escalation_rules` resueltas del despliegue conservan cada condición del rol predefinido, con su descripción y su `escalate_to`:
$$\text{parent.conditions} \subseteq \text{resolved.conditions}$$
Y el `audit_policy` resuelto no afloja la redacción: `capture_tool_input` no pasa a un valor verdadero que el rol no tiene, y `redact_keys` conserva cada clave del rol. Es el mismo techo que tiene un agente del importador sobre su padre (`docs/platform_es/role.md`).

---

## El algoritmo de mezcla del harness (*harness merge algorithm*)

```
function build_runtime(client, role_type, user_identity):
  generic = load_folder("platform/roles/{role_type}/")
  override = load_folder("deployments/{client}/{role_type}/")  # puede no existir

  if override is None:
    definition = generic
  else:
    definition = merge(generic, override)
    assert definition.tools ⊆ generic.tools  # Invariante 1: subconjunto de herramientas
    assert definition.permissions ⊆ generic.permissions  # Invariante 2: subconjunto de permisos
    assert definition.autonomy ≤ generic.autonomy  # Invariante 3: techo de autonomía
    assert definition.execution_limits ≤ generic.execution_limits  # Invariante 4: límites más estrictos
    assert generic.escalation_conditions ⊆ definition.escalation_conditions  # Invariante 5: escalamiento
    assert definition.audit_redaction ⊇ generic.audit_redaction  # Invariante 5: redacción de auditoría

  return AgentFactory.build(definition, user_identity)
```

Si no existe una sobreescritura de despliegue para un cliente y rol específicos, la plataforma utiliza la definición predefinida tal como está. Esto permite una especialización gradual: un cliente puede comenzar operando con el rol predefinido e ir agregando sobreescrituras de forma incremental a medida que se refinan sus necesidades. Este es el comportamiento propio de `resolve()`/`load_override()` cuando se llaman directamente (una aplicación que usa la librería sin un cliente propio); el arranque de `create_app` es más estricto cada vez que se nombra un cliente explícitamente — ver el punto sobre `clients` más abajo.

---

## Ejemplo: ACME sales-agent

### `platform/roles/sales-agent/role.md` (predefinido)
```
name: sales-agent
purpose: >
  Asistir a los clientes a realizar pedidos a través de una interfaz conversacional.
  Interpretar solicitudes de productos en lenguaje natural, contrastarlas con el
  catálogo disponible, confirmar el pedido y persistirlo.
scope: order-taking, catalog-lookup, order-confirmation
```

### `deployments/acme/sales-agent/role.md` (sobreescritura)
```
extends: platform/roles/sales-agent
company: a regional beverage distributor
language: es-AR (Español Rioplatense)
domain: distribución de cerveza y bebidas — Argentina
vocabulary:
  - "la rubia" → BrandA
  - "cajón" → cajón de 24 unidades retornables
  - "preventista" → vendedor de campo
  - "punto de venta" → comercio minorista cliente (kiosco, bar, almacén)
purpose_extension: >
  Gestionar pedidos de clientes minoristas registrados (puntos de venta) a
  través de la API de WhatsApp Business. Interpretar nombres y cantidades
  coloquiales de productos argentinos, contrastarlos con el catálogo de ACME
  mediante RAG, y persistir los pedidos confirmados en el sistema DeW / App Preventas.
```

---

## Servir: registrar runtimes con `create_app`

Una aplicación registra lo que sirve cuando construye la app (ADR-004). Cada id de runtime lo elegís vos, y es opaco: nada lo parsea buscando un rol o un despliegue.

```python
app = create_app(
    registry_factory=build_registry,
    roots=RootConfig(deployments_root=Path("deployments")),
    agents={
        "acme-sales": "sales-agent",  # un rol predefinido
        "triage-bot": Agent.from_folder("agents/triage-bot"),  # un agente propio
    },
    clients={"acme-sales": "acme"},  # deployments/acme/sales-agent/ lo acota
    grants={
        "acme-sales": ["read:catalog", "write:orders"],
        "triage-bot": ["read:catalog"],
    },
)
```

- **Cada id registrado se construye en el arranque.** Una entrada que no resuelve o no se equipa hace fallar todo el arranque; nunca se sirve un runtime mientras otro se descarta.
- **Un id** tiene de 1 a 64 letras, dígitos, `_` o `-`, y empieza con una letra o un dígito.
- **`clients`** vale solo para un rol predefinido registrado por nombre. Hace fallar el arranque para una entrada `Agent`, para un id que `agents` no registra, sin `agents`, y para un valor que no es un nombre de cliente (un `str` de letras, dígitos, `_` o `-`, que empieza con una letra o un dígito): un cliente nombra una sobreescritura sustractiva, y una ignorada en silencio serviría al agente sin su acotamiento. Un valor `None`, como una variable de entorno sin definir, no significa "sin cliente"; en ese caso hay que dejar el id fuera de `clients`. Una sobreescritura de cliente necesita un `RootConfig(deployments_root=...)` explícito. **Un cliente nombrado cuya carpeta de sobreescritura de despliegue no existe también hace fallar el arranque** — `deployments/{client}/{role_type}/` tiene que existir en cuanto `clients` lo nombra; una carpeta mal escrita o eliminada, si no, serviría el rol en silencio SIN su acotamiento, el mismo riesgo de ensanchamiento contra el que protege más abajo un grant faltante.
- **`grants`** es el grant explícito de despliegue, una lista por id registrado, y necesita `agents=` para tener contra qué mapearse — igual que `clients`, hace fallar el arranque sin `agents=`. Nada se otorga automáticamente: un id registrado sin entrada hace fallar el arranque, y también un string suelto en lugar de una lista o un id de `grants` que `agents` no registra. Sin `grants=`, la fuente es `DEPLOY_GRANTS`, con las mismas claves. Ver `docs/architecture_es/permission-model.md`.
- **Los canales buscan ids.** `WHATSAPP_RUNTIME_ID` y cada id de `ADAPTER_RUNTIMES` tienen que ser ids registrados, o el arranque falla nombrándolos. `/v1/models` lista solo los ids de `ADAPTER_RUNTIMES`, nunca todos los registrados.

### Sin `agents=`: `AGENT_REGISTRATIONS`

Sin `agents=`, `create_app` registra en su lugar las entradas de la variable de entorno `AGENT_REGISTRATIONS`, y las construye con las mismas reglas. Mapea cada id de runtime a un rol predefinido, como `"{role}"` o `"{role}@{client}"`:

```bash
AGENT_REGISTRATIONS='{"acme-sales": "sales-agent@acme", "support": "sales-agent"}'
ADAPTER_RUNTIMES='["acme-sales", "support"]'
DEPLOY_GRANTS='{"acme-sales": ["read:catalog", "write:orders"], "support": ["read:catalog"]}'
```

- **Un valor es estricto.** Como mucho un `@`; el rol y el cliente cumplen cada uno la regla de ids de arriba. Un id o un valor malformado hace fallar el arranque, nombrándolo junto con `AGENT_REGISTRATIONS`. `__` no significa nada en ningún lado: es parte del nombre.
- **`"{role}@{client}"`** es la entrada de `clients` de ese id, así que necesita un `RootConfig(deployments_root=...)` explícito. `"{role}"` solo significa sin sobreescritura de despliegue.
- **Roles predefinidos, o una carpeta.** Una variable de entorno no puede llevar un objeto `Agent`; un valor es el nombre de un rol predefinido, o `"folder:<name>"` (abajo). Cualquier otra cosa se registra con `agents=`.
- **`agents=` gana.** Cuando `create_app` recibe `agents=`, `AGENT_REGISTRATIONS` se ignora, nunca se mezcla.

### Servir una carpeta de agente: `AGENTS_ROOT`

Un valor de la forma `"folder:<name>"` registra la carpeta de agente `<name>` bajo el directorio que nombra `AGENTS_ROOT`, por ejemplo una copia de un [agente de referencia](../../examples/agents/README.md):

```bash
AGENTS_ROOT=/srv/agents
AGENT_REGISTRATIONS='{"support": "folder:support-agent"}'
ADAPTER_RUNTIMES='["support"]'
DEPLOY_GRANTS='{"support": ["read:catalog"]}'
```

`AGENTS_ROOT` lo fija el operador y un valor de registro no es de confianza, así que todo escape hace fallar el arranque, nombrando el id y `AGENT_REGISTRATIONS`:

- **Raíz sin definir o relativa.** Un valor de carpeta con `AGENTS_ROOT` sin definir (el valor por defecto) o que no es una ruta absoluta falla cerrado. Sin un valor de carpeta, `AGENTS_ROOT` no se lee.
- **Un solo nombre de carpeta.** `<name>` es una única carpeta directamente bajo la raíz, que cumple la regla de caracteres de los ids de runtime. Una ruta absoluta, un segmento `..`, un separador o un `@` falla. Un valor de carpeta no admite `@{client}`: las sobreescrituras de despliegue aplican solo a roles predefinidos.
- **Sin symlinks hacia afuera.** La carpeta debe existir y, tras seguir los symlinks, seguir estando estrictamente dentro de la raíz. Un archivo dentro de la carpeta que sea un symlink hacia afuera hace fallar la carga, como en cualquier carpeta de agente.
- **Los hermanos quedan adentro.** La raíz es la raíz del importador, así que un `extends: ../operator-agent` hermano dentro de ella funciona, y uno que sale de ella falla.

La carpeta queda sujeta al techo del importador como cualquier `Agent.from_folder`. El nombre de un rol predefinido sigue funcionando, con su aviso de obsolescencia.

### Límites de ejecución y respuesta por timeout

El despliegue fija los límites de cada runtime y el texto con el que responde un turno que se queda sin tiempo, con los mismos ids que `grants`:

```python
app = create_app(
    registry_factory=build_registry,
    agents={"reports": Agent.from_folder("agents/reports")},
    grants={"reports": ["read:catalog"]},
    execution_limits={
        "reports": {"total_execution_timeout_s": 240, "tool_call_timeout_s": 60}
    },
    timeout_reply={"reports": "Esto está tardando más de lo esperado. Probá de nuevo."},
    tool_error_reply={
        "reports": "No pude completar esto porque una herramienta falló. Probá de nuevo."
    },
)
```

Sin esos argumentos, la fuente son `EXECUTION_LIMITS`, `TURN_TIMEOUT_REPLY` y `TOOL_ERROR_REPLY`, con la misma forma:

```bash
EXECUTION_LIMITS='{"reports": {"total_execution_timeout_s": 240, "tool_call_timeout_s": 60}}'
TURN_TIMEOUT_REPLY='{"reports": "Esto está tardando más de lo esperado. Probá de nuevo."}'
TOOL_ERROR_REPLY='{"reports": "No pude completar esto porque una herramienta falló. Probá de nuevo."}'
```

- **Solo el despliegue sube.** Un valor reemplaza al defecto de la plataforma (60 s por turno, 10 s por llamada a herramienta, 20 llamadas) como techo de ese runtime; el agente puede ajustarlo hacia abajo, nunca superarlo.
- **El lease lo acota.** Un turno tiene que durar menos de 540 s: el lease de 600 s del outbox menos los 60 s de `LEASE_MARGIN_S` para el envío de la respuesta y el checkpointer. Un turno que no entra, una llamada a herramienta más larga que su turno (validada otra vez al combinarse con los límites del agente), una clave desconocida o un id que el arranque no construye hacen fallar el arranque, nombrando el runtime. El margen también cubre la lectura y el guardado del checkpointer, así que con un canal y checkpointer `CHECKPOINTER_TIMEOUT_S` puede ser de hasta 20 s.
- **La respuesta es texto plano**, de hasta 1000 caracteres, y se envía tal cual. Un runtime sin respuesta propia responde el texto por defecto en inglés.
- **Errores de herramienta** *(llega en el próximo release)*. Una herramienta que lanza una excepción llega al modelo como un resultado de error; `max_tool_errors` (por defecto 3, configurable como los demás límites) termina el turno con `tool_error_reply` cuando esa cantidad de llamadas lanzó una excepción o agotó su tiempo. Ver [Política: errores de herramienta](policy.md#errores-de-herramienta).

Ver [Política: límites de ejecución](policy.md#límites-de-ejecución-execution-limits).

### Reintentar mensajes fallidos y la respuesta de falla

Un mensaje de canal que falla en el worker diferido se reintenta solo cuando la falla puede resolverse sola. Cada reintento vuelve a correr el turno entero (llamadas al modelo, SQL, ejecución de código), así que una falla que se repite con la misma entrada no lo vale.

- **Las fallas transitorias se reintentan** con backoff, hasta `max_attempts` (por defecto 5): límites de tasa del proveedor (429), 5xx y timeouts, errores de red y de conexión, errores de conexión a la base de datos, `TimeoutError` y `ConnectionError`. Una excepción levantada `from` una de estas también cuenta.
- **Todo lo demás es permanente** y se ejecuta una sola vez: una excepción de una herramienta o de la lógica del turno, una entrada inválida, un aborto de seguridad. (Con los errores de herramienta devueltos al modelo, lo que queda aquí son sobre todo bugs y abortos.)
- **Se avisa al usuario una vez.** Cuando un mensaje falla definitivamente, sea permanente o sin reintentos, el usuario recibe una respuesta de respaldo por el mismo canal. La respuesta se confirma junto con el estado terminal y se envía después, de modo que una caída en el medio pierde el aviso y nunca lo repite; un envío fallido se audita y no se reintenta. No se envía si la fila ya tiene una respuesta (un turno con timeout responde el texto de timeout como una respuesta normal).
- **Queda visible.** El evento de auditoría terminal `webhook_delivery_terminal_failure` lleva `failure_class`, `permanent` y `fallback_reply`; `webhook_fallback_reply` registra si el aviso fue `delivered`. Una falla permanente se registra en el log (`webhook_worker.permanent_failure`) y se cuenta en `outbox_permanent_failures_total{channel, exception, reason}`, solo con el **nombre de la clase** de la excepción, nunca su mensaje.

```python
from agents_system.services.retry_policy import OutboxRetryPolicy

app = create_app(
    ...,
    outbox_retry={
        "reports": OutboxRetryPolicy(
            max_attempts=3,
            fallback_reply="Algo salió mal con tu mensaje. Probá de nuevo.",
        ),
        # solo se reintentan estas excepciones; con include_default=True se conserva la lista integrada
        "billing": OutboxRetryPolicy.retrying(MyBackendUnavailable, max_attempts=4),
    },
)
```

Sin una entrada, un runtime conserva los valores por defecto (5 intentos, el clasificador integrado, una respuesta de respaldo en inglés). `is_transient` acepta cualquier predicado sobre la excepción. El entorno lleva los dos valores simples, por id de runtime; una política en `create_app` los reemplaza por completo:

```bash
OUTBOX_MAX_ATTEMPTS='{"reports": 3}'
OUTBOX_FALLBACK_REPLY='{"reports": "Algo salió mal con tu mensaje. Probá de nuevo."}'
```

- **La respuesta es texto plano**, de hasta 1000 caracteres, enviada tal cual y nunca con plantillas.
- **El arranque falla** ante una política que nombra un runtime que no construye, o ante un valor inválido, nombrando el runtime.
- **Una caída no está cubierta.** Una fila cuyo worker murió en el último intento se cierra en el siguiente claim con el evento de auditoría terminal pero sin respuesta: la plataforma no conoce ahí la dirección del usuario.

### Contexto por solicitud para la API compatible con OpenAI

Un turno de canal puede llevar contexto determinista (el canal, la fecha de hoy en la zona horaria del despliegue, reglas de formato de salida) porque lo construye la aplicación. Un turno de `/v1/chat/completions` lo construye el adaptador, así que la aplicación pasa un hook:

```python
from datetime import datetime
from zoneinfo import ZoneInfo

from agents_system.integration.request_context import AdapterRequestInfo


async def request_context(info: AdapterRequestInfo) -> str | None:
    today = datetime.now(ZoneInfo("America/New_York")).date()
    return f"Channel: {info.channel}. Today is {today.isoformat()}."


app = create_app(
    ...,
    request_context_hook=request_context,
    adapter_channel="web-chat",  # por defecto "openai-adapter"
)
```

- **Entrada: solo metadatos de la plataforma.** El adaptador espera el hook una vez por solicitud con un `AdapterRequestInfo` inmutable: `runtime_id` (el modelo solicitado), `channel` (la etiqueta `adapter_channel`), `request_id` (el encabezado de respuesta `x-request-id`), `session_id` (el del turno) y `subject` (el usuario final que resolvió un `SubjectResolver`, o `None`; ver [Alcance por usuario final](end-user-scope.md)). Nada del cuerpo de la solicitud llega al hook.
- **Salida: contexto confiable en una posición fija.** El texto que devuelve se agrega al mensaje de sistema, después del prompt propio del agente, bajo un encabezado fijo `## Context for this request`. El prompt del agente queda primero y conserva la prioridad; el contexto nunca lo reemplaza. Los mensajes `system` del cliente se siguen descartando. `None` o un texto en blanco no agregan nada.
- **Confiable frente a no confiable.** La salida del hook es confiable: la construye la aplicación, por eso va en el mensaje de sistema. El contenido del usuario sigue siendo no confiable, igual que cualquier dato almacenado o externo. No copie texto de la solicitud, el valor de un encabezado ni una fila de base de datos en la salida del hook; pase ese tipo de datos como un bloque de datos no confiables (`run_turn(untrusted_data=...)`).
- **Nunca se persiste.** Como el prompt de sistema, el contexto se agrega al llamar al modelo y nunca se guarda en el estado de la conversación. Cuenta contra `max_context_tokens`.
- **Local a la solicitud.** Cada solicitud construye su propio contexto y lo pasa a su propio turno; las solicitudes concurrentes nunca ven el de otra.
- **Falla cerrada.** Un hook que levanta una excepción, o que devuelve algo que no sea texto o `None`, hace fallar la solicitud con un `500` (`The request context could not be built.`), registra `adapter.request_context_failed` con el stack y no corre ningún turno. El error del hook nunca llega al cliente. El hook corre antes de que el turno ocupe un lugar de capacidad.
- **Sin hook, sin cambios.** Sin `request_context_hook`, el adaptador llama al runtime exactamente como antes.

Quien usa la biblioteca y corre los turnos por su cuenta pasa el mismo texto directamente: `run_turn(..., trusted_context="...")`.

### Archivos que produce un turno, por la API compatible con OpenAI

Un turno de canal puede entregar los archivos que produjo (una planilla, un gráfico) porque la aplicación los envía por el propio canal. Chat completions no tiene un campo para archivos del asistente, así que el adaptador responde solo texto salvo que la aplicación le entregue los archivos:

```python
from agents_system.integration.output_files import (
    LocalDirectoryFileStore,
    OutputFile,
    OutputFiles,
)
from agents_system.integration.request_context import AdapterRequestInfo


async def turn_files(info: AdapterRequestInfo, messages) -> list[OutputFile]:
    # Buscar lo que este turno escribió en el espacio de trabajo de este usuario.
    return [
        OutputFile(filename="sales.csv", mime_type="text/csv", path=...),
        OutputFile(
            filename="chart.png",
            mime_type="image/png",
            data=png,
            caption="Sales by region",
        ),
    ]


app = create_app(
    ...,
    output_files=OutputFiles(
        hook=turn_files,
        store=LocalDirectoryFileStore("/var/lib/my-app/output-files"),
        inline_image_max_bytes=256 * 1024,  # el valor por defecto
        max_file_bytes=20 * 1024 * 1024,  # el valor por defecto
        ttl_s=3600,  # el valor por defecto
        public_base_url="https://chat.example.com",  # opcional
    ),
)
```

- **El hook corre después del turno.** El adaptador lo espera una vez por solicitud, cuando el turno terminó y liberó su lugar de capacidad, con el mismo `AdapterRequestInfo` que recibe el [hook de contexto por solicitud](#contexto-por-solicitud-para-la-api-compatible-con-openai) (así `info.subject` indica de quién fue el turno) y los mensajes del turno. Devuelve objetos `OutputFile`: `filename`, `mime_type` (un `type/subtype` sin parámetros), exactamente uno de `data` (bytes) o `path`, y un `caption` opcional. `None` o una lista vacía no agregan nada.
- **Se muestran como Markdown que el cliente ya renderiza.** Cada archivo se agrega al `content` del asistente, después del texto, uno por párrafo. Una imagen de hasta `inline_image_max_bytes` se vuelve `![caption](data:image/png;base64,...)`. Una imagen más grande se vuelve una imagen Markdown que apunta a una URL de descarga, y cualquier otro archivo un enlace, `[sales.csv](https://.../v1/files/<token>)`, seguido de su caption. Los nombres de archivo y los captions se escapan, así que no pueden romper el Markdown.
- **Las URL de descarga son enlaces de capacidad sin bearer.** `GET /v1/files/{token}` sirve el archivo sin la clave bearer del adaptador, para que un navegador pueda abrirlo desde el chat. El token es la única autoridad: 43 caracteres seguros para URL generados con `secrets`, guardados solo como su hash SHA-256, sin ninguna ruta que los liste. Mantenga el TTL corto: quien vea un enlace (una captura compartida, un mensaje reenviado, un log de un proxy o de acceso) puede descargar el archivo hasta que venza. La URL se arma con la URL base de la solicitud; configure `public_base_url` cuando el adaptador está detrás de un proxy que la reescribe.
- **Vencido, desconocido o malformado: el mismo 404.** Un enlace deja de funcionar `ttl_s` segundos después de la respuesta. Cada fallo responde el mismo `404 Not Found` que responde una ruta inexistente, así que un sondeo no puede saber si un archivo existió alguna vez. La descarga se sirve como adjunto con su tipo MIME guardado, un nombre de archivo saneado, `X-Content-Type-Options: nosniff`, `Cache-Control: private, no-store` y una `Content-Security-Policy` con sandbox.
- **Límites.** Un archivo que supera `max_file_bytes` queda afuera y se registra como `adapter.output_file_skipped` (`reason="too_large"`, su `size` y el `limit`); los demás archivos y el texto se responden igual. Un archivo que el adaptador no puede leer, o que el almacenamiento rechaza, se omite de la misma forma.
- **Un hook que falla responde el texto.** Un hook que levanta una excepción, o que devuelve algo que no sea una lista de `OutputFile`, registra `adapter.output_files_failed` con el stack y la respuesta lleva solo el texto. Esto difiere a propósito del hook de contexto: el turno ya corrió y su costo ya se pagó, así que hacer fallar la solicitud descartaría una respuesta válida.
- **El almacenamiento es un puerto.** `LocalDirectoryFileStore` guarda cada archivo con un nombre elegido por el servidor en un directorio (nunca el nombre que da el cliente), lo borra al consultarlo si ya venció y barre los vencidos como mucho una vez por minuto al guardar. Varios procesos pueden compartir el directorio. Otro backend implementa `OutputFileStore`: `put` devuelve un token nuevo, `get` devuelve el archivo o `None`, `cleanup` borra los vencidos.
- **Las imágenes inline no llegan dos veces al modelo.** Un cliente reenvía todo el historial, imágenes inline incluidas. Con `output_files` configurado, el adaptador reemplaza cada imagen `data:` inline de un mensaje anterior del asistente por `[image: <caption>]` antes de que la vea el modelo, así el base64 nunca cuenta contra el contexto.
- **Local a la solicitud.** Cada solicitud pasa su propio `info` y sus propios mensajes al hook y renderiza sus propios archivos; las solicitudes concurrentes nunca ven los de otra.
- **Sin configuración, sin cambios.** Sin `output_files`, las respuestas son idénticas byte a byte a las de antes y `/v1/files/{token}` no se monta.

### Migrar desde los ids de runtime viejos

Antes de ADR-004, el arranque desde Settings leía el rol y el despliegue del propio id de runtime: `acme__sales-agent`, o `_generic__sales-agent` sin despliegue. Ese esquema ya no existe. Un id de runtime es solo una clave, y un despliegue que sigue configurado a la vieja usanza falla al arrancar, nombrando el id no registrado y señalando `AGENT_REGISTRATIONS`. Para migrar:

1. **Elegí un id para cada runtime.** Conservar el string viejo sirve solo si es un id válido: `acme__sales-agent` lo es (ahora es solo un nombre), `_generic__sales-agent` no (un id no puede empezar con `_`).
2. **Registralo** en `AGENT_REGISTRATIONS`: lo que era `acme__sales-agent` pasa a `"sales-agent@acme"`, lo que era `_generic__sales-agent` pasa a `"sales-agent"`.
3. **Renombrá el id en todos lados donde se usa**: `ADAPTER_RUNTIMES`, `WHATSAPP_RUNTIME_ID`, y cada clave de `DEPLOY_GRANTS`. Una clave vieja de `DEPLOY_GRANTS` nunca se asocia a un id renombrado: el runtime falla al arrancar por no tener grant.
4. **Actualizá los clientes compatibles con OpenAI** (por ejemplo Open WebUI): mandan el id como `model`, y `/v1/models` ahora lista los ids nuevos.

| Antes | Después |
|---|---|
| `ADAPTER_RUNTIMES='["_generic__sales-agent"]'` | `AGENT_REGISTRATIONS='{"sales": "sales-agent"}'` y `ADAPTER_RUNTIMES='["sales"]'` |
| `WHATSAPP_RUNTIME_ID=acme__sales-agent` | `AGENT_REGISTRATIONS='{"acme-sales": "sales-agent@acme"}'` y `WHATSAPP_RUNTIME_ID=acme-sales` |
| `DEPLOY_GRANTS='{"acme__sales-agent": [...]}'` | `DEPLOY_GRANTS='{"acme-sales": [...]}'` |

`MODEL_PRICES` no necesita cambios: su clave es el id de modelo del proveedor, nunca un id de runtime. `agents_system.integration.openai_adapter.to_model_id` y `parse_model_id` se borran junto con el esquema, sin reemplazo: el código que los importa falla al importar. Registrá con `create_app(agents=...)` o con `AGENT_REGISTRATIONS`.

---

## Delimitación de memoria en despliegues (*Memory scoping*)

La capa de memoria sigue el mismo esquema de delimitación de dos niveles:

| Alcance / Scope | Qué almacena |
|---|---|
| `platform / {role_type}` | Comportamiento predefinido del agente aprendido a lo largo del tiempo en todos los despliegues. |
| `deployment / {client} / {role_type}` | Conocimiento específico del cliente (patrones de catálogo, preferencias corporativas). |
| `deployment / {client} / {role_type} / {user_id}` | Memoria individual del usuario (historial de pedidos, preferencias personales, notas de entrega). |

La memoria de un despliegue específico siempre se encuentra aislada de otros despliegues. Jamás se permite el acceso a memoria cruzada entre diferentes clientes.

---

## Referencias cruzadas

- Esquema de definición de roles: `docs/platform_es/role.md`
- Esquema de políticas y control: `docs/platform_es/policy.md`
- Inyección de capacidades y límites en el harness: `docs/platform_es/harness.md`
- Modelo de permisos y RBAC: `docs/architecture/permission-model.md`
- Alcance de entrega de ACME: `docs/delivery/acme-seller-ai.md`
