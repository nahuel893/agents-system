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
| `name` | `string` | obligatorio | Identificador único de la herramienta. Utilizado por los manifiestos y el pipeline de inyección para referenciarla. En snake_case (ej. `catalog_search`). |
| `description` | `string` | obligatorio | Describe detalladamente qué hace la herramienta. El runtime del agente lee esta descripción para seleccionar e invocar la herramienta de forma correcta. Debe ser precisa y libre de ambigüedades. |
| `connector` | `string` | obligatorio | El sistema externo o servicio con el que se conecta esta herramienta. Ejemplos: `meta_whatsapp_api`, `postgres`, `redis`, `slack`. |
| `required_permissions` | `list[string]` | obligatorio | Identificadores de permisos RBAC que deben estar presentes en el conjunto de permisos del agente solicitante antes de que la herramienta pueda inyectarse. Si el agente carece de alguno de estos permisos, no recibirá la herramienta. |
| `tier` | `T0 \| T1 \| T2 \| T3` | obligatorio | Nivel de capacidad (ADR-002 C.10): qué tan peligrosa ES la herramienta, independientemente de cómo se nombre `required_permissions`. Sin valor por defecto — quien defina la herramienta debe clasificarla explícitamente. Ver "Niveles de capacidad" más abajo. |
| `inputs` | `object` | obligatorio | Parámetros de entrada que acepta la herramienta. Cada entrada contiene: `name` (string), `type` (string), `required` (booleano), `description` (string). |
| `outputs` | `object` | obligatorio | Estructura de los datos que devuelve la herramienta al ejecutarse con éxito. Cada entrada contiene: `name` (string), `type` (string), `description` (string). |
| `error_handling` | `object` | obligatorio | Define el comportamiento de la herramienta en caso de fallo. Subcampos: `on_connector_unavailable` (uno de `fail_open` (ignorar y continuar), `fail_closed` (bloquear), `escalate` (escalar)), `on_permission_denied` (uno de `fail_closed`, `escalate`), `retries` (entero, 0 significa que no realiza reintentos). |

---

## Niveles de capacidad (*Capability tiers*)

ADR-002 C.10. Toda herramienta declara un `tier` que clasifica qué tan peligrosa ES, independientemente de cómo se nombre su `required_permissions`. Quien defina una herramienta con un permiso peligroso sin prefijo `exec:`/`write:`/`send:` ya no queda invisible para las capas de aplicación descritas abajo — el tier es una clasificación explícita y revisable, no una inferencia a partir de una convención de nombres.

| Tier | Significado | Ejemplo | Quién lo recibe |
|---|---|---|---|
| **T0** | Inherente — todo agente lo necesita para funcionar | lectura de sesión | Todo agente, vía `base`/`agent` |
| **T1** | Lectura acotada | búsqueda en base de conocimiento, lectura de reporte de ventas | Roles cuyo manifiesto declara el permiso `read:*` correspondiente |
| **T2** | Escritura/envío acotado, o una consulta que escribe el modelo | escritor de pedidos, `send:message`, `sql_query` | Siempre revalidado en tiempo de llamada |
| **T3** | Ejecución en el host | `use_term`, `read_file` | Solo la rama `operator-agent` |

De tier se derivan dos consecuencias deterministas, ambas reemplazando (como superconjunto de, nunca un recorte de) la heurística anterior basada en el prefijo `write:`/`send:`:

- **Interceptor Layer 2 (tiempo de llamada).** Toda herramienta con tier T2 o T3 se revalida contra los permisos vigentes en cada llamada, sin importar cómo esté nombrado su `required_permissions`. `always_revalidate: true` extiende la misma revalidación a una herramienta T0/T1 puntual sin reclasificarla.
- **Capability Injector (tiempo de construcción), segunda barrera.** Un rol cuyo `policy.md` declara `untrusted_input: true` (ADR-002 C.11) nunca recibe una herramienta con tier T3, aunque el manifiesto del rol y los permisos otorgados a la identidad solicitante satisfagan `required_permissions`. Esto es independiente del invariante de exclusión mutua `untrusted_input`/`exec:*` de C.11: ese invariante bloquea por *familia de permiso*, esta barrera bloquea por *tier*, de modo que una herramienta T3 registrada bajo un permiso sin prefijo `exec:` igual queda atrapada.

**Invariante en tiempo de construcción.** Ambas consecuencias dependen de que `tier` refleje realmente el peligro de la herramienta — un tier vale lo que valga quien lo asignó. Por eso `ToolSpec` se valida a sí mismo al construirse (`__post_init__`): cualquier entrada de `required_permissions` de la familia `write:`/`send:` (sin distinguir mayúsculas ni espacios) exige que `tier` sea `T2` o `T3`; cualquier permiso `exec:*` exige específicamente `tier=T3`. Una combinación inválida lanza `ValueError` de inmediato, para cualquier invocador — constructores de herramientas de la plataforma y fixtures de prueba por igual —, de modo que quien defina una herramienta no pueda subclasificar en silencio un permiso peligroso y dejarlo pasar ambas capas de aplicación anteriores.

---

## Herramientas de comando declarativas (`command_tools`)

ADR-002 C.12. Antes de esto, la única forma de exponer un comando del host
era el conector genérico único `use_term`, controlado enteramente por una
lista blanca de nombres de programa (`TerminalPolicy.allowed_commands`) — un
rol obtenía acceso sin restricciones dentro de la lista blanca, o ninguno, y
esa lista no decía nada sobre la *forma de los argumentos* que hace
explotable a un comando permitido (`git -c core.sshCommand=...`, `find . -exec
rm {} \;`, `psql -c "DROP TABLE..."`, `curl -d @/etc/secret` son ejemplos
reales de un comando permitido por nombre vuelto peligroso por sus
argumentos).

`command_tools` cierra esa brecha: el `manifest.md` de un rol puede declarar
una o más herramientas de comando con `argv` fijo, cada una con marcadores de
posición de parámetro tipados y validados por patrón. No hay `argv` libre ni
shell — solo varían los parámetros declarados, y solo dentro de las
restricciones que el autor del manifiesto escribió para ellos.

```yaml
command_tools:
  - name: check_stock
    argv: ["/usr/bin/inventory-cli", "--sku", "{sku}", "--format", "json"]
    params:
      sku:
        type: string
        pattern: "^[A-Za-z0-9_-]{1,32}$"
        max_length: 32
    tier: T2
    permission: run:check_stock
```

### Esquema

| Campo | Tipo | Obligatorio | Descripción |
|---|---|---|---|
| `name` | `string` | obligatorio | Identificador único para esta herramienta de comando dentro del rol. |
| `argv` | `list[string]` | obligatorio | Plantilla `argv` fija. Cada elemento es un literal (controlado por el autor, nunca varía) o un marcador de posición `{param}` que debe ocupar el elemento COMPLETO — `--flag={param}` es inválido. `argv[0]` es la ruta del programa; nunca puede ser en sí mismo un marcador de posición. |
| `params` | `object` | opcional | Validación tipada por parámetro. Cada clave es un nombre de parámetro referenciado por un marcador `{param}` en `argv`; cada valor declara `type` (`string` o `integer`) y — **obligatorio para todo parámetro `string`** — `pattern` (expresión regular) o `enum` (lista de valores permitidos); `max_length` (entero, para parámetros de tipo string, acotado) sigue siendo opcional. |
| `tier` | `T0 \| T1 \| T2 \| T3` | obligatorio | Nivel de capacidad (ver "Niveles de capacidad" arriba) — declarado por el autor en cada entrada, no fijo, pero **aplicado con un piso: solo `T2` o `T3`.** `T0`/`T1` se rechazan en tiempo de carga. `check_stock` arriba es `T2` — el mínimo, y el nivel que ADR-002 C.12 permite deliberadamente que un rol `untrusted_input` sostenga, precisamente *porque* el `argv` fijo más un `pattern`/`enum` obligatorio en cada parámetro string lo mantienen acotado. Use `T3` para cualquier cosa que mute el estado del host o pueda alcanzar rutas arbitrarias (más amplio que una sola lectura/acción estrechamente delimitada). |
| `permission` | `string` | obligatorio | Debe comenzar con `run:` — una familia de permisos distinta de `exec:*`. Esto es lo que permite que un rol `untrusted_input` (ADR-002 C.11) sostenga con seguridad una herramienta de comando acotada sin activar el invariante de exclusión mutua `untrusted_input`/`exec:*` de C.11: ese invariante bloquea específicamente la familia `exec:*`, y una herramienta de comando nunca pertenece a ella. |

### El piso de nivel aplicado

Un permiso `run:*` (la familia de toda herramienta de comando) exige `tier` en `{T2, T3}` — aplicado dos veces, de forma independiente: `ToolSpec.__post_init__` (`harness/registry.py`) lanza `ValueError` al construirse, y el cargador (`harness/loader.py`) rechaza una herramienta de comando `T0`/`T1` en tiempo de carga con un `DefinitionError` que nombra la herramienta, de modo que un rol mal configurado nunca llega a resolverse. La razón es mecánica, no de estilo: `interceptor._is_sensitive` solo revalida una herramienta en tiempo de llamada cuando su tier es `T2`/`T3` (o cuando opta explícitamente con `always_revalidate`) — una herramienta de comando `T0`/`T1` quedaría equipada para un rol `untrusted_input` Y nunca se revalidaría, exactamente la combinación que la segunda barrera de ADR-002 C.10 existe para prevenir en `use_term`/`read_file` y que debe prevenir aquí igualmente.

`T2` en un rol `untrusted_input` sigue permitido — ese es el objetivo completo de ADR-002 C.12, y su propio ejemplo trabajado (`check_stock` arriba) lo usa. Lo que hace segura a una herramienta de comando `T2` para entrada no confiable no es el tier por sí solo; es la combinación de una plantilla `argv` fija (sin argumentos libres en absoluto) y un `pattern`/`enum` obligatorio en cada parámetro string (ver "La acotación es obligatoria" abajo). `T3` es para una herramienta de comando más amplia que eso — una que mute el estado del host o pueda alcanzar rutas arbitrarias.

### La acotación es obligatoria, no opcional

Todo parámetro `string` **debe** declarar `pattern` o `enum` — el cargador rechaza un parámetro `string` sin ninguno de los dos en tiempo de carga. Un parámetro `type: string` sin restricciones, sin `pattern`, no es en absoluto una herramienta de comando acotada: es una forma de contrabandear un valor arbitrario dentro del `argv` del comando, exactamente lo que un tier `T2` en un rol `untrusted_input` se supone que descarta por construcción. `max_length` sigue siendo opcional, pero cuando se declara está acotado (4096 caracteres) — un `max_length` un orden de magnitud más allá de lo que necesita cualquier parámetro real tampoco es una restricción de acotación significativa.

### Reglas de seguridad en tiempo de carga

Aplicadas por el cargador (`harness/loader.py`) antes de que un rol pueda siquiera resolverse, de modo que una declaración mal formada falla al desplegar, no en la primera llamada:

- Un marcador de posición debe ocupar un **elemento completo de argv**. Un marcador de posición parcial (`--flag={x}`) se rechaza, porque esa forma es exactamente cómo se cuela la inyección de opciones — `--flag=--evil-flag` de otro modo contrabandearía una segunda bandera a través de lo que parece un valor ordinario.
- Todo marcador de posición en `argv` debe referenciar un parámetro declarado, y todo parámetro declarado debe usarse en al menos un marcador de posición.
- `argv[0]` se resuelve a una **ruta absoluta una sola vez, en tiempo de carga** — un nombre relativo se busca en `$PATH` aquí y solo aquí; nada lo vuelve a resolver contra `$PATH` en tiempo de llamada, lo que elimina el vector de manipulación de `$PATH`.
- `permission` debe pertenecer a la familia `run:*`.
- `tier` debe ser `T2` o `T3` — ver "El piso de nivel aplicado" arriba.
- Todo parámetro `string` debe declarar `pattern` o `enum`, y cualquier `max_length` declarado no puede exceder el tope de la plataforma (4096) — ver "La acotación es obligatoria" arriba.
- Un override de despliegue solo puede **quitar** herramientas de comando de las que declara el rol, reflejando la regla sustractiva que ya sigue el resto del manifiesto (ver `docs/platform_es/deployment.md`) — nunca puede agregar una que el rol no haya declarado, ni redefinir el `argv`/`params`/`tier` de una entrada.

### Validación en tiempo de llamada y ejecución

El conector construido para una herramienta de comando declarada
(`connectors/command_tools.py`) rechaza, antes de sustituir cualquier valor
en la plantilla: un nombre de parámetro desconocido, un parámetro requerido
faltante, un valor del tipo declarado incorrecto, un valor que no cumple su
`pattern`/`max_length`/`enum` y — de forma independiente a todo lo anterior —
cualquier valor cuyo primer carácter sea `-` (U+002D) **o un carácter
Unicode similar a un guion** (guion medio `–`, guion largo `—`, cualquier
otro guion de la categoría Unicode Pd, o específicamente U+2212 SIGNO MENOS,
ya que ese pertenece a la categoría Sm y de otro modo se colaría ante una
verificación basada solo en la categoría). Esa protección es el cierre
directo de la clase de inyección de opciones de la tabla de ataques
anterior: ninguno de `-c core.sshCommand=...`, `-exec rm`, `-c "DROP
TABLE..."` ni `-d @/etc/secret` — ni una variante con guion similar de
cualquiera de ellos — puede llegar jamás al `argv` de una herramienta de
comando como valor de parámetro, sin importar qué `pattern` haya escrito o
no el autor del parámetro.

**Consecuencia, por diseño:** un valor entero negativo (p. ej. `-1`) nunca
puede pasarse a través de un parámetro de herramienta de comando — no hay
una forma acotada de distinguir "un número negativo" de "una bandera de
opción" en esta capa, así que ambos se rechazan. Una herramienta de comando
que realmente necesite un valor con signo debe aceptarlo como `string` con
un `pattern` que exprese su propio manejo del signo (por ejemplo, una
palabra de signo explícita, o un parámetro que en la práctica nunca sea
negativo).

Una vez que todos los valores son válidos, el conector los sustituye en la
plantilla y ejecuta el resultado a través del MISMO motor de subprocesos sin
shell que usa `use_term` (`create_subprocess_exec`, nunca un shell; el mismo
mecanismo de tiempo límite y límite de salida) — una herramienta de comando
no es un segundo ejecutor de comandos, es un frente declarativo sobre el
único mecanismo de ejecución que la plataforma ya reforzó. Ese único
mecanismo compartido es también lo que permite que el sandbox de bubblewrap
de ADR-002 C.14 envuelva `use_term` y cada herramienta de comando de forma
idéntica — ver "Sandbox T3 (bubblewrap)" más abajo.

### Sandbox T3 (bubblewrap)

ADR-002 C.14. `TerminalPolicy.sandbox` es un campo `SandboxPolicy`
obligatorio — sin valor por defecto, la misma postura que ya tenían `root`
y `allowed_commands`. Todo comando que pasa por el mecanismo compartido de
arriba (`use_term` y cada entrada de `command_tools`) corre dentro de
`bwrap`: sin red salvo que la política de la herramienta la declare
explícitamente, las rutas fijas del sistema (`/usr`, `/bin`, `/sbin`,
`/lib`, `/lib64`, `/etc`) de solo lectura, un `/tmp` privado, `policy.root`
de lectura-escritura, un entorno limpio, y un techo de memoria/CPU impuesto
vía `RLIMIT_AS`/`RLIMIT_CPU` (bubblewrap en sí no tiene banderas de límite
de recursos). Si `bwrap` no está presente en el host, el comando se niega
en vez de recaer en ejecutar sin sandbox.

---

## Cómo se registran las herramientas

Las herramientas se definen en el registro de herramientas de la plataforma. Cada definición de herramienta es un registro estructurado (según el esquema anterior) que el Capability Injector consulta al momento de construir el runtime de un agente.

El registro hace que una herramienta esté disponible para ser inyectada en cualquier agente que lo solicite. Sin embargo, registrarla no otorga acceso automático a ningún agente. El acceso real está estrictamente gobernado por el `manifest.md` del agente (campo `tools`) y el modelo de permisos (campo `required_permissions`).

El registro de herramientas es una operación a nivel de plataforma. Las nuevas herramientas que se introduzcan en las entregas de clientes deben estar registradas formalmente antes de poder ser declaradas en el `manifest.md` de cualquier agente.

---

## Cómo se inyectan las herramientas

El Capability Injector resuelve e inyecta las herramientas como el primer paso del pipeline de inyección (antes de las habilidades, el contexto, los permisos, la memoria y las políticas — ver `docs/platform_es/harness.md` para la explicación completa del orden).

**Secuencia de inyección para cada herramienta declarada en el `manifest.md` del agente:**

1. Se confirma que el nombre de la herramienta exista en el registro global. Si no existe, se aborta la instanciación del runtime.
2. Si el `policy.md` del rol solicitante declara `untrusted_input: true` y el `tier` de la herramienta es `T3`, se excluye la herramienta — sin importar si `required_permissions` se cumpliría de otro modo (segunda barrera de ADR-002 C.10; ver "Niveles de capacidad" más arriba).
3. Se evalúa el campo `required_permissions` contra el conjunto de permisos del agente solicitante. Si algún permiso requerido está ausente, la herramienta se excluye de la inyección. Si el `manifest.md` del agente declaró esta herramienta como obligatoria, la instanciación del runtime falla; si era opcional, se omite silenciosamente.
4. Se vincula el manejador del conector (*connector handle*) de la herramienta a la superficie de capacidades del runtime en memoria.
5. Para herramientas sensibles (tier `T2` o `T3`) o cualquier herramienta declarada explícitamente con `always_revalidate: true`, se marca la herramienta para realizar una revalidación de seguridad en tiempo de ejecución. `always_revalidate` permite que una herramienta T0/T1 opte por esa misma revalidación en tiempo de ejecución sin reclasificarla — la válvula de escape para una lectura que igual debe verificarse en el momento de la llamada. Su valor por defecto es `false`.

> **Nota sobre la revalidación de permisos:** Las comprobaciones de seguridad durante la fase de inyección reflejan el estado del sistema en el instante exacto de la instanciación. Para acciones que generan efectos secundarios críticos —escribir en base de datos, enviar mensajes externos, mutar estados—, los permisos se vuelven a evaluar en el momento preciso de la ejecución de la herramienta, y no solo durante la inyección. Esto protege al sistema contra cambios de permisos de usuario que ocurran durante sesiones de larga duración. Ver `docs/architecture/permission-model.md`.

---

## Ejemplos de herramientas

### `whatsapp_sender` (Emisor de WhatsApp)

| Campo | Valor |
|---|---|
| Conector | `meta_whatsapp_api` |
| Permisos requeridos | `send:whatsapp` |
| Tier | `T2` (envío acotado) |
| Entradas | `to` (string, número de teléfono en formato internacional E.164), `body` (string, texto del mensaje). |
| Salidas | `message_id` (string), `status` (string). |
| Manejo de errores | `on_connector_unavailable: fail_closed`, `on_permission_denied: escalate`, `retries: 1`. |

Envía un mensaje de texto a un contacto de WhatsApp a través de la API de Meta Cloud. Falla de forma cerrada (*fail closed*) si el conector no está disponible, ya que es preferible marcar el error de forma visible antes que descartar silenciosamente el mensaje y dejar al cliente esperando una respuesta que nunca llegará.

---

### `catalog_search` (Búsqueda de Catálogo)

| Campo | Valor |
|---|---|
| Conector | `CatalogSource` provisto por el despliegue |
| Permisos requeridos | `read:catalog` |
| Tier | `T1` (lectura acotada) |
| Entradas | `q` (string, solicitud de catálogo en lenguaje natural) |
| Salidas | `results` (lista de `{ sku, description, similarity }`, donde `similarity` es un float o null para la recuperación por palabras clave), `classification` (`direct`, `ambiguous` o `no_match`) |

Busca en el catálogo mediante un `CatalogSource` provisto por el despliegue. El despliegue es dueño de su esquema de catálogo y de su implementación de recuperación; la superficie pública de la herramienta devuelve `results` y `classification`.

---

### `postgres_order_writer` (Escritor de Pedidos Postgres)

| Campo | Valor |
|---|---|
| Conector | `postgres` (tablas implicadas: `orders`, `order_items`). |
| Permisos requeridos | `write:orders`, `write:order_items` |
| Tier | `T2` (escritura acotada) |
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
| Tier | `T2` (escritura acotada — `write:session_state` exige T2 o T3, verificado en la construcción) |
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
| Tier | `T1` (lectura acotada) |
| Entradas | `phone_number` (string, formato internacional E.164). |
| Salidas | `client_id` (entero), `name` (string), `price_list_id` (entero o null), `active` (booleano). |
| Manejo de errores | `on_connector_unavailable: fail_open`, `on_permission_denied: fail_closed`, `retries: 0`. |

Resuelve un número de teléfono entrante asociándolo a un registro de cliente registrado. Falla de forma abierta (*fail open*) ante la indisponibilidad del conector, alineado con la política general de la plataforma de que los fallos en búsquedas periféricas de información complementaria no deben bloquear el procesamiento general de los mensajes entrantes.

---

### `sql_query` (Consulta SQL de solo lectura)

| Campo | Valor |
|---|---|
| Conector | `connectors/sql_query_connector.py`, sobre un engine dedicado para el rol `sql_readonly` (`scripts/provision_sql_readonly.sql`). |
| Permisos requeridos | `query:sql` (la familia `Query`) |
| Tier | `T2` — la consulta la escribe el modelo; la decisión de tier está en ADR-007 |
| Entradas | `sql` (string: una sentencia PostgreSQL `SELECT` o `WITH … SELECT` sobre las vistas permitidas del despliegue). |
| Salidas | `columns` (lista), `rows` (lista de listas, apta para JSON; los importes como strings), `row_count`, `truncated` (coincidieron más filas que `row_limit`, o el presupuesto de bytes las cortó), `truncated_bytes` (las filas se cortaron en `byte_limit`), `row_limit`, `byte_limit`, `empty_result` (la consulta corrió y no coincidió nada), `relations` (vistas leídas) y `note` (una explicación fija, solo cuando `truncated_bytes`). |
| Errores | Textos fijos con un `error_kind`: `query_rejected` (con un código `reason`), `query_timeout`, `refused_by_database`, `query_invalid`, `data_error`, `query_failed`, `database_unavailable`, `role_not_read_only`, `sql_not_configured`. Ninguno incluye salida de la base de datos ni del driver. |

Ejecuta una consulta de solo lectura escrita por el modelo, para preguntas que el catálogo fijo de `run_report` no puede responder. Es la única herramienta que ejecuta SQL escrito por el modelo, por lo que su límite es el rol de base de datos y no la aplicación: el rol solo puede leer las vistas permitidas y no puede escribir nada, cada llamada corre en una transacción `READ ONLY` con un timeout de sentencia del lado del servidor, y la herramienta vuelve a verificar el rol en cada llamada y se niega a ejecutar si pudiera escribir o leer fuera de la lista permitida. Por encima, un guard de aplicación parsea la consulta y ejecuta solo su representación canónica, acotada a `row_limit + 1` filas y a `byte_limit` bytes (64 KiB por defecto), que la base de datos hace cumplir antes de enviar cualquier fila excesiva. Ningún rol predefinido la equipa; un despliegue la registra con `build_sql_query_tool_spec(engine, SqlQueryConfig(views=...))`. El aprovisionamiento, el modelo de amenazas y la enmienda a AD-2 están en [ADR-007](../architecture_es/adr-007-read-only-sql-tool.md).

---

## Referencias cruzadas

- Modelo de permisos y reglas de inyección por conector: `docs/architecture/permission-model.md`
- Herramienta SQL de solo lectura, su límite de confianza y la enmienda a AD-2: `docs/architecture_es/adr-007-read-only-sql-tool.md`
- Pipeline de inyección y orden de precedencia: `docs/platform_es/harness.md`
- Habilidades (*skills* — contraparte de comportamiento de las herramientas): `docs/platform_es/skill.md`
- Campo `tools` en el `manifest.md` del agente: `docs/platform_es/role.md`
