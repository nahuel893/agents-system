# Reference backends for the platform-generic ports

ADR-002 C.15. `KnowledgeBase`, `ConversationSummarizer`, `EscalationChannel`,
and `OrderWriter` (`src/agents_system/services/{knowledge,summaries,escalation,orders}.py`)
are `Protocol` ports the platform deliberately ships with **no**
implementation — see each module's own docstring for why. Left with nothing
behind them, the platform-generic tools bound to those ports
(`connectors/platform_connectors.py`, `connectors/order_connector.py`) are
correctly fail-closed, but that also meant no test anywhere in this
repository ever exercised a role actually retrieving from a knowledge base,
summarizing a real conversation, logging an escalation, or writing an order.

`src/agents_system/services/reference.py` ships eight small reference backends that
close that gap. They are **opt-in**: nothing wires any of them by default, so
an application that selects none keeps getting the same fail-closed refusals
as before. They are also **not** production integrations — see "What these
are not" below.

---

## The eight opt-in backends

| Backend | Implements or exposes | Storage / source |
|---|---|---|
| `InMemoryKnowledgeBase` | `KnowledgeBase` | In-memory markdown documents, seeded by you |
| `LLMConversationSummarizer` | `ConversationSummarizer` | In-memory transcript, seeded by you; summarizes with a `BaseChatModel` you already have |
| `LoggingEscalationChannel` | `EscalationChannel` | None — writes a structured log entry |
| `InMemoryOrderWriter` | `OrderWriter` | In-memory, for the life of the process |
| `ReferenceBackends.catalog_search_tool_spec()` | `catalog_search` `ToolSpec` | Lexical lookup over `articulos` |
| `ReferenceBackends.client_lookup_tool_spec()` | `client_lookup` `ToolSpec` | Existing `padron_clientes` record, resolved by synthetic phone |
| `ReferenceBackends.run_report_tool_spec()` | `run_report` `ToolSpec` | Portable sales `CATALOG` on a dedicated BI read-only engine |
| `ReferenceBackends.message_sender_tool_spec()` | `message_sender` `ToolSpec` | In-process recording only; no delivery provider |

## Wiring the original four

Each `build_*_tool_spec` function in `platform_connectors.py` /
`order_connector.py` already accepts `None` (fail-closed, the default) or a
port implementation. Pass one of these instead, wherever your `registry_factory`
builds its `ToolRegistry`:

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
    [KnowledgeDocument(id="returns", title="Return policy", content="...")]
)
escalation_channel = LoggingEscalationChannel()
order_writer = InMemoryOrderWriter()

# The summarizer reuses the SAME chat model your role's AgentRuntime is
# built with -- no new provider config, no new API key.
summarizer = LLMConversationSummarizer(model=your_configured_chat_model)

registry.register(build_knowledge_retrieval_tool_spec(knowledge_base))
registry.register(build_conversation_summarizer_tool_spec(summarizer))
registry.register(build_escalation_notifier_tool_spec(escalation_channel))
registry.register(build_order_writer_tool_spec(order_writer))
```

`LLMConversationSummarizer` and `InMemoryKnowledgeBase` need seeding before
they have anything to answer with — see their docstrings (`seed_session` /
`add_document` / the constructor's `documents` argument).

## Wiring the demo/company tools

Load only a disposable demo/company database with the existing guarded loader
before the application starts. It accepts an empty database or one it marked
on a prior run, and refuses every other non-empty target because loading
replaces its own tables:

```bash
DEMO_DATABASE_URL=postgresql+asyncpg://... uv run python demo/load_demo_company.py
```

Point `BI_DATABASE_URL` at that loaded database with read-only credentials.
Keep this BI engine separate from the application's transactional engine; the
`run_report` tool delegates to the portable sales `CATALOG` through this
dedicated engine. `ReferenceBackends` does not register anything itself, and
no production registry selects these tools by default.

```python
import os

from sqlalchemy.ext.asyncio import create_async_engine

from agents_system.services.reference import ReferenceBackends

# BI_DATABASE_URL targets the guarded loader's disposable database.
# Its database credentials must be read-only.
readonly_engine = create_async_engine(os.environ["BI_DATABASE_URL"])
backends = ReferenceBackends(readonly_engine)

registry.register(backends.catalog_search_tool_spec())
registry.register(backends.client_lookup_tool_spec())
registry.register(backends.run_report_tool_spec())
registry.register(backends.message_sender_tool_spec())
```

### Observable demo behavior

- `client_lookup` takes `{"phone": ...}`. Its only valid phone form is
  `+549110000` followed by an existing `padron_clientes.nro_cliente` padded to
  four digits: client `1` is `+5491100000001`. An unknown or malformed phone
  returns `client_id: None` and `name: None`.
- `message_sender` takes `{"to": ..., "text": ...}` and validates `to` the
  same way. An existing recipient returns `status: "recorded"`; it is never
  sent or delivered, and its in-memory record is lost at process exit. An
  unknown recipient returns `status: "not_found"`.
- `catalog_search` is lexical only (SKU or description substring matching).
  Each result has `similarity: null`; it makes no vector or semantic-search
  claim. An empty query or no match returns `results: []` with
  `classification: "no_match"`.
- `run_report` exposes the closed portable sales `CATALOG`; its tool schema
  directly documents every report parameter's type, allowed values, and
  default from the catalog's `ReportSpec` / `ParamSpec` definitions. A valid
  report with no rows is an empty report response (`rows: []`,
  `empty_result: true`).

## What these are not

- **Not persistent.** All in-memory state is gone on process restart. A real
  deployment supplies its own backend over its own schema, the same way
  `deployments/acme/` supplies `CatalogSource` and `OrderWriter`
  implementations today.
- **Not a real escalation channel.** `LoggingEscalationChannel` never
  notifies a human — no paging system, no Slack, no SMS. It logs a
  structured entry and reports `status: "logged"`, never `"notified"`,
  precisely so nothing downstream can read this as a human having been
  reached. A client delivery that needs a real paging integration writes its
  own `EscalationChannel` (`manifesto.md`'s platform/client boundary).
- **Not a new LLM provider.** `LLMConversationSummarizer` never constructs
  its own chat model; it is handed one. Pass the same `BaseChatModel`
  instance the calling role's `AgentRuntime` already uses.

## Cross-references

- Library integration overview: `docs/platform/library-usage.md`
- The ports themselves: `src/agents_system/services/{knowledge,summaries,escalation,orders}.py`
- Fail-closed connector behavior: `src/agents_system/connectors/platform_connectors.py`, `src/agents_system/connectors/order_connector.py`
- ADR-002 C.15: `docs/architecture/adr-002-agent-model-and-capabilities.md`
