## Exploration: Arquitectura — agents-badie

### Current State

**Pre-code.** Solo existen el PRD y un diagrama de arquitectura SVG. No hay código fuente, tests, ni infraestructura desplegada. El PRD es sólido y detallado — cubre stack, modelo de datos, diseño de agentes, RAG, costos y roadmap en 4 fases.

---

### Análisis Crítico de la Arquitectura Propuesta

#### 1. Multi-agent Supervisor Pattern (LangGraph)

- **Para el sistema completo (Fase 2+): SÍ.** Los sub-agentes tienen responsabilidades claramente distintas, herramientas diferentes y modelos LLM diferentes. El Supervisor pattern mapea naturalmente a las fases de conversación definidas en `ConversationState.phase`.
- **Para el MVP (Fase 1): OVERKILL.** Un solo grafo lineal con edges condicionales es suficiente. El MVP es: saludar → tomar pedido → confirmar. Es una máquina de estados de 3-4 estados, no un sistema multi-agente.
- **Tensión:** El PRD dice "single node" para Fase 1 pero el schema de estado ya tiene 6 fases y 8 nodos definidos. Definir un schema MÍNIMO para Fase 1 y evolucionar.

#### 2. LangGraph State Management + Redis

- **Usar `langgraph-checkpoint-redis`** (paquete oficial). Verificar que `summarize_node` REEMPLACE mensajes en el estado, no solo agregue un resumen.
- **TTL 12h debería ser SLIDING** (extender en cada interacción), no fijo desde creación. Caso edge: cliente empieza a las 11pm, TTL vence a las 11am, envía "confirmo" a las 10:30am — estado perdido.

#### 3. RAG con pgvector

- **pgvector reutiliza PostgreSQL existente** — correcto, sin nueva dependencia.
- **IVFFlat con lists=100 es patológico** para un catálogo de 50-500 SKUs (2 items por lista). **Usar HNSW** o al menos `lists=sqrt(n_items)`.
- **Elegir UN modelo de embeddings explícitamente.** El PRD menciona "text-embedding-3-small O voyage-3" — espacios vectoriales incompatibles. **Recomendación: text-embedding-3-small** (más barato, probado para search).
- **La curación de sinónimos ES la calidad del producto.** Empezar desde Fase 1, no Fase 4.

#### 4. Token Optimization

- **La estimación de $0.0045/conversación es OPTIMISTA.** Asume conversación ideal (4 turnos). Conversaciones reales con desambiguación, browsing de catálogo y modificaciones: **$0.006-0.010/conversación**. Sigue siendo muy asequible (~$6-10/día), pero tratar $0.005 como MEDIANA, no techo.
- **Prompt caching bien pensado.** Las definiciones de tools también contribuyen al prefix cacheado — mantenerlas estables.

#### 5. Tres Capas (Integration / Orchestration / Data)

- **Separación correcta**, pero la capa de integración está demasiado delgada. Falta: deduplicación de mensajes (Meta envía duplicados), idempotencia, manejo de delivery/read receipts.
- **La capa de datos mezcla concerns:** datos transaccionales (pedidos) vs infraestructura AI (embeddings, logs). Considerar schemas separados en PostgreSQL.

#### 6. Celery

- Para MVP, **FastAPI `BackgroundTasks`** es suficiente. Celery desde Fase 2.

---

### Gaps No Cubiertos en el PRD

1. **Sin estrategia de testing.** Necesita test harness de conversaciones (inputs/outputs esperados).
2. **Sin versionado de prompts.** Crítico en producción para rollback y A/B testing.
3. **Sin degradación graceful** más allá de fallo de LLM (¿PostgreSQL caído? ¿Redis caído?).
4. **Sin interfaz de handoff a humano.** ¿Dónde ve el preventista el historial cuando `escalate_node` dispara?
5. **Sin protocolo de desambiguación multi-item.** "Dame dos cajones de rubia, tres six de sin alcohol y una agua" — ¿qué pasa con matches parciales?
6. **phone_number como thread_id es frágil.** ¿Cambio de número? ¿Teléfono compartido?
7. **Sin observabilidad a nivel de request** (solo dashboard agregado). Falta tracing distribuido.

---

### Approaches para MVP

| Approach | Descripción | Pros | Cons | Effort |
|----------|-------------|------|------|--------|
| **A. Linear Graph** | Estado único, flujo lineal greeting→ordering→confirm | Rápido, fácil de debuggear | Refactoring significativo en Fase 2 | 2-3 sem |
| **B. Supervisor desde Day 1** | Supervisor pattern completo con stubs | Sin refactoring futuro | Sobre-ingeniería para MVP | 3-4 sem |
| **C. Hybrid (recomendado)** | Grafo único con routing por fase via prompt templates | Schema completo desde day 1, routing determinista, evolución natural a sub-agentes | Manejo de prompts puede crecer | 2-3 sem |

---

### Recommendation

**Approach C (Hybrid) para MVP, evolucionando a Approach B en Fase 2.**

El riesgo central del proyecto NO es la arquitectura — es si el RAG matchea bien el lunfardo cervecero argentino, y si los clientes van a usar un bot en vez de llamar al preventista. El MVP debe optimizarse para validar ESAS hipótesis lo antes posible.

**Decisiones a lockear para el proposal:**
1. MVP: Hybrid (routing por fase, sin Supervisor)
2. Embedding: text-embedding-3-small
3. Vector index: HNSW sobre IVFFlat
4. Async: FastAPI BackgroundTasks para MVP, Celery desde Fase 2
5. Redis TTL: Sliding, no fijo
6. Testing: Conversation test harness como parte de Fase 1
7. Idempotencia: Deduplicación de mensajes desde day 1

---

### Risks

| Riesgo | Severidad | Probabilidad | Mitigación |
|--------|-----------|--------------|------------|
| RAG matching insuficiente para lunfardo argentino | Alta | Media | Invertir en curación de sinónimos ANTES de ir a producción; test set de 100+ expresiones reales |
| Pérdida de estado Redis en boundary de TTL | Media | Media | TTL sliding; persistir pedidos confirmados a PostgreSQL inmediatamente |
| Estimación de costo optimista para conversaciones complejas | Baja | Alta | Presupuestar $0.008-0.010/conversación; alertas en $0.015 |
| IVFFlat patológico para catálogo chico | Media | Alta | Cambiar a HNSW |
| Sin testing → regresiones al cambiar prompts | Alta | Alta | Conversation test harness en Fase 1; versionar prompts |
| Deduplicación no implementada → pedidos duplicados | Alta | Media | Claves de idempotencia desde day 1 |
| Rechazo del bot por clientes | Alta | Media | Piloto gradual; escape "HUMANO" prominente |

---

### Ready for Proposal: YES
