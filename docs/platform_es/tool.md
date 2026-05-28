# Herramienta (Tool)

## Qué es una herramienta

Una herramienta es un conector con un sistema externo o una capacidad ejecutable que el runtime de un agente puede invocar durante su ejecución. Las herramientas son el mecanismo mediante el cual los agentes interactúan con el mundo exterior al contexto del modelo: leyendo datos, escribiendo registros, enviando mensajes o realizando consultas sobre almacenes de información.

Las herramientas son elementos discretos, nombrados y registrados en la plataforma. Nunca están disponibles de forma predeterminada para un runtime; deben declararse explícitamente en el `manifest.md` del agente y ser inyectadas por el Capability Injector.

---

## Herramienta frente a Habilidad (*Tool vs. Skill*)

Estos dos conceptos son completamente distintos y no deben confundirse.

| Concepto | Qué hace | Ejemplo |
|---|---|---|
| **Herramienta (Tool)** | Un conector ejecutable con un sistema o capacidad externa. | `whatsapp_sender` envía un mensaje a través de la API de Meta. |
| **Habilidad (Skill)** | Un paquete de comportamiento o prompt que define cómo razona el agente. | `colloquial_product_matching` enseña al agente cómo interpretar referencias informales de productos. |

Una herramienta *hace algo*. Una habilidad *define cómo piensa el agente antes de hacer algo*. Una herramienta produce efectos secundarios o devuelve datos reales del entorno. Una habilidad no tiene efectos secundarios: influye en el proceso de razonamiento del modelo a través de módulos de prompts y requerimientos de contexto inyectados.

---

## Esquema de definición de herramientas (*Tool definition schema*)

| Campo | Tipo | Obligatorio | Descripción |
|---|---|---|---|
| `name` | `string` | obligatorio | Identificador único de la herramienta. Utilizado por los manifiestos y el pipeline de inyección para referenciarla. En snake_case (ej. `rag_catalog_search`). |
| `description` | `string` | obligatorio | Describe detalladamente qué hace la herramienta. El runtime del agente lee esta descripción para seleccionar e invocar la herramienta de forma correcta. Debe ser precisa y libre de ambigüedades. |
| `connector` | `string` | obligatorio | El sistema externo o servicio con el que se conecta esta herramienta. Ejemplos: `meta_whatsapp_api`, `postgres`, `redis`, `slack`. |
| `required_permissions` | `list[string]` | obligatorio | Identificadores de permisos RBAC que deben estar presentes en el conjunto de permisos del agente solicitante antes de que la herramienta pueda inyectarse. Si el agente carece de alguno de estos permisos, no recibirá la herramienta. |
| `inputs` | `object` | obligatorio | Parámetros de entrada que acepta la herramienta. Cada entrada contiene: `name` (string), `type` (string), `required` (booleano), `description` (string). |
| `outputs` | `object` | obligatorio | Estructura de los datos que devuelve la herramienta al ejecutarse con éxito. Cada entrada contiene: `name` (string), `type` (string), `description` (string). |
| `error_handling` | `object` | obligatorio | Define el comportamiento de la herramienta en caso de fallo. Subcampos: `on_connector_unavailable` (uno de `fail_open` (ignorar y continuar), `fail_closed` (bloquear), `escalate` (escalar)), `on_permission_denied` (uno de `fail_closed`, `escalate`), `retries` (entero, 0 significa que no realiza reintentos). |

---

## Cómo se registran las herramientas

Las herramientas se definen en el registro de herramientas de la plataforma. Cada definición de herramienta es un registro estructurado (según el esquema anterior) que el Capability Injector consulta al momento de construir el runtime de un agente.

El registro hace que una herramienta esté disponible para ser inyectada en cualquier agente que lo solicite. Sin embargo, registrarla no otorga acceso automático a ningún agente. El acceso real está estrictamente gobernado por el `manifest.md` del agente (campo `tools`) y el modelo de permisos (campo `required_permissions`).

El registro de herramientas es una operación a nivel de plataforma. Las nuevas herramientas que se introduzcan en las entregas de clientes deben estar registradas formalmente antes de poder ser declaradas en el `manifest.md` de cualquier agente.

---

## Cómo se inyectan las herramientas

El Capability Injector resuelve e inyecta las herramientas como el primer paso del pipeline de inyección (antes de las habilidades, el contexto, los permisos, la memoria y las políticas — ver `docs/platform/harness.md` para la explicación completa del orden).

**Secuencia de inyección para cada herramienta declarada en el `manifest.md` del agente:**

1. Se confirma que el nombre de la herramienta exista en el registro global. Si no existe, se aborta la instanciación del runtime.
2. Se evalúa el campo `required_permissions` contra el conjunto de permisos del agente solicitante. Si algún permiso requerido está ausente, la herramienta se excluye de la inyección. Si el `manifest.md` del agente declaró esta herramienta como obligatoria, la instanciación del runtime falla; si era opcional, se omite silenciosamente.
3. Se vincula el manejador del conector (*connector handle*) de la herramienta a la superficie de capacidades del runtime en memoria.
4. Para herramientas altamente sensibles (aquellas cuyos permisos requeridos implican acciones de escritura o envío: `write` o `send`), se marca la herramienta para realizar una revalidación de seguridad en tiempo de ejecución.

> **Nota sobre la revalidación de permisos:** Las comprobaciones de seguridad durante la fase de inyección reflejan el estado del sistema en el instante exacto de la instanciación. Para acciones que generan efectos secundarios críticos —escribir en base de datos, enviar mensajes externos, mutar estados—, los permisos se vuelven a evaluar en el momento preciso de la ejecución de la herramienta, y no solo durante la inyección. Esto protege al sistema contra cambios de permisos de usuario que ocurran durante sesiones de larga duración. Ver `docs/architecture/permission-model.md`.

---

## Ejemplos de herramientas

### `whatsapp_sender` (Emisor de WhatsApp)

| Campo | Valor |
|---|---|
| Conector | `meta_whatsapp_api` |
| Permisos requeridos | `send:whatsapp` |
| Entradas | `to` (string, número de teléfono en formato internacional E.164), `body` (string, texto del mensaje). |
| Salidas | `message_id` (string), `status` (string). |
| Manejo de errores | `on_connector_unavailable: fail_closed`, `on_permission_denied: escalate`, `retries: 1`. |

Envía un mensaje de texto a un contacto de WhatsApp a través de la API de Meta Cloud. Falla de forma cerrada (*fail closed*) si el conector no está disponible, ya que es preferible marcar el error de forma visible antes que descartar silenciosamente el mensaje y dejar al cliente esperando una respuesta que nunca llegará.

---

### `rag_catalog_search` (Búsqueda de Catálogo RAG)

| Campo | Valor |
|---|---|
| Conector | `postgres` (índice HNSW en pgvector sobre `catalog_embeddings`). |
| Permisos requeridos | `read:catalog` |
| Entradas | `query` (string, solicitud de producto en lenguaje natural), `top_k` (entero, valor por defecto 5), `min_score` (float, umbral mínimo de similitud de coseno). |
| Salidas | `results` (lista de objetos `{ sku, description, score }`). |
| Manejo de errores | `on_connector_unavailable: fail_open`, `on_permission_denied: fail_closed`, `retries: 0`. |

Genera el embedding de la consulta utilizando el proveedor configurado en la plataforma y busca coincidencias en la tabla `catalog_embeddings` mediante similitud de coseno. Devuelve los productos candidatos ordenados por relevancia. En caso de que el conector no esté disponible, falla de forma abierta (*fail open*): el agente puede intentar continuar respondiendo utilizando su razonamiento base y conocimientos previos con un nivel de confianza menor, en lugar de bloquearse por completo.

---

### `postgres_order_writer` (Escritor de Pedidos Postgres)

| Campo | Valor |
|---|---|
| Conector | `postgres` (tablas implicadas: `orders`, `order_items`). |
| Permisos requeridos | `write:orders`, `write:order_items` |
| Entradas | `client_id` (entero), `items` (lista de objetos `{ sku, description, quantity, unit_price }`), `notes` (string, opcional). |
| Salidas | `order_id` (entero), `status` (string). |
| Manejo de errores | `on_connector_unavailable: fail_closed`, `on_permission_denied: fail_closed`, `retries: 0`. |

Escribe un pedido confirmado y sus correspondientes líneas de detalle en la base de datos local. Falla de forma cerrada (*fail closed*) porque registrar un pedido incompleto o generar duplicados accidentales es mucho más perjudicial para el negocio que emitir un fallo controlado y visible.

---

### `redis_session_state` (Estado de Sesión Redis)

| Campo | Valor |
|---|---|
| Conector | `redis` |
| Permisos requeridos | `read:session_state`, `write:session_state` |
| Entradas | `operation` (uno de `get`, `set`, `delete`), `key` (string), `value` (string, obligatorio para `set`), `ttl_seconds` (entero, opcional). |
| Salidas | `value` (string o null). |
| Manejo de errores | `on_connector_unavailable: fail_open`, `on_permission_denied: fail_closed`, `retries: 0`. |

Lee y escribe estados de sesión efímeros en Redis. Utilizado para el almacenamiento de puntos de control (*checkpointing*) de conversaciones en LangGraph y para estados de deduplicación de vida corta. Falla de forma abierta (*fail open*): perder el estado efímero degrada ligeramente la experiencia del usuario, pero no corrompe la integridad de los datos de negocio.

---

### `client_lookup` (Búsqueda de Cliente)

| Campo | Valor |
|---|---|
| Conector | `postgres` (tabla implicada: `clients`). |
| Permisos requeridos | `read:client_registry` |
| Entradas | `phone_number` (string, formato internacional E.164). |
| Salidas | `client_id` (entero), `name` (string), `price_list_id` (entero o null), `active` (booleano). |
| Manejo de errores | `on_connector_unavailable: fail_open`, `on_permission_denied: fail_closed`, `retries: 0`. |

Resuelve un número de teléfono entrante asociándolo a un registro de cliente registrado. Falla de forma abierta (*fail open*) ante la indisponibilidad del conector, alineado con la política general de la plataforma de que los fallos en búsquedas periféricas de información complementaria no deben bloquear el procesamiento general de los mensajes entrantes.

---

## Referencias cruzadas

- Modelo de permisos y reglas de inyección por conector: `docs/architecture/permission-model.md`
- Pipeline de inyección y orden de precedencia: `docs/platform/harness.md`
- Habilidades (*skills* — contraparte de comportamiento de las herramientas): `docs/platform/skill.md`
- Campo `tools` en el `manifest.md` del agente: `docs/platform/role.md`
