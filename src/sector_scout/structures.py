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
    """One leg of the structure, with its live snapshot economics.

    The Robinhood fields (fill-rate prices, broker chance of profit, volume,
    adjusted mark) populate when the leg was priced from a connector
    snapshot; the Massive path leaves them None and the ticket says which
    basis priced it."""

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
    snapshot_at: str            # ISO timestamp of the pricing snapshot
    theta: float | None = None
    vega: float | None = None
    volume: float | None = None
    adjusted_mark: float | None = None
    high_fill_rate_buy: float | None = None
    high_fill_rate_sell: float | None = None
    chance_of_profit_long: float | None = None
    pricing_basis: str = "live"   # "live" | "prior_session_close"
    source: str = "massive"       # "robinhood" | "massive"

    @property
    def spread_pct_of_mark(self) -> float | None:
        if self.bid is None or self.ask is None or not self.mark:
            return None
        return round((self.ask - self.bid) / self.mark * 100.0, 1)


def leg_from_rh_quote(
    action: str,
    option_type: str,
    instrument: dict[str, Any],
    quote: dict[str, Any],
    *,
    snapshot_at: str,
) -> Leg | None:
    """Build a Leg from a Robinhood option-quote row (connector field names,
    verbatim). Returns None when even a settled mark is absent -- n/a, never
    fabricated. Pricing basis: 'live' when a two-sided quote is present,
    otherwise 'prior_session_close' priced from adjusted mark / official
    close, and the ticket labels it."""

    def _f(key: str) -> float | None:
        raw = quote.get(key)
        try:
            return float(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    bid, ask = _f("bid_price"), _f("ask_price")
    mark = _f("adjusted_mark_price") or _f("mark_price")
    close_obj = quote.get("close") or {}
    close_price = None
    try:
        close_price = float(close_obj.get("price")) if close_obj.get("price") else None
    except (TypeError, ValueError):
        close_price = None

    live = bid is not None and ask is not None and (bid > 0 or ask > 0)
    if not live and mark is None and close_price is None:
        return None
    price_mark = mark if mark is not None else close_price
    basis = "live" if live else "prior_session_close"

    strike = instrument.get("strike_price") or quote.get("strike_price")
    expiry = instrument.get("expiration_date") or quote.get("expiration_date") or ""
    try:
        strike_f = float(strike)
    except (TypeError, ValueError):
        return None

    return Leg(
        action=action,
        option_type=option_type,
        ticker=str(instrument.get("id") or quote.get("instrument_id") or ""),
        strike=strike_f,
        expiry=str(expiry),
        bid=bid,
        ask=ask,
        mark=price_mark,
        delta=_f("delta"),
        iv=_f("implied_volatility"),
        open_interest=_f("open_interest"),
        snapshot_at=str(quote.get("updated_at") or snapshot_at),
        theta=_f("theta"),
        vega=_f("vega"),
        volume=_f("volume"),
        adjusted_mark=_f("adjusted_mark_price"),
        high_fill_rate_buy=_f("high_fill_rate_buy_price"),
        high_fill_rate_sell=_f("high_fill_rate_sell_price"),
        chance_of_profit_long=_f("chance_of_profit_long"),
        pricing_basis=basis,
        source="robinhood",
    )


def liquidity_violations(leg: Leg, cfg: dict[str, Any]) -> list[str]:
    """The contract-level liquidity gate: open interest, spread width, and
    volume. A leg failing any of these makes the structure unfillable in
    practice (2026-09-04: IHI legs with OI 0 and 9, spread wider than the
    debit). Volume is today's session (a single-snapshot proxy for the
    five-session rule; the docs say so)."""
    st = cfg.get("structures", {}) or {}
    liq = st.get("liquidity", {}) or {}
    min_oi = float(liq.get("min_open_interest", 250))
    max_spread_pct = float(liq.get("max_spread_pct_of_mid", 10))
    out: list[str] = []
    if leg.open_interest is None or leg.open_interest < min_oi:
        out.append(
            f"strike {leg.strike:g}: open interest "
            f"{int(leg.open_interest) if leg.open_interest is not None else 'n/a'} "
            f"below the {int(min_oi)} floor"
        )
    if leg.bid is not None and leg.ask is not None and (leg.bid + leg.ask) > 0:
        mid = (leg.bid + leg.ask) / 2.0
        if mid > 0:
            spread_pct = (leg.ask - leg.bid) / mid * 100.0
            if spread_pct > max_spread_pct:
                out.append(
                    f"strike {leg.strike:g}: bid-ask spread {spread_pct:.0f}% of mid, "
                    f"wider than the {max_spread_pct:.0f}% cap"
                )
    if liq.get("require_volume", True) and (leg.volume is None or leg.volume <= 0):
        if leg.pricing_basis == "live":
            out.append(f"strike {leg.strike:g}: no volume this session")
    return out


@dataclass(frozen=True)
class OrderTicket:
    """The complete ticket: exact contracts + every derived number."""

    structure: str              # e.g. "long call debit spread"
    direction: str              # "bullish" | "bearish"
    legs: tuple[Leg, ...]
    dte_calendar: int
    limit_price: float          # fill-rate-derived when available, NOT midpoint
    midpoint: float
    worst_case: float | None    # long ask - short bid (n/a on settled-mark pricing)
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
    # --- Robinhood-quote analytics (None on the Massive path) ----------------
    pricing_basis: str = "live"           # "live" | "prior_session_close"
    limit_basis: str = "modeled"          # "fill_rate_fields" | "modeled"
    broker_chance_of_profit_long: float | None = None  # long leg, broker's own
    theta_pct_of_debit_60d: float | None = None        # % of cost lost in 60 quiet days
    vega_crush_pnl: float | None = None                # $/spread if IV falls 20%, spot flat
    implied_move_pct: float | None = None              # ATM straddle / spot, to expiry
    move_required_vs_implied: float | None = None      # move_required / implied move
    exit_underlying_at_take_profit: float | None = None  # at-expiry mapping
    liquidity_notes: tuple[str, ...] = ()


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
    implied_move_pct: float | None = None,
) -> tuple[OrderTicket, SpreadProbability] | None:
    """Assemble the ticket + probability block for a debit vertical. Returns
    None when even a settled mark is missing -- n/a, never fabricated.
    Two-sided quotes make it LIVE pricing; marks alone make it
    PRIOR-SESSION pricing, labeled as such (a pre-market run still prices a
    ticket from settled closes, per P0-1)."""
    st = cfg.get("structures", {}) or {}
    if long_leg.mark is None or short_leg.mark is None:
        return None

    frac = float(st.get("fill_model_half_spread_fraction", 0.40))
    step = float(st.get("limit_price_rounding", 0.05))
    tp_frac = float(st.get("take_profit_fraction", 0.65))
    roll_days = int(st.get("roll_or_close_days_before_expiry", 45))

    midpoint = round(long_leg.mark - short_leg.mark, 2)
    if midpoint <= 0:
        return None
    # THE LIMIT: the broker's own fill-rate estimates when the legs carry
    # them (high_fill_rate_buy on the long minus high_fill_rate_sell on the
    # short); the modeled mark +/- fraction-of-half-spread only as fallback,
    # and the ticket says which basis produced it.
    if long_leg.high_fill_rate_buy is not None and short_leg.high_fill_rate_sell is not None:
        limit = _round_to(max(long_leg.high_fill_rate_buy - short_leg.high_fill_rate_sell, 0.01), step)
        limit_basis = "fill_rate_fields"
    elif None not in (long_leg.bid, long_leg.ask, short_leg.bid, short_leg.ask):
        fill_long = _fill_estimate(long_leg.mark, long_leg.bid, long_leg.ask, buying=True, fraction=frac)
        fill_short = _fill_estimate(short_leg.mark, short_leg.bid, short_leg.ask, buying=False, fraction=frac)
        limit = _round_to(max(fill_long - fill_short, 0.01), step)
        limit_basis = "modeled"
    else:
        limit = _round_to(max(midpoint, 0.01), step)  # settled marks only
        limit_basis = "prior_session_midpoint"
    worst = (
        round(long_leg.ask - short_leg.bid, 2)
        if (long_leg.ask is not None and short_leg.bid is not None)
        else None
    )
    pricing_basis = (
        "live"
        if (long_leg.pricing_basis == "live" and short_leg.pricing_basis == "live")
        else "prior_session_close"
    )

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

    # --- Robinhood-quote analytics (each None when its inputs are) -----------
    # Theta as a fraction of the debit: "loses X% of its cost over 60 quiet
    # days" is the sentence that makes decay concrete.
    theta_pct_60d = None
    if long_leg.theta is not None and short_leg.theta is not None and limit > 0:
        net_theta = long_leg.theta - short_leg.theta  # both negative for longs
        theta_pct_60d = round(abs(net_theta) * 60.0 / limit * 100.0, 1)
    # Vega crush: P&L per spread if IV falls 20 percent of itself, spot flat.
    vega_crush = None
    if (long_leg.vega is not None and short_leg.vega is not None
            and long_leg.iv is not None):
        net_vega = long_leg.vega - short_leg.vega
        iv_drop_points = long_leg.iv * 0.20 * 100.0  # vega is per 1 IV point
        vega_crush = round(-net_vega * iv_drop_points * 100.0, 0)
    # The exit level: the underlying price where the spread's AT-EXPIRY value
    # equals entry debit + 65% of max gain (labeled at-expiry mapping).
    exit_value = limit + tp_frac * (prob.max_gain / 100.0)
    if direction == "bullish":
        exit_underlying = round(long_leg.strike + exit_value, 2)
    else:
        exit_underlying = round(long_leg.strike - exit_value, 2)
    move_vs_implied = None
    if implied_move_pct is not None and implied_move_pct > 0:
        move_vs_implied = round(abs(move_req) / (implied_move_pct * 100.0), 2)

    if limit_basis == "fill_rate_fields":
        fill_note = (
            "Limit derived from the broker's high-fill-rate estimates "
            "(long buy estimate minus short sell estimate), rounded to five cents; "
            "midpoint and worst case shown for comparison."
        )
    elif limit_basis == "modeled":
        fill_note = (
            f"Limit modeled as mark +/- {frac * 100:.0f}% of the half-spread per leg "
            "(fill-rate fields unavailable on this pricing basis)."
        )
    else:
        fill_note = (
            "Prior-session pricing: limit set at the settled-mark midpoint; live "
            "quotes were dark when this snapshot was taken. Re-price before entering."
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
        fill_model_note=fill_note,
        pricing_basis=pricing_basis,
        limit_basis=limit_basis,
        broker_chance_of_profit_long=long_leg.chance_of_profit_long,
        theta_pct_of_debit_60d=theta_pct_60d,
        vega_crush_pnl=vega_crush,
        implied_move_pct=(
            round(implied_move_pct * 100.0, 2) if implied_move_pct is not None else None
        ),
        move_required_vs_implied=move_vs_implied,
        exit_underlying_at_take_profit=exit_underlying,
        liquidity_notes=tuple(
            liquidity_violations(long_leg, cfg) + liquidity_violations(short_leg, cfg)
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
    if short_leg.mark is None or long_leg.mark is None:
        return None
    frac = float(st.get("fill_model_half_spread_fraction", 0.40))
    step = float(st.get("limit_price_rounding", 0.05))
    tp_frac = float(st.get("take_profit_fraction", 0.65))
    roll_days = int(st.get("roll_or_close_days_before_expiry", 45))

    midpoint = round(short_leg.mark - long_leg.mark, 2)
    if midpoint <= 0:
        return None
    if short_leg.high_fill_rate_sell is not None and long_leg.high_fill_rate_buy is not None:
        limit = _round_to(max(short_leg.high_fill_rate_sell - long_leg.high_fill_rate_buy, 0.01), step)
        limit_basis = "fill_rate_fields"
    elif None not in (short_leg.bid, short_leg.ask, long_leg.bid, long_leg.ask):
        fill_short = _fill_estimate(short_leg.mark, short_leg.bid, short_leg.ask, buying=False, fraction=frac)
        fill_long = _fill_estimate(long_leg.mark, long_leg.bid, long_leg.ask, buying=True, fraction=frac)
        limit = _round_to(max(fill_short - fill_long, 0.01), step)
        limit_basis = "modeled"
    else:
        limit = _round_to(max(midpoint, 0.01), step)
        limit_basis = "prior_session_midpoint"
    worst = (
        round(short_leg.bid - long_leg.ask, 2)
        if (short_leg.bid is not None and long_leg.ask is not None)
        else None
    )
    pricing_basis = (
        "live"
        if (short_leg.pricing_basis == "live" and long_leg.pricing_basis == "live")
        else "prior_session_close"
    )

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
            "Credit limit from the broker fill-rate fields."
            if limit_basis == "fill_rate_fields"
            else f"Credit limit modeled as mark -/+ {frac * 100:.0f}% of the half-spread per leg."
            if limit_basis == "modeled"
            else "Prior-session pricing: credit set at the settled-mark midpoint; re-price before entering."
        ),
        pricing_basis=pricing_basis,
        limit_basis=limit_basis,
        liquidity_notes=tuple(
            liquidity_violations(short_leg, cfg) + liquidity_violations(long_leg, cfg)
        ),
    )
    return ticket, prob


def implied_move_from_straddle(
    call_mark: float | None, put_mark: float | None, spot: float
) -> float | None:
    """The implied move to expiry as a FRACTION of spot: ATM straddle price
    over spot. None when either side lacks a mark -- n/a, never invented."""
    if call_mark is None or put_mark is None or spot <= 0:
        return None
    return (call_mark + put_mark) / spot


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
