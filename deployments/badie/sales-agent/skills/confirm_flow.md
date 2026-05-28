# Skill: confirm_flow

Guide the agent through order confirmation before persisting any order.

---

## What this skill does

Before calling `order_writer`, always present a complete order summary to the
customer and wait for explicit confirmation. This is a hard requirement — order
writes without prior confirmation are a policy violation.

---

## Confirmation flow

### Step 1 — Build the summary

Compile the full order from the matched items produced by `colloquial_matching`.
For each line item include:

- canonical product name (as it appears in the catalog — not the colloquial
  expression)
- quantity
- unit
- unit price if available from the injected price list; omit the price column
  if price data is not available for this session

Present the summary as a numbered list, followed by the total if prices are
available.

Example format:

> Tu pedido:
> 1. Quilmes Lager — 2 cajones (24u c/u) — $X cada uno
> 2. Stella Artois 473ml lata — 1 cajón (24u) — $X cada uno
>
> Total estimado: $X
>
> ¿Confirmamos?

If prices are not available, omit the price column and total line. Do not
invent or estimate prices.

### Step 2 — Wait for confirmation

Send the summary and wait for the customer's response. Accept only one of:

- **Affirmative confirmation** (`"sí"`, `"dale"`, `"confirmado"`, `"va"`,
  `"ok"`, or equivalent) → proceed to Step 4.
- **Rejection or correction** (`"no"`, `"cambiá"`, `"falta"`, `"sobrá"`,
  explicit product correction) → proceed to Step 3.

### Step 3 — Handle correction

On rejection or partial correction, do not restart the conversation from
scratch. Apply only the change the customer specified.

- If the customer specifies a quantity change, update that line item.
- If the customer specifies a product change, return to `order_extraction`
  for that single item and re-run `colloquial_matching` for it.
- Once the correction is applied, regenerate the summary and repeat Step 2.

Ask one question per turn if clarification is needed for the correction.
Do not ask the customer to repeat the entire order.

### Step 4 — Persist the order

On affirmative confirmation, call `order_writer` with the confirmed order.
Do not call `order_writer` before receiving explicit confirmation — this is
enforced by the policy layer but the skill must not attempt it regardless.

After a successful write, send the customer a concise acknowledgement:

> "¡Pedido registrado! En breve lo procesamos."

Do not include internal identifiers (order ID, SKU codes) in the customer
message unless the deployment configuration explicitly enables it.

---

## Failure handling

If `order_writer` returns an error, do not tell the customer the order was
confirmed. Notify the customer that there was a problem and that a human
operator will follow up. Escalate via `escalation_notifier`.
