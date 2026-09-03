# Lineage reconciliation

- declared edges (hand-maintained): **8**
- dbt-derived edges              : **6**
- declared edges dbt confirms    : **2**

## Declared edges dbt does NOT confirm

- none (all confirmed)

## Edges outside dbt's visibility

These assets are downstream of the warehouse but not dbt models, so no
generated lineage will ever cover them. They are only protected by the
hand-maintained graph - and are exactly where blast radius gets missed.

- `fct_daily_revenue` -> `ceo_revenue_dashboard`
- `kb_active_docs` -> `rag_index`
- `kb_documents` -> `kb_active_docs`
- `rag_index` -> `support_agent`
- `raw_customers` -> `stg_customers`
- `raw_orders` -> `stg_orders`

## Blast radius reference

```text
stg_orders    -> fct_daily_revenue, ceo_revenue_dashboard
kb_documents  -> kb_active_docs, rag_index, support_agent
```
