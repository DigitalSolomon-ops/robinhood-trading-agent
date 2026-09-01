"""One arm-gated, paper-first options trade cycle.

This is the seam between the 007 dashboard's arm toggle and the options paper
loop. ``run_option_paper_loop`` (option_runtime) is paper by construction but does
NOT consult the arm state -- it would run cycles regardless of the toggle.
``run_option_cycle`` puts the arm gate in front of it, reading the SAME shared arm
store the dashboard writes (``FirestoreArmStore`` in the cloud):

  * DISARMED -- the fail-closed default, and the state a fresh stand-up is in --
    the cycle is a NO-OP and places zero orders. Standing the runtime up changes
    nothing until the operator flips the toggle.
  * ARMED -- exactly ONE bounded PAPER cycle runs (read analysis-only scout plays
    -> defined-risk candidates -> simulated PaperBroker fills). The only connector
    the cycle can build is the headless ``PaperProvingOptionConnector``, whose
    order methods RAISE, so no real order is reachable even in principle.

PAPER-FIRST beyond arming: a non-paper posture requires ``OPTIONS_TRADER_LIVE``
to equal ``"1"`` EXACTLY -- a switch the arm toggle never writes and this
deployment never sets. Even were it set, the headless connector cannot place, so
the cloud runtime is paper-only; a real live options order stays an in-session,
agent-hosted human action.
"""
from __future__ import annotations

import logging
import os
from datetime import date
from pathlib import Path
from typing import Any

from ..option_runtime import (
    build_option_paper_proving_connector,
    load_option_settings,
    option_kill_switch,
    reconcile_option_paper,
    run_option_paper_loop,
)
from ..shared_state import build_arm_store
from .scout_play_source import ScoutPlaySource

_LOG = logging.getLogger(__name__)

LANE = "options"


def live_requested() -> bool:
    """Paper-first: a non-paper posture requires ``OPTIONS_TRADER_LIVE == '1'``
    EXACTLY. The arm toggle never writes this and the deployment never sets it; it
    exists only so 'is this paper?' is an explicit, auditable read rather than an
    accident of some truthy value. Even when true, the cloud runtime is still
    paper -- its only connector cannot place."""
    return os.getenv("OPTIONS_TRADER_LIVE", "") == "1"


def run_option_cycle(
    root: Path,
    *,
    play_source: Any | None = None,
    arm_store: Any | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Run at most ONE options trade cycle, gated on the shared arm state.

    Returns a structured summary for the execution log. An ordinary
    disarmed / halted / empty cycle is a clean no-op -- it returns normally (the
    entrypoint exits 0); only an unexpected internal error propagates.
    """
    root = Path(root)
    rules = load_option_settings(root)  # also loads .env (dotenv, override=False)
    store = arm_store if arm_store is not None else build_arm_store(root, rules)

    # The cloud runtime is paper regardless: the connector cannot place a live
    # order and the deployment never sets OPTIONS_TRADER_LIVE. The read is here so
    # the posture is recorded explicitly, never inferred.
    mode = "live-requested-but-paper-only" if live_requested() else "paper"

    # (1) FAIL-CLOSED ARM GATE -- the seam the paper loop does not apply itself.
    # A missing / unreadable arm doc reads DISARMED, so a fresh stand-up no-ops.
    if not store.is_armed(LANE):
        _LOG.info("options lane DISARMED -> no-op cycle, zero orders")
        return {
            "lane": LANE,
            "armed": False,
            "mode": mode,
            "cycles": 0,
            "fills": 0,
            "status": "disarmed_noop",
        }

    # (2) KILL SWITCH -- an armed lane can still be halted (its STOP_TRADING_OPTIONS
    # file or TRADING_ENABLED=false). The loop re-checks this too; checking here
    # gives a clean top-level halt record and avoids building the play source.
    kill = option_kill_switch(rules, root)
    halt_reasons = kill.halt_reasons()
    if halt_reasons:
        _LOG.info("options lane ARMED but HALTED by kill switch: %s", "; ".join(halt_reasons))
        return {
            "lane": LANE,
            "armed": True,
            "mode": mode,
            "cycles": 0,
            "fills": 0,
            "status": "halted_noop",
            "halt_reasons": list(halt_reasons),
        }

    # (3) ARMED + not halted -> exactly ONE PAPER cycle. The connector is the
    # headless paper-proving connector: its order methods raise, so the cycle
    # cannot reach a live submit even if some caller tried to.
    source = play_source if play_source is not None else ScoutPlaySource()
    connector = build_option_paper_proving_connector(root)
    loop = run_option_paper_loop(
        connector,
        root,
        source,
        iterations=1,
        arm_store=store,
        today=today,
    )
    reconcile = reconcile_option_paper(root)

    return {
        "lane": LANE,
        "armed": True,
        "mode": mode,
        "cycles": int(loop.get("iterations_completed", 0) or 0),
        "fills": int(loop.get("fills", 0) or 0),
        "halted": bool(loop.get("halted", False)),
        "basis": loop.get("quote_basis") or getattr(source, "basis_name", "unknown"),
        "reconcile_errors": list(reconcile.get("errors", [])),
        "status": "paper_cycle",
    }
