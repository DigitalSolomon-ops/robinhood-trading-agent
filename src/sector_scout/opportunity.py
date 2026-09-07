"""The opportunity score, direction rules, and day-trade entry levels.

REWORKED 2026-09-07 against the 2026-09-05 defect review:

* P1-2 -- the score is DIRECTION-CONDITIONED for real now. A Coiled long
  scores highest at LOW relative strength and LOW price percentile, and its
  momentum legs reward the washout depth (negative 12m) plus the stabilising
  3m turn -- so a reversal can actually rank into the Top 9. An Extended
  short keeps high-RS fit but LOSES points for positive acceleration.
* P1-3 -- Extended produces a bearish TRADE only when acceleration is
  negative AND price has lost the 50 day. Otherwise the direction is None
  and the fund reports as "Extended, no trade" with the trigger level.
* P2-1 -- the IV component is OUT of the score until a real implied-vol
  history exists; the score renormalises to the columns that are live, and
  the report states which those are. A realized-volatility percentile ships
  as labeled context, never printed as "IV rank".
* P2-5 -- rate beta is OUT of the score entirely: it is a clustering
  diagnostic, kept on the board and in the narrative at zero weight.

Every weight stays config-tunable; the breakdown prints beside every score;
the docs ledger records this as an operator decree, not a backtest-earned
weighting.

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

# The columns that currently carry scoring weight. IV is deliberately absent
# (no real implied-vol history yet) and BETA is deliberately absent
# (diagnostic, not a signal). The report prints this list verbatim.
CONTRIBUTING_COLUMNS = ("CLASS", "RS PCT", "PRICE PCT", "3M", "12M", "CONT")
EXCLUDED_COLUMNS_NOTE = (
    "IV excluded until a real implied-volatility history accumulates; "
    "BETA is a clustering diagnostic and carries no weight."
)


@dataclass(frozen=True)
class OpportunityRead:
    score: float                  # 0..100, renormalised over live components
    raw_points: float
    live_max: float
    components: dict[str, float]  # each live column's earned points
    direction: str | None         # "bullish" | "bearish" | None (no trade)
    no_trade_reason: str | None = None
    trigger_level_hint: str | None = None

    def breakdown(self) -> str:
        parts = " + ".join(f"{k} {v:g}" for k, v in self.components.items())
        return f"{self.score:g}/100 (raw {self.raw_points:g} of {self.live_max:g}: {parts})"


def direction_for(
    classification: str,
    *,
    continuation_score: int | None,
    gates_all_pass: bool,
    accel: float | None,
    above_sma50: bool,
) -> tuple[str | None, str | None]:
    """(direction, no_trade_reason). The hard rules:

    * Falling knife: never a trade.
    * Extended: bearish ONLY when acceleration is negative AND price has lost
      the 50 day (P1-3). Otherwise no trade, with the trigger stated.
    * Bullish requires Coiled, or the continuation gates ALL passing with a
      score of at least 5 (P1-1: a gate failure is a hard filter).
    """
    if classification == FALLING_KNIFE:
        return None, "Falling knife: no structure by rule."
    if classification == EXTENDED:
        rolling_over = accel is not None and accel < 0 and not above_sma50
        if rolling_over:
            return "bearish", None
        return None, (
            "Extended, no trade: the short triggers only when acceleration turns "
            "negative AND price closes below the 50 day average."
        )
    if classification == COILED:
        return "bullish", None
    if gates_all_pass and (continuation_score or 0) >= 5:
        return "bullish", None
    return None, (
        "No trade: not Coiled, and the continuation gates do not all pass with "
        "a score of at least 5."
    )


def opportunity_read(row: dict[str, Any], cfg: dict[str, Any]) -> OpportunityRead:
    """Score one fund row by thesis fit over the LIVE columns only."""
    opp = cfg.get("opportunity", {}) or {}
    class_points: dict[str, float] = {
        **DEFAULT_CLASS_POINTS,
        **{str(k): float(v) for k, v in (opp.get("class_points") or {}).items()},
    }
    rs_price_max = float(opp.get("rs_price_max", 10))
    ret_max = float(opp.get("ret_max", 10))
    s3 = float(opp.get("ret3m_scale", 0.15))
    s3_coiled = float(opp.get("ret3m_scale_coiled", 0.08))
    s12 = float(opp.get("ret12m_scale", 0.40))
    cont_max = float(opp.get("cont_max", 20))
    accel_penalty_max = float(opp.get("bearish_accel_penalty_max", 5))
    accel_scale = float(opp.get("accel_scale", 0.5))

    cls = str(row.get("classification") or "Mid range")
    ext = row.get("extremes") or {}
    cont = row.get("continuation") or {}
    cont_score = cont.get("score")
    gates_all = bool(
        cont.get("gate_momentum") and cont.get("gate_structure") and cont.get("gate_no_exhaustion")
    )
    accel = cont.get("accel")

    direction, no_trade = direction_for(
        cls,
        continuation_score=int(cont_score) if cont_score is not None else None,
        gates_all_pass=gates_all,
        accel=accel,
        above_sma50=bool(ext.get("above_sma50")),
    )

    components: dict[str, float] = {}
    live_max = 0.0

    components["class"] = round(class_points.get(cls, 0.0), 1)
    live_max += max(class_points.values())

    # Explicit None checks: an RS percentile of 0.0 is the MOST washed-out
    # reading possible (the ITB case), not a missing value -- `or 50.0`
    # would silently neutralise exactly the setups this score exists to rank.
    raw_rs, raw_price = ext.get("rs_pctile"), ext.get("price_pctile")
    rs = float(raw_rs) if raw_rs is not None else 50.0
    price = float(raw_price) if raw_price is not None else 50.0
    mean_reverting = cls == COILED
    if mean_reverting:
        components["rs"] = round((100.0 - rs) / 100.0 * rs_price_max, 1)
        components["price"] = round((100.0 - price) / 100.0 * rs_price_max, 1)
    else:
        components["rs"] = round(rs / 100.0 * rs_price_max, 1)
        components["price"] = round(price / 100.0 * rs_price_max, 1)
    live_max += 2 * rs_price_max

    r3 = ext.get("ret_3m")
    r12 = ext.get("ret_12m")
    if mean_reverting:
        # The washout IS the setup: reward the negative 12m depth and the
        # stabilising 3m turn (P1-2 -- a reversal can now rank).
        components["3m"] = round(_clip01((r3 or 0.0) / s3_coiled) * ret_max, 1)
        components["12m"] = round(_clip01(-(r12 or 0.0) / s12) * ret_max, 1)
    elif direction == "bearish":
        # Fading an extension: the 12m run-up is the fuel; the 3m leg pays
        # for evidence of the roll-over, not for more strength.
        components["3m"] = round(_clip01(-(r3 or 0.0) / s3) * ret_max, 1)
        components["12m"] = round(_clip01((r12 or 0.0) / s12) * ret_max, 1)
    else:
        components["3m"] = round(_clip01((r3 or 0.0) / s3) * ret_max, 1)
        components["12m"] = round(_clip01((r12 or 0.0) / s12) * ret_max, 1)
    live_max += 2 * ret_max

    if cont_score is not None:
        if direction == "bearish":
            components["cont"] = round((8 - min(int(cont_score), 8)) / 8.0 * cont_max, 1)
        else:
            components["cont"] = round(min(int(cont_score), 8) / 8.0 * cont_max, 1)
        live_max += cont_max
    # A gate-failed fund has no continuation score: the column is DEAD for it
    # and the renormalisation below excludes it rather than scoring a zero.

    if direction == "bearish" and accel is not None and accel > 0:
        components["accel_penalty"] = round(-_clip01(accel / accel_scale) * accel_penalty_max, 1)

    raw = sum(components.values())
    raw = max(raw, 0.0)
    score = round(raw / live_max * 100.0, 1) if live_max > 0 else 0.0

    trigger_hint = None
    if cls == EXTENDED and direction is None:
        trigger_hint = "below_sma50_with_negative_acceleration"

    return OpportunityRead(
        score=score,
        raw_points=round(raw, 1),
        live_max=round(live_max, 1),
        components=components,
        direction=direction,
        no_trade_reason=no_trade,
        trigger_level_hint=trigger_hint,
    )


def realized_vol_percentile(closes_daily: list[float], window: int = 20) -> float | None:
    """Rank of the CURRENT trailing-window realized vol within the series of
    all trailing-window realized vols over the available history. Labeled
    context: this is REALIZED volatility, never printed as IV rank."""
    if len(closes_daily) < window * 3:
        return None
    import math

    rvs: list[float] = []
    rets = [
        closes_daily[i] / closes_daily[i - 1] - 1.0
        for i in range(1, len(closes_daily))
        if closes_daily[i - 1] > 0
    ]
    for end in range(window, len(rets) + 1):
        chunk = rets[end - window : end]
        mean = sum(chunk) / window
        var = sum((r - mean) ** 2 for r in chunk) / (window - 1)
        rvs.append(math.sqrt(var))
    if len(rvs) < 30:
        return None
    current = rvs[-1]
    below = sum(1 for v in rvs[:-1] if v < current)
    return round(100.0 * below / (len(rvs) - 1), 1)


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
