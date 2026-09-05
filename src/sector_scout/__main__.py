"""Container / cron entrypoint for the Sector Scout daily email.

    python -m src.sector_scout                 # send using config
    SECTOR_SCOUT_DRY_RUN=1 python -m ...       # compose + print, do not send
    SECTOR_SCOUT_TOP=4 python -m ...           # override max plays

ANALYSIS ONLY. This imports the scout and nothing from the trading lanes.
Secrets come from the environment (MASSIVE_API_KEY, GMAIL_APP_PASSWORD) that
Cloud Run injects from Secret Manager; no gcloud/ADC is used in-container
(DS_VAULT_NO_GCLOUD=1).
"""
from __future__ import annotations

import os
import sys

from . import run_sector_scout_email


def main() -> int:
    top = os.getenv("SECTOR_SCOUT_TOP")
    dry = os.getenv("SECTOR_SCOUT_DRY_RUN") == "1"
    result = run_sector_scout_email(
        dry_run=dry,
        top_n=int(top) if top and top.isdigit() else None,
        out_path=None,
    )
    print(getattr(result, "detail", str(result)))
    return 0 if (dry or getattr(result, "sent", False)) else 1


if __name__ == "__main__":
    sys.exit(main())
