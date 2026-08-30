from __future__ import annotations

from dataclasses import dataclass, replace
from statistics import fmean
from typing import Any

from .equity_intelligence.indicator_signals import (
    SKIP,
    IndicatorModulation,
    IndicatorProvider,
    IndicatorSnapshot,
    evaluate_indicators,
    indicator_config,
    modulated_confidence,
)


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
    # --- EOD indicator modulation (equities lane) --------------------------
    # `strategy_signal` stays the RULES verdict; `side`/`final_signal` are the
    # verdict after the indicators had their say. These fields record what the
    # indicators saw and what it was taken to mean, so a decision's rationale
    # can cite the numbers rather than assert a conclusion.
    indicator_source: str = ""
    indicator_action: str = ""
    indicator_notes: tuple[str, ...] = ()
    indicator_values: tuple[tuple[str, float], ...] = ()
    indicator_confidence_multiplier: float = 1.0
    base_confidence: float = 0.0


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

    # Lane -> (strategy config key, profiles config key, fallback profile name).
    # Equities get their OWN namespace (equity_strategy/equity_profiles) so
    # selecting an equities profile never reads or depends on the crypto
    # lane's `strategy.active_profile` -- the two lanes' active profiles are
    # independent settings, on purpose.
    _LANE_CONFIG_KEYS = {
        "crypto": ("strategy", "profiles", "conservative_test"),
        "equities": ("equity_strategy", "equity_profiles", "equities_core"),
    }

    def _active_profile(self, lane: str = "crypto") -> tuple[str, dict[str, Any]]:
        strategy_key, profiles_key, fallback_name = self._LANE_CONFIG_KEYS[lane]
        strategy = self.strategy_config.get(strategy_key, {})
        profiles = self.strategy_config.get(profiles_key, {})
        name = strategy.get("active_profile", fallback_name)
        profile = profiles.get(name) or profiles.get(fallback_name) or {}
        return name, profile

    def generate_signal(
        self,
        symbol: str,
        prices: list[float],
        has_open_position: bool = False,
        allow_position_scaling: bool = False,
        lane: str = "crypto",
    ) -> TradeSignal:
        strategy_key = self._LANE_CONFIG_KEYS[lane][0]
        minimum_history = int(self.strategy_config.get(strategy_key, {}).get("minimum_history_points", 50))
        history_points = len(prices)
        current = prices[-1] if prices else None
        ema_20 = self.ema(prices, 20)
        ema_50 = self.ema(prices, 50)
        rsi_14 = self.rsi(prices)
        momentum_5 = self.momentum(prices)
        profile_name, profile = self._active_profile(lane)
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

    # --- EOD indicator modulation (equities lane only) ----------------------

    @property
    def indicator_config(self) -> dict[str, Any]:
        """`equity_indicators:` from config/strategy.yaml over the defaults."""
        return indicator_config(self.strategy_config)

    @staticmethod
    def _indicator_snapshot(
        symbol: str,
        indicators: IndicatorSnapshot | None,
        provider: IndicatorProvider | None,
    ) -> IndicatorSnapshot | None:
        """An explicit snapshot wins; otherwise ask the provider. A provider
        that raises degrades to an errored snapshot -- an indicator feed going
        down must never take the lane down with it."""
        if indicators is not None:
            return indicators
        if provider is None:
            return None
        try:
            return provider.snapshot(symbol)
        except Exception as exc:  # noqa: BLE001 -- degrade, never crash
            return IndicatorSnapshot(symbol=symbol, error=f"{type(exc).__name__}: {exc}")

    def apply_indicator_modulation(self, signal: TradeSignal, modulation: IndicatorModulation) -> TradeSignal:
        """Fold an IndicatorModulation into a rules signal.

        The one-directional guarantee lives here: this method may move an
        actionable side to "hold" and may change `confidence`, and it does
        NOTHING else. It never sets `side` to "buy" or "sell", so no indicator
        reading can create an order the rules did not already ask for, and
        every risk gate, the kill switch and the human confirm-flag still run
        afterwards on whatever comes out.
        """
        snapshot = modulation.snapshot
        recorded = {
            "indicator_source": snapshot.source if snapshot is not None else "",
            "indicator_action": modulation.action,
            "indicator_notes": modulation.notes,
            "indicator_values": snapshot.values() if snapshot is not None else (),
            "indicator_confidence_multiplier": modulation.confidence_multiplier,
            "base_confidence": signal.confidence,
        }

        # A non-actionable rules signal is recorded and returned untouched --
        # there is nothing to modulate, and nothing here may create something.
        if signal.side not in {"buy", "sell"}:
            return replace(signal, **recorded)

        # An EXIT is annotated with what the indicators saw and otherwise left
        # entirely alone -- not skipped, not reweighted, whatever the
        # modulation says. A data feed must never trap an open position.
        if signal.side == "sell":
            return replace(signal, reason=f"{signal.reason} | {modulation.rationale()}", **recorded)

        # From here the signal is an ENTRY, the only thing indicators may act on.
        if modulation.action == SKIP:
            return replace(
                signal,
                side="hold",
                final_signal="hold",
                confidence=0.0,
                stop_loss_percent=None,
                take_profit_percent=None,
                reason=(
                    f"entry skipped by indicators: {modulation.rationale()} "
                    f"| rules signal was '{signal.strategy_signal}' ({signal.reason})"
                ),
                **recorded,
            )

        confidence = modulated_confidence(signal.confidence, modulation, self.indicator_config)
        return replace(
            signal,
            confidence=confidence,
            reason=(
                f"{signal.reason} | {modulation.rationale()} "
                f"| confidence {signal.confidence:.2f} -> {confidence:.2f}"
            ),
            **recorded,
        )

    def generate_equity_signal(
        self,
        symbol: str,
        prices: list[float],
        has_open_position: bool = False,
        allow_position_scaling: bool = False,
        indicators: IndicatorSnapshot | None = None,
        indicator_provider: IndicatorProvider | None = None,
    ) -> TradeSignal:
        """Equities entry point for the shared engine.

        Same conditions and condition machinery as the crypto lane, under the
        equities lane's own profile namespace (equity_strategy /
        equity_profiles in config/strategy.yaml). `prices` is expected to be
        session-bound history from the connector, not a 24/7 series -- the
        engine itself is time-agnostic (it only ever sees a plain price
        list), so the "not 24/7" constraint is the caller's job: feed it
        regular-hours bars only. Market-hours enforcement for the actual
        order lives in RobinhoodEquityBroker, not here.

        `indicators` / `indicator_provider` add the Massive EOD readings
        (SMA/EMA trend filter, RSI bands, MACD cross) as a MODULATION on top
        of that rules verdict, with thresholds from `equity_indicators:` in
        config/strategy.yaml. They can downweight or skip an entry and they
        annotate the rationale with the values used; they can never create an
        order, and every risk gate still runs downstream. With both omitted
        the signal is exactly what it was before indicators existed.
        """
        signal = self.generate_signal(
            symbol,
            prices,
            has_open_position=has_open_position,
            allow_position_scaling=allow_position_scaling,
            lane="equities",
        )
        config = self.indicator_config
        snapshot = self._indicator_snapshot(symbol, indicators, indicator_provider)
        if snapshot is None and not (config.get("enabled") and config.get("require_indicators")):
            # No indicator data and none demanded: the signal is exactly what
            # it was before indicators existed.
            return signal
        # Reaching here with snapshot None means the config demands indicators
        # and none arrived -- evaluate_indicators fails that CLOSED for entries.
        modulation = evaluate_indicators(snapshot, signal.side, signal.confidence, config)
        return self.apply_indicator_modulation(signal, modulation)
