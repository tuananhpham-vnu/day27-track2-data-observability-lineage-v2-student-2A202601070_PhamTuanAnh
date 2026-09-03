# Solution notes — Lab 27 Data Reliability Game Day

Written answers to the questions each phase asks, plus where the code and the
evidence for each claim live.

Reproduce everything with:

```bash
make all
```

---

## Phase 0 — Baseline & system understanding

**Which dataset is critical?**
Two, on independent branches:

- `orders` — feeds revenue reporting. A defect here is a **financial** error on a
  CEO-visible surface.
- `kb_documents` — feeds the RAG Support Agent. A defect here is a **customer**
  error, and the worst kind: the agent answers confidently from a stale policy.

**Which downstream consumers depend on them?**

```text
orders → stg_orders → fct_daily_revenue → ceo_revenue_dashboard
kb_documents → kb_active_docs → rag_index → support_agent
```

**Which metric tells us the data is not trustworthy?**
No single one, and that is the design. Four independent families:

| Metric | Failure class it sees |
|---|---|
| `critical_contract_failures` | Deterministic rule breaks (a bad key, an invalid enum) |
| `row_count` anomaly, same-weekday | Statistical breaks nobody wrote a rule for |
| `freshness_minutes` / KB age | Data that is perfectly valid but too old to be true |
| SLO burn rate | Whether any of the above is worth paging a human for |

The `duplicate_pk` incident was caught only by the first and missed by all the
others; `volume_drop` was caught only by the second. Neither could substitute for
the other.

---

## Phase 1 — Contract + validation

`src/contract_validator.py`

**Type validation.** `_type_violation_mask` checks each declared type. The trap
the starter warned about is real: `pd.to_numeric(..., errors="coerce")` turns
`"N/A"` into `NaN`, so a range check on a text-corrupted column silently passes.
The type check compares before and after coercion and flags the difference. For
`integer` it additionally rejects fractional values, because `100.5` coerces
perfectly well to a number and is still not an order id.

**Freshness validation.** `validate_freshness` compares `max(freshness.column)`
against `max_delay_minutes`.

A freshness SLA compares data time to **pipeline run time**, and a validator
handed only a DataFrame has no run time. So `now` is an explicit parameter, and
when it is absent there is a documented `replay_guard_hours` (default 24h): a
batch whose newest record is more than a day old is reported as a *skipped*
check rather than a breach, because replaying last month's data must not page
anyone. Live staleness — including `stale_kb`'s 3-hour shift — is still caught.

**Severity → action.**

| Severity | Action | Rationale |
|---|---|---|
| `critical` | `block` | Pipeline stops. A broken key or a negative amount corrupts revenue; shipping it is worse than shipping nothing. |
| `warning` | `quarantine` | Offending rows go to a side table, clean rows continue. A new `status` value is a product change, not corruption — losing the whole batch over it is an over-reaction. |
| `info` | `warn` | Logged. |

`decide_action` collapses many issues into one verdict (`block` > `quarantine` >
`warn` > `pass`), and `enforce_contract` performs the split and writes
`reports/quarantine/*.csv` (**automatic quarantine**).

**Great Expectations** (`gx/validate_orders.py`) is a full
Suite → BatchDefinition → ValidationDefinition → Checkpoint flow. Severity lives
in each expectation's `meta`, and `route_actions` maps the checkpoint result onto
the same block/quarantine/warn policy, quarantining via
`unexpected_index_list`. It exits non-zero on `block` — a validator that cannot
fail the build is decoration.

---

## Phase 2 — dbt

**Generic data tests** — `not_null`, `unique`, `accepted_values`, and
`relationships` (`stg_orders.customer_id → stg_customers.customer_id`; an orphan
order is dropped or nulled by the downstream join, quietly shrinking revenue).

**Singular business tests** (`dbt_project/tests/`):

- `assert_revenue_matches_orders.sql` — reconciles `fct_daily_revenue` against
  `stg_orders` day by day. This catches revenue **inflation**, which no generic
  test can: `unique(order_date)` still passes because the `GROUP BY` collapses a
  fanned-out join back to one row per day.
- `assert_one_active_customer_version.sql` — at most one active SCD-2 version per
  customer, naming the broken dataset directly instead of leaving the team to
  reverse-engineer an inflated total.

**Why `not_null`/`unique` are not unit tests.** A **data test** asserts a property
of whatever rows happen to be in the warehouse. It runs against real data and can
only fail *after* bad data has arrived. A **unit test** asserts the
transformation logic against fixed, made-up input; it never touches real data and
fails the moment the SQL is wrong — even while production data is clean.

This is demonstrated, not asserted. `fct_daily_revenue` originally joined
`stg_customers where is_active = true` without deduplicating, so an SCD-2
dimension with two active versions duplicated order rows and inflated revenue
with no SQL error. Today's seed has 75 active rows across 75 distinct customers,
so **every data test passes and the bug is invisible**.

Verified by reverting the fix and re-running:

```text
buggy model → FAIL 1 fct_daily_revenue::scd_fanout_does_not_inflate_revenue
              (all 22 data tests still PASS)
fixed model → PASS=29 ERROR=0
```

The fix collapses the dimension to one row per customer via `row_number()`;
`unit_tests.yml` pins it with a 1-order / 2-active-version fixture.

---

## Phase 3 — Anomaly detection

`observability/anomaly.py`. Evidence: `make evidence` → `reports/detector_evidence.md`.

**When is the z-score wrong?**

1. **`std = 0`** — a flat history. The starter's `mad_is_zero_todo` early return
   made flat metrics permanently undetectable: a `null_rate` sitting at exactly
   0.0 for two weeks then jumping to 0.35 returned `is_anomaly=False`. A metric
   that never moves means *any* material move is the anomaly, so a zero-dispersion
   history now falls back to a relative-deviation rule.

2. **Seasonality** — the real defect in this dataset. Weekdays run ~600 orders,
   weekends ~250, so a pooled window mixes two populations and inflates the
   standard deviation to 160. Measured consequence: a **Wednesday collapsing to
   330 orders — a 47% revenue shortfall — scores 1.10 and is silent.** Restricted
   to the 6 previous Wednesdays (spread ~13), the same shortfall scores **7.91**.
   The pooled baseline is simultaneously too noisy on Saturdays and too blind on
   Wednesdays.

3. **Outliers** — the mean and std are computed from a history that may already
   contain an incident. One `0` from yesterday's outage drags the mean down and
   inflates the std, so the detector goes blind exactly after a bad day
   (**masking**): a still-broken metric scores 1.13 on the z-score against 40.34
   on median/MAD.

4. **Trend** — a steadily growing metric makes every recent value "anomalous"
   against an old mean. `context["trend"]` switches to an EWMA baseline.

**What `auto` does now.** Segments the history by weekday (from
`context["history_day_of_week"]`, an explicit `same_segment_history`, or a weekly
stride), then applies median/MAD, falling back to EWMA for trending metrics and
to the z-score when the segment is too short for a stable MAD. It reports the
strategy and the baseline it used, and `known_event` suppresses the page while
keeping the score visible.

**Distribution** (`observability/distribution.py`) combines the original mean
ratio with a two-sample KS statistic and PSI (numpy only, no scipy). A mean ratio
is blind to a bimodal split: half the orders switching unit keeps the mean
**exactly unchanged** while the distribution changes completely — mean ratio 1.00,
KS 0.50, PSI fires.

---

## Phase 4 — Lineage & blast radius

`observability/lineage.py`

Required answer:

```python
graph = load_graph("data/baseline/lineage_graph.json")
get_downstream_assets(graph, "stg_orders")
# ['fct_daily_revenue', 'ceo_revenue_dashboard']
```

- `get_column_downstream` is now **transitive**. The starter returned direct
  children only, so `raw_orders.amount` reported a blast radius of 1 column and
  never named `ceo_revenue_dashboard.revenue` — the number the CEO actually
  looks at. It now returns all 3.
- `blast_radius` returns affected datasets, affected columns, leaf consumers, and
  which business-critical assets are hit.
- `get_upstream_assets` reverses the graph — the search space for a root cause.
- `extract_dbt_model_graph` parses `target/manifest.json` into readable names.
- `to_openlineage_events` / `write_openlineage_events` emit OpenLineage
  `COMPLETE` RunEvents (`make lineage`, 8 events, Marquez-loadable).

`make lineage` also reconciles the two graphs, which is the point worth making:
**6 of the 8 declared edges are outside dbt's visibility entirely** — the CEO
dashboard and the whole RAG branch are not dbt models. Generated lineage is
always true but incomplete; hand-maintained lineage is complete but drifts. The
reconciliation report names exactly which edges nobody is validating.

---

## Phase 5 — SLO / error budget

`observability/slo.py`. Required calculation, SLO 99.5%, 2 bad / 100 checks:

| Quantity | Value |
|---|---|
| allowed bad rate | 0.5% (`1 - 0.995`) |
| actual bad rate | 2.0% (`2/100`) |
| burn rate | **4.0** (`0.02 / 0.005`) |
| breached | **True** |
| remaining budget | 0% |

Burn rate 4.0 means the budget is being consumed four times faster than granted —
a 30-day budget gone in ~7.5 days.

**Multi-window burn** implements the Google SRE Workbook tiers (14.4 / 6 / 3) as
a **conjunction**: a tier fires only when the short *and* long windows both burn
above it.

- The **long** window proves the problem is *sustained* — a 5-minute blip that is
  already over cannot page anyone.
- The **short** window proves it is *still happening* — an incident that recovered
  an hour ago stops paging, instead of ringing until the long window rolls off
  (the classic "slow reset" of a single-window alert).

| Scenario | short | long | page? | tier |
|---|---:|---:|---|---|
| Transient spike | 20.0 | 0.5 | **False** | `transient_spike` |
| Sustained fast burn | 20.0 | 15.0 | **True** | `fast_burn` |
| Recovering | 0.5 | 8.0 | **False** | `recovering` |
| Healthy | 0.4 | 0.3 | **False** | `healthy` |

---

## Phase 6/7 — Incident & reports

- `reports/incident_report.md` — full RCA of the duplicate-key incident.
- `reports/agent_log.md` — AI agent decisions, including two proposals rejected
  after testing.
- `scripts/triage.py` (`make triage`) — the tool for the mystery dataset. It
  diffs `data/incoming` against the baseline along schema → volume → keys →
  per-column distributions → time → freshness → contract → blast radius, reads no
  fault script, and hard-codes no scenario.
- `scripts/mystery_drill.py` (`make drill`) — **readiness evidence for Phase 6.**

The mystery dataset is supplied by the instructor at class time, so the RCA
itself cannot be written in advance. What *can* be shown in advance is that the
stack generalises to faults it was not built against. The drill injects eight
fault classes that appear nowhere in `scripts/inject_fault.py` — type drift,
currency/unit change, enum drift, null spike, negative amounts, SCD fan-out,
truncated day, KB content collapse — and never tells the detection code which
one ran.

Result: **8/8 detected, each by the layer that should own it** (stable across
seeds 1, 27, 99, 2026).

| fault class | caught by |
|---|---|
| `type_drift` | contract — `type(amount)` |
| `enum_drift` | contract — `accepted_values(status)` |
| `null_spike` | contract — `not_null(customer_id)` |
| `negative_amounts` | contract — `range(amount)` |
| `currency_unit_change` | anomaly — `avg_amount` |
| `truncated_day` | freshness + volume anomaly |
| `scd_fanout` | dbt — singular + unit test |
| `kb_content_collapse` | RAG — `min_length` + text-length anomaly |

The drill also caught a bug in **itself**: the first `type_drift` injector used
`f"{v:,.2f}"`, which is a no-op here because amounts max out at $255 and never
gain a thousands separator. It injected nothing and reported a false `MISS`. The
validator was fine; the test was wrong. Worth recording — a red result is only
evidence once you have checked the harness.

---

## Detection coverage

Which layer catches which fault — the honest version, including the misses:

| Fault | Contract | Anomaly | Freshness | dbt | Caught? |
|---|---|---|---|---|---|
| `duplicate_pk` | **BLOCK** | ok (603 rows is normal) | ok | **FAIL**, 9 nodes skipped | yes |
| `volume_drop` | pass (every row valid) | **ALERT** (score 18.17) | ok | pass | yes |
| `stale_kb` | quarantine | ok | **ALERT** (190 min > 60) | n/a | yes |
| SCD fan-out | pass | ok | ok | **unit test FAIL** | yes |

No single column catches everything. That is the whole argument for layering.

---

## Known limitations

- The `replay_guard_hours` default means a batch older than 24 hours validated
  with no explicit `now` reports freshness as skipped rather than breached. Pass
  `now` to force evaluation.
- `evaluate_multiwindow_burn` takes pre-computed burn rates. Deriving them from a
  real event-count time series needs run history the lab does not persist;
  `run_baseline.py` approximates the long window from a single run, which
  correctly reads as "not yet proven sustained" rather than pretending otherwise.
- Embedding drift works on precomputed norms; no embedding model is loaded.
- The weekly-stride fallback in `_seasonal_segment` assumes a contiguous daily
  series. Supply `history_day_of_week` whenever labels are available.
