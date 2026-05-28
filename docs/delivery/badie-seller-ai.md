# BADIE Delivery Scope: Seller AI

## What is promised to the client

The committed delivery for **Distribuidora BADIE S.A.** (Grupo Manzur) is the **Seller AI / Preventa agent**: a WhatsApp-based conversational agent that receives product orders from retail points of sale, interprets them in colloquial Argentine Spanish, matches products via semantic search over the beverage catalog, and persists confirmed orders.

No other platform agents are part of this delivery scope. Employee agents, summary agents, data agents, and local workstation runtimes are platform capabilities or future roadmap options — they are not committed to BADIE unless explicitly re-scoped.

> **Open decision (5):** The exact contract boundary between platform IP and client-specific implementation has not been formalized. Specifically: agent definition folders, skill prompt modules, and tool configurations developed for BADIE may encode domain knowledge that is both platform-generic and client-specific. The classification of these artifacts (platform-owned vs. client-owned) must be resolved before licensing or multi-client deployment is considered.

---

## Business context

**Company:** Distribuidora BADIE S.A., part of Grupo Manzur. Argentine beverage distributor covering major brands: Quilmes, Brahma, Stella Artois, CCU, Branca, among others.

**Domain:** BADIE operates a network of field sellers called *preventistas* who visit retail points of sale — kiosks, convenience stores, bars, restaurants — and take orders manually. The Seller AI digitizes this process so that points of sale can place orders via WhatsApp at any time, without waiting for a preventista visit.

**Language and register:** Clients communicate in colloquial Argentine Spanish. Orders frequently contain informal product references, nicknames, and abbreviated descriptions. Examples:
- *"dame dos cajones de la rubia"* → 2 cases of Quilmes lager
- *"tres Brahma lata"* → 3 units of Brahma 473ml can
- *"algo para los chicos, sin alcohol"* → requires clarification (product category, not a specific SKU)

The agent must handle this register fluently. Literal string matching over the catalog is insufficient — semantic retrieval and colloquial vocabulary mapping are required.

**Client data model:**
- Clients (points of sale) are identified by E.164 phone number
- Each client is assigned a price list (`id_lista_precio`) sourced from `gold.dim_cliente` in the medallion warehouse
- Clients must be registered and active before the agent interacts with them; unknown numbers are auto-registered as inactive and do not receive agent responses
- The product catalog is sourced from `gold.dim_articulo` (medallion warehouse), with attributes: `marca`, `generico`, `calibre`, `proveedor`

---

## Roles delivered

### Preventa Agent (primary)

The sole agent role committed to the BADIE delivery.

**Responsible for:**
- Receiving inbound WhatsApp messages from registered clients
- Interpreting product requests in colloquial Argentine Spanish
- Executing semantic catalog search (RAG over `catalog_embeddings`) to match products to SKUs
- Applying the client's assigned price list
- Confirming the order with the client before persisting
- Persisting confirmed orders to the `orders` and `order_items` tables
- Escalating to a human operator when required (unregistered client, ambiguous match after clarification rounds, explicit human request, threshold-exceeding order)

**Does not do:**
- Answer general business questions
- Look up stock levels or delivery schedules
- Handle returns, complaints, or billing queries
- Operate outside the scope of a single order transaction

### Orchestrator Agent (minimal, routing only)

For the MVP, the Orchestrator Agent is active in a minimal capacity: it receives the inbound trigger (WhatsApp message arriving via webhook), confirms the client is registered and active, and routes the message to a Preventa Agent runtime. It performs no complex orchestration in the initial delivery.

The Orchestrator Agent is not user-facing and is not part of the client-visible product.

---

## Tools injected for the Preventa Agent

| Tool | Connector | Purpose |
|---|---|---|
| `whatsapp_sender` | Meta WhatsApp Cloud API | Send reply messages to the client |
| `rag_catalog_search` | PostgreSQL / pgvector (HNSW) | Semantic product search over embedded catalog |
| `postgres_order_writer` | PostgreSQL | Persist confirmed orders and line items |
| `redis_session_state` | Redis | LangGraph conversation checkpointing, deduplication |
| `client_lookup` | PostgreSQL | Resolve phone number to registered client record |

---

## Skills injected for the Preventa Agent

| Skill | Purpose |
|---|---|
| `order_extraction` | Extract structured order data from unstructured conversational input; handle quantity expressions, multi-item messages, and incomplete specs requiring clarification |
| `colloquial_product_matching` | Map informal Argentine product references to catalog SKUs; interpret RAG results and select best candidate |
| `confirm_flow` | Guide the agent through the order confirmation exchange: present the order summary, handle corrections, and confirm before persisting |

---

## Integration points

### DeW / App Preventas

- **Catalog source:** `gold.dim_articulo` (medallion warehouse) → synced to local `catalog_embeddings` table. The sync pipeline reads from the medallion gold layer and writes embedded vectors to the local PostgreSQL instance.
- **Order destination:** Confirmed orders written to the local `orders` / `order_items` tables. Full integration with DeW's order ingestion pipeline is a subsequent milestone (not MVP).

### Local PostgreSQL

Tables in scope for this delivery:

| Table | Role |
|---|---|
| `clients` | Client registry — phone number, price list assignment, active status |
| `orders` | Order header — client, status, totals |
| `order_items` | Order line items — SKU, description, quantity, price |
| `conversation_logs` | Audit trail of agent/client exchanges |
| `catalog_embeddings` | Vector embeddings of catalog items (512d, HNSW index) |

### Redis

- Webhook deduplication (SET NX, TTL 300s)
- LangGraph conversation checkpointing

### Meta WhatsApp Business API

- Inbound: webhook POST with HMAC-SHA256 signature verification
- Outbound: `whatsapp_sender` tool via the Meta Cloud API

---

## Not in scope for this delivery

The following are explicitly excluded from the BADIE Seller AI delivery:

- **Employee agents** — personal AI assistants for BADIE staff
- **Summary Agent** — meeting and conversation summarization
- **Data Agent** — business intelligence retrieval from App Sergio, Outline, or extended warehouse queries
- **Local runtimes** — Hermes Agent, OpenClaw, PicoClaw workstation deployments
- **Hierarchical child-agent delegation** — the Preventa Agent has `delegation_policy.allowed: false` in its `policy.md`
- **Supervisor pattern orchestration** — Phase 1 uses phase-based routing; Supervisor pattern is a Phase 2 consideration

---

## Development status

| Milestone | Status |
|---|---|
| Webhook reception + HMAC verification | Complete (Paso 1A.1) |
| Webhook deduplication via Redis | Complete (Paso 1A.2) |
| Client lookup + auto-register | Complete (Paso 1A.3) |
| Catalog sync pipeline (medallion → local) | Complete (Paso 1A.4c) |
| Client sync pipeline with E.164 normalization | Complete (Paso 1A.4d) |
| Embedding service abstraction (OpenAI + Fake providers) | Complete (Paso 1A.4b) |
| Local BGE-M3 embedding provider | Complete (Paso 1A.4e) |
| RAG service (pgvector search, thresholds 0.92/0.82) | Complete (Paso 1A.5) |
| RAG test suite (colloquial Argentine expressions) | **Next: Paso 1A.6** |
| LangGraph conversation state (TypedDict) | Pending (Paso 1A.7) |
| Agent implementation (router, retrieval, generation, order creation) | Pending (Paso 1B.x) |
| WhatsApp send client | Pending (Paso 1C.x) |
| Phase 2: Supervisor pattern, Celery, sliding TTL | Future |

---

## Cross-references

- Platform manifesto and Core vs. delivery scope boundary: `docs/platform/manifesto.md`
- Preventa Agent definition folder: `agents/preventa/` — see `docs/platform/role.md` for the folder schema
- Tool definitions: `docs/platform/tool.md`
- Skill definitions: `docs/platform/skill.md`
- Execution pipeline: `docs/platform/harness.md`
- Delegation policy (Preventa has no delegation): `docs/architecture/delegation-policy.md`
- Permission model and connector rules: `docs/architecture/permission-model.md`
- Core architecture overview: `docs/architecture/agent-platform.md`
