from __future__ import annotations

from src.kill_switch import KillSwitch
from src.portfolio import Portfolio
from src.risk_manager import RiskManager
from src.strategy_engine import StrategyEngine


def strategy_config() -> dict:
    return {
        # Crypto lane's own namespace -- present to prove selecting the
        # equities profile never reads or depends on this.
        "strategy": {"active_profile": "balanced_test", "minimum_history_points": 50},
        "profiles": {
            "balanced_test": {
                "buy_when": ["momentum_5_negative"],
                "sell_when": ["rsi_above_70"],
            }
        },
        "equity_strategy": {"active_profile": "equities_core_test", "minimum_history_points": 50},
        "equity_profiles": {
            "equities_core_test": {
                "risk_level": "low",
                "notes": "Trend-confirmed entries for regular-hours equities.",
                "buy_when": ["ema20_above_ema50", "rsi_between_35_and_70", "momentum_5_positive"],
                "sell_when": ["rsi_above_75", "momentum_5_negative", "stop_loss_hit", "take_profit_hit"],
            }
        },
    }


def trading_rules(symbol: str = "AAPL") -> dict:
    return {
        "trading": {"enabled": True, "mode": "paper", "allowed_symbols": [symbol]},
        "risk": {
            "max_trade_amount_usd": 1000.0,
            "max_daily_loss_usd": 1000.0,
            "max_open_positions": 10,
            "max_trades_per_day": 50,
            "min_order_cooldown_seconds": 0,
            "require_cash_available": True,
            "allow_position_scaling": False,
            "allow_margin": False,
            "allow_shorts": False,
        },
        "orders": {"require_stop_loss": True, "require_take_profit": True},
        "exits": {"stop_loss_percent": 2.0, "take_profit_percent": 4.0},
    }


def uptrend_prices(count: int = 60) -> list[float]:
    # Slow enough drift to keep RSI inside equities_core_test's 35-70 band
    # (a steep uptrend pins RSI at 100, which fails rsi_between_35_and_70)
    # while still leaving ema20 > ema50 and momentum positive.
    return [100 + i * 0.03 + ((-1) ** i) * 0.05 for i in range(count)]


def downtrend_prices(count: int = 60) -> list[float]:
    return [100 - i * 0.05 for i in range(count)]


def test_equity_profile_emits_buy_signal_with_recorded_inputs() -> None:
    prices = uptrend_prices()
    signal = StrategyEngine(strategy_config(), trading_rules()).generate_equity_signal(
        "AAPL",
        prices,
        has_open_position=False,
    )

    assert signal.side == "buy"
    assert signal.final_signal == "buy"
    assert signal.profile == "equities_core_test"

    # The inputs behind the decision are all captured on the signal itself.
    assert signal.history_points == len(prices)
    assert signal.current_mid == prices[-1]
    assert signal.ema_20 is not None
    assert signal.ema_50 is not None
    assert signal.rsi_14 is not None
    assert signal.momentum_5 is not None
    assert "momentum_5_positive" in signal.buy_conditions_met
    assert "ema20_above_ema50" in signal.buy_conditions_met
    assert "rsi_between_35_and_70" in signal.buy_conditions_met


def test_equity_profile_is_independent_of_crypto_active_profile() -> None:
    """Changing which crypto profile is active must not change the equities signal.

    balanced_test's buy_when (momentum_5_negative) would fire on a downtrend,
    the opposite of what equities_core_test needs (momentum_5_positive) -- if
    the equities lane were accidentally reading the crypto namespace, an
    uptrend would not produce a buy here.
    """
    config = strategy_config()
    engine = StrategyEngine(config, trading_rules())

    equity_signal = engine.generate_equity_signal("AAPL", uptrend_prices(), has_open_position=False)
    crypto_signal = engine.generate_signal("BTC-USD", uptrend_prices(), has_open_position=False)

    assert equity_signal.profile == "equities_core_test"
    assert crypto_signal.profile == "balanced_test"
    assert equity_signal.side == "buy"


def test_equity_profile_sell_ignored_without_open_position() -> None:
    signal = StrategyEngine(strategy_config(), trading_rules()).generate_equity_signal(
        "AAPL",
        downtrend_prices(),
        has_open_position=False,
    )

    assert signal.side == "hold"
    assert signal.reason == "sell_signal_ignored_no_position"
    assert signal.profile == "equities_core_test"
    assert "momentum_5_negative" in signal.sell_conditions_met


def test_equity_profile_holds_on_insufficient_history() -> None:
    signal = StrategyEngine(strategy_config(), trading_rules()).generate_equity_signal(
        "AAPL",
        [100.0, 101.0, 99.5],
        has_open_position=False,
    )

    assert signal.side == "hold"
    assert signal.reason == "insufficient market data"
    assert signal.history_points == 3
    assert signal.profile == "equities_core_test"


def test_risk_manager_can_evaluate_an_equities_signal(monkeypatch) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "true")
    rules = trading_rules("AAPL")
    signal = StrategyEngine(strategy_config(), rules).generate_equity_signal(
        "AAPL",
        uptrend_prices(),
        has_open_position=False,
    )
    assert signal.side == "buy"

    risk = RiskManager(rules, KillSwitch(stop_file="__no_such_stop_file__", env_var="TRADING_ENABLED"))
    decision = risk.evaluate(
        signal=signal,
        mode="paper",
        notional=250.0,
        portfolio=Portfolio(cash_usd=10_000.0, positions={}),
        daily_summary={"realized_pnl": 0.0, "trade_count": 0},
        has_api_credentials=True,
        order_quantity=250.0 / signal.current_mid,
        current_price=signal.current_mid,
    )

    assert decision.allowed, decision.reasons


def test_risk_manager_blocks_equity_signal_outside_allowlist() -> None:
    rules = trading_rules("MSFT")  # AAPL not allowlisted here
    signal = StrategyEngine(strategy_config(), rules).generate_equity_signal(
        "AAPL",
        uptrend_prices(),
        has_open_position=False,
    )

    risk = RiskManager(rules, KillSwitch(stop_file="__no_such_stop_file__", env_var="TRADING_ENABLED"))
    decision = risk.evaluate(
        signal=signal,
        mode="paper",
        notional=250.0,
        portfolio=Portfolio(cash_usd=10_000.0, positions={}),
        daily_summary={"realized_pnl": 0.0, "trade_count": 0},
        has_api_credentials=True,
        order_quantity=250.0 / signal.current_mid,
        current_price=signal.current_mid,
    )

    assert not decision.allowed
    assert any("not allowlisted" in reason for reason in decision.reasons)
