"""Structure selection and the order ticket for one Sector Scout play.

Structure by classification AND IV rank:
  * Coiled            -> long call debit spread, ~0.35 delta long leg
  * Continuation      -> long call debit spread, slightly-ITM ~0.60 delta long
                         leg, short leg above the six-month measured move
  * Extended          -> long put debit spread
  * Falling knife     -> NO structure; the falsifier that would create one
  * IV-rank routing   -> rank < buy_max: debit structures are cheap, favour
                         them; rank > sell_min: express the SAME view as a
                         credit spread; between: the classification decides.

Every structure ships as a complete order ticket. THE LIMIT PRICE IS NOT THE
MIDPOINT: it is modeled from a high-fill-rate estimate (mark +/- a configured
fraction of the half-spread -- Massive has no broker fill-rate field, and the
docs say this is a model), rounded to the nearest five cents, with the
midpoint and the worst-case debit shown beside it so the cost of certainty is
visible.

Standing execution rules ship in every ticket and are part of the rendered
report: single multi-leg order never legged in, never a market order on a
spread, GTC not day, take profit at the configured fraction of max gain, roll
or close at the configured days before expiry, exit on the named falsifier.

ANALYSIS ONLY. This module produces TEXT AND NUMBERS for a report. It never
places, previews, reviews, or cancels an order.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from ..equity_intelligence.massive_client import OptionSnapshot
from .probability import SpreadProbability, credit_vertical, debit_call_spread, debit_put_spread

EXECUTION_RULES = (
    "Enter as a single multi-leg order, never legged in.",
    "Never a market order on a spread.",
    "Good-til-cancelled, not day.",
    "Take profit at {tp_pct:.0f} percent of max gain.",
    "Roll or close by {roll_date} (about {roll_days} days before expiry).",
    "Exit immediately on the named falsifier.",
)


@dataclass(frozen=True)
class Leg:
    """One leg of the structure, with its live snapshot economics."""

    action: str                 # "buy" | "sell"
    option_type: str            # "call" | "put"
    ticker: str
    strike: float
    expiry: str                 # YYYY-MM-DD
    bid: float | None
    ask: float | None
    mark: float | None
    delta: float | None
    iv: float | None
    open_interest: float | None
    snapshot_at: str            # ISO timestamp of the Massive snapshot

    @property
    def spread_pct_of_mark(self) -> float | None:
        if self.bid is None or self.ask is None or not self.mark:
            return None
        return round((self.ask - self.bid) / self.mark * 100.0, 1)


@dataclass(frozen=True)
class OrderTicket:
    """The complete ticket: exact contracts + every derived number."""

    structure: str              # e.g. "long call debit spread"
    direction: str              # "bullish" | "bearish"
    legs: tuple[Leg, ...]
    dte_calendar: int
    limit_price: float          # modeled high-fill-rate estimate, NOT midpoint
    midpoint: float
    worst_case: float           # long ask - short bid (cost of certainty)
    order_type: str             # "net debit" | "net credit"
    max_loss: float             # dollars per spread
    max_gain: float
    reward_to_risk: float | None
    breakeven: float
    move_required_pct: float    # underlying move to breakeven
    net_delta: float | None
    take_profit_level: float    # dollars per spread
    roll_or_close_date: str
    execution_rules: tuple[str, ...]
    fill_model_note: str


@dataclass(frozen=True)
class Structure:
    """A selected structure with its probability block, or a documented pass."""

    fund: str
    classification: str
    action: str                 # "debit_call_spread" | ... | "no_structure"
    rationale: str
    ticket: OrderTicket | None = None
    probability: SpreadProbability | None = None
    skipped_reason: str | None = None
    dropped_contracts: tuple[str, ...] = field(default_factory=tuple)


def pick_monthly_expiry(expirations: list[str], today: date, cfg: dict[str, Any]) -> str | None:
    """The MONTHLY expiration (third Friday) closest to target_dte, accepting
    dte_min..dte_max. Always report actual DTE, never 'six months' alone."""
    st = cfg.get("structures", {}) or {}
    target = int(st.get("target_dte", 180))
    lo = int(st.get("dte_min", 150))
    hi = int(st.get("dte_max", 240))

    def _is_monthly(iso: str) -> bool:
        try:
            d = date.fromisoformat(iso)
        except ValueError:
            return False
        return d.weekday() == 4 and 15 <= d.day <= 21

    candidates = []
    for iso in expirations:
        try:
            dte = (date.fromisoformat(iso) - today).days
        except ValueError:
            continue
        if lo <= dte <= hi:
            candidates.append((iso, dte, _is_monthly(iso)))
    if not candidates:
        return None
    monthlies = [c for c in candidates if c[2]]
    pool = monthlies or candidates
    return min(pool, key=lambda c: abs(c[1] - target))[0]


def _round_to(value: float, step: float) -> float:
    return round(round(value / step) * step, 2)


def _leg_from_snapshot(
    action: str, option_type: str, ticker: str, strike: float, expiry: str,
    snap: OptionSnapshot | None, snapshot_at: str,
) -> Leg:
    return Leg(
        action=action,
        option_type=option_type,
        ticker=ticker,
        strike=strike,
        expiry=expiry,
        bid=snap.bid if snap else None,
        ask=snap.ask if snap else None,
        mark=snap.premium if snap else None,
        delta=snap.delta if snap else None,
        iv=snap.implied_volatility if snap else None,
        open_interest=snap.open_interest if snap else None,
        snapshot_at=snapshot_at,
    )


def _fill_estimate(mark: float, bid: float, ask: float, *, buying: bool, fraction: float) -> float:
    """Modeled high-fill-rate price: mark nudged toward the touch by the
    configured fraction of the half-spread. A MODEL, not a broker estimate --
    the ticket and the docs both say so."""
    half = max((ask - bid) / 2.0, 0.0)
    return mark + fraction * half if buying else mark - fraction * half


def build_debit_spread_ticket(
    *,
    direction: str,                # "bullish" -> calls, "bearish" -> puts
    long_leg: Leg,
    short_leg: Leg,
    spot: float,
    today: date,
    cfg: dict[str, Any],
    rate: float,
) -> tuple[OrderTicket, SpreadProbability] | None:
    """Assemble the ticket + probability block for a debit vertical. Returns
    None when a required live number is missing -- n/a, never fabricated."""
    st = cfg.get("structures", {}) or {}
    if None in (long_leg.mark, short_leg.mark, long_leg.bid, long_leg.ask,
                short_leg.bid, short_leg.ask):
        return None

    frac = float(st.get("fill_model_half_spread_fraction", 0.40))
    step = float(st.get("limit_price_rounding", 0.05))
    tp_frac = float(st.get("take_profit_fraction", 0.65))
    roll_days = int(st.get("roll_or_close_days_before_expiry", 45))

    midpoint = round(long_leg.mark - short_leg.mark, 2)
    if midpoint <= 0:
        return None
    fill_long = _fill_estimate(long_leg.mark, long_leg.bid, long_leg.ask, buying=True, fraction=frac)
    fill_short = _fill_estimate(short_leg.mark, short_leg.bid, short_leg.ask, buying=False, fraction=frac)
    limit = _round_to(max(fill_long - fill_short, 0.01), step)
    worst = round(long_leg.ask - short_leg.bid, 2)

    expiry_d = date.fromisoformat(long_leg.expiry)
    dte = (expiry_d - today).days

    if direction == "bullish":
        prob = debit_call_spread(
            spot=spot, long_strike=long_leg.strike, short_strike=short_leg.strike,
            debit=limit, long_iv=long_leg.iv, short_iv=short_leg.iv,
            dte_calendar_days=dte, rate=rate,
        )
        breakeven = round(long_leg.strike + limit, 2)
        move_req = (breakeven / spot - 1.0) * 100.0
        structure_name = "long call debit spread"
    else:
        prob = debit_put_spread(
            spot=spot, long_strike=long_leg.strike, short_strike=short_leg.strike,
            debit=limit, long_iv=long_leg.iv, short_iv=short_leg.iv,
            dte_calendar_days=dte, rate=rate,
        )
        breakeven = round(long_leg.strike - limit, 2)
        move_req = (1.0 - breakeven / spot) * 100.0
        structure_name = "long put debit spread"
    if prob is None:
        return None

    net_delta = None
    if long_leg.delta is not None and short_leg.delta is not None:
        net_delta = round(long_leg.delta - short_leg.delta, 3)

    roll_date = (expiry_d - timedelta(days=roll_days)).isoformat()
    rules = tuple(
        r.format(tp_pct=tp_frac * 100.0, roll_date=roll_date, roll_days=roll_days)
        for r in EXECUTION_RULES
    )
    ticket = OrderTicket(
        structure=structure_name,
        direction=direction,
        legs=(long_leg, short_leg),
        dte_calendar=dte,
        limit_price=limit,
        midpoint=midpoint,
        worst_case=worst,
        order_type="net debit",
        max_loss=prob.max_loss,
        max_gain=prob.max_gain,
        reward_to_risk=prob.reward_to_risk,
        breakeven=breakeven,
        move_required_pct=round(move_req, 2),
        net_delta=net_delta,
        take_profit_level=round(prob.max_gain * tp_frac, 2),
        roll_or_close_date=roll_date,
        execution_rules=rules,
        fill_model_note=(
            f"Limit modeled as mark +/- {frac * 100:.0f}% of the half-spread per leg "
            "(no broker fill-rate field on Massive); midpoint and worst-case shown for comparison."
        ),
    )
    return ticket, prob


def build_credit_spread_ticket(
    *,
    direction: str,
    short_leg: Leg,
    long_leg: Leg,
    spot: float,
    today: date,
    cfg: dict[str, Any],
    rate: float,
) -> tuple[OrderTicket, SpreadProbability] | None:
    """Credit vertical for the sell-premium IV regime (short put spread when
    bullish, short call spread when bearish)."""
    st = cfg.get("structures", {}) or {}
    if None in (short_leg.mark, long_leg.mark, short_leg.bid, short_leg.ask,
                long_leg.bid, long_leg.ask):
        return None
    frac = float(st.get("fill_model_half_spread_fraction", 0.40))
    step = float(st.get("limit_price_rounding", 0.05))
    tp_frac = float(st.get("take_profit_fraction", 0.65))
    roll_days = int(st.get("roll_or_close_days_before_expiry", 45))

    midpoint = round(short_leg.mark - long_leg.mark, 2)
    if midpoint <= 0:
        return None
    fill_short = _fill_estimate(short_leg.mark, short_leg.bid, short_leg.ask, buying=False, fraction=frac)
    fill_long = _fill_estimate(long_leg.mark, long_leg.bid, long_leg.ask, buying=True, fraction=frac)
    limit = _round_to(max(fill_short - fill_long, 0.01), step)
    worst = round(short_leg.bid - long_leg.ask, 2)

    expiry_d = date.fromisoformat(short_leg.expiry)
    dte = (expiry_d - today).days
    prob = credit_vertical(
        spot=spot, short_strike=short_leg.strike, long_strike=long_leg.strike,
        credit=limit, short_iv=short_leg.iv, long_iv=long_leg.iv,
        dte_calendar_days=dte, direction=direction, rate=rate,
    )
    if prob is None:
        return None

    if direction == "bullish":
        breakeven = round(short_leg.strike - limit, 2)
        move_req = (breakeven / spot - 1.0) * 100.0  # can be negative: cushion
        structure_name = "short put credit spread"
    else:
        breakeven = round(short_leg.strike + limit, 2)
        move_req = (breakeven / spot - 1.0) * 100.0
        structure_name = "short call credit spread"

    net_delta = None
    if short_leg.delta is not None and long_leg.delta is not None:
        net_delta = round(-(short_leg.delta - long_leg.delta), 3)

    roll_date = (expiry_d - timedelta(days=roll_days)).isoformat()
    rules = tuple(
        r.format(tp_pct=tp_frac * 100.0, roll_date=roll_date, roll_days=roll_days)
        for r in EXECUTION_RULES
    )
    ticket = OrderTicket(
        structure=structure_name,
        direction=direction,
        legs=(short_leg, long_leg),
        dte_calendar=dte,
        limit_price=limit,
        midpoint=midpoint,
        worst_case=worst,
        order_type="net credit",
        max_loss=prob.max_loss,
        max_gain=prob.max_gain,
        reward_to_risk=prob.reward_to_risk,
        breakeven=breakeven,
        move_required_pct=round(move_req, 2),
        net_delta=net_delta,
        take_profit_level=round(prob.max_gain * tp_frac, 2),
        roll_or_close_date=roll_date,
        execution_rules=rules,
        fill_model_note=(
            f"Limit modeled as mark +/- {frac * 100:.0f}% of the half-spread per leg "
            "(no broker fill-rate field on Massive); midpoint and worst-case shown for comparison."
        ),
    )
    return ticket, prob


def measured_move_strike(spot: float, expected_move_pct: float) -> float:
    """The level one six-month measured move up from spot; the continuation
    short leg sits at or above it."""
    return spot * (1.0 + max(expected_move_pct, 0.0))


def expected_move_6m(closes_daily: list[float], lookback: int = 63) -> float | None:
    """Realized-vol six-month expected move (fraction): stdev of daily returns
    over the lookback, scaled by sqrt(126 trading days). None when the data
    is too thin -- n/a, never a fabricated default."""
    rets = [
        closes_daily[i] / closes_daily[i - 1] - 1.0
        for i in range(max(1, len(closes_daily) - lookback), len(closes_daily))
        if closes_daily[i - 1] > 0
    ]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(126.0)
