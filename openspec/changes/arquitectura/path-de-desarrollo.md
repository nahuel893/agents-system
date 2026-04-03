# Path de Desarrollo: agents-badie

## De Cero a Producción — WhatsApp Sales Agent Bot para BADIE S.A.

---

> Este documento es tu mapa de ruta. Cada paso te dice QUÉ construir, QUÉ vas a aprender, y CÓMO sabés que terminaste. No es una lista de tareas — es un camino de aprendizaje que te lleva de "no tengo código" a "tengo un bot tomando pedidos en producción".
>
> **Regla de oro**: Nunca avanzar a un paso sin que el anterior tenga su criterio de "terminé" cumplido. La tentación de avanzar sin consolidar es el error más caro en sistemas con LLMs, porque los bugs son silenciosos — no te tiran un 500, te dan una respuesta incorrecta que parece correcta.

---

## Fase 0: Setup e Infraestructura

**Objetivo**: Tener el esqueleto del proyecto, la base de datos lista, Redis corriendo, y un endpoint de health que responda. Cero lógica de negocio. Esto es los cimientos del edificio — sin esto, todo lo que construyas arriba se cae.

**Duración total estimada**: 4-5 días

---

### Paso 0.1: Scaffold del Proyecto Python

**Duración estimada**: 1 día
**Prerequisitos**: Python 3.12+, un editor, Git instalado
**Qué vas a construir**: La estructura de carpetas y el archivo `pyproject.toml` con todas las dependencias. El proyecto tiene que poder instalarse con `pip install -e .` desde el minuto uno.

**Qué vas a aprender**: Cómo estructurar un proyecto Python moderno con `pyproject.toml` (no `setup.py`, no `requirements.txt` suelto). La estructura de carpetas refleja la arquitectura — capas de integración, orquestación y datos separadas desde el arranque.

**Estructura de carpetas objetivo**:
```
agents-badie/
├── pyproject.toml
├── .env.example
├── .gitignore
├── .python-version
├── src/
│   └── badie/
│       ├── __init__.py
│       ├── config.py              # Pydantic Settings
│       ├── main.py                # FastAPI app factory
│       ├── integration/           # Capa 1 — Webhook, WhatsApp
│       │   ├── __init__.py
│       │   ├── webhook.py
│       │   └── whatsapp_client.py
│       ├── agent/                 # Capa 2 — LangGraph, nodos
│       │   ├── __init__.py
│       │   ├── graph.py
│       │   ├── state.py
│       │   ├── nodes/
│       │   └── prompts/
│       ├── services/              # Lógica de negocio
│       │   ├── __init__.py
│       │   ├── catalog.py
│       │   ├── orders.py
│       │   └── rag.py
│       ├── models/                # SQLAlchemy models
│       │   └── __init__.py
│       └── observability/         # structlog, middleware
│           └── __init__.py
├── tests/
│   ├── conftest.py
│   ├── conversations/            # JSON fixtures
│   ├── mocks/
│   └── payloads/                 # Meta webhook payloads
└── scripts/
    └── embed_catalog.py          # Script de embedding
```

**Dependencias principales (pyproject.toml)**:
- `fastapi`, `uvicorn[standard]` — servidor
- `langgraph`, `langgraph-checkpoint-redis` — orquestación
- `langchain-anthropic`, `langchain-openai` — LLMs y embeddings
- `sqlalchemy[asyncio]`, `asyncpg` — BD
- `redis`, `httpx` — Redis y HTTP client
- `pgvector` — extensión Python para pgvector
- `pydantic-settings` — configuración
- `structlog` — logging
- `pytest`, `pytest-asyncio`, `ruff`, `mypy` — dev tools

**Criterio de "terminé"**: `pip install -e ".[dev]"` funciona sin errores. `ruff check .` pasa. `mypy src/` pasa (aunque sea vacío). `pytest` corre (0 tests, 0 errores).

**Archivos que vas a crear**:
- `pyproject.toml`
- `src/badie/__init__.py` y todos los `__init__.py` de subcarpetas
- `.env.example`
- `.gitignore`
- `.python-version`
- `tests/conftest.py`

**Guía teórica**: Ningún tema específico — esto es fundamento de ingeniería de software.

**Trampas comunes**:
- Poner dependencias en `requirements.txt` en vez de `pyproject.toml`. No hagas eso. Es 2026, `pyproject.toml` es el estándar.
- No separar las capas desde el principio. "Después lo refactoreo" es mentira. La estructura de carpetas ES tu arquitectura.
- Olvidar `.env` en el `.gitignore`. Vas a tener API keys de Anthropic y OpenAI ahí. Si se pushean, tenés que rotarlas.

---

### Paso 0.2: Configuración con Pydantic Settings

**Duración estimada**: 0.5 días
**Prerequisitos**: Paso 0.1
**Qué vas a construir**: `config.py` que lee variables de entorno (y `.env`) con validación de tipos. Una sola fuente de verdad para TODA la configuración.

**Qué vas a aprender**: Pydantic Settings para configuración type-safe. Por qué NUNCA hardcodear URLs, API keys, o umbrales.

**Criterio de "terminé"**: `from badie.config import settings` funciona. Si falta una variable requerida (como `ANTHROPIC_API_KEY`), el app NO arranca y te dice cuál falta.

**Archivos que vas a crear/modificar**:
- `src/badie/config.py`
- `.env.example` (actualizar con todas las variables)

**Variables mínimas**:
```
DATABASE_URL, REDIS_URL, ANTHROPIC_API_KEY, OPENAI_API_KEY,
META_VERIFY_TOKEN, META_APP_SECRET, META_PHONE_NUMBER_ID, META_ACCESS_TOKEN,
SLACK_WEBHOOK_URL, EMBEDDING_MODEL, EMBEDDING_DIMENSIONS,
RAG_THRESHOLD_DIRECT, RAG_THRESHOLD_AMBIGUOUS, REDIS_TTL_SECONDS
```

**Trampas comunes**:
- Usar `os.getenv()` directamente en el código. Centralizá TODO en `config.py`. Si un módulo necesita una config, la importa de ahí.
- No validar tipos. `REDIS_TTL_SECONDS` tiene que ser `int`, no `str`. Pydantic te lo valida gratis.

---

### Paso 0.3: Base de Datos — PostgreSQL + pgvector

**Duración estimada**: 1 día
**Prerequisitos**: Paso 0.1, PostgreSQL instalado
**Qué vas a construir**: El schema completo de la BD: tablas `clients`, `orders`, `order_items`, `conversation_logs`, `catalog_embeddings` (con `vector(512)` y índice HNSW). Modelos SQLAlchemy para cada tabla.

**Qué vas a aprender**: Cómo funciona pgvector como extensión de PostgreSQL. Por qué HNSW y no IVFFlat para catálogos chicos. La diferencia entre `vector(512)` y `vector(1536)`.

**Guía teórica**: Tema 1 (Vector Indexes — HNSW vs IVFFlat), Tema 2 (Embeddings)

**Detalles críticos del deep-dive que aplicar**:
- `vector(512)` NO `vector(1536)` — usamos Matryoshka dimensions de text-embedding-3-small
- Índice HNSW con `m=16, ef_construction=64` — NO IVFFlat
- Schema SQL corregido del deep-dive sección 1.1 y 1.2

**Criterio de "terminé"**:
1. `CREATE EXTENSION vector;` ejecutado sin error
2. Todas las tablas creadas con un script de migración (o Alembic)
3. `SELECT * FROM catalog_embeddings LIMIT 1;` funciona
4. El índice HNSW aparece en `\di` de psql
5. SQLAlchemy models mapean correctamente a las tablas — un test que haga `session.add(Client(...))` y `session.commit()` pasa

**Archivos que vas a crear/modificar**:
- `src/badie/models/client.py`
- `src/badie/models/order.py`
- `src/badie/models/catalog.py`
- `src/badie/models/conversation_log.py`
- `scripts/init_db.sql` (o Alembic migrations)

**Trampas comunes**:
- Copiar el schema del PRD literalmente — tiene `vector(1536)` e IVFFlat. Ambos están mal para este caso. Usá las correcciones del deep-dive.
- No instalar la extensión `vector` en PostgreSQL antes de intentar crear la tabla. Es `CREATE EXTENSION IF NOT EXISTS vector;` como primer paso.
- Olvidar el `SET hnsw.ef_search = 40;` en la configuración de la conexión de SQLAlchemy.

---

### Paso 0.4: Redis Setup

**Duración estimada**: 0.5 días
**Prerequisitos**: Paso 0.1, Redis instalado
**Qué vas a construir**: Conexión Redis verificada. Un helper que testee la conexión al arrancar. La decisión de TTL documentada.

**Qué vas a aprender**: Redis como store efímero vs persistente. Por qué usamos TTL de 24h fijo para MVP (y sliding para producción).

**Guía teórica**: Tema 3 (TTL en Redis)

**Decisión lockeada**: TTL de 24h FIJO para MVP. Sliding TTL en Fase 2 (el deep-dive sección 1.3 da el pattern de `SlidingTTLRedisSaver`).

**Criterio de "terminé"**: Un test que haga `SET`, `GET`, y verifique que el TTL se aplica.

**Archivos que vas a crear/modificar**:
- `src/badie/config.py` (agregar `REDIS_URL`)
- `tests/test_redis_connection.py`

**Trampas comunes**:
- Usar Redis sin password en desarrollo y olvidar configurarlo para producción.
- No entender que `langgraph-checkpoint-redis` va a usar Redis para estado conversacional Y vos también lo usás para deduplicación. Son keys distintas con prefijos distintos, en la misma instancia.

---

### Paso 0.5: FastAPI App con Health Endpoint + structlog

**Duración estimada**: 1 día
**Prerequisitos**: Pasos 0.1-0.4
**Qué vas a construir**: `main.py` con app factory de FastAPI. Endpoint `GET /health` que verifica PostgreSQL y Redis. Middleware de `request_id` con structlog. Logging JSON estructurado desde el primer request.

**Qué vas a aprender**: FastAPI app factory pattern. Structured logging con correlation IDs. Middleware en Starlette/FastAPI.

**Guía teórica**: Tema 13 (Observabilidad y Logging Estructurado)

**Lo que el deep-dive dice (sección 2.7)**: structlog con `request_id` y `thread_id` en contextvars. Cada log entry es JSON con timestamp, level, event, y metadata.

**Criterio de "terminé"**:
1. `uvicorn badie.main:app --reload` arranca sin errores
2. `GET /health` devuelve `{"status": "ok", "postgres": "ok", "redis": "ok"}`
3. Si PostgreSQL está caído, devuelve `{"status": "degraded", "postgres": "error", ...}`
4. Cada request genera logs JSON con `request_id`
5. Los logs son parseables con `jq`

**Archivos que vas a crear/modificar**:
- `src/badie/main.py`
- `src/badie/observability/logging.py`
- `src/badie/observability/middleware.py`
- `tests/test_health.py`

**Trampas comunes**:
- Usar `print()` para debugging y nunca configurar logging. Esto te va a costar SEMANAS cuando tengas que debuggear por qué el bot le respondió mal a un cliente.
- No probar el health endpoint con dependencias caídas. El health check que solo devuelve "ok" sin verificar nada es inútil.

---

### Paso 0.6: CI Básico (GitHub Actions)

**Duración estimada**: 0.5 días
**Prerequisitos**: Paso 0.5
**Qué vas a construir**: GitHub Actions workflow que corra `ruff check`, `mypy`, y `pytest` en cada push.

**Qué vas a aprender**: CI como red de seguridad. Por qué correr linting y type checking automáticamente.

**Criterio de "terminé"**: Push al repo, el workflow pasa en verde.

**Archivos que vas a crear**:
- `.github/workflows/ci.yml`
- `ruff.toml` (o sección en `pyproject.toml`)
- `mypy.ini` (o sección en `pyproject.toml`)

**Trampas comunes**:
- No incluir PostgreSQL y Redis como services en el CI. Sin ellos, los tests de integración no corren.

---

### Checkpoint Fase 0

**Antes de pasar a Fase 1, verificá**:
- [ ] `pip install -e ".[dev]"` funciona
- [ ] `ruff check .` pasa
- [ ] `pytest` pasa (aunque tenga pocos tests)
- [ ] `uvicorn badie.main:app` arranca y `/health` responde
- [ ] Los logs son JSON estructurado
- [ ] CI en verde
- [ ] `.env.example` tiene TODAS las variables documentadas

**Si algo de esto falla, NO avances.** Estás construyendo sobre arena.

---

## Fase 1A: Core Pipeline — Sin LLM

**Objetivo**: Todo el pipeline de ida y vuelta con WhatsApp, la deduplicación, el lookup de clientes, y el RAG — SIN tocar LangGraph todavía. La idea es que puedas hacer `curl` al webhook, que el mensaje se procese, y que el RAG te devuelva productos. Probás cada pieza en aislamiento antes de ensamblar.

**Duración total estimada**: 7-9 días

---

### Paso 1A.1: Webhook Endpoint — Recibir mensajes de Meta

**Duración estimada**: 1.5 días
**Prerequisitos**: Paso 0.5
**Qué vas a construir**: Dos endpoints:
1. `GET /webhook` — Verificación del webhook (Meta te manda un challenge token y vos lo devolvés)
2. `POST /webhook` — Recepción de mensajes. Parsea el payload de Meta, extrae `phone_number`, `text`, `message_id`, `timestamp`.

Incluye validación de firma HMAC del payload (seguridad, no opcional).

**Qué vas a aprender**: Cómo funciona la verificación de webhooks de Meta. El formato del payload de WhatsApp Cloud API. HMAC signature verification.

**Guía teórica**: Tema 5 (Idempotencia de Webhooks)

**Criterio de "terminé"**:
1. Un test con un payload JSON real de Meta (guardado en `tests/payloads/`) que verifique que se parsea correctamente
2. Un test que verifique que una firma HMAC inválida devuelve 403
3. Un test que verifique que el GET de verificación devuelve el challenge token
4. El endpoint responde HTTP 200 en menos de 100ms (sin procesar el mensaje todavía — solo parsear y loguear)

**Archivos que vas a crear/modificar**:
- `src/badie/integration/webhook.py`
- `src/badie/integration/meta_signature.py`
- `tests/payloads/text_message.json`
- `tests/payloads/status_update.json`
- `tests/test_webhook.py`

**Trampas comunes**:
- No distinguir entre mensajes de texto y notificaciones de estado (delivered, read). Meta te manda AMBOS al mismo endpoint. Si no filtrás, vas a intentar "responder" a un delivery receipt.
- Hardcodear el `META_APP_SECRET`. Va en `config.py` que lee de `.env`.
- Olvidar que el body del request lo necesitás como `bytes` para verificar la firma Y como `dict` para parsear. Leé el body UNA vez, verificá la firma, y después parseá.

---

### Paso 1A.2: Deduplicación de Mensajes

**Duración estimada**: 1 día
**Prerequisitos**: Pasos 0.4, 1A.1
**Qué vas a construir**: Middleware que use Redis `SET NX` con TTL de 5 minutos para descartar webhooks duplicados de Meta. Usa el `message_id` (campo `wamid.*`) como clave de idempotencia.

**Qué vas a aprender**: Por qué Meta envía duplicados (reintentos por timeout, errores, y duplicados "fantasma"). El patrón `SET NX` en Redis para idempotencia. Middleware en Starlette.

**Guía teórica**: Tema 5 (Idempotencia de Webhooks y Deduplicación)

**Patrón del deep-dive (sección 1.5)**: `WebhookDeduplicationMiddleware` con Redis `SET NX`. Inyecta `request.state.new_message_ids` para que el handler sepa cuáles procesar.

**Criterio de "terminé"**:
1. Enviar el MISMO payload dos veces → el segundo retorna 200 con body `"duplicate"` y NO se procesa
2. Enviar dos payloads DISTINTOS → ambos se procesan
3. Esperar 6 minutos y reenviar → se procesa de nuevo (TTL expiró)
4. Test parametrizado con fixtures de payloads duplicados

**Archivos que vas a crear/modificar**:
- `src/badie/integration/dedup_middleware.py`
- `src/badie/main.py` (registrar middleware)
- `tests/test_dedup.py`

**Trampas comunes**:
- Usar el `timestamp` como clave de dedup en vez del `message_id`. El timestamp NO es único si un cliente manda dos mensajes en el mismo segundo.
- No cachear el body del request. Si lo leés en el middleware, el handler no puede leerlo de nuevo. Starlette consume el body stream una sola vez.
- TTL demasiado largo (horas). 5 minutos es suficiente para cubrir reintentos de Meta. Más de eso desperdicia memoria Redis.

---

### Paso 1A.3: Client Lookup (phone → client_id)

**Duración estimada**: 1 día
**Prerequisitos**: Paso 0.3
**Qué vas a construir**: Servicio que dado un `phone_number` busca el cliente en la tabla `clients`. Si existe, devuelve `client_id`, `name`, `business_type`. Si no existe, devuelve `None` (en MVP, un cliente no registrado se escala a humano).

**Qué vas a aprender**: Async SQLAlchemy con asyncpg. El patrón repository. Por qué `phone_number` como identificador es frágil pero funciona para MVP.

**Guía teórica**: Tema 12 (Resolución de Identidad)

**Criterio de "terminé"**:
1. Test: buscar un teléfono que existe → devuelve el cliente correcto
2. Test: buscar un teléfono que NO existe → devuelve `None`
3. Test: buscar un teléfono con formato distinto (`+549...` vs `549...`) → normalización funciona
4. Script de seed data que inserte 10 clientes de prueba

**Archivos que vas a crear/modificar**:
- `src/badie/services/clients.py`
- `scripts/seed_clients.sql` (o Python seed script)
- `tests/test_client_lookup.py`

**Trampas comunes**:
- No normalizar el formato del teléfono. Meta te manda `5491123456789`. Tu BD puede tener `+5491123456789` o `01123456789`. Normalizá a E.164 (`+` + código de país + número) en el punto de entrada.
- Hacer la query síncrona bloqueando el event loop de FastAPI. Usá `async` con `asyncpg`.

---

### Paso 1A.4: Catálogo + Pipeline de Embeddings

**Duración estimada**: 2 días
**Prerequisitos**: Pasos 0.2, 0.3
**Qué vas a construir**: 
1. Tabla de catálogo con productos de BADIE (seed data de al menos 20-30 productos reales)
2. Script `embed_catalog.py` que toma cada producto, construye el texto de embedding (nombre + sinónimos + categoría), llama a OpenAI `text-embedding-3-small` con `dimensions=512`, y guarda el vector en `catalog_embeddings`
3. Los sinónimos y expresiones coloquiales se curan A MANO en un archivo YAML/JSON por producto

**Qué vas a aprender**: Cómo funcionan los embeddings en la práctica. Matryoshka embeddings y reducción de dimensiones. Por qué la curación de sinónimos ES la calidad del producto (no un "nice to have").

**Guía teórica**: Tema 2 (Embeddings y Modelos de Embedding)

**Decisiones lockeadas**: `text-embedding-3-small`, 512 dimensiones, OpenAI API.

**El texto de embedding por producto debe ser algo como**:
```
Cerveza Salta Rubia 970ml retornable caja 12 unidades.
Sinónimos: rubia, la rubia, cerveza salta, salta rubia, cajón de rubia,
cerveza retornable, 970, litro, caja rubia, birra rubia, la de siempre rubia.
Categoría: cerveza rubia nacional.
```

**Criterio de "terminé"**:
1. El script de embedding corre y genera vectores para todos los productos
2. `SELECT sku, embedding <=> '[0.1, 0.2, ...]'::vector AS distance FROM catalog_embeddings ORDER BY distance LIMIT 3;` devuelve resultados
3. El costo del embedding es < $0.01 (verificar contra la API de OpenAI)
4. Archivo de sinónimos (`data/synonyms.yaml`) tiene al menos 5 sinónimos/expresiones por producto

**Archivos que vas a crear/modificar**:
- `scripts/embed_catalog.py`
- `data/catalog.json` (o seed SQL)
- `data/synonyms.yaml`
- `src/badie/services/embeddings.py` (wrapper de OpenAI embeddings)

**Trampas comunes**:
- No curar sinónimos desde el principio. El RAG es tan bueno como tus sinónimos. "La rubia", "cajón de rubia", "la de litro" — todo eso tiene que estar en el texto de embedding.
- Usar 1536 dimensiones. El deep-dive es claro: 512 dims con Matryoshka es suficiente y ahorra 3x en storage/velocidad.
- No incluir la categoría y la presentación en el texto de embedding. "Cerveza" sola no alcanza — necesitás "cerveza rubia 970ml retornable caja 12" para diferenciar de "cerveza rubia 340ml six pack".

---

### Paso 1A.5: Servicio RAG (búsqueda vectorial)

**Duración estimada**: 1.5 días
**Prerequisitos**: Paso 1A.4
**Qué vas a construir**: `rag.py` — servicio que recibe un texto de consulta, lo embeddea, busca los top-K más cercanos en pgvector, y devuelve resultados con score. Implementa los tres umbrales:
- Score >= 0.92 → match directo
- Score entre 0.82 y 0.92 → ambiguo (mostrar opciones)
- Score < 0.82 → no match

Incluye fallback a `ILIKE` search si el embedding falla (circuit breaker básico).

**Qué vas a aprender**: Búsqueda vectorial con pgvector en la práctica. Similitud coseno. Por qué los umbrales importan y cómo calibrarlos.

**Guía teórica**: Tema 1 (Vector Indexes), Tema 9 (Degradación Elegante y Circuit Breakers)

**Criterio de "terminé"**:
1. `rag_service.search("cajones de la rubia")` devuelve `CS-RUB-970-R` con score > 0.85
2. `rag_service.search("la de siempre")` devuelve score < 0.82 (no match, demasiado genérico)
3. `rag_service.search("agua")` devuelve >= 2 opciones con score entre 0.82-0.92 (ambiguo)
4. Si la API de embeddings falla, el fallback `ILIKE` funciona
5. Logs estructurados con query, resultados, scores, y latencia

**Archivos que vas a crear/modificar**:
- `src/badie/services/rag.py`
- `tests/test_rag.py`

**Trampas comunes**:
- No setear `hnsw.ef_search` en la sesión de SQLAlchemy. Sin esto, pgvector usa defaults que pueden no ser óptimos.
- Comparar con distancia euclidiana en vez de similitud coseno. pgvector tiene operadores distintos: `<=>` es coseno, `<->` es L2. Para text embeddings, usá coseno.
- Umbrales rígidos. Estos son puntos de partida, vas a tener que calibrarlos con datos reales. Dejá los umbrales en `config.py`, no hardcodeados.

---

### Paso 1A.6: RAG Test Suite (100+ expresiones coloquiales)

**Duración estimada**: 1.5 días
**Prerequisitos**: Paso 1A.5
**Qué vas a construir**: Suite de tests parametrizados con 100+ expresiones coloquiales argentinas del rubro cervecero, cada una con el SKU esperado y un score mínimo. Este es EL test más importante del proyecto — si el RAG no matchea bien, el bot es inútil.

**Qué vas a aprender**: Testing determinista de componentes no deterministas. Parametrización de tests con pytest. Cómo calibrar umbrales con datos reales.

**Guía teórica**: Tema 6 (Testing de Sistemas Basados en LLM), Tema 7 (Pirámide de Testing)

**El deep-dive (sección 1.6)** da el patrón exacto con `@pytest.mark.parametrize`.

**Categorías de expresiones a cubrir**:
- Nombres formales ("Cerveza Salta Rubia 970ml")
- Nombres coloquiales ("la rubia", "cajón de rubia")
- Abreviaciones ("six de sin", "la 970")
- Regionalismos ("birra", "porrón")
- Plurales y variaciones ("dos cajones", "cajoncito")
- Expresiones ambiguas ("una cerveza", "dame agua")
- Expresiones que NO deberían matchear ("me prestás un encendedor?")

**Criterio de "terminé"**:
1. >= 100 casos de test en `tests/conversations/rag_expressions.json`
2. Tasa de match correcto >= 85% (objetivo 90%, pero 85% es el mínimo para seguir)
3. Si alguna expresión no matchea bien, agregas sinónimos al `data/synonyms.yaml` y re-embeddeás
4. Tests corren en < 30 segundos (los embeddings de query son rápidos)

**Archivos que vas a crear/modificar**:
- `tests/test_rag_matching.py`
- `tests/conversations/rag_expressions.json`
- `data/synonyms.yaml` (iterar hasta cumplir el 85%)

**Trampas comunes**:
- Testear solo el happy path. "Cerveza salta rubia" matchea siempre. La pregunta es si "la de litro", "dos de la grande", "la misma de siempre" matchean.
- No iterar los sinónimos. Este paso es ITERATIVO: correr los tests, ver qué falla, agregar sinónimos, re-embeddear, repetir. Esperá al menos 3-4 ciclos.
- Asumir que 100 expresiones es "suficiente". Es el mínimo. En producción vas a necesitar 500+, pero para MVP 100+ te da una base sólida.

---

### Paso 1A.7: Conversation State Model

**Duración estimada**: 0.5 días
**Prerequisitos**: Paso 0.1
**Qué vas a construir**: `ConversationState` TypedDict como lo define el PRD, con el agregado de `pending_disambiguation` del deep-dive. Este es el "contrato" de datos que va a fluir por todo el grafo de LangGraph.

**Qué vas a aprender**: TypedDict para tipado de estado. El patrón de estado inmutable que LangGraph usa. `Annotated` con `add_messages` para acumular mensajes.

**Criterio de "terminé"**: Un test que instancie `ConversationState` con datos de prueba, verifique los tipos, y valide que `add_messages` acumula correctamente.

**Archivos que vas a crear/modificar**:
- `src/badie/agent/state.py`
- `tests/test_state.py`

**Trampas comunes**:
- Olvidar `pending_disambiguation`. Sin esto, el flujo multi-item del paso 1B.4 no funciona.
- Hacer el estado mutable. LangGraph espera que cada nodo DEVUELVA un nuevo estado (o un delta), no que mute el existente.

---

### Checkpoint Fase 1A

**Antes de pasar a Fase 1B, verificá**:
- [ ] `POST /webhook` recibe un payload de Meta y lo parsea correctamente
- [ ] Mensajes duplicados se descartan silenciosamente
- [ ] `client_lookup("5491123456789")` devuelve un cliente conocido
- [ ] El catálogo está embeddeado con 512 dims y HNSW index
- [ ] `rag_search("cajones de la rubia")` devuelve el SKU correcto
- [ ] 100+ expresiones coloquiales testeadas, >= 85% match rate
- [ ] `ConversationState` definido y testeado
- [ ] CI en verde con todos los tests

**Esto que acabás de construir es el MOTOR del sistema.** Sin LLM, sin grafo, sin nada fancy — pero los cimientos están firmes. Cada pieza se testea en aislamiento. Ahora sí, ensamblamos.

---

## Fase 1B: Agent Graph — MVP

**Objetivo**: El grafo de LangGraph funcional con routing por fase, extracción de items con Haiku, RAG matching, carrito, confirmación de pedido, y envío de respuesta por WhatsApp. Esto es el CORAZÓN del bot.

**Duración total estimada**: 7-9 días

---

### Paso 1B.1: LangGraph Graph Setup + Redis Checkpointer

**Duración estimada**: 1.5 días
**Prerequisitos**: Pasos 0.4, 1A.7
**Qué vas a construir**: El grafo de LangGraph con `StateGraph(ConversationState)`, compilado con `langgraph-checkpoint-redis`. Los nodos van a ser stubs por ahora (funciones que solo loguean y devuelven el estado sin cambios). Lo importante es que el FLUJO funcione: mensaje entra → nodo se ejecuta → estado se guarda en Redis → próximo mensaje recupera el estado.

**Qué vas a aprender**: LangGraph StateGraph, nodos, edges condicionales. Redis checkpointing. El concepto de `thread_id` para aislamiento de conversaciones.

**Guía teórica**: Tema 3 (TTL en Redis) — el checkpointer usa Redis para persistir estado entre mensajes.

**Nodos stub iniciales**: `classify_intent`, `greeting`, `order`, `confirm`, `escalate`. Cada uno solo loguea y pasa al siguiente.

**Criterio de "terminé"**:
1. `graph.ainvoke({"pending_message": "hola"}, config={"configurable": {"thread_id": "test-1"}})` ejecuta y devuelve estado
2. El estado se guarda en Redis (verificar con `redis-cli KEYS "checkpoint:*"`)
3. Una segunda invocación con el mismo `thread_id` recupera el estado anterior
4. Dos `thread_id` distintos tienen estados completamente aislados
5. Después de 24h (o TTL configurado), el estado desaparece de Redis

**Archivos que vas a crear/modificar**:
- `src/badie/agent/graph.py`
- `src/badie/agent/nodes/__init__.py`
- `src/badie/agent/nodes/classify.py` (stub)
- `src/badie/agent/nodes/greeting.py` (stub)
- `src/badie/agent/nodes/order.py` (stub)
- `src/badie/agent/nodes/confirm.py` (stub)
- `src/badie/agent/nodes/escalate.py` (stub)
- `tests/test_graph_flow.py`

**Trampas comunes**:
- No entender que LangGraph persiste estado entre invocaciones (no entre nodos dentro de una invocación). Cada mensaje del cliente es UNA invocación del grafo. El estado se recupera del checkpoint al inicio y se guarda al final.
- Olvidar el `configurable` en el config. Sin `thread_id`, LangGraph no sabe qué checkpoint usar.

---

### Paso 1B.2: Phase-Based Routing + Prompt Files

**Duración estimada**: 1.5 días
**Prerequisitos**: Paso 1B.1
**Qué vas a construir**: 
1. El nodo `classify_intent` REAL que usa Haiku para determinar la fase de la conversación
2. Routing condicional basado en `phase` del estado
3. Archivos de prompts versionados en `src/badie/agent/prompts/` con el registry del deep-dive

**Qué vas a aprender**: Prompt engineering para clasificación de intención. Routing determinista vs. LLM-based. Versionado de prompts con archivos.

**Guía teórica**: Tema 8 (Versionado de Prompts)

**El approach Hybrid del exploration**: No usamos Supervisor pattern. El `classify_intent` determina la fase y un edge condicional despacha al nodo correcto. Esto es una máquina de estados enriquecida con LLM, no un sistema multi-agente.

**Criterio de "terminé"**:
1. "Hola" → `phase = greeting`
2. "Dame dos cajones de rubia" → `phase = ordering`
3. "Listo, confirmá" → `phase = confirm_pending`
4. Prompts en archivos `.txt` bajo `src/badie/agent/prompts/system/v1.txt`, etc.
5. `get_prompt("system")` carga el archivo correcto
6. Test con LLM mockeado que verifica las transiciones

**Archivos que vas a crear/modificar**:
- `src/badie/agent/nodes/classify.py` (implementación real)
- `src/badie/agent/prompts/registry.py`
- `src/badie/agent/prompts/system/v1.txt`
- `src/badie/agent/prompts/classify_intent/v1.txt`
- `tests/test_routing.py`

**Trampas comunes**:
- Hacer classify_intent demasiado complejo. Es UNA llamada a Haiku que devuelve una de 5-6 categorías. No le pidas que razone, que explique, ni que considere 20 casos edge. Es un clasificador, no un ensayo.
- Hardcodear prompts como strings en el código Python. Desde el día 1, prompts en archivos. El deep-dive sección 2.2 explica por qué.

---

### Paso 1B.3: Greeting Node (Nodo de Saludo)

**Duración estimada**: 0.5 días
**Prerequisitos**: Pasos 1A.3, 1B.2
**Qué vas a construir**: El nodo `greeting` que saluda al cliente por nombre usando la info de `client_lookup`. Si el cliente no está registrado, responde con un mensaje de escalado.

**Qué vas a aprender**: Cómo un nodo de LangGraph usa el estado y los servicios de negocio.

**Criterio de "terminé"**:
1. Cliente conocido: respuesta incluye el nombre ("Hola Roberto, ...")
2. Cliente desconocido: respuesta indica que será atendido por un preventista
3. El `phase` se actualiza a `greeting` en el estado

**Archivos que vas a crear/modificar**:
- `src/badie/agent/nodes/greeting.py` (implementación real)
- `tests/test_greeting.py`

---

### Paso 1B.4: Multi-Item Extraction + RAG Matching (Order Node)

**Duración estimada**: 2.5 días
**Prerequisitos**: Pasos 1A.5, 1B.2
**Qué vas a construir**: El nodo `order_agent` completo:
1. Extracción de items con Haiku (prompt que parsea "dos cajones de rubia y tres six de sin alcohol" en items individuales)
2. RAG matching en paralelo para cada item (`asyncio.gather`)
3. Clasificación de resultados (directo / ambiguo / no match)
4. Confirmación parcial: items con match directo van al carrito, items ambiguos generan opciones
5. Manejo de `pending_disambiguation` en el estado

Este es el paso MAS COMPLEJO de todo el MVP. Tomate el tiempo necesario.

**Qué vas a aprender**: Structured output de LLMs. Paralelismo con asyncio.gather. Manejo de estado parcial en conversaciones.

**Guía teórica**: Tema 11 (Parsing Multi-Item y Desambiguación)

**El deep-dive (sección 2.5)** tiene el flujo completo con código de ejemplo.

**Criterio de "terminé"**:
1. "Dame dos cajones de rubia" → 1 item en el carrito con SKU correcto
2. "Dame dos cajones de rubia, tres six de sin alcohol y una agua" → 2 en carrito, 1 en desambiguación (agua es ambigua)
3. Respuesta del bot confirma los items resueltos y pregunta por los ambiguos
4. Respuesta del cliente a la desambiguación ("con gas") resuelve el item pendiente
5. El carrito ACUMULA items entre mensajes (no se resetea)
6. Tests con LLM mockeado para la extracción Y con RAG real para el matching

**Archivos que vas a crear/modificar**:
- `src/badie/agent/nodes/order.py`
- `src/badie/agent/prompts/extract_items/v1.txt`
- `tests/test_order_agent.py`
- `tests/conversations/happy_path_multi_item.json`
- `tests/conversations/disambiguation.json`

**Trampas comunes**:
- Intentar matchear el mensaje COMPLETO como un solo producto en vez de separar items primero. "Dos cajones de rubia y tres six de sin alcohol" es DOS items, no uno.
- No manejar el caso donde Haiku devuelve JSON malformado. Agregar try/except con retry o fallback.
- Olvidar que la desambiguación es un ESTADO que persiste. Si el cliente manda "con gas" en el siguiente mensaje, el bot tiene que saber que estaba resolviendo "agua".
- No paralelizar las búsquedas RAG. Si hay 3 items, hacer 3 búsquedas secuenciales es 3x más lento. Usá `asyncio.gather`.

---

### Paso 1B.5: Confirm Node + Order Persistence

**Duración estimada**: 1 día
**Prerequisitos**: Pasos 1B.4, 0.3
**Qué vas a construir**: El nodo `confirm_agent`:
1. Presenta resumen del carrito con precios y total
2. Espera confirmación explícita del cliente ("sí", "dale", "confirmá")
3. Al confirmar: crea el `order` y `order_items` en PostgreSQL, genera `external_id`, devuelve número de pedido al cliente
4. El `phase` pasa a `confirmed`

**Qué vas a aprender**: Persistencia transaccional. Por qué el pedido se guarda SINCRÓNICAMENTE (no en BackgroundTasks) — si se pierde, es un pedido perdido.

**Criterio de "terminé"**:
1. "Listo, confirmá" → pedido creado en PostgreSQL con todos los items
2. Respuesta incluye número de pedido
3. Test: verificar que la tabla `orders` y `order_items` tienen los registros correctos
4. Test: si el cliente dice "no, sacame la rubia" → se vuelve a ordering, no se confirma
5. Test: si PostgreSQL falla al guardar → respuesta de error amigable, no crash

**Archivos que vas a crear/modificar**:
- `src/badie/agent/nodes/confirm.py`
- `src/badie/services/orders.py`
- `tests/test_confirm.py`

**Trampas comunes**:
- Guardar el pedido en BackgroundTasks. NO. El pedido es la TRANSACCIÓN CORE del negocio. Se guarda síncronamente, en el request. Si falla, el cliente tiene que saberlo.
- No generar un `external_id` legible. "Pedido #a1b2c3d4" es mejor que "Pedido #7382". El cliente lo va a leer en un chat de WhatsApp.

---

### Paso 1B.6: WhatsApp Response Sending

**Duración estimada**: 1 día
**Prerequisitos**: Paso 1A.1
**Qué vas a construir**: `whatsapp_client.py` — servicio que envía mensajes de texto al cliente via WhatsApp Cloud API. Incluye:
1. Envío de texto plano
2. Manejo de errores de la API de Meta (rate limits, token expirado)
3. Retry con backoff exponencial para errores transitorios
4. Indicador "typing" (escribiendo...) antes de responder

**Qué vas a aprender**: WhatsApp Cloud API para envío de mensajes. Backoff exponencial. Rate limits de Meta.

**Criterio de "terminé"**:
1. Test de integración con WhatsApp sandbox: enviar un mensaje y recibirlo
2. Test unitario: si la API devuelve 429 (rate limit), se reintenta con backoff
3. Test unitario: si la API devuelve 401, NO se reintenta (error de auth)
4. El envío NO bloquea la respuesta del webhook (se manda dentro del flujo del grafo pero con timeout razonable)

**Archivos que vas a crear/modificar**:
- `src/badie/integration/whatsapp_client.py`
- `tests/test_whatsapp_client.py`
- `tests/mocks/whatsapp_mock.py`

**Trampas comunes**:
- No manejar el "typing indicator". Sin él, el cliente manda un mensaje y se queda mirando la pantalla 3-5 segundos sin feedback. Con el typing indicator, ve "BADIE está escribiendo..." y espera tranquilo.
- Hardcodear el token de acceso. Los tokens de Meta expiran. Va en config y eventualmente necesitás un refresh flow.

---

### Paso 1B.7: Conversation Test Harness

**Duración estimada**: 1 día
**Prerequisitos**: Pasos 1B.1-1B.6
**Qué vas a construir**: El framework de tests de conversaciones completas descrito en el deep-dive sección 1.6. Tests parametrizados con JSON fixtures que simulan conversaciones multi-turno con assertions sobre estado, carrito, y contenido semántico de la respuesta.

**Qué vas a aprender**: Testing de sistemas conversacionales. La separación entre capa determinista (mock LLM) y capa semántica (LLM real). LLM-as-judge para evaluación semántica.

**Guía teórica**: Tema 6 (Testing de Sistemas Basados en LLM), Tema 7 (Pirámide de Testing)

**Criterio de "terminé"**:
1. Al menos 5 conversation fixtures: happy path simple, happy path multi-item, desambiguación, cliente desconocido, carrito vacío + confirmación
2. Tests de Capa 1 (mock LLM) corren en < 10 segundos
3. Tests de Capa 2 (RAG real, mock LLM) corren en < 30 segundos
4. `pytest tests/ -k "not integration"` pasa en CI

**Archivos que vas a crear/modificar**:
- `tests/conversations/happy_path_simple.json`
- `tests/conversations/happy_path_multi_item.json`
- `tests/conversations/disambiguation.json`
- `tests/conversations/unknown_client.json`
- `tests/conversations/empty_cart_confirm.json`
- `tests/test_conversation_flows.py`
- `tests/evaluators.py`
- `tests/mocks/llm_mock.py`

**Trampas comunes**:
- Testear solo el happy path. Los edge cases son donde el bot se rompe: mensajes vacíos, emojis solos, audios (que no soportamos), stickers, etc.
- No separar las capas de testing. Si TODOS tus tests usan el LLM real, el CI tarda minutos, cuesta plata, y es flaky. La Capa 1 con mocks es la base.

---

### Checkpoint Fase 1B

**Antes de pasar a Fase 1C, verificá**:
- [ ] Conversación completa: "Hola" → "Dame dos cajones de rubia" → "Listo, confirmá" → pedido en BD
- [ ] Multi-item funciona: 3 items en un mensaje → al menos 2 resueltos directamente
- [ ] Desambiguación funciona: item ambiguo → opciones → resolución
- [ ] Estado persiste entre mensajes (Redis checkpoint)
- [ ] Respuestas se envían por WhatsApp (o mock/sandbox)
- [ ] 5+ conversation fixtures testeados
- [ ] CI en verde

**Felicitaciones. Tenés un bot que toma pedidos.** Es básico, no es robusto, y va a fallar en muchos edge cases. Pero funciona de punta a punta. Ahora lo hacemos confiable.

---

## Fase 1C: Robustez y Handoff

**Objetivo**: El bot puede fallar gracefully, escalar a humanos, y loguear todo. Esto es lo que separa un demo de un producto.

**Duración total estimada**: 4-5 días

---

### Paso 1C.1: Escalation Node + Slack Notification

**Duración estimada**: 1.5 días
**Prerequisitos**: Paso 1B.2
**Qué vas a construir**: El nodo `escalate_node`:
1. Envía mensaje al cliente: "Te paso con un preventista..."
2. Envía notificación rica a Slack con: nombre del cliente, teléfono, resumen de conversación, carrito actual, razón del escalado, botón "Tomar caso"
3. Setea flag `silenced:{thread_id}` en Redis con TTL 2h

**Qué vas a aprender**: Integración con Slack API (Block Kit). El concepto de "silent mode" para handoff.

**Guía teórica**: Tema 10 (Handoff Humano en IA Conversacional)

**El deep-dive (sección 2.4)** tiene el payload de Slack completo.

**Criterio de "terminé"**:
1. Trigger de escalado → mensaje en Slack con toda la info
2. Test: después del escalado, nuevos mensajes del cliente NO se procesan por el bot
3. Test: después de 2h, el bot vuelve a responder
4. El preventista puede ver el historial de la conversación en Slack

**Archivos que vas a crear/modificar**:
- `src/badie/agent/nodes/escalate.py`
- `src/badie/integration/slack_client.py`
- `tests/test_escalation.py`

**Trampas comunes**:
- No incluir el carrito en la notificación de Slack. El preventista necesita saber qué estaba pidiendo el cliente para no empezar de cero.
- Olvidar el modo silencioso. Si el bot sigue respondiendo después del escalado, el cliente recibe mensajes contradictorios.

---

### Paso 1C.2: Silent Mode para Threads Escalados

**Duración estimada**: 0.5 días
**Prerequisitos**: Paso 1C.1
**Qué vas a construir**: Check en el webhook handler que verifica `silenced:{thread_id}` en Redis ANTES de invocar el grafo. Si está silenciado, loguea el mensaje pero no responde.

**Criterio de "terminé"**: Test end-to-end: escalado → mensaje del cliente → NO hay respuesta del bot → pasan 2h → mensaje del cliente → bot responde.

**Archivos que vas a crear/modificar**:
- `src/badie/integration/webhook.py` (agregar check)
- `tests/test_silent_mode.py`

---

### Paso 1C.3: Circuit Breaker para LLM Calls

**Duración estimada**: 1 día
**Prerequisitos**: Paso 1B.2
**Qué vas a construir**: `SimpleCircuitBreaker` del deep-dive sección 2.3. Se aplica a:
1. Llamadas a Anthropic API (Haiku + Sonnet)
2. Llamadas a OpenAI API (embeddings)

Si el circuit breaker se abre: para LLM → mensaje de fallback al cliente ("problemas técnicos, te contacta un preventista"). Para embeddings → fallback a ILIKE search.

**Qué vas a aprender**: Circuit breaker pattern. Por qué es mejor fallar rápido que esperar un timeout.

**Guía teórica**: Tema 9 (Degradación Elegante y Circuit Breakers)

**Criterio de "terminé"**:
1. Test: 3 fallos consecutivos del LLM → circuit abierto → fallback message → NO se llama al LLM
2. Test: después de recovery_timeout → circuit half-open → se prueba una llamada
3. Test: embedding falla → ILIKE search devuelve resultados (degradados pero funcionales)
4. Logs registran cada cambio de estado del circuit breaker

**Archivos que vas a crear/modificar**:
- `src/badie/services/circuit_breaker.py`
- `src/badie/services/rag.py` (agregar fallback)
- `tests/test_circuit_breaker.py`

**Trampas comunes**:
- Hacer el circuit breaker global en vez de por servicio. Un circuit breaker para Anthropic y otro para OpenAI. Si Anthropic se cae, OpenAI puede seguir funcionando.
- No testear la recuperación (half-open → closed). El circuit breaker que se abre y nunca se cierra es un kill switch permanente.

---

### Paso 1C.4: BackgroundTasks para Archival + Logging

**Duración estimada**: 0.5 días
**Prerequisitos**: Pasos 0.3, 0.5
**Qué vas a construir**: Tareas en `BackgroundTasks` de FastAPI para:
1. Archivar cada mensaje (user + assistant) en `conversation_logs`
2. Loguear tokens usados y modelo por cada llamada LLM

**Qué vas a aprender**: FastAPI BackgroundTasks. Qué va en background (logueo, archival) y qué NO (guardar pedidos, enviar respuestas).

**Guía teórica**: Tema 4 (Procesamiento Sync vs Async)

**Criterio de "terminé"**:
1. Después de una conversación, `conversation_logs` tiene todos los mensajes con timestamps, modelo, y tokens
2. Si BackgroundTasks falla (ej: PostgreSQL caído para archival), el request NO falla — solo se pierde el log

**Archivos que vas a crear/modificar**:
- `src/badie/services/conversation_logger.py`
- `src/badie/integration/webhook.py` (agregar BackgroundTasks)
- `tests/test_conversation_logger.py`

---

### Paso 1C.5: Wiring Completo (Webhook → Graph → WhatsApp)

**Duración estimada**: 1 día
**Prerequisitos**: TODOS los pasos anteriores
**Qué vas a construir**: El flujo COMPLETO:
```
Meta webhook → Signature verification → Dedup middleware → Client lookup 
→ LangGraph invoke → WhatsApp response → Background archival
```

Acá es donde ensamblás todo. Hasta ahora cada pieza funcionaba aislada. Ahora las conectás.

**Criterio de "terminé"**:
1. `curl` con un payload de Meta → respuesta enviada por WhatsApp → mensaje logueado en PostgreSQL → estado en Redis
2. El mismo mensaje dos veces → solo se procesa una vez
3. Cliente desconocido → escalado a Slack
4. Error del LLM → mensaje de fallback
5. Latencia total < 5 segundos (requisito de Meta)

**Archivos que vas a crear/modificar**:
- `src/badie/integration/webhook.py` (wiring final)
- `src/badie/main.py` (registrar todo)
- `tests/test_e2e.py`

**Trampas comunes**:
- Responder a Meta después de 5 segundos. Meta reintenta y tenés duplicados (que el dedup debería atrapar, pero mejor no testar eso en producción).
- No manejar los status webhooks (delivered, read). Meta los manda al mismo endpoint. Si no los filtrás, intentás procesar un "read receipt" como un pedido.

---

### Checkpoint Fase 1C

- [ ] Escalado funciona: bot → Slack → preventista informado → bot silenciado
- [ ] Circuit breaker: LLM caído → fallback amigable → recuperación automática
- [ ] RAG caído → ILIKE fallback funciona
- [ ] Conversaciones se archivan en PostgreSQL
- [ ] Flujo completo webhook-to-WhatsApp funciona
- [ ] Latencia < 5s

---

## Fase 1D: Piloto Interno

**Objetivo**: Deploy a staging y test con 10 personas reales del equipo. Encontrar y arreglar los bugs que los tests unitarios no capturan.

**Duración total estimada**: 3-4 días

---

### Paso 1D.1: Deploy a Staging

**Duración estimada**: 1 día
**Prerequisitos**: Fase 1C completa
**Qué vas a construir**: Deploy en el servidor Debian con uvicorn + gunicorn. Webhook apuntando al dominio de staging. WhatsApp sandbox o número de prueba.

**Criterio de "terminé"**: El equipo puede mandarle un WhatsApp al bot desde sus teléfonos y recibir respuestas.

**Archivos que vas a crear/modificar**:
- `deploy/gunicorn.conf.py`
- `deploy/systemd/badie-api.service`
- `deploy/nginx.conf` (reverse proxy + SSL)

**Trampas comunes**:
- No configurar SSL. Meta requiere HTTPS para webhooks. Sin SSL, el webhook no se registra.
- Un solo worker de gunicorn. Para el piloto interno alcanza con 2-4 workers, pero 1 solo va a ser un cuello de botella si dos personas mandan mensaje al mismo tiempo.

---

### Paso 1D.2: Testing Interno (10 personas)

**Duración estimada**: 2-3 días
**Prerequisitos**: Paso 1D.1
**Qué vas a hacer**: 10 miembros del equipo usan el bot como si fueran clientes reales. Cada uno tiene un "guión" de prueba pero también se les pide que "rompan" el bot.

**Guiones de prueba**:
1. Pedido simple (1 item)
2. Pedido multi-item (3+ items)
3. Pedido con desambiguación
4. Modificación post-confirmación (si está implementado) o verificar que no rompe
5. Escribir "HUMANO" → escalado
6. Enviar emoji solo, audio, imagen, sticker
7. Escribir en inglés, mandar un link, pedir algo que no está en catálogo
8. Mandar 5 mensajes seguidos sin esperar respuesta
9. No responder por 30 minutos y después retomar

**Criterio de "terminé"**:
1. Log de issues encontrados (Notion, GitHub Issues, o lo que usen)
2. Todos los issues críticos (bot crashea, pedido duplicado, pedido perdido) resueltos
3. Issues menores documentados para Fase 2
4. Match rate real medido (comparar con el 85% de los tests)

**Trampas comunes**:
- No dar guiones de prueba. Si la gente prueba "como quiere", todos mandan "hola" y "dame una cerveza" y no encontrás los edge cases.
- No revisar los logs durante el piloto. structlog + `jq` es tu mejor amigo acá. Filtrá por errores, latencias altas, y escalados.

---

### Paso 1D.3: Fix Issues + Estabilización

**Duración estimada**: 1-2 días (incluido en el testing)
**Qué vas a hacer**: Arreglar los bugs encontrados en 1D.2. Agregar expresiones coloquiales que fallaron al `synonyms.yaml` y re-embeddear. Ajustar umbrales de RAG si es necesario.

**Criterio de "terminé"**:
1. Todos los issues críticos cerrados
2. Re-run de la suite de RAG con las nuevas expresiones descubiertas → >= 88% match rate
3. Un "smoke test" end-to-end que represente el happy path más común pasa consistentemente

---

### Checkpoint Fase 1D — MVP COMPLETADO

**Esto es un milestone. Parás, respirás, y evaluás.**

- [ ] Bot funcional en staging con 10 testers reales
- [ ] Issues críticos resueltos
- [ ] Match rate >= 85% con expresiones reales
- [ ] Latencia p95 < 5s
- [ ] Escalado a humano funciona
- [ ] Logs y observabilidad funcionan
- [ ] El equipo tiene confianza en que el bot no va a "hacer macanas"

**Decisión a tomar**: ¿El bot es lo suficientemente bueno para probarlo con 50 clientes reales? Si la respuesta es "no estoy seguro", la respuesta es NO. Volvé a iterar.

---

## Fase 2: Multi-Agent + Scale

**Objetivo**: Refactorizar a Supervisor pattern, agregar funcionalidades que faltan (modificación post-cierre, compresión de historial), y escalar a 50+ clientes reales.

**Duración total estimada**: 3-4 semanas

---

### Paso 2.1: Refactor a Supervisor + Sub-Agents

**Duración estimada**: 3-4 días
**Prerequisitos**: Fase 1D completa
**Qué vas a construir**: Migrar del routing por fase (Hybrid approach) al Supervisor pattern de LangGraph. Cada nodo se convierte en un sub-agente con su propio set de tools:
- `catalog_agent` con tool `catalog_search`
- `order_agent` con tool `match_product`
- `confirm_agent` con tool `save_order`
- `modify_agent` con tool `update_order`

El Supervisor decide a quién despachar basándose en el mensaje + estado.

**Qué vas a aprender**: Supervisor pattern en LangGraph. Tool-calling agents. La diferencia entre routing determinista y routing por LLM.

**Criterio de "terminé"**:
1. TODOS los conversation tests de Fase 1B siguen pasando (regresión = 0)
2. El Supervisor despacha correctamente a cada sub-agente
3. Cada sub-agente es testeable en aislamiento
4. Latencia no aumentó más de 500ms respecto a Fase 1

**Trampas comunes**:
- Refactorear sin tener los tests de conversación como red de seguridad. Si no tenés los tests de Fase 1B.7, vas a romper cosas sin enterarte.
- Hacer el Supervisor demasiado inteligente. El Supervisor clasifica y despacha. No razona, no decide, no piensa. Es un router con LLM.

---

### Paso 2.2: Summarize Node (Compresión de Historial)

**Duración estimada**: 2 días
**Prerequisitos**: Paso 2.1
**Qué vas a construir**: `summarize_node` que cada 8 turnos comprime el historial de mensajes en un resumen de <= 200 tokens usando Haiku. El historial raw se archiva en PostgreSQL. El grafo solo ve el resumen + los últimos 2-3 mensajes.

**Qué vas a aprender**: Compresión de contexto. El principio del PRD: "El LLM nunca ve el historial crudo."

**Criterio de "terminé"**:
1. Conversación de 10+ turnos → summarize_node se dispara al turno 8
2. El resumen captura: cliente, estado del pedido, último punto de conversación
3. El bot sigue respondiendo coherentemente después de la compresión
4. Tokens por conversación de 10+ turnos no crecen linealmente

---

### Paso 2.3: Modify Agent (Post-Confirmación)

**Duración estimada**: 2 días
**Prerequisitos**: Paso 2.1
**Qué vas a construir**: `modify_agent` que permite agregar, quitar o cambiar cantidades de items en un pedido ya confirmado, antes del horario de corte.

**Criterio de "terminé"**:
1. "Agregame un six de sin alcohol al pedido" → item agregado, nuevo total
2. "Sacame la rubia" → item removido, nuevo total
3. Después del horario de corte → "Ya pasó el horario de modificación"
4. Las modificaciones se registran con timestamp y referencia al pedido original

---

### Paso 2.4: Sliding TTL para Redis

**Duración estimada**: 1 día
**Prerequisitos**: Paso 2.1
**Qué vas a construir**: `SlidingTTLRedisSaver` del deep-dive sección 1.3. El TTL se renueva en cada interacción.

**Guía teórica**: Tema 3 (TTL en Redis)

**Criterio de "terminé"**: Test que verifica que el TTL se extiende con cada mensaje, no es fijo desde la creación del thread.

---

### Paso 2.5: Rate Limiting por Cliente

**Duración estimada**: 1 día
**Prerequisitos**: Paso 0.4
**Qué vas a construir**: Middleware que limita mensajes por teléfono (ej: max 30 mensajes por minuto). Usa Redis sliding window.

**Criterio de "terminé"**: Test: 31 mensajes en 1 minuto → el mensaje 31 recibe "Esperá un momento, estás enviando mensajes muy rápido."

---

### Paso 2.6: Load Testing

**Duración estimada**: 2 días
**Prerequisitos**: Pasos 2.1-2.5
**Qué vas a construir**: Script de load testing con `locust` o similar que simule 100 conversaciones simultáneas. Cada "usuario virtual" sigue un guión de conversación.

**Criterio de "terminé"**:
1. 100 conversaciones simultáneas sin errores
2. Latencia p95 < 5 segundos bajo carga
3. No hay memory leaks (RSS estable después de 30 minutos)
4. Redis memory usage dentro de lo esperado

---

### Paso 2.7: Piloto con 50 Clientes Reales

**Duración estimada**: 1-2 semanas (ongoing)
**Prerequisitos**: Pasos 2.1-2.6
**Qué vas a hacer**: Seleccionar 50 clientes reales (mix de almacenes chicos y supermercados), darles el número del bot, y monitorear durante 1-2 semanas.

**Métricas a medir**:
- Tasa de pedidos completados sin intervención humana (objetivo: >= 70%)
- Tasa de escalado (objetivo: < 15%)
- Tasa de match RAG correcta (medir manualmente sobre sample de conversaciones)
- Costo por conversación
- Feedback cualitativo de los clientes

---

## Fase 3: Optimización + Monitoreo

**Objetivo**: Controlar costos, tener visibilidad completa, y preparar para producción general.

**Duración total estimada**: 2 semanas

---

### Paso 3.1: Prompt Caching (Anthropic API)

**Duración estimada**: 1 día
**Qué vas a construir**: Marcar el system prompt con `cache_control: ephemeral` en las llamadas a Anthropic. Las definiciones de tools también contribuyen al prefix cacheado.

**Criterio de "terminé"**: Los logs muestran `cache_read_input_tokens > 0` en las llamadas a Sonnet. El costo del system prompt baja a 0.10x.

---

### Paso 3.2: Model Routing (Haiku vs Sonnet)

**Duración estimada**: 1 día
**Qué vas a construir**: Verificar y optimizar qué modelo se usa para qué tarea. Haiku para: classify_intent, extract_items, summarize. Sonnet para: generación de respuestas conversacionales.

**Criterio de "terminé"**: Costo por conversación medido y documentado. Objetivo: < $0.008.

---

### Paso 3.3: Dashboard Plotly Dash

**Duración estimada**: 3-4 días
**Qué vas a construir**: Dashboard interno con:
- Conversaciones activas (real-time)
- Pedidos registrados hoy / esta semana
- Tasa de matching RAG
- Gasto en tokens por modelo, por día
- Tasa de escalado
- Errores y latencias

**Criterio de "terminé"**: Dashboard accesible internamente con datos en vivo.

---

### Paso 3.4: Quality Metrics + Cost Monitoring

**Duración estimada**: 2 días
**Qué vas a construir**: Queries SQL y alertas automáticas (Slack) para:
- Matching rate < 85%
- Costo por conversación > $0.015
- Tasa de escalado > 20%
- Latencia p95 > 8s
- Spike de tokens > 2x del promedio

**Criterio de "terminé"**: Una alerta se dispara en Slack cuando una métrica cruza el umbral.

---

### Paso 3.5: Celery Migration (Opcional)

**Duración estimada**: 2-3 días
**Qué vas a construir**: Migrar tareas críticas de BackgroundTasks a Celery: archival de conversaciones (garantía de entrega), notificaciones de escalado (retry), re-embedding de catálogo.

**Criterio de "terminé"**: Workers de Celery corriendo. Flower para monitoreo. Tasks que fallan se reintentan automáticamente.

---

## Fase 4: Producción General

**Objetivo**: Escalar a toda la cartera de clientes activos. Esto ya no es "desarrollo", es operación + mejora continua.

**Duración**: Ongoing

---

### Paso 4.1: Onboarding de Todos los Clientes

**Duración estimada**: 1-2 semanas
**Qué vas a hacer**: Onboarding gradual. No prender el bot para 1000 clientes de golpe. Grupos de 100-200 por semana. Monitorear métricas después de cada grupo.

---

### Paso 4.2: Integración Semi-Automática con ChessERP

**Duración estimada**: 1-2 semanas
**Qué vas a construir**: Exportación de pedidos en formato compatible con ChessERP. Puede ser CSV, API, o vista de PostgreSQL. El objetivo es que el paso "pedido en bot → pedido en ERP" sea un click, no una carga manual.

---

### Paso 4.3: Prompt Fine-Tuning con Conversaciones Reales

**Duración estimada**: Ongoing
**Qué vas a hacer**: Revisar las conversaciones reales (archivadas en `conversation_logs`), identificar patrones de fallo, y ajustar prompts. Agregar sinónimos al catálogo basándote en lo que los clientes realmente dicen.

---

### Paso 4.4: Curación Continua de Sinónimos

**Duración estimada**: Ongoing
**Qué vas a hacer**: Mantener y expandir `data/synonyms.yaml` con expresiones nuevas que aparecen en producción. Re-embeddear después de cada actualización. Medir el impacto en el match rate.

---

### Paso 4.5: SLA y Procedimientos de Escalado

**Duración estimada**: 1 semana
**Qué vas a hacer**: Documentar: quién responde si el bot se cae a las 3am. Tiempos de respuesta para escalados. Procedimiento de rollback si un deploy rompe algo.

---

## Resumen de Esfuerzo Total

| Fase | Duración Estimada | Qué tenés al final |
|------|-------------------|---------------------|
| **Fase 0**: Setup | 4-5 días | Proyecto scaffolded, BD lista, CI corriendo |
| **Fase 1A**: Core sin LLM | 7-9 días | Webhook, dedup, RAG funcional con 100+ tests |
| **Fase 1B**: Agent Graph MVP | 7-9 días | Bot toma pedidos de punta a punta |
| **Fase 1C**: Robustez | 4-5 días | Escalado, circuit breaker, fallbacks |
| **Fase 1D**: Piloto interno | 3-4 días | 10 testers reales, bugs arreglados |
| **Fase 2**: Multi-Agent + Scale | 3-4 semanas | Supervisor pattern, 50 clientes reales |
| **Fase 3**: Optimización | 2 semanas | Dashboard, costos controlados |
| **Fase 4**: Producción | Ongoing | Toda la cartera, mejora continua |

**Total hasta MVP (Fase 1D)**: ~5-6 semanas para un desarrollador aprendiendo sobre la marcha.
**Total hasta producción controlada (Fase 2)**: ~9-10 semanas.
**Total hasta producción general (Fase 3)**: ~11-12 semanas.

---

## Mapa de Dependencias Visual

```
Fase 0.1 (scaffold)
  ├── 0.2 (config)
  ├── 0.3 (PostgreSQL + pgvector) ──── 1A.3 (client lookup)
  │                                ──── 1A.4 (catalog + embeddings)
  │                                          └── 1A.5 (RAG service)
  │                                                └── 1A.6 (RAG test suite)
  ├── 0.4 (Redis) ─────────────── 1A.2 (dedup)
  │                           ──── 1B.1 (LangGraph + checkpoint)
  ├── 0.5 (FastAPI + structlog) ── 1A.1 (webhook)
  │                                  └── 1A.2 (dedup)
  └── 0.6 (CI)

1A.7 (state model) ──┐
1A.1-1A.6 ───────────┼── 1B.1 (graph setup)
                      │     └── 1B.2 (routing + prompts)
                      │           ├── 1B.3 (greeting)
                      │           ├── 1B.4 (order + multi-item) ★ más complejo
                      │           │     └── 1B.5 (confirm + persistence)
                      │           └── 1B.6 (WhatsApp sending)
                      │                 └── 1B.7 (conversation test harness)
                      │                       └── 1C.5 (wiring completo)
                      └──────────── 1C.1 (escalation + Slack)
                                      └── 1C.2 (silent mode)
                                 1C.3 (circuit breaker)
                                 1C.4 (background archival)
```

---

## Referencia Rápida: Guía Teórica por Paso

| Paso | Temas de la Guía Teórica |
|------|--------------------------|
| 0.3 BD + pgvector | Tema 1 (HNSW), Tema 2 (Embeddings) |
| 0.4 Redis | Tema 3 (TTL) |
| 0.5 structlog | Tema 13 (Observabilidad) |
| 1A.1 Webhook | Tema 5 (Idempotencia) |
| 1A.2 Dedup | Tema 5 (Idempotencia) |
| 1A.3 Client lookup | Tema 12 (Resolución de Identidad) |
| 1A.4 Embeddings | Tema 2 (Embeddings) |
| 1A.5 RAG | Tema 1 (HNSW), Tema 9 (Circuit Breakers) |
| 1A.6 RAG tests | Tema 6 (Testing LLM), Tema 7 (Pirámide) |
| 1B.2 Routing + prompts | Tema 8 (Versionado Prompts) |
| 1B.4 Multi-item | Tema 11 (Parsing Multi-Item) |
| 1B.7 Test harness | Tema 6 (Testing LLM), Tema 7 (Pirámide) |
| 1C.1 Escalation | Tema 10 (Handoff Humano) |
| 1C.3 Circuit breaker | Tema 9 (Degradación Elegante) |
| 1C.4 BackgroundTasks | Tema 4 (Sync vs Async) |
| 2.4 Sliding TTL | Tema 3 (TTL Redis) |