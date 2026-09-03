# Detector evidence: starter baseline vs upgrade

Every case uses the real 43-day metric history in `data/history/metrics_history.csv`, which has strong weekly seasonality: weekdays run ~600 orders, weekends ~250.

## Case 1 - Weekend false positive (alert fatigue)

A perfectly normal **Saturday** with 250 orders. The pooled history mixes weekdays and weekends, so its mean sits near 500.

- starter z-score (pooled history): **no alert** (score=1.60)
- upgraded auto (same-weekday)   : **no alert** (score=0.13, baseline_median=252)

> The pooled baseline scores a routine Saturday at 1.60 sigma - under the threshold, but it spends most of the alerting margin on ordinary seasonality. The pooled standard deviation is 160 orders against just 13 for Saturdays alone, and that inflated spread is exactly what goes wrong in Case 2.

## Case 2 - Weekday collapse hidden by seasonality (the miss that matters)

A **Wednesday** that ingested only 330 of its usual ~600 orders - a 47% revenue shortfall reaching the CEO dashboard.

- starter z-score (pooled history): **no alert** (score=1.10)
- upgraded auto (same-weekday)   : **ANOMALY** (score=7.91, baseline_median=588)

> This is the headline result. Weekend values inflate the pooled standard deviation to 160, so a ~260-order shortfall scores only 1.10 sigma and the starter detector stays **silent** while revenue is nearly halved. Restricting the baseline to the 6 previous Wednesdays (median 588, MAD-scale spread of a few orders) turns the same shortfall into a score of 7.91 - an unmissable alert.

## Case 3 - One past incident blinds the detector (masking)

Yesterday's outage left a `0` in the history. Today the metric is still broken at 300.

- starter z-score (mean/std) : **no alert** (score=1.13)
- upgraded auto (median/MAD) : **ANOMALY** (score=40.34)

> The single `0` drags the mean down and inflates the std to ~190, so the detector goes blind exactly after a bad day. The median/MAD centre ignores the contaminated point.

## Case 4 - Flat metric (`std = 0`)

A `null_rate` that has been exactly 0.0 for two weeks suddenly reads 0.35.

- starter MAD (`mad_is_zero_todo`): **no alert** (the starter returned early on mad == 0)
- upgraded auto                   : **ANOMALY** (auto:mad:flat_history)

> A zero-dispersion history means the metric never moves, so any material move IS the anomaly. Returning `is_anomaly = False` there is the worst possible answer.

## Case 5 - Distribution shift with an unchanged mean

Half the orders switched currency unit, splitting one tight population into two. The **mean is unchanged**, so a mean-ratio check cannot see it.

- starter mean ratio : **no alert** (mean_ratio=1.00, threshold=3.0)
- upgraded KS + PSI  : **ANOMALY** (signals=['ks', 'psi'], ks=0.50)

## Case 6 - Multi-window burn rate

The starter policy never paged for anything. The upgrade separates a blip from an outage.

| scenario | short | long | page? | tier |
|---|---:|---:|---|---|
| transient spike (short hot, long cold) | 20.0 | 0.5 | **False** | transient_spike |
| sustained fast burn (both hot) | 20.0 | 15.0 | **True** | fast_burn |
| recovering (short cold, long hot) | 0.5 | 8.0 | **False** | recovering |
| healthy | 0.4 | 0.3 | **False** | healthy |

> Paging requires BOTH windows to be burning: the long window proves the problem is sustained, the short window proves it is still happening.

## Case 7 - Column-level blast radius

How far does a bad `raw_orders.amount` actually travel?

- starter (direct children only): `['stg_orders.amount_usd']` -> 1 column
- upgraded (transitive)         : `['stg_orders.amount_usd', 'fct_daily_revenue.daily_revenue', 'ceo_revenue_dashboard.revenue']` -> 3 columns

> The starter stopped at the staging layer and never named `ceo_revenue_dashboard.revenue` - the number the CEO is actually looking at.

## Case 8 - RAG embedding drift

The embedding model was silently upgraded. Documents are unchanged and every row-level contract still passes.

- starter                      : **no alert** (returned `not_implemented` for every input)
- upgraded, model swap         : **ANOMALY** (signals=['centre_shift'])
- upgraded, half-migrated batch: **ANOMALY** (signals=['spread_blowup'])

> The second case is the subtle one. Half the batch was embedded by each model, so the mean norm is preserved (1.000 vs a baseline of 1.007) and any centre-based check passes. Only the spread signal (ratio 13.5x) sees the half-migrated index.

---

Regenerate with `python scripts/evidence.py`.
