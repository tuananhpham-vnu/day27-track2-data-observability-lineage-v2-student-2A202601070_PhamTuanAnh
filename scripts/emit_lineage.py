#!/usr/bin/env python3
"""Emit OpenLineage events and a dbt-derived lineage graph.

Two sources of lineage, deliberately compared:

1. `data/baseline/lineage_graph.json` - hand-maintained, covers the whole system
   including assets dbt does not know about (the CEO dashboard, the RAG index,
   the support agent).
2. `dbt_project/target/manifest.json` - generated, always true for the warehouse
   half, but blind to everything outside dbt.

A hand-maintained graph drifts; a generated one is incomplete. Printing the
difference is the point - it shows exactly which edges nobody is validating.

Run:  python scripts/emit_lineage.py      (run `make dbt` first for the manifest)
Writes: reports/openlineage_events.jsonl, reports/lineage_reconciliation.md
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from observability.lineage import (
    extract_dbt_model_graph,
    get_downstream_assets,
    load_graph,
    write_openlineage_events,
)

MANIFEST = ROOT / "dbt_project" / "target" / "manifest.json"


def main() -> None:
    declared = load_graph(ROOT / "data" / "baseline" / "lineage_graph.json")

    events_path = write_openlineage_events(
        declared,
        ROOT / "reports" / "openlineage_events.jsonl",
        job_namespace="data-reliability-lab",
    )
    event_count = sum(1 for _ in events_path.open(encoding="utf-8"))
    print("=== OPENLINEAGE ===")
    print(f"emitted {event_count} RunEvent(s) -> {events_path.relative_to(ROOT)}")
    print("load into Marquez with: docker run -p 5000:5000 marquezproject/marquez")

    lines = ["# Lineage reconciliation", ""]
    print("\n=== DBT MANIFEST ===")
    if not MANIFEST.exists():
        message = "manifest.json not found - run `make dbt` first."
        print(message)
        lines += [message, ""]
    else:
        derived = extract_dbt_model_graph(MANIFEST)
        print(f"dbt nodes with children: {len([k for k, v in derived.items() if v])}")

        declared_edges = {(p, c) for p, cs in declared.items() for c in cs}
        derived_edges = {(p, c) for p, cs in derived.items() for c in cs}
        dbt_nodes = set(derived) | {c for cs in derived.values() for c in cs}

        # Only compare edges whose endpoints dbt could possibly know about.
        comparable = {(p, c) for p, c in declared_edges if p in dbt_nodes and c in dbt_nodes}
        missing_in_dbt = sorted(comparable - derived_edges)
        outside_dbt = sorted(declared_edges - comparable)

        lines += [
            f"- declared edges (hand-maintained): **{len(declared_edges)}**",
            f"- dbt-derived edges              : **{len(derived_edges)}**",
            f"- declared edges dbt confirms    : **{len(comparable & derived_edges)}**",
            "",
            "## Declared edges dbt does NOT confirm",
            "",
        ]
        lines += [f"- `{p}` -> `{c}`" for p, c in missing_in_dbt] or ["- none (all confirmed)"]
        lines += [
            "",
            "## Edges outside dbt's visibility",
            "",
            "These assets are downstream of the warehouse but not dbt models, so no",
            "generated lineage will ever cover them. They are only protected by the",
            "hand-maintained graph - and are exactly where blast radius gets missed.",
            "",
        ]
        lines += [f"- `{p}` -> `{c}`" for p, c in outside_dbt]

        print(f"declared edges dbt confirms: {len(comparable & derived_edges)}/{len(comparable)}")
        print(f"edges outside dbt's visibility: {len(outside_dbt)}")
        for parent, child in outside_dbt:
            print(f"    {parent} -> {child}")

    lines += [
        "",
        "## Blast radius reference",
        "",
        "```text",
        "stg_orders    -> " + ", ".join(get_downstream_assets(declared, "stg_orders")),
        "kb_documents  -> " + ", ".join(get_downstream_assets(declared, "kb_documents")),
        "```",
    ]

    out = ROOT / "reports" / "lineage_reconciliation.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nreport: {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
