# Badie — Documentación Técnica Completa

> WhatsApp Sales Agent para **Distribuidora BADIE S.A.** (Grupo Manzur)
> Bot conversacional que reemplaza el levantamiento manual de pedidos por preventistas, atendiendo a puntos de venta vía WhatsApp Business API.

---

## Índice

1. [Visión General](#1-visión-general)
2. [Stack Tecnológico](#2-stack-tecnológico)
3. [Arquitectura](#3-arquitectura)
4. [Estructura del Proyecto](#4-estructura-del-proyecto)
5. [Modelo de Datos](#5-modelo-de-datos)
6. [Endpoints HTTP](#6-endpoints-http)
7. [Servicios Internos](#7-servicios-internos)
8. [Observabilidad](#8-observabilidad)
9. [Configuración](#9-configuración)
10. [Tests](#10-tests)
11. [Setup Local](#11-setup-local)
12. [CI/CD](#12-cicd)
13. [Roadmap y Estado](#13-roadmap-y-estado)
14. [Decisiones Arquitectónicas Clave](#14-decisiones-arquitectónicas-clave)
15. [Workflow SDD](#15-workflow-sdd)
16. [Glosario](#16-glosario)

---

## 1. Visión General

### Empresa

**Distribuidora BADIE S.A.** (parte de Grupo Manzur). Distribuidora de bebidas en Argentina — cervezas, gaseosas, aguas, jugos. Catálogo dominado por marcas como **Quilmes**, **Brahma**, **Stella Artois**, **CCU**, **Branca**.

Tiene una red de **preventistas** (vendedores en la calle) que visitan puntos de venta — kioscos, almacenes, bares, restaurantes — y levantan pedidos manualmente. El bot busca digitalizar este proceso para que el cliente pueda hacer el pedido por WhatsApp en cualquier momento.

### Objetivo del Bot

- Recibir mensajes de WhatsApp de los clientes registrados
- Entender pedidos en lenguaje coloquial argentino: *"dame dos cajones de la rubia"* → 2 cajones de Quilmes
- Buscar productos por similitud semántica (RAG) sobre el catálogo embebido
- Aplicar lista de precios correcta según el cliente
- Confirmar el pedido y persistirlo
- Eventualmente integrarse al ERP de BADIE

### Principios Rectores

| Principio | Aplicación |
|-----------|------------|
| **Foundations first** | Async-first, structured logging, type-safe (mypy strict) desde día 1 |
| **Spec-Driven Development** | Cada feature pasa por explore → spec → design → tasks → apply → verify |
| **Strict TDD** | Tests RED antes de implementación |
| **Fail-open en lookups** | Si Redis o DB caen, el mensaje se procesa igual |
| **Observabilidad por default** | Cada request tiene `request_id` correlacionado en todos los logs |

---

## 2. Stack Tecnológico

### Runtime

| Componente | Versión | Rol |
|------------|---------|-----|
| **Python** | 3.12+ | Lenguaje |
| **uv** | latest | Gestor de paquetes y entornos |
| **FastAPI** | `>=0.115,<1.0` | Framework HTTP async |
| **Uvicorn** | `>=0.34` | ASGI server |
| **SQLAlchemy** | `>=2.0` async | ORM con asyncpg |
| **asyncpg** | `>=0.30` | Driver Postgres async |
| **pgvector** | `>=0.3` | Vectores en Postgres (HNSW) |
| **redis** | `>=5.0` async | Cache, dedup, state |
| **structlog** | `>=24.0` | JSON logging |
| **pydantic-settings** | `>=2.0` | Config desde env/dotenv |

### Inteligencia Artificial

| Componente | Rol |
|------------|-----|
| **LangGraph** `>=0.2` | Orquestación de agentes (state machine) |
| **langgraph-checkpoint-redis** | Persistencia de conversaciones |
| **langchain-anthropic** | Cliente para Claude (LLM conversacional) |
| **langchain-openai** | Cliente para OpenAI (embeddings) |
| **Claude Sonnet 4** | LLM principal — diálogo, comprensión |
| **Claude Haiku 4.5** | LLM auxiliar — clasificación, resúmenes |
| **text-embedding-3-small** | Embeddings 512d (Matryoshka) — LOCKED |

### Infraestructura externa

| Servicio | Uso |
|----------|-----|
| **PostgreSQL 17 + pgvector** | DB principal: clients, orders, conversation_logs, catalog_embeddings |
| **Redis 7+** | Dedup webhooks, checkpointing LangGraph, sessions |
| **Meta WhatsApp Cloud API** | Recepción y envío de mensajes |
| **GitHub Actions** | CI (lint + types + tests) |

### Tooling de desarrollo

| Tool | Función |
|------|---------|
| **pytest** `>=9` + `pytest-asyncio` | Tests unitarios + integración async |
| **aiosqlite** | DB in-memory para tests |
| **ruff** `>=0.15.9` | Linter |
| **mypy** `>=1.13` (strict) | Type checking |

---

## 3. Arquitectura

### Vista de alto nivel

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         META WHATSAPP CLOUD API                         │
│                       (developers.facebook.com)                         │
└──────────┬──────────────────────────────────┬───────────────────────────┘
           │ GET /webhook                     │ POST /webhook
           │ (challenge handshake)            │ (mensajes entrantes + HMAC)
           ▼                                  ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                        FastAPI Application                              │
│                         (main.py: create_app)                           │
│                                                                         │
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │  RequestIdMiddleware                                              │  │
│  │  - genera request_id (8-char UUID)                                │  │
│  │  - bind structlog contextvars → log JSON estructurado             │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│                                                                         │
│  ┌─────────────────┐  ┌────────────────────────────────────────────┐    │
│  │ /health         │  │ webhook_router (APIRouter prefix=/webhook) │    │
│  │ - probe pg+redis│  │  GET / → challenge handshake               │    │
│  │ - timeout 3s    │  │  POST / → flow completo                    │    │
│  └─────────────────┘  └────────────────────────────────────────────┘    │
│                                                                         │
│  ┌──────────────────────────────────────────────────────────────────┐   │
│  │ Lifespan (startup/shutdown)                                       │   │
│  │   startup:  app.state.engine = get_engine(database_url)           │   │
│  │   shutdown: engine.dispose() + close_redis_pool()                 │   │
│  └──────────────────────────────────────────────────────────────────┘   │
└──────┬───────────────────────────────────────────────────┬──────────────┘
       │                                                   │
       ▼                                                   ▼
┌──────────────────────────┐                ┌────────────────────────────┐
│   PostgreSQL 17 + pgvec  │                │   Redis 7                  │
│                          │                │                            │
│   5 tablas ORM:          │                │   Connection pool          │
│   - clients              │                │   singleton                │
│   - orders               │                │   - dedup keys (TTL 5min)  │
│   - order_items          │                │   - LangGraph checkpoints  │
│   - conversation_logs    │                │     (próximo paso)         │
│   - catalog_embeddings   │                │                            │
│     (Vector 512d HNSW)   │                │                            │
└──────────────────────────┘                └────────────────────────────┘
```

### Pipeline de procesamiento del POST `/webhook`

```
POST /webhook
  │
  ├─[middleware]─► genera request_id, binds a structlog
  │
  ├─[1]─► await request.body()         ← bytes crudos (NO request.json() todavía)
  │
  ├─[2]─► verify_signature(body, headers, secret)
  │       └─ HMAC-SHA256 + compare_digest (timing-safe)
  │       └─ 403 si falla
  │
  ├─[3]─► json.loads(body)
  │       └─ navigate entry[0].changes[0].value
  │
  ├─[4]─► filter status updates (no key 'messages')
  │       └─ return early si es status (delivered/read)
  │
  ├─[5]─► extract message[0]: phone, message_id, text, timestamp
  │
  ├─[6]─► dedup: SET dedup:{message_id} "1" NX EX 300
  │       ├─ True (nuevo)        → continuar
  │       ├─ None (duplicado)    → log + return early
  │       └─ Error Redis         → log warning + continuar (fail-open)
  │
  ├─[7]─► normalize_phone("5491123456789") → "+5491123456789"
  │
  ├─[8]─► lookup_or_create_client(session, phone)
  │       ├─ Found + active=True  → continuar
  │       ├─ Found + active=False → log + return early
  │       ├─ Not found            → crear con active=False → log + return early
  │       └─ DB error             → log warning + continuar (fail-open)
  │
  ├─[9]─► log "webhook.message_received"
  │
  └──────► return {"status": "ok"}
```

### Capas (cuando esté completo)

```
┌─────────────────────────────────────────────────────┐
│ Layer 1: INTEGRATION (external boundary)            │
│  - integration/webhook.py     — recv from Meta       │
│  - integration/whatsapp_client.py — send to Meta    │
│  - integration/meta_signature.py — HMAC             │
└─────────────────────────────────────────────────────┘
                        ↕
┌─────────────────────────────────────────────────────┐
│ Layer 2: AGENT (LangGraph orchestration) [scaffold]  │
│  - agent/graph.py    — state machine                │
│  - agent/state.py    — TypedDict shape              │
│  - agent/nodes/      — router, retrieval, gen, etc. │
│  - agent/prompts/    — system + few-shot            │
└─────────────────────────────────────────────────────┘
                        ↕
┌─────────────────────────────────────────────────────┐
│ Layer 3: SERVICES (business logic)                  │
│  - services/dedup.py     — Redis SET NX             │
│  - services/clients.py   — lookup, normalize        │
│  - services/redis.py     — pool singleton           │
│  - services/catalog.py   — vector search [scaffold] │
│  - services/orders.py    — CRUD pedidos [scaffold]  │
│  - services/rag.py       — retrieval+gen [scaffold] │
└─────────────────────────────────────────────────────┘
                        ↕
┌─────────────────────────────────────────────────────┐
│ Layer 4: DATA (persistence)                         │
│  - models/base.py      — engine, session, Base      │
│  - models/tables.py    — 5 ORM models               │
└─────────────────────────────────────────────────────┘
                        ↕
┌─────────────────────────────────────────────────────┐
│ CROSS-CUTTING                                       │
│  - config.py                — Settings singleton    │
│  - observability/logging    — structlog             │
│  - observability/middleware — RequestIdMiddleware   │
└─────────────────────────────────────────────────────┘
```

---

## 4. Estructura del Proyecto

```
agents-badie/
├── src/badie/                          # Código fuente
│   ├── __init__.py
│   ├── main.py                         # FastAPI factory + lifespan + /health
│   ├── config.py                       # Settings (pydantic-settings, @lru_cache)
│   │
│   ├── observability/
│   │   ├── __init__.py                 # Re-exports setup_logging, middleware
│   │   ├── logging.py                  # structlog config + ContextVars
│   │   └── middleware.py               # RequestIdMiddleware
│   │
│   ├── integration/                    # Capa de integración externa
│   │   ├── __init__.py                 # Exports webhook_router
│   │   ├── webhook.py                  # GET + POST /webhook
│   │   ├── meta_signature.py           # HMAC-SHA256 verify
│   │   └── whatsapp_client.py          # [scaffold] envío a Meta
│   │
│   ├── services/                       # Lógica de negocio
│   │   ├── __init__.py
│   │   ├── redis.py                    # Pool singleton async
│   │   ├── dedup.py                    # is_duplicate (SET NX EX 300)
│   │   ├── clients.py                  # normalize_phone + lookup_or_create
│   │   ├── catalog.py                  # [scaffold] búsqueda vectorial
│   │   ├── orders.py                   # [scaffold] CRUD orders
│   │   └── rag.py                      # [scaffold] retrieval + generation
│   │
│   ├── models/
│   │   ├── __init__.py                 # Re-exports todos los modelos
│   │   ├── base.py                     # AsyncEngine, AsyncSession factory, Base
│   │   └── tables.py                   # 5 modelos ORM
│   │
│   └── agent/                          # [scaffold] LangGraph
│       ├── __init__.py
│       ├── graph.py                    # state machine
│       ├── state.py                    # TypedDict
│       ├── nodes/__init__.py
│       └── prompts/__init__.py
│
├── tests/                              # Test suite
│   ├── conftest.py                     # Fixtures globales
│   ├── payloads/                       # Mock JSONs de Meta
│   │   ├── text_message.json
│   │   └── status_update.json
│   ├── test_config.py                  # Settings (2 tests)
│   ├── test_health.py                  # /health + middleware (5 tests)
│   ├── test_models.py                  # ORM definitions (8 tests)
│   ├── test_redis.py                   # Pool singleton (3 tests)
│   ├── test_dedup.py                   # is_duplicate (3 tests)
│   ├── test_client_lookup.py           # normalize + lookup (5 tests)
│   └── test_webhook.py                 # GET + POST /webhook (16 tests)
│
├── scripts/                            # Scripts ejecutables
│   ├── init_db.py                      # [scaffold] crear tablas
│   └── embed_catalog.py                # [scaffold] embed catalog
│
├── openspec/                           # Artefactos SDD (legacy/manual)
│   └── changes/
│       ├── arquitectura/
│       ├── scaffold-paso-0.1/
│       ├── config-paso-0.2/
│       ├── database-paso-0.3/
│       └── redis-paso-0.4/
│
├── .github/workflows/ci.yml            # Pipeline CI
├── .atl/skill-registry.md              # SDD skill registry
├── .engram/                            # Memoria persistente entre sesiones
├── pyproject.toml                      # Project + deps + tool config
├── uv.lock                             # Lockfile
├── README.md
├── DOCUMENTACION.md                    # Este archivo
└── PRD_WhatsApp_Sales_Agent.md         # Requerimientos originales (español)
```

---

## 5. Modelo de Datos

### Diagrama relacional

```
┌──────────────────┐       ┌──────────────────┐       ┌──────────────────┐
│   clients        │ 1   N │   orders         │ 1   N │   order_items    │
├──────────────────┤───────├──────────────────┤───────├──────────────────┤
│ id (PK)          │       │ id (PK)          │       │ id (PK)          │
│ phone_number ★   │       │ external_id (U)  │       │ order_id (FK)    │
│ name             │       │ client_id (FK)   │       │ sku              │
│ business_type    │       │ status           │       │ description      │
│ zone             │       │ created_at       │       │ quantity         │
│ price_list_id    │       │ confirmed_at     │       │ unit_price       │
│ active           │       │ cutoff_at        │       │ subtotal         │
│ created_at       │       │ total_amount     │       └──────────────────┘
└────────┬─────────┘       │ notes            │
         │ 1               └──────────────────┘
         │
         │ N
┌────────▼─────────┐       ┌──────────────────┐
│ conversation_logs│       │catalog_embeddings│
├──────────────────┤       ├──────────────────┤
│ id (PK)          │       │ id (PK)          │
│ thread_id        │       │ sku (UNIQUE)     │
│ client_id (FK)   │       │ description      │
│ role             │       │ embedding ◆      │
│ content          │       │ active           │
│ tokens_used      │       │ updated_at       │
│ model_used       │       └──────────────────┘
│ created_at       │       ◆ Vector(512) HNSW
└──────────────────┘
★ ix_clients_phone_number (índice no-único)
```

### Detalle de tablas

#### `clients`

Punto de venta (kiosco, almacén, bar, etc.) identificado por número de teléfono normalizado E.164.

| Columna | Tipo | Constraint | Descripción |
|---------|------|------------|-------------|
| `id` | INTEGER PK autoincr | NOT NULL | ID interno |
| `phone_number` | VARCHAR(20) | NULL, indexed | Teléfono E.164 (`+5491123456789`) |
| `name` | VARCHAR(200) | NOT NULL | Nombre — `"Pendiente de alta"` para auto-registrados |
| `business_type` | VARCHAR(100) | NULL | Ej: kiosco, bar, almacén |
| `zone` | VARCHAR(100) | NULL | Zona geográfica |
| `price_list_id` | INTEGER | NULL | Lista de precios asignada (FK lógico al warehouse) |
| `active` | BOOLEAN | DEFAULT true | `false` = no registrado, no atender |
| `created_at` | TIMESTAMPTZ | server_default now() | |

**Índice**: `ix_clients_phone_number` (no único — un teléfono podría asociarse a múltiples clientes en teoría)

**Política de auto-register**: cuando llega un mensaje de un teléfono desconocido, se crea un `Client` con `active=False`, `name="Pendiente de alta"`. El bot NO conversa con ellos hasta que alguien los habilita manualmente.

#### `orders`

Pedido de compra creado en una conversación.

| Columna | Tipo | Constraint | Descripción |
|---------|------|------------|-------------|
| `id` | INTEGER PK autoincr | NOT NULL | |
| `external_id` | VARCHAR(50) | UNIQUE NULL | ID externo (ERP) cuando se sincronice |
| `client_id` | INTEGER FK → clients.id | NULL | |
| `status` | VARCHAR(20) | DEFAULT 'pending' | `pending`/`confirmed`/`cancelled` |
| `created_at` | TIMESTAMPTZ | server_default now() | |
| `confirmed_at` | TIMESTAMPTZ | NULL | |
| `cutoff_at` | TIMESTAMPTZ | NULL | Deadline de corte de pedidos |
| `total_amount` | NUMERIC(12,2) | NULL | |
| `notes` | TEXT | NULL | |

#### `order_items`

Línea de un pedido.

| Columna | Tipo | Constraint |
|---------|------|------------|
| `id` | INTEGER PK autoincr | NOT NULL |
| `order_id` | INTEGER FK → orders.id | NULL |
| `sku` | VARCHAR(50) | NOT NULL |
| `description` | VARCHAR(300) | NULL |
| `quantity` | INTEGER | NOT NULL |
| `unit_price` | NUMERIC(10,2) | NULL |
| `subtotal` | NUMERIC(12,2) | NULL |

#### `conversation_logs`

Histórico de mensajes para auditoría y futura fine-tuning.

| Columna | Tipo | Constraint |
|---------|------|------------|
| `id` | INTEGER PK autoincr | NOT NULL |
| `thread_id` | VARCHAR(50) | NOT NULL |
| `client_id` | INTEGER FK → clients.id | NULL |
| `role` | VARCHAR(10) | NOT NULL — `user`/`assistant` |
| `content` | TEXT | NOT NULL |
| `tokens_used` | INTEGER | NULL |
| `model_used` | VARCHAR(50) | NULL |
| `created_at` | TIMESTAMPTZ | server_default now() |

#### `catalog_embeddings`

Vectores semánticos del catálogo de productos para RAG.

| Columna | Tipo | Constraint |
|---------|------|------------|
| `id` | INTEGER PK autoincr | NOT NULL |
| `sku` | VARCHAR(50) | UNIQUE NOT NULL |
| `description` | TEXT | NOT NULL |
| `embedding` | `Vector(512)` | NULL |
| `active` | BOOLEAN | DEFAULT true |
| `updated_at` | TIMESTAMPTZ | server_default now() |

**Índice HNSW**:
```sql
CREATE INDEX ix_catalog_embeddings_hnsw
ON catalog_embeddings USING hnsw (embedding vector_cosine_ops)
WITH (m = 16, ef_construction = 64);
```

> ¿Por qué HNSW y no IVFFlat? IVFFlat es **patológico** para catálogos pequeños (<10k items) porque agrupa en clusters que terminan siendo poco discriminativos. HNSW siempre funciona bien y escala mejor.

> ¿Por qué 512 dims? `text-embedding-3-small` por default tira 1536 dims pero soporta **Matryoshka reduction** — pedirle 512 dims directamente sin perder casi nada de calidad. Menos espacio, más rápido.

---

## 6. Endpoints HTTP

### `GET /health`

Health check con probes reales a Postgres y Redis.

**Response 200**:
```json
{
  "status": "ok",            // o "degraded"
  "environment": "development",
  "postgres": "ok",          // o "error"
  "redis": "ok"              // o "error"
}
```

**Lógica**:
- `SELECT 1` contra Postgres con timeout 3s
- `redis.ping()` con timeout 3s
- `status` = `"ok"` SOLO si ambos están `"ok"`. Si alguno falla → `"degraded"`
- Siempre responde 200 (no es 503 — es info para load balancers o monitoring)

### `GET /webhook`

Handshake de verificación de Meta.

**Query params**:
- `hub.mode` — debe ser `"subscribe"`
- `hub.verify_token` — debe coincidir con `WHATSAPP_VERIFY_TOKEN` del config
- `hub.challenge` — string que tenemos que devolver tal cual

**Responses**:
- `200` + body = `hub.challenge` (text/plain) → si todo OK
- `400` → falta `hub.challenge`
- `403` → token incorrecto o `hub.mode != "subscribe"`

### `POST /webhook`

Recepción de mensajes y status updates de Meta.

**Headers requeridos**:
- `X-Hub-Signature-256: sha256=<hex>` — HMAC-SHA256 del body con `META_WEBHOOK_SECRET`

**Body** (ejemplo simplificado):
```json
{
  "entry": [{
    "changes": [{
      "value": {
        "messages": [{
          "from": "5491123456789",
          "id": "wamid.ABGGFlA5FpafAgo6tHcNmNjXmuSf",
          "timestamp": "1669058305",
          "text": {"body": "dame dos cajones de la rubia"},
          "type": "text"
        }]
      }
    }]
  }]
}
```

**Responses**:
- `200 {"status": "ok"}` → procesado correctamente (o duplicado, o cliente no registrado, o status update — Meta SIEMPRE espera 200)
- `403` → firma HMAC inválida o ausente

**Estados posibles del log**:

| Evento de log | Cuándo |
|---------------|--------|
| `webhook.message_received` | Mensaje válido de cliente activo, listo para procesar |
| `webhook.duplicate_skipped` | message_id ya visto en últimos 5 min |
| `webhook.unregistered_client` | Cliente nuevo, auto-registrado como `active=False` |
| `client_lookup.db_error` | DB caída → fail-open, procesa igual |
| `dedup.redis_error` | Redis caído → fail-open, procesa igual |

---

## 7. Servicios Internos

### `services/redis.py`

Pool singleton de conexiones Redis async.

```python
get_redis_pool(url: str) -> ConnectionPool      # crea o retorna pool cacheado
get_redis_client(url: str) -> Redis             # client usando el pool
close_redis_pool() -> None                      # cleanup en shutdown
```

`decode_responses=True` por default. El pool global se invalida solo en shutdown (lifespan).

### `services/dedup.py`

Idempotencia de webhooks vía Redis SET NX.

```python
async def is_duplicate(redis_client: Redis, message_id: str) -> bool
```

**Algoritmo**:
1. `SET dedup:{message_id} "1" NX EX 300`
2. Si retorna truthy → key creado → mensaje NUEVO (return False)
3. Si retorna None → key existía → mensaje DUPLICADO (return True)
4. Si Redis falla → log warning + return False (**fail-open**: procesar igual)

**TTL**: 300 segundos = 5 minutos (matchea el retry window de Meta).

**Key format**: `dedup:wamid.ABGGFlA5FpafAgo6tHcNmNjXmuSf`

### `services/clients.py`

```python
def normalize_phone(raw: str) -> str
async def lookup_or_create_client(session: AsyncSession, phone: str) -> Client
```

**`normalize_phone`** (versión actual — naif, será reemplazada por `phonenumbers` en próximo paso):
- Si no empieza con `+` → agrega `+`
- Si ya tiene `+` → deja como está

**`lookup_or_create_client`**:
1. SELECT por `phone_number == phone`
2. Si existe → retorna
3. Si no existe → crea `Client(phone, name="Pendiente de alta", active=False)`, commit, retorna

### `integration/meta_signature.py`

```python
def verify_signature(body: bytes, headers: Headers, secret: str) -> None
```

1. Lee header `x-hub-signature-256`
2. Calcula HMAC-SHA256(body, secret)
3. `hmac.compare_digest()` (timing-safe) — si NO coincide → `HTTPException(403)`

**Por qué `compare_digest` y no `==`**: prevención de **timing attacks**. `==` short-circuita en cuanto encuentra diferencia; `compare_digest` SIEMPRE tarda lo mismo independiente de la posición del fallo.

### `integration/webhook.py`

Router con dos endpoints (ver sección 6 — Endpoints HTTP).

---

## 8. Observabilidad

### Logging estructurado (structlog)

**Output**: JSON newline-delimited a stdout. Cada línea es parseable por cualquier log aggregator (Datadog, Loki, ELK).

**Configuración** (`observability/logging.py`):
```python
processors = [
    contextvars.merge_contextvars,    # ← inyecta request_id automático
    add_log_level,
    TimeStamper(fmt="iso"),
    JSONRenderer(),
]
```

**ContextVars disponibles**:
- `request_id_ctx` — UUID 8-char, único por request
- `thread_id_ctx` — para conversaciones LangGraph (futuro)

### RequestIdMiddleware

Registrado a nivel app (`main.py`). En cada request:

1. Genera `request_id = uuid4().hex[:8]`
2. Bind a structlog contextvars
3. Log `request.started` con `method`, `path`
4. Llama el handler, mide tiempo
5. Log `request.completed` con `status_code`, `elapsed_ms`
6. `clear_contextvars()` en `finally`

### Ejemplo de log line

```json
{
  "event": "webhook.message_received",
  "request_id": "a3f8b2c1",
  "phone_number": "+5491123456789",
  "message_id": "wamid.ABGGFlA5FpafAgo6tHcNmNjXmuSf",
  "text": "dame dos cajones de la rubia",
  "timestamp": "1669058305",
  "level": "info",
  "log_time": "2026-04-30T15:32:11.456Z"
}
```

Con el `request_id` podés correlacionar TODOS los logs de un mismo request: el `request.started`, el `webhook.message_received`, las queries SQL, el `request.completed`. Trazabilidad completa.

---

## 9. Configuración

### Settings (`src/badie/config.py`)

Pydantic-settings con `@lru_cache` (singleton). Carga de env vars > `.env` > defaults.

```python
class Settings(BaseSettings):
    # Database
    database_url: str = "postgresql+asyncpg://localhost:5432/badie"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # LLM
    anthropic_api_key: str = ""
    openai_api_key: str = ""

    # WhatsApp / Meta
    meta_webhook_secret: str = ""
    meta_phone_number_id: str = ""
    meta_access_token: str = ""
    whatsapp_token: str = ""
    whatsapp_verify_token: str = ""
    whatsapp_phone_number_id: str = ""

    # Slack (alertas, opcional)
    slack_webhook_url: str = ""

    # App
    log_level: str = "INFO"
    debug: bool = False
    environment: str = "development"
```

### Variables requeridas para producción

| Variable | Descripción | Sensible |
|----------|-------------|----------|
| `DATABASE_URL` | Postgres con asyncpg driver | sí |
| `REDIS_URL` | Redis connection string | sí |
| `ANTHROPIC_API_KEY` | Para Claude (LLM) | sí |
| `OPENAI_API_KEY` | Para embeddings | sí |
| `META_WEBHOOK_SECRET` | HMAC secret del app de Meta | sí |
| `META_ACCESS_TOKEN` | Token para enviar mensajes | sí |
| `WHATSAPP_VERIFY_TOKEN` | Token del challenge GET | sí |
| `META_PHONE_NUMBER_ID` | ID del número de WhatsApp | no |
| `ENVIRONMENT` | `development` / `staging` / `production` | no |

### Singleton via `@lru_cache`

```python
@lru_cache
def get_settings() -> Settings:
    return Settings()
```

Cacheado por proceso. En tests usamos `get_settings.cache_clear()` antes/después de cada test (autouse fixture en cada test_*.py).

---

## 10. Tests

### Stack

- `pytest>=9` con `pytest-asyncio` mode `auto` (todo async sin `@pytest.mark.asyncio`)
- `httpx.AsyncClient` con `ASGITransport` para tests del API sin servidor
- `unittest.mock.AsyncMock` / `MagicMock` para Redis y SQLAlchemy
- `aiosqlite` para tests con DB real in-memory (queries verdaderas)
- `pytest.fixture(autouse=True)` para `get_settings.cache_clear()`

### Cobertura actual: **42 tests**

| Archivo | Tests | Qué cubre |
|---------|-------|-----------|
| `test_config.py` | 2 | Settings carga defaults, singleton cacheado |
| `test_health.py` | 5 | /health all-ok, postgres-degraded, redis-degraded, both-degraded, middleware |
| `test_models.py` | 8 | 5 tablas registradas, nombres, dim 512, relationships, índice phone |
| `test_redis.py` | 3 | Pool singleton, client creation |
| `test_dedup.py` | 3 | is_duplicate: nuevo, duplicado, fail-open |
| `test_client_lookup.py` | 5 | normalize_phone (2), lookup (3) |
| `test_webhook.py` | 16 | verify_signature (3), GET challenge (3), POST (10) |

### Patrón de test típico

```python
async def test_post_text_message(client: AsyncClient, text_payload: bytes):
    sig = sign_payload(text_payload, TEST_SECRET)
    response = await client.post(
        "/webhook",
        content=text_payload,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig},
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
```

### Fixtures clave

**`tests/test_client_lookup.py`** — DB SQLite in-memory:
```python
@pytest.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()
```

**`tests/test_webhook.py`** — App con mocks para engine y Redis:
```python
@pytest.fixture
def app():
    test_settings = make_settings()
    application = create_app()
    application.dependency_overrides[get_settings] = lambda: test_settings
    return application

@pytest.fixture
async def client(app):
    mock_engine = MagicMock()
    mock_engine.dispose = MagicMock(return_value=None)
    app.state.engine = mock_engine
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
```

### Comando para correr

```bash
uv run pytest                    # todos los tests
uv run pytest tests/test_webhook.py -v
uv run pytest -k dedup           # solo los que matchean "dedup"
```

### Quality gates locales

```bash
uv run ruff check .              # lint
uv run mypy src/                 # type checking strict
uv run pytest                    # tests
```

Los 3 deben pasar antes de cualquier commit. CI los corre automáticamente en cada push.

---

## 11. Setup Local

### Prerequisitos

- **Python 3.12+**
- **uv** (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- **Docker** (para Postgres + Redis locales)

### Instalación

```bash
# Clonar
git clone <repo>
cd agents-badie

# Instalar deps
uv sync --group dev

# Variables de entorno
cp .env.example .env  # editar con valores reales
```

### Variables de entorno requeridas

| Variable | Valor por defecto | Descripción |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://postgres:postgres@localhost:5432/badie` | URL async de Postgres (asyncpg driver) |
| `REDIS_URL` | `redis://localhost:6379/0` | URL de Redis |

Estas variables ya están en `.env.example`. Copiar y ajustar si los puertos o credenciales difieren.

### Levantar dependencias (docker compose)

```bash
# Arranca Postgres (pgvector/pgvector:pg16) + Redis en background
docker compose up -d

# Esperar a que Postgres esté healthy (healthcheck: pg_isready, interval 5s, retries 5)
# Luego inicializar el schema (CREATE EXTENSION vector + 5 tablas)
uv run python scripts/init_db.py
```

El script `init_db.py` es idempotente — se puede correr múltiples veces sin error.

#### Verificación de conectividad (opcional)

```bash
# Requiere que el stack esté levantado y el schema inicializado
uv run pytest -m integration -v
```

### Levantar la app

```bash
# Modo desarrollo con auto-reload
uv run uvicorn badie.main:create_app --factory --reload --port 8000

# O directo (sin reload)
uv run uvicorn badie.main:app --port 8000
```

### Smoke tests

```bash
# Health
curl http://localhost:8000/health
# {"status":"ok","environment":"development","postgres":"ok","redis":"ok"}

# Challenge (configura WHATSAPP_VERIFY_TOKEN en .env primero)
curl "http://localhost:8000/webhook?hub.mode=subscribe&hub.verify_token=TU_TOKEN&hub.challenge=test123"
# test123

# POST con firma HMAC
BODY='{"entry":[{"changes":[{"value":{"messages":[{"from":"5491155555555","id":"wamid.test","text":{"body":"hola"},"timestamp":"123"}]}}]}]}'
SIG=$(echo -n "$BODY" | openssl dgst -sha256 -hmac "TU_SECRET" | awk '{print "sha256="$2}')
curl -X POST http://localhost:8000/webhook \
  -H "Content-Type: application/json" \
  -H "X-Hub-Signature-256: $SIG" \
  -d "$BODY"
```

### Exponer a Meta (testing E2E)

Para que Meta llegue a tu local, usá un tunnel HTTPS:

```bash
ngrok http 8000
# Copiar la URL https://xxx.ngrok-free.app/webhook al dashboard de Meta
```

---

## 12. CI/CD

### GitHub Actions (`.github/workflows/ci.yml`)

Trigger: cada `push` a cualquier rama + cada `pull_request`.

```yaml
jobs:
  ci:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
        with: { enable-cache: true }
      - run: uv sync --group dev
      - run: uv run ruff check .
      - run: uv run mypy src/
      - run: uv run pytest
```

**Caché**: `setup-uv@v5` cachea automáticamente el `uv.lock` y los wheels. Pipeline típico ~30s.

**Quality gates** (todos bloquean el merge):
1. **ruff** — lint
2. **mypy strict** — type checking
3. **pytest** — los 42 tests

### Próximos pasos de deploy

- Containerización (Dockerfile + docker-compose para producción)
- Deployment a Cloud Run / ECS / similar
- Postgres → Cloud SQL / RDS / Supabase
- Redis → Memorystore / ElastiCache / Upstash

---

## 13. Roadmap y Estado

### ✅ Completado

| Paso | Descripción | Commit |
|------|-------------|--------|
| **0.1** Scaffold | Estructura inicial del proyecto | (legacy) |
| **0.2** Config | Settings con pydantic-settings | (legacy) |
| **0.3** Database | SQLAlchemy async + 5 modelos | (legacy) |
| **0.4** Redis | Connection pool + lifespan | `fc8a8f7` |
| **0.5** Observability | structlog + RequestIdMiddleware + /health real | `b221d7c` |
| **0.6** CI | GitHub Actions workflow | `b43a26d` |
| **1A.1** Webhook | GET challenge + POST + HMAC verify | `e107dca` |
| **1A.2** Dedup | Redis SET NX, fail-open, TTL 300s | `2763331` |
| **1A.3** Client Lookup | normalize_phone + auto-register | `25a936c` |

### 🚧 En curso

| Paso | Descripción |
|------|-------------|
| **1A.4** Catálogo + Embeddings + Sync clientes | Pipeline `medallion → local`, embeddings 512d, normalización E.164 con `phonenumbers` |

### 📋 Pendiente

| Paso | Descripción |
|------|-------------|
| **1A.5** Servicio RAG | Búsqueda vectorial con pgvector (umbrales 0.92/0.82) |
| **1A.6** RAG Test Suite | 100+ expresiones coloquiales argentinas |
| **1A.7** Conversation State | TypedDict para LangGraph |
| **1B.1+** Agent | LangGraph: router, retrieval, generation, order creation |
| **1C.x** WhatsApp Send | Cliente para mandar respuestas via Meta |
| **2.x** Phase 2 | Supervisor pattern, Celery, sliding TTL |

### Datos del warehouse externo (`gold` schema medallion)

Próximo paso conecta con:
- `gold.dim_articulo` → catálogo (PK: `id_articulo`, atributos: `marca`, `generico`, `calibre`)
- `gold.dim_cliente` → clientes (PK: `id_cliente`, teléfono en `telefono_movil`, lista de precios en `id_lista_precio`)
- **Tabla de precios**: pendiente de desarrollo en el warehouse

---

## 14. Decisiones Arquitectónicas Clave

### ADR-001: Async-first end-to-end

**Decisión**: FastAPI + SQLAlchemy asyncio + asyncpg + redis.asyncio.
**Por qué**: 1000+ conversaciones concurrentes esperadas. Sync bloquearía el event loop con I/O.
**Tradeoff**: Más complejidad inicial, librerías deben soportar async.

### ADR-002: PostgreSQL + pgvector (no Pinecone/Weaviate)

**Decisión**: Vectores en la misma DB.
**Por qué**: Simplicidad operativa (un solo servicio), transaccionalidad (catálogo + embeddings juntos), HNSW competitivo a esta escala.
**Tradeoff**: A escala >1M vectores, una vector DB dedicada sería más rápida.

### ADR-003: HNSW over IVFFlat

**Decisión**: Índice HNSW con `m=16, ef_construction=64`.
**Por qué**: IVFFlat patológico para catálogos pequeños. HNSW siempre funciona y escala bien.

### ADR-004: text-embedding-3-small con 512 dims (Matryoshka)

**Decisión**: 512 dims en vez de 1536 default.
**Por qué**: Matryoshka reduction casi sin pérdida de calidad. 3x menos espacio, 3x más rápido en distancia coseno. **DECISIÓN LOCKED** — no cambiar sin re-embeddear.

### ADR-005: Redis SET NX para dedup desde día 1

**Decisión**: Dedup obligatorio antes de procesar mensajes.
**Por qué**: Meta retransmite webhooks. Sin dedup, mensajes duplicados → respuestas duplicadas → pedidos duplicados.

### ADR-006: Auto-register de clientes desconocidos como `active=False`

**Decisión**: Cualquier teléfono nuevo se registra automáticamente pero queda inactivo.
**Por qué**: No perder mensajes, pero no atender a desconocidos sin alta manual. El bot le avisará al cliente que se contacte con un humano para darse de alta.

### ADR-007: Fail-open en lookups (Redis y DB)

**Decisión**: Si Redis o DB caen durante dedup/lookup, procesar el mensaje igual.
**Por qué**: Mejor procesar un duplicado o "olvidar" un cliente registrado que perder un pedido real.

### ADR-008: Strict TDD desde Paso 0.5 en adelante

**Decisión**: Tests RED antes de implementación, verificado por SDD pipeline.
**Por qué**: Calidad del código + confianza para refactorizar + documentación viva.

### ADR-009: structlog con `merge_contextvars` en lugar de `logging` stdlib

**Decisión**: structlog para JSON estructurado.
**Por qué**: Correlación automática vía ContextVars, output parseable, soporte async nativo.

### ADR-010: Hybrid agent approach para MVP (no Supervisor)

**Decisión**: Phase-based routing en lugar de Supervisor pattern de LangGraph para Fase 1A.
**Por qué**: Supervisor agrega latencia y complejidad innecesaria para MVP. Migrar a Supervisor en Fase 2 cuando escale.

---

## 15. Workflow SDD

### Spec-Driven Development

Cada feature pasa por un pipeline de fases. Cada fase produce un artefacto persistido (engram + opcionalmente archivos en `openspec/changes/`).

```
┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐
│ explore  │──▶│ propose  │──▶│   spec   │──▶│  design  │──▶│  tasks   │──▶│  apply   │──▶│  verify  │
└──────────┘   └──────────┘   └──────────┘   └──────────┘   └──────────┘   └──────────┘   └──────────┘
   ↓              ↓              ↓              ↓              ↓              ↓              ↓
investigar   intent+scope   requirements   technical    breakdown    implement     check
codebase     +approach      +scenarios     approach     RED tasks    code+tests    against
                                                                                   spec
```

| Fase | Output | Cuándo |
|------|--------|--------|
| `explore` | exploration.md | Investigar antes de comprometerse |
| `propose` | proposal.md | Definir intent, scope, approach, rollback |
| `spec` | spec.md | Requirements (RF-XX) y scenarios (SC-XX) |
| `design` | design.md | Technical approach, módulos, data flow |
| `tasks` | tasks.md | Lista RED → GREEN ordenada |
| `apply` | apply-progress.md | Implementación TDD |
| `verify` | verify-report.md | Validación contra spec |
| `archive` | archive-report.md | Cierre del cambio |

### Strict TDD Mode

Activado por proyecto (`sdd-init/agents-badie` flag). En cada `apply`:

1. Escribir test RED primero
2. Correr → confirmar que falla
3. Implementar
4. Correr → confirmar GREEN
5. Repeat

### Memory persistente (engram)

Topic keys por cambio:
- `sdd/{change-name}/explore`
- `sdd/{change-name}/proposal`
- `sdd/{change-name}/spec`
- `sdd/{change-name}/design`
- `sdd/{change-name}/tasks`
- `sdd/{change-name}/apply-progress`
- `sdd/{change-name}/verify-report`

Permite recuperar contexto entre sesiones — si la conversación se compacta, el siguiente turno puede leer todo el histórico.

### Convención de commits

```
feat: add <feature> (Paso X.Y.Z)
fix: <bug> in <module>
refactor: <reason>
test: <coverage>
chore: <maintenance>
```

Sin co-authored-by. Un commit por paso SDD.

---

## 16. Glosario

| Término | Definición |
|---------|------------|
| **BADIE** | Distribuidora BADIE S.A., parte de Grupo Manzur. Distribuidora de bebidas. |
| **Preventista** | Vendedor de campo que visita puntos de venta y levanta pedidos manualmente. El bot busca digitalizar este rol. |
| **Punto de venta** | Cliente final del distribuidor — kiosco, almacén, bar, restaurant. |
| **Pedido** | Conjunto de items que un punto de venta solicita en un día específico. |
| **Lista de precios** | Tarifa diferenciada según tipo de cliente (mayorista/minorista, zona, etc.). 4-5 listas distintas. |
| **wamid** | Format de IDs de mensajes de WhatsApp Cloud API: `wamid.<base64>`. |
| **E.164** | Formato internacional de teléfono: `+CC<número>` (ej: `+5491123456789`). |
| **HMAC-SHA256** | Hash con clave secreta para verificar autenticidad e integridad de un payload. |
| **HNSW** | Hierarchical Navigable Small World — algoritmo de índice para búsqueda aproximada de vectores. |
| **IVFFlat** | Inverted File con clustering — algoritmo alternativo de índice vectorial, peor para catálogos chicos. |
| **Matryoshka embeddings** | Embeddings que mantienen calidad al truncar dimensiones — `text-embedding-3-small` lo soporta. |
| **RAG** | Retrieval-Augmented Generation — combinar búsqueda en una base + LLM para generar respuestas grounded. |
| **SCD Type 1** | Slowly Changing Dimension Type 1 — overwrite, sin histórico. Usado en `gold.dim_articulo` y `dim_cliente`. |
| **Medallion** | Arquitectura de data warehouse en capas: bronze (raw) → silver (cleaned) → gold (business-ready). |
| **Fail-open** | Política donde un fallo en un check periférico (Redis, DB) NO bloquea el flujo principal. Lo opuesto: fail-closed. |
| **Timing attack** | Vulnerabilidad de seguridad donde el tiempo de respuesta revela información — `compare_digest` lo previene. |
| **ContextVar** | Variable de Python 3.7+ que se propaga a través de tareas async — usada por structlog para correlation IDs. |
| **Lifespan** | Hook async de FastAPI para startup/shutdown de la app. |
| **Idempotencia** | Propiedad de una operación que produce el mismo resultado si se ejecuta múltiples veces. Crucial para webhooks. |
| **Dedup** | Deduplicación — evitar procesar el mismo mensaje dos veces. |
| **Webhook** | Endpoint HTTP que un servicio externo (Meta) llama cuando ocurre un evento. |
| **SDD** | Spec-Driven Development — workflow estructurado de fases con artefactos persistidos. |
| **TDD** | Test-Driven Development — escribir test antes que implementación. |
| **engram** | Sistema de memoria persistente entre sesiones de Claude Code. |

---

## Apéndices

### A. Comando útil para nuevos desarrolladores

```bash
# Setup completo desde cero
git clone <repo> && cd agents-badie
uv sync --group dev
cp .env.example .env  # rellenar
docker compose up -d  # postgres + redis
uv run pytest         # verificar 42 tests passing
uv run uvicorn badie.main:create_app --factory --reload
```

### B. Estructura de un payload de Meta (text message)

```json
{
  "object": "whatsapp_business_account",
  "entry": [{
    "id": "WHATSAPP_BUSINESS_ACCOUNT_ID",
    "changes": [{
      "value": {
        "messaging_product": "whatsapp",
        "metadata": {
          "display_phone_number": "15550783881",
          "phone_number_id": "123456123"
        },
        "contacts": [{
          "profile": {"name": "Test User"},
          "wa_id": "5491123456789"
        }],
        "messages": [{
          "from": "5491123456789",
          "id": "wamid.ABGGFlA5FpafAgo6tHcNmNjXmuSf",
          "timestamp": "1669058305",
          "text": {"body": "dame dos cajones de la rubia"},
          "type": "text"
        }]
      },
      "field": "messages"
    }]
  }]
}
```

### C. Estructura de un status update (NO es un mensaje)

```json
{
  "entry": [{
    "changes": [{
      "value": {
        "statuses": [{
          "id": "wamid.ABC",
          "status": "delivered",
          "timestamp": "1669058310"
        }]
      }
    }]
  }]
}
```

El webhook lo recibe pero `value.messages` no existe → return early.

---

**Última actualización**: 2026-04-30
**Versión del proyecto**: 0.1.0
**Mantenedor**: nahuel893
