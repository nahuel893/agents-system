# Skill: colloquial_matching

Match a colloquial product expression to a catalog SKU using semantic search
and vocabulary mapping.

---

## What this skill does

For each item produced by `order_extraction`, resolve the `product_query` to
a catalog entry. The catalog is searched via the `catalog_search` tool, which
performs embedding-based retrieval over BADIE's product catalog.

---

## Matching process

### Step 1 — Apply vocabulary mapping as a pre-normalization boost

Before calling `catalog_search`, check whether `product_query` contains an
expression defined in the role vocabulary table. If it does, use the canonical
mapping as the search query instead of the raw expression, and note the
original expression in the match record for transparency.

Vocabulary mappings (from `role.md`):

| Expression | Canonical |
|---|---|
| `"la rubia"` | `Quilmes Lager` |
| `"cajón"` | `case of 24 units` (unit, not a product) |
| `"birra"` | use as-is in semantic search |
| `"preventista"` | not a product — context flag only |
| `"punto de venta"` | not a product — context flag only |

### Step 2 — Execute semantic search

Call `catalog_search` with the resolved query. The tool returns a ranked list
of candidate SKUs with similarity scores.

### Step 3 — Evaluate the top result

Apply the following decision logic based on the similarity score of the top
candidate:

- **Score ≥ 0.92** — high confidence match. Accept without asking.
- **Score ≥ 0.75 and < 0.92** — medium confidence match. Present as the
  selected item but surface it to the customer in the confirmation summary so
  they can correct it if wrong.
- **Score < 0.75** — low confidence. Do not select automatically. Surface the
  top 2–3 candidates to the customer and ask them to choose.

### Step 4 — Handle multiple plausible candidates

When the score is below 0.75 or when two or more candidates are within 0.05 of
each other at the top of the ranking, do not guess. Present the candidates
clearly:

> "Encontré estas opciones para 'XX'. ¿Cuál querés?
> 1. [catalog name, package, brand]
> 2. [catalog name, package, brand]"

Ask once. If the customer's response still does not disambiguate, escalate per
the escalation rules in `policy.md`.

---

## Output format

For each matched item:

```
- product_query: <original expression>
  matched_sku: <catalog SKU or null>
  matched_name: <canonical catalog name>
  score: <similarity score>
  match_confidence: <high | medium | low>
  requires_confirmation: <true | false>
```

Pass the full matched item list to `confirm_flow`.
