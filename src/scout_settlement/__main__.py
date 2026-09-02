"""Container / scheduler entrypoint for ONE settlement sweep.

    python -m src.scout_settlement                 # settle recent days, write outcomes
    SCOUT_SETTLEMENT_DRY_RUN=1 python -m ...        # compute + report, persist nothing

ANALYSIS ONLY. Reads delayed public price bars (Massive) and writes verdicts to
the shared day-state (GCS). Secrets come from the environment (MASSIVE_API_KEY)
that Cloud Run injects from Secret Manager; no gcloud/ADC in-container
(DS_VAULT_NO_GCLOUD=1). Scheduled once after the close each weekday; running it
off-hours is a cheap idempotent no-op (nothing new to settle).
"""

from __future__ import annotations

import logging
import os
import sys

from .engine import run_settlement

# Log via the logging module (stderr, reliably captured) rather than a bare print,
# which a fast-exiting Cloud Run Job can drop before it flushes.
logging.basicConfig(level=logging.INFO, format="%(message)s")
_log = logging.getLogger("scout_settlement")


def main() -> int:
    dry = os.getenv("SCOUT_SETTLEMENT_DRY_RUN") == "1"
    result = run_settlement(dry_run=dry)
    _log.info(result.detail)
    if result.errors:
        _log.warning("errors (first 10): %s", "; ".join(result.errors[:10]))
    # A data hiccup on some symbols is logged but not a job failure; the sweep is
    # idempotent and the next run retries. Exit 0 so Cloud Run does not thrash.
    return 0


if __name__ == "__main__":
    sys.exit(main())
