# Manifiesto de la Plataforma

## Qué es esta plataforma

`agents-system` es un entorno de ejecución (*runtime*) de agentes reutilizable. Proporciona la infraestructura necesaria para construir, desplegar y operar agentes de IA basados en roles a través de diferentes dominios de clientes, sin necesidad de reescribir el runtime en cada despliegue.

La plataforma no es un producto diseñado para un único cliente. Es la base sobre la cual se construyen los productos específicos de cada cliente.

**Responsabilidades principales de la plataforma:**
- Definición declarativa de roles a través de carpetas de definición del agente.
- Factoría de agentes (*Agent Factory*) que resuelve modelos, políticas y configuración base.
- Flujo de inyección de capacidades (*Capability Injection Pipeline*): herramientas (*tools*), habilidades (*skills*), contexto, permisos, memoria y políticas de ejecución.
- Aplicación de permisos basados en roles (RBAC) vinculados a la identidad del usuario/empleado.
- Gestión del ciclo de vida: instanciación bajo demanda, almacenamiento en caché cuando sea beneficioso y destrucción limpia.
- Delegación y orquestación de agentes hijos bajo políticas explícitas.
- Registro de auditoría (*audit trail*) y persistencia de memoria.

## Qué NO es esta plataforma

**No es un enjambre (*swarm*).** Los agentes no son procesos siempre activos que se ejecutan en paralelo. Se instancian bajo demanda, ejecutan un rol y luego se destruyen o se devuelven a una caché activa (*warm cache*).

**No es un monolito.** El runtime de la plataforma y cualquier implementación específica para un cliente son responsabilidades separadas. La plataforma no incluye lógica de negocio, vocabulario de la empresa, credenciales de integración ni flujos de trabajo específicos del dominio. Esas responsabilidades pertenecen al alcance de entrega (*delivery scope*).

**No es un chatbot con comportamiento estático (*hardcoded*).** Ningún comportamiento está grabado a fuego en el runtime. Todo el comportamiento de un rol se ensambla al momento de la instanciación a partir de la carpeta de definición del agente, capacidades inyectadas y políticas. Cambiar un rol no requiere modificar el runtime.

**No es un simple wrapper de LLM.** La plataforma gestiona la orquestación, delegación, RBAC, ciclo de vida y auditoría; aspectos que un cliente de API de LLM básico no resuelve de forma nativa.

---

## Principios fundamentales

### 1. Enfoque declarativo primero (*Declarative first*)

Los roles de los agentes se definen como carpetas bajo `platform/roles/` (para las plantillas genéricas) y `deployments/` (para las sobreescrituras específicas de clientes) — no como subclases, ni como blobs de configuración, ni como prompts embebidos en el código. Cada carpeta de definición del agente contiene `role.md` (identidad), `manifest.md` (capacidades) y `policy.md` (comportamiento). La carpeta de definición del agente es la especificación autoritativa de lo que un rol tiene permitido ser y hacer.

**Justificación:** Las definiciones declarativas son legibles por seres humanos, auditables, versionables en Git e independientes de la implementación del runtime. Hacen que el sistema sea inspeccionable sin necesidad de rastrear líneas de código.

### 2. Instanciación bajo demanda (*Instantiate on demand*)

Los agentes se crean cuando llega un disparador (*trigger*) y se destruyen (o se almacenan en caché) cuando se completa la ejecución. No existe un enjambre permanente por defecto.

**Justificación:** Los agentes siempre activos consumen recursos, acumulan contexto obsoleto y complican la aplicación de permisos. La instanciación basada en la demanda mantiene el sistema eficiente y hace que los costos de inicio en frío (*cold start*) sean visibles y medibles.

### 3. Inyección de dependencias sobre acoplamiento oculto (*Dependency injection over hidden coupling*)

Cada capacidad que utiliza un agente —herramientas, habilidades, contexto, permisos, manejadores de memoria, políticas de orquestación— se inyecta explícitamente al momento de la instanciación. El runtime no accede a estados globales.

**Justificación:** El acoplamiento oculto hace que el comportamiento sea impredecible y dificulta las pruebas. La inyección explícita hace que la superficie de capacidades de cualquier agente en ejecución sea completamente inspeccionable y auditable.

### 4. Menor privilegio (*Least privilege*)

Cada agente recibe únicamente el conjunto mínimo de capacidades necesarias para el rol y la tarea actual. Ningún agente hereda capacidades que no necesita, incluso si un agente padre tiene permisos más amplios.

**Justificación:** El menor privilegio limita el radio de impacto cuando algo sale mal —ya sea que un modelo se comporte de forma inesperada, una herramienta falle o se explote una cadena de delegación. También refuerza el principio de que los agentes no deben ser capaces de hacer más de lo que requiere su rol declarado.

### 5. Auditabilidad (*Auditability*)

Cada ruta de ejecución debe ser reconstruible a posteriori: quién la disparó, qué rol estaba activo, qué herramientas se inyectaron, qué acciones se tomaron y qué resultados se produjeron.

**Justificación:** Los agentes que toman acciones en nombre de usuarios u organizaciones deben rendir cuentas. La auditabilidad es una condición previa para operar agentes de IA en un contexto de producción empresarial, no una ocurrencia de último momento.

### 6. Delegación composable (*Composable delegation*)

Los agentes pueden delegar trabajo a agentes hijos únicamente cuando la política lo permite de forma explícita. La delegación es una capacidad que debe ser inyectada, no un comportamiento por defecto que cualquier agente pueda invocar libremente.

**Justificación:** La delegación sin restricciones genera grafos de ejecución infinitos, consumo impredecible de recursos, riesgos de bypass de permisos y brechas en la auditoría. Hacer de la delegación una capacidad gobernada mantiene al sistema seguro y predecible.

### 7. Runtime consciente del rendimiento (*Performance-aware runtime*)

La instanciación de agentes (inicio en frío) debe ser lo suficientemente económica para que la creación bajo demanda sea el comportamiento por defecto. En los casos en que la latencia no tolere el inicio en frío, se puede mantener una caché activa (*warm cache*) de runtimes pre-instanciados, sujeta a reglas estrictas de aislamiento de contexto.

**Justificación:** Las preocupaciones de rendimiento no deben forzar compromisos arquitectónicos. La caché activa es una capa de optimización, no un requerimiento estructural. Solo es segura cuando se garantiza que los permisos y el contexto privado obsoleto de un usuario o sesión no puedan filtrarse a otros.

---

## Límite entre plataforma y entrega de cliente

La frontera entre la Plataforma Core y el alcance de entrega de un cliente no es un límite físico de archivos rígido; es un contrato conceptual sobre qué pertenece a cada lugar.

### La Plataforma Core posee:

- El runtime del agente y su ciclo de vida.
- La factoría de agentes y la resolución del proveedor de modelo.
- El pipeline de inyección de capacidades y sus reglas de ordenamiento.
- El modelo RBAC y la lógica de evaluación de permisos.
- El framework de políticas de delegación y orquestación.
- Las interfaces del registro de auditoría y persistencia de memoria.
- El mecanismo de caché activa y las garantías de aislamiento de contexto.

La Plataforma Core no distribuye lógica de negocio, vocabulario corporativo, credenciales de integración ni conocimiento del dominio. Proporciona infraestructura agnóstica al dominio donde se despliegue.

### El alcance de entrega de cliente posee:

- Carpetas de definición del agente que describen comportamientos de agentes específicos del dominio (incluyendo `manifest.md` y `policy.md` completos).
- Definiciones de herramientas y configuraciones de conectores para las integraciones del cliente.
- Habilidades (*skills*): paquetes de comportamiento y módulos de prompts afinados para el dominio e idioma del cliente.
- Reglas de negocio, umbrales de aprobación, vocabulario y políticas de escalamiento.
- Cualquier credencial, secreto o endpoint de integración específico del cliente.

> **Decisión abierta (5):** El límite contractual exacto entre la propiedad intelectual (IP) de la plataforma y la implementación específica del cliente no se ha formalizado. Específicamente: qué artefactos (carpetas de definición del agente, prompts de habilidades, esquemas de herramientas) pertenecen a la plataforma frente a cuáles pertenecen al cliente cuando la plataforma se despliega para un nuevo cliente. Ver `docs/architecture/permission-model.md` para discusiones relacionadas.

---

## Entrega actual: BADIE

La primera implementación de cliente para esta plataforma es el **agente Seller AI / Preventa** para **Distribuidora BADIE S.A.** (Grupo Manzur), una distribuidora argentina de bebidas con marcas que incluyen Quilmes, Brahma, Stella Artois, CCU y Branca.

BADIE opera una red de vendedores de campo llamados *preventistas* que visitan los puntos de venta (kioscos, almacenes, bares, restaurantes) y toman pedidos manualmente. El Seller AI digitaliza este proceso: los puntos de venta pueden enviar pedidos a través de WhatsApp en español coloquial argentino, el agente los interpreta, mapea los productos mediante búsqueda semántica sobre el catálogo (RAG), y confirma y persiste los pedidos.

El alcance de entrega para BADIE está acotado intencionalmente: el **Agente de Preventa** es el único agente comprometido con el cliente. Las capacidades más amplias de la plataforma —agentes para empleados, agentes de resumen, agentes de datos, runtimes locales— son elementos del roadmap del producto, no compromisos comerciales de esta entrega.

La especificación completa de la entrega para BADIE se encuentra en `docs/delivery/badie-seller-ai.md`.
