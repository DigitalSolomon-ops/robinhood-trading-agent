"""The opportunity score and the day-trade entry levels for the Top 9 board.

OPERATOR-DIRECTED RANKING (2026-09-04): the report culminates in a Top 9 of
the whole universe, ranked by ONE cumulative score built from the board
columns: CLASS, RS PCT, PRICE PCT, 3M, 12M, CONT, IV RANK, BETA. The score is
a transparent heuristic, not a backtest-earned weighting -- every component
is config-tunable, the breakdown prints beside every score, and the docs
ledger records it as an operator decree. Each column is scored by THESIS
FIT: a Coiled fund earns points for being washed out where a Leading fund
earns them for strength, so the number always means "how cleanly does this
fund fit the trade it argues for."

Day levels: from the last close and ATR(14), so the 6:00 pre-market email
hands the options trader today's actionable prices: an ideal entry (the
pullback/fade level), and the day's expected ceiling and floor (one ATR
either side). The price-action paragraph says in plain English what the tape
should look like before entering, and what voids the entry.

Pure functions; no I/O. ANALYSIS ONLY -- no order path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .lenses import COILED, EXTENDED, FALLING_KNIFE, LEADING


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


DEFAULT_CLASS_POINTS = {
    COILED: 25.0,
    LEADING: 20.0,
    EXTENDED: 15.0,
    "Mid range": 8.0,
    FALLING_KNIFE: 0.0,
}


@dataclass(frozen=True)
class OpportunityRead:
    score: float                  # 0..100
    components: dict[str, float]  # each column's earned points
    direction: str | None         # "bullish" | "bearish" | None (no trade)

    def breakdown(self) -> str:
        parts = " + ".join(f"{k} {v:g}" for k, v in self.components.items())
        return f"{self.score:g} = {parts}"


def opportunity_read(row: dict[str, Any], cfg: dict[str, Any]) -> OpportunityRead:
    """Score one fund row (the analyzer's board-row dict) by thesis fit."""
    opp = cfg.get("opportunity", {}) or {}
    class_points: dict[str, float] = {
        **DEFAULT_CLASS_POINTS,
        **{str(k): float(v) for k, v in (opp.get("class_points") or {}).items()},
    }
    rs_price_max = float(opp.get("rs_price_max", 10))
    ret_max = float(opp.get("ret_max", 10))
    s3 = float(opp.get("ret3m_scale", 0.15))
    s12 = float(opp.get("ret12m_scale", 0.40))
    cont_max = float(opp.get("cont_max", 20))
    iv_max = float(opp.get("iv_max", 10))
    beta_max = float(opp.get("beta_max", 5))

    cls = str(row.get("classification") or "Mid range")
    ext = row.get("extremes") or {}
    cont = row.get("continuation") or {}
    ivr = row.get("iv_rank") or {}
    cont_score = int(cont.get("score") or 0)

    # Direction: the same rule the play selection uses.
    if cls == FALLING_KNIFE:
        direction: str | None = None
    elif cls == EXTENDED:
        direction = "bearish"
    elif cls == COILED or cont_score >= 5:
        direction = "bullish"
    else:
        direction = None

    components: dict[str, float] = {}
    components["class"] = round(class_points.get(cls, 0.0), 1)

    rs = float(ext.get("rs_pctile") or 50.0)
    price = float(ext.get("price_pctile") or 50.0)
    if cls == COILED:
        # Mean reversion: the washout IS the opportunity.
        components["rs"] = round((100.0 - rs) / 100.0 * rs_price_max, 1)
        components["price"] = round((100.0 - price) / 100.0 * rs_price_max, 1)
    else:
        components["rs"] = round(rs / 100.0 * rs_price_max, 1)
        components["price"] = round(price / 100.0 * rs_price_max, 1)

    r3 = ext.get("ret_3m")
    r12 = ext.get("ret_12m")
    # Momentum magnitude that FEEDS the thesis: bullish trades want the up
    # move, Extended puts want the very extension they fade.
    components["3m"] = round(_clip01((r3 or 0.0) / s3) * ret_max, 1)
    components["12m"] = round(_clip01((r12 or 0.0) / s12) * ret_max, 1)

    if direction == "bearish":
        # A weak continuation read CONFIRMS exhaustion.
        components["cont"] = round((8 - min(cont_score, 8)) / 8.0 * cont_max, 1)
    else:
        components["cont"] = round(min(cont_score, 8) / 8.0 * cont_max, 1)

    rank = ivr.get("iv_rank")
    if rank is None:
        components["iv"] = round(iv_max * 0.5, 1)  # collecting: neutral, stated
    elif (ivr.get("regime") or "") == "sell_premium":
        components["iv"] = round(float(rank) / 100.0 * iv_max, 1)  # rich premium to sell
    else:
        components["iv"] = round((100.0 - float(rank)) / 100.0 * iv_max, 1)  # cheap to buy

    beta = row.get("rate_beta")
    pure = 1.0 - _clip01(abs(float(beta))) if beta is not None else 0.5
    components["beta"] = round(pure * beta_max, 1)

    score = round(sum(components.values()), 1)
    return OpportunityRead(score=score, components=components, direction=direction)


@dataclass(frozen=True)
class DayLevels:
    """Today's actionable prices on the underlying, from close and ATR(14)."""

    reference_close: float
    atr: float
    ideal_entry: float
    day_floor: float
    day_ceiling: float


def day_levels(
    close: float, atr_value: float, direction: str, cfg: dict[str, Any]
) -> DayLevels | None:
    if close <= 0 or atr_value <= 0:
        return None
    opp = cfg.get("opportunity", {}) or {}
    entry_frac = float(opp.get("entry_atr_fraction", 0.35))
    band = float(opp.get("day_range_atr", 1.0))
    if direction == "bullish":
        entry = close - entry_frac * atr_value  # buy the pullback
    else:
        entry = close + entry_frac * atr_value  # fade the push
    return DayLevels(
        reference_close=round(close, 2),
        atr=round(atr_value, 2),
        ideal_entry=round(entry, 2),
        day_floor=round(close - band * atr_value, 2),
        day_ceiling=round(close + band * atr_value, 2),
    )


def price_action_plan(
    fund: str, classification: str, direction: str, levels: DayLevels,
) -> str:
    """Plain-English description of the tape we want before entering, with
    the levels in it, and what voids the entry. Deterministic prose from the
    data -- no adjectives the numbers cannot back."""
    e, f, c, ref = levels.ideal_entry, levels.day_floor, levels.day_ceiling, levels.reference_close
    if direction == "bullish":
        if classification == COILED:
            want = (
                f"an early dip toward the ideal entry {e:g} that HOLDS above the day floor "
                f"{f:g} and turns back up. Enter the call spread on the turn back through "
                f"{ref:g}, not on the way down."
            )
        else:
            want = (
                f"an orderly pullback toward {e:g} on quiet volume that holds above the day "
                f"floor {f:g}, then a push back through {ref:g}. Enter the call spread on "
                "that reclaim; strength that never pulls back can be entered in thirds."
            )
        void = (
            f"A gap open above the day ceiling {c:g} is a chase: stand down and wait for the "
            f"next session. A close below the day floor {f:g} voids the entry for the day."
        )
    else:
        want = (
            f"an early push toward the ideal entry {e:g} that STALLS below the day ceiling "
            f"{c:g} and rolls over. Enter the put spread as price falls back through "
            f"{ref:g}; do not short weakness already sitting at the day floor {f:g}."
        )
        void = (
            f"A clean break and hold above {c:g} says the extension is continuing: stand "
            "aside today rather than fighting it."
        )
    return f"Price action we are looking for in {fund}: {want} {void}"
