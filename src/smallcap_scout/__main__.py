"""Container / cron entrypoint for the Small-Cap Scout daily email.

    python -m src.smallcap_scout                 # send using config top_n
    SMALLCAP_SCOUT_DRY_RUN=1 python -m ...        # compose + print, do not send
    SMALLCAP_SCOUT_TOP=10 python -m ...           # override top_n

ANALYSIS ONLY. This imports the scout and nothing from the trading lanes, so a
lean container needs only httpx + PyYAML. Secrets come from the environment
(MASSIVE_API_KEY, GMAIL_APP_PASSWORD) that Cloud Run injects from Secret Manager;
no gcloud/ADC is used in-container (DS_VAULT_NO_GCLOUD=1).
"""
from __future__ import annotations

import os
import sys

from .runner import run_smallcap_scout_email


def main() -> int:
    top = os.getenv("SMALLCAP_SCOUT_TOP")
    dry = os.getenv("SMALLCAP_SCOUT_DRY_RUN") == "1"
    result = run_smallcap_scout_email(
        dry_run=dry,
        top_n=int(top) if top and top.isdigit() else None,
        out_path=None,
    )
    print(getattr(result, "detail", str(result)))
    # Exit non-zero on a real send that did not go out, so Cloud Run marks the
    # run failed and the operator sees it; a dry-run always exits 0.
    return 0 if (dry or getattr(result, "sent", False)) else 1


if __name__ == "__main__":
    sys.exit(main())
