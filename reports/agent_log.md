# AI Agent Decision Log

Agent used: Claude (Claude Code). Every proposal below was verified by running
code — no change was accepted because the agent sounded confident.

---

## Decision 1 — Freshness validation vs. the shipped public test

- **Hypothesis:** Adding contract freshness validation to `validate_dataframe`
  is required by Phase 1 and should be straightforward.
- **Prompt / request to agent:** Implement `contract['freshness']` checking
  against `max_delay_minutes` with severity.
- **Agent proposal:** Compare `max(updated_at)` to wall-clock `now()`.
- **Evidence/test:** `pytest tests_public/test_contracts.py` → **FAILED**.
  `test_healthy_contract_passes_starter_checks` uses a fixture with hard-coded
  timestamps (`2026-08-28T10:05:00Z`). Six days later that fixture is stale by
  ~8,800 minutes against a 30-minute SLA, so a *correct* freshness check fails a
  test that is supposed to represent healthy data.
- **Accept / reject / revise:** **Revised.**
- **Why:** Both behaviours are defensible, so the disagreement was real, not a
  bug. A freshness SLA compares data time to *pipeline run time*; a validator
  handed only a DataFrame has no run time. The fix was to make that explicit: an
  optional `now` parameter, and a documented `replay_guard_hours` (default 24h)
  under which a batch with no supplied run time whose newest record is more than
  a day old is reported as a **skipped** check rather than a breach — replaying
  last month's data must not page anyone. Verified this keeps all three live
  behaviours: healthy batch passes, `stale_kb` (3h) is still **caught**, and the
  historical fixture is correctly skipped. All 10 public tests pass.

---

## Decision 2 — Rejected the agent's first distribution-shift wiring

- **Hypothesis:** `fct_daily_revenue`'s input amounts should be monitored for
  distribution drift on every run.
- **Prompt / request to agent:** Wire `detect_distribution_shift` into the
  baseline pipeline.
- **Agent proposal:** Compare today's per-order `amount` values against the
  `avg_amount` column of `metrics_history.csv`.
- **Evidence/test:** `python scripts/run_baseline.py` on a **known-healthy**
  batch → `amount distribution : ALERT (signals=['ks', 'psi'])`.
- **Accept / reject / revise:** **Rejected.**
- **Why:** A false positive on healthy data, and the cause was a population
  mismatch, not a threshold that needed loosening. 600 individual order amounts
  (spread $10–$200) were being compared against 43 *daily averages* (clustered
  near $70). Averages are far tighter than the values they average, so KS and PSI
  correctly reported two different distributions — the comparison itself was
  wrong. Fixed by comparing per-order amounts against the per-order baseline
  snapshot, and monitoring the daily-average series separately as a time series.
  Healthy run is now clean. **Lesson: an alert firing is not evidence the
  detector works.** We only trusted the detectors after checking them against
  healthy data, not just against faults.

---

## Decision 3 — Same-weekday baseline, and the test that justified it

- **Hypothesis:** The pooled z-score is unsafe on this data because
  `metrics_history.csv` has obvious weekly seasonality (weekdays ~600, weekends
  ~250).
- **Prompt / request to agent:** Upgrade `method="auto"` for seasonality and
  outlier robustness.
- **Agent proposal:** Segment history by weekday, then apply median/MAD; fall
  back to EWMA when `context['trend']` is set and to z-score when the segment is
  short.
- **Evidence/test:** Built `scripts/evidence.py` to compare old vs new on the
  same inputs rather than trusting the reasoning. Result: a Wednesday collapsing
  to 330 orders (a **47% revenue shortfall**) scores only **1.10** on the pooled
  z-score — *silent* — because weekend values inflate the pooled std to 160.
  The same-weekday baseline (spread ~13) scores it **7.91** — a loud alert.
- **Accept / reject / revise:** **Accepted.**
- **Why:** The measured miss is the justification, not the argument. Also fixed
  two related blind spots found while testing: the starter's `mad_is_zero_todo`
  early-return made flat metrics permanently undetectable (a `null_rate` jumping
  0.0 → 0.35 returned `is_anomaly=False`), and a single past outage in the
  history blinded the z-score to an ongoing one (score 1.13 vs 40.34).

---

## Decision 4 — Demanded proof that the dbt unit test earns its place

- **Hypothesis:** `fct_daily_revenue` can inflate revenue via SCD-2 fan-out, but
  the current seed data hides it.
- **Prompt / request to agent:** Write the smallest unit test exposing revenue
  inflation from multiple active customer versions.
- **Agent proposal:** A fixture with 1 order and 2 active versions of the same
  customer, expecting `completed_order_rows: 1, daily_revenue: 100.0`; plus a
  `row_number()` de-duplication fix in the model.
- **Evidence/test:** Not taken on trust. Temporarily **reverted the model to the
  buggy join** and re-ran `dbt test --select fct_daily_revenue`:
  - buggy model → `FAIL 1 fct_daily_revenue::scd_fanout_does_not_inflate_revenue`,
    while **all 22 data tests still passed**
  - fixed model → `PASS=29 ERROR=0`
- **Accept / reject / revise:** **Accepted.**
- **Why:** This is the only test in the project that fails on the bug, and it
  does so on fixed fixture data while every `not_null`/`unique`/`accepted_values`
  test passes. That is the concrete answer to "why isn't `not_null` a unit test":
  data tests assert properties of whatever rows happen to exist and can only fail
  *after* bad data arrives; a unit test asserts the transformation logic and
  fails while production data is still clean. Verified that today's seed has 75
  active rows / 75 distinct customers — the bug was genuinely latent.

---

## Decision 5 — Kept a heuristic the agent proposed, but narrowed it

- **Hypothesis:** `scripts/triage.py` should flag any column whose distribution
  moved.
- **Prompt / request to agent:** Build a dataset-diff triage tool for the mystery
  incident.
- **Agent proposal:** Run KS/PSI across every numeric column.
- **Evidence/test:** On the `volume_drop` scenario the tool flagged **`order_id`
  as SUSPECT** (`ks=0.75, psi=8.96`) alongside the real finding.
- **Accept / reject / revise:** **Revised.**
- **Why:** `order_id` is a monotonically increasing surrogate key — its
  "distribution" shifts every single healthy day, so the signal is pure noise and
  would train the on-call to ignore the table. Excluded the declared key column
  from distribution comparison (key health is already covered by the dedicated
  duplicate/missing-key section). After the change `volume_drop` reports exactly
  one fired signal (`volume`) and `duplicate_pk` reports exactly two (`keys`,
  `contract`). **A detector that is right but noisy is a detector that gets
  muted.**

---

## Where the agent was most and least useful

- **Most useful:** mechanical breadth — GX 1.21's Suite/ValidationDefinition/
  Checkpoint object model, `row_number()` de-duplication, numpy-only KS/PSI so no
  scipy dependency was added, and the Google SRE 14.4/6/3 burn-rate tiers.
- **Least useful / needed correction:** every judgement about *what to compare
  against what* (Decisions 2 and 5). The agent produced statistically valid code
  applied to the wrong populations. Both errors were invisible in code review and
  only appeared when the pipeline was run against **known-healthy** data.
- **Practice adopted:** run every detector against a healthy batch before running
  it against a fault. A detector that alerts on a fault has proven nothing until
  it also stays quiet on healthy data.
