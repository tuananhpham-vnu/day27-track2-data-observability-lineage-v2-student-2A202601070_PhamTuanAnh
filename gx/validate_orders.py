#!/usr/bin/env python3
"""Great Expectations Core 1.21 validation flow for the orders dataset.

Upgrades the starter one-expectation-at-a-time demo into the real GX object
model:

    ExpectationSuite -> BatchDefinition -> ValidationDefinition -> Checkpoint

and adds a **severity-aware action layer** on top of the Checkpoint result:
each expectation is tagged ``critical`` / ``warning`` / ``info`` via its meta,
and the run is translated into one pipeline action (``block`` / ``quarantine``
/ ``warn`` / ``pass``) plus a quarantine side table.

Run:  python gx/validate_orders.py  [--data path/to/orders.csv]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    import great_expectations as gx
except ImportError as exc:  # friendlier classroom failure
    raise SystemExit(
        "great_expectations is not installed. Run: pip install -r requirements.txt"
    ) from exc

from src.contract_validator import SEVERITY_ORDER, action_for_severity

SUITE_NAME = "orders_contract_suite"
CRITICAL, WARNING, INFO = "critical", "warning", "info"


def build_suite() -> Any:
    """Expectation suite mirroring contracts/orders_contract.yaml.

    Severity lives in each expectation's ``meta`` so the Checkpoint result can be
    routed by business impact rather than treated as one undifferentiated
    boolean. GX tells us *what* failed; severity tells us *what to do about it*.
    """
    expectation_specs: list[tuple[Any, str, str]] = [
        # --- identity: a broken primary key silently double-counts revenue
        (gx.expectations.ExpectColumnValuesToNotBeNull(column="order_id"), CRITICAL, "block"),
        (gx.expectations.ExpectColumnValuesToBeUnique(column="order_id"), CRITICAL, "block"),
        (
            gx.expectations.ExpectColumnValuesToBeOfType(column="order_id", type_="int64"),
            CRITICAL,
            "block",
        ),
        # --- money: nulls and negatives corrupt the CEO dashboard directly
        (gx.expectations.ExpectColumnValuesToNotBeNull(column="amount"), CRITICAL, "block"),
        (
            gx.expectations.ExpectColumnValuesToBeBetween(column="amount", min_value=0),
            CRITICAL,
            "block",
        ),
        (
            gx.expectations.ExpectColumnValuesToBeInSet(
                column="currency", value_set=["USD", "VND"]
            ),
            CRITICAL,
            "block",
        ),
        (
            gx.expectations.ExpectColumnValuesToNotBeNull(column="customer_id"),
            CRITICAL,
            "block",
        ),
        # --- enum drift: a new status code is a product change, not corruption,
        #     so isolate the rows instead of stopping the whole pipeline
        (
            gx.expectations.ExpectColumnValuesToBeInSet(
                column="status",
                value_set=["pending", "completed", "refunded", "cancelled"],
            ),
            WARNING,
            "quarantine",
        ),
        (gx.expectations.ExpectColumnValuesToNotBeNull(column="created_at"), CRITICAL, "block"),
        (gx.expectations.ExpectColumnValuesToNotBeNull(column="updated_at"), CRITICAL, "block"),
        # --- volume: catches the partial-ingestion fault that every row-level
        #     expectation passes right through
        (
            gx.expectations.ExpectTableRowCountToBeBetween(min_value=200, max_value=2000),
            WARNING,
            "quarantine",
        ),
        (
            gx.expectations.ExpectTableColumnsToMatchSet(
                column_set=[
                    "order_id",
                    "customer_id",
                    "amount",
                    "currency",
                    "status",
                    "created_at",
                    "updated_at",
                ]
            ),
            CRITICAL,
            "block",
        ),
        # --- distribution: informational guard rail on average order value
        (
            gx.expectations.ExpectColumnMeanToBeBetween(
                column="amount", min_value=40, max_value=120
            ),
            INFO,
            "warn",
        ),
    ]

    suite = gx.ExpectationSuite(name=SUITE_NAME)
    for expectation, severity, action in expectation_specs:
        expectation.meta = {"severity": severity, "action": action}
        suite.add_expectation(expectation)
    return suite


def severity_of(result: dict[str, Any]) -> str:
    meta = (result.get("expectation_config") or {}).get("meta") or {}
    return str(meta.get("severity", WARNING)).lower()


def route_actions(checkpoint_result: Any) -> dict[str, Any]:
    """Translate a Checkpoint result into one pipeline action.

    This is the piece the starter was missing: GX answers "did an expectation
    fail?", but the pipeline needs "do I stop, isolate, or log?".
    """
    failures: list[dict[str, Any]] = []
    unexpected_rows: set[int] = set()

    for run_result in checkpoint_result.run_results.values():
        payload = run_result.to_json_dict() if hasattr(run_result, "to_json_dict") else run_result
        for result in payload.get("results", []):
            if result.get("success"):
                continue
            severity = severity_of(result)
            config = result.get("expectation_config") or {}
            failures.append(
                {
                    "expectation": config.get("type") or config.get("expectation_type"),
                    "column": (config.get("kwargs") or {}).get("column"),
                    "severity": severity,
                    "action": action_for_severity(severity),
                    "details": result.get("result", {}),
                }
            )
            unexpected_rows.update(result.get("result", {}).get("unexpected_index_list") or [])

    if not failures:
        action = "pass"
    else:
        worst = max(failures, key=lambda f: SEVERITY_ORDER.get(f["severity"], 1))
        action = action_for_severity(worst["severity"])

    return {
        "action": action,
        "failures": failures,
        "unexpected_rows": sorted(unexpected_rows),
        "failed_count": len(failures),
        "critical_count": sum(1 for f in failures if f["severity"] == CRITICAL),
    }


def run(data_path: Path, quarantine_path: Path | None = None) -> dict[str, Any]:
    df = pd.read_csv(data_path)
    context = gx.get_context()

    # Suite + BatchDefinition + ValidationDefinition + Checkpoint.
    suite = context.suites.add(build_suite())

    data_source = context.data_sources.add_pandas("orders_pandas")
    asset = data_source.add_dataframe_asset(name="orders_dataframe")
    batch_definition = asset.add_batch_definition_whole_dataframe("whole_orders")

    validation_definition = context.validation_definitions.add(
        gx.ValidationDefinition(
            name="orders_contract_validation",
            data=batch_definition,
            suite=suite,
        )
    )

    checkpoint = context.checkpoints.add(
        gx.Checkpoint(
            name="orders_contract_checkpoint",
            validation_definitions=[validation_definition],
            # Ask GX for the offending row indexes so failures can be quarantined
            # instead of only counted.
            result_format={"result_format": "COMPLETE"},
        )
    )

    result = checkpoint.run(batch_parameters={"dataframe": df})
    routed = route_actions(result)
    routed["success"] = bool(result.success)
    routed["rows"] = int(len(df))
    routed["data_path"] = str(data_path)

    if quarantine_path and routed["unexpected_rows"]:
        bad = [i for i in routed["unexpected_rows"] if i in df.index]
        quarantine_path.parent.mkdir(parents=True, exist_ok=True)
        df.loc[bad].to_csv(quarantine_path, index=False)
        routed["quarantined_rows"] = len(bad)
        routed["quarantine_path"] = str(quarantine_path)
    else:
        routed["quarantined_rows"] = 0
        routed["quarantine_path"] = None

    return routed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data", default=str(ROOT / "data" / "incoming" / "orders.csv"), help="orders CSV"
    )
    parser.add_argument(
        "--quarantine",
        default=str(ROOT / "reports" / "quarantine" / "gx_orders_quarantine.csv"),
    )
    args = parser.parse_args()

    routed = run(Path(args.data), Path(args.quarantine))

    print("=== GREAT EXPECTATIONS CHECKPOINT ===")
    print(f"dataset            : {routed['data_path']} ({routed['rows']} rows)")
    print(f"checkpoint success : {routed['success']}")
    print(f"failed expectations: {routed['failed_count']} (critical={routed['critical_count']})")
    for failure in routed["failures"]:
        column = failure["column"] or "<table>"
        print(f"  [{failure['severity']:<8}] {failure['expectation']} on {column}")
    print(f"pipeline action    : {routed['action'].upper()}")
    if routed["quarantined_rows"]:
        print(f"quarantined rows   : {routed['quarantined_rows']} -> {routed['quarantine_path']}")

    out = ROOT / "reports" / "gx_checkpoint_result.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(routed, indent=2, default=str), encoding="utf-8")
    print(f"report             : {out.relative_to(ROOT)}")

    # A blocking failure must fail the process, otherwise the pipeline reports
    # SUCCESS while shipping bad data - the exact trap this lab is about.
    if routed["action"] == "block":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
