"""Container / scheduler entrypoint for ONE entry-hit poll cycle.

    python -m src.entry_alerts                 # run one cycle, send if a hit fires
    ENTRY_ALERTS_DRY_RUN=1 python -m ...        # compose + print, send nothing, persist nothing
    ENTRY_ALERTS_FORCE=1 python -m ...          # bypass the market-hours guard (manual run)

NOTIFICATION ONLY. This imports the alerter and nothing from the trading lanes.
Secrets come from the environment (MASSIVE_API_KEY, GMAIL_APP_PASSWORD) that
Cloud Run injects from Secret Manager; no gcloud/ADC is used in-container
(DS_VAULT_NO_GCLOUD=1). The shared day-state lives in GCS (ENTRY_ALERTS_BUCKET).

The scheduler runs this every ~10 minutes during market hours; the market-hours
guard makes an off-hours firing a cheap no-op, so an over-broad cron is safe.
"""
from __future__ import annotations

import os
import sys

from .alerter import run_cycle


def main() -> int:
    dry = os.getenv("ENTRY_ALERTS_DRY_RUN") == "1"
    force = os.getenv("ENTRY_ALERTS_FORCE") == "1"
    result = run_cycle(dry_run=dry, force=force)
    print(result.detail or f"cycle complete: {len(result.hits)} hit(s)")
    # A skipped (off-hours) cycle and a dry-run both exit 0 -- they are not
    # failures. A real cycle that found hits but could not send exits non-zero so
    # Cloud Run marks it failed and the operator sees it.
    if dry or result.skipped:
        return 0
    if result.hits and not result.sent:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
