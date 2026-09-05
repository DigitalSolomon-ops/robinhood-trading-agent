"""The Top 9 opportunity score, day levels, and price-action plan."""

from __future__ import annotations

from src.sector_scout.opportunity import day_levels, opportunity_read, price_action_plan

CFG = {
    "opportunity": {
        "rs_price_max": 10, "ret_max": 10, "ret3m_scale": 0.15, "ret12m_scale": 0.40,
        "cont_max": 20, "iv_max": 10, "beta_max": 5,
        "entry_atr_fraction": 0.35, "day_range_atr": 1.0,
    }
}


def _row(cls: str, rs: float, price: float, r3, r12, cont: int,
         iv_rank, regime: str, beta) -> dict:
    return {
        "classification": cls,
        "extremes": {"rs_pctile": rs, "price_pctile": price, "ret_3m": r3, "ret_12m": r12},
        "continuation": {"score": cont},
        "iv_rank": {"iv_rank": iv_rank, "regime": regime},
        "rate_beta": beta,
    }


def test_coiled_scores_washout_as_opportunity() -> None:
    read = opportunity_read(
        _row("Coiled", rs=10, price=15, r3=0.05, r12=-0.20, cont=2,
             iv_rank=20, regime="buy_premium", beta=0.1), CFG,
    )
    assert read.direction == "bullish"
    assert read.components["class"] == 25.0
    assert read.components["rs"] == 9.0      # (100-10)/100 * 10
    assert read.components["price"] == 8.5   # (100-15)/100 * 10
    assert read.components["iv"] == 8.0      # cheap premium to buy
    assert read.components["beta"] == 4.5
    assert abs(read.score - sum(read.components.values())) < 0.01
    assert read.breakdown().startswith(f"{read.score:g} = ")


def test_leading_scores_strength_and_extended_inverts_cont() -> None:
    leading = opportunity_read(
        _row("Leading and earning it", rs=90, price=85, r3=0.12, r12=0.45, cont=7,
             iv_rank=25, regime="buy_premium", beta=-0.2), CFG,
    )
    assert leading.direction == "bullish"
    assert leading.components["rs"] == 9.0
    assert leading.components["cont"] == 17.5   # 7/8 * 20

    extended = opportunity_read(
        _row("Extended", rs=95, price=95, r3=0.20, r12=0.60, cont=2,
             iv_rank=80, regime="sell_premium", beta=0.0), CFG,
    )
    assert extended.direction == "bearish"
    assert extended.components["cont"] == 15.0  # (8-2)/8 * 20: weakness confirms
    assert extended.components["iv"] == 8.0     # rich premium to sell


def test_falling_knife_has_no_direction_and_scores_bottom() -> None:
    knife = opportunity_read(
        _row("Falling knife", rs=5, price=5, r3=-0.10, r12=-0.30, cont=0,
             iv_rank=50, regime="either", beta=0.0), CFG,
    )
    assert knife.direction is None
    assert knife.components["class"] == 0.0


def test_collecting_iv_scores_neutral() -> None:
    read = opportunity_read(
        _row("Mid range", rs=60, price=60, r3=0.05, r12=0.20, cont=6,
             iv_rank=None, regime="collecting", beta=0.3), CFG,
    )
    assert read.components["iv"] == 5.0
    assert read.direction == "bullish"  # cont >= 5


def test_day_levels_bullish_entry_below_close_bearish_above() -> None:
    up = day_levels(100.0, 2.0, "bullish", CFG)
    assert up is not None
    assert up.ideal_entry == 99.3       # 100 - 0.35*2
    assert up.day_floor == 98.0 and up.day_ceiling == 102.0

    down = day_levels(100.0, 2.0, "bearish", CFG)
    assert down is not None
    assert down.ideal_entry == 100.7    # fade the push
    assert day_levels(0.0, 2.0, "bullish", CFG) is None
    assert day_levels(100.0, 0.0, "bullish", CFG) is None


def test_price_action_plan_names_the_levels_plainly() -> None:
    levels = day_levels(100.0, 2.0, "bullish", CFG)
    plan = price_action_plan("XLE", "Coiled", "bullish", levels)
    for token in ("XLE", "99.3", "98", "102", "voids the entry"):
        assert token in plan, token
    assert "—" not in plan and "–" not in plan

    bear = price_action_plan("GDX", "Extended", "bearish", day_levels(100.0, 2.0, "bearish", CFG))
    assert "STALLS" in bear and "stand" in bear.lower()
