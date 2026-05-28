# Skill

## What a skill is

A skill is a behavioral pack or prompt module that shapes how an agent runtime reasons, responds, or makes decisions. Skills are injected into the runtime's prompt and context surface at capability injection time. They have no side effects — they do not execute external calls, write to databases, or send messages.

Skills are the mechanism for injecting domain knowledge, reasoning strategies, and behavioral conventions into an agent without hardcoding them in the agent definition or in the runtime.

---

## Skill vs. tool

| Concept | What it is | Has side effects | Examples |
|---|---|---|---|
| **Skill** | A behavioral pack that shapes reasoning | No | `colloquial_product_matching`, `escalation_decision` |
| **Tool** | An executable connector to an external system | Yes | `whatsapp_sender`, `rag_catalog_search` |

A skill guides the agent's internal reasoning before it decides what action to take. A tool is the action itself. A skill might instruct the agent how to interpret an ambiguous product request; the `rag_catalog_search` tool is what retrieves the candidates once the agent has formed a clear query.

---

## Skill definition schema

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | `string` | required | Unique identifier. Snake-case (e.g., `order_extraction`). Referenced by `manifest.md` in the `skills` field. |
| `description` | `string` | required | What behavioral capability this skill provides to the agent. Used during injection to explain to the runtime what this skill enables. |
| `prompt_modules` | `list[object]` | required | Ordered list of prompt fragments injected into the agent's system context. Each entry: `id` (string), `role` (one of `system`, `context`, `instruction`, `example`), `content` (string). |
| `context_requirements` | `object` | optional | Any context the skill requires to be already injected before it can function correctly. Sub-fields: `requires_tool_output` (list of tool names whose results must be available), `requires_session_context` (boolean). |
| `applicable_roles` | `list[string]` | optional | List of role names this skill is designed for. When present, the injector warns if the skill is applied to a role not in this list, but does not prevent injection. An empty list means the skill is general-purpose. |

---

## How skills are composed and injected

Skills are injected after tools and before context in the injection pipeline (see `docs/platform/harness.md` for the full ordering rationale).

**Injection sequence:**

1. For each skill name declared in the agent's `manifest.md` (`skills` field), retrieve the skill definition from the registry.
2. Check `context_requirements.requires_tool_output` — confirm the required tools have already been injected (tools precede skills in the pipeline). If a required tool is absent, fail the skill injection and log the dependency gap.
3. Merge `prompt_modules` into the runtime's system prompt surface, in the order they appear in the skill definition. Skills declared earlier in the agent's `manifest.md` are injected first.
4. If the skill declares `context_requirements.requires_session_context: true`, confirm that session context injection is scheduled (it follows in the pipeline). If session context is not available, log a warning but do not fail — the skill will operate with reduced effectiveness.

**Composition rule:** Multiple skills can coexist in a single runtime. Their prompt modules are concatenated in injection order. Skill authors are responsible for ensuring their modules do not contradict each other. The platform does not detect or resolve semantic conflicts between skills.

---

## Relationship between skills and agent definitions

An agent's `manifest.md` declares which skills are active in the `skills` field. This is an allowlist: the runtime will only receive skills explicitly named there, even if additional skills exist in the registry.

This keeps the capability surface of any running agent fully predictable from its agent definition. No skill is injected silently or by default.

A skill may declare `applicable_roles` to signal where it was designed to work. This is advisory — the platform issues a warning but allows the injection. The responsibility for appropriate skill assignment belongs to the agent definition author.

---

## Examples

### `order_extraction`

| Field | Value |
|---|---|
| Description | Teaches the agent to extract structured order data from unstructured conversational input. Handles multiple items in a single message, quantity expressions (units, cases, crates), and incomplete specifications that require clarification. |
| Applicable roles | `preventa_agent` |
| Context requirements | `requires_session_context: true` (prior messages needed to resolve references like "the same as last time") |

Prompt modules injected:
1. **`system/order-extraction-instructions`** — how to identify product mentions, quantities, and units in conversational text
2. **`instruction/ambiguity-handling`** — when to ask for clarification vs. when to proceed with the best match
3. **`example/extraction-examples`** — few-shot examples in colloquial Argentine Spanish

---

### `colloquial_product_matching`

| Field | Value |
|---|---|
| Description | Teaches the agent to map informal, colloquial, or abbreviated product references to SKUs in the catalog. Handles brand nicknames, generic category references, regional slang, and partial descriptions common in Argentine beverage retail. |
| Applicable roles | `preventa_agent` |
| Context requirements | `requires_tool_output: [rag_catalog_search]` (RAG results must be available before the matching reasoning begins) |

Prompt modules injected:
1. **`system/colloquial-vocabulary`** — known mappings and heuristics for common Argentine informal product references (e.g., "la rubia" → Quilmes lager, "cajón" → case of bottles)
2. **`instruction/rag-result-interpretation`** — how to reason over cosine-similarity results and select the best candidate
3. **`instruction/low-confidence-handling`** — what to do when no match exceeds the confidence threshold

---

### `escalation_decision`

| Field | Value |
|---|---|
| Description | Provides the agent with a structured decision framework for determining when to escalate to a human operator versus when to proceed autonomously. Covers ambiguous identity, policy violations, approval thresholds, and explicit client requests. |
| Applicable roles | `preventa_agent`, `orchestrator_agent` |
| Context requirements | none |

Prompt modules injected:
1. **`system/escalation-principles`** — the underlying principles: when in doubt, escalate; never guess on behalf of the client; humans handle exceptions
2. **`instruction/escalation-triggers`** — explicit enumeration of conditions that require escalation (mirrors the `escalation_rules` in the agent's `policy.md`)
3. **`instruction/escalation-communication`** — how to communicate an escalation to the client clearly and without technical jargon

---

## Cross-references

- Tool definitions (executable counterpart to skills): `docs/platform/tool.md`
- Injection pipeline and ordering: `docs/platform/harness.md`
- Agent `manifest.md` `skills` field: `docs/platform/role.md`
