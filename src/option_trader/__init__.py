"""Options TRADER runtime: the arm-gated, paper-first per-invocation trade cycle.

This package is deliberately distinct from ``option_runtime`` (the proving
harness). Its one job is to add the ARM GATE the paper loop does not apply on its
own: ``run_option_cycle`` reads the SHARED arm state the 007 dashboard toggle
writes (``FirestoreArmStore`` in the cloud, selected by
``TRADER_ARM_FIRESTORE_PROJECT``) and, when the options lane is DISARMED -- the
fail-closed default, and the state a fresh stand-up is in -- does nothing.

Consequences, by construction:
  * Standing this runtime up places ZERO orders until the operator arms.
  * Even ARMED, it runs PAPER cycles only. Going live is a separate, deliberate
    decision the arm toggle never makes, and the only connector this runtime can
    build is the headless paper-proving connector whose order methods RAISE -- so
    a live options order is not reachable from here even in principle.
"""
from __future__ import annotations

from .cycle import run_option_cycle

__all__ = ["run_option_cycle"]
