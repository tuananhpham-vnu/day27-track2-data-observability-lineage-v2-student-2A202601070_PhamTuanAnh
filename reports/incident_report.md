# Incident Report — Revenue inflation from duplicate order keys

**Incident ID:** GAMEDAY-27-001
**Date:** 2026-09-03
**Team:** Data/AI Reliability
**Status:** Resolved

---

## Severity

**P2.**

Not P1: the absolute error is small (+1.58% revenue) and no customer-facing
system was down. Not P3: the corrupted number reaches the **CEO revenue
dashboard**, a decision-making surface, and the pipeline reported `SUCCESS`
throughout — so the error would have been trusted rather than questioned.

---

## Summary

The `orders` batch for 2026-09-03 arrived with **3 order records duplicated**
(603 rows instead of 600, order_ids `100000`, `100001`, `100002` each appearing
twice). Every duplicated row was individually valid: correct types, valid
currency, valid status, positive amount, fresh timestamps. Only the *uniqueness*
of the primary key was violated.

The duplicates inflated completed-order revenue by **$300.01 (+1.58%)**
— $19,261.05 reported against a true $18,961.04.

The critical property of this incident is that **the pipeline never failed**.
Row counts were normal, freshness was normal, distributions were normal. Without
a deterministic key constraint, this reaches the CEO dashboard silently.

---

## Detection

- **Signal:** `unique` contract check on `orders.order_id` — severity `critical`,
  action `block`.
- **First observed:** 2026-09-03 06:07 UTC, on the first pipeline run after the
  batch landed.
- **Detected by:** `src/contract_validator.py` (contract layer), confirmed
  independently by the GX checkpoint and by the dbt test `unique_stg_orders_order_id`.

### What did NOT detect it — and why that matters

| Layer | Verdict | Why it was blind |
|---|---|---|
| Volume anomaly detector | **ok** (score 0.71, expected ~622) | 603 rows is a perfectly normal Wednesday. 3 extra rows is 0.5% — statistically invisible. |
| Freshness check | **ok** (5.0 min lag) | The duplicated rows carried valid, fresh timestamps. |
| Distribution checks (KS/PSI) | **ok** | Duplicating 3 of 600 rows does not move a distribution. |
| `not_null` / `accepted_values` / `range` | **pass** | Every duplicated row is individually well-formed. |
| `unique(order_date)` on the mart | **pass** | The `GROUP BY` collapses duplicates into the same daily row. |

This is the core lesson of the exercise: **statistical detection cannot catch a
small deterministic violation.** A 0.5% row-count change will never clear any
sane anomaly threshold, yet it is unambiguously wrong. Conversely, a
deterministic rule could never have caught the volume-drop scenario. The layers
are not redundant — each is the only thing that sees its own failure class.

---

## Root Cause

**A re-run of the extract job appended instead of upserting.**

Evidence supporting this over the alternatives:

1. The duplicates are **exact, full-row copies** — every column identical,
   including `created_at` and `updated_at`. A genuine business event (a customer
   ordering twice) would produce a new `order_id` and a different timestamp.
2. They are the **first 3 rows by `order_id`** (`100000`–`100002`), i.e. the head
   of the source table — the signature of a re-read that restarted from the
   beginning rather than from a watermark.
3. **No keys are missing** (`keys present in baseline but missing now: 0`) and no
   new keys appeared. The batch is the complete correct set *plus* a replayed
   prefix, which rules out a partial load or a merge with a foreign source.
4. Schema is unchanged, so no producer-side contract change is involved.

Underlying cause: the load step has **no primary-key constraint and no
idempotency guarantee**. A retry is therefore not safe, and any retry — for any
reason — silently duplicates data.

---

## Evidence

1. **Contract validator** (`python scripts/run_baseline.py`):
   ```
   contract action          : BLOCK
   contract failed checks   : 1 (critical=1)
       [critical] unique on order_id: duplicate_rows=6
   quarantined rows         : 6 -> reports/quarantine/orders_quarantine.csv
   RUN STATUS: FAILED - downstream consumers must not use this batch.
   ```
   (6 rows = 3 keys × 2 copies each; both copies are quarantined because the
   validator cannot know which one is authoritative.)

2. **dbt build** (`make dbt`) — the transformation layer refuses to proceed:
   ```
   20 of 29 FAIL 3 unique_stg_orders_order_id ......... [FAIL 3 in 0.02s]
   Done. PASS=19 WARN=0 ERROR=1 SKIP=9 NO-OP=0 TOTAL=29
   ```
   **9 downstream nodes were SKIPPED**, including `fct_daily_revenue`. The mart
   was never built from corrupt data — the failure was contained at staging.

3. **Great Expectations checkpoint** (`make gx`) — independent confirmation with
   severity routing:
   ```
   checkpoint success : False
   failed expectations: 1 (critical=1)
     [critical] expect_column_values_to_be_unique on order_id
   pipeline action    : BLOCK
   quarantined rows   : 6
   ```

4. **Triage report** (`python scripts/triage.py`) — localises the fault:
   ```
   Volume : 603 rows vs 600 baseline (+0.5%) — same-weekday detector: ok
   Keys   : duplicate order_id values: 3 → [100000, 100001, 100002]
            keys present in baseline but missing now: 0
   Schema : No schema change.
   Signals that fired: keys, contract
   ```

5. **Quantified business impact** — reconciling the mart against source truth:
   ```
   revenue with duplicates : 19,261.05
   revenue deduplicated    : 18,961.04
   inflation               :   +300.01  (+1.58%)
   ```

---

## Blast Radius

Computed from `data/baseline/lineage_graph.json`, not from memory:

```text
raw_orders (duplicate keys introduced here)
└── stg_orders                     ← contract + dbt test BLOCKED here
    └── fct_daily_revenue          ← would have been inflated (SKIPPED by dbt)
        └── ceo_revenue_dashboard  ← CEO-visible wrong number (protected)
```

**Column-level path** (`column_downstream`):

```text
raw_orders.amount
  → stg_orders.amount_usd
    → fct_daily_revenue.daily_revenue
      → ceo_revenue_dashboard.revenue
```

- **Affected datasets:** `fct_daily_revenue`, `ceo_revenue_dashboard`
- **Affected columns:** `fct_daily_revenue.daily_revenue`, `ceo_revenue_dashboard.revenue`
- **Impacted consumers (leaf nodes):** `ceo_revenue_dashboard`
- **Business-critical assets in path:** `ceo_revenue_dashboard`, `fct_daily_revenue`
- **NOT affected:** the entire RAG branch (`kb_documents → kb_active_docs →
  rag_index → support_agent`). It has no lineage dependency on `orders`, so the
  Support Agent needed no action. Lineage is what let us say that confidently
  instead of investigating it.

**Actual customer/business impact: none.** The block held at staging, so no
downstream consumer ever read the inflated number.

---

## Mitigation

1. **Blocked** the batch — `enforce_contract` returned `action=block` and
   `run_baseline.py` exited non-zero, so nothing propagated.
2. **Quarantined** the 6 offending rows to
   `reports/quarantine/orders_quarantine.csv` for inspection rather than
   discarding them.
3. **Left the mart untouched.** `fct_daily_revenue` still held the last known-good
   build, so the dashboard showed stale-but-correct data rather than fresh-but-wrong
   data. This is the right trade for a financial metric.
4. **Recovered** by deduplicating on the primary key and re-running:
   `make reset && make baseline && make dbt`.

---

## Recovery

```bash
make reset      # restore a clean batch (in production: re-extract with the correct watermark)
make baseline   # contract + anomaly + lineage + SLO
make dbt        # rebuild the models with tests
```

Post-recovery state:

```
contract action          : PASS
contract failed checks   : 0 (critical=0)
row-count anomaly        : ok (auto:mad, score=0.83, baseline_median=622)
RUN STATUS: HEALTHY

dbt: Done. PASS=29 WARN=0 ERROR=0 SKIP=0 TOTAL=29
```

---

## Verification

- [x] **Contract healthy** — `run_baseline.py` reports `action=PASS`, 0 failed
      checks, exit code 0.
- [x] **dbt tests healthy** — `PASS=29 ERROR=0 SKIP=0`, including the singular
      test `assert_revenue_matches_orders` which reconciles the mart against
      `stg_orders` row-by-row.
- [x] **Anomaly returned to expected range** — row count 600 against a
      same-weekday median of 622, score 0.83 (threshold 3.5).
- [x] **SLO healthy / budget understood** — all three SLIs at burn rate 0.0,
      `multiwindow_burn` tier `healthy`, `page=False`.
- [x] **Downstream output verified** — `assert_revenue_matches_orders` returns
      zero rows, i.e. `fct_daily_revenue.daily_revenue` equals the sum of
      completed `stg_orders` amounts exactly. Revenue is $18,961.04, matching
      source truth.

The important one is the last: verifying that *the tests pass* is not the same
as verifying that *the number is right*. The singular test checks the number.

---

## Prevention / Action Items

| Action | Owner | Deadline | Why |
|---|---|---|---|
| Add a PK constraint / idempotent `MERGE` on the orders load | data-platform | 2026-09-10 | Removes the failure class entirely instead of detecting it after the fact. This is the only item that actually fixes the root cause. |
| Keep `unique(order_id)` as a **blocking** critical contract check | commerce-data | done | The only layer that saw this incident. Must never be downgraded to a warning. |
| Watermark-based extraction instead of full re-read | data-platform | 2026-09-17 | A retry restarting from row 0 is what produced the replayed prefix. |
| Alert on `quarantine` file non-empty | data-platform | 2026-09-10 | Quarantined rows are currently written but nobody is told. Silent isolation is only half a control. |
| Add a source-vs-mart reconciliation test to every financial mart | analytics-eng | 2026-09-24 | `assert_revenue_matches_orders` caught the class of bug that `unique`/`not_null` structurally cannot see. |
| Keep the `scd_fanout_does_not_inflate_revenue` unit test | analytics-eng | done | Guards a *latent* fan-out bug that today's clean data hides. See note below. |

### Note on a second, latent defect found during this investigation

While tracing how revenue could be inflated, we found that `fct_daily_revenue`
joined `stg_customers where is_active = true` **without deduplicating the
dimension**. An SCD-2 dimension can legitimately carry more than one active row
per customer (a late-arriving update that never closed the previous version),
and the join would then duplicate order rows and inflate revenue — with no SQL
error and no failing data test.

Today's customer seed has exactly one active version per customer (75 active
rows, 75 distinct customers), so **every data test passes and the bug is
invisible**. It is a live landmine, not a theoretical one.

Fixed by collapsing the dimension to one row per customer before the join, and
pinned by a dbt **unit test** with a two-active-version fixture. Verified by
reverting the fix: the unit test fails (`FAIL 1`) while all 22 data tests still
pass — which is exactly why unit tests are not redundant with data tests.

---

## Appendix — Phase 0 system understanding

**Which dataset is critical?**
Two, on independent branches: `orders` (feeds revenue reporting — direct
financial impact) and `kb_documents` (feeds the RAG Support Agent — direct
customer impact, and the one that produces confidently wrong answers).

**Which downstream consumers depend on them?**
- `orders → stg_orders → fct_daily_revenue → ceo_revenue_dashboard`
- `kb_documents → kb_active_docs → rag_index → support_agent`

**Which metric tells us the data is not trustworthy?**
No single one — that is the point. `critical_contract_failures` catches
deterministic breaks, the same-weekday `row_count` anomaly catches statistical
ones, `freshness_minutes` / KB age catch valid-but-stale data, and the SLO burn
rate converts all of them into "is this worth paging someone?". This incident
was caught by the first and missed by all the others.
