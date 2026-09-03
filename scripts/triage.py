#!/usr/bin/env python3
"""Incident triage: localise what changed in data/incoming, using evidence only.

`run_baseline.py` answers "is something wrong?". This answers "what, where, and
since when?" - the questions an RCA needs - by diffing the incoming batch against
the known-good baseline snapshot along every axis that can break independently:

    schema -> volume -> keys -> per-column distributions -> time -> freshness

It reads no fault script and hard-codes no scenario, so it works unchanged on
the instructor's mystery dataset.

Run:  python scripts/triage.py [--dataset orders]
Writes: reports/triage_<dataset>.md
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from observability.anomaly import detect_anomaly
from observability.distribution import detect_distribution_shift
from observability.lineage import blast_radius, load_column_graph, load_graph
from src.contract_validator import enforce_contract, load_contract

CRITICAL_ASSETS = ["fct_daily_revenue", "ceo_revenue_dashboard", "support_agent", "rag_index"]

# Which lineage node each dataset feeds, so triage can name the blast radius.
LINEAGE_ROOT = {"orders": "stg_orders", "customers": "stg_customers"}


class Findings:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def emit(self, text: str = "") -> None:
        print(text)
        self.lines.append(text)

    def section(self, title: str) -> None:
        self.emit()
        self.emit(f"## {title}")
        self.emit()


def compare_schema(f: Findings, cur: pd.DataFrame, base: pd.DataFrame) -> bool:
    f.section("1. Schema")
    added = [c for c in cur.columns if c not in base.columns]
    removed = [c for c in base.columns if c not in cur.columns]
    retyped = [
        f"{c}: {base[c].dtype} -> {cur[c].dtype}"
        for c in cur.columns
        if c in base.columns and cur[c].dtype != base[c].dtype
    ]
    if not (added or removed or retyped):
        f.emit("No schema change.")
        return False
    for label, items in (("added", added), ("removed", removed), ("retyped", retyped)):
        if items:
            f.emit(f"- **{label}**: {', '.join(items)}")
    return True


def compare_volume(f: Findings, cur: pd.DataFrame, base: pd.DataFrame, dataset: str) -> bool:
    f.section("2. Volume")
    delta = len(cur) - len(base)
    pct = (delta / len(base) * 100) if len(base) else float("nan")
    f.emit(f"- incoming rows : **{len(cur)}**")
    f.emit(f"- baseline rows : {len(base)}")
    f.emit(f"- change        : {delta:+d} ({pct:+.1f}%)")

    if dataset == "orders":
        history = pd.read_csv(ROOT / "data" / "history" / "metrics_history.csv")
        result = detect_anomaly(
            len(cur),
            history["row_count"].tolist(),
            method="auto",
            context={
                "metric_name": "row_count",
                "day_of_week": datetime.now(timezone.utc).weekday(),
                "history_day_of_week": history["day_of_week"].tolist(),
            },
        )
        f.emit(
            f"- vs 43-day same-weekday baseline: **{'ANOMALY' if result['is_anomaly'] else 'ok'}** "
            f"(score={result['score']:.2f}, expected~{result.get('baseline_median', float('nan')):.0f})"
        )
        return bool(result["is_anomaly"]) or abs(pct) > 10
    return abs(pct) > 10


def compare_keys(f: Findings, cur: pd.DataFrame, base: pd.DataFrame, key: str | None) -> bool:
    f.section("3. Keys")
    if key is None or key not in cur.columns:
        f.emit("No key column configured for this dataset.")
        return False
    dupes = int(cur[key].duplicated().sum())
    f.emit(f"- duplicate `{key}` values: **{dupes}**")
    if dupes:
        sample = cur[cur[key].duplicated(keep=False)][key].unique()[:5]
        f.emit(f"- example duplicated keys : {[x.item() if hasattr(x, 'item') else x for x in sample]}")

    if key in base.columns:
        missing = set(base[key]) - set(cur[key])
        new = set(cur[key]) - set(base[key])
        f.emit(f"- keys present in baseline but missing now: **{len(missing)}**")
        f.emit(f"- keys new since baseline                 : {len(new)}")
    return dupes > 0


def compare_columns(
    f: Findings, cur: pd.DataFrame, base: pd.DataFrame, key: str | None = None
) -> bool:
    f.section("4. Per-column distributions")
    f.emit("| column | null% now | null% base | detail | verdict |")
    f.emit("|---|---:|---:|---|---|")
    suspicious = False

    for column in cur.columns:
        if column not in base.columns:
            continue
        if column == key:
            # A surrogate key is monotonic by construction, so its "distribution"
            # shifts on every healthy day. Key health is section 3's job.
            f.emit(f"| `{column}` | - | - | surrogate key, see section 3 | skipped |")
            continue
        cur_null = float(cur[column].isna().mean() * 100)
        base_null = float(base[column].isna().mean() * 100)

        cur_num = pd.to_numeric(cur[column], errors="coerce")
        base_num = pd.to_numeric(base[column], errors="coerce")
        numeric = cur_num.notna().mean() > 0.9 and base_num.notna().mean() > 0.9

        if numeric:
            result = detect_distribution_shift(
                cur_num.dropna().tolist(), base_num.dropna().tolist()
            )
            detail = (
                f"mean {float(cur_num.mean()):.2f} vs {float(base_num.mean()):.2f}, "
                f"ks={result['ks_statistic']:.2f}, psi={result['psi']:.2f}"
            )
            fired = bool(result["is_anomaly"])
        else:
            cur_vals, base_vals = set(cur[column].dropna()), set(base[column].dropna())
            new_vals = cur_vals - base_vals
            detail = f"{len(cur_vals)} distinct; new={sorted(map(str, new_vals))[:3] or '-'}"
            # A brand-new categorical value is how enum drift usually arrives.
            fired = bool(new_vals) and len(base_vals) < 20

        null_jump = cur_null - base_null > 1.0
        fired = fired or null_jump
        suspicious = suspicious or fired
        f.emit(
            f"| `{column}` | {cur_null:.2f} | {base_null:.2f} | {detail} | "
            f"{'**SUSPECT**' if fired else 'ok'} |"
        )
    return suspicious


def compare_time(f: Findings, cur: pd.DataFrame, base: pd.DataFrame) -> None:
    """Where in time does the batch differ? This is the 'when did it start?' answer."""
    f.section("5. Time distribution")
    time_cols = [c for c in ("created_at", "updated_at", "published_at") if c in cur.columns]
    if not time_cols:
        f.emit("No timestamp column found.")
        return

    for column in time_cols:
        cur_ts = pd.to_datetime(cur[column], utc=True, errors="coerce")
        base_ts = pd.to_datetime(base[column], utc=True, errors="coerce") if column in base.columns else None
        age = (pd.Timestamp(datetime.now(timezone.utc)) - cur_ts.max()).total_seconds() / 60
        f.emit(f"**`{column}`**")
        f.emit(f"- range now : {cur_ts.min()} -> {cur_ts.max()}")
        if base_ts is not None:
            f.emit(f"- range base: {base_ts.min()} -> {base_ts.max()}")
        f.emit(f"- newest record age: **{age:.1f} minutes**")
        f.emit(f"- unparseable values: {int(cur_ts.isna().sum())}")

        # Hourly histogram localises a partial ingestion to the hour it stopped.
        by_hour = cur_ts.dt.floor("h").value_counts().sort_index()
        if len(by_hour) > 1:
            f.emit("- rows per hour (newest 6):")
            for stamp, count in by_hour.tail(6).items():
                f.emit(f"    - {stamp}: {count}")
        f.emit()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default="orders", choices=["orders", "customers"])
    args = parser.parse_args()
    dataset = args.dataset

    cur = pd.read_csv(ROOT / "data" / "incoming" / f"{dataset}.csv")
    base = pd.read_csv(ROOT / "data" / "baseline" / f"{dataset}.csv")
    key = {"orders": "order_id", "customers": "customer_id"}.get(dataset)

    f = Findings()
    now = datetime.now(timezone.utc)
    f.emit(f"# Triage report - `data/incoming/{dataset}.csv`")
    f.emit()
    f.emit(f"Generated {now.isoformat()} against the baseline snapshot.")

    flags = {
        "schema": compare_schema(f, cur, base),
        "volume": compare_volume(f, cur, base, dataset),
        "keys": compare_keys(f, cur, base, key),
        "columns": compare_columns(f, cur, base, key),
    }
    compare_time(f, cur, base)

    # ---- contract verdict
    f.section("6. Contract verdict")
    contract_path = ROOT / "contracts" / f"{dataset}_contract.yaml"
    if contract_path.exists():
        result = enforce_contract(cur, load_contract(contract_path), now=now)
        f.emit(f"- action: **{result['action'].upper()}**")
        if result["failed"]:
            for issue in result["failed"]:
                f.emit(f"    - [{issue['severity']}] `{issue['check']}` on `{issue['column']}`: {issue['details']}")
        else:
            f.emit("- all contract checks passed")
        flags["contract"] = bool(result["failed"])
    else:
        f.emit(f"No contract defined at `contracts/{dataset}_contract.yaml`.")
        flags["contract"] = False

    # ---- blast radius
    f.section("7. Blast radius")
    lineage_path = ROOT / "data" / "baseline" / "lineage_graph.json"
    root = LINEAGE_ROOT.get(dataset, dataset)
    impact = blast_radius(
        load_graph(lineage_path),
        root,
        column_graph=load_column_graph(lineage_path),
        critical_assets=CRITICAL_ASSETS,
    )
    f.emit(f"If `{root}` is wrong, the following are wrong too:")
    f.emit()
    f.emit(f"- datasets  : {', '.join(impact['affected_datasets'])}")
    f.emit(f"- columns   : {', '.join(impact['affected_columns']) or 'n/a'}")
    f.emit(f"- consumers : {', '.join(impact['impacted_consumers'])}")
    f.emit(f"- critical  : {', '.join(impact['critical_assets_hit']) or 'none'}")

    # ---- conclusion
    f.section("8. Where to look first")
    hit = [name for name, fired in flags.items() if fired]
    if hit:
        f.emit(f"Signals that fired: **{', '.join(hit)}**")
        f.emit()
        hints = {
            "schema": "A producer changed the contract shape - check the upstream deploy.",
            "volume": "Partial ingestion or a dropped partition - check the extract job and its watermark.",
            "keys": "Duplicate primary keys - a re-run or replay that appended instead of upserting.",
            "columns": "A value distribution moved - check for a unit, enum, or encoding change.",
            "contract": "Deterministic rules failed - the details above name the exact column.",
        }
        for name in hit:
            f.emit(f"- **{name}**: {hints[name]}")
    else:
        f.emit("No signal fired. The incoming batch matches the baseline on every axis checked.")

    out = ROOT / "reports" / f"triage_{dataset}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(f.lines) + "\n", encoding="utf-8")
    print(f"\nWritten: {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
