"""Calibration math: predicted-vs-realized accuracy over settled scout plays.

ANALYSIS ONLY. Pure aggregation (compute_calibration) plus a thin store reader
(load_calibration). SURFACE-ONLY in this phase -- it reports whether the scout's
conviction ranks outcomes correctly and whether its backtested hit-rate matches
reality; it does NOT (yet) auto-tune any weight.

Inputs are the raw play dicts the dashboard already reads (store.report_plays):
each may carry the scout's forecast (`conviction` 0..100, `predicted_hit_rate`
0..1) and, once the settlement engine has run, an `outcome` dict with a WIN/LOSS/
OPEN verdict + return_pct. A play with no WIN/LOSS outcome is not yet settled and
is excluded from realized stats (OPEN ones are counted separately).
"""

from __future__ import annotations

from typing import Any, Sequence

# Conviction bands (half-open [lo, hi)); the last is inclusive of 100 via hi=100.01.
CONVICTION_TIERS: tuple[tuple[float, float, str], ...] = (
    (0.0, 40.0, "0-40 (low)"),
    (40.0, 60.0, "40-60 (medium)"),
    (60.0, 80.0, "60-80 (high)"),
    (80.0, 100.01, "80-100 (very high)"),
)

# Below this many settled plays, a rate is too noisy to flag drift on.
MIN_SAMPLE_FOR_DRIFT = 10
# A |predicted - realized| gap beyond this (with enough sample) is flagged drift.
DRIFT_THRESHOLD = 0.15
# A tier needs at least this many settled plays to enter the monotonicity check.
MIN_TIER_SAMPLE = 3


def _settled_verdict(rec: dict[str, Any]) -> str | None:
    """WIN/LOSS if this play is settled, else None. Reads `outcome` (or the
    legacy `actual`) exactly as store.play_outcome normalizes it."""
    raw = rec.get("outcome")
    if raw is None:
        raw = rec.get("actual")
    if isinstance(raw, dict):
        verdict = str(raw.get("verdict", "")).strip().upper()
        return verdict if verdict in {"WIN", "LOSS"} else None
    if isinstance(raw, str):
        verdict = raw.strip().upper()
        return verdict if verdict in {"WIN", "LOSS"} else None
    return None


def _is_open(rec: dict[str, Any]) -> bool:
    raw = rec.get("outcome")
    return isinstance(raw, dict) and str(raw.get("verdict", "")).strip().upper() == "OPEN"


def _return_pct(rec: dict[str, Any]) -> float | None:
    raw = rec.get("outcome")
    if not isinstance(raw, dict):
        return None
    val = raw.get("return_pct", raw.get("return"))
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def _opt_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _in_tier(conviction: float | None, lo: float, hi: float) -> bool:
    return conviction is not None and lo <= conviction < hi


def _mean(values: Sequence[float]) -> float | None:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def compute_calibration(records: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate settled plays into a calibration report. Never raises; unknown
    or missing fields degrade to None/skips rather than errors."""
    settled: list[tuple[dict[str, Any], str]] = []
    open_count = 0
    for rec in records:
        if not isinstance(rec, dict):
            continue
        verdict = _settled_verdict(rec)
        if verdict is None:
            if _is_open(rec):
                open_count += 1
            continue
        settled.append((rec, verdict))

    n = len(settled)
    wins = sum(1 for _, v in settled if v == "WIN")
    win_rate = (wins / n) if n else None

    predicted = [_opt_float(rec.get("predicted_hit_rate")) for rec, _ in settled]
    avg_predicted = _mean([p for p in predicted if p is not None])
    n_with_prediction = sum(1 for p in predicted if p is not None)
    drift = (
        round(win_rate - avg_predicted, 4)
        if (win_rate is not None and avg_predicted is not None)
        else None
    )
    avg_return = _mean([r for r in (_return_pct(rec) for rec, _ in settled) if r is not None])

    # --- by conviction tier -------------------------------------------------
    tiers: list[dict[str, Any]] = []
    for lo, hi, label in CONVICTION_TIERS:
        members = [(rec, v) for rec, v in settled if _in_tier(_opt_float(rec.get("conviction")), lo, hi)]
        tn = len(members)
        tw = sum(1 for _, v in members if v == "WIN")
        tiers.append(
            {
                "label": label,
                "lo": lo,
                "hi": hi,
                "n": tn,
                "wins": tw,
                "win_rate": (tw / tn) if tn else None,
                "avg_conviction": _mean([_opt_float(rec.get("conviction")) for rec, _ in members]),
                "avg_return_pct": _mean([r for r in (_return_pct(rec) for rec, _ in members) if r is not None]),
            }
        )

    # monotonic: do realized win-rates rise with conviction across well-sampled tiers?
    ranked = [t["win_rate"] for t in tiers if t["n"] >= MIN_TIER_SAMPLE and t["win_rate"] is not None]
    monotonic: bool | None = None
    if len(ranked) >= 2:
        monotonic = all(a <= b + 1e-9 for a, b in zip(ranked, ranked[1:]))

    # --- by source ----------------------------------------------------------
    by_source: dict[str, dict[str, Any]] = {}
    for src in ("options", "smallcap"):
        members = [(rec, v) for rec, v in settled if str(rec.get("source", "")).strip().lower() == src]
        sn = len(members)
        sw = sum(1 for _, v in members if v == "WIN")
        if sn:
            by_source[src] = {"n": sn, "wins": sw, "win_rate": sw / sn}

    drift_flag = bool(
        drift is not None and n_with_prediction >= MIN_SAMPLE_FOR_DRIFT and abs(drift) > DRIFT_THRESHOLD
    )

    return {
        "settled": n,
        "wins": wins,
        "losses": n - wins,
        "open": open_count,
        "realized_win_rate": win_rate,
        "avg_predicted_hit_rate": avg_predicted,
        "n_with_prediction": n_with_prediction,
        "predicted_vs_realized_drift": drift,
        "drift_flag": drift_flag,
        "avg_return_pct": round(avg_return, 2) if avg_return is not None else None,
        "tiers": tiers,
        "by_source": by_source,
        "conviction_monotonic": monotonic,
    }


def load_calibration(*, days: int = 60, bucket: str | None = None) -> dict[str, Any]:
    """Read the recent report days from the shared store and compute calibration
    across all their plays. Thin wrapper so the dashboard has one call site."""
    from ..entry_alerts import store  # local import: keep this module dependency-light

    records: list[dict[str, Any]] = []
    day_list = store.list_report_days(limit=days, bucket=bucket)
    for day in day_list:
        raw = store.load_report_raw(day, bucket=bucket)
        records.extend(store.report_plays(raw))
    report = compute_calibration(records)
    report["as_of_days"] = day_list
    return report
