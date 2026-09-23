# Reference backends for the platform-generic ports

ADR-002 C.15. `KnowledgeBase`, `ConversationSummarizer`, `EscalationChannel`,
and `OrderWriter` (`src/agentsys/services/{knowledge,summaries,escalation,orders}.py`)
are `Protocol` ports the platform deliberately ships with **no**
implementation — see each module's own docstring for why. Left with nothing
behind them, the platform-generic tools bound to those ports
(`connectors/platform_connectors.py`, `connectors/order_connector.py`) are
correctly fail-closed, but that also meant no test anywhere in this
repository ever exercised a role actually retrieving from a knowledge base,
summarizing a real conversation, logging an escalation, or writing an order.

`src/agentsys/services/reference.py` ships four small reference
implementations that close that gap. They are **opt-in**: nothing wires them
by default, so an application that configures none of them keeps getting the
same fail-closed refusals as before. They are also **not** production
integrations — see "What these are not" below.

---

## The four classes

| Class | Implements | Storage |
|---|---|---|
| `InMemoryKnowledgeBase` | `KnowledgeBase` | In-memory markdown documents, seeded by you |
| `LLMConversationSummarizer` | `ConversationSummarizer` | In-memory transcript, seeded by you; summarizes with a `BaseChatModel` you already have |
| `LoggingEscalationChannel` | `EscalationChannel` | None — writes a structured log entry |
| `InMemoryOrderWriter` | `OrderWriter` | In-memory, for the life of the process |

## Wiring one in

Each `build_*_tool_spec` function in `platform_connectors.py` /
`order_connector.py` already accepts `None` (fail-closed, the default) or a
port implementation. Pass one of these instead, wherever your `registry_factory`
builds its `ToolRegistry`:

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
- The ports themselves: `src/agentsys/services/{knowledge,summaries,escalation,orders}.py`
- Fail-closed connector behavior: `src/agentsys/connectors/platform_connectors.py`, `src/agentsys/connectors/order_connector.py`
- ADR-002 C.15: `docs/architecture/adr-002-agent-model-and-capabilities.md`
