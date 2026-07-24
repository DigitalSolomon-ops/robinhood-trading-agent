from __future__ import annotations

from dataclasses import dataclass
from statistics import fmean
from typing import Any


@dataclass(frozen=True)
class TradeSignal:
    symbol: str
    side: str
    confidence: float
    reason: str
    stop_loss_percent: float | None = None
    take_profit_percent: float | None = None
    strategy_signal: str = "hold"
    history_points: int = 0
    current_mid: float | None = None
    ema_20: float | None = None
    ema_50: float | None = None
    rsi_14: float | None = None
    momentum_5: float | None = None
    profile: str = ""
    buy_conditions_met: tuple[str, ...] = ()
    sell_conditions_met: tuple[str, ...] = ()
    conditions_failed: tuple[str, ...] = ()
    final_signal: str = "hold"


class StrategyEngine:
    def __init__(self, strategy_config: dict, trading_rules: dict) -> None:
        self.strategy_config = strategy_config
        self.trading_rules = trading_rules

    @staticmethod
    def ema(values: list[float], period: int) -> float | None:
        if len(values) < period:
            return None
        multiplier = 2 / (period + 1)
        ema_value = fmean(values[:period])
        for value in values[period:]:
            ema_value = (value - ema_value) * multiplier + ema_value
        return ema_value

    @staticmethod
    def rsi(values: list[float], period: int = 14) -> float | None:
        if len(values) <= period:
            return None
        gains: list[float] = []
        losses: list[float] = []
        for previous, current in zip(values[-period - 1 : -1], values[-period:]):
            delta = current - previous
            gains.append(max(delta, 0))
            losses.append(abs(min(delta, 0)))
        average_gain = fmean(gains)
        average_loss = fmean(losses)
        if average_loss == 0:
            return 100.0
        relative_strength = average_gain / average_loss
        return 100 - (100 / (1 + relative_strength))

    @staticmethod
    def macd(values: list[float]) -> tuple[float | None, float | None]:
        if len(values) < 35:
            return None, None
        macd_series: list[float] = []
        for index in range(26, len(values) + 1):
            window = values[:index]
            ema_12 = StrategyEngine.ema(window, 12)
            ema_26 = StrategyEngine.ema(window, 26)
            if ema_12 is not None and ema_26 is not None:
                macd_series.append(ema_12 - ema_26)
        if len(macd_series) < 9:
            return None, None
        return macd_series[-1], StrategyEngine.ema(macd_series, 9)

    @staticmethod
    def momentum(values: list[float], period: int = 5) -> float | None:
        if len(values) <= period:
            return None
        previous = values[-period - 1]
        if previous == 0:
            return None
        return ((values[-1] - previous) / previous) * 100

    @staticmethod
    def _condition_met(name: str, indicators: dict[str, float | None]) -> bool:
        current = indicators["current"]
        ema_20 = indicators["ema_20"]
        ema_50 = indicators["ema_50"]
        rsi_14 = indicators["rsi_14"]
        momentum_5 = indicators["momentum_5"]
        if name == "ema20_above_ema50":
            return ema_20 is not None and ema_50 is not None and ema_20 > ema_50
        if name == "ema20_below_ema50":
            return ema_20 is not None and ema_50 is not None and ema_20 < ema_50
        if name == "rsi_between_35_and_70":
            return rsi_14 is not None and 35 <= rsi_14 <= 70
        if name == "rsi_between_30_and_65":
            return rsi_14 is not None and 30 <= rsi_14 <= 65
        if name == "rsi_between_25_and_70":
            return rsi_14 is not None and 25 <= rsi_14 <= 70
        if name == "rsi_between_40_and_60":
            return rsi_14 is not None and 40 <= rsi_14 <= 60
        if name == "momentum_5_positive":
            return momentum_5 is not None and momentum_5 > 0
        if name == "momentum_5_negative":
            return momentum_5 is not None and momentum_5 < 0
        if name == "rsi_above_75":
            return rsi_14 is not None and rsi_14 > 75
        if name == "rsi_above_70":
            return rsi_14 is not None and rsi_14 > 70
        if name == "rsi_above_78":
            return rsi_14 is not None and rsi_14 > 78
        if name == "price_above_ema20":
            return current is not None and ema_20 is not None and current > ema_20
        if name == "price_below_ema20":
            return current is not None and ema_20 is not None and current < ema_20
        if name in {"stop_loss_hit", "take_profit_hit"}:
            return False
        return False

    @staticmethod
    def _evaluate_conditions(names: list[str], indicators: dict[str, float | None]) -> tuple[tuple[str, ...], tuple[str, ...]]:
        met: list[str] = []
        failed: list[str] = []
        for name in names:
            if StrategyEngine._condition_met(name, indicators):
                met.append(name)
            else:
                failed.append(name)
        return tuple(met), tuple(failed)

    def _active_profile(self) -> tuple[str, dict[str, Any]]:
        strategy = self.strategy_config.get("strategy", {})
        profiles = self.strategy_config.get("profiles", {})
        name = strategy.get("active_profile", "conservative_test")
        profile = profiles.get(name) or profiles.get("conservative_test") or {}
        return name, profile

    def generate_signal(
        self,
        symbol: str,
        prices: list[float],
        has_open_position: bool = False,
        allow_position_scaling: bool = False,
    ) -> TradeSignal:
        minimum_history = int(self.strategy_config.get("strategy", {}).get("minimum_history_points", 50))
        history_points = len(prices)
        current = prices[-1] if prices else None
        ema_20 = self.ema(prices, 20)
        ema_50 = self.ema(prices, 50)
        rsi_14 = self.rsi(prices)
        momentum_5 = self.momentum(prices)
        profile_name, profile = self._active_profile()
        buy_conditions = list(profile.get("buy_when", []))
        sell_conditions = list(profile.get("sell_when", []))
        indicators = {
            "current": current,
            "ema_20": ema_20,
            "ema_50": ema_50,
            "rsi_14": rsi_14,
            "momentum_5": momentum_5,
        }
        buy_met, buy_failed = self._evaluate_conditions(buy_conditions, indicators)
        sell_met, sell_failed = self._evaluate_conditions(sell_conditions, indicators)

        def signal(
            side: str,
            confidence: float,
            reason: str,
            strategy_signal: str = "hold",
            conditions_failed: tuple[str, ...] | None = None,
        ) -> TradeSignal:
            exits = self.trading_rules.get("exits", {})
            return TradeSignal(
                symbol=symbol,
                side=side,
                confidence=confidence,
                reason=reason,
                stop_loss_percent=float(exits.get("stop_loss_percent", 0) or 0) if side == "buy" else None,
                take_profit_percent=float(exits.get("take_profit_percent", 0) or 0) if side == "buy" else None,
                strategy_signal=strategy_signal,
                history_points=history_points,
                current_mid=current,
                ema_20=ema_20,
                ema_50=ema_50,
                rsi_14=rsi_14,
                momentum_5=momentum_5,
                profile=profile_name,
                buy_conditions_met=buy_met,
                sell_conditions_met=sell_met,
                conditions_failed=conditions_failed if conditions_failed is not None else tuple(sorted(set(buy_failed + sell_failed))),
                final_signal=strategy_signal,
            )

        if history_points < minimum_history:
            return signal("hold", 0.0, "insufficient market data")

        if current is None or ema_20 is None or ema_50 is None or rsi_14 is None or momentum_5 is None:
            return signal("hold", 0.0, "indicator warmup incomplete")

        sell_signal = bool(sell_met)
        buy_signal = bool(buy_conditions) and len(buy_met) == len(buy_conditions)

        if has_open_position:
            if sell_signal:
                return signal("sell", 0.6, "+".join(sell_met), "sell", sell_failed)
            return signal("hold", 0.0, "position_open_no_sell_conditions_met", "hold", sell_failed)

        if sell_signal:
            return signal("hold", 0.0, "sell_signal_ignored_no_position", "hold", buy_failed)

        if buy_signal:
            return signal("buy", 0.6, "+".join(buy_met), "buy", buy_failed)

        if not allow_position_scaling and has_open_position:
            return signal("hold", 0.0, "position_scaling_disabled", "hold", buy_failed)

        return signal("hold", 0.0, "no_entry_conditions_met", "hold", buy_failed)
