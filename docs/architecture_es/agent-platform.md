# Arquitectura de la Plataforma de Agentes

Este documento define la arquitectura formal para expandir `agents-badie` desde un sistema de IA único enfocado en ventas hacia una plataforma de agentes reutilizable.

## Resumen ejecutivo

La plataforma está dividida en dos capas distintas:

1. **Plataforma Core (Core Platform)**: Infraestructura y entorno de ejecución reutilizable, orquestación, ciclo de vida, permisos e inyección de capacidades.
2. **Alcance de Entrega de BADIE (BADIE Delivery Scope)**: La implementación concreta del cliente que se entrega actualmente, comenzando con el agente Seller AI / Preventa.

Esta separación es intencional. La plataforma es el producto de software reutilizable. BADIE es un despliegue de cliente específico de dicho producto.

---

## Límites del alcance

### Plataforma Core

La Plataforma Core es dueña de la infraestructura multi-agente genérica:
- Definiciones declarativas de roles mediante carpetas y archivos en Markdown.
- Factoría de agentes (*Agent Factory*) y entorno de ejecución del proveedor.
- Inyección dinámica de herramientas, habilidades, contexto y políticas de ejecución.
- Reglas de orquestación y delegación.
- Ciclo de vida de instanciación (*spawn*) para agentes hijos.
- Aplicación de control de acceso basado en roles (RBAC) consciente de la identidad.
- Persistencia de memoria, registro de auditoría (*audit trail*) y logs de ejecución.
- Caché activa (*warm cache*) opcional para la creación de agentes con baja latencia.

### Alcance de Entrega de BADIE

La implementación para BADIE es dueña de los comportamientos específicos de la distribuidora:
- Flujos de trabajo y lógica conversacional de Seller AI / Preventista.
- Integraciones con los sistemas internos de la empresa: `DeW` y `App Preventas`.
- Integraciones de consulta y monitoreo con `App Sergio`.
- Integraciones con la base de conocimiento de `Outline`.
- Integraciones de mensajería con la red de chat de `Element`.
- Reglas de negocio del cliente, vocabulario local (jerga cervecera argentina), flujos de aprobación y restricciones comerciales.

### No incluido en el alcance por defecto
Las siguientes capacidades se consideran módulos de plataforma futuros y no forman parte de la entrega comprometida inicial para BADIE, a menos que se acuerde formalmente lo contrario:
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
- **Preventa Agent (Agente de Preventa / Ventas)**
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

## Referencias cruzadas

- Límites de entrega comprometidos para BADIE: `docs/delivery_es/badie-seller-ai.md`
- Esquema de directivas y mezcla de carpetas: `docs/platform_es/deployment.md`
- Reglas y límites de delegación: `docs/architecture_es/delegation-policy.md`
- Matriz de control de acceso y RBAC: `docs/architecture_es/permission-model.md`
