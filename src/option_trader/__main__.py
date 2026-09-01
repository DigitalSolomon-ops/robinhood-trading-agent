"""Container / cron entrypoint for ONE options TRADER cycle (Cloud Run Job).

    python -m src.option_trader

Reads the SHARED arm state the 007 dashboard toggle writes (FirestoreArmStore,
selected by TRADER_ARM_FIRESTORE_PROJECT). DISARMED -- the default -- is a no-op
cycle that places zero orders; ARMED runs one bounded PAPER cycle. The container
can build only the headless paper connector (its order methods raise), so it
cannot place a live order even in principle, and no Robinhood credential is
present in this image. Going live stays a separate agent-hosted human action.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

from . import run_option_cycle

# .../src/option_trader/__main__.py -> parents[2] is the app root (holds config/).
ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log = logging.getLogger(__name__)
    try:
        summary = run_option_cycle(ROOT)
    except Exception:  # pragma: no cover - top-level container guard
        log.exception("options trade cycle failed")
        return 1
    # One structured line for the execution log. Emit it via the logging module
    # (reliably captured by Cloud Run on stderr) AND stdout for local runs -- a
    # fast-exiting container can drop an unflushed stdout print, and this summary
    # is the operator's per-cycle monitoring record.
    line = json.dumps(summary, default=str)
    log.info("options trade cycle summary: %s", line)
    print(line, flush=True)
    # A disarmed / halted / paper cycle is a clean success (exit 0). Only an
    # unexpected internal error (caught above) fails the execution. A reconcile
    # error is surfaced in the summary as a data signal; it does not, by itself,
    # fail the container.
    return 0


if __name__ == "__main__":
    sys.exit(main())
