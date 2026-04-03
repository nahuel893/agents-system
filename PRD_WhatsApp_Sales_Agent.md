# PRD — Sistema de Agentes de Ventas por WhatsApp
**Proyecto:** WhatsApp Sales Agent Bot  
**Empresa:** Distribuidora BADIE S.A. — Grupo Manzur  
**Autor:** Área de Ingeniería de Datos  
**Versión:** 1.0.0  
**Fecha:** Marzo 2026  
**Estado:** En definición

---

## Índice

1. [Resumen ejecutivo](#1-resumen-ejecutivo)
2. [Contexto y problema](#2-contexto-y-problema)
3. [Objetivos del sistema](#3-objetivos-del-sistema)
4. [Alcance](#4-alcance)
5. [Usuarios y stakeholders](#5-usuarios-y-stakeholders)
6. [Arquitectura del sistema](#6-arquitectura-del-sistema)
7. [Requerimientos funcionales](#7-requerimientos-funcionales)
8. [Requerimientos no funcionales](#8-requerimientos-no-funcionales)
9. [Stack tecnológico](#9-stack-tecnológico)
10. [Diseño del agente LangGraph](#10-diseño-del-agente-langgraph)
11. [Estrategia RAG y matching de productos](#11-estrategia-rag-y-matching-de-productos)
12. [Optimización de tokens y costos](#12-optimización-de-tokens-y-costos)
13. [Integración WhatsApp Business API](#13-integración-whatsapp-business-api)
14. [Modelo de datos](#14-modelo-de-datos)
15. [Limitaciones y riesgos](#15-limitaciones-y-riesgos)
16. [Path de desarrollo (roadmap)](#16-path-de-desarrollo-roadmap)
17. [Métricas de éxito](#17-métricas-de-éxito)
18. [Consideraciones legales y de privacidad](#18-consideraciones-legales-y-de-privacidad)
19. [Glosario](#19-glosario)

---

## 1. Resumen ejecutivo

El proyecto consiste en la construcción de un **sistema multi-agente de ventas por WhatsApp**, cuyo objetivo principal es suplantar la función operativa del preventista en el proceso de toma de pedidos diarios. El sistema interactúa con clientes via WhatsApp Business API, entiende lenguaje coloquial, matchea artículos del catálogo mediante búsqueda vectorial (RAG), registra los pedidos en la base de datos de BADIE, y permite modificaciones post-cierre.

El sistema debe escalar a un mínimo de **1.000 clientes activos por día** operando con múltiples conversaciones simultáneas, con costos de LLM controlados mediante compresión de contexto, routing por modelo y prompt caching.

---

## 2. Contexto y problema

### 2.1 Contexto operativo

BADIE S.A. opera una red de distribución de cerveza y bebidas con preventistas que visitan clientes (puntos de venta, almacenes, kioscos, supermercados) de forma periódica. La comunicación entre clientes y preventistas ocurre en gran parte por WhatsApp, con el preventista como intermediario entre el cliente y el sistema de gestión interno (ChessERP).

### 2.2 Problemas actuales

| Problema | Impacto |
|---|---|
| El preventista toma pedidos manualmente y los carga en el sistema | Errores humanos, demoras, doble carga de trabajo |
| Disponibilidad limitada del preventista (horarios, días de visita) | Clientes que no pueden pedir fuera del horario |
| Variabilidad en la calidad de la atención | Dependencia del desempeño individual |
| Escalabilidad limitada: 1 preventista → N clientes fijo | Difícil crecer sin contratar más personas |
| Sin trazabilidad automática de conversaciones | Pérdida de datos de demanda e historial |

### 2.3 Oportunidad

Un agente conversacional entrenado en el rol del preventista puede cubrir el proceso de toma de pedidos de forma autónoma, disponible 24/7, con capacidad de atender múltiples clientes en paralelo y con trazabilidad total. El foco es **automatizar la operación rutinaria**, no reemplazar la relación comercial estratégica.

---

## 3. Objetivos del sistema

### 3.1 Objetivo principal

Desarrollar un agente conversacional por WhatsApp que suplante la función operativa del preventista en el ciclo de pedido diario: saludar al cliente, ofrecer catálogo, interpretar el pedido en lenguaje coloquial, confirmarlo y permitir modificaciones.

### 3.2 Objetivos secundarios

- Reducir el tiempo de carga manual de pedidos en el sistema
- Aumentar la disponibilidad de atención (de horario de negocio a 24/7)
- Generar trazabilidad automática de todas las conversaciones y pedidos
- Controlar el costo operativo del sistema de LLM a escala de 1.000+ clientes/día
- Proveer una base de datos de conversaciones para análisis de demanda y futuro fine-tuning

---

## 4. Alcance

### 4.1 Dentro del alcance (v1.0)

- Comunicación bidireccional via WhatsApp Business API
- Presentación del catálogo de productos activos
- Interpretación de pedidos en lenguaje coloquial via RAG (pgvector)
- Registro de pedidos en PostgreSQL
- Confirmación del pedido al cliente con resumen
- Modificación o ampliación del pedido del día por parte del cliente
- Atención simultánea de múltiples clientes con aislamiento de estado por `thread_id`
- Dashboard de monitoreo de conversaciones, gasto de tokens y tasa de éxito

### 4.2 Fuera del alcance (v1.0)

- Negociación de precios o condiciones comerciales (sigue siendo función humana)
- Gestión de crédito o deuda del cliente
- Procesamiento de pagos
- Integración directa con ChessERP (la carga en ERP queda como paso posterior manual o semi-automático en v1.0)
- Atención de reclamos o devoluciones
- Soporte multilenguaje (solo español rioplatense)
- Aplicación móvil o canal distinto a WhatsApp

---

## 5. Usuarios y stakeholders

### 5.1 Usuarios del sistema

| Usuario | Rol | Interacción |
|---|---|---|
| Cliente (punto de venta) | Usuario final | Conversa via WhatsApp |
| Preventista | Receptor de pedidos procesados | Revisa pedidos registrados; interviene en escalados |
| Supervisor comercial | Stakeholder | Accede al dashboard de métricas |
| Área de IT / Data Engineering | Propietario técnico | Administra el sistema |

### 5.2 Personas de usuario — cliente tipo

**"Don Roberto" — Almacén de barrio**
- 55 años, comunica en lenguaje coloquial ("dame dos cajones de la rubia")
- Usa WhatsApp todos los días
- No tiene paciencia para formularios o menús extensos
- Quiere confirmar rápido y seguir con su día

**"Supermercado La Esquina" — Comprador joven**
- 30 años, más digital, puede usar lenguaje más formal
- Hace pedidos más grandes y complejos
- Puede querer ver el catálogo completo antes de decidir

---

## 6. Arquitectura del sistema

### 6.1 Vista de alto nivel

El sistema se organiza en tres capas:

```
┌─────────────────────────────────────────────────┐
│  Capa 1 — Integración                           │
│  WhatsApp Business API → Webhook FastAPI         │
│  → Message Router → Rate Limiter                │
└─────────────────────┬───────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────┐
│  Capa 2 — Orquestación LangGraph                │
│  Supervisor Agent                               │
│  ├── Catalog Agent    (consulta catálogo)       │
│  ├── Order Agent      (interpreta pedido)       │
│  ├── Confirm Agent    (cierra pedido)           │
│  └── Modify Agent     (post-cierre)             │
│  Estado: Redis (por thread_id, TTL 12h)         │
└─────────────────────┬───────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────┐
│  Capa 3 — Datos                                 │
│  pgvector (embeddings catálogo)                 │
│  PostgreSQL (pedidos, clientes, catálogo)       │
│  Redis (estado conversacional)                  │
│  LLM API (Claude / GPT-4o mini)                 │
└─────────────────────────────────────────────────┘
```

### 6.2 Flujo de un mensaje entrante

```
1. Cliente envía mensaje por WhatsApp
2. Meta llama al webhook de FastAPI (HTTPS POST)
3. Message Router extrae: phone_number, texto, timestamp
4. Se busca o crea el thread_id en Redis
5. El Supervisor Agent recibe el mensaje + estado comprimido
6. El Supervisor clasifica la intención (LLM call barato)
7. Se delega al sub-agente correspondiente
8. El sub-agente ejecuta su lógica + tools (RAG, BD, etc.)
9. Se genera la respuesta y se actualiza el estado en Redis
10. FastAPI envía la respuesta al cliente via WhatsApp API
```

### 6.3 Patrón de resiliencia

- Si el webhook no puede procesar el mensaje en < 5 segundos, encola en Celery y responde a Meta con HTTP 200 para evitar reenvíos
- Si el LLM falla, se activa respuesta de fallback: "Estoy teniendo un inconveniente técnico, en breve te contacta un preventista"
- Si el matching de producto falla dos veces seguidas, el agente escala a humano

---

## 7. Requerimientos funcionales

### RF-01 — Inicio de conversación

El sistema debe identificar si un número de teléfono corresponde a un cliente registrado en la base de datos. Si no está registrado, debe iniciar un flujo de alta simplificado o escalar a un preventista.

**Criterio de aceptación:** El agente saluda al cliente por su nombre o razón social en < 2 segundos desde que llega el primer mensaje.

### RF-02 — Presentación del catálogo

El agente debe poder presentar el catálogo de productos disponibles, ya sea el catálogo completo o filtrado por categoría, con precios actualizados al momento de la consulta.

**Criterio de aceptación:** El agente muestra productos con nombre, presentación y precio. El catálogo mostrado refleja el estado actual de la BD (sin cache estático).

### RF-03 — Interpretación del pedido en lenguaje coloquial

El sistema debe poder interpretar expresiones como "dame dos cajones de la rubia", "quiero 3 six de la sin alcohol" y mapearlas al SKU correcto del catálogo mediante búsqueda vectorial (RAG).

**Criterio de aceptación:** Tasa de matching correcto ≥ 90% sobre un set de prueba de 100 expresiones coloquiales reales. En caso de ambigüedad, el agente ofrece máximo 3 opciones para que el cliente elija.

### RF-04 — Construcción del pedido del día

El agente mantiene un carrito de compras en el estado de la conversación. El cliente puede agregar múltiples artículos durante la conversación antes de confirmar.

**Criterio de aceptación:** El carrito persiste entre mensajes del mismo día para el mismo cliente. Se muestra el resumen acumulado antes de confirmar.

### RF-05 — Confirmación del pedido

Antes de cerrar el pedido, el agente presenta un resumen completo (artículos, cantidades, precios) y solicita confirmación explícita del cliente.

**Criterio de aceptación:** El pedido solo se registra en la BD después de la confirmación. El cliente recibe número de pedido y resumen por WhatsApp.

### RF-06 — Modificación post-cierre

Después de confirmado el pedido, el cliente debe poder modificarlo (agregar o quitar artículos, cambiar cantidades) hasta un horario de corte configurable.

**Criterio de aceptación:** Las modificaciones se registran en la BD con timestamp y referencia al pedido original. El cliente recibe confirmación de la modificación.

### RF-07 — Concurrencia

El sistema debe manejar múltiples conversaciones simultáneas sin interferencia entre clientes. Cada conversación tiene estado completamente aislado.

**Criterio de aceptación:** 1.000 conversaciones simultáneas sin degradación de latencia (p95 < 5 segundos).

### RF-08 — Escalado a humano

Cuando el agente no puede resolver la consulta después de 2 intentos, debe notificar al cliente que será atendido por un preventista y generar una alerta interna (Slack / notificación al supervisor).

**Criterio de aceptación:** La tasa de escalado no supera el 10% de las conversaciones. El escalado ocurre en < 30 segundos desde que se detecta la necesidad.

### RF-09 — Dashboard de monitoreo

Dashboard en Plotly Dash que muestre: conversaciones activas, pedidos registrados, tasa de matching, gasto en tokens, escalados, errores.

**Criterio de aceptación:** Dashboard actualizado en tiempo real (o con lag < 60 segundos).

---

## 8. Requerimientos no funcionales

### RNF-01 — Latencia

- Respuesta al cliente: p50 < 3 segundos, p95 < 5 segundos
- El webhook a Meta debe responder HTTP 200 en < 5 segundos (límite de Meta)

### RNF-02 — Disponibilidad

- El sistema debe estar disponible 24/7
- SLA objetivo: 99.5% de uptime mensual

### RNF-03 — Escalabilidad

- Diseño stateless en FastAPI: escala horizontalmente con múltiples workers (uvicorn + gunicorn)
- El estado de conversación reside en Redis, no en memoria del proceso
- Diseñado para soportar crecimiento a 5.000 clientes/día sin rediseño

### RNF-04 — Costo operativo LLM

- Costo objetivo: < 0.005 USD por conversación completa (incluyendo todas las llamadas LLM)
- Monitoreo de gasto en tokens por modelo, por cliente, por día

### RNF-05 — Seguridad

- Validación de webhook signature de Meta en cada request
- HTTPS obligatorio en todos los endpoints
- Rate limiting por número de teléfono (máximo N mensajes por minuto)
- Datos de conversación cifrados en reposo

### RNF-06 — Mantenibilidad

- Cada sub-agente es un módulo independiente, testeable en aislamiento
- Logs estructurados (JSON) con trazabilidad de `thread_id`, `model`, `tokens_used`, `latency_ms`
- Variables de entorno para configuración de modelos, umbrales y prompts

---

## 9. Stack tecnológico

| Componente | Tecnología | Justificación |
|---|---|---|
| Integración WhatsApp | Meta Cloud API (WABA) + BSP (360dialog o Twilio) | Único canal oficial; BSP para volúmenes altos |
| Backend / Webhook | FastAPI (Python) | Stack existente en BADIE; async nativo; alto rendimiento |
| Orquestación agentes | LangGraph 0.2+ | Grafo de estados nativo; checkpointing; multi-agente; stateful por thread |
| LLM conversacional | claude-sonnet-4-20250514 | Calidad de respuesta, soporte de tools, prompt caching |
| LLM clasificación / resumen | claude-haiku-4-5 | Barato y rápido para tasks simples |
| Embeddings | text-embedding-3-small (OpenAI) o voyage-3 (Anthropic) | Costo/calidad balanceado para catálogo |
| Vector DB | pgvector (extensión PostgreSQL) | Reutiliza infraestructura existente; sin nueva dependencia |
| Estado conversacional | Redis 7+ (con TTL 12h por sesión) | Sub-milisegundo de lectura/escritura; evicción automática |
| Base de datos principal | PostgreSQL 17 (existente) | Pedidos, clientes, catálogo maestro |
| Queue async | Celery + Redis | Tasks lentas: compresión historial, notificaciones, archivado |
| Servidor | Debian Linux (existente) | uvicorn + gunicorn para FastAPI |
| Monitoreo | Plotly Dash + PostgreSQL | Dashboard interno; stack existente |
| CI/CD | GitHub Actions | Deploy automático desde `main` |

---

## 10. Diseño del agente LangGraph

### 10.1 Concepto: grafo de estados

LangGraph modela cada conversación como un **grafo de estados dirigido**. Cada nodo es una función Python que recibe el estado actual y devuelve un estado modificado. Las aristas son condicionales basadas en el resultado del nodo anterior.

El estado persiste entre mensajes del mismo cliente via el **checkpointer de Redis**, usando `thread_id = phone_number` como clave de aislamiento.

### 10.2 Esquema de estado

```python
from typing import TypedDict, Literal, Annotated
from langgraph.graph.message import add_messages

class OrderItem(TypedDict):
    sku: str
    description: str
    quantity: int
    unit: str              # "cajón", "six", "unidad"
    unit_price: float

class ConversationState(TypedDict):
    # Identificación
    thread_id: str         # phone_number del cliente
    client_id: str         # ID en BD
    client_name: str

    # Fase actual del flujo
    phase: Literal[
        "greeting",
        "catalog",
        "ordering",
        "confirm_pending",
        "confirmed",
        "modify"
    ]

    # Carrito actual
    cart: list[OrderItem]
    order_id: str | None   # Seteado al confirmar

    # Contexto comprimido (nunca el historial raw)
    summary: str           # Resumen ≤ 200 tokens
    turns_since_summary: int

    # Mensaje actual del cliente
    pending_message: str

    # Resultado del último nodo (para routing)
    last_node_result: str | None

    # Mensajes (solo los últimos N, no el historial completo)
    messages: Annotated[list, add_messages]
```

### 10.3 Nodos del grafo

| Nodo | Responsabilidad | Modelo usado |
|---|---|---|
| `classify_intent` | Determina qué sub-agente debe responder | claude-haiku-4-5 |
| `greeting_agent` | Saluda, identifica cliente, introduce flujo | claude-sonnet-4 |
| `catalog_agent` | Presenta catálogo, filtra por categoría, responde dudas de productos | claude-sonnet-4 + tool `catalog_search` |
| `order_agent` | Parsea artículos del mensaje, ejecuta RAG matching | claude-sonnet-4 + tool `match_product` |
| `confirm_agent` | Presenta resumen del carrito, pide confirmación, guarda pedido | claude-sonnet-4 + tool `save_order` |
| `modify_agent` | Permite cambios post-confirmación | claude-sonnet-4 + tool `update_order` |
| `summarize_node` | Comprime historial cuando `turns_since_summary >= 8` | claude-haiku-4-5 |
| `escalate_node` | Notifica cliente + alerta interna cuando hay fallo reiterado | Sin LLM — lógica determinista |

### 10.4 Grafo de transiciones

```
START
  └─→ classify_intent
        ├─→ greeting_agent    (phase = greeting)
        ├─→ catalog_agent     (phase = catalog)
        ├─→ order_agent       (phase = ordering)
        ├─→ confirm_agent     (phase = confirm_pending)
        ├─→ modify_agent      (phase = modify / confirmed)
        └─→ escalate_node     (intent = unknown × 2)

Cada agente → summarize_node (si turns_since_summary >= 8)
Cada agente → END (respuesta enviada, espera próximo mensaje)
```

### 10.5 System prompt del rol (compartido, cacheado)

El system prompt define el rol del preventista y se mantiene **idéntico** para todas las conversaciones para activar prompt caching de Anthropic (el bloque cacheado se cobra a 0.10x del precio base).

```
Sos un preventista de Distribuidora BADIE S.A., empresa distribuidora de 
cerveza Salta y otras bebidas en Argentina. Tu rol es atender clientes 
mayoristas (almacenes, kioscos, supermercados) y tomar pedidos por WhatsApp.

Reglas:
- Hablás en español rioplatense informal pero profesional
- No negociás precios, condiciones comerciales ni crédito
- Si no entendés un producto después de dos intentos, escalás a un 
  preventista humano
- Sos cordial, eficiente y vas al punto — el cliente no tiene tiempo
- Nunca inventás productos ni precios que no están en el catálogo
```

---

## 11. Estrategia RAG y matching de productos

### 11.1 Problema

Los clientes piden en lenguaje coloquial que no coincide con las descripciones formales del catálogo del ERP:

| El cliente dice | El sistema tiene |
|---|---|
| "dos cajones de la rubia" | `CERVEZA SALTA RUBIA 970ML RETORNABLE X12 - SKU: CS-RUB-970-R` |
| "un six de la sin alcohol" | `CERVEZA SALTA SIN ALCOHOL 340ML X6 - SKU: CS-SA-340-6` |
| "tres aguas con gas" | `AGUA MINERAL CON GAS 500ML - SKU: AGM-GAS-500` |

### 11.2 Solución: RAG con pgvector

El catálogo se embeddea una vez (y se re-embeddea cuando hay cambios). Cada artículo genera un embedding que combina: nombre, categoría, presentación, sinónimos y términos coloquiales curados.

**Texto de embedding por artículo:**
```
Cerveza Salta Rubia 970ml retornable caja 12 unidades.
Sinónimos: rubia, cerveza salta, salta rubia, cajón de rubia,
cerveza retornable, 970, litro, caja rubia.
Categoría: cerveza rubia nacional.
```

**Flujo de matching en `order_agent`:**
```
1. Recibir texto del cliente: "dame dos cajones de la rubia"
2. Extraer expresión de producto con LLM (Haiku): "cajones de la rubia"
3. Embed de esa expresión (text-embedding-3-small)
4. Búsqueda en pgvector: top-3 por similitud coseno (umbral ≥ 0.82)
5. Si score top-1 ≥ 0.92: match directo → agregar al carrito
6. Si score entre 0.82 y 0.92: mostrar 2-3 opciones al cliente
7. Si score < 0.82: preguntar "¿Me podés especificar qué producto querés?"
8. Si falla dos veces: escalar a humano
```

### 11.3 Tabla de embeddings (pgvector)

```sql
CREATE TABLE catalog_embeddings (
    id              SERIAL PRIMARY KEY,
    sku             VARCHAR(50) NOT NULL UNIQUE,
    description     TEXT NOT NULL,         -- texto de embedding
    embedding       vector(1536),           -- dimensión según modelo
    active          BOOLEAN DEFAULT TRUE,
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX ON catalog_embeddings USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);
```

### 11.4 Actualización del índice

El catálogo se re-embeddea automáticamente cuando hay cambios en la tabla de productos (via trigger de PostgreSQL + tarea Celery). La actualización es incremental: solo los artículos modificados o nuevos se re-embeddean.

---

## 12. Optimización de tokens y costos

### 12.1 Principio central

> El LLM nunca ve el historial crudo. Solo ve el estado comprimido más el mensaje actual.

### 12.2 Técnicas aplicadas

**Compresión periódica del historial**

Cada 8 turnos, el nodo `summarize_node` llama a claude-haiku-4-5 para reemplazar el historial acumulado por un resumen estructurado de ≤ 200 tokens. El historial raw se archiva en PostgreSQL para auditoría.

```python
SUMMARIZE_PROMPT = """
Resumí la conversación de venta en ≤ 200 tokens con este formato:
- Cliente: {nombre}, {tipo de negocio}
- Estado del pedido: {artículos en carrito o "vacío"}
- Último punto de la conversación: {frase}
- Próximo paso esperado: {frase}
"""
```

**Routing por modelo**

| Tarea | Modelo | Costo estimado |
|---|---|---|
| Clasificación de intención | claude-haiku-4-5 | ~$0.0001 / call |
| RAG: extracción de expresión | claude-haiku-4-5 | ~$0.0001 / call |
| Generación de respuesta | claude-sonnet-4 | ~$0.001 / call |
| Compresión de historial | claude-haiku-4-5 | ~$0.0002 / call |

**Prompt caching (Anthropic)**

El system prompt del rol (~500 tokens) se marca como `cache_control: ephemeral`. Todas las conversaciones reutilizan ese bloque cacheado → costo 0.10x sobre ese bloque.

**Tools en lugar de catálogo en contexto**

El catálogo nunca se inyecta en el prompt. El agente llama a `catalog_search(query)` como tool y recibe solo los 3-5 resultados relevantes.

### 12.3 Estimación de costo por conversación completa

```
Clasificación × 3 llamadas:   3 × $0.0001  = $0.0003
Respuesta × 4 llamadas:       4 × $0.001   = $0.004
Compresión × 1 llamada:       1 × $0.0002  = $0.0002
Embeddings (RAG) × 3 queries: negligible   = ~$0.00002
─────────────────────────────────────────────────────
Total estimado por conversación:             ~$0.0045
Total estimado 1.000 clientes/día:           ~$4.50/día → ~$135/mes
```

---

## 13. Integración WhatsApp Business API

### 13.1 Setup requerido

- Cuenta de Meta Business Manager verificada
- Número de teléfono dedicado para el bot (no puede ser el mismo que usa un humano)
- BSP (Business Solution Provider): se recomienda **360dialog** para Argentina (soporte en español, pricing por sesión) o **Twilio** (más flexible, mejor SDK)
- Webhook HTTPS en endpoint público con SSL válido

### 13.2 Tipos de mensajes

WhatsApp Business API diferencia dos tipos de conversación con costos distintos:

| Tipo | Cuándo aplica | Costo |
|---|---|---|
| **Service conversation** | El cliente escribe primero (dentro de ventana de 24h) | Gratis o muy bajo |
| **Marketing / Utility conversation** | El bot escribe primero (plantilla pre-aprobada) | ~$0.04-0.08 / conversación |

**Estrategia:** El bot responde siempre — nunca inicia. Si se necesita notificar al cliente (recordatorio de pedido, confirmación), usar plantillas pre-aprobadas del tipo "utility".

### 13.3 Rate limits y gestión de errores

- Meta impone límites de mensajes por número por hora (tier-based, empieza en 1.000/h)
- Implementar backoff exponencial ante errores 429 o 500 de Meta
- Queue con Celery para mensajes que llegan en rafagas

### 13.4 Validación del webhook

```python
import hmac, hashlib

def verify_webhook_signature(payload: bytes, signature: str, secret: str) -> bool:
    expected = hmac.new(
        secret.encode(), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)
```

---

## 14. Modelo de datos

### 14.1 Tablas principales

```sql
-- Clientes (puede existir ya en el ERP — mapear o extender)
CREATE TABLE clients (
    id              SERIAL PRIMARY KEY,
    phone_number    VARCHAR(20) UNIQUE NOT NULL,  -- E.164 format
    name            VARCHAR(200) NOT NULL,
    business_type   VARCHAR(100),                 -- almacén, kiosco, etc.
    zone            VARCHAR(100),
    price_list_id   INT REFERENCES price_lists(id),
    active          BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Pedidos
CREATE TABLE orders (
    id              SERIAL PRIMARY KEY,
    external_id     VARCHAR(50) UNIQUE,           -- número visible al cliente
    client_id       INT REFERENCES clients(id),
    status          VARCHAR(20) DEFAULT 'pending', -- pending, confirmed, modified, cancelled
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    confirmed_at    TIMESTAMPTZ,
    cutoff_at       TIMESTAMPTZ,                  -- hasta cuándo se puede modificar
    total_amount    NUMERIC(12, 2),
    notes           TEXT
);

-- Items del pedido
CREATE TABLE order_items (
    id              SERIAL PRIMARY KEY,
    order_id        INT REFERENCES orders(id),
    sku             VARCHAR(50) NOT NULL,
    description     VARCHAR(300),
    quantity        INT NOT NULL,
    unit_price      NUMERIC(10, 2),
    subtotal        NUMERIC(12, 2)
);

-- Historial de conversaciones (para auditoría y futuro fine-tuning)
CREATE TABLE conversation_logs (
    id              SERIAL PRIMARY KEY,
    thread_id       VARCHAR(50) NOT NULL,          -- phone_number
    client_id       INT REFERENCES clients(id),
    role            VARCHAR(10) NOT NULL,           -- 'user' o 'assistant'
    content         TEXT NOT NULL,
    tokens_used     INT,
    model_used      VARCHAR(50),
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Embeddings del catálogo (pgvector)
CREATE TABLE catalog_embeddings (
    id              SERIAL PRIMARY KEY,
    sku             VARCHAR(50) NOT NULL UNIQUE,
    description     TEXT NOT NULL,
    embedding       vector(1536),
    active          BOOLEAN DEFAULT TRUE,
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);
```

---

## 15. Limitaciones y riesgos

### 15.1 Limitaciones técnicas conocidas

| Limitación | Descripción | Mitigación |
|---|---|---|
| Latencia LLM | Cada respuesta agrega 1-3 segundos | Aceptable para WhatsApp; indicar con "escribiendo..." |
| Matching imperfecto | RAG no cubre 100% de expresiones coloquiales | Curar sinónimos manualmente; agregar feedback loop |
| WhatsApp rate limits | Meta limita throughput por número | Escalar tier del WABA; BSP con múltiples números si necesario |
| Sin contexto de precios personalizados | v1.0 usa lista de precios estándar | v2.0 puede incorporar precio por cliente via BD |
| Ventana de contexto | Conversaciones muy largas saturan el contexto | Compresión periódica mitiga; hard limit de 20 turnos/sesión |

### 15.2 Riesgos del proyecto

| Riesgo | Probabilidad | Impacto | Plan de contingencia |
|---|---|---|---|
| Rechazo del cliente al bot | Media | Alto | Piloto con 50 clientes seleccionados; rollback a preventista humano inmediato |
| Costo LLM mayor al estimado | Media | Medio | Monitoreo diario de gasto; ajuste de modelo/compresión |
| Incumplimiento de policies de Meta | Baja | Alto | Revisión legal de mensajes; uso de plantillas aprobadas para outbound |
| Fallos del servicio LLM (Anthropic/OpenAI) | Baja | Alto | Fallback a modelo alternativo; respuesta de error amigable |
| Pérdida de datos en Redis | Baja | Medio | Persistencia de Redis (AOF); checkpoint periódico en PostgreSQL |

---

## 16. Path de desarrollo (roadmap)

### Fase 1 — MVP (semanas 1-3)

**Objetivo:** Bot funcional end-to-end con un único agente simplificado.

- [ ] Setup WhatsApp Business API + webhook FastAPI
- [ ] Modelo de datos en PostgreSQL (clients, orders, order_items)
- [ ] Agente LangGraph de un solo nodo (greeting → ordering → confirm)
- [ ] RAG básico con pgvector (embedding del catálogo existente)
- [ ] Estado en Redis con TTL 12h
- [ ] Test con 10 clientes internos (equipo BADIE)
- [ ] Logging básico de conversaciones

**Entregable:** Bot que toma un pedido simple de punta a punta.

### Fase 2 — Multi-agente y robustez (semanas 4-6)

**Objetivo:** Arquitectura multi-agente completa, escalado a 100 clientes reales.

- [ ] Refactorizar a Supervisor + sub-agentes especializados
- [ ] Implementar `summarize_node` y compresión de historial
- [ ] Agregar `modify_agent` para post-cierre
- [ ] Implementar `escalate_node` con notificación a Slack
- [ ] Rate limiting por cliente
- [ ] Tests de carga: 100 conversaciones simultáneas
- [ ] Piloto con 50 clientes reales seleccionados

**Entregable:** Sistema multi-agente en producción controlada.

### Fase 3 — Optimización y monitoreo (semanas 7-8)

**Objetivo:** Controlar costos a escala, tener visibilidad del sistema.

- [ ] Activar prompt caching en Anthropic API
- [ ] Routing diferenciado por modelo (Haiku / Sonnet por tarea)
- [ ] Dashboard Plotly Dash: conversaciones, pedidos, tokens, escalados
- [ ] Métricas de calidad: tasa de matching, tasa de éxito de pedidos
- [ ] Alertas automáticas ante anomalías de costo o tasa de error

**Entregable:** Sistema observable con costo controlado.

### Fase 4 — Producción general (semana 9+)

**Objetivo:** Escalar a toda la cartera de clientes.

- [ ] Onboarding de todos los clientes activos
- [ ] Integración semi-automática con ChessERP (exportación de pedidos)
- [ ] Fine-tuning del prompt con base en conversaciones reales
- [ ] Curación continua de sinónimos del catálogo
- [ ] SLA formal y procedimiento de escalado documentado

---

## 17. Métricas de éxito

### 17.1 Métricas de negocio

| Métrica | Objetivo v1.0 | Cómo se mide |
|---|---|---|
| Tasa de pedidos completados sin intervención humana | ≥ 80% | `orders.status = confirmed` / total sesiones |
| Satisfacción del cliente (post-pedido) | ≥ 4/5 estrellas | Encuesta automática post-confirmación |
| Reducción de tiempo de carga manual | ≥ 60% | Comparativa vs línea base de preventista |
| Cobertura horaria de atención | 24/7 | Pedidos recibidos fuera de horario laboral |

### 17.2 Métricas técnicas

| Métrica | Objetivo | Alerta |
|---|---|---|
| Latencia p95 de respuesta | < 5 segundos | > 8 segundos |
| Tasa de matching RAG correcto | ≥ 90% | < 85% |
| Costo por conversación | < $0.005 USD | > $0.01 USD |
| Tasa de escalado a humano | < 10% | > 20% |
| Uptime del webhook | ≥ 99.5% | < 99% |
| Tokens consumidos por día | Monitoreo continuo | Spike > 2x del promedio |

---

## 18. Consideraciones legales y de privacidad

### 18.1 Ley 25.326 — Protección de Datos Personales (Argentina)

El sistema almacena conversaciones, nombres, números de teléfono y datos comerciales de clientes. Obligaciones:

- Los clientes deben ser informados de que interactúan con un sistema automatizado
- El uso de sus datos debe estar cubierto por el contrato comercial con BADIE o con consentimiento explícito
- Deben tener derecho de acceso, rectificación y eliminación de sus datos
- Los datos no pueden cederse a terceros sin consentimiento

### 18.2 Políticas de Meta / WhatsApp Business

- Prohibido usar WhatsApp Business API para spam o mensajes no solicitados
- Los mensajes iniciados por el negocio (outbound) deben usar plantillas pre-aprobadas
- Meta puede auditar el uso del número; el incumplimiento implica bloqueo del número
- Consultar [Meta Business Policy](https://www.facebook.com/policies/commerce/) antes del go-live

### 18.3 Transparencia con el cliente

Se recomienda incluir en el primer mensaje del bot una indicación clara:

> "Hola [Nombre], soy el asistente virtual de BADIE. Te ayudo a hacer tu pedido hoy. Si preferís hablar con un preventista, respondé HUMANO en cualquier momento."

---

## 19. Glosario

| Término | Definición |
|---|---|
| **Preventista** | Vendedor de campo de BADIE que visita clientes y toma pedidos |
| **Thread ID** | Identificador único de conversación en LangGraph; en este sistema = número de teléfono del cliente |
| **RAG (Retrieval-Augmented Generation)** | Técnica que combina búsqueda vectorial con LLM para responder con información de una base de conocimiento propia |
| **Embedding** | Representación numérica de un texto en un espacio vectorial de alta dimensión; permite buscar por similitud semántica |
| **pgvector** | Extensión de PostgreSQL que permite almacenar y consultar vectores (embeddings) directamente en la BD relacional |
| **LangGraph** | Framework de orquestación de agentes LLM basado en grafos de estados con checkpointing |
| **Checkpointing** | Persistencia del estado del grafo en un store externo (Redis) para que la conversación sobreviva reinicios |
| **Prompt caching** | Feature de Anthropic que permite cachear bloques estáticos del prompt y cobrarlos a 0.10x del precio normal |
| **WABA** | WhatsApp Business Account — cuenta de negocio en Meta para usar la API oficial |
| **BSP** | Business Solution Provider — proveedor intermediario autorizado por Meta para acceder a la API de WhatsApp |
| **Carrito (cart)** | Lista de artículos que el cliente ha pedido durante la conversación, antes de confirmar |
| **Corte** | Hora límite del día hasta la cual se pueden recibir o modificar pedidos |
| **SKU** | Stock Keeping Unit — código único que identifica un artículo en el catálogo |
| **Escalado** | Proceso de transferir la conversación de un agente automático a un preventista humano |

---

*Documento vivo — actualizar con cada cambio de arquitectura o decisión de diseño relevante.*  
*Próxima revisión: al cierre de Fase 1 (MVP).*
