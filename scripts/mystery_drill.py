#!/usr/bin/env python3
"""Blind drill: does the detection stack generalise to faults it has never seen?

Phase 6 grades an investigation of a fault the analyst did not write. The three
public faults are useless for that - the detectors were tuned against them, so
catching them proves nothing about the mystery incident.

This drill injects **seven fault classes that appear nowhere in the public set**
and asks, for each: did any layer fire, and did triage point at the right place?

Crucially, the detection code is never told which fault ran. `inject()` writes
only to `data/incoming/`; the expectation is compared afterwards. Nothing in
`observability/`, `src/`, or `scripts/triage.py` branches on scenario name.

Run:  python scripts/mystery_drill.py [--scenario NAME] [--seed N]
Writes: reports/mystery_drill.md
"""
from __future__ import annotations

import argparse
import io
import random
import sys
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from observability.anomaly import detect_anomaly
from observability.rag_metrics import detect_kb_staleness
from src.contract_validator import enforce_contract, load_contract
from src.io_utils import load_jsonl, save_jsonl

INCOMING = ROOT / "data" / "incoming"
ORDERS = INCOMING / "orders.csv"


# ---------------------------------------------------------------------------
# Fault classes - NONE of these exist in scripts/inject_fault.py
# ---------------------------------------------------------------------------


def type_drift(rng: random.Random) -> dict:
    """Upstream starts sending amounts as locale-formatted / annotated strings.

    NOTE: an earlier version of this injector used f"{v:,.2f}", which is a no-op
    on this dataset because amounts max out at $255 and never gain a thousands
    separator. It injected nothing and the drill reported a false MISS. Real
    type drift needs values that genuinely will not coerce.
    """
    df = pd.read_csv(ORDERS, dtype={"amount": str})
    idx = rng.sample(range(len(df)), 40)
    forms = ["{v} USD", "1.{n}50,00", "N/A", " {v} "]
    for i, row in enumerate(idx):
        raw = df.loc[row, "amount"]
        df.loc[row, "amount"] = forms[i % len(forms)].format(v=raw, n=i)
    df.to_csv(ORDERS, index=False)
    return {"expect_layer": "contract", "expect_column": "amount", "expect_check": "type"}


def currency_unit_change(rng: random.Random) -> dict:
    """A subset switches to VND without converting - amounts inflate ~25000x."""
    df = pd.read_csv(ORDERS)
    idx = rng.sample(range(len(df)), 120)
    df.loc[idx, "currency"] = "VND"
    df.loc[idx, "amount"] = (df.loc[idx, "amount"] * 25000).round(2)
    df.to_csv(ORDERS, index=False)
    return {"expect_layer": "anomaly/distribution", "expect_column": "amount", "expect_check": "distribution"}


def enum_drift(rng: random.Random) -> dict:
    """Producer ships a new status code nobody agreed on."""
    df = pd.read_csv(ORDERS)
    idx = rng.sample(range(len(df)), 25)
    df.loc[idx, "status"] = "partially_refunded"
    df.to_csv(ORDERS, index=False)
    return {"expect_layer": "contract", "expect_column": "status", "expect_check": "accepted_values"}


def null_spike(rng: random.Random) -> dict:
    """A join upstream starts losing customer ids."""
    df = pd.read_csv(ORDERS)
    idx = rng.sample(range(len(df)), 55)
    df.loc[idx, "customer_id"] = None
    df.to_csv(ORDERS, index=False)
    return {"expect_layer": "contract", "expect_column": "customer_id", "expect_check": "not_null"}


def negative_amounts(rng: random.Random) -> dict:
    """Refunds written as negative gross instead of a separate refund row."""
    df = pd.read_csv(ORDERS)
    idx = rng.sample(range(len(df)), 18)
    df.loc[idx, "amount"] = -df.loc[idx, "amount"].abs()
    df.to_csv(ORDERS, index=False)
    return {"expect_layer": "contract", "expect_column": "amount", "expect_check": "range"}


def scd_fanout(rng: random.Random) -> dict:
    """The latent dimension bug: duplicate ACTIVE customer versions."""
    path = INCOMING / "customers.csv"
    df = pd.read_csv(path)
    active = df[df["is_active"].astype(str).str.lower() == "true"]
    dupes = active.sample(n=min(12, len(active)), random_state=rng.randint(0, 9999)).copy()
    dupes["valid_from"] = "2026-08-01T00:00:00+00:00"
    pd.concat([df, dupes], ignore_index=True).to_csv(path, index=False)
    return {"expect_layer": "dbt", "expect_column": "customer_id", "expect_check": "singular/unit test"}


def truncated_day(rng: random.Random) -> dict:
    """Extract stopped mid-run: newest hours missing, so data is valid but stale."""
    df = pd.read_csv(ORDERS)
    updated = pd.to_datetime(df["updated_at"], utc=True, format="mixed")
    cutoff = updated.max() - timedelta(minutes=95)
    df[updated <= cutoff].to_csv(ORDERS, index=False)
    return {"expect_layer": "freshness", "expect_column": "updated_at", "expect_check": "freshness"}


def kb_content_collapse(rng: random.Random) -> dict:
    """A parser regression truncates KB docs to their first few words."""
    path = INCOMING / "kb_documents.jsonl"
    docs = load_jsonl(path)
    for doc in docs:
        doc["content"] = " ".join(str(doc["content"]).split()[:3])
    save_jsonl(path, docs)
    return {"expect_layer": "rag", "expect_column": "content", "expect_check": "text_length/min_length"}


SCENARIOS = {
    "type_drift": type_drift,
    "currency_unit_change": currency_unit_change,
    "enum_drift": enum_drift,
    "null_spike": null_spike,
    "negative_amounts": negative_amounts,
    "scd_fanout": scd_fanout,
    "truncated_day": truncated_day,
    "kb_content_collapse": kb_content_collapse,
}


# ---------------------------------------------------------------------------
# Detection - identical to what runs on a real batch, told nothing
# ---------------------------------------------------------------------------


def detect() -> dict:
    """Run the standard stack against data/incoming and report what fired."""
    now = datetime.now(timezone.utc)
    fired: list[str] = []
    detail: list[str] = []

    orders = pd.read_csv(ORDERS)
    history = pd.read_csv(ROOT / "data" / "history" / "metrics_history.csv")

    result = enforce_contract(
        orders, load_contract(ROOT / "contracts" / "orders_contract.yaml"), now=now
    )
    for issue in result["failed"]:
        if issue["check"] == "freshness":
            fired.append("freshness")
        else:
            fired.append("contract")
        detail.append(f"{issue['check']}({issue['column']})")

    customers = pd.read_csv(INCOMING / "customers.csv")
    active = customers[customers["is_active"].astype(str).str.lower() == "true"]
    if active["customer_id"].duplicated().any():
        fired.append("dbt")
        n = int(active["customer_id"].duplicated().sum())
        detail.append(f"scd_active_versions(customer_id)x{n}")

    row = detect_anomaly(
        len(orders),
        history["row_count"].tolist(),
        method="auto",
        context={
            "metric_name": "row_count",
            "day_of_week": now.weekday(),
            "history_day_of_week": history["day_of_week"].tolist(),
        },
    )
    if row["is_anomaly"]:
        fired.append("anomaly/volume")
        detail.append(f"row_count={len(orders)}")

    amounts = pd.to_numeric(orders["amount"], errors="coerce").dropna()
    avg = detect_anomaly(
        float(amounts.mean()),
        history["avg_amount"].tolist(),
        method="auto",
        context={"metric_name": "avg_amount"},
    )
    if avg["is_anomaly"]:
        fired.append("anomaly/distribution")
        detail.append(f"avg_amount={amounts.mean():,.1f}")

    docs = load_jsonl(INCOMING / "kb_documents.jsonl")
    kb = detect_kb_staleness(docs, max_age_minutes=60.0, now=now)
    if kb["is_anomaly"]:
        fired.append("freshness")
        detail.append(f"kb_age={kb['age_minutes']:.0f}min")

    kb_contract = enforce_contract(
        pd.DataFrame(docs), load_contract(ROOT / "contracts" / "kb_contract.yaml"), now=now
    )
    for issue in kb_contract["failed"]:
        fired.append("rag" if issue["check"] == "min_length" else "contract")
        detail.append(f"kb.{issue['check']}({issue['column']})")

    lengths = [len(str(d["content"]).split()) for d in docs]
    text = detect_anomaly(
        float(sum(lengths) / len(lengths)),
        history["mean_text_length"].tolist(),
        method="auto",
        context={"metric_name": "mean_text_length"},
    )
    if text["is_anomaly"]:
        fired.append("rag")
        detail.append(f"mean_text_length={sum(lengths)/len(lengths):.1f}")

    return {"fired": sorted(set(fired)), "detail": detail, "action": result["action"]}


def reset() -> None:
    import runpy

    argv = sys.argv
    sys.argv = ["reset_lab.py"]
    try:
        with redirect_stdout(io.StringIO()):
            runpy.run_path(str(ROOT / "scripts" / "reset_lab.py"), run_name="__main__")
    finally:
        sys.argv = argv


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), default=None)
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args()

    names = [args.scenario] if args.scenario else sorted(SCENARIOS)
    seed = args.seed if args.seed is not None else random.randrange(10_000)
    rng = random.Random(seed)

    rows: list[dict] = []
    print("=== MYSTERY DRILL - fault classes absent from the public set ===")
    print(f"seed={seed}\n")

    for name in names:
        reset()
        expected = SCENARIOS[name](rng)
        found = detect()

        want = expected["expect_layer"].split("/")[0]
        caught = any(f.split("/")[0] == want for f in found["fired"])
        # Detected by *some* layer, even if not the one predicted.
        detected = bool(found["fired"])

        rows.append(
            {
                "scenario": name,
                "expected_layer": expected["expect_layer"],
                "fired": ", ".join(found["fired"]) or "-",
                "detected": detected,
                "right_layer": caught,
                "evidence": "; ".join(found["detail"][:3]) or "-",
            }
        )
        status = "PASS" if caught else ("PARTIAL" if detected else "MISS")
        print(f"[{status:<7}] {name:<22} expected={expected['expect_layer']:<22} fired={found['fired']}")

    reset()

    detected_n = sum(r["detected"] for r in rows)
    right_n = sum(r["right_layer"] for r in rows)
    print(f"\ndetected by some layer : {detected_n}/{len(rows)}")
    print(f"detected by the predicted layer: {right_n}/{len(rows)}")

    lines = [
        "# Mystery drill - unseen fault classes",
        "",
        f"Seed `{seed}`. Eight fault classes that appear **nowhere** in "
        "`scripts/inject_fault.py`. The detection stack is never told which ran.",
        "",
        f"- detected by some layer: **{detected_n}/{len(rows)}**",
        f"- detected by the predicted layer: **{right_n}/{len(rows)}**",
        "",
        "| scenario | expected layer | layers that fired | right layer? | evidence |",
        "|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| `{r['scenario']}` | {r['expected_layer']} | {r['fired']} | "
            f"{'yes' if r['right_layer'] else ('other layer' if r['detected'] else '**NO**')} | "
            f"{r['evidence']} |"
        )
    lines += [
        "",
        "Reproduce: `python scripts/mystery_drill.py --seed " + str(seed) + "`",
        "",
        "Investigate one by hand: `python scripts/mystery_drill.py --scenario <name>` "
        "leaves the fault in `data/incoming/` only if you comment out the final reset.",
    ]
    out = ROOT / "reports" / "mystery_drill.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"report: {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
