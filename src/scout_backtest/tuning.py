"""Propose bounded, walk-forward-validated weight changes from a backtest.

ANALYSIS ONLY -- this PROPOSES; it never writes the scout's config. Given settled
backtest records carrying each play's factor raws (trend/momentum/rsi) + outcome,
it measures how well each factor discriminated wins from losses on an EARLIER
(train) window, proposes a BOUNDED reweight, and validates on the LATER (holdout)
window by whether the reweighted conviction separates wins from losses better.

Honest limits: the validation re-scores ALREADY-QUALIFIED plays (it does not
re-run play selection), it reflects the backtest's regime, and news is excluded.
So it is a suggestion for a human to approve, not a certified improvement.
"""

from __future__ import annotations

from typing import Any, Sequence

FACTORS = ("trend", "momentum", "rsi")


def _dir_sign(rec: dict[str, Any]) -> float:
    return 1.0 if str(rec.get("direction", "")).strip().lower() in ("call", "long") else -1.0


def _verdict(rec: dict[str, Any]) -> str | None:
    outcome = rec.get("outcome")
    if isinstance(outcome, dict):
        v = str(outcome.get("verdict", "")).strip().upper()
        return v if v in ("WIN", "LOSS") else None
    return None


def _settled_with_factors(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for rec in records:
        if _verdict(rec) is None:
            continue
        factors = rec.get("factors")
        if isinstance(factors, dict) and any(f in factors for f in FACTORS):
            out.append(rec)
    return out


def _norm_weights(weights: dict[str, Any]) -> dict[str, float]:
    vals = {f: max(float(weights.get(f, 0.0) or 0.0), 0.0) for f in FACTORS}
    total = sum(vals.values()) or 1.0
    return {f: vals[f] / total for f in FACTORS}


def _aligned_factor(rec: dict[str, Any], factor: str) -> float | None:
    """The factor's raw read pointed in the play's chosen direction: positive when
    the factor supported the direction actually taken."""
    val = (rec.get("factors") or {}).get(factor)
    if val is None:
        return None
    return float(val) * _dir_sign(rec)


def combined_aligned_score(rec: dict[str, Any], weights: dict[str, Any]) -> float | None:
    """Weighted directional conviction (mirrors directional_score_at's math),
    pointed in the play's direction. None if no factors present."""
    nw = _norm_weights(weights)
    total = 0.0
    have = False
    for factor in FACTORS:
        aligned = _aligned_factor(rec, factor)
        if aligned is None:
            continue
        total += nw[factor] * aligned
        have = True
    return total if have else None


def factor_discrimination(records: Sequence[dict[str, Any]], factor: str) -> float:
    """mean(aligned raw | WIN) - mean(aligned raw | LOSS). Positive => the factor,
    when it supported the taken direction, tended to be right."""
    wins = [a for a in (_aligned_factor(r, factor) for r in records if _verdict(r) == "WIN") if a is not None]
    losses = [a for a in (_aligned_factor(r, factor) for r in records if _verdict(r) == "LOSS") if a is not None]
    if not wins or not losses:
        return 0.0
    return (sum(wins) / len(wins)) - (sum(losses) / len(losses))


def discriminator_gap(records: Sequence[dict[str, Any]], weights: dict[str, Any]) -> float | None:
    """How well the weighted conviction separates wins from losses on `records`:
    win-rate of the top half by conviction minus win-rate of the bottom half.
    Higher is better; None when too few scored plays."""
    scored = []
    for rec in records:
        score = combined_aligned_score(rec, weights)
        verdict = _verdict(rec)
        if score is not None and verdict is not None:
            scored.append((score, 1 if verdict == "WIN" else 0))
    if len(scored) < 4:
        return None
    scored.sort(key=lambda x: x[0])
    half = len(scored) // 2
    bottom = scored[:half]
    top = scored[len(scored) - half:]
    top_wr = sum(w for _, w in top) / len(top)
    bottom_wr = sum(w for _, w in bottom) / len(bottom)
    return round(top_wr - bottom_wr, 4)


def propose_weights(
    records: Sequence[dict[str, Any]],
    current_weights: dict[str, Any],
    *,
    bound: float = 0.35,
    train_frac: float = 0.7,
) -> dict[str, Any]:
    """Walk-forward proposal. Returns current vs proposed weights, the per-factor
    discrimination (from train), the holdout validation gap for each, and a plain
    recommendation. Never mutates config."""
    settled = _settled_with_factors(records)
    settled.sort(key=lambda r: str(r.get("date", "")))
    n = len(settled)
    if n < 20:
        return {
            "current": {f: round(float(current_weights.get(f, 0.0) or 0.0), 4) for f in FACTORS},
            "proposed": None,
            "recommendation": "insufficient data",
            "settled": n,
            "note": "Need at least ~20 settled plays with factors to propose a change.",
        }

    cut = max(int(n * train_frac), 1)
    train, validate = settled[:cut], settled[cut:]

    disc = {f: round(factor_discrimination(train, f), 4) for f in FACTORS}
    max_abs = max((abs(d) for d in disc.values()), default=0.0) or 1.0

    raw_proposed = {}
    for factor in FACTORS:
        current = max(float(current_weights.get(factor, 0.0) or 0.0), 0.0)
        nudge = (disc[factor] / max_abs) * bound  # in [-bound, +bound]
        raw_proposed[factor] = current * (1.0 + nudge)

    # Renormalize so the proposed weights sum to the same total as the current ones.
    orig_sum = sum(max(float(current_weights.get(f, 0.0) or 0.0), 0.0) for f in FACTORS) or 1.0
    prop_sum = sum(raw_proposed.values()) or 1.0
    proposed = {f: round(raw_proposed[f] * orig_sum / prop_sum, 4) for f in FACTORS}

    current_gap = discriminator_gap(validate, current_weights)
    proposed_gap = discriminator_gap(validate, proposed)
    improved = (
        current_gap is not None
        and proposed_gap is not None
        and proposed_gap > current_gap + 0.01
    )

    return {
        "current": {f: round(float(current_weights.get(f, 0.0) or 0.0), 4) for f in FACTORS},
        "proposed": proposed,
        "discrimination": disc,
        "train_n": len(train),
        "validate_n": len(validate),
        "validation": {
            "current_holdout_gap": current_gap,
            "proposed_holdout_gap": proposed_gap,
            "improved": improved,
        },
        "bound": bound,
        "recommendation": "apply (validated improvement)" if improved else "keep current (no validated gain)",
    }
