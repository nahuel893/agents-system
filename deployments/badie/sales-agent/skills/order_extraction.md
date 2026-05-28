# Skill: order_extraction

Extract structured order items from an inbound WhatsApp message.

---

## What this skill does

When the agent receives a message that contains a product order, apply the
following extraction process before taking any other action.

---

## Extraction rules

**Identify each requested item.** A single message may contain multiple items
separated by conjunctions, commas, or line breaks. Treat each distinct product
reference as a separate item.

For each item, extract:

- **product_query** — the product name as the customer expressed it, including
  any colloquial, abbreviated, or brand-nickname form. Do not normalize at this
  stage; pass the raw expression to the matching skill.
- **quantity** — the numeric amount requested. If the customer uses an
  indefinite expression, map it as follows:
  - `"un par"`, `"un par de"` → `quantity: 2, confidence: high`
  - `"unos cuantos"`, `"varios"` → `quantity: null, confidence: low` — flag for
    clarification
  - `"algunos"` → `quantity: null, confidence: low` — flag for clarification
  - A single item with no quantity stated → `quantity: 1, confidence: medium`
- **unit** — the unit of measure as expressed by the customer. Recognized units:
  `cajón`, `caja`, `unidad`, `botella`, `lata`, `six`, `pack`. If no unit is
  stated, set `unit: null`.
- **confidence** — your confidence that you correctly parsed this item:
  `high`, `medium`, or `low`.

---

## Output format

Produce a structured list. For each item:

```
- product_query: <raw expression from customer>
  quantity: <number or null>
  unit: <unit string or null>
  confidence: <high | medium | low>
```

---

## Clarification threshold

If **any** item has `confidence: low` or `quantity: null`, do not proceed to
matching. Instead, ask the customer a single clarifying question that resolves
the most critical ambiguity first. Ask one question per turn — do not ask about
multiple items in the same message.

If the extraction produces a clean list (all items with `confidence: high` or
`medium` and no null quantities), proceed to `colloquial_matching`.

---

## Examples

Customer message: `"2 cajones de quilmes y una stella"`

```
- product_query: quilmes
  quantity: 2
  unit: cajón
  confidence: high
- product_query: stella
  quantity: 1
  unit: null
  confidence: medium
```

Customer message: `"dame unos cuantos de la rubia"`

```
- product_query: la rubia
  quantity: null
  unit: null
  confidence: low
```

Action: ask the customer how many cases or units they need before proceeding.
