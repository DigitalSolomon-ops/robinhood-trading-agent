from __future__ import annotations

from src.strategy_engine import StrategyEngine


def strategy_config() -> dict:
    return {
        "strategy": {"active_profile": "balanced_test", "minimum_history_points": 50},
        "profiles": {
            "balanced_test": {
                "buy_when": ["rsi_between_30_and_65", "momentum_5_positive"],
                "sell_when": ["rsi_above_70", "momentum_5_negative", "stop_loss_hit", "take_profit_hit"],
            }
        },
    }


def trading_rules() -> dict:
    return {"exits": {"stop_loss_percent": 2.0, "take_profit_percent": 4.0}}


def test_profile_generates_buy_from_flat_state() -> None:
    prices = [100 + i * 0.02 + ((-1) ** i) * 0.05 for i in range(60)]
    signal = StrategyEngine(strategy_config(), trading_rules()).generate_signal(
        "BTC-USD",
        prices,
        has_open_position=False,
    )

    assert signal.side == "buy"
    assert signal.profile == "balanced_test"
    assert signal.final_signal == "buy"
    assert "momentum_5_positive" in signal.buy_conditions_met


def test_sell_signal_is_ignored_without_open_position() -> None:
    prices = [100 - i * 0.02 for i in range(60)]
    signal = StrategyEngine(strategy_config(), trading_rules()).generate_signal(
        "BTC-USD",
        prices,
        has_open_position=False,
    )

    assert signal.side == "hold"
    assert signal.reason == "sell_signal_ignored_no_position"
    assert signal.final_signal == "hold"
    assert "momentum_5_negative" in signal.sell_conditions_met
