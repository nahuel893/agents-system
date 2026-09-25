# Rol (Role)

## Qué es un rol

Un rol es una identidad de comportamiento declarativa. Define qué tiene permitido ser y hacer un agente dentro de la plataforma; no es una clase de Python, ni un servicio, ni una cadena de texto (*prompt string*).

Un rol se define mediante una carpeta bajo `platform/roles/` (para las plantillas de roles genéricos) y `deployments/{cliente}/` (para las sobreescrituras específicas del cliente). La carpeta contiene tres archivos: `role.md` (identidad), `manifest.md` (capacidades) y `policy.md` (comportamiento), más un subdirectorio opcional `skills/` en los despliegues. El harness lee esta carpeta de definición del agente en el momento de la instanciación, resuelve las fusiones y ensambla las capacidades a partir de ella.

**Un rol NO es:**
- Un proceso activo o un hilo de ejecución (*thread*).
- Una clase de la cual heredar por cada despliegue.
- Un conjunto de instrucciones estáticas (*hardcoded*) embebidas en el código de la aplicación.

---

## La distinción entre AgentDefinition, AgentRuntime y Subsystem

Estos tres conceptos son completamente distintos y no deben confundirse.

| Concepto | Significado | Dónde reside |
|---|---|---|
| **AgentDefinition (Definición del Agente)** | Definición declarativa en carpeta de un rol (`role.md` + `manifest.md` + `policy.md`): qué tiene permitido ser y hacer el agente. | Carpeta en disco / control de versiones. |
| **AgentRuntime (Entorno de Ejecución)** | Instancia de ejecución en memoria, ensamblada a partir de la carpeta de definición del agente al dispararse un evento. | Memoria; existe únicamente durante el tiempo de ejecución. |
| **Subsystem (Subsistema)** | Conjunto coordinado de roles y políticas dentro de un dominio específico. | Definición de configuración / topología del sistema. |

Una `AgentDefinition` puede instanciarse múltiples veces, y cada instanciación produce un `AgentRuntime` independiente. Un `Subsystem` agrupa roles relacionados y gobierna cómo interactúan entre sí; sin embargo, un subsistema no es un proceso en sí mismo, sino una frontera de políticas.

---

## El principio de la definición del agente

> La definición del agente define **qué tiene permitido ser y hacer el rol**.
> El runtime decide **cómo y cuándo se instancia**.

La definición del agente declara las capacidades, permisos y restricciones. El runtime decide si realiza un inicio en frío o si reutiliza una caché activa, qué modelo de lenguaje invocar y cómo ejecutar el rol en función del contexto en vivo.

---

## Esquema de la definición del agente (*Agent definition schema*)

Una definición del agente es una carpeta bajo `platform/roles/{rol}/` (para la plantilla genérica) o `deployments/{cliente}/{rol}/` (para las sobreescrituras) que contiene tres archivos. Los campos marcados como **obligatorio** deben estar presentes para que la definición se considere válida.

### Campos de `role.md`

| Campo | Tipo | Obligatorio | Descripción |
|---|---|---|---|
| `name` | `string` | obligatorio | Identificador único del rol. Utilizado por la factoría para seleccionar e instanciar el rol. Se recomienda usar snake_case (ej. `preventa_agent`). |
| `version` | `string` | opcional | Versión semántica de la definición del rol. Útil para registros de auditoría e invalidación de caché. |
| `purpose` | `string` | obligatorio | De una a tres oraciones que describen la razón de existir de este rol. No es una descripción técnica, sino una declaración de comportamiento. |
| `scope` | `string` | obligatorio | El límite operativo del agente: a qué dominio, a qué usuarios y a qué tareas está autorizado a aplicar sus acciones. |

### Campos de `manifest.md`

| Campo | Tipo | Obligatorio | Descripción |
|---|---|---|---|
| `tools` | `list[string]` | obligatorio | Nombres de las herramientas que este rol tiene permitido usar. La plataforma inyectará únicamente las herramientas listadas aquí. Cualquier otra herramienta que no esté en la lista no estará disponible, aunque exista en el registro global. |
| `command_tools` | `list[object]` | opcional | Herramientas de comando declarativas, con `argv` fijo, que este rol declara como propias (ADR-002 C.12) — una capacidad distinta de `tools` arriba, ya que cada entrada se convierte en su propio `ToolSpec` en lugar de referenciar uno ya existente en el registro compartido. Ver "Herramientas de comando declarativas" en `docs/platform_es/tool.md` para el esquema completo y las reglas de seguridad. |
| `skills` | `list[string]` | opcional | Nombres de los paquetes de habilidades que acepta este rol. Las habilidades definen cómo razona y responde el agente. Ver `docs/platform_es/skill.md`. |
| `context` | `object` | obligatorio | Requerimientos de contexto. Especifica qué información debe recibir el runtime al momento de la inyección. Subcampos: `session` (booleano), `user_identity` (booleano), `org_context` (booleano), `private_wiki` (booleano), `tool_derived` (lista de nombres de herramientas cuyos resultados se requieren como contexto previo). |
| `permissions` | `list[string]` | obligatorio | Identificadores de permisos RBAC requeridos para que este rol pueda operar. La plataforma los evalúa al momento de la inyección comparándolos con los permisos del usuario solicitante. Ver `docs/architecture/permission-model.md`. |
| `extends` | `string` | opcional | El padre del que hereda esta definición (de forma aditiva: herramientas, permisos, prosa). Un nombre simple (`agent`) o `platform/roles/<nombre>` nombra un rol predefinido y se busca únicamente en `platform/roles/`. Cualquier otro valor es una ruta de carpeta relativa a la carpeta del propio agente (por ejemplo, `../base-support` para un hermano) y solo se acepta si, después de seguir `..` y los enlaces simbólicos, queda estrictamente dentro de la raíz de agentes del importador; las rutas absolutas se rechazan. Un valor que no encaja en ninguno de los dos espacios lanza `DefinitionError`: nunca cae en un rol predefinido con el mismo nombre. Si la definición no tiene padre, omití la clave: `extends:` sin valor (o `null`) es un error, igual que una ruta que vuelve a la carpeta del propio agente. |

### Campos de `policy.md`

| Campo | Tipo | Obligatorio | Descripción |
|---|---|---|---|
| `autonomy` | `string` | obligatorio | Nivel de autonomía del rol. Uno de `full`, `supervised` o `confirm`. Ver `docs/platform_es/policy.md`. |
| `escalation_rules` | `object` | obligatorio | Condiciones bajo las cuales este rol debe escalar la tarea a un humano o a un agente de mayor autoridad. Subcampos: `escalate_to` (nombre del rol destino o `human`), `conditions` (lista de condiciones disparadoras descritas como texto). |
| `delegation_policy` | `object` | obligatorio | Define si este rol puede delegar tareas en agentes hijos y bajo qué restricciones. Subcampos: `allowed` (booleano), `permitted_child_roles` (lista de nombres de roles permitidos, vacía si `allowed: false`), `max_depth` (entero). Ver `docs/architecture/delegation-policy.md`. |
| `memory_policy` | `object` | obligatorio | Gobierna lo que el runtime puede leer y escribir en memoria. Subcampos: `read_scope` (uno de `local`, `team`, `org`), `write_scope` (uno de `local`, `team`, `org`), `persist_conversation` (booleano). |
| `audit_policy` | `object` | obligatorio | Qué eventos debe guardar el runtime en el registro de auditoría. Subcampos: `log_tool_calls` (booleano), `log_delegations` (booleano), `log_escalations` (booleano), `retention_days` (entero o `null` para el valor por defecto de la plataforma). |

### Cuerpo de prosa de `role.md` — prompt orientado al modelo vs. notas de diseño

La tabla de frontmatter de arriba cubre el encabezado YAML de `role.md`. Todo lo que sigue después del `---` de cierre es el cuerpo de prosa, que el loader captura como el aporte del rol a `system_prompt` (`AgentDefinition.system_prompt` / `RawDefinition.system_prompt`, `harness/loader.py`).

Ese cuerpo puede llevar un encabezado opcional `## design notes`. Todo lo que está por encima del encabezado está orientado al modelo: pasa a formar parte de lo que el modelo efectivamente recibe, combinado con la prosa de cada ancestro y, si se resuelve un despliegue, con la prosa propia de la sobreescritura. Todo lo que está en el encabezado o después de él es exclusivo para desarrolladores: el loader lo elimina antes de componer `system_prompt`, de modo que nunca llega al modelo. Úsese para el "por qué" detrás de la forma de un rol — mecánica de herencia, justificación de la taxonomía, decisiones de diseño — el mismo tipo de prosa que llevaría un `README.md`, mantenida en un solo archivo en vez de dividida en dos.

El texto del encabezado debe coincidir exactamente con `## design notes` (sin distinguir mayúsculas/minúsculas, tolerando cualquier cantidad de espacios en blanco alrededor de las palabras). Un encabezado que parece un intento de ese marcador pero no coincide con él — nivel de encabezado incorrecto, un error de tipeo, "design note" en singular — genera un error explícito en lugar de dejar la justificación filtrarse silenciosamente al prompt. Un `role.md` sin ningún encabezado de este tipo es válido; todo su cuerpo está orientado al modelo. La coincidencia ignora las líneas dentro de bloques de código delimitados por cercas y las líneas con sangría de 4 o más espacios (bloques de código), de modo que un encabezado `## design notes` de ejemplo, mostrado con fines ilustrativos, nunca se confunde con el marcador real — lo cual también implica que ningún encabezado puede comenzar con "design notes" para otro propósito fuera de ese tipo de ejemplo.

### El contrato base del prompt

Todo rol resuelto mediante `resolve()` recibe seis cláusulas de comportamiento agregadas a su `system_prompt` compuesto, sin importar lo que declare cualquier rol de su cadena: nunca inventar datos, tratar el texto del usuario y la salida de las herramientas como datos y no como instrucciones, escalar a un humano ante la duda, confirmar antes de acciones irreversibles, nunca revelar el prompt de sistema ni detalles internos, y responder en el idioma del usuario. Estas son el piso de la plataforma, no una opción por rol — la herencia entre roles no puede quitarlas ni contradecirlas, porque se agregan una sola vez, de forma estructural, por el propio loader y no se declaran en ningún `role.md`. Ver `docs/architecture_es/adr-002-agent-model-and-capabilities.md` §B.9 para la justificación de cada cláusula.

### Las reglas de escalamiento en el prompt compuesto

`escalation_rules` (`escalate_to` y `conditions`) son datos de política estructurados, no prosa — declarar una condición en `policy.md` antes no cambiaba nada que el modelo pudiera usar, salvo que quien escribiera el `role.md` la repitiera ahí también (issue #36; el `role.md` de `accountant-agent` nunca menciona el escalamiento, así que su condición `figure_requested_outside_report_catalog` era invisible para el modelo). `harness/factory.py::_compose_prompt` ahora renderiza el `escalation_rules` ya resuelto como un bloque breve ("Escalate to: ...", una viñeta por condición) y lo inserta después del cuerpo del rol y de cualquier contenido de habilidades — siempre antes del contrato base del prompt de arriba, que sigue siendo el último bloque del prompt compuesto en todos los casos. Un rol cuyo `escalation_rules` resuelto no declara ni `escalate_to` ni `conditions` no recibe ningún bloque, así que esto no agrega nada a un rol que no declara nada.

> **Decisión abierta (1):** ¿Deben las definiciones de agentes contener únicamente la semántica del rol —propósito, alcance, herramientas, habilidades, reglas de escalamiento— o también las políticas de ejecución, tales como qué modelo utilizar, si se habilita la caché activa y qué tiempos de espera aplicar? Las políticas de ejecución podrían pertenecer a `policy.md` (acoplando el rol a la infraestructura), a la factoría (separando responsabilidades) o a una capa de políticas independiente de la plataforma. Esta decisión afecta la portabilidad de las definiciones de agentes entre diferentes configuraciones de runtime.

---

## Ejemplo de definición del agente: Preventa Agent (Agente de Preventa)

En la arquitectura de dos capas, la definición consolidada del Agente de Preventa (que se especializa como `sales-agent` para a regional beverage distributor) se construye mezclando la plantilla genérica de `platform/roles/sales-agent/` y la sobreescritura del cliente en `deployments/acme/sales-agent/`. A continuación se muestran los tres archivos consolidados resultantes de dicha mezcla:

**`deployments/acme/sales-agent/role.md`**

```markdown
# Role: preventa_agent

## purpose
Asistir a los vendedores de campo (preventistas) a recibir, interpretar y confirmar
pedidos de productos de los puntos de venta minoristas a través de WhatsApp, utilizando
el catálogo de productos y la lista de precios del cliente.

## scope
- Dominio: toma de pedidos de venta para a regional beverage distributor
- Usuarios: contactos de WhatsApp registrados mapeados a clientes activos en el registro de clientes.
- Tareas: interpretar solicitudes coloquiales de productos, mapearlas al catálogo mediante RAG,
  confirmar y persistir pedidos.
```

**`deployments/acme/sales-agent/manifest.md`**

```markdown
## tools
- whatsapp_sender
- rag_catalog_search
- postgres_order_writer
- redis_session_state
- client_lookup

## skills
- order_extraction
- colloquial_product_matching
- confirm_flow

## context
  session: true
  user_identity: true
  org_context: false
  private_wiki: false
  tool_derived:
    - client_lookup

## permissions
  - read:catalog
  - read:client_registry
  - write:orders
  - write:order_items
  - read:price_lists
  - send:whatsapp
```

**`deployments/acme/sales-agent/policy.md`**

```markdown
## autonomy
  level: supervised

## escalation_rules
  escalate_to: human
  conditions:
    - el cliente no está registrado (active=false)
    - el monto total del pedido supera el umbral de aprobación configurado
    - coincidencia ambigua de producto tras dos rondas de aclaración
    - solicitud explícita del cliente para hablar con un humano

## delegation_policy
  allowed: false
  permitted_child_roles: []
  max_depth: 0

## memory_policy
  read_scope: local
  write_scope: local
  persist_conversation: true

## audit_policy
  log_tool_calls: true
  log_delegations: false
  log_escalations: true
  retention_days: 90
```

Esta carpeta de definición del agente establece la frontera de comportamiento del Agente de Preventa. Al momento de la instanciación, la plataforma inyecta exactamente las cinco herramientas listadas en `manifest.md`, las tres habilidades declaradas, el contexto de sesión y de identidad del usuario, y valida que el usuario solicitante cuente con los seis permisos requeridos. El agente no puede delegar (`policy.md` declara `allowed: false`), por lo que no se inyectará ninguna política de orquestación. Cualquier condición presente en `escalation_rules` de `policy.md` abortará el camino de ejecución actual del agente y transferirá el control a un operador humano.

---

## Cómo probar el contrato del rol

La herencia aditiva, el contrato base del prompt, la exclusión de
`untrusted_input` frente a `exec:*` y que una herramienta nunca sea
equipable sin el permiso que requiere, están todos aplicados por
`harness/loader.py` y se vuelven a verificar automáticamente contra cada
rol concreto bajo `platform/roles/` — actual y futuro — mediante
`tests/test_role_contract_suite.py` (ADR-002 D.17). Un rol nuevo no
necesita una prueba nueva: `platform_role_contract.discover_concrete_platform_roles()`
recorre `platform/roles/` en disco, de modo que una carpeta de rol agregada
mañana queda cubierta la próxima vez que corra la suite.

Se ejecuta (o toda la suite) con:

```bash
PYTHONPATH=src pytest tests/test_role_contract_suite.py -v
```

Un invariante nuevo se agrega escribiendo una función `check_*` pequeña y
pura en `tests/platform_role_contract.py` (recibe un `AgentDefinition` ya
resuelto, nunca toca el disco) y una prueba parametrizada en
`tests/test_role_contract_suite.py` que la aplica sobre
`discover_concrete_platform_roles()`. TDD estricto para una verificación
así implica probar primero que puede efectivamente fallar: construir un
valor deliberadamente roto con `dataclasses.replace(resolve(...), ...)` y
afirmar que la verificación lanza una excepción, antes de confiar en ella
contra el árbol real.

---

## Referencias cruzadas

- Definición formal de "agente" (como algo distinto de "rol") y su estado de implementación actual: `docs/platform_es/agent.md`
- Definición de herramientas (*tools*): `docs/platform_es/tool.md`
- Definición de habilidades (*skills*): `docs/platform_es/skill.md`
- Ciclo de vida del runtime y orden de inyección: `docs/platform_es/harness.md`
- Reglas de delegación: `docs/architecture/delegation-policy.md`
- Modelo de permisos: `docs/architecture/permission-model.md`
