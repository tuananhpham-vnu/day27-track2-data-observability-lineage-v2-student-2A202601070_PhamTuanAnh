"""Anomaly detection for the Data Reliability Game Day lab.

The starter shipped a plain z-score. Z-score is kept intact as an explicit
method, and ``auto`` is upgraded into a context-aware, robust detector.

Why the plain z-score is not enough
-----------------------------------
1. ``std == 0``  - a perfectly flat history makes every deviation infinitely
   anomalous (or, if you guard it naively, nothing is ever anomalous).
2. **Seasonality** - this business does ~600 orders on weekdays and ~250 at the
   weekend. A raw 14-day window mixes both populations, inflating ``std`` so a
   real weekday collapse hides inside it, while a perfectly normal Saturday
   looks like a 1.5-sigma dip. Segmenting by weekday fixes both directions.
3. **Outliers** - the mean and std are computed from the same history that
   contains the incident. One past spike drags the mean and, worse, inflates the
   std, so the detector goes blind exactly after a bad day (masking).
4. **Trend** - a slowly growing metric makes every recent value "anomalous"
   against an old mean.

``auto`` therefore: segments the history by weekday when it can, prefers the
robust median/MAD centre, falls back to a rolling z-score when MAD degenerates,
and reports which strategy fired.
"""
from __future__ import annotations

from typing import Any, Iterable, Sequence

import numpy as np

#: Scale factor making the MAD a consistent estimator of sigma for normal data.
MAD_TO_SIGMA = 0.6745

#: A same-weekday segment needs at least this many points to be trusted on its own.
MIN_SEGMENT_POINTS = 3

#: Relative tolerance used when the dispersion estimate collapses to zero.
FLAT_HISTORY_REL_TOLERANCE = 0.05


def _as_array(values: Iterable[float]) -> np.ndarray:
    array = np.asarray(list(values), dtype=float)
    return array[np.isfinite(array)]


def zscore_detector(
    current: float, history: Iterable[float], threshold: float = 3.0
) -> dict[str, Any]:
    values = _as_array(history)
    if values.size < 3:
        return {
            "is_anomaly": False,
            "score": 0.0,
            "method": "zscore",
            "reason": "insufficient_history",
        }
    mean = float(np.mean(values))
    std = float(np.std(values))
    if std == 0:
        score = float("inf") if float(current) != mean else 0.0
    else:
        score = abs(float(current) - mean) / std
    return {
        "is_anomaly": bool(score > threshold),
        "score": float(score),
        "method": "zscore",
        "reason": f"mean={mean:.3f}, std={std:.3f}, threshold={threshold}",
    }


def mad_detector(
    current: float, history: Iterable[float], threshold: float = 3.5
) -> dict[str, Any]:
    """Median/MAD detector - robust to outliers already present in the history.

    The starter bailed out whenever the MAD was zero, which silently disabled
    detection for flat metrics (a very common shape: ``null_rate``, a constant
    row count, a fixed schema size). A zero MAD means "this metric never moves",
    so *any* material move is the anomaly; we fall back to a relative-deviation
    rule instead of returning a false negative.
    """
    values = _as_array(history)
    if values.size < 5:
        return {
            "is_anomaly": False,
            "score": 0.0,
            "method": "mad",
            "reason": "insufficient_history",
        }

    median = float(np.median(values))
    mad = float(np.median(np.abs(values - median)))
    current = float(current)

    if mad == 0:
        return _flat_history_verdict(current, median, method="mad", threshold=threshold)

    modified_z = MAD_TO_SIGMA * abs(current - median) / mad
    return {
        "is_anomaly": bool(modified_z > threshold),
        "score": float(modified_z),
        "method": "mad",
        "reason": f"median={median:.3f}, mad={mad:.3f}, threshold={threshold}",
    }


def _flat_history_verdict(
    current: float, centre: float, *, method: str, threshold: float
) -> dict[str, Any]:
    """Verdict when the dispersion estimate is zero (a perfectly flat history)."""
    deviation = abs(current - centre)
    scale = max(abs(centre), 1.0)
    relative = deviation / scale
    is_anomaly = relative > FLAT_HISTORY_REL_TOLERANCE
    return {
        "is_anomaly": bool(is_anomaly),
        # Report a score on the same scale as the threshold so downstream
        # comparisons and dashboards stay meaningful.
        "score": float(threshold * 2 if is_anomaly else 0.0),
        "method": f"{method}:flat_history",
        "reason": (
            f"dispersion=0 (flat history), centre={centre:.3f}, "
            f"relative_deviation={relative:.3%}, tolerance={FLAT_HISTORY_REL_TOLERANCE:.0%}"
        ),
    }


def ewma_detector(
    current: float, history: Iterable[float], *, alpha: float = 0.3, threshold: float = 3.0
) -> dict[str, Any]:
    """Exponentially weighted baseline - tracks trend instead of a static mean."""
    values = _as_array(history)
    if values.size < 3:
        return {
            "is_anomaly": False,
            "score": 0.0,
            "method": "ewma",
            "reason": "insufficient_history",
        }

    level = float(values[0])
    residuals: list[float] = []
    for value in values[1:]:
        residuals.append(float(value) - level)
        level = alpha * float(value) + (1 - alpha) * level

    residual_std = float(np.std(residuals)) if residuals else 0.0
    current = float(current)
    if residual_std == 0:
        return _flat_history_verdict(current, level, method="ewma", threshold=threshold)

    score = abs(current - level) / residual_std
    return {
        "is_anomaly": bool(score > threshold),
        "score": float(score),
        "method": "ewma",
        "reason": (
            f"ewma_level={level:.3f}, residual_std={residual_std:.3f}, "
            f"alpha={alpha}, threshold={threshold}"
        ),
    }


def _seasonal_segment(
    history: Sequence[float], context: dict[str, Any] | None
) -> tuple[list[float], str | None]:
    """Pick the same-weekday (or caller-supplied) slice of the history.

    Returns ``(values, description)``; ``description`` is ``None`` when no
    seasonal segmentation was possible and the full history should be used.
    """
    if not context:
        return list(history), None

    # 1. The caller already did the segmentation for us.
    explicit = context.get("same_segment_history")
    if explicit:
        values = list(explicit)
        if len(values) >= MIN_SEGMENT_POINTS:
            return values, "same_segment_history"

    # 2. Derive a same-weekday segment from parallel day-of-week labels.
    day_of_week = context.get("day_of_week")
    labels = context.get("history_day_of_week") or context.get("history_days_of_week")
    if day_of_week is not None and labels is not None:
        labels = list(labels)
        if len(labels) == len(history):
            values = [
                float(v) for v, label in zip(history, labels) if label == day_of_week
            ]
            if len(values) >= MIN_SEGMENT_POINTS:
                return values, f"same_weekday(dow={day_of_week})"

    # 3. Weekly seasonality with no labels: assume the history is a daily series
    #    ending on the day *before* the observation, and step back in 7s.
    if day_of_week is not None and len(history) >= 7 + MIN_SEGMENT_POINTS:
        values = [float(v) for v in history[-7::-7]][::-1]
        if len(values) >= MIN_SEGMENT_POINTS:
            return values, f"weekly_stride(dow={day_of_week})"

    return list(history), None


def detect_anomaly(
    current: float,
    history: Iterable[float],
    *,
    method: str = "auto",
    threshold: float = 3.0,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Stable lab API.

    - ``zscore``: the original mean/std detector, unchanged.
    - ``mad``: robust median/MAD detector with a flat-history fallback.
    - ``ewma``: trend-following baseline.
    - ``auto``: seasonality-aware, robust, and context-driven (see below).

    ``auto`` honours these ``context`` keys:

    ``day_of_week``
        Weekday of the observation. Combined with ``history_day_of_week`` (or,
        failing that, a weekly stride) it builds a same-weekday baseline so a
        normal weekend is not paged and a weekday collapse is not masked.
    ``same_segment_history``
        A pre-segmented baseline; used verbatim when long enough.
    ``known_event``
        A declared launch/campaign/holiday. Suppresses the alert (the deviation
        is expected) but keeps the score so it stays visible on dashboards.
    ``trend``
        ``"increasing"``/``"decreasing"`` switches to the EWMA baseline, which
        does not treat a healthy trend as a permanent anomaly.
    ``metric_name``
        Carried into the reason string for triage.
    """
    if method == "mad":
        return mad_detector(current, history, threshold=3.5)
    if method == "ewma":
        return ewma_detector(current, history, threshold=threshold)
    if method == "zscore":
        return zscore_detector(current, history, threshold=threshold)
    if method != "auto":
        raise ValueError(f"Unsupported method: {method}")

    context = context or {}
    values = [float(v) for v in _as_array(history)]

    segment, segment_desc = _seasonal_segment(values, context)
    baseline = segment if segment_desc else values

    if len(baseline) < MIN_SEGMENT_POINTS:
        return {
            "is_anomaly": False,
            "score": 0.0,
            "method": "auto",
            "reason": "insufficient_history",
        }

    # Trend-dominated metrics need a moving level, not a static centre.
    trend = str(context.get("trend") or "").lower()
    if trend in {"increasing", "decreasing", "trending"}:
        result = ewma_detector(current, baseline, threshold=threshold)
        strategy = "ewma"
    elif len(baseline) >= 5:
        result = mad_detector(current, baseline, threshold=3.5)
        strategy = "mad"
    else:
        # Too few points for a stable MAD; the z-score is the honest fallback.
        result = zscore_detector(current, baseline, threshold=threshold)
        strategy = "zscore"

    median = float(np.median(baseline))
    reason_parts = [f"strategy={strategy}", f"baseline_n={len(baseline)}"]
    if segment_desc:
        reason_parts.append(f"segment={segment_desc}")
    else:
        reason_parts.append("segment=full_history")
    if context.get("metric_name"):
        reason_parts.append(f"metric={context['metric_name']}")
    reason_parts.append(f"baseline_median={median:.3f}")
    reason_parts.append(f"current={float(current):.3f}")
    reason_parts.append(result["reason"])

    verdict: dict[str, Any] = {
        "is_anomaly": bool(result["is_anomaly"]),
        "score": float(result["score"]),
        "method": f"auto:{result['method']}",
        "reason": "; ".join(reason_parts),
        "baseline_median": median,
        "baseline_n": len(baseline),
        "direction": "drop" if float(current) < median else "spike",
    }

    # A declared event explains the deviation: keep the signal, drop the page.
    known_event = context.get("known_event")
    if known_event and verdict["is_anomaly"]:
        verdict["is_anomaly"] = False
        verdict["suppressed_by"] = str(known_event)
        verdict["reason"] += f"; suppressed_by_known_event={known_event}"

    return verdict
