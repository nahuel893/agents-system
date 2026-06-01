# Modelo de Despliegue (Deployment Model)

La plataforma separa las definiciones de roles genéricas de las implementaciones específicas de los clientes a través de una estructura de dos capas. Los roles genéricos definen la plantilla de comportamiento y las fronteras de capacidades. Los despliegues (*deployments*) extienden y especializan esos roles para el contexto específico de cada cliente.

Esta separación es la resolución de la decisión abierta #5: la propiedad intelectual (IP) de la plataforma reside en `platform/roles/`, mientras que la implementación específica del cliente reside en `deployments/{client}/`.

---

## Estructura de carpetas

```
platform/
  roles/
    sales-agent/          ← genérico: qué es un agente de ventas
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
  badie/
    sales-agent/          ← sobreescritura de BADIE: preventista + DeW + español coloquial AR
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

1. Carga el rol genérico desde `platform/roles/{role-type}/`
2. Mezcla (*merge*) la sobreescritura del cliente desde `deployments/{client}/{role-type}/`
3. Construye el runtime a partir de la definición ya mezclada y consolidada

La sobreescritura sigue estrictamente estas reglas por cada archivo:

### Sobreescritura de `role.md`

El archivo `role.md` del despliegue extiende el rol genérico con contexto específico del cliente:
- Añade el nombre de la empresa, el dominio, el idioma y el vocabulario de negocio.
- Añade la declaración del propósito del cliente por encima de la definición genérica.
- No puede eliminar ni contradecir las fronteras de alcance del rol genérico.

### Sobreescritura de `manifest.md`

El archivo `manifest.md` del despliegue puede:
- Añadir herramientas desde el registro aprobado de la plataforma (ej. `dew_connector`, `app_preventas_writer`).
- Declarar cuáles habilidades de la carpeta `skills/` del despliegue están activas.
- Restringir el alcance del contexto (haciéndolo más estrecho que el genérico).

El archivo `manifest.md` del despliegue no puede:
- Añadir herramientas que no estén presentes en el registro global de la plataforma.
- Elevar los requisitos de permisos de ninguna herramienta.
- Expandir el acceso al contexto más allá de lo que permite el manifiesto genérico.

### Sobreescritura de `policy.md`

El archivo `policy.md` del despliegue puede:
- Restringir el nivel de autonomía (ej. `supervised` → `confirm`).
- Añadir reglas de escalamiento más estrictas.
- Reducir los límites de ejecución (timeouts más cortos, menor número de llamadas a herramientas permitidas).
- Definir umbrales específicos del cliente para la intervención humana (*human-in-the-loop*).

El archivo `policy.md` del despliegue no puede:
- Elevar el nivel de autonomía por encima del límite máximo del rol genérico.
- Eliminar reglas de escalamiento definidas en la política genérica.
- Incrementar los límites de ejecución más allá de los valores por defecto de la plataforma.

### `skills/` (exclusivo de los despliegues)

Las habilidades son siempre específicas del cliente. No existen habilidades genéricas de plataforma. La carpeta `skills/` existe únicamente dentro de los despliegues. Las habilidades son módulos de prompts de comportamiento que definen cómo razona el agente sobre tareas específicas del dominio — codifican el vocabulario del cliente, las reglas de negocio y los patrones de interacción conversacional.

---

## Directivas de mezcla en YAML (YAML Merge Directives)

El cargador de definiciones (`loader.py`) implementa un vocabulario preciso de directivas en YAML para regular cómo se mezclan las propiedades de la plantilla genérica con las sobreescrituras del cliente. Esto evita redundancias y mantiene el control de versión limpio:

### 1. Comportamiento por defecto (Ausencia de campo)
Si un campo está definido en la plantilla genérica (`platform/roles/`) pero está **completamente ausente** en la sobreescritura del despliegue (`deployments/{client}/`), el cargador lo hereda automáticamente sin modificaciones.

### 2. Heredar explícitamente (`inherit`)
Si un campo escalar se define con el valor literal `"inherit"` en la sobreescritura, toma el valor original del padre tal cual:
```yaml
permissions: inherit
```

### 3. Directivas para listas (List Directives)
Para campos que contienen colecciones (como `tools`, `skills` o `permissions`), el cargador admite directivas estructuradas en lugar de simples valores planos:
- **`add` (Agregar con deduplicación)**: Mantiene la lista del padre y le añade nuevos elementos, eliminando duplicados automáticamente y preservando el orden.
  ```yaml
  skills:
    inherit: true
    add:
      - order_extraction
      - colloquial_matching
  ```
- **`remove` (Remover elementos)**: Mantiene la lista del padre pero filtra y elimina los elementos indicados.
  ```yaml
  tools:
    inherit: true
    remove:
      - redis_session_state
  ```
- **`override` (Reemplazo total)**: Sobreescribe por completo la lista del padre por la nueva lista proporcionada (sujeto a las restricciones de los invariantes). Se puede declarar como una lista directa o usando la clave `override`:
  ```yaml
  tools:
    override:
      - whatsapp_sender
      - rag_catalog_search
  ```

### 4. Directivas para diccionarios y mapeos (Mapping Directives)
Para campos estructurados como diccionarios (por ejemplo, `escalation_rules`, `delegation_policy`, `memory_policy` o `audit_policy`), se admite la mezcla incremental:
- **Mezcla con `inherit: true`**: Si el diccionario incluye `inherit: true`, el cargador toma todas las propiedades del padre como base y aplica selectivamente los campos declarados en el hijo.
  ```yaml
  escalation_rules:
    inherit: true
    conditions:
      inherit: true
      add:
        - "el cliente no está registrado"
  ```
  *(Las listas dentro de estos mapeos, como `conditions`, también soportan las directivas `add` y `remove` mencionadas arriba).*
- **Reemplazo absoluto**: Si el diccionario no contiene `inherit: true`, la sobreescritura reemplaza por completo la configuración del padre.

---

## Invariantes estructurales (Structural Invariants)

> **Regla de Oro de la Plataforma:** Un despliegue específico de cliente solo puede *restringir o especializar* el comportamiento. Jamás puede elevar capacidades, relajar límites ni saltarse controles de seguridad definidos en el rol genérico.

Durante el proceso de mezcla (`merge`), el cargador de configuraciones (`loader.py`) valida de forma estricta los siguientes **cuatro invariantes estructurales**. Si se viola cualquiera de ellos, el cargador aborta de inmediato levantando una excepción `DefinitionError`, bloqueando la instanciación del runtime:

### Invariante 1 — Restricción de Herramientas (`tools`)
Las herramientas declaradas por la sobreescritura del despliegue del cliente deben ser un subconjunto estricto de las herramientas declaradas en la plantilla genérica del rol:
$$\text{set(override.tools)} \subseteq \text{set(parent.tools)}$$
Si la sobreescritura solicita una herramienta que no está aprobada en la plantilla genérica del rol, la inyección falla.

### Invariante 2 — Restricción de Permisos (`permissions`)
Los permisos resueltos finales del despliegue deben ser un subconjunto de los permisos declarados en el rol genérico:
$$\text{resolved\_permissions} \subseteq \text{parent.permissions}$$
Esto impide que una configuración de cliente intente elevar privilegios o solicitar accesos de seguridad no planificados para ese tipo de rol en la plataforma.

### Invariante 3 — Techo de Autonomía (`autonomy`)
El nivel de autonomía del despliegue debe ser menor o igual al nivel de autonomía declarado en el rol genérico:
$$\text{autonomy\_rank(override)} \le \text{autonomy\_rank(parent)}$$
El cargador ordena los rangos de autonomía de menor a mayor (más restrictivo a más permisivo):
1. **`confirm` (Rango 0)** — El agente debe pedir confirmación humana antes de ejecutar cualquier herramienta.
2. **`supervised` (Rango 1)** — El agente actúa autónomamente en tareas seguras y escala en excepciones.
3. **`full` (Rango 2)** — El agente ejecuta libremente todas las herramientas de su superficie.

Si el rol genérico está definido con autonomía `supervised`, un despliegue de cliente puede restringirlo a `confirm`, pero **jamás** elevarlo a `full`.

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

  return AgentFactory.build(definition, user_identity)
```

Si no existe una sobreescritura de despliegue para un cliente y rol específicos, la plataforma utiliza la definición genérica tal como está. Esto permite una especialización gradual: un cliente puede comenzar operando con el rol genérico e ir agregando sobreescrituras de forma incremental a medida que se refinan sus necesidades.

---

## Ejemplo: BADIE sales-agent

### `platform/roles/sales-agent/role.md` (genérico)
```
name: sales-agent
purpose: >
  Asistir a los clientes a realizar pedidos a través de una interfaz conversacional.
  Interpretar solicitudes de productos en lenguaje natural, contrastarlas con el
  catálogo disponible, confirmar el pedido y persistirlo.
scope: order-taking, catalog-lookup, order-confirmation
```

### `deployments/badie/sales-agent/role.md` (sobreescritura)
```
extends: platform/roles/sales-agent
company: Distribuidora BADIE S.A. (Grupo Manzur)
language: es-AR (Español Rioplatense)
domain: distribución de cerveza y bebidas — Argentina
vocabulary:
  - "la rubia" → Quilmes
  - "cajón" → cajón de 24 unidades retornables
  - "preventista" → vendedor de campo
  - "punto de venta" → comercio minorista cliente (kiosco, bar, almacén)
purpose_extension: >
  Gestionar pedidos de clientes minoristas registrados (puntos de venta) a
  través de la API de WhatsApp Business. Interpretar nombres y cantidades
  coloquiales de productos argentinos, contrastarlos con el catálogo de BADIE
  mediante RAG, y persistir los pedidos confirmados en el sistema DeW / App Preventas.
```

---

## Delimitación de memoria en despliegues (*Memory scoping*)

La capa de memoria sigue el mismo esquema de delimitación de dos niveles:

| Alcance / Scope | Qué almacena |
|---|---|
| `platform / {role_type}` | Comportamiento genérico del agente aprendido a lo largo del tiempo en todos los despliegues. |
| `deployment / {client} / {role_type}` | Conocimiento específico del cliente (patrones de catálogo, preferencias corporativas). |
| `deployment / {client} / {role_type} / {user_id}` | Memoria individual del usuario (historial de pedidos, preferencias personales, notas de entrega). |

La memoria de un despliegue específico siempre se encuentra aislada de otros despliegues. Jamás se permite el acceso a memoria cruzada entre diferentes clientes.

---

## Referencias cruzadas

- Esquema de definición de roles: `docs/platform_es/role.md`
- Esquema de políticas y control: `docs/platform_es/policy.md`
- Inyección de capacidades y límites en el harness: `docs/platform_es/harness.md`
- Modelo de permisos y RBAC: `docs/architecture/permission-model.md`
- Alcance de entrega de BADIE: `docs/delivery/badie-seller-ai.md`
