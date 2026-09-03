# Triage report - `data/incoming/orders.csv`

Generated 2026-09-03T06:07:12.624535+00:00 against the baseline snapshot.

## 1. Schema

No schema change.

## 2. Volume

- incoming rows : **603**
- baseline rows : 600
- change        : +3 (+0.5%)
- vs 43-day same-weekday baseline: **ok** (score=0.71, expected~622)

## 3. Keys

- duplicate `order_id` values: **3**
- example duplicated keys : [np.int64(100000), np.int64(100001), np.int64(100002)]
- keys present in baseline but missing now: **0**
- keys new since baseline                 : 0

## 4. Per-column distributions

| column | null% now | null% base | detail | verdict |
|---|---:|---:|---|---|
| `order_id` | - | - | surrogate key, see section 3 | skipped |
| `customer_id` | 0.00 | 0.00 | 75 distinct; new=- | ok |
| `amount` | 0.00 | 0.00 | mean 66.40 vs 66.23, ks=0.00, psi=0.00 | ok |
| `currency` | 0.00 | 0.00 | 1 distinct; new=- | ok |
| `status` | 0.00 | 0.00 | 4 distinct; new=- | ok |
| `created_at` | 0.00 | 0.00 | 189 distinct; new=['2026-09-03T02:41:11+0000', '2026-09-03T02:45:11+0000', '2026-09-03T02:47:11+0000'] | ok |
| `updated_at` | 0.00 | 0.00 | 22 distinct; new=['2026-09-03T05:41:11+0000', '2026-09-03T05:42:11+0000', '2026-09-03T05:43:11+0000'] | ok |

## 5. Time distribution

**`created_at`**
- range now : 2026-09-03 02:41:11+00:00 -> 2026-09-03 06:01:11+00:00
- range base: 2026-08-28 09:00:30+00:00 -> 2026-08-28 12:20:30+00:00
- newest record age: **6.0 minutes**
- unparseable values: 0
- rows per hour (newest 6):
    - 2026-09-03 02:00:00+00:00: 28
    - 2026-09-03 03:00:00+00:00: 214
    - 2026-09-03 04:00:00+00:00: 192
    - 2026-09-03 05:00:00+00:00: 167
    - 2026-09-03 06:00:00+00:00: 2

**`updated_at`**
- range now : 2026-09-03 05:41:11+00:00 -> 2026-09-03 06:02:11+00:00
- range base: 2026-08-28 12:00:30+00:00 -> 2026-08-28 12:21:30+00:00
- newest record age: **5.0 minutes**
- unparseable values: 0
- rows per hour (newest 6):
    - 2026-09-03 05:00:00+00:00: 534
    - 2026-09-03 06:00:00+00:00: 69


## 6. Contract verdict

- action: **BLOCK**
    - [critical] `unique` on `order_id`: duplicate_rows=6

## 7. Blast radius

If `stg_orders` is wrong, the following are wrong too:

- datasets  : fct_daily_revenue, ceo_revenue_dashboard
- columns   : fct_daily_revenue.daily_revenue, ceo_revenue_dashboard.revenue
- consumers : ceo_revenue_dashboard
- critical  : ceo_revenue_dashboard, fct_daily_revenue

## 8. Where to look first

Signals that fired: **keys, contract**

- **keys**: Duplicate primary keys - a re-run or replay that appended instead of upserting.
- **contract**: Deterministic rules failed - the details above name the exact column.
