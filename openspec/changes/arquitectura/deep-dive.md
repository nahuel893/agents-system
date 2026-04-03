# Deep Dive: Ajustes Críticos y Gaps

## Parte 1: Ajustes Críticos

---

### 1.1 HNSW vs IVFFlat para pgvector

**El Problema**

IVFFlat con `lists=100` para un catálogo de 50-500 SKUs es patológico. La razón es matemática:

IVFFlat funciona particionando el espacio vectorial en `lists` celdas Voronoi. Durante la búsqueda, solo se escanean `probes` celdas (por defecto 1). Con 100 listas y 500 items, tenés **5 items por lista en promedio**. Con 50 items, tenés **0.5 items por lista** — la mayoría de las listas están VACÍAS.

El problema concreto:
- **Recall degradado**: Con `probes=1` (default), buscás en UNA celda de 100. Si tu vector cae en una celda vecina a la correcta, perdés el match. Para catálogos chicos, la partición es tan granular que el centroide de la celda puede estar lejos del vector real.
- **IVFFlat requiere `VACUUM` después de cada carga de datos** para recalcular centroides. Si no hacés `VACUUM`, los centroides quedan stale y el recall cae aún más.
- **La regla empírica** para `lists` es `sqrt(n_rows)` para menos de 1M filas. Para 500 items: `lists=22`. Para 50 items: `lists=7`. Con `lists=100` estás 5-15x por encima del rango óptimo.

**La Solución: HNSW**

```sql
-- REEMPLAZAR esto:
-- CREATE INDEX ON catalog_embeddings USING ivfflat (embedding vector_cosine_ops)
--     WITH (lists = 100);

-- POR esto:
CREATE INDEX idx_catalog_embedding_hnsw ON catalog_embeddings
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- Y en tiempo de query:
SET hnsw.ef_search = 40;  -- default es 40, suficiente para catálogos chicos
```

**Por qué HNSW es superior para este caso:**

| Aspecto | IVFFlat | HNSW |
|---------|---------|------|
| Recall a 500 items | ~85-92% (con lists mal calibrado, peor) | ~99%+ |
| Requiere VACUUM post-insert | SÍ (obligatorio) | NO |
| Build time 500 items | ~0.1s | ~0.2s (irrelevante) |
| Memoria por item | ~6KB (1536 dims × 4 bytes) | ~12KB (grafo + vector) |
| Memoria total 500 items | ~3MB | ~6MB |
| Query latency | ~0.5ms | ~1ms |
| Funciona bien sin tuning | NO (necesitás calibrar lists y probes) | SÍ |

**¿Y cuando el catálogo crezca a 2000+ SKUs?**

HNSW sigue siendo la respuesta correcta. El punto de inflexión donde IVFFlat puede competir es ~100K+ filas, donde el costo de memoria de HNSW se vuelve significativo. Para 2000 items:
- Memoria HNSW: ~24MB (completamente despreciable)
- Build time: <1 segundo
- Recall: >99%

Para este proyecto, **no existe un escenario realista donde IVFFlat sea preferible**. Un distribuidor de bebidas en Argentina tiene máximo 1000-5000 SKUs, no 100K.

**Configuración para escala futura (2000+ SKUs):**

```sql
-- Para 2000+ items, subir ef_construction para mejor recall:
CREATE INDEX idx_catalog_embedding_hnsw ON catalog_embeddings
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 128);

-- ef_search se puede ajustar por query si necesitás más recall:
SET hnsw.ef_search = 100;  -- más preciso pero ~2ms en vez de ~1ms
```

**Tradeoff**: HNSW usa ~2x más memoria por vector y tiene un build time marginalmente mayor. Para catálogos de menos de 10K items, esto es absolutamente irrelevante. La ganancia es recall near-perfect sin necesidad de tuning ni VACUUM.

**Prioridad**: MUST-HAVE para MVP. El PRD ya define `IVFFlat lists=100` en el schema SQL. Si esto llega a producción así, van a tener matches incorrectos y van a culpar al RAG cuando el problema es el índice.

---

### 1.2 Embedding Model: text-embedding-3-small como elección definitiva

**El Problema**

El PRD dice "text-embedding-3-small (OpenAI) **o** voyage-3 (Anthropic)". Son espacios vectoriales incompatibles. No podés cambiar de uno a otro sin re-embeddear todo. Hay que elegir UNO y lockearlo.

**Comparación concreta:**

| Dimensión | text-embedding-3-small (OpenAI) | voyage-3-lite (Voyager/Anthropic) |
|-----------|------|------|
| Dimensiones | 1536 (reducible a 256/512) | 512 |
| Costo / 1M tokens | $0.020 | $0.020 |
| MTEB benchmark (retrieval avg) | ~62.3 | ~61.8 |
| Soporte español explícito | Sí (multilingüe entrenado) | Sí (multilingüe) |
| Latency (batch 100 items) | ~200ms | ~250ms |
| API | OpenAI (`/v1/embeddings`) | Voyager (`/v1/embeddings`) |
| Dimension reduction (Matryoshka) | SÍ — podés usar 256 dims sin re-entrenar | NO nativo |
| SDK ecosystem | Enorme (langchain, llama-index, etc.) | Más limitado |

**Costo de re-embed para 500 items:**

Calculemos el costo real. El texto de embedding por artículo (nombre + sinónimos + categoría) es ~50-80 tokens.

```
500 items × 70 tokens promedio = 35,000 tokens
Costo: 35,000 / 1,000,000 × $0.020 = $0.0007

Es decir: MENOS DE UN CENTAVO para embeddear todo el catálogo.
```

Esto significa que:
1. El costo de embeddings es completamente despreciable
2. Re-embeddear ante cambios de catálogo es gratis en la práctica
3. La elección del modelo NO se basa en costo sino en **calidad de matching**

**¿Por qué text-embedding-3-small gana para este caso?**

1. **Matryoshka embeddings**: Podés usar 512 dims en vez de 1536, reduciendo a 1/3 el storage y acelerando queries, sin pérdida significativa de calidad para un catálogo chico. Esto significa `vector(512)` en vez de `vector(1536)` en la tabla.

2. **Ecosystem**: LangChain, LlamaIndex y todo el ecosistema tienen first-class support para OpenAI embeddings. Menos código custom.

3. **Latencia más predecible**: OpenAI tiene más infraestructura distribuida, menos variance en latency.

4. **Dimensión reducida = menos memoria HNSW**:
```
1536 dims × 4 bytes × 500 items × ~2 (HNSW overhead) = ~6MB
512 dims  × 4 bytes × 500 items × ~2 (HNSW overhead) = ~2MB
```

**Configuración recomendada:**

```python
# config.py
EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIMENSIONS = 512  # Matryoshka reduction — suficiente para catálogo
EMBEDDING_PROVIDER = "openai"

# Para el schema SQL:
# embedding vector(512)  -- NO 1536
```

```sql
-- Schema corregido:
CREATE TABLE catalog_embeddings (
    id              SERIAL PRIMARY KEY,
    sku             VARCHAR(50) NOT NULL UNIQUE,
    description     TEXT NOT NULL,
    embedding       vector(512),  -- 512 dims, no 1536
    active          BOOLEAN DEFAULT TRUE,
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);
```

**Tradeoff**: Dependés de la API de OpenAI para embeddings (además de Anthropic para LLM). Si OpenAI se cae, no podés hacer nuevos embeddings — pero los existentes ya están en pgvector y las queries RAG siguen funcionando. Solo falla el re-embed de nuevos productos. Riesgo bajo.

**Prioridad**: MUST-HAVE para MVP. Hay que definir esto ANTES de crear la tabla y los primeros embeddings, porque cambiar después requiere re-embeddear todo (trivial en costo, pero implica downtime del matching).

---

### 1.3 Sliding TTL para Redis con LangGraph

**El Problema**

El PRD dice "Redis con TTL 12h". Un TTL fijo desde la creación del thread significa que si un cliente empieza a las 11pm, a las 11am del día siguiente el estado se evapora. Si el cliente escribe "dale, confirmá el pedido" a las 10:30am, el estado ya no existe. Pedido perdido, cliente frustrado.

**La Solución: TTL Sliding**

`langgraph-checkpoint-redis` usa Redis como backend de checkpointing. Cada vez que el grafo procesa un mensaje, escribe un nuevo checkpoint. El truco es renovar el TTL en cada escritura.

El paquete `langgraph-checkpoint-redis` (versión 2.0+) soporta TTL configurado en la conexión. Sin embargo, el TTL nativo del paquete es **fijo en la creación del key**, no sliding. Hay que implementar el sliding manualmente.

**Approach 1: Redis middleware con EXPIRE en cada GET (recomendado)**

```python
from langgraph.checkpoint.redis import RedisSaver
import redis

class SlidingTTLRedisSaver(RedisSaver):
    """RedisSaver que extiende el TTL en cada lectura/escritura."""

    def __init__(self, conn: redis.Redis, ttl_seconds: int = 43200):  # 12h
        super().__init__(conn)
        self.ttl_seconds = ttl_seconds

    def get_tuple(self, config):
        """Override get_tuple para extender TTL en cada lectura."""
        result = super().get_tuple(config)
        if result:
            thread_id = config["configurable"]["thread_id"]
            # Renovar TTL de todas las keys de este thread
            self._refresh_ttl(thread_id)
        return result

    def put(self, config, checkpoint, metadata, new_versions):
        """Override put para setear TTL en cada escritura."""
        result = super().put(config, checkpoint, metadata, new_versions)
        thread_id = config["configurable"]["thread_id"]
        self._refresh_ttl(thread_id)
        return result

    def _refresh_ttl(self, thread_id: str):
        """Renueva TTL de todas las keys asociadas a un thread."""
        # langgraph-checkpoint-redis usa keys con prefijo
        pattern = f"checkpoint:{thread_id}:*"
        for key in self.conn.scan_iter(match=pattern, count=100):
            self.conn.expire(key, self.ttl_seconds)
```

**Approach 2: Si el paquete usa hash keys en vez de string keys**

Verificar el formato de keys que usa `langgraph-checkpoint-redis`. En versiones recientes usa Redis Hashes:

```python
def _refresh_ttl(self, thread_id: str):
    """Para versiones que usan hash keys."""
    # Verificar qué keys existen para este thread
    for key in self.conn.scan_iter(match=f"*{thread_id}*", count=100):
        self.conn.expire(key, self.ttl_seconds)
```

**Approach 3: Redis Keyspace Notifications (más robusto, más complejo)**

Configurar un listener que renueve TTL automáticamente en cada acceso. Overkill para MVP.

**Uso en el grafo:**

```python
import redis
from langgraph.graph import StateGraph

redis_conn = redis.Redis(host="localhost", port=6379, db=0)
checkpointer = SlidingTTLRedisSaver(conn=redis_conn, ttl_seconds=43200)  # 12h

# Construir el grafo
graph = StateGraph(ConversationState)
# ... agregar nodos y edges ...
app = graph.compile(checkpointer=checkpointer)

# Cada invocación renueva el TTL automáticamente
result = app.invoke(
    {"pending_message": "dame dos cajones de rubia"},
    config={"configurable": {"thread_id": phone_number}}
)
```

**Edge case adicional**: ¿Qué pasa con el horario de corte? Si el cutoff es a las 14:00 y el cliente escribe a las 13:59, el TTL de 12h lo cubre hasta las 01:59. Pero si el corte se mueve a las 18:00, necesitás TTL de al menos 18h para cubrir clientes que empiezan temprano. **Recomendación: TTL de 24h sliding**, no 12h. El costo de memoria extra en Redis es despreciable (~1KB por checkpoint × 1000 clientes = 1MB).

**Tradeoff**: El override del Saver agrega una capa de complejidad y acopla el código al formato interno de keys de `langgraph-checkpoint-redis`. Si el paquete cambia el formato de keys, hay que actualizar `_refresh_ttl`. Alternativa más segura: usar TTL de 24h fijo sin sliding — cubre el 99% de los casos sin código custom.

**Prioridad**: SHOULD-HAVE para MVP. Un TTL fijo de 24h es aceptable para el piloto. Sliding TTL es ideal para producción general (Fase 2+).

---

### 1.4 FastAPI BackgroundTasks vs Celery para MVP

**El Problema**

El PRD incluye Celery en el stack desde Fase 1. Celery requiere:
- Un broker (Redis, pero con config separada del state store)
- Worker processes separados
- Monitoreo del worker (Flower o similar)
- Serialización de tasks (pickle/json)
- Deploy y restart de workers

Para un MVP que va a tener 10 usuarios internos, esto es over-engineering.

**¿Cuándo BackgroundTasks se rompe?**

`FastAPI.BackgroundTasks` ejecuta tareas en el mismo event loop o thread pool del server. Los límites concretos:

| Escenario | BackgroundTasks OK? | Por qué |
|-----------|-------------------|---------|
| 10-50 tasks/min, I/O bound (API calls, DB writes) | SÍ | El event loop maneja I/O concurrente |
| 100+ tasks/min, I/O bound | DEPENDE | Si los tasks son rápidos (<1s), sí. Si son lentos (>5s), empezás a acumular |
| Cualquier task CPU-bound (procesamiento pesado) | NO | Bloquea el event loop, degrada latencia de TODOS los requests |
| Tasks que DEBEN completarse (no se pueden perder) | NO | Si el proceso muere, los tasks en queue se pierden |
| Tasks que necesitan retry con backoff | NO | BackgroundTasks no tiene retry nativo |

**¿Qué tasks van a background en Fase 1?**

Mirando el PRD:

| Task | Duración estimada | CPU/IO | ¿Puede perderse? | Background? |
|------|-------------------|--------|-------------------|-------------|
| Archivar conversación en PostgreSQL | <100ms | I/O | No (auditoría) | SÍ — pero con garantía |
| Enviar respuesta a WhatsApp API | 200-500ms | I/O | NO | NO — hacer en request |
| Compresión de historial (summarize_node) | 1-2s | I/O (LLM call) | Sí (se regenera) | SÍ |
| Re-embed de producto actualizado | 200ms | I/O | Sí (se retriggerina) | SÍ |
| Notificación de escalado (Slack) | 200ms | I/O | Tolerable | SÍ |

**Patrón para MVP:**

```python
from fastapi import BackgroundTasks, FastAPI

app = FastAPI()

async def archive_conversation(thread_id: str, messages: list):
    """Guarda mensajes en PostgreSQL para auditoría."""
    async with get_db_session() as session:
        for msg in messages:
            session.add(ConversationLog(thread_id=thread_id, **msg))
        await session.commit()

async def notify_escalation(thread_id: str, client_name: str):
    """Envía alerta de escalado a Slack."""
    async with httpx.AsyncClient() as client:
        await client.post(SLACK_WEBHOOK, json={
            "text": f"🔴 Escalado: {client_name} ({thread_id}) necesita atención"
        })

@app.post("/webhook")
async def handle_webhook(request: Request, background_tasks: BackgroundTasks):
    payload = await request.json()
    # ... procesar mensaje, invocar grafo ...

    # Tasks en background
    background_tasks.add_task(archive_conversation, thread_id, messages)

    if escalated:
        background_tasks.add_task(notify_escalation, thread_id, client_name)

    return {"status": "ok"}
```

**¿Cuándo migrar a Celery?**

La señal concreta es cuando aparece UNO de estos:
1. **Tasks que no pueden perderse** en un crash del server (ej: confirmación de pedido al ERP en Fase 4)
2. **Tasks que necesitan retry automático** (ej: envío de plantillas WhatsApp con rate limiting)
3. **Tasks CPU-bound** (ej: procesamiento batch de analytics)
4. **Queue depth visible** — querés saber cuántos tasks están pendientes

Para este proyecto, eso es Fase 2-3 realísticamente.

**Tradeoff**: BackgroundTasks pierde tasks si el proceso muere. Para MVP con 10 usuarios internos, esto es aceptable — el impacto es que un log no se archiva o una notificación de Slack no se envía. No se pierde un pedido (eso se guarda síncronamente).

**Prioridad**: MUST-HAVE (remover Celery del MVP). Agregar Celery en Fase 2 cuando haya tasks con garantía de entrega.

---

### 1.5 Deduplicación de Mensajes desde Day 1

**El Problema**

Meta envía webhooks duplicados. Esto es un comportamiento DOCUMENTADO y ESPERADO:

1. **Reintentos por timeout**: Si tu webhook no responde HTTP 200 en <5s, Meta reenvía. Puede reenviar hasta 3 veces.
2. **Reintentos por error**: HTTP 5xx → reenvío automático.
3. **Duplicados "fantasma"**: En condiciones de alta carga, Meta puede enviar el mismo evento más de una vez incluso si respondiste 200. Está en su documentación.

Sin deduplicación, un mensaje "dame dos cajones de rubia" se procesa 2-3 veces → 2-3 items duplicados en el carrito → pedido inflado → cliente enojado → credibilidad del bot destruida.

**La clave de idempotencia de Meta:**

Cada mensaje de Meta viene con un ID único en el payload:

```json
{
  "entry": [{
    "changes": [{
      "value": {
        "messages": [{
          "id": "wamid.HBgLNTQ5MTIzNDU2NzgVAgASGBQzRUI...",
          "from": "5491123456789",
          "timestamp": "1711627200",
          "text": {"body": "dame dos cajones de rubia"}
        }]
      }
    }]
  }]
}
```

El campo `messages[].id` (prefijo `wamid.`) es el idempotency key. Es ÚNICO por mensaje y ESTABLE entre reintentos.

**Implementación: Middleware con Redis SET**

```python
import redis
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

class WebhookDeduplicationMiddleware(BaseHTTPMiddleware):
    """Deduplica webhooks de Meta usando message_id en Redis."""

    def __init__(self, app, redis_conn: redis.Redis, ttl: int = 300):
        super().__init__(app)
        self.redis = redis_conn
        self.ttl = ttl  # 5 minutos — suficiente para cubrir reintentos de Meta

    async def dispatch(self, request: Request, call_next):
        if request.url.path != "/webhook" or request.method != "POST":
            return await call_next(request)

        # Leer body (necesitamos cachearlo para que el handler lo pueda leer)
        body = await request.body()
        message_ids = self._extract_message_ids(body)

        if not message_ids:
            return await call_next(request)

        # Checkear si TODOS los message_ids ya fueron procesados
        new_ids = []
        for msg_id in message_ids:
            key = f"dedup:{msg_id}"
            # SET NX = solo setea si no existe. Retorna True si es nuevo.
            is_new = self.redis.set(key, "1", nx=True, ex=self.ttl)
            if is_new:
                new_ids.append(msg_id)

        if not new_ids:
            # Todos los mensajes ya fueron procesados — responder 200 y cortar
            return Response(status_code=200, content="duplicate")

        # Hay mensajes nuevos — pasar al handler
        # Inyectar los IDs nuevos para que el handler sepa cuáles procesar
        request.state.new_message_ids = set(new_ids)
        return await call_next(request)

    def _extract_message_ids(self, body: bytes) -> list[str]:
        """Extrae message IDs del payload de Meta."""
        import json
        try:
            data = json.loads(body)
            ids = []
            for entry in data.get("entry", []):
                for change in entry.get("changes", []):
                    for msg in change.get("value", {}).get("messages", []):
                        if "id" in msg:
                            ids.append(msg["id"])
            return ids
        except (json.JSONDecodeError, KeyError):
            return []
```

**Uso en el webhook handler:**

```python
@app.post("/webhook")
async def handle_webhook(request: Request):
    # Solo procesar mensajes nuevos
    new_ids = getattr(request.state, "new_message_ids", None)
    if not new_ids:
        return {"status": "duplicate_skipped"}

    payload = await request.json()
    for message in extract_messages(payload):
        if message["id"] in new_ids:
            await process_message(message)

    return {"status": "ok"}
```

**¿Por qué Redis y no PostgreSQL para dedup?**

| Aspecto | Redis SET NX | PostgreSQL INSERT ... ON CONFLICT |
|---------|-------------|----------------------------------|
| Latencia | <1ms | 2-5ms |
| TTL automático | SÍ (nativo) | Hay que hacer cleanup manual |
| En el hot path del webhook | 1 round trip | 1 round trip + I/O disco |
| Persistencia | No (volatile) | Sí |

Redis es perfecto porque: la dedup es efímera (solo necesitás 5 minutos), es hot path (cada request pasa por acá), y ya tenés Redis en la infra.

**Tradeoff**: Si Redis se cae, perdés la dedup y podés procesar duplicados. Mitigación: loguear el message_id en PostgreSQL DESPUÉS del procesamiento (async, en BackgroundTasks), y hacer un check secundario contra PostgreSQL si Redis no está disponible.

**Prioridad**: MUST-HAVE para MVP. Sin dedup, el primer día que Meta reintente un webhook vas a tener pedidos duplicados. Es un bug que destruye la confianza del usuario.

---

### 1.6 Conversation Test Harness

**El Problema**

Testear un sistema basado en LLM es fundamentalmente distinto a testear código determinista. La misma pregunta puede generar respuestas diferentes cada vez. ¿Cómo testear algo no-determinista?

**Arquitectura del Test Harness**

```
tests/
├── conftest.py              # Fixtures compartidos
├── conversations/           # JSON fixtures de conversaciones
│   ├── happy_path_simple.json
│   ├── happy_path_multi_item.json
│   ├── disambiguation.json
│   ├── modify_after_confirm.json
│   ├── escalation.json
│   └── edge_cases/
│       ├── empty_message.json
│       ├── ttl_boundary.json
│       └── unknown_product.json
├── test_conversation_flows.py   # Tests parametrizados
├── test_rag_matching.py         # Tests de matching aislado
├── evaluators.py                # Funciones de evaluación semántica
└── mocks/
    ├── llm_mock.py              # Mock del LLM con respuestas fijas
    └── whatsapp_mock.py         # Mock del webhook de Meta
```

**Formato de los JSON fixtures:**

```json
{
  "name": "happy_path_simple_order",
  "description": "Cliente conocido hace un pedido simple de un item",
  "tags": ["happy_path", "single_item", "mvp"],
  "setup": {
    "client": {
      "phone_number": "+5491123456789",
      "name": "Roberto",
      "business_type": "almacen"
    },
    "catalog_items": ["CS-RUB-970-R", "CS-SA-340-6"]
  },
  "turns": [
    {
      "user": "Hola",
      "assertions": {
        "phase_after": "greeting",
        "response_contains_any": ["Roberto", "Hola", "buenos días"],
        "response_not_contains": ["error", "no puedo"],
        "cart_length": 0
      }
    },
    {
      "user": "Dame dos cajones de la rubia",
      "assertions": {
        "phase_after": "ordering",
        "cart_length": 1,
        "cart_contains": {
          "sku": "CS-RUB-970-R",
          "quantity": 2
        },
        "response_semantic": "confirma que se agregaron dos cajones de cerveza rubia"
      }
    },
    {
      "user": "Listo, confirmá",
      "assertions": {
        "phase_after": "confirmed",
        "order_created": true,
        "response_contains_any": ["confirmado", "pedido", "número"]
      }
    }
  ]
}
```

**Estrategia de testing en 3 capas:**

**Capa 1: Deterministic (sin LLM) — 80% de los tests**

```python
import pytest
from unittest.mock import AsyncMock, patch

class MockLLM:
    """LLM mock que devuelve respuestas scripted por intención."""

    RESPONSES = {
        "greeting": "¡Hola Roberto! ¿Qué te puedo anotar hoy?",
        "order_confirm": "Perfecto, te anoto 2 cajones de Cerveza Salta Rubia.",
        "confirm": "Tu pedido #1234 está confirmado. ¡Gracias Roberto!"
    }

    async def ainvoke(self, messages, **kwargs):
        # Determinar intención por keywords en el último mensaje
        last_msg = messages[-1].content.lower()
        if "hola" in last_msg:
            return AIMessage(content=self.RESPONSES["greeting"])
        # ... etc

@pytest.fixture
def mock_graph(mock_llm, redis_conn, db_session):
    """Grafo completo con LLM mockeado."""
    with patch("app.agents.get_llm", return_value=mock_llm):
        graph = build_graph(checkpointer=MemorySaver())  # In-memory, no Redis
        yield graph
```

Esto testea: routing, estado, transiciones, persistencia del carrito, lógica de negocio.

**Capa 2: RAG Matching aislado (sin LLM principal, con embeddings reales)**

```python
import pytest

# Fixtures de expresiones coloquiales argentinas
RAG_TEST_CASES = [
    # (input, expected_sku, min_score)
    ("cajones de la rubia", "CS-RUB-970-R", 0.85),
    ("salta rubia", "CS-RUB-970-R", 0.90),
    ("six de sin alcohol", "CS-SA-340-6", 0.85),
    ("cerveza sin", "CS-SA-340-6", 0.80),
    ("agua con gas", "AGM-GAS-500", 0.88),
    ("agüita", "AGM-GAS-500", 0.70),  # Esperamos score bajo
    ("dos cajones de la negra", "CS-NEG-970-R", 0.85),
    ("la de siempre", None, None),  # Debería NO matchear
]

@pytest.mark.parametrize("query,expected_sku,min_score", RAG_TEST_CASES)
async def test_rag_matching(rag_service, query, expected_sku, min_score):
    results = await rag_service.search(query, top_k=3)

    if expected_sku is None:
        # No debería matchear con score alto
        assert not results or results[0].score < 0.82
        return

    assert len(results) > 0
    top_result = results[0]
    assert top_result.sku == expected_sku
    assert top_result.score >= min_score
```

Este test ES determinista porque embeddings son deterministas (mismo input → mismo vector). Lo corrés contra la DB real (o un test DB con fixture de catálogo).

**Capa 3: LLM Integration (con LLM real, pero evaluación semántica)**

```python
@pytest.mark.integration
@pytest.mark.slow
async def test_full_conversation_with_real_llm(real_graph, conversation_fixture):
    """Test end-to-end con LLM real. Solo para CI nightly, no en cada push."""
    config = {"configurable": {"thread_id": "test-" + str(uuid4())}}

    for turn in conversation_fixture["turns"]:
        result = await real_graph.ainvoke(
            {"pending_message": turn["user"]},
            config=config
        )

        # Assertions estructurales (deterministas)
        if "phase_after" in turn["assertions"]:
            assert result["phase"] == turn["assertions"]["phase_after"]

        if "cart_length" in turn["assertions"]:
            assert len(result["cart"]) == turn["assertions"]["cart_length"]

        # Assertion semántica (usa LLM como judge)
        if "response_semantic" in turn["assertions"]:
            is_match = await semantic_eval(
                response=result["response"],
                expected_meaning=turn["assertions"]["response_semantic"]
            )
            assert is_match, f"Response '{result['response']}' no matchea semánticamente"
```

**Evaluador semántico (LLM-as-judge):**

```python
async def semantic_eval(response: str, expected_meaning: str) -> bool:
    """Usa un LLM barato para evaluar si la respuesta tiene el significado esperado."""
    eval_prompt = f"""Evaluá si la RESPUESTA transmite el SIGNIFICADO ESPERADO.
Respondé SOLO "SÍ" o "NO".

RESPUESTA: {response}
SIGNIFICADO ESPERADO: {expected_meaning}"""

    result = await haiku_llm.ainvoke([HumanMessage(content=eval_prompt)])
    return "sí" in result.content.lower()
```

**Tradeoff**: La capa 3 es no-determinista y más lenta (~2-5s por turno). Correrla solo en CI nightly o antes de deploy, no en cada commit. Las capas 1 y 2 se corren en cada push (<10s total).

**Prioridad**: MUST-HAVE para MVP (capas 1 y 2). La capa 3 puede esperar a Fase 2 pero el HARNESS (la estructura de fixtures + parametrize) debe existir desde day 1.

---

## Parte 2: Gaps

---

### 2.1 Testing Strategy (más allá del harness)

**Impacto si no se aborda**: Cambiar un prompt rompe un flujo que no tocaste. No te enterás hasta que un cliente se queja. En un sistema con LLMs, los bugs son sutiles — no te tiran un 500, te dan una respuesta incorrecta que parece correcta.

**Pirámide de testing para un sistema multi-agente con LLM:**

```
                    ┌──────────────┐
                    │  E2E/Smoke   │  ← 5-10 tests, LLM real, CI nightly
                    │  (Capa 3)    │
                   ┌┴──────────────┴┐
                   │  Integration   │  ← 20-30 tests, LLM real, pre-deploy
                   │  (Agent + RAG) │
                  ┌┴────────────────┴┐
                  │  RAG Matching    │  ← 100+ cases, determinista, cada push
                  │  (Capa 2)        │
                 ┌┴──────────────────┴┐
                 │  Unit / Graph      │  ← 50+ tests, mock LLM, cada push
                 │  (Capa 1)          │
                ┌┴────────────────────┴┐
                │  Contract / API      │  ← Webhook schema, response format
                └──────────────────────┘
```

**Tests adicionales al harness:**

```python
# test_webhook_contract.py — Verifica que el webhook parsea correctamente
@pytest.mark.parametrize("payload_file", glob("tests/payloads/*.json"))
async def test_webhook_parses_meta_payload(client, payload_file):
    payload = json.loads(Path(payload_file).read_text())
    response = await client.post("/webhook", json=payload)
    assert response.status_code == 200

# test_state_transitions.py — Verifica transiciones válidas
VALID_TRANSITIONS = {
    "greeting": ["catalog", "ordering"],
    "catalog": ["ordering", "greeting"],
    "ordering": ["confirm_pending", "ordering", "catalog"],
    "confirm_pending": ["confirmed", "ordering"],
    "confirmed": ["modify"],
    "modify": ["confirmed"],
}

@pytest.mark.parametrize("from_phase,to_phase", [
    ("greeting", "confirmed"),  # Inválida — no podés saltar
    ("confirmed", "greeting"),  # Inválida
])
async def test_invalid_transitions_blocked(graph, from_phase, to_phase):
    # ... setup state con from_phase, intentar transición a to_phase
    # Debe quedarse en from_phase o ir a una fase intermedia válida
    pass

# test_prompt_regression.py — Golden file tests para prompts
def test_system_prompt_unchanged():
    """Si cambiás el system prompt, este test falla. Tenés que actualizar el golden file."""
    current = load_system_prompt()
    golden = Path("tests/golden/system_prompt.txt").read_text()
    assert current == golden, "System prompt changed! Update golden file if intentional."
```

**Cuándo abordar**: Fase 1 (MVP). El harness + unit + RAG tests son ~3 días de setup. Integration y E2E se agregan incrementalmente.

**Esfuerzo**: Medium (3-5 días para el setup completo del framework, después los tests individuales son rápidos de agregar).

---

### 2.2 Prompt Versioning

**Impacto si no se aborda**: Cambiás el system prompt, el bot empieza a responder distinto, no podés volver atrás fácilmente. En A/B testing necesitás correr dos versiones del prompt en paralelo. Sin versionado, hacés "edit and pray".

**Solución propuesta: Files + Git + config flag**

Para un sistema FastAPI/Python, la solución más simple y efectiva es archivos de texto versionados en Git con un selector por configuración:

```
app/
├── prompts/
│   ├── __init__.py
│   ├── registry.py          # Carga y selecciona versiones
│   ├── system/
│   │   ├── v1.txt           # System prompt original
│   │   ├── v2.txt           # System prompt mejorado
│   │   └── current -> v1.txt  # Symlink o config
│   ├── classify_intent/
│   │   ├── v1.txt
│   │   └── v2.txt
│   └── summarize/
│       └── v1.txt
```

```python
# app/prompts/registry.py
from pathlib import Path
from functools import lru_cache

PROMPTS_DIR = Path(__file__).parent

@lru_cache(maxsize=32)
def get_prompt(name: str, version: str = "current") -> str:
    """Carga un prompt por nombre y versión.

    Usage:
        get_prompt("system")          # última versión activa
        get_prompt("system", "v1")    # versión específica
    """
    prompt_dir = PROMPTS_DIR / name

    if version == "current":
        # Leer de config o env var
        version = _get_active_version(name)

    prompt_file = prompt_dir / f"{version}.txt"
    if not prompt_file.exists():
        raise FileNotFoundError(f"Prompt {name}/{version} not found")

    return prompt_file.read_text().strip()

def _get_active_version(name: str) -> str:
    """Lee la versión activa de un config file o env var."""
    # Opción 1: env var (PROMPT_SYSTEM_VERSION=v2)
    import os
    env_key = f"PROMPT_{name.upper()}_VERSION"
    return os.getenv(env_key, "v1")
```

```python
# Uso en el agente:
from app.prompts.registry import get_prompt

system_prompt = get_prompt("system")  # Lee la versión activa
classify_prompt = get_prompt("classify_intent", version="v2")  # Versión específica
```

**¿Por qué NO en base de datos?**

Para este proyecto, prompts en DB agrega complejidad sin beneficio real:
- Los prompts cambian CON el código (nueva versión del agente → nuevo prompt)
- Git te da diff, blame, revert, branching GRATIS
- No necesitás cambiar prompts en runtime sin deploy (para eso está A/B con env vars)

**¿Cuándo SÍ necesitarías DB?** Si querés que un usuario no-técnico edite prompts desde un dashboard. Eso es Fase 4+.

**Cuándo abordar**: Fase 1 (MVP). Es simplemente organizar los prompts en archivos en vez de hardcodearlos en strings de Python. Cero overhead.

**Esfuerzo**: Small (1-2 días). La mayor parte es mover prompts de strings inline a archivos.

---

### 2.3 Degradación Graceful: Mapa completo de dependencias

**Impacto si no se aborda**: Cualquier caída de una dependencia externa tira abajo TODO el bot. El PRD solo contempla fallo de LLM. Pero PostgreSQL, Redis, Meta API y la API de embeddings también pueden fallar.

**Mapa de fallos y fallbacks:**

| Dependencia | Probabilidad de fallo | Impacto sin fallback | Modo de fallo | Fallback propuesto | Complejidad |
|-------------|----------------------|---------------------|---------------|-------------------|-------------|
| **Anthropic API (LLM)** | Baja-Media | Total — bot no responde | Timeout, 5xx, rate limit | Mensaje fijo: "Estamos con problemas técnicos, te contacta un preventista" | Baja |
| **OpenAI API (embeddings)** | Baja | RAG no funciona → no matchea productos | Timeout, 5xx | Cache de embeddings en pgvector. Solo falla si hay producto NUEVO que necesita embedding. Para queries, el embedding del query falla → fallback a búsqueda por keyword (ILIKE) | Media |
| **PostgreSQL** | Muy baja | Total — no hay datos | Connection refused, timeout | NO hay fallback razonable. Si PostgreSQL se cae, el bot no puede operar. Retry con backoff + alerta inmediata a ops. | N/A |
| **Redis** | Baja | Pérdida de estado conversacional | Connection refused, OOM | Fallback a `MemorySaver` in-process (pierde estado entre restarts pero sigue funcionando por sesión). O: PostgreSQL como checkpointer de emergencia. | Media |
| **Meta WhatsApp API** | Baja | No podés enviar respuestas | Rate limit, 5xx | Queue + retry con backoff exponencial. Si fallo persistente: loguear respuesta pendiente, enviar cuando Meta vuelva. | Media |
| **pgvector (extensión)** | Muy baja | RAG no funciona | Extension crash | Fallback a ILIKE search en tabla de catálogo: `WHERE description ILIKE '%rubia%'` | Baja |

**Implementación del circuit breaker básico:**

```python
import time
from enum import Enum

class CircuitState(Enum):
    CLOSED = "closed"      # Funcionando normal
    OPEN = "open"          # Cortado — usar fallback
    HALF_OPEN = "half_open"  # Probando si se recuperó

class SimpleCircuitBreaker:
    def __init__(self, failure_threshold: int = 3, recovery_timeout: int = 30):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.failures = 0
        self.state = CircuitState.CLOSED
        self.last_failure_time = 0

    def record_failure(self):
        self.failures += 1
        self.last_failure_time = time.time()
        if self.failures >= self.failure_threshold:
            self.state = CircuitState.OPEN

    def record_success(self):
        self.failures = 0
        self.state = CircuitState.CLOSED

    def can_execute(self) -> bool:
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.OPEN:
            if time.time() - self.last_failure_time > self.recovery_timeout:
                self.state = CircuitState.HALF_OPEN
                return True
            return False
        return True  # HALF_OPEN — probar

# Uso:
llm_breaker = SimpleCircuitBreaker(failure_threshold=3, recovery_timeout=60)
embedding_breaker = SimpleCircuitBreaker(failure_threshold=2, recovery_timeout=30)
```

**Fallback de RAG a keyword search:**

```python
async def search_product(query: str) -> list[ProductMatch]:
    """Busca producto: primero RAG, fallback a keyword."""
    if embedding_breaker.can_execute():
        try:
            results = await rag_search(query)
            embedding_breaker.record_success()
            return results
        except Exception:
            embedding_breaker.record_failure()

    # Fallback: keyword search
    return await keyword_search(query)

async def keyword_search(query: str) -> list[ProductMatch]:
    """Búsqueda por keywords cuando RAG no está disponible."""
    words = query.lower().split()
    conditions = " AND ".join([f"LOWER(description) LIKE '%{w}%'" for w in words])
    # NOTA: Usar parámetros, no string interpolation (esto es pseudo-código)
    rows = await db.fetch(f"SELECT * FROM catalog_embeddings WHERE {conditions} AND active LIMIT 5")
    return [ProductMatch(sku=r["sku"], description=r["description"], score=0.5) for r in rows]
```

**Cuándo abordar**: Fase 1 para LLM fallback y Redis degradation (ya definidos en PRD). PostgreSQL y embedding fallbacks en Fase 2. Circuit breaker completo en Fase 3.

**Esfuerzo**: Medium (3-5 días). La mayor parte es definir los paths de fallback y testearlos, no tanto código.

---

### 2.4 Human Handoff Interface

**Impacto si no se aborda**: El `escalate_node` del PRD dispara una notificación... ¿a dónde? ¿El preventista abre WhatsApp y le escribe al cliente "a mano"? ¿Cómo sabe qué estaba pidiendo el cliente? Sin esto, la experiencia de escalado es desastrosa.

**Solución mínima viable (Slack-based):**

No construir un dashboard de handoff en Fase 1. Usar Slack (que BADIE ya usa o puede adoptar trivialmente).

```
Flujo de escalado:
1. Bot detecta necesidad de escalado
2. Bot envía al cliente: "Te paso con un preventista que te va a ayudar en unos minutos"
3. Bot envía a canal #escalados de Slack:
   - Nombre del cliente + teléfono
   - Resumen de la conversación (del summary del state)
   - Carrito actual (si hay items)
   - Razón del escalado
   - Botón "Tomar caso" (Slack interactive)
4. Preventista toca "Tomar caso" → se marca como asignado
5. Preventista abre WhatsApp y escribe al cliente directamente
6. Bot se pone en modo "silencioso" para ese thread por 2 horas
```

**Implementación del mensaje a Slack:**

```python
async def escalate_to_human(state: ConversationState, reason: str):
    """Notifica a Slack y pone el bot en modo silencioso."""

    cart_summary = "\n".join(
        f"  • {item['quantity']}x {item['description']}"
        for item in state["cart"]
    ) or "  (carrito vacío)"

    slack_payload = {
        "channel": SLACK_ESCALATION_CHANNEL,
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"🔴 Escalado: {state['client_name']}"}
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Teléfono:*\n{state['thread_id']}"},
                    {"type": "mrkdwn", "text": f"*Razón:*\n{reason}"},
                ]
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Resumen conversación:*\n{state['summary']}"}
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Carrito actual:*\n{cart_summary}"}
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Tomar caso"},
                        "style": "primary",
                        "action_id": f"take_case_{state['thread_id']}"
                    }
                ]
            }
        ]
    }

    async with httpx.AsyncClient() as client:
        await client.post(SLACK_WEBHOOK_URL, json=slack_payload)

    # Poner bot en modo silencioso para este thread
    await redis_conn.set(
        f"silenced:{state['thread_id']}",
        "1",
        ex=7200  # 2 horas
    )
```

**Modo silencioso en el webhook:**

```python
@app.post("/webhook")
async def handle_webhook(request: Request):
    # ... extraer message ...

    # Chequear si el thread está silenciado (escalado activo)
    is_silenced = await redis_conn.get(f"silenced:{thread_id}")
    if is_silenced:
        # No responder — el preventista está atendiendo
        # Opcionalmente: loguear el mensaje para que el preventista lo vea
        return {"status": "silenced"}

    # Procesar normalmente...
```

**Cuándo abordar**: Fase 1 (MVP). El piloto con 10 usuarios internos VA a generar escalados. Si no hay interfaz de handoff, el escalado es un "no funciono más, chau".

**Esfuerzo**: Small (1-2 días). Es un mensaje de Slack con formato + un flag en Redis.

---

### 2.5 Multi-Item Disambiguation Protocol

**Impacto si no se aborda**: "Dame dos cajones de rubia, tres six de sin alcohol y una agua" es un mensaje REAL que los clientes van a mandar. Si el bot solo matchea el primer item, o intenta matchear el string completo como un solo producto, la experiencia es terrible.

**Diseño del flujo de parsing + partial match:**

```
Mensaje: "Dame dos cajones de rubia, tres six de sin alcohol y una agua"

PASO 1: Extracción de items (LLM - Haiku)
   Prompt: "Extraé cada item pedido del mensaje. Formato JSON."
   Output: [
     {"expression": "dos cajones de rubia", "quantity": 2, "unit": "cajón"},
     {"expression": "tres six de sin alcohol", "quantity": 3, "unit": "six"},
     {"expression": "una agua", "quantity": 1, "unit": "unidad"}
   ]

PASO 2: RAG matching para CADA item (paralelo)
   "cajones de rubia"    → CS-RUB-970-R (score: 0.95) ✅ Match directo
   "six de sin alcohol"  → CS-SA-340-6  (score: 0.89) ✅ Match directo
   "una agua"            → [AGM-GAS-500 (0.84), AGM-SIN-500 (0.83)] ⚠️ Ambiguo

PASO 3: Respuesta con confirmación parcial
   Bot: "Perfecto Roberto, te anoto:
   ✅ 2 cajones de Cerveza Salta Rubia 970ml - $X
   ✅ 3 six de Cerveza Salta Sin Alcohol 340ml - $X
   ❓ La agua, ¿cuál querés?
     1. Agua mineral con gas 500ml
     2. Agua mineral sin gas 500ml"

PASO 4: Cliente responde "con gas"
   → Matchear solo el item pendiente → agregar al carrito
```

**Implementación del extraction prompt:**

```python
EXTRACT_ITEMS_PROMPT = """Sos un parser de pedidos. Extraé cada artículo pedido del mensaje del cliente.

Reglas:
- Cada item tiene: expresión original, cantidad, unidad (cajón, six, pack, unidad, botella)
- Si no se especifica cantidad, asumí 1
- Si no se especifica unidad, asumí "unidad"
- Respuesta SOLO en JSON, sin explicaciones

Mensaje del cliente: {message}

Respuesta (JSON array):"""

async def extract_items(message: str) -> list[dict]:
    """Usa Haiku para extraer items individuales de un mensaje multi-item."""
    response = await haiku_llm.ainvoke([
        HumanMessage(content=EXTRACT_ITEMS_PROMPT.format(message=message))
    ])
    return json.loads(response.content)
```

**Manejo del estado de disambiguation:**

```python
class ConversationState(TypedDict):
    # ... campos existentes ...

    # Nuevo: items pendientes de desambiguación
    pending_disambiguation: list[dict]  # Items con score ambiguo
    # Formato: [{"expression": "agua", "candidates": [...], "quantity": 1, "unit": "unidad"}]
```

**Flujo completo en el order_agent:**

```python
async def order_agent(state: ConversationState) -> ConversationState:
    message = state["pending_message"]

    # Si hay disambiguation pendiente, el mensaje es la respuesta
    if state.get("pending_disambiguation"):
        return await resolve_disambiguation(state)

    # Extraer items del mensaje
    items = await extract_items(message)

    confirmed = []
    ambiguous = []

    # Buscar cada item en paralelo
    tasks = [rag_search(item["expression"]) for item in items]
    results = await asyncio.gather(*tasks)

    for item, matches in zip(items, results):
        if not matches or matches[0].score < 0.82:
            ambiguous.append({
                "expression": item["expression"],
                "quantity": item["quantity"],
                "unit": item["unit"],
                "candidates": [],  # No match
                "status": "no_match"
            })
        elif matches[0].score >= 0.92:
            confirmed.append(OrderItem(
                sku=matches[0].sku,
                description=matches[0].description,
                quantity=item["quantity"],
                unit=item["unit"],
                unit_price=matches[0].price,
            ))
        else:
            ambiguous.append({
                "expression": item["expression"],
                "quantity": item["quantity"],
                "unit": item["unit"],
                "candidates": [{"sku": m.sku, "desc": m.description, "score": m.score}
                              for m in matches[:3]],
                "status": "ambiguous"
            })

    # Agregar confirmados al carrito
    new_cart = state["cart"] + confirmed

    return {
        **state,
        "cart": new_cart,
        "pending_disambiguation": ambiguous,
    }
```

**Cuándo abordar**: Fase 1 (MVP). El parsing multi-item es la ESENCIA del producto. Sin esto, el bot solo acepta un item por mensaje, lo que es más lento que llamar al preventista.

**Esfuerzo**: Medium (3-5 días). La extracción con Haiku es simple. La complejidad está en el manejo de estado con items parcialmente confirmados y la UX de la desambiguación.

---

### 2.6 phone_number como thread_id: Riesgos y mitigación

**Impacto si no se aborda**: Casos concretos que van a pasar:

1. **Teléfono compartido**: Un kiosco tiene UN teléfono. El dueño pide a las 8am, la esposa quiere pedir a las 2pm. Misma thread → el bot cree que es el mismo cliente, puede confundir el carrito.

2. **Cambio de número**: El cliente cambia de chip/número. Pierde todo el historial. El bot no lo reconoce.

3. **WhatsApp multi-device**: Un cliente tiene WhatsApp en el celular Y en la PC. Manda mensajes desde ambos al mismo tiempo. Meta los envía como mensajes separados del mismo número, pero podés tener race conditions en el estado.

4. **Portabilidad numérica**: En Argentina, la portabilidad existe. Un cliente cambia de carrier, mantiene el número pero Meta puede tener un período de transición donde el `from` field cambia de formato.

**Mitigación mínima (sin cambiar la arquitectura):**

```python
# 1. Agregar un "client_id" como capa de indirección
# thread_id sigue siendo phone_number (para LangGraph)
# pero el client_id viene de la DB

async def resolve_client(phone_number: str) -> Client | None:
    """Busca cliente por phone_number. Soporta múltiples números por cliente."""
    return await db.fetchone(
        "SELECT * FROM clients WHERE phone_number = $1 AND active",
        phone_number
    )

# 2. Para teléfonos compartidos: comando "CAMBIAR CLIENTE"
# Si en el mismo thread aparece otro nombre, el bot pregunta:
# "¿Estás pidiendo como Roberto (Almacén Don Roberto) o sos otra persona?"

# 3. Para cambio de número: endpoint admin
@app.post("/admin/update-phone")
async def update_client_phone(client_id: int, new_phone: str):
    """Permite al admin migrar un cliente a nuevo número."""
    await db.execute(
        "UPDATE clients SET phone_number = $1 WHERE id = $2",
        new_phone, client_id
    )
```

**Para Fase 2+: tabla de phone_numbers separada**

```sql
-- Separar phone_number de clients
CREATE TABLE client_phones (
    id          SERIAL PRIMARY KEY,
    client_id   INT REFERENCES clients(id),
    phone       VARCHAR(20) NOT NULL UNIQUE,
    is_primary  BOOLEAN DEFAULT TRUE,
    added_at    TIMESTAMPTZ DEFAULT NOW()
);

-- Un cliente puede tener múltiples números
-- Un número solo puede pertenecer a un cliente
```

**Cuándo abordar**: El mapping `phone → client_id` en Fase 1. La tabla `client_phones` en Fase 2. El manejo de teléfonos compartidos en Fase 3 (requiere UX design).

**Esfuerzo**: Small (1-2 días para el mapping básico). Medium para la tabla separada + migración.

---

### 2.7 Observabilidad a nivel de request

**Impacto si no se aborda**: Cuando un cliente dice "el bot me respondió cualquier cosa", no tenés forma de saber QUÉ pasó. Sin tracing, debuggear un sistema multi-agente es adivinanza. Cada mensaje pasa por 3-5 pasos internos (webhook → dedup → routing → LLM → RAG → respuesta). ¿Cuál falló? ¿Cuánto tardó cada uno?

**Solución mínima para Fase 1: Structured logging con correlation ID**

No meter OpenTelemetry ni Jaeger en el MVP. Structured logging con un `request_id` que acompaña todo el flujo es suficiente.

```python
import structlog
import uuid
from contextvars import ContextVar

# Context var para el request ID
request_id_ctx: ContextVar[str] = ContextVar("request_id", default="")
thread_id_ctx: ContextVar[str] = ContextVar("thread_id", default="")

def setup_logging():
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
    )

logger = structlog.get_logger()

# Middleware para setear request_id en cada request
class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        rid = str(uuid.uuid4())[:8]
        request_id_ctx.set(rid)
        structlog.contextvars.bind_contextvars(request_id=rid)

        logger.info("webhook.received", path=request.url.path)
        start = time.time()

        response = await call_next(request)

        elapsed_ms = (time.time() - start) * 1000
        logger.info("webhook.completed", status=response.status_code, elapsed_ms=round(elapsed_ms))
        structlog.contextvars.unbind_contextvars("request_id", "thread_id")

        return response
```

**Logging en puntos críticos:**

```python
# En el webhook handler
async def handle_message(message: dict):
    thread_id = message["from"]
    structlog.contextvars.bind_contextvars(thread_id=thread_id)

    logger.info("message.processing", text_length=len(message["text"]["body"]))

    # Deduplicación
    if is_duplicate:
        logger.info("message.deduplicated", message_id=message["id"])
        return

    # Invocación del grafo
    start = time.time()
    result = await graph.ainvoke(...)
    elapsed_ms = (time.time() - start) * 1000

    logger.info("graph.completed",
        phase=result["phase"],
        cart_items=len(result["cart"]),
        elapsed_ms=round(elapsed_ms),
    )

# En el RAG search
async def rag_search(query: str):
    start = time.time()
    results = await db.fetch(...)
    elapsed_ms = (time.time() - start) * 1000

    logger.info("rag.search",
        query=query,
        results_count=len(results),
        top_score=results[0].score if results else None,
        elapsed_ms=round(elapsed_ms),
    )
    return results

# En cada LLM call
async def call_llm(model: str, messages: list):
    start = time.time()
    response = await llm.ainvoke(messages)
    elapsed_ms = (time.time() - start) * 1000

    logger.info("llm.call",
        model=model,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        elapsed_ms=round(elapsed_ms),
    )
    return response
```

**Output (JSON, una línea por log entry):**

```json
{"request_id": "a1b2c3d4", "thread_id": "+5491123456789", "event": "webhook.received", "level": "info", "timestamp": "2026-03-28T14:30:00Z"}
{"request_id": "a1b2c3d4", "thread_id": "+5491123456789", "event": "message.processing", "text_length": 42, "level": "info"}
{"request_id": "a1b2c3d4", "thread_id": "+5491123456789", "event": "rag.search", "query": "cajones de rubia", "results_count": 3, "top_score": 0.95, "elapsed_ms": 12}
{"request_id": "a1b2c3d4", "thread_id": "+5491123456789", "event": "llm.call", "model": "claude-sonnet-4-20250514", "input_tokens": 450, "output_tokens": 85, "elapsed_ms": 1200}
{"request_id": "a1b2c3d4", "thread_id": "+5491123456789", "event": "graph.completed", "phase": "ordering", "cart_items": 1, "elapsed_ms": 1450}
{"request_id": "a1b2c3d4", "event": "webhook.completed", "status": 200, "elapsed_ms": 1480}
```

Con esto podés: filtrar por thread_id para ver toda la conversación de un cliente, filtrar por request_id para ver el flujo de un solo mensaje, hacer analytics sobre latencia por componente, detectar anomalías de tokens.

**Para Fase 2+: agregar OpenTelemetry spans**

```python
# Solo cuando necesités tracing visual (Jaeger, etc.)
pip install opentelemetry-api opentelemetry-sdk opentelemetry-instrumentation-fastapi
```

**Cuándo abordar**: Fase 1 (MVP). El structured logging es 1 día de trabajo y te salva SEMANAS de debugging.

**Esfuerzo**: Small (1-2 días). `structlog` + middleware + log statements en 5-6 puntos del código.

---

## Priority Matrix

| Item | Prioridad | Fase | Esfuerzo | Dependencia |
|------|-----------|------|----------|-------------|
| **1.1** HNSW vs IVFFlat | MUST-HAVE | Fase 1 | Small (1 día) | Ninguna — cambio de SQL |
| **1.2** text-embedding-3-small (512 dims) | MUST-HAVE | Fase 1 | Small (1 día) | Lockear antes de crear tabla |
| **1.3** Sliding TTL Redis | SHOULD-HAVE | Fase 1 (TTL 24h fijo) / Fase 2 (sliding) | Small (1-2 días) | `langgraph-checkpoint-redis` |
| **1.4** FastAPI BackgroundTasks | MUST-HAVE | Fase 1 (remover Celery) | Small (1 día) | Ninguna |
| **1.5** Message deduplication | MUST-HAVE | Fase 1 | Small (1-2 días) | Redis |
| **1.6** Conversation test harness | MUST-HAVE | Fase 1 | Medium (3-5 días) | Estructura del grafo |
| **2.1** Testing strategy completa | MUST-HAVE (capas 1-2) | Fase 1 base / Fase 2 completa | Medium (3-5 días setup) | Test harness (1.6) |
| **2.2** Prompt versioning | SHOULD-HAVE | Fase 1 | Small (1-2 días) | Ninguna |
| **2.3** Graceful degradation | SHOULD-HAVE | Fase 1 (LLM) / Fase 2 (completo) | Medium (3-5 días) | Circuit breaker pattern |
| **2.4** Human handoff interface | MUST-HAVE | Fase 1 | Small (1-2 días) | Slack workspace |
| **2.5** Multi-item disambiguation | MUST-HAVE | Fase 1 | Medium (3-5 días) | RAG + extraction prompt |
| **2.6** phone_number → client_id mapping | SHOULD-HAVE | Fase 1 (básico) / Fase 2 (multi-phone) | Small (1-2 días) | Schema de clients |
| **2.7** Request-level observability | MUST-HAVE | Fase 1 | Small (1-2 días) | structlog |

**Resumen de esfuerzo Fase 1:**

- MUST-HAVE total: ~15-22 días de desarrollo
- SHOULD-HAVE total: ~5-9 días adicionales
- La Fase 1 del PRD dice 3 semanas (15 días hábiles). Con los ajustes MUST-HAVE, son 4-5 semanas más realistas.