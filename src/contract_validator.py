"""Contract validator for the Data Reliability Game Day lab.

Extends the starter baseline with:
- declared type validation (integer / number / string / datetime / boolean),
  including detection of *silent* type drift that ``pd.to_numeric(errors='coerce')``
  would otherwise hide,
- contract-level freshness validation driven by ``contract['freshness']``,
- severity-aware action routing (``block`` / ``quarantine`` / ``warn``),
- automatic quarantine of offending rows into a side table.

Every issue keeps the starter shape (``check``, ``column``, ``severity``,
``passed``, ``details``) so the stable ``student_api`` contract is preserved, and
adds observability metadata (``action``, ``failed_rows``, ``row_count``).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

# --------------------------------------------------------------------------
# severity / action policy
# --------------------------------------------------------------------------

SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2}

#: How the pipeline must react to a *failed* check of a given severity.
#: critical  -> stop the pipeline, nothing propagates downstream
#: warning   -> keep the good rows, push the offending rows to a side table
#: info      -> log only
SEVERITY_ACTION = {"critical": "block", "warning": "quarantine", "info": "warn"}


def action_for_severity(severity: str) -> str:
    return SEVERITY_ACTION.get(str(severity).lower(), "warn")


def _issue(
    check: str,
    *,
    column: str | None,
    severity: str,
    passed: bool,
    details: str,
    failed_rows: list[int] | None = None,
) -> dict[str, Any]:
    severity = str(severity).lower()
    issue: dict[str, Any] = {
        "check": check,
        "column": column,
        "severity": severity,
        "passed": bool(passed),
        "details": details,
        # Only a *failed* check triggers an action; a passing check is a no-op.
        "action": action_for_severity(severity) if not passed else "none",
    }
    if failed_rows is not None:
        issue["failed_rows"] = [int(i) for i in failed_rows]
    return issue


def load_contract(path: str | Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _column_rules(contract: dict[str, Any]) -> dict[str, Any]:
    """Support both ``columns:`` (orders contract) and ``fields:`` (kb contract)."""
    rules = contract.get("columns")
    if rules is None:
        rules = contract.get("fields", {})
    return rules or {}


# --------------------------------------------------------------------------
# type validation
# --------------------------------------------------------------------------

_BOOL_LITERALS = {"true", "false", "1", "0", "yes", "no", "t", "f", "y", "n"}


def _type_violation_mask(series: pd.Series, declared: str) -> pd.Series:
    """Boolean mask of values that do NOT satisfy the declared contract type.

    Nulls are never a type violation - that is what the ``not_null`` check is
    for; reporting the same row twice makes triage noisier, not better.
    """
    declared = str(declared).lower()
    notna = series.notna()

    if declared in {"integer", "int", "bigint", "long"}:
        coerced = pd.to_numeric(series, errors="coerce")
        # Non-numeric strings AND floats with a fractional part are both type
        # drift for an integer column; coerce alone would hide the first.
        bad = notna & coerced.isna()
        fractional = notna & coerced.notna() & (coerced != coerced.round())
        return bad | fractional.fillna(False)

    if declared in {"number", "float", "double", "decimal", "numeric"}:
        coerced = pd.to_numeric(series, errors="coerce")
        return notna & coerced.isna()

    if declared in {"datetime", "timestamp", "date"}:
        coerced = _to_datetime(series)
        return notna & coerced.isna()

    if declared in {"boolean", "bool"}:
        def _is_bool(value: Any) -> bool:
            if isinstance(value, bool):
                return True
            return str(value).strip().lower() in _BOOL_LITERALS

        return notna & ~series.map(_is_bool)

    if declared in {"string", "str", "varchar", "text"}:
        # A single numeric-looking id is normal in a string column, so only
        # non-scalar payloads count as drift here.
        return notna & series.map(lambda v: isinstance(v, (list, dict, set, tuple)))

    # Unknown declared type: nothing we can assert deterministically.
    return pd.Series(False, index=series.index)


def _to_datetime(series: pd.Series) -> pd.Series:
    """Parse timestamps tolerantly across pandas versions."""
    try:
        return pd.to_datetime(series, utc=True, errors="coerce", format="mixed")
    except (ValueError, TypeError):
        return pd.to_datetime(series, utc=True, errors="coerce")


# --------------------------------------------------------------------------
# freshness validation
# --------------------------------------------------------------------------


#: When no pipeline run time is supplied, a batch whose newest record predates
#: this many hours is treated as a historical replay/backfill rather than a live
#: load, and the freshness SLA is reported as skipped instead of breached.
#: Freshness compares data time to *run* time, so replaying last month's batch
#: must not page anybody. Override per contract with ``freshness.replay_guard_hours``.
DEFAULT_REPLAY_GUARD_HOURS = 24.0


def validate_freshness(
    df: pd.DataFrame,
    contract: dict[str, Any],
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Check ``max(freshness.column)`` against ``freshness.max_delay_minutes``."""
    spec = contract.get("freshness")
    if not spec:
        return []

    column = spec.get("column")
    max_delay = spec.get("max_delay_minutes")
    severity = str(spec.get("severity", "warning")).lower()
    if column is None or max_delay is None:
        return []

    if column not in df.columns:
        return [
            _issue(
                "freshness",
                column=column,
                severity=severity,
                passed=False,
                details=f"freshness column '{column}' missing from dataset",
            )
        ]

    parsed = _to_datetime(df[column])
    if not parsed.notna().any():
        return [
            _issue(
                "freshness",
                column=column,
                severity=severity,
                passed=False,
                details=f"no parseable timestamps in '{column}'",
            )
        ]

    supplied_run_time = now is not None
    now_ts = pd.Timestamp(now or datetime.now(timezone.utc))
    if now_ts.tzinfo is None:
        now_ts = now_ts.tz_localize("UTC")
    latest = parsed.max()
    lag_minutes = float((now_ts - latest).total_seconds() / 60.0)

    guard_hours = float(spec.get("replay_guard_hours", DEFAULT_REPLAY_GUARD_HOURS))
    is_replay = (not supplied_run_time) and lag_minutes > guard_hours * 60.0

    if is_replay:
        issue = _issue(
            "freshness",
            column=column,
            severity="info",
            passed=True,
            details=(
                f"skipped: no run time supplied and batch is {lag_minutes / 60.0:.1f}h old "
                f"(> replay_guard_hours={guard_hours}), treated as historical replay"
            ),
        )
        issue["skipped"] = True
    else:
        issue = _issue(
            "freshness",
            column=column,
            severity=severity,
            passed=lag_minutes <= float(max_delay),
            details=(
                f"lag_minutes={lag_minutes:.1f}; max_delay_minutes={max_delay}; "
                f"latest={latest.isoformat()}"
            ),
        )
        issue["skipped"] = False

    issue["lag_minutes"] = lag_minutes
    issue["max_delay_minutes"] = float(max_delay)
    return [issue]


# --------------------------------------------------------------------------
# main entry point
# --------------------------------------------------------------------------


def validate_dataframe(
    df: pd.DataFrame,
    contract: dict[str, Any],
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    columns = _column_rules(contract)

    for column, rules in columns.items():
        rules = rules or {}
        severity = str(rules.get("severity", "warning")).lower()
        required = bool(rules.get("required", False))

        if column not in df.columns:
            if required:
                issues.append(
                    _issue(
                        "required_column",
                        column=column,
                        severity=severity,
                        passed=False,
                        details=f"Missing required column: {column}",
                    )
                )
            continue

        series = df[column]

        if required:
            null_mask = series.isna()
            null_count = int(null_mask.sum())
            issues.append(
                _issue(
                    "not_null",
                    column=column,
                    severity=severity,
                    passed=(null_count == 0),
                    details=f"null_count={null_count}",
                    failed_rows=list(series.index[null_mask]),
                )
            )

        declared_type = rules.get("type")
        if declared_type:
            bad_mask = _type_violation_mask(series, declared_type)
            bad_count = int(bad_mask.sum())
            sample = series[bad_mask].head(3).tolist()
            issues.append(
                _issue(
                    "type",
                    column=column,
                    severity=severity,
                    passed=(bad_count == 0),
                    details=(
                        f"declared={declared_type}; invalid_count={bad_count}"
                        + (f"; sample={sample}" if sample else "")
                    ),
                    failed_rows=list(series.index[bad_mask]),
                )
            )

        if rules.get("unique"):
            dup_mask = series.duplicated(keep=False)
            duplicate_count = int(dup_mask.sum())
            issues.append(
                _issue(
                    "unique",
                    column=column,
                    severity=severity,
                    passed=(duplicate_count == 0),
                    details=f"duplicate_rows={duplicate_count}",
                    failed_rows=list(series.index[dup_mask]),
                )
            )

        accepted = rules.get("accepted_values")
        if accepted is not None:
            invalid_mask = series.notna() & ~series.isin(accepted)
            invalid_count = int(invalid_mask.sum())
            issues.append(
                _issue(
                    "accepted_values",
                    column=column,
                    severity=severity,
                    passed=(invalid_count == 0),
                    details=f"invalid_count={invalid_count}; accepted={accepted}",
                    failed_rows=list(series.index[invalid_mask]),
                )
            )

        if "min" in rules or "max" in rules:
            numeric = pd.to_numeric(series, errors="coerce")
            invalid = pd.Series(False, index=series.index)
            if "min" in rules:
                invalid |= numeric < rules["min"]
            if "max" in rules:
                invalid |= numeric > rules["max"]
            invalid = invalid.fillna(False)
            invalid_count = int(invalid.sum())
            issues.append(
                _issue(
                    "range",
                    column=column,
                    severity=severity,
                    passed=(invalid_count == 0),
                    details=f"invalid_count={invalid_count}",
                    failed_rows=list(series.index[invalid]),
                )
            )

        min_length = rules.get("min_length")
        if min_length is not None:
            lengths = series.astype("string").str.len()
            short_mask = series.notna() & (lengths < int(min_length))
            short_mask = short_mask.fillna(False)
            short_count = int(short_mask.sum())
            issues.append(
                _issue(
                    "min_length",
                    column=column,
                    severity=severity,
                    passed=(short_count == 0),
                    details=f"too_short={short_count}; min_length={min_length}",
                    failed_rows=list(series.index[short_mask]),
                )
            )

    issues.extend(validate_freshness(df, contract, now=now))

    row_count = int(len(df))
    for issue in issues:
        issue["row_count"] = row_count
    return issues


# --------------------------------------------------------------------------
# triage helpers
# --------------------------------------------------------------------------


def failed_issues(
    issues: list[dict[str, Any]], min_severity: str | None = None
) -> list[dict[str, Any]]:
    failed = [i for i in issues if not i.get("passed", False)]
    if min_severity is None:
        return failed
    threshold = SEVERITY_ORDER[min_severity]
    return [
        i for i in failed if SEVERITY_ORDER.get(i.get("severity", "warning"), 1) >= threshold
    ]


def decide_action(issues: list[dict[str, Any]]) -> str:
    """Collapse all failed checks into one pipeline action.

    ``block`` wins over ``quarantine`` wins over ``warn`` wins over ``pass``.
    """
    failed = failed_issues(issues)
    if not failed:
        return "pass"
    actions = {
        i.get("action", action_for_severity(i.get("severity", "warning"))) for i in failed
    }
    for action in ("block", "quarantine", "warn"):
        if action in actions:
            return action
    return "warn"


def quarantine_rows(issues: list[dict[str, Any]]) -> list[int]:
    """Row indexes that any failed row-level check pointed at."""
    bad: set[int] = set()
    for issue in failed_issues(issues):
        bad.update(issue.get("failed_rows", []))
    return sorted(bad)


def split_quarantine(
    df: pd.DataFrame, issues: list[dict[str, Any]]
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return ``(clean_df, quarantined_df)`` based on failed row-level checks."""
    bad = [i for i in quarantine_rows(issues) if i in df.index]
    return df.drop(index=bad), df.loc[bad]


def enforce_contract(
    df: pd.DataFrame,
    contract: dict[str, Any],
    *,
    quarantine_path: str | Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate, decide an action, and write the quarantine side table.

    This is the piece that turns a passing *pipeline* into a passing *dataset*:
    the run is only clean when the contract itself is satisfied.
    """
    issues = validate_dataframe(df, contract, now=now)
    action = decide_action(issues)
    clean, quarantined = split_quarantine(df, issues)

    written: str | None = None
    if quarantine_path is not None and not quarantined.empty:
        path = Path(quarantine_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        quarantined.to_csv(path, index=False)
        written = str(path)

    return {
        "dataset": contract.get("dataset"),
        "action": action,
        "passed": action == "pass",
        "issues": issues,
        "failed": failed_issues(issues),
        "critical_failed": failed_issues(issues, min_severity="critical"),
        "clean_rows": int(len(clean)),
        "quarantined_rows": int(len(quarantined)),
        "quarantine_path": written,
        "clean_df": clean,
        "quarantined_df": quarantined,
    }
