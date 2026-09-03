"""RAG / knowledge-base reliability metrics.

A support agent fails silently: the pipeline is green, the index is built, and
the answers are simply wrong because the retrieved documents are stale, empty,
or truncated. These metrics are the cheap proxies that catch it without needing
an embedding model at hand.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

import numpy as np

from observability.anomaly import detect_anomaly, zscore_detector


def approximate_token_lengths(texts: Iterable[str]) -> list[int]:
    # Deliberately simple proxy; no tokenizer/model download needed.
    return [len(str(t).split()) for t in texts]


def detect_text_length_shift(
    current_texts: Iterable[str],
    baseline_batch_means: Iterable[float],
    *,
    threshold: float = 3.0,
) -> dict[str, Any]:
    """Flag a collapse (truncation) or explosion (boilerplate) in chunk length."""
    lengths = approximate_token_lengths(current_texts)
    current_mean = float(np.mean(lengths)) if lengths else 0.0

    result = zscore_detector(current_mean, baseline_batch_means, threshold=threshold)
    if not result["is_anomaly"]:
        # The z-score goes blind when one bad batch already sits in the history
        # or when batch means barely move; the robust detector is the backstop.
        robust = detect_anomaly(
            current_mean,
            baseline_batch_means,
            method="auto",
            context={"metric_name": "mean_text_length"},
        )
        if robust["is_anomaly"]:
            result = robust

    baseline = list(baseline_batch_means)
    baseline_mean = float(np.mean(baseline)) if baseline else 0.0
    result["metric"] = "mean_text_length"
    result["current_mean"] = current_mean
    result["baseline_mean"] = baseline_mean
    result["direction"] = "collapse" if current_mean < baseline_mean else "expansion"
    return result


def detect_embedding_norm_shift(
    current_norms: Iterable[float],
    baseline_norms: Iterable[float],
    *,
    threshold: float = 3.0,
) -> dict[str, Any]:
    """Detect embedding-space drift from precomputed vector norms.

    Embedding norms move when the *encoder* changes (model/version swap,
    different normalisation, a truncated context window) even though the
    documents themselves are unchanged - a failure no row-level data contract
    can see, because every row is still perfectly valid.

    Both the centre and the spread are checked: a model swap usually shifts the
    mean norm, while a partially failed batch (some documents embedded by the
    new model, some by the old) shows up as a variance blow-up with a nearly
    unchanged mean.
    """
    cur = np.asarray(list(current_norms), dtype=float)
    base = np.asarray(list(baseline_norms), dtype=float)
    cur = cur[np.isfinite(cur)]
    base = base[np.isfinite(base)]

    if cur.size == 0 or base.size == 0:
        return {
            "is_anomaly": False,
            "score": 0.0,
            "method": "embedding_norm",
            "reason": "empty_input",
        }
    if base.size < 3:
        return {
            "is_anomaly": False,
            "score": 0.0,
            "method": "embedding_norm",
            "reason": "insufficient_baseline",
        }

    cur_mean, base_mean = float(np.mean(cur)), float(np.mean(base))
    base_median = float(np.median(base))
    base_mad = float(np.median(np.abs(base - base_median)))

    # Centre shift, measured robustly so one bad historical batch cannot mask it.
    if base_mad > 0:
        centre_score = 0.6745 * abs(cur_mean - base_median) / base_mad
    else:
        scale = max(abs(base_median), 1e-9)
        centre_score = float("inf") if abs(cur_mean - base_median) / scale > 0.01 else 0.0
    centre_fired = centre_score > threshold

    # Spread blow-up: a mixed-encoder batch keeps the mean but widens the spread.
    cur_std, base_std = float(np.std(cur)), float(np.std(base))
    if base_std > 0:
        spread_ratio = cur_std / base_std
    else:
        spread_ratio = float("inf") if cur_std > 0 else 1.0
    spread_fired = bool(cur.size >= 3 and spread_ratio > 3.0)

    fired = [
        name
        for name, flag in (("centre_shift", centre_fired), ("spread_blowup", spread_fired))
        if flag
    ]

    return {
        "is_anomaly": bool(fired),
        "score": float(centre_score if np.isfinite(centre_score) else 1e9),
        "method": "embedding_norm:robust",
        "reason": (
            f"baseline_median={base_median:.4f}, baseline_mad={base_mad:.4f}, "
            f"current_mean={cur_mean:.4f} (baseline_mean={base_mean:.4f}), "
            f"centre_score={centre_score:.3f} (threshold={threshold}), "
            f"spread_ratio={spread_ratio:.3f}; fired={fired or 'none'}"
        ),
        "metric": "embedding_norm",
        "current_mean": cur_mean,
        "baseline_mean": base_mean,
        "spread_ratio": float(spread_ratio),
        "signals_fired": fired,
        "direction": "drop" if cur_mean < base_median else "rise",
    }


def detect_kb_staleness(
    documents: Sequence[dict[str, Any]],
    *,
    max_age_minutes: float = 60.0,
    timestamp_field: str = "published_at",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Freshness of the knowledge base actually serving the support agent.

    This is the signal behind "the agent quotes an old refund policy": the
    documents are schema-valid and the index builds fine, so only a freshness
    check on the *newest* document can catch it.
    """
    import pandas as pd

    stamps = pd.to_datetime(
        [doc.get(timestamp_field) for doc in documents], utc=True, errors="coerce"
    )
    stamps = stamps[~stamps.isna()]
    if len(stamps) == 0:
        return {
            "is_anomaly": True,
            "score": float("inf"),
            "method": "kb_freshness",
            "reason": f"no parseable '{timestamp_field}' in {len(documents)} documents",
        }

    now_ts = pd.Timestamp(now or datetime.now(timezone.utc))
    if now_ts.tzinfo is None:
        now_ts = now_ts.tz_localize("UTC")
    newest = stamps.max()
    age_minutes = float((now_ts - newest).total_seconds() / 60.0)

    return {
        "is_anomaly": bool(age_minutes > max_age_minutes),
        "score": float(age_minutes / max_age_minutes) if max_age_minutes else 0.0,
        "method": "kb_freshness",
        "reason": (
            f"newest_document_age_minutes={age_minutes:.1f}; "
            f"max_age_minutes={max_age_minutes}; newest={newest.isoformat()}; "
            f"documents={len(documents)}"
        ),
        "metric": "kb_age_minutes",
        "age_minutes": age_minutes,
        "max_age_minutes": float(max_age_minutes),
        "document_count": len(documents),
    }
