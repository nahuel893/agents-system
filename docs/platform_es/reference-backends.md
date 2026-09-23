# Backends de referencia para los puertos genéricos de la plataforma

ADR-002 C.15. `KnowledgeBase`, `ConversationSummarizer`, `EscalationChannel`
y `OrderWriter` (`src/agentsys/services/{knowledge,summaries,escalation,orders}.py`)
son puertos `Protocol` que la plataforma entrega deliberadamente **sin**
implementación — ver el docstring de cada módulo para el porqué. Al no tener
nada detrás, las herramientas genéricas de plataforma vinculadas a esos
puertos (`connectors/platform_connectors.py`, `connectors/order_connector.py`)
fallan correctamente en modo cerrado, pero eso también significaba que
ninguna prueba en este repositorio ejercitó jamás a un rol recuperando de
verdad datos de una base de conocimiento, resumiendo una conversación real,
registrando un escalamiento o escribiendo un pedido.

`src/agentsys/services/reference.py` entrega cuatro implementaciones de
referencia pequeñas que cierran esa brecha. Son **opcionales** (opt-in):
nada las conecta por defecto, así que una aplicación que no configura
ninguna sigue recibiendo los mismos rechazos en modo cerrado que antes.
Tampoco son integraciones de grado productivo — ver "Qué NO son" más abajo.

---

## Las cuatro clases

| Clase | Implementa | Almacenamiento |
|---|---|---|
| `InMemoryKnowledgeBase` | `KnowledgeBase` | Documentos markdown en memoria, sembrados por vos |
| `LLMConversationSummarizer` | `ConversationSummarizer` | Transcripción en memoria, sembrada por vos; resume con un `BaseChatModel` que ya tenés |
| `LoggingEscalationChannel` | `EscalationChannel` | Ninguno — escribe una entrada de log estructurado |
| `InMemoryOrderWriter` | `OrderWriter` | En memoria, durante la vida del proceso |

## Cómo conectar una

Cada función `build_*_tool_spec` en `platform_connectors.py` /
`order_connector.py` ya acepta `None` (modo cerrado, el valor por defecto) o
una implementación del puerto. Pasá una de estas en su lugar, donde sea que
tu `registry_factory` construya su `ToolRegistry`:

```python
from agentsys.connectors.order_connector import build_order_writer_tool_spec
from agentsys.connectors.platform_connectors import (
    build_conversation_summarizer_tool_spec,
    build_escalation_notifier_tool_spec,
    build_knowledge_retrieval_tool_spec,
)
from agentsys.services.reference import (
    InMemoryKnowledgeBase,
    InMemoryOrderWriter,
    KnowledgeDocument,
    LLMConversationSummarizer,
    LoggingEscalationChannel,
)

knowledge_base = InMemoryKnowledgeBase(
    [KnowledgeDocument(id="returns", title="Política de devoluciones", content="...")]
)
escalation_channel = LoggingEscalationChannel()
order_writer = InMemoryOrderWriter()

# El resumidor reutiliza el MISMO modelo de chat con el que ya está
# configurado el AgentRuntime del rol -- sin configuración de proveedor
# nueva, sin API key nueva.
summarizer = LLMConversationSummarizer(model=tu_modelo_de_chat_configurado)

registry.register(build_knowledge_retrieval_tool_spec(knowledge_base))
registry.register(build_conversation_summarizer_tool_spec(summarizer))
registry.register(build_escalation_notifier_tool_spec(escalation_channel))
registry.register(build_order_writer_tool_spec(order_writer))
```

`LLMConversationSummarizer` e `InMemoryKnowledgeBase` necesitan que las
siembres antes de tener algo con qué responder — ver sus docstrings
(`seed_session` / `add_document` / el argumento `documents` del
constructor).

## Qué NO son

- **No son persistentes.** Todo el estado en memoria se pierde al reiniciar
  el proceso. Una entrega real provee su propio backend sobre su propio
  esquema, de la misma forma en que `deployments/acme/` ya provee sus
  propias implementaciones de `CatalogSource` y `OrderWriter`.
- **No es un canal de escalamiento real.** `LoggingEscalationChannel` nunca
  notifica a una persona — no hay sistema de aviso de guardia, ni Slack, ni
  SMS detrás. Registra una entrada estructurada e informa
  `status: "logged"`, nunca `"notified"`, precisamente para que nada aguas
  abajo pueda leer esto como si se hubiera alcanzado a una persona real. Una
  entrega para un cliente que necesite una integración real de aviso de
  guardia escribe su propio `EscalationChannel` (según el límite
  plataforma/cliente de `manifesto.md`).
- **No es un proveedor de LLM nuevo.** `LLMConversationSummarizer` nunca
  construye su propio modelo de chat; recibe uno. Pasale la misma instancia
  de `BaseChatModel` que ya usa el `AgentRuntime` del rol que lo invoca.

## Referencias cruzadas

- Panorama de integración de la biblioteca: `docs/platform/library-usage.md`
- Los puertos en sí: `src/agentsys/services/{knowledge,summaries,escalation,orders}.py`
- Comportamiento en modo cerrado de los conectores: `src/agentsys/connectors/platform_connectors.py`, `src/agentsys/connectors/order_connector.py`
- ADR-002 C.15: `docs/architecture_es/adr-002-agent-model-and-capabilities.md`
