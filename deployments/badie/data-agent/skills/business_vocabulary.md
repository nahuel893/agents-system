# Skill: business_vocabulary

Map how BADIE staff actually phrase a question onto the report catalog.

---

## Reports available

| Report | Answers | Key parameters |
|---|---|---|
| `ventas_por_mes` | Evolution over time — how a month compares to the ones before it | `months_back`, `status` |
| `top_clientes` | Who buys the most | `limit`, `months_back`, `status` |
| `ventas_por_zona` | Where the money comes from geographically | `limit`, `months_back`, `status` |
| `ventas_por_tipo_negocio` | Which channel performs — kiosco vs bar vs supermercado | `limit`, `months_back`, `status` |
| `top_productos` | What sells | `limit`, `months_back`, `status` |
| `resumen_estados` | How much is confirmed vs pending vs cancelled | `months_back` |

---

## Phrasing → report

- "¿Cómo venimos este mes?", "¿cómo cerró marzo?", "¿estamos mejor que el año
  pasado?" → `ventas_por_mes`
- "¿Quién me compra más?", "mis mejores clientes", "el top 10" →
  `top_clientes`
- "¿Cómo viene Morón?", "¿qué zona rinde más?", "por barrio" →
  `ventas_por_zona`
- "¿Los kioscos o los bares?", "¿qué canal funciona?" →
  `ventas_por_tipo_negocio`
- "¿Qué es lo que más sale?", "los productos que más giran" → `top_productos`
- "¿Cuánto tengo pendiente?", "¿cuánto se cayó?", "¿cuánto está confirmado?" →
  `resumen_estados`

---

## Vocabulary

- **Facturación / venta / lo que se vendió** → `revenue` (the summed
  `total_amount`).
- **Ticket / ticket promedio** → average order value, not revenue per client.
  If someone asks "cuánto gasta un cliente por mes" that is neither — say the
  catalog does not have it rather than substituting the closest number.
- **Pedido caído / anulado / cancelado** → `status = 'cancelled'`.
- **Pendiente** → `status = 'pending'`: taken but not yet confirmed. Real
  pipeline, not yet real revenue.
- **Zona** → `clients.zone`, the delivery area. Not the same as the client's
  address.
- **Tipo de negocio / canal / rubro** → `clients.business_type` (kiosco,
  almacén, bar, restaurante, supermercado…).
- **Un cajón** → 24 units, the standard distribution unit. Reports return
  quantities as they are stored; do not silently convert between units and
  cases.

---

## Choosing the status filter

Default (`confirmed` + `pending`, cancelled excluded) fits most questions.
Deviate deliberately:

- **"¿Cuánto facturamos?"** in the accounting sense → `confirmed` only.
  Pending is not money yet.
- **"¿Cuánto se nos cayó?"** → `cancelled`, or `resumen_estados` for the
  breakdown.
- **"¿Cuánto pedido entró?"** including everything → `all`.

When the wording is genuinely ambiguous, run the default and say which one you
used, so the user can correct you in one message. Do not stall the answer with
a clarifying question that the disclosure line already resolves.

---

## What this catalog cannot answer

Say so plainly instead of approximating with a report that is merely nearby:

- Margin or profitability — no cost data exists.
- Stock or availability — no inventory table.
- Salesperson or route performance — not modelled here.
- Anything about a client's contact details beyond `client_lookup` by phone.
- Comparisons against budget or target — no target data exists.
