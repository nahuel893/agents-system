# Arquitectura de la Plataforma de Agentes

Este documento define la arquitectura formal de agents-system, un kit de desarrollo para construir agentes de IA. El camino más corto para empezar es el [Inicio rápido](../platform_es/library-usage.md#inicio-rápido) (*Quickstart*): un agente con una herramienta propia en unas diez líneas.

## Resumen ejecutivo

El kit está dividido en tres partes:

1. **Kit core**: el harness. Definiciones de agentes declarativas y en Python, orquestación, ciclo de vida, permisos e inyección de capacidades. Es con lo que se construyen los agentes.
2. **Piezas opcionales**: el **servidor** compatible con OpenAI, los **canales** (WhatsApp es el primero), los **roles predefinidos** (arquetipos de agente reutilizables) y los **backends de referencia** (Postgres, Redis, embeddings). Son opcionales en tiempo de ejecución, no al instalar: se activan solo las que hacen falta, pero al instalar el kit igual se traen las dependencias de todas (fastapi, asyncpg, redis y torch) hasta que #188, una instalación liviana del core, cambie el empaquetado.
3. **Tu aplicación**: las herramientas, conectores, reglas de negocio y overrides por cliente que convierten el kit en un agente concreto. Viven en tu propio repositorio, no en este.

Esta separación es intencional. El kit es el producto reutilizable. Cualquier agente construido con él, un asistente de ventas incluido, es una aplicación del kit y nunca el kit en sí.

---

## Límites del alcance

### Kit core

El kit core es dueño de la infraestructura multi-agente genérica:
- Definiciones declarativas de roles mediante carpetas y archivos en Markdown.
- Factoría de agentes (*Agent Factory*) y entorno de ejecución del proveedor.
- Inyección dinámica de herramientas, habilidades, contexto y políticas de ejecución.
- Reglas de orquestación y delegación.
- Ciclo de vida de instanciación (*spawn*) para agentes hijos.
- Aplicación de control de acceso basado en roles (RBAC) consciente de la identidad.
- Persistencia de memoria, registro de auditoría (*audit trail*) y logs de ejecución.
- Caché activa (*warm cache*) opcional para la creación de agentes con baja latencia.

### Piezas opcionales

El kit las entrega para que no tengas que construirlas, y nada del core depende de ellas en tiempo de ejecución. Al instalar el kit igual se traen sus dependencias:
- El servidor compatible con OpenAI y sus canales.
- Los roles predefinidos bajo `platform/roles/`.
- Los backends de referencia para persistencia, checkpointing y embeddings.

### Tu aplicación

Una aplicación construida sobre el kit es dueña de todo lo específico de su dominio:
- Sus flujos de trabajo y su lógica conversacional.
- Las integraciones con sus propios sistemas, registradas como herramientas.
- Sus reglas, vocabulario, flujos de aprobación y restricciones de negocio.
- El estrechamiento por cliente de un rol predefinido.

### No habilitado por defecto
Las siguientes capacidades se consideran capacidades del kit o módulos futuros; una aplicación las adopta de forma explícita:
- Agentes de asistencia personal para empleados corporativos.
- Agentes especializados en resúmenes de reuniones y minutas de conversación.
- Agentes avanzados de analítica de datos a demanda.
- Entornos de ejecución locales para estaciones de trabajo (*workstation runtimes*).
- Flujos jerárquicos complejos de delegación de agentes hijos múltiples.

---

## Principios de diseño

1. **Enfoque declarativo primero (Declarative first)**:
   - Los roles se definen como carpetas bajo `platform/roles/` o `deployments/`, conteniendo `role.md`, `manifest.md` y `policy.md`.
   - El comportamiento en tiempo de ejecución se ensambla a partir de estas definiciones, no se escribe en código duro por cada caso.

2. **Instanciación bajo demanda (Instantiate on demand)**:
   - Los agentes se crean y destruyen bajo demanda como objetos normales del runtime.
   - No hay enjambres permanentes encendidos por defecto.

3. **Inyección de dependencias sobre acoplamiento oculto**:
   - Todas las herramientas, habilidades, contexto, permisos y políticas se inyectan explícitamente al inicio del ciclo de vida.

4. **Menor privilegio (Least privilege)**:
   - Cada agente recibe únicamente el conjunto mínimo de capacidades necesarias para el rol y la tarea actual.

5. **Auditabilidad**:
   - Cada ruta de ejecución debe ser completamente reconstruible: quién la disparó, qué rol se usó, qué herramientas se inyectaron y qué acciones se tomaron.

6. **Delegación composable (Composable delegation)**:
   - Los agentes pueden delegar trabajo únicamente cuando las políticas de la factoría lo permitan de forma explícita.
   - La delegación es una capacidad inyectada, no una suposición por defecto.

7. **Runtime consciente del rendimiento**:
   - El inicio en frío del agente debe ser extremadamente económico.
   - Se deben usar cachés activas (*warm caches*) cuando la latencia sea crítica.

---

## Vista general de la arquitectura

```text
Disparador (Trigger) -> System Router -> Agent Factory -> Capability Injector -> Agent Runtime -> Ejecución / Delegación -> Auditoría / Memoria
```

### Etapas principales

1. **Disparador (Trigger)**:
   - Acción de usuario (mensaje de WhatsApp, API, web).
   - Mensaje entrante.
   - Programación temporal (cron/recordatorio).
   - Evento externo (webhook).
   - Callback de ejecución de herramienta.

2. **System Router / Enrutador del Sistema**:
   - Identifica el dominio y subsistema.
   - Selecciona el rol adecuado.
   - Decide si se instancia un runtime fresco o se reutiliza una caché activa (*warm cache*).

3. **Agent Factory / Factoría de Agentes**:
   - Construye el runtime base.
   - Resuelve la selección de proveedor y modelo (ej. Gemini vs Claude).
   - Aplica las políticas básicas de ejecución.

4. **Capability Injector / Inyector de Capacidades**:
   - Inyecta herramientas, habilidades, contexto organizacional, permisos RBAC, manejadores de memoria y políticas de orquestación.

5. **Agent Runtime / Entorno de Ejecución**:
   - Ejecuta el rol asignado, llama herramientas y colabora o escala según lo dicte la política.

6. **Persistencia y Limpieza**:
   - El runtime se destruye o se recicla en caché.
   - Los registros de auditoría y actualizaciones de memoria se persisten de forma duradera.

---

## Modelo formal de runtime

La plataforma distingue estrictamente entre tres conceptos:

| Concepto | Significado | Dónde reside |
|---|---|---|
| **AgentDefinition (Definición)** | Estructura declarativa en carpeta de un rol (`role.md` + `manifest.md` + `policy.md`). | Disco y Git. |
| **AgentRuntime (Runtime)** | Instancia de ejecución en memoria instanciada bajo demanda. | Memoria de la factoría. |
| **Subsystem (Subsistema)** | Grupo coordinado de roles y políticas en un dominio operativo. | Configuración de topología. |

Esta separación evita mezclar las definiciones, la identidad del runtime y las fronteras de orquestación.

---

## Modelo de agentes

### Agentes centrales del MVP
La topología inicial del sistema incluye:
- **Orchestrator Agent (Agente Orquestador)**
- **Sales Agent (Agente de Ventas)**
- **Data Agent (Agente de Datos)**
- **Summary Agent (Agente de Resumen)**

Todos estos agentes son gobernados por roles e instanciados dinámicamente.

### Agente de Empleado (Employee Agent)
Cada empleado tiene asignado conceptualmente:
- **Un único agente de empleado principal**.
- Instanciado bajo demanda y acotado estrictamente a su identidad y permisos RBAC.
- No es un proceso persistente ni parte de un enjambre continuo.

### Runtime local opcional
El agente de empleado puede correr localmente en la estación de trabajo del usuario utilizando:
- **Hermes Agent**
- **OpenClaw**
- **PicoClaw**

Esto es parte de la topología futura y no un requerimiento inicial obligatorio.

---

## Delegación y agentes hijos

### Comportamiento por defecto
Un agente de empleado **no** crea múltiples agentes hijos de forma predeterminada.

### Comportamiento excepcional
El agente de empleado **puede** instanciar agentes hijos únicamente cuando la política de su `policy.md` lo permita de forma explícita, para casos como:
- Descomposición de tareas complejas en subtareas.
- Investigación paralela en múltiples fuentes de información.
- Ejecución aislada de herramientas sensibles (sandbox).
- División de trabajo entre productor y revisor (evitar anclaje mental del LLM).
- Mantenimiento de fronteras de privacidad de contextos protegidos.

### Orquestación local temporal
Cuando un agente de empleado delega, actúa temporalmente como un **orquestador local limitado a su propio dominio**. El sistema le inyecta las políticas:
- `orchestrator_generic`: Reglas universales de ciclo de vida de hijos, registro de auditoría, handoff de datos y límites de seguridad.
- `orchestrator_role`: Restricciones del dominio específico, tales como qué tipos de agentes hijos se permiten spawnear y qué herramientas se les pueden transferir.

---

## Definiciones de agentes (Carpetas de definición)

Cada rol reside en una carpeta específica conteniendo tres archivos en Markdown:

**`role.md`**:
- Nombre del rol.
- Propósito empresarial.
- Alcance operativo y fronteras.

**`manifest.md`**:
- Herramientas permitidas.
- Habilidades activas.
- Requerimientos de contexto.
- Permisos RBAC exigidos.

**`policy.md`**:
- Reglas de escalamiento y HITL.
- Política de delegación.
- Alcance de memoria y persistencia.
- Política de auditoría y retención.

---

## Modelo de inyección de capacidades

Al instanciarse un runtime, el inyector asocia de forma ordenada y obligatoria:
- **Tools**: Conectores ejecutables con el mundo exterior.
- **Skills**: Prompts estructurados de comportamiento y razonamiento.
- **Context**: Datos en vivo de la tarea, sesión y organización.
- **Permissions**: Verificaciones de seguridad RBAC de la identidad solicitante.
- **Memory**: Manejadores de memoria local, compartida o corporativa.
- **Execution policies**: Autonomía, escalamiento y límites del cargador.

---

## Ciclo de vida y caché activa

La factoría puede almacenar en una caché activa (*warm cache*) runtimes pre-inyectados para roles de alta frecuencia con el fin de eliminar la latencia de inicio en frío, bajo una regla inquebrantable: **la caché activa jamás debe compartir datos privados, contextos de sesión o snapshots de permisos entre diferentes usuarios**.

---

## Modelo de permisos y RBAC

Los agentes operan en nombre de identidades reales. Sus accesos están limitados por el set de permisos del empleado que los dispara. El control se realiza en dos capas:
1. **En la inyección de capacidades**: Excluyendo herramientas no autorizadas antes del inicio del runtime.
2. **En tiempo de ejecución (Revalidación en caliente)**: Para llamadas a herramientas sensibles o críticas (ej. persistencia de pedidos), protegiendo al sistema contra escalamientos de privilegios o cambios repentinos de permisos en el backend.

---

## Qué entrega el kit y qué te deja a ti

### Entregado

El kit entrega el harness core, los ocho roles predefinidos, el servidor compatible con OpenAI y los backends de referencia. Un agente construido sobre el kit, un asistente de ventas por ejemplo, es un rol predefinido más las herramientas de la aplicación que lo usa.

### Potencial de hoja de ruta

El kit más amplio admite extensiones futuras hacia:

- agentes para empleados
- resúmenes
- asistencia de datos más profunda
- runtimes locales para estaciones de trabajo
- delegación controlada de agentes hijos

Son capacidades del kit u opciones de la hoja de ruta; una aplicación las adopta de forma explícita.

---

## Referencias cruzadas

- Esquema de directivas y mezcla de carpetas: `docs/platform_es/deployment.md`
- Reglas y límites de delegación: `docs/architecture_es/delegation-policy.md`
- Matriz de control de acceso y RBAC: `docs/architecture_es/permission-model.md`
- Correcciones de arquitectura al modelo de agentes, prompts, permisos de herramientas y persistencia descritos en este documento — incluye la definición formal de "agente" (`docs/platform_es/agent.md`) y señala dónde la versión en inglés de este documento (`docs/architecture/agent-platform.md`) quedó desactualizada con la ruta `agents/` (ruta real: `platform/roles/`; esta versión en español ya usa la ruta correcta): `docs/architecture_es/adr-002-agent-model-and-capabilities.md`
