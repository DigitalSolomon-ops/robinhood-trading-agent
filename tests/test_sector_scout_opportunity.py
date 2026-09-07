"""The direction-conditioned opportunity score (2026-09-07 rework), day
levels, and the price-action plan. Pins the P1-1/P1-2/P1-3 fixes with the
2026-09-05 conditions as fixtures."""

from __future__ import annotations

from src.sector_scout.opportunity import (
    CONTRIBUTING_COLUMNS,
    day_levels,
    direction_for,
    opportunity_read,
    price_action_plan,
    realized_vol_percentile,
)

CFG = {
    "opportunity": {
        "rs_price_max": 10, "ret_max": 10, "ret3m_scale": 0.15,
        "ret3m_scale_coiled": 0.08, "ret12m_scale": 0.40, "cont_max": 20,
        "bearish_accel_penalty_max": 5, "accel_scale": 0.5,
        "entry_atr_fraction": 0.35, "day_range_atr": 1.0,
    }
}


def _row(cls: str, rs: float, price: float, r3, r12, *, cont_score, gates: bool,
         accel, above50: bool, above200: bool = True) -> dict:
    return {
        "classification": cls,
        "extremes": {"rs_pctile": rs, "price_pctile": price, "ret_3m": r3,
                     "ret_12m": r12, "above_sma50": above50, "above_sma200": above200},
        "continuation": {
            "score": cont_score, "accel": accel,
            "gate_momentum": gates, "gate_structure": gates, "gate_no_exhaustion": gates,
        },
        "iv_rank": {"iv_rank": None, "regime": "collecting"},
        "rate_beta": 0.6,
    }


def test_contributing_columns_exclude_iv_and_beta() -> None:
    assert "IV RANK" not in CONTRIBUTING_COLUMNS
    assert "BETA" not in CONTRIBUTING_COLUMNS
    read = opportunity_read(
        _row("Mid range", 60, 60, 0.05, 0.20, cont_score=6, gates=True,
             accel=0.1, above50=True), CFG,
    )
    assert "iv" not in read.components and "beta" not in read.components
    assert read.score == round(read.raw_points / read.live_max * 100.0, 1)


def test_coiled_reversal_can_rank_high() -> None:
    """P1-2 regression, the ITB shape from 2026-09-05: the ONLY Coiled name,
    RS percentile ~0, washed out 12m, stabilising 3m -- must score MATERIALLY
    above the old 51.1 because low RS is the setup, not a demerit."""
    itb = opportunity_read(
        _row("Coiled", rs=0.0, price=8.0, r3=0.06, r12=-0.25,
             cont_score=2, gates=False, accel=0.1, above50=True), CFG,
    )
    assert itb.direction == "bullish"
    assert itb.components["rs"] == 10.0          # RS 0 earns FULL washout points
    assert itb.components["12m"] > 0             # negative 12m is the opportunity
    assert itb.score > 65, itb.breakdown()


def test_gate_failure_bars_long_candidacy() -> None:
    """P1-1 regression, the SMH shape from 2026-09-05: 3m return negative,
    acceleration -0.97, price below the 50 day. Not a bullish candidate."""
    smh = _row("Mid range", rs=85, price=80, r3=-0.005, r12=0.60,
               cont_score=None, gates=False, accel=-0.97, above50=False)
    read = opportunity_read(smh, CFG)
    assert read.direction is None
    assert "cont" not in read.components  # voided score = dead column, renormalised
    direction, reason = direction_for(
        "Mid range", continuation_score=None, gates_all_pass=False,
        accel=-0.97, above_sma50=False,
    )
    assert direction is None and "gates" in reason


def test_extended_with_positive_acceleration_is_no_trade() -> None:
    """P1-3 regression, the GDX shape from 2026-09-05: +25.9% over 3 months,
    accel +0.50, above both averages -- NO put spread, a trigger instead."""
    gdx = _row("Extended", rs=95, price=96, r3=0.259, r12=0.55,
               cont_score=3, gates=False, accel=0.50, above50=True)
    read = opportunity_read(gdx, CFG)
    assert read.direction is None
    assert "triggers only when" in (read.no_trade_reason or "")
    assert read.trigger_level_hint == "below_sma50_with_negative_acceleration"


def test_extended_rolling_over_is_bearish_and_penalised_only_when_accelerating() -> None:
    triggered = opportunity_read(
        _row("Extended", rs=95, price=96, r3=-0.04, r12=0.55,
             cont_score=2, gates=False, accel=-0.3, above50=False), CFG,
    )
    assert triggered.direction == "bearish"
    assert triggered.components["cont"] == 15.0   # (8-2)/8 * 20: weakness confirms
    assert "accel_penalty" not in triggered.components
    assert triggered.components["3m"] > 0         # negative 3m = the roll-over pays


def test_falling_knife_never_trades() -> None:
    knife = opportunity_read(
        _row("Falling knife", 5, 5, -0.1, -0.3, cont_score=0, gates=False,
             accel=-0.2, above50=False), CFG,
    )
    assert knife.direction is None
    assert knife.components["class"] == 0.0


def test_realized_vol_percentile_is_labeled_context() -> None:
    quiet = [100.0 + 0.05 * i for i in range(300)]
    spike = quiet[:-20] + [quiet[-21] * (1 + 0.03 * ((-1) ** i)) for i in range(20)]
    pct = realized_vol_percentile(spike)
    assert pct is not None and pct > 80  # a volatility spike ranks high
    assert realized_vol_percentile([100.0] * 30) is None  # thin data: None


def test_day_levels_bullish_entry_below_close_bearish_above() -> None:
    up = day_levels(100.0, 2.0, "bullish", CFG)
    assert up is not None
    assert up.ideal_entry == 99.3
    assert up.day_floor == 98.0 and up.day_ceiling == 102.0
    down = day_levels(100.0, 2.0, "bearish", CFG)
    assert down is not None and down.ideal_entry == 100.7
    assert day_levels(0.0, 2.0, "bullish", CFG) is None


def test_price_action_plan_names_the_levels_plainly() -> None:
    levels = day_levels(100.0, 2.0, "bullish", CFG)
    plan = price_action_plan("XLE", "Coiled", "bullish", levels)
    for token in ("XLE", "99.3", "98", "102", "voids the entry"):
        assert token in plan, token
    assert "—" not in plan and "–" not in plan
