"""Scout calibration: compare what the scout FORECAST (conviction, backtested
hit-rate) against what the settlement engine REALIZED (WIN/LOSS), so the operator
can see whether the scout's confidence is trustworthy and where it drifts.

ANALYSIS ONLY. Read-only aggregation over settled plays; no order path, and (in
this phase) it only SURFACES calibration -- it does not change scout weights.
"""
