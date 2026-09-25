# Backends de referencia para los puertos genéricos de la plataforma

ADR-002 C.15. `KnowledgeBase`, `ConversationSummarizer`, `EscalationChannel`
y `OrderWriter` (`src/agents_system/services/{knowledge,summaries,escalation,orders}.py`)
son puertos `Protocol` que la plataforma entrega deliberadamente **sin**
implementación — ver el docstring de cada módulo para el porqué. Al no tener
nada detrás, las herramientas genéricas de plataforma vinculadas a esos
puertos (`connectors/platform_connectors.py`, `connectors/order_connector.py`)
fallan correctamente en modo cerrado, pero eso también significaba que
ninguna prueba en este repositorio ejercitó jamás a un rol recuperando de
verdad datos de una base de conocimiento, resumiendo una conversación real,
registrando un escalamiento o escribiendo un pedido.

`src/agents_system/services/reference.py` entrega ocho backends de referencia
pequeños que cierran esa brecha. Son **opcionales** (opt-in): nada conecta
ninguno por defecto, así que una aplicación que no selecciona ninguno sigue
recibiendo los mismos rechazos en modo cerrado que antes. Tampoco son
integraciones de grado productivo — ver "Qué NO son" más abajo.

---

## Los ocho backends opcionales

| Backend | Implementa o expone | Almacenamiento / fuente |
|---|---|---|
| `InMemoryKnowledgeBase` | `KnowledgeBase` | Documentos markdown en memoria, sembrados por vos |
| `LLMConversationSummarizer` | `ConversationSummarizer` | Transcripción en memoria, sembrada por vos; resume con un `BaseChatModel` que ya tenés |
| `LoggingEscalationChannel` | `EscalationChannel` | Ninguno — escribe una entrada de log estructurado |
| `InMemoryOrderWriter` | `OrderWriter` | En memoria, durante la vida del proceso |
| `ReferenceBackends.catalog_search_tool_spec()` | `ToolSpec` `catalog_search` | Búsqueda léxica sobre `articulos` |
| `ReferenceBackends.client_lookup_tool_spec()` | `ToolSpec` `client_lookup` | Registro existente de `padron_clientes`, resuelto por teléfono sintético |
| `ReferenceBackends.run_report_tool_spec()` | `ToolSpec` `run_report` | `CATALOG` portátil de ventas sobre un motor BI dedicado de solo lectura |
| `ReferenceBackends.message_sender_tool_spec()` | `ToolSpec` `message_sender` | Registro solo en proceso; sin proveedor de entrega |

## Cómo conectar las cuatro originales

Cada función `build_*_tool_spec` en `platform_connectors.py` /
`order_connector.py` ya acepta `None` (modo cerrado, el valor por defecto) o
una implementación del puerto. Pasá una de estas en su lugar, donde sea que
tu `registry_factory` construya su `ToolRegistry`:

```python
from agents_system.connectors.order_connector import build_order_writer_tool_spec
from agents_system.connectors.platform_connectors import (
    build_conversation_summarizer_tool_spec,
    build_escalation_notifier_tool_spec,
    build_knowledge_retrieval_tool_spec,
)
from agents_system.services.reference import (
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

## Cómo conectar las herramientas demo/de compañía

Antes de iniciar la aplicación, cargá solo una base demo/de compañía
descartable con el cargador existente y protegido. Acepta una base vacía o
una que haya marcado en una ejecución anterior, y rechaza cualquier otro
destino no vacío porque la carga reemplaza sus propias tablas:

```bash
DEMO_DATABASE_URL=postgresql+asyncpg://... uv run python demo/load_demo_company.py
```

Apuntá `BI_DATABASE_URL` a esa base cargada usando credenciales de solo
lectura. Mantené este motor BI separado del motor transaccional de la
aplicación; la herramienta `run_report` delega al `CATALOG` portátil de ventas
a través de ese motor dedicado. `ReferenceBackends` no registra nada por sí
mismo y ningún registry productivo selecciona estas herramientas por defecto.

```python
import os

from sqlalchemy.ext.asyncio import create_async_engine

from agents_system.services.reference import ReferenceBackends

# BI_DATABASE_URL apunta a la base descartable del cargador protegido.
# Sus credenciales de base de datos deben ser de solo lectura.
readonly_engine = create_async_engine(os.environ["BI_DATABASE_URL"])
backends = ReferenceBackends(readonly_engine)

registry.register(backends.catalog_search_tool_spec())
registry.register(backends.client_lookup_tool_spec())
registry.register(backends.run_report_tool_spec())
registry.register(backends.message_sender_tool_spec())
```

### Comportamiento observable de la demo

- `client_lookup` toma `{"phone": ...}`. Su única forma válida de teléfono es
  `+549110000` seguida de un `padron_clientes.nro_cliente` existente, completado
  a cuatro dígitos con ceros: el cliente `1` es `+5491100000001`. Un teléfono
  desconocido o malformado devuelve `client_id: None` y `name: None`.
- `message_sender` toma `{"to": ..., "text": ...}` y valida `to` de la misma
  forma. Un destinatario existente devuelve `status: "recorded"`; nunca se
  envía ni se entrega, y su registro en memoria se pierde al terminar el
  proceso. Un destinatario desconocido devuelve `status: "not_found"`.
- `catalog_search` es solo léxica (subcadenas en SKU o descripción). Cada
  resultado tiene `similarity: null`; no afirma búsqueda vectorial ni
  semántica. Una consulta vacía o sin coincidencias devuelve `results: []` con
  `classification: "no_match"`.
- `run_report` expone el `CATALOG` portátil y cerrado de ventas; su esquema
  de herramienta documenta directamente el tipo, los valores permitidos y el
  valor por defecto de cada parámetro desde las definiciones `ReportSpec` /
  `ParamSpec` del catálogo. Un reporte válido sin filas devuelve una respuesta
  vacía (`rows: []`, `empty_result: true`).

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
- Los puertos en sí: `src/agents_system/services/{knowledge,summaries,escalation,orders}.py`
- Comportamiento en modo cerrado de los conectores: `src/agents_system/connectors/platform_connectors.py`, `src/agents_system/connectors/order_connector.py`
- ADR-002 C.15: `docs/architecture_es/adr-002-agent-model-and-capabilities.md`
