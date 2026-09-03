"""Distribution shift detection.

The starter compared means only. A mean ratio is blind to the shifts that
actually break analytics and RAG pipelines:

- a **bimodal split** (half the rows switched currency/unit) keeps the mean
  roughly stable while the distribution changes completely,
- a **variance blow-up** with an unchanged mean,
- a **tail shift** that moves p95 but barely moves the average.

This module keeps the cheap mean ratio as one signal and combines it with a
two-sample **Kolmogorov-Smirnov** statistic and the **Population Stability
Index**, both implemented with numpy only (no scipy dependency).
"""
from __future__ import annotations

from typing import Any, Iterable

import numpy as np

#: Conventional PSI reading: < 0.1 stable, 0.1-0.25 moderate, > 0.25 significant.
PSI_THRESHOLD = 0.25

#: KS critical value multiplier for approximately alpha = 0.05.
KS_ALPHA_COEFFICIENT = 1.36


def _clean(values: Iterable[float]) -> np.ndarray:
    array = np.asarray(list(values), dtype=float)
    return array[np.isfinite(array)]


def ks_statistic(current: np.ndarray, baseline: np.ndarray) -> float:
    """Two-sample Kolmogorov-Smirnov statistic (max CDF gap)."""
    if current.size == 0 or baseline.size == 0:
        return 0.0
    grid = np.sort(np.concatenate([current, baseline]))
    cdf_current = np.searchsorted(np.sort(current), grid, side="right") / current.size
    cdf_baseline = np.searchsorted(np.sort(baseline), grid, side="right") / baseline.size
    return float(np.max(np.abs(cdf_current - cdf_baseline)))


def ks_critical_value(n_current: int, n_baseline: int) -> float:
    """Approximate KS critical value at alpha = 0.05."""
    if n_current == 0 or n_baseline == 0:
        return float("inf")
    return KS_ALPHA_COEFFICIENT * np.sqrt(
        (n_current + n_baseline) / (n_current * n_baseline)
    )


def population_stability_index(
    current: np.ndarray, baseline: np.ndarray, *, bins: int = 10
) -> float:
    """PSI over quantile bins of the baseline distribution."""
    if current.size == 0 or baseline.size == 0:
        return 0.0
    edges = np.unique(np.quantile(baseline, np.linspace(0, 1, bins + 1)))
    if edges.size < 2:
        # Baseline is a single constant: PSI is undefined, so report movement
        # away from that constant as maximal instability.
        return 0.0 if np.allclose(current, baseline[0]) else float("inf")
    edges[0], edges[-1] = -np.inf, np.inf

    baseline_share = np.histogram(baseline, bins=edges)[0] / baseline.size
    current_share = np.histogram(current, bins=edges)[0] / current.size

    # Laplace-style floor so an emptied bin contributes a large but finite term.
    epsilon = 1e-6
    baseline_share = np.clip(baseline_share, epsilon, None)
    current_share = np.clip(current_share, epsilon, None)
    return float(np.sum((current_share - baseline_share) * np.log(current_share / baseline_share)))


def detect_distribution_shift(
    current_values: Iterable[float],
    baseline_values: Iterable[float],
    *,
    ratio_threshold: float = 3.0,
) -> dict[str, Any]:
    """Combine mean ratio, KS, and PSI into one verdict.

    Any single signal firing is enough - these detect different failure shapes,
    so requiring consensus would only reintroduce blind spots.
    """
    cur = _clean(current_values)
    base = _clean(baseline_values)
    if cur.size == 0 or base.size == 0:
        return {
            "is_anomaly": False,
            "score": 0.0,
            "method": "mean_ratio+ks+psi",
            "reason": "empty_input",
        }

    cur_mean, base_mean = float(np.mean(cur)), float(np.mean(base))

    # --- signal 1: mean ratio (kept from the starter, cheap and interpretable)
    if base_mean == 0:
        mean_ratio = float("inf") if cur_mean != 0 else 1.0
    elif cur_mean == 0:
        mean_ratio = float("inf")
    else:
        mean_ratio = max(abs(cur_mean / base_mean), abs(base_mean / cur_mean))
    ratio_fired = mean_ratio >= ratio_threshold

    # --- signal 2: KS (shape of the whole distribution)
    ks = ks_statistic(cur, base)
    ks_critical = ks_critical_value(cur.size, base.size)
    # Two tiny samples make the critical value exceed 1, which no statistic can
    # reach; only trust KS when the samples can actually reject.
    ks_fired = bool(ks_critical <= 1.0 and ks > ks_critical)

    # --- signal 3: PSI (binned mass movement, standard in monitoring)
    psi = population_stability_index(cur, base)
    psi_fired = bool(psi > PSI_THRESHOLD)

    fired = [
        name
        for name, flag in (("mean_ratio", ratio_fired), ("ks", ks_fired), ("psi", psi_fired))
        if flag
    ]

    return {
        "is_anomaly": bool(fired),
        # Normalised so 1.0 == "at the alert boundary" regardless of signal.
        "score": float(mean_ratio),
        "method": "mean_ratio+ks+psi",
        "reason": (
            f"baseline_mean={base_mean:.3f}, current_mean={cur_mean:.3f}, "
            f"mean_ratio={mean_ratio:.3f} (threshold={ratio_threshold}), "
            f"ks={ks:.3f} (critical={ks_critical:.3f}), "
            f"psi={psi:.3f} (threshold={PSI_THRESHOLD}); "
            f"fired={fired or 'none'}"
        ),
        "mean_ratio": float(mean_ratio),
        "ks_statistic": float(ks),
        "ks_critical_value": float(ks_critical),
        "psi": float(psi),
        "signals_fired": fired,
    }
