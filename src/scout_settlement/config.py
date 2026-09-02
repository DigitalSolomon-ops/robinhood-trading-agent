"""Config for the settlement sweep. Reuses the entry-alerts bucket resolution
(the settlement engine reads/writes the SAME day-state store the scouts write),
and adds its own horizon + lookback knobs. ANALYSIS ONLY."""

from __future__ import annotations

import os

# Re-export so the engine has one import site; the bucket is shared with the scouts.
from ..entry_alerts.config import load_alerts_config, resolve_bucket  # noqa: F401

# How many trading sessions AFTER the report date a play may take to hit target
# or stop before it is scored OPEN. 10 sessions ~= two calendar weeks.
DEFAULT_HORIZON_DAYS = 10

# How many recent report days to sweep each run. Must comfortably exceed the
# horizon so an OPEN play keeps being re-checked until it resolves or ages out.
DEFAULT_LOOKBACK_DAYS = 21


def resolve_horizon_days() -> int:
    try:
        return max(int(os.getenv("SCOUT_SETTLEMENT_HORIZON_DAYS", str(DEFAULT_HORIZON_DAYS))), 1)
    except (TypeError, ValueError):
        return DEFAULT_HORIZON_DAYS


def resolve_lookback_days() -> int:
    try:
        return max(int(os.getenv("SCOUT_SETTLEMENT_LOOKBACK_DAYS", str(DEFAULT_LOOKBACK_DAYS))), 1)
    except (TypeError, ValueError):
        return DEFAULT_LOOKBACK_DAYS


def resolve_min_interval() -> float:
    """Seconds between Massive requests (0 on the paid tier; set for free-tier
    rate limits). Mirrors the scouts' MASSIVE_MIN_INTERVAL_SECONDS knob."""
    try:
        return max(float(os.getenv("MASSIVE_MIN_INTERVAL_SECONDS", "0")), 0.0)
    except (TypeError, ValueError):
        return 0.0
