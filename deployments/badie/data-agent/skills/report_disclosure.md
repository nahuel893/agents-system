# Skill: report_disclosure

State what every figure counted. This is not a formatting preference — it is
the difference between a number someone can check and a number they have to
trust.

---

## The rule

Every time you present figures from `run_report`, state underneath them:

1. **Which order statuses were counted**, and explicitly whether cancelled
   orders were included.
2. **Which period the figures cover**, as a real date — not "the last year".

Both values are already in the tool's response, under `meta`. You are not
inferring them; you are relaying them. Never describe a filter you did not
read there.

---

## Why this is non-negotiable

In this dataset, cancelled orders are **15.7% of gross revenue**. Two
answers to "how much did we bill" can differ by a sixth and both be correct —
what makes one of them *wrong* is not saying which question it answered.

The window has the same trap. `months_back` counts **30-day blocks, not
calendar months**, so the default of 12 covers 360 days, and `meta.window_start`
will show a date a few days later than "one year ago". That is close enough to
be useful and far enough to change a total. Report the date the tool gives you.

A reader who has the filter can reconcile your number against the database. A
reader who doesn't can only believe you. Analytics that cannot be checked is
just confident-sounding text.

---

## Format

Present the rows first, then the disclosure. Keep it short — one block, not a
paragraph:

> | Zona | Pedidos | Facturación |
> |---|---|---|
> | Morón | 31 | $5.014.100 |
>
> *Incluye pedidos `confirmed` y `pending`. **No** incluye cancelados.
> Período: desde el 02/08/2025 (últimos 360 días).*

If the caller asked for a specific status or window, say so in the same place
rather than assuming they remember what they asked for.

---

## Edge cases

**A report that does not filter by status** — `resumen_estados` breaks results
down by status on purpose. Say that it covers every status, including
cancelled, so the reader does not assume the usual exclusion applies.

**Comparing two periods** — disclose the window for each side. Comparing a
360-day window against a calendar year is a real error, and it is invisible
unless both windows are on the page.

**A number the user quotes back at you** — if they cite a figure that
disagrees with yours, the first thing to check is whether the filters match,
not whether the database changed. Ask which statuses and period their number
covered before concluding anything.

**Empty result** — an empty analytical result usually means a broken filter or
a broken pipeline, not a true zero. Say the query returned no rows, state the
filter that produced it, and do not present it as "we sold nothing".
