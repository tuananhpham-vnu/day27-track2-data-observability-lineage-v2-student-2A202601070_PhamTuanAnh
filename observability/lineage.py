"""Dataset and column lineage, blast radius, and dbt/OpenLineage interop."""
from __future__ import annotations

import json
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def load_graph(path: str | Path) -> dict[str, list[str]]:
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    return payload["dataset_lineage"] if "dataset_lineage" in payload else payload


def load_column_graph(path: str | Path) -> dict[str, list[str]]:
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    return payload.get("column_lineage", {})


def _bfs(graph: dict[str, list[str]], start: str) -> list[str]:
    """Transitive descendants in BFS order, excluding ``start``.

    Cycle-safe: ``seen`` is seeded with ``start`` and every node is enqueued at
    most once, so a graph with a loop terminates instead of hanging.
    """
    seen = {start}
    queue: deque[str] = deque([start])
    out: list[str] = []
    while queue:
        node = queue.popleft()
        for child in graph.get(node, []) or []:
            if child not in seen:
                seen.add(child)
                out.append(child)
                queue.append(child)
    return out


def get_downstream_assets(graph: dict[str, list[str]], start: str) -> list[str]:
    """Return transitive downstream assets in BFS order, excluding start."""
    return _bfs(graph, start)


def get_upstream_assets(graph: dict[str, list[str]], start: str) -> list[str]:
    """Return transitive *upstream* assets - the search space for a root cause."""
    return _bfs(reverse_graph(graph), start)


def reverse_graph(graph: dict[str, list[str]]) -> dict[str, list[str]]:
    reversed_graph: dict[str, list[str]] = {}
    for parent, children in graph.items():
        for child in children or []:
            reversed_graph.setdefault(child, []).append(parent)
    return reversed_graph


def get_column_downstream(
    column_graph: dict[str, list[str]], start_column: str
) -> list[str]:
    """Transitive column-level downstream traversal.

    The starter returned only direct children, so a three-hop path such as
    ``raw_orders.amount -> stg_orders.amount_usd -> fct_daily_revenue.daily_revenue
    -> ceo_revenue_dashboard.revenue`` reported a blast radius of one column when
    it is really three. Column lineage is what turns "a table is broken" into
    "this number on the CEO dashboard is wrong".
    """
    return _bfs(column_graph, start_column)


def blast_radius(
    graph: dict[str, list[str]],
    start: str,
    *,
    column_graph: dict[str, list[str]] | None = None,
    changed_columns: Iterable[str] | None = None,
    critical_assets: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Full impact assessment for an incident on ``start``.

    Returns the affected datasets, the affected columns (when a column graph is
    supplied), the impacted leaf consumers, and whether anything business
    critical is in the path.
    """
    datasets = get_downstream_assets(graph, start)
    consumers = [asset for asset in datasets if not graph.get(asset)]

    columns: list[str] = []
    if column_graph:
        for column in changed_columns or [
            key for key in column_graph if key.startswith(f"{start}.")
        ]:
            for downstream in get_column_downstream(column_graph, column):
                if downstream not in columns:
                    columns.append(downstream)

    critical = sorted(set(datasets) & set(critical_assets or []))
    return {
        "root": start,
        "affected_datasets": datasets,
        "affected_columns": columns,
        "impacted_consumers": consumers,
        "critical_assets_hit": critical,
        "depth": len(datasets),
    }


# --------------------------------------------------------------------------
# dbt manifest
# --------------------------------------------------------------------------


def extract_dbt_dataset_graph(manifest_path: str | Path) -> dict[str, list[str]]:
    """Map each dbt node unique_id to the nodes that depend on it."""
    path = Path(manifest_path)
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    graph: dict[str, list[str]] = {}
    for parent, children in manifest.get("child_map", {}).items():
        graph[parent] = list(children)
    return graph


def extract_dbt_model_graph(manifest_path: str | Path) -> dict[str, list[str]]:
    """Same as :func:`extract_dbt_dataset_graph` but keyed by readable names.

    dbt unique_ids (``model.data_reliability_lab.stg_orders``) are unusable in an
    incident channel. This collapses them to ``stg_orders`` and drops test nodes,
    which are assertions about the graph rather than assets in it.
    """
    path = Path(manifest_path)
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    nodes = {**manifest.get("nodes", {}), **manifest.get("sources", {})}

    def readable(unique_id: str) -> str | None:
        if unique_id.startswith("test."):
            return None
        node = nodes.get(unique_id)
        if node is None:
            return unique_id.split(".")[-1]
        return node.get("name") or unique_id.split(".")[-1]

    graph: dict[str, list[str]] = {}
    for parent, children in manifest.get("child_map", {}).items():
        parent_name = readable(parent)
        if parent_name is None:
            continue
        child_names = [name for name in (readable(c) for c in children) if name]
        graph.setdefault(parent_name, [])
        for name in child_names:
            if name not in graph[parent_name]:
                graph[parent_name].append(name)
    return graph


# --------------------------------------------------------------------------
# OpenLineage
# --------------------------------------------------------------------------


def to_openlineage_events(
    graph: dict[str, list[str]],
    *,
    job_namespace: str = "data-reliability-lab",
    producer: str = "https://github.com/vinai-lab/track2-lab27",
    run_id: str = "00000000-0000-0000-0000-000000000000",
    event_time: str | None = None,
) -> list[dict[str, Any]]:
    """Emit OpenLineage ``COMPLETE`` RunEvents describing the dataset graph.

    One event per producing job (``<parent> -> <children>``), which is the shape
    Marquez and other OpenLineage backends expect. Emitting the file rather than
    POSTing keeps the lab offline and free.
    """
    event_time = event_time or datetime.now(timezone.utc).isoformat()
    events: list[dict[str, Any]] = []
    for parent, children in graph.items():
        if not children:
            continue
        events.append(
            {
                "eventType": "COMPLETE",
                "eventTime": event_time,
                "producer": producer,
                "schemaURL": (
                    "https://openlineage.io/spec/1-0-5/OpenLineage.json"
                    "#/definitions/RunEvent"
                ),
                "run": {"runId": run_id},
                "job": {"namespace": job_namespace, "name": f"build.{parent}"},
                "inputs": [{"namespace": job_namespace, "name": parent}],
                "outputs": [
                    {"namespace": job_namespace, "name": child} for child in children
                ],
            }
        )
    return events


def write_openlineage_events(
    graph: dict[str, list[str]], path: str | Path, **kwargs: Any
) -> Path:
    """Write the OpenLineage events as newline-delimited JSON."""
    events = to_openlineage_events(graph, **kwargs)
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        for event in events:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    return out
