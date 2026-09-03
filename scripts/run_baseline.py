#!/usr/bin/env python3
"""Data reliability pipeline run: validate -> detect -> blast radius -> SLO.

Every layer answers a different question, and the whole point of the lab is that
no single one of them is enough:

  contract   deterministic rules I can write down in advance
  anomaly    statistical shape I could not have written a rule for
  freshness  data that is valid but too old to be true
  lineage    who is hurt by the failure
  SLO        whether this is worth waking somebody up for

The script exits non-zero when the run must not be trusted, so the pipeline
cannot report SUCCESS while shipping bad data.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from observability.anomaly import detect_anomaly
from observability.distribution import detect_distribution_shift
from observability.lineage import blast_radius, load_column_graph, load_graph
from observability.rag_metrics import detect_kb_staleness, detect_text_length_shift
from observability.slo import calculate_slo, evaluate_multiwindow_burn
from src.contract_validator import enforce_contract, load_contract
from src.io_utils import load_jsonl

CRITICAL_ASSETS = ["fct_daily_revenue", "ceo_revenue_dashboard", "support_agent", "rag_index"]


def _fmt(flag: bool) -> str:
    return "ALERT" if flag else "ok"


def main() -> None:
    now = datetime.now(timezone.utc)
    orders = pd.read_csv(ROOT / "data" / "incoming" / "orders.csv")
    history = pd.read_csv(ROOT / "data" / "history" / "metrics_history.csv")

    # ------------------------------------------------------------------ 1. contract
    orders_result = enforce_contract(
        orders,
        load_contract(ROOT / "contracts" / "orders_contract.yaml"),
        quarantine_path=ROOT / "reports" / "quarantine" / "orders_quarantine.csv",
        now=now,
    )
    failed = orders_result["failed"]
    critical_failed = orders_result["critical_failed"]

    # ------------------------------------------------------------------ 2. anomaly
    # Same-weekday baseline: this business runs ~600 orders on weekdays and ~250
    # at the weekend, so a pooled window both hides weekday collapses and cries
    # wolf every Saturday. The segmentation now lives inside detect_anomaly, and
    # the labels are handed over as context rather than pre-filtered by hand.
    current_dow = now.weekday()
    row_result = detect_anomaly(
        len(orders),
        history["row_count"].tolist(),
        method="auto",
        context={
            "metric_name": "row_count",
            "day_of_week": current_dow,
            "history_day_of_week": history["day_of_week"].tolist(),
        },
    )

    # Distribution drift must compare like with like: per-order amounts against
    # per-order amounts. Comparing today's individual orders to a history of
    # *daily averages* is a population mismatch and fires on every healthy run,
    # because averages are far tighter than the values they average.
    baseline_orders = pd.read_csv(ROOT / "data" / "baseline" / "orders.csv")
    amount_result = detect_distribution_shift(
        orders["amount"].dropna().tolist(),
        baseline_orders["amount"].dropna().tolist(),
    )

    # The daily-average series is still a useful, separate signal - just as a
    # time series compared against its own history.
    avg_amount_result = detect_anomaly(
        float(orders["amount"].mean()),
        history["avg_amount"].tolist(),
        method="auto",
        context={"metric_name": "avg_amount"},
    )

    # ------------------------------------------------------------------ 3. freshness
    freshness_issue = next(
        (i for i in orders_result["issues"] if i["check"] == "freshness"), None
    )
    orders_lag_minutes = freshness_issue["lag_minutes"] if freshness_issue else float("nan")

    docs = load_jsonl(ROOT / "data" / "incoming" / "kb_documents.jsonl")
    kb_result = enforce_contract(
        pd.DataFrame(docs), load_contract(ROOT / "contracts" / "kb_contract.yaml"), now=now
    )
    kb_freshness = detect_kb_staleness(docs, max_age_minutes=60.0, now=now)
    text_result = detect_text_length_shift(
        [d["content"] for d in docs], history["mean_text_length"].tail(14).tolist()
    )

    # ------------------------------------------------------------------ 4. lineage
    lineage_path = ROOT / "data" / "baseline" / "lineage_graph.json"
    graph = load_graph(lineage_path)
    column_graph = load_column_graph(lineage_path)

    orders_broken = bool(critical_failed or row_result["is_anomaly"])
    kb_broken = bool(kb_freshness["is_anomaly"] or kb_result["critical_failed"])

    impacts = []
    if orders_broken:
        impacts.append(
            blast_radius(
                graph,
                "stg_orders",
                column_graph=column_graph,
                changed_columns=["stg_orders.amount_usd"],
                critical_assets=CRITICAL_ASSETS,
            )
        )
    if kb_broken:
        impacts.append(
            blast_radius(
                graph,
                "kb_documents",
                column_graph=column_graph,
                changed_columns=["kb_documents.content"],
                critical_assets=CRITICAL_ASSETS,
            )
        )

    # ------------------------------------------------------------------ 5. SLO
    # One pipeline run is one check event per SLI.
    checks = {
        "critical_contract_pass": (0.999, 1 if critical_failed else 0),
        "revenue_freshness": (0.995, 1 if (freshness_issue and not freshness_issue["passed"]) else 0),
        "rag_index_freshness": (0.99, 1 if kb_freshness["is_anomaly"] else 0),
    }
    slos = {
        name: calculate_slo(target, bad_events=bad, total_events=1)
        for name, (target, bad) in checks.items()
    }
    bad_now = sum(bad for _, bad in checks.values())
    # Short window = this run; long window = this run against the recent record.
    # With a single run available the long window is approximated by the same
    # event, which correctly reads as "not yet proven sustained".
    burn = evaluate_multiwindow_burn(
        short_window_burn=max(s["burn_rate"] for s in slos.values()),
        long_window_burn=0.0 if bad_now == 0 else 1.0,
    )

    # ------------------------------------------------------------------ report
    report = {
        "timestamp": now.isoformat(),
        "orders_rows": int(len(orders)),
        "contract_action": orders_result["action"],
        "failed_contract_checks": len(failed),
        "critical_contract_failures": len(critical_failed),
        "failed_checks_detail": [
            {k: v for k, v in i.items() if k != "failed_rows"} for i in failed
        ],
        "quarantined_rows": orders_result["quarantined_rows"],
        "quarantine_path": orders_result["quarantine_path"],
        "row_count_anomaly": row_result,
        "amount_distribution": amount_result,
        "avg_amount_anomaly": avg_amount_result,
        "freshness_minutes": orders_lag_minutes,
        "kb_contract_action": kb_result["action"],
        "kb_freshness": kb_freshness,
        "kb_text_length_signal": text_result,
        "slos": slos,
        "multiwindow_burn": burn,
        "blast_radius": impacts,
    }
    out = ROOT / "reports" / "latest_metrics.json"
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    # ------------------------------------------------------------------ stdout
    print("=== DATA RELIABILITY BASELINE ===")
    print(f"orders rows              : {len(orders)}")
    print(f"contract action          : {orders_result['action'].upper()}")
    print(f"contract failed checks   : {len(failed)} (critical={len(critical_failed)})")
    for issue in failed:
        print(f"    [{issue['severity']:<8}] {issue['check']} on {issue['column']}: {issue['details'][:80]}")
    if orders_result["quarantined_rows"]:
        print(f"quarantined rows         : {orders_result['quarantined_rows']} -> {orders_result['quarantine_path']}")
    print(
        f"row-count anomaly        : {_fmt(row_result['is_anomaly'])} "
        f"({row_result['method']}, score={row_result['score']:.2f}, "
        f"baseline_median={row_result.get('baseline_median', float('nan')):.0f})"
    )
    print(f"amount distribution      : {_fmt(amount_result['is_anomaly'])} (signals={amount_result['signals_fired'] or 'none'})")
    print(f"avg amount anomaly       : {_fmt(avg_amount_result['is_anomaly'])} ({avg_amount_result['method']}, score={avg_amount_result['score']:.2f})")
    print(f"orders freshness minutes : {orders_lag_minutes:.1f}")
    print(f"KB freshness             : {_fmt(kb_freshness['is_anomaly'])} (age={kb_freshness['age_minutes']:.1f} min, max=60)")
    print(f"KB contract action       : {kb_result['action'].upper()}")
    print(f"KB length anomaly        : {_fmt(text_result['is_anomaly'])}")
    for name, slo in slos.items():
        print(f"SLO {name:<24}: burn_rate={slo['burn_rate']:.1f} breached={slo['breached']}")
    print(f"burn policy              : {burn['tier']} (page={burn['page']}, severity={burn['severity']})")
    for impact in impacts:
        print(f"blast radius from {impact['root']}:")
        print(f"    datasets : {', '.join(impact['affected_datasets'])}")
        print(f"    columns  : {', '.join(impact['affected_columns']) or 'n/a'}")
        print(f"    consumers: {', '.join(impact['impacted_consumers'])}")
        print(f"    critical : {', '.join(impact['critical_assets_hit']) or 'none'}")
    if not impacts:
        print("blast radius             : none - no upstream asset is currently degraded")
    print(f"report                   : {out.relative_to(ROOT)}")

    # A run that failed a critical contract check, or that detected a volume
    # collapse, must not be reported as SUCCESS.
    if orders_result["action"] == "block" or row_result["is_anomaly"] or kb_freshness["is_anomaly"]:
        print("\nRUN STATUS: FAILED - downstream consumers must not use this batch.")
        raise SystemExit(1)
    print("\nRUN STATUS: HEALTHY")


if __name__ == "__main__":
    main()
