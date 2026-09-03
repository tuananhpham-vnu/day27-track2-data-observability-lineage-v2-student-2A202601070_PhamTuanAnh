"""SLO, error budget, and multi-window burn-rate alerting.

``calculate_slo`` is unchanged in behaviour. ``evaluate_multiwindow_burn``
implements the Google SRE Workbook multi-window / multi-burn-rate policy so a
transient spike does not page while a sustained fast burn does.
"""
from __future__ import annotations

from typing import Any

#: Google SRE Workbook alerting tiers, most urgent first.
#: ``burn`` is the burn-rate threshold both windows must clear together;
#: ``budget_consumed`` documents how much of a 30-day budget that rate spends
#: over the long window, which is what makes the tier worth its severity.
BURN_TIERS: tuple[dict[str, Any], ...] = (
    {
        "name": "fast_burn",
        "burn": 14.4,
        "severity": "critical",
        "page": True,
        "budget_consumed": "2% of a 30-day budget in 1 hour",
    },
    {
        "name": "medium_burn",
        "burn": 6.0,
        "severity": "critical",
        "page": True,
        "budget_consumed": "5% of a 30-day budget in 6 hours",
    },
    {
        "name": "slow_burn",
        "burn": 3.0,
        "severity": "warning",
        "page": False,
        "budget_consumed": "10% of a 30-day budget in 1-3 days",
    },
)


def calculate_slo(target: float, bad_events: int, total_events: int) -> dict[str, Any]:
    if not 0 < target < 1:
        raise ValueError("target must be between 0 and 1 (exclusive)")
    if bad_events < 0 or total_events < 0 or bad_events > total_events:
        raise ValueError("invalid event counts")
    allowed_bad_rate = 1.0 - target
    if total_events == 0:
        return {
            "target": target,
            "actual_bad_rate": 0.0,
            "allowed_bad_rate": allowed_bad_rate,
            "burn_rate": 0.0,
            "remaining_error_budget_fraction": 1.0,
            "breached": False,
        }
    actual_bad_rate = bad_events / total_events
    burn_rate = actual_bad_rate / allowed_bad_rate
    consumed_fraction = min(1.0, actual_bad_rate / allowed_bad_rate)
    return {
        "target": target,
        "actual_bad_rate": actual_bad_rate,
        "allowed_bad_rate": allowed_bad_rate,
        "burn_rate": burn_rate,
        "remaining_error_budget_fraction": max(0.0, 1.0 - consumed_fraction),
        "breached": bool(actual_bad_rate > allowed_bad_rate),
    }


def evaluate_multiwindow_burn(
    *,
    short_window_burn: float,
    long_window_burn: float,
    policy: str = "multiwindow",
) -> dict[str, Any]:
    """Decide whether a burn rate deserves a page.

    The rule is deliberately a **conjunction**: a tier fires only when the short
    *and* the long window are both burning above its threshold.

    - The **long** window proves the problem is sustained, so a 5-minute blip
      that is already over cannot page anyone.
    - The **short** window proves the problem is still happening, so an incident
      that recovered an hour ago stops paging instead of ringing until the long
      window finally rolls off (the classic "slow reset" of a single-window
      alert).

    A burn rate of 1.0 means the budget is being spent exactly as fast as it is
    granted; anything above 1.0 but below the slow-burn tier is a ticket, not a
    page, and anything at or below 1.0 is healthy.
    """
    short = float(short_window_burn)
    long = float(long_window_burn)
    if short < 0 or long < 0:
        raise ValueError("burn rates must be non-negative")

    for tier in BURN_TIERS:
        if short >= tier["burn"] and long >= tier["burn"]:
            return {
                "page": bool(tier["page"]),
                "severity": tier["severity"],
                "tier": tier["name"],
                "reason": (
                    f"sustained burn: short={short:.2f} and long={long:.2f} both "
                    f">= {tier['burn']} ({tier['budget_consumed']})"
                ),
                "short_window_burn": short,
                "long_window_burn": long,
                "policy": policy,
            }

    # Short window hot, long window cold => the spike has not lasted long enough
    # to matter. This is exactly the case the starter policy could not express.
    if short >= BURN_TIERS[-1]["burn"] > long:
        return {
            "page": False,
            "severity": "warning",
            "tier": "transient_spike",
            "reason": (
                f"transient spike: short={short:.2f} is burning fast but "
                f"long={long:.2f} is below {BURN_TIERS[-1]['burn']}; "
                "not yet sustained, do not page"
            ),
            "short_window_burn": short,
            "long_window_burn": long,
            "policy": policy,
        }

    # Long window hot, short window cold => the incident is over; the budget
    # damage is real and worth a ticket, but waking someone fixes nothing.
    if long >= BURN_TIERS[-1]["burn"] > short:
        return {
            "page": False,
            "severity": "warning",
            "tier": "recovering",
            "reason": (
                f"recovering: long={long:.2f} still reflects spent budget but "
                f"short={short:.2f} has returned below {BURN_TIERS[-1]['burn']}"
            ),
            "short_window_burn": short,
            "long_window_burn": long,
            "policy": policy,
        }

    if max(short, long) > 1.0:
        return {
            "page": False,
            "severity": "info",
            "tier": "elevated",
            "reason": (
                f"elevated but sub-alert burn: short={short:.2f}, long={long:.2f}; "
                "budget is shrinking faster than granted - track, do not page"
            ),
            "short_window_burn": short,
            "long_window_burn": long,
            "policy": policy,
        }

    return {
        "page": False,
        "severity": "info",
        "tier": "healthy",
        "reason": f"burn within budget: short={short:.2f}, long={long:.2f}",
        "short_window_burn": short,
        "long_window_burn": long,
        "policy": policy,
    }
