"""Pure, dependency-free technical indicators and the directional score.

Everything here is a pure function over price arrays -- no I/O, no config
mutation, no network. The analyzer (today's thesis) and the backtest (historical
replay) BOTH derive their signal from `directional_score_at` over the SAME
precomputed series, which is what makes the empirical hit-rate honest: the rule
that fires today is byte-for-byte the rule replayed over history.

ANALYSIS ONLY -- no order path anywhere in this module.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def sma(values: list[float], period: int) -> list[float | None]:
    """Simple moving average, None for the warmup window."""
    out: list[float | None] = [None] * len(values)
    if period <= 0:
        return out
    running = 0.0
    for i, v in enumerate(values):
        running += v
        if i >= period:
            running -= values[i - period]
        if i >= period - 1:
            out[i] = running / period
    return out


def ema(values: list[float], period: int) -> list[float | None]:
    """Exponential moving average seeded with the SMA of the first `period`
    points; None until the seed is available."""
    out: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    k = 2.0 / (period + 1.0)
    prev = seed
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1.0 - k)
        out[i] = prev
    return out


def rsi(values: list[float], period: int = 14) -> list[float | None]:
    """Wilder's RSI. None until `period` deltas are available."""
    out: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) <= period:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        delta = values[i] - values[i - 1]
        gains += max(delta, 0.0)
        losses += max(-delta, 0.0)
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = _rsi_from(avg_gain, avg_loss)
    for i in range(period + 1, len(values)):
        delta = values[i] - values[i - 1]
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = _rsi_from(avg_gain, avg_loss)
    return out


def _rsi_from(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0.0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def macd_hist(
    values: list[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> list[float | None]:
    """MACD histogram (macd_line - signal_line). None during warmup."""
    fast_ema = ema(values, fast)
    slow_ema = ema(values, slow)
    macd_line: list[float | None] = [
        (f - s) if (f is not None and s is not None) else None
        for f, s in zip(fast_ema, slow_ema)
    ]
    # Signal EMA over the defined portion of the macd line.
    defined = [(i, v) for i, v in enumerate(macd_line) if v is not None]
    out: list[float | None] = [None] * len(values)
    if len(defined) < signal:
        return out
    sub_values = [v for _, v in defined]
    sub_signal = ema(sub_values, signal)
    for (idx, macd_v), sig_v in zip(defined, sub_signal):
        if sig_v is not None:
            out[idx] = macd_v - sig_v
    return out


def atr(
    highs: list[float], lows: list[float], closes: list[float], period: int = 14
) -> list[float | None]:
    """Wilder's Average True Range. None until `period` true ranges exist."""
    n = len(closes)
    out: list[float | None] = [None] * n
    if n <= period:
        return out
    trs: list[float] = [highs[0] - lows[0]]
    for i in range(1, n):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    first = sum(trs[1 : period + 1]) / period
    out[period] = first
    prev = first
    for i in range(period + 1, n):
        prev = (prev * (period - 1) + trs[i]) / period
        out[i] = prev
    return out


def realized_vol_at(closes: list[float], idx: int, lookback: int) -> float | None:
    """Stdev of the last `lookback` daily simple returns ending at `idx`."""
    if idx < lookback:
        return None
    rets: list[float] = []
    for i in range(idx - lookback + 1, idx + 1):
        prev = closes[i - 1]
        if prev <= 0:
            return None
        rets.append((closes[i] - prev) / prev)
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var)


@dataclass(frozen=True)
class IndicatorSeries:
    """Precomputed full-length indicator arrays for one symbol's daily bars."""

    closes: list[float]
    highs: list[float]
    lows: list[float]
    ema_fast: list[float | None]
    ema_slow: list[float | None]
    sma_long: list[float | None]
    rsi: list[float | None]
    macd_hist: list[float | None]
    atr: list[float | None]

    @property
    def length(self) -> int:
        return len(self.closes)


def build_series(
    highs: list[float], lows: list[float], closes: list[float], ind_cfg: dict[str, Any]
) -> IndicatorSeries:
    return IndicatorSeries(
        closes=closes,
        highs=highs,
        lows=lows,
        ema_fast=ema(closes, int(ind_cfg.get("ema_fast", 20))),
        ema_slow=ema(closes, int(ind_cfg.get("ema_slow", 50))),
        sma_long=sma(closes, int(ind_cfg.get("sma_long", 200))),
        rsi=rsi(closes, int(ind_cfg.get("rsi_window", 14))),
        macd_hist=macd_hist(
            closes,
            int(ind_cfg.get("macd_fast", 12)),
            int(ind_cfg.get("macd_slow", 26)),
            int(ind_cfg.get("macd_signal", 9)),
        ),
        atr=atr(highs, lows, closes, int(ind_cfg.get("atr_window", 14))),
    )


@dataclass(frozen=True)
class Factor:
    """One conviction factor's raw read and its weighted contribution."""

    name: str
    raw: float  # in [-1, 1], signed toward bullish
    weight: float
    contribution: float  # raw * normalized_weight, in [-1, 1] space


@dataclass(frozen=True)
class DirectionalRead:
    """The directional score at one point in time, with its factor breakdown."""

    score: float  # weighted, in [-1, 1]; >=0 bullish (call), <0 bearish (put)
    factors: tuple[Factor, ...]

    @property
    def direction(self) -> str:
        return "call" if self.score >= 0 else "put"


def directional_score_at(
    series: IndicatorSeries, idx: int, weights: dict[str, Any]
) -> DirectionalRead | None:
    """The core rule. Returns None during indicator warmup (so the backtest can
    only count occurrences on days where the identical live rule could fire).

    Continuous factor reads in [-1, 1], signed toward bullish:
      * trend: EMA_fast vs EMA_slow, blended with close vs SMA_long
      * momentum: MACD histogram, scaled by price
      * rsi: distance of RSI(14) from 50
    """
    ef = series.ema_fast[idx]
    es = series.ema_slow[idx]
    sl = series.sma_long[idx]
    rv = series.rsi[idx]
    hist = series.macd_hist[idx]
    close = series.closes[idx]
    if None in (ef, es, sl, rv, hist) or close <= 0:
        return None

    trend_ema = _clip((ef - es) / (0.02 * close), -1.0, 1.0)
    trend_sma = _clip((close - sl) / (0.05 * close), -1.0, 1.0)
    f_trend = 0.5 * trend_ema + 0.5 * trend_sma
    f_macd = _clip(hist / (0.005 * close), -1.0, 1.0)
    f_rsi = _clip((rv - 50.0) / 25.0, -1.0, 1.0)

    w_trend = float(weights.get("trend", 0.40))
    w_macd = float(weights.get("momentum", 0.30))
    w_rsi = float(weights.get("rsi", 0.30))
    total = w_trend + w_macd + w_rsi
    if total <= 0:
        return None

    n_trend, n_macd, n_rsi = w_trend / total, w_macd / total, w_rsi / total
    score = n_trend * f_trend + n_macd * f_macd + n_rsi * f_rsi
    factors = (
        Factor("trend", f_trend, w_trend, n_trend * f_trend),
        Factor("momentum", f_macd, w_macd, n_macd * f_macd),
        Factor("rsi", f_rsi, w_rsi, n_rsi * f_rsi),
    )
    return DirectionalRead(score=_clip(score, -1.0, 1.0), factors=factors)
