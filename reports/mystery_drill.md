# Mystery drill - unseen fault classes

Seed `2026`. Eight fault classes that appear **nowhere** in `scripts/inject_fault.py`. The detection stack is never told which ran.

- detected by some layer: **8/8**
- detected by the predicted layer: **8/8**

| scenario | expected layer | layers that fired | right layer? | evidence |
|---|---|---|---|---|
| `currency_unit_change` | anomaly/distribution | anomaly/distribution | yes | avg_amount=330,620.9 |
| `enum_drift` | contract | contract | yes | accepted_values(status) |
| `kb_content_collapse` | rag | rag | yes | kb.min_length(content); mean_text_length=3.0 |
| `negative_amounts` | contract | contract | yes | range(amount) |
| `null_spike` | contract | contract | yes | not_null(customer_id) |
| `scd_fanout` | dbt | dbt | yes | scd_active_versions(customer_id)x12 |
| `truncated_day` | freshness | anomaly/volume, freshness | yes | freshness(updated_at); row_count=0 |
| `type_drift` | contract | contract | yes | not_null(amount); type(amount) |

Reproduce: `python scripts/mystery_drill.py --seed 2026`

Investigate one by hand: `python scripts/mystery_drill.py --scenario <name>` leaves the fault in `data/incoming/` only if you comment out the final reset.
