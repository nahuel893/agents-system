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

## Invariante de permisos

> Un despliegue solo puede restringir o especializar. Jamás puede elevar capacidades.

El rol genérico define la superficie de capacidades máxima. Ninguna sobreescritura de despliegue puede excederla. El Capability Injector aplica esta regla durante la fase de inyección: si el manifiesto de un despliegue solicita una herramienta o un permiso que no está presente en la superficie permitida del rol genérico, la inyección falla de inmediato y el runtime no se crea.

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
    assert definition.permissions ⊆ generic.permissions  # invariante
    assert definition.autonomy_level ≤ generic.autonomy_level  # invariante

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
