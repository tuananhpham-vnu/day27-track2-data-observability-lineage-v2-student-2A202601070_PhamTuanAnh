"""Stable interface used by public and instructor-side hidden evaluation.

Students may refactor internals, but keep these function names and return shapes.

The nine documented functions below keep their original signatures. Extra
keyword arguments are optional and additive, and the extra helpers at the bottom
expose the upgraded capabilities (quarantine, blast radius, KB freshness)
without changing anything the hidden evaluation already relies on.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

import pandas as pd

from observability.anomaly import detect_anomaly
from observability.distribution import detect_distribution_shift
from observability.lineage import (
    blast_radius,
    get_column_downstream,
    get_downstream_assets,
    get_upstream_assets,
)
from observability.rag_metrics import (
    detect_embedding_norm_shift,
    detect_kb_staleness,
    detect_text_length_shift,
)
from observability.slo import calculate_slo, evaluate_multiwindow_burn
from src.contract_validator import (
    decide_action,
    enforce_contract,
    failed_issues,
    load_contract,
    validate_dataframe,
)

# ---------------------------------------------------------------------------
# 1-9: the documented stable interface
# ---------------------------------------------------------------------------


def validate_orders(
    df: pd.DataFrame,
    contract_path: str | Path,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Validate a dataframe against a contract YAML.

    ``now`` optionally supplies the pipeline run time used by the freshness
    check; without it the check falls back to wall clock.
    """
    return validate_dataframe(df, load_contract(contract_path), now=now)


def detect_metric(
    current: float,
    history: Iterable[float],
    *,
    method: str = "auto",
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return detect_anomaly(current, history, method=method, context=context)


def detect_distribution(
    current_values: Iterable[float], baseline_values: Iterable[float]
) -> dict[str, Any]:
    return detect_distribution_shift(current_values, baseline_values)


def slo_status(target: float, bad_events: int, total_events: int) -> dict[str, Any]:
    return calculate_slo(target, bad_events, total_events)


def multiwindow_burn(short_window_burn: float, long_window_burn: float) -> dict[str, Any]:
    return evaluate_multiwindow_burn(
        short_window_burn=short_window_burn,
        long_window_burn=long_window_burn,
    )


def downstream_assets(graph: dict[str, list[str]], start: str) -> list[str]:
    return get_downstream_assets(graph, start)


def column_downstream(graph: dict[str, list[str]], start: str) -> list[str]:
    return get_column_downstream(graph, start)


def rag_length_shift(
    current_texts: Iterable[str], baseline_batch_means: Iterable[float]
) -> dict[str, Any]:
    return detect_text_length_shift(current_texts, baseline_batch_means)


def rag_embedding_shift(
    current_norms: Iterable[float], baseline_norms: Iterable[float]
) -> dict[str, Any]:
    return detect_embedding_norm_shift(current_norms, baseline_norms)


# ---------------------------------------------------------------------------
# Additive helpers used by the pipeline, dashboard, and incident report
# ---------------------------------------------------------------------------


def contract_action(issues: list[dict[str, Any]]) -> str:
    """Collapse validation issues into ``block`` / ``quarantine`` / ``warn`` / ``pass``."""
    return decide_action(issues)


def contract_failures(
    issues: list[dict[str, Any]], min_severity: str | None = None
) -> list[dict[str, Any]]:
    return failed_issues(issues, min_severity=min_severity)


def enforce_orders_contract(
    df: pd.DataFrame,
    contract_path: str | Path,
    *,
    quarantine_path: str | Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate, decide an action, and split/persist the quarantined rows."""
    return enforce_contract(
        df,
        load_contract(contract_path),
        quarantine_path=quarantine_path,
        now=now,
    )


def upstream_assets(graph: dict[str, list[str]], start: str) -> list[str]:
    return get_upstream_assets(graph, start)


def impact_report(
    graph: dict[str, list[str]],
    start: str,
    *,
    column_graph: dict[str, list[str]] | None = None,
    changed_columns: Iterable[str] | None = None,
    critical_assets: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Dataset + column blast radius for an incident on ``start``."""
    return blast_radius(
        graph,
        start,
        column_graph=column_graph,
        changed_columns=changed_columns,
        critical_assets=critical_assets,
    )


def kb_freshness(
    documents: Sequence[dict[str, Any]],
    *,
    max_age_minutes: float = 60.0,
    now: datetime | None = None,
) -> dict[str, Any]:
    return detect_kb_staleness(
        documents, max_age_minutes=max_age_minutes, now=now
    )
