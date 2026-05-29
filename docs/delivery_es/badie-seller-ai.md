# Alcance de Entrega de BADIE: Seller AI (Preventista Inteligente)

## Qué se ha comprometido con el cliente

La entrega comprometida y acotada para **Distribuidora BADIE S.A.** (Grupo Manzur) es el **agente Seller AI / Preventa**: un agente conversacional operado a través de WhatsApp que recibe solicitudes de pedidos de productos de los comercios y puntos de venta minoristas, interpreta los pedidos en español coloquial argentino (jerga preventista), busca y mapea los artículos mediante búsqueda semántica (RAG) sobre el catálogo de bebidas, y persiste los pedidos una vez confirmados.

Ningún otro agente de la plataforma forma parte de este alcance de entrega inicial. Los agentes para empleados internos, agentes de resumen de minutas, agentes de analítica de datos a demanda y los entornos de ejecución locales para estaciones de trabajo son capacidades exclusivas de la plataforma o metas a futuro del roadmap, y no forman parte de los compromisos comerciales con BADIE a menos que se amplíe explícitamente el contrato.

> **Decisión abierta #5:** El límite de propiedad intelectual (IP) exacto entre el Core de la Plataforma y la implementación específica para el cliente no se ha formalizado formalmente. Específicamente: las carpetas de definición de agentes, los prompts de habilidades específicas del dominio y las configuraciones de conectores de herramientas desarrolladas para BADIE contienen lógicas de negocio mixtas (tanto genéricas de distribución como particulares del cliente). La clasificación legal y técnica de estos artefactos (propiedad de la plataforma frente a propiedad del cliente) debe resolverse antes de considerar licencias o despliegues multi-cliente.

---

## Contexto de negocio

**Empresa:** Distribuidora BADIE S.A., miembro del Grupo Manzur. Distribuidora de bebidas líder en Argentina que comercializa las principales marcas de consumo masivo: Quilmes, Brahma, Stella Artois, Patagonia, CCU (Schneider, Imperial, Heineken), fernet Branca, entre otras.

**Dominio operativo:** BADIE cuenta con una red de vendedores de campo llamados **preventistas** que visitan físicamente a los clientes minoristas (kioscos, almacenes, minimercados, bares y restaurantes) y toman sus pedidos de reposición manualmente en planillas o apps. El Seller AI digitaliza y automatiza este canal: los puntos de venta pueden enviar sus pedidos por WhatsApp en cualquier momento (las 24 horas del día), sin tener que esperar la visita semanal del preventista.

**Idioma y registro lingüístico:** Los clientes se comunican utilizando español rioplatense extremadamente coloquial e informal. Los pedidos raramente mencionan nombres de SKUs exactos de catálogo; en su lugar, usan apodos de productos, marcas genéricas, abreviaciones y terminologías locales. Ejemplos reales:
- *"dame dos cajones de la rubia"* → 2 cajones retornables de cerveza Quilmes clásica de 1L.
- *"tres Brahma lata"* → 3 latas de cerveza Brahma de 473ml (o el calibre disponible).
- *"algo para los chicos, sin alcohol"* → Requiere aclaración (categoría de gaseosas, aguas o jugos, no un SKU de catálogo directo).

El agente debe interpretar este registro de manera fluida y natural. Las búsquedas literales de strings sobre la base de datos de catálogo no funcionan para esto; se requiere recuperación semántica (embeddings de texto) y mapeo dinámico de sinónimos conversacionales.

**Modelo de datos del cliente:**
- Los comercios y puntos de venta se identifican unívocamente en la base de datos mediante su número de teléfono en formato internacional E.164.
- Cada punto de venta tiene asignada una lista de precios específica (`id_lista_precio`) proveniente de la tabla `gold.dim_cliente` del data warehouse medallion de la distribuidora.
- Los clientes deben estar previamente registrados y marcados como activos antes de poder interactuar con el bot. Cualquier número desconocido que escriba al bot se registrará automáticamente como inactivo para auditoría y no recibirá respuestas automáticas del agente.
- El catálogo de productos se alimenta de la tabla `gold.dim_articulo` (capa gold del data warehouse medallion), estructurada con atributos clave: `marca`, `generico` (categoría base), `calibre` (contenido/envase) y `proveedor`.

---

## Roles de agentes entregados

### Agente de Preventa (Preventa Agent — Primario)
Es el único rol conversacional directo de cara al usuario comprometido en esta entrega.

**Responsabilidades:**
- Recibir mensajes entrantes de WhatsApp desde los clientes minoristas registrados.
- Interpretar de forma inteligente los pedidos coloquiales y desestructurados en español rioplatense.
- Ejecutar búsquedas semánticas (RAG con pgvector sobre la tabla local `catalog_embeddings`) para emparejar las solicitudes coloquiales con los SKUs exactos de catálogo.
- Aplicar la lista de precios específica asignada al cliente para cotizar el pedido en vivo.
- Presentar un resumen consolidado al cliente y obtener su confirmación explícita antes de guardar.
- Persistir los pedidos confirmados en las tablas locales de base de datos `orders` y `order_items`.
- Escalar inmediatamente a un operador de atención humano ante excepciones críticas (número no registrado, coincidencia de producto ambigua tras el número máximo de aclaraciones permitidas, pedido explícito del cliente de hablar con un humano o un pedido que supere el límite de crédito o monto máximo establecido).

**No realiza:**
- Responder preguntas generales sobre el estado de cuenta de la distribuidora.
- Consultar niveles de stock físicos en tiempo real en los camiones o depósitos.
- Gestionar reclamos, devoluciones de envases vacíos o problemas de facturación.
- Operar fuera de los límites transaccionales de la toma del pedido actual.

### Agente Orquestador (Orchestrator Agent — Mínimo de ruteo)
Para esta primera entrega del MVP, el Agente Orquestador opera con capacidades mínimas de enrutamiento: recibe el trigger de WhatsApp entrante desde el webhook de Meta, valida rápidamente que el cliente exista y esté activo en la tabla de clientes, e instancia y delega el mensaje al runtime del Agente de Preventa. No ejecuta lógicas complejas de orquestación ni delegaciones jerárquicas avanzadas en esta fase.

Este agente no tiene interfaz conversacional con el cliente y es transparente para los usuarios.

---

## Herramientas inyectadas para el Agente de Preventa

| Herramienta | Conector de Infraestructura | Propósito |
|---|---|---|
| `whatsapp_sender` | Meta WhatsApp Cloud API | Envío de mensajes y plantillas salientes de WhatsApp al cliente. |
| `rag_catalog_search` | PostgreSQL / pgvector (HNSW) | Búsqueda semántica (RAG) sobre los vectores del catálogo de productos. |
| `postgres_order_writer` | PostgreSQL | Guardar y persistir las cabeceras y líneas de pedidos confirmados. |
| `redis_session_state` | Redis | Persistencia de estados de conversación de LangGraph y deduplicación. |
| `client_lookup` | PostgreSQL | Resolver el número de teléfono entrante a un registro de cliente activo. |

---

## Habilidades inyectadas para el Agente de Preventa

| Habilidad (Skill) | Propósito y Comportamiento |
|---|---|
| `order_extraction` | Extraer intenciones de compra estructuradas desde mensajes conversacionales desestructurados. Procesa cantidades, especificaciones de envase, marcas y detecta artículos faltantes para aclaración. |
| `colloquial_product_matching` | Mapear términos populares y apodos del dialecto argentino de bebidas a los SKUs correctos del catálogo; filtra y evalúa las puntuaciones de distancia vectorial devueltas por RAG. |
| `confirm_flow` | Guiar al agente en la fase de cierre: presentar el resumen de artículos y precios totales, permitir correcciones de cantidades al cliente y asegurar la confirmación final de compra antes del guardado. |

---

## Puntos de integración

### Integración con DeW / App Preventas
- **Origen de catálogo:** La tabla `gold.dim_articulo` del almacén de datos (data warehouse) se sincroniza periódicamente hacia la tabla local `catalog_embeddings`. Un script de sincronización lee la capa gold de medallion, genera los vectores semánticos y los escribe con índices HNSW en el PostgreSQL local de la plataforma.
- **Destino de pedidos:** Los pedidos confirmados y consolidados por el agente de preventa se graban localmente en las tablas `orders` y `order_items`. La integración asíncrona directa hacia la cola de ingesta del ERP DeW representa una fase subsiguiente en el roadmap técnico (fuera del MVP inicial).

### Base de Datos PostgreSQL Local
Tablas locales en el alcance de esta entrega:
- `clients`: Registro de preventa (teléfono, id de cliente, estado activo, lista de precios asignada).
- `orders`: Cabeceras de pedidos (cliente, estado, totales, marcas de tiempo).
- `order_items`: Líneas de pedidos (SKU del catálogo, descripción, cantidad unitaria, precio cobrado).
- `conversation_logs`: Registro de auditoría (*audit trail*) conversacional detallado para reconstrucción.
- `catalog_embeddings`: Vectores de 512 dimensiones de los productos del catálogo con índice HNSW.

### Redis
- Deduplicación de webhooks entrantes (evita doble procesamiento por reintentos de Meta mediante flags `SET NX` con TTL de 300 segundos).
- Checkpointing de persistencia de estados conversacionales para LangGraph.

### Meta WhatsApp Business API
- Canal de entrada: webhook HTTP POST protegido por verificación de firma digital HMAC-SHA256.
- Canal de salida: herramienta `whatsapp_sender` a través de la API de nube oficial de Meta.

---

## Fuera de alcance para esta entrega

Las siguientes características y agentes quedan **estrictamente fuera del alcance** comprometido para el MVP de BADIE Seller AI:
- Agentes de empleado para uso interno de los administrativos de BADIE.
- Agente especializado en resúmenes de minutas o conversaciones internas (`Summary Agent`).
- Agente avanzado de analítica (`Data Agent`) para consultas complejas sobre App Sergio, Outline u otros data warehouses.
- Entornos de ejecución locales para PCs corporativas (Hermes Agent, OpenClaw, PicoClaw).
- Delegaciones jerárquicas complejas de agentes hijos (el Agente de Preventa tiene `delegation_policy.allowed: false` en su configuración de políticas).
- Orquestación con patrón Supervisor avanzado (se utiliza ruteo simple de primer nivel; el patrón Supervisor queda bajo análisis para la Fase 2 del roadmap técnico).

---

## Estado del desarrollo

| Hito técnico del proyecto | Estado actual |
|---|---|
| Recepción de webhooks + validación de firma HMAC | Completado (Paso 1A.1) |
| Deduplicación y control de concurrencia en Redis | Completado (Paso 1A.2) |
| Resolución de identidad de cliente y auto-registro | Completado (Paso 1A.3) |
| Tubería de sincronización de catálogo (`gold` a local) | Completado (Paso 1A.4c) |
| Sincronización y normalización de teléfonos de clientes | Completado (Paso 1A.4d) |
| Abstracción de servicios de embeddings (OpenAI / Fake) | Completado (Paso 1A.4b) |
| Proveedor de embeddings local optimizado (BGE-M3) | Completado (Paso 1A.4e) |
| Servicio de búsqueda semántica (pgvector HNSW, umbrales 0.92/0.82) | Completado (Paso 1A.5) |
| Suite de pruebas de RAG con jerga cervecera local | **Siguiente: Paso 1A.6** |
| Estado de conversación conversacional en LangGraph | Pendiente (Paso 1A.7) |
| Lógica del agente (ruteador, recuperación, cotización, guardado) | Pendiente (Paso 1B.x) |
| Cliente de comunicación saliente de WhatsApp | Pendiente (Paso 1C.x) |
| Fase 2: Patrón Supervisor, colas distribuidas Celery | Futuro |

---

## Referencias cruzadas

- Manifiesto de la plataforma y límites core/delivery: `docs/platform_es/manifesto.md`
- Definición de carpeta de rol del Agente de Preventa: `deployments/badie/sales-agent/` (ver `docs/platform_es/role.md` para el esquema)
- Especificación de herramientas y conectores: `docs/platform_es/tool.md`
- Especificación de comportamiento de habilidades: `docs/platform_es/skill.md`
- Ciclo de vida y tubería de inyección en el harness: `docs/platform_es/harness.md`
- Políticas de control (Preventa no delega): `docs/architecture_es/delegation-policy.md`
- Políticas de seguridad y control RBAC: `docs/architecture_es/permission-model.md`
- Arquitectura general de la plataforma: `docs/architecture_es/agent-platform.md`
