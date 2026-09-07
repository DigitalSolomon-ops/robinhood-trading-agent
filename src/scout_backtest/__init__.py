"""Scout backtest: replay the options scout AS-OF past dates (point-in-time),
settle each play it would have emitted against what actually happened, and feed
the results into the same calibration engine the live tab uses.

ANALYSIS ONLY. Read-only historical replay to BOOTSTRAP calibration without
waiting weeks for live data. No order path. The news factor is disabled during a
replay (its live "latest headlines" fetch is not point-in-time), so a backtest
calibrates the core technical + volatility + regime signal.
"""
