"""Incident-decision dashboard for the Data Reliability Game Day.

Every element answers a question an on-call engineer actually asks:
is this batch trustworthy, who is affected, and do I need to act now?
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "reports" / "latest_metrics.json"
HISTORY = ROOT / "data" / "history" / "metrics_history.csv"

ACTION_STYLE = {
    "block": ("red", ":material/block:", "Pipeline stopped. Nothing propagated downstream."),
    "quarantine": ("orange", ":material/inventory_2:", "Bad rows isolated; clean rows continue."),
    "warn": ("orange", ":material/warning:", "Logged only; data continues."),
    "pass": ("green", ":material/check_circle:", "All contract checks passed."),
}

st.set_page_config(page_title="Data reliability", layout="wide")
st.title(":material/monitoring: Data reliability game day")


@st.cache_data(ttl="60s")
def load_history() -> pd.DataFrame:
    return pd.read_csv(HISTORY)


if not REPORT.exists():
    st.warning("Run `make baseline` first to generate `reports/latest_metrics.json`.")
    st.stop()

report = json.loads(REPORT.read_text(encoding="utf-8"))
history = load_history()

row_anomaly = report["row_count_anomaly"]
kb_fresh = report["kb_freshness"]
burn = report["multiwindow_burn"]
action = report["contract_action"]
critical = report["critical_contract_failures"]

# --------------------------------------------------------------------------
# Incident status - the single most important line on the page
# --------------------------------------------------------------------------
healthy = (
    action == "pass"
    and not row_anomaly["is_anomaly"]
    and not kb_fresh["is_anomaly"]
    and critical == 0
)

if healthy:
    st.success(
        "**Batch is trustworthy.** Contracts pass, volume is in range, and the "
        "knowledge base is fresh.",
        icon=":material/check_circle:",
    )
else:
    reasons = []
    if critical:
        reasons.append(f"{critical} critical contract failure(s)")
    if row_anomaly["is_anomaly"]:
        reasons.append(
            f"row count {report['orders_rows']} vs expected "
            f"~{row_anomaly.get('baseline_median', float('nan')):.0f}"
        )
    if kb_fresh["is_anomaly"]:
        reasons.append(f"knowledge base {kb_fresh['age_minutes']:.0f} min stale")
    st.error(
        f"**Do not trust this batch.** {'; '.join(reasons)}.",
        icon=":material/error:",
    )

st.caption(f"Last run: {report['timestamp']}")

# --------------------------------------------------------------------------
# KPI row
# --------------------------------------------------------------------------
recent_rows = history["row_count"].tail(14).tolist()
expected = row_anomaly.get("baseline_median")
delta = (
    f"{report['orders_rows'] - expected:+.0f} vs same-weekday median"
    if expected
    else None
)

with st.container(horizontal=True):
    st.metric(
        "Orders rows",
        f"{report['orders_rows']:,}",
        delta,
        border=True,
        chart_data=recent_rows,
        chart_type="line",
    )
    st.metric(
        "Orders freshness",
        f"{report['freshness_minutes']:.1f} min",
        "SLA 30 min",
        delta_color="off",
        border=True,
    )
    st.metric(
        "KB freshness",
        f"{kb_fresh['age_minutes']:.0f} min",
        "SLA 60 min",
        delta_color="off",
        border=True,
    )
    st.metric(
        "Critical contract failures",
        critical,
        border=True,
    )

# --------------------------------------------------------------------------
# Contract action + SLO budget
# --------------------------------------------------------------------------
left, right = st.columns(2)

with left:
    with st.container(border=True):
        st.subheader("Contract decision")
        color, icon, explanation = ACTION_STYLE.get(action, ("gray", "", ""))
        st.markdown(f"### {icon} :{color}[{action.upper()}]")
        st.caption(explanation)

        failures = report.get("failed_checks_detail", [])
        if failures:
            st.dataframe(
                pd.DataFrame(failures)[["severity", "check", "column", "details"]],
                hide_index=True,
                width="stretch",
            )
        else:
            st.caption("No failed checks.")

        if report.get("quarantined_rows"):
            st.warning(
                f"{report['quarantined_rows']} row(s) quarantined to "
                f"`{report['quarantine_path']}`",
                icon=":material/inventory_2:",
            )

with right:
    with st.container(border=True):
        st.subheader("Error budget")
        slo_rows = [
            {
                "SLI": name,
                "target": f"{slo['target']:.3%}",
                "burn rate": round(slo["burn_rate"], 1),
                "budget left": f"{slo['remaining_error_budget_fraction']:.0%}",
                "breached": slo["breached"],
            }
            for name, slo in report["slos"].items()
        ]
        st.dataframe(pd.DataFrame(slo_rows), hide_index=True, width="stretch")

        page_color = "red" if burn["page"] else "green"
        st.markdown(
            f"**Paging policy:** :{page_color}[{'PAGE' if burn['page'] else 'no page'}] "
            f"— tier `{burn['tier']}`"
        )
        st.caption(burn["reason"])

# --------------------------------------------------------------------------
# Detection signals
# --------------------------------------------------------------------------
with st.container(border=True):
    st.subheader("Detection signals")
    st.caption(
        "Each layer sees a different failure class. A green row does not mean the "
        "data is fine — it means *this* detector cannot see the problem."
    )
    signals = [
        {
            "layer": "Contract (deterministic)",
            "verdict": "ALERT" if critical else "ok",
            "detail": f"{report['failed_contract_checks']} failed check(s), action={action}",
        },
        {
            "layer": "Volume anomaly (same-weekday MAD)",
            "verdict": "ALERT" if row_anomaly["is_anomaly"] else "ok",
            "detail": f"score={row_anomaly['score']:.2f}, {row_anomaly['method']}",
        },
        {
            "layer": "Amount distribution (KS + PSI)",
            "verdict": "ALERT" if report["amount_distribution"]["is_anomaly"] else "ok",
            "detail": f"signals={report['amount_distribution']['signals_fired'] or 'none'}",
        },
        {
            "layer": "KB freshness",
            "verdict": "ALERT" if kb_fresh["is_anomaly"] else "ok",
            "detail": kb_fresh["reason"],
        },
        {
            "layer": "KB text length (RAG)",
            "verdict": "ALERT" if report["kb_text_length_signal"]["is_anomaly"] else "ok",
            "detail": f"current_mean={report['kb_text_length_signal']['current_mean']:.1f}",
        },
    ]
    st.dataframe(pd.DataFrame(signals), hide_index=True, width="stretch")

# --------------------------------------------------------------------------
# Blast radius + history
# --------------------------------------------------------------------------
left, right = st.columns(2)

with left:
    with st.container(border=True):
        st.subheader("Blast radius")
        impacts = report.get("blast_radius") or []
        if not impacts:
            st.caption("No upstream asset is currently degraded.")
        for impact in impacts:
            st.markdown(f"**Root: `{impact['root']}`**")
            st.markdown(
                "```text\n"
                + f"{impact['root']}\n"
                + "\n".join(
                    f"{'  ' * (i + 1)}└── {asset}"
                    for i, asset in enumerate(impact["affected_datasets"])
                )
                + "\n```"
            )
            if impact["affected_columns"]:
                st.caption("Columns: " + ", ".join(f"`{c}`" for c in impact["affected_columns"]))
            if impact["critical_assets_hit"]:
                st.error(
                    "Business-critical assets affected: "
                    + ", ".join(impact["critical_assets_hit"]),
                    icon=":material/priority_high:",
                )

with right:
    with st.container(border=True):
        st.subheader("Row count history")
        chart_data = history.copy()
        chart_data["segment"] = chart_data["day_of_week"].map(
            lambda d: "weekend" if d >= 5 else "weekday"
        )
        st.caption(
            "Weekly seasonality is why a pooled baseline fails: weekdays run ~600 "
            "orders, weekends ~250."
        )
        st.line_chart(chart_data.set_index("date")[["row_count"]], height=260)

with st.expander("Runbook", icon=":material/menu_book:"):
    st.markdown(
        """
| Action | Command |
|---|---|
| Reset to a healthy batch | `make reset` |
| Re-run detection | `make baseline` |
| Rebuild models with tests | `make dbt` |
| Localise what changed | `python scripts/triage.py --dataset orders` |
| Detector evidence (old vs new) | `python scripts/evidence.py` |

Owners — `orders`: commerce-data · `kb_documents`: support-ai
        """
    )
