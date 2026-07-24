from __future__ import annotations

import json
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from src import main as app_main
from src.dashboard import dashboard_app
from src.intelligence import (
    apply_intelligence_filter,
    collect_intelligence,
    export_intelligence_report,
    intelligence_status,
    score_all_symbols,
    score_symbol,
)
from src.intelligence.intelligence_store import IntelligenceStore
from src.kill_switch import KillSwitch
from src.portfolio import Portfolio
from src.risk_manager import RiskManager
from src.strategy_engine import TradeSignal


def make_intelligence_root(tmp_path: Path) -> Path:
    root = tmp_path
    (root / "config").mkdir()
    (root / "data").mkdir()
    (root / "logs").mkdir()
    (root / ".env").write_text(
        "\n".join(
            [
                "ROBINHOOD_API_KEY=secret-robinhood-key",
                "ROBINHOOD_PRIVATE_KEY=secret-private-key",
                "CRYPTOPANIC_API_KEY=",
                "COINGECKO_API_KEY=",
                "FRED_API_KEY=",
                "TRADING_MODE=paper",
                "TRADING_ENABLED=false",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "config" / "trading_rules.yaml").write_text(
        """
trading:
  enabled: true
  mode: live
  allowed_symbols:
    - BTC-USD
symbols:
  research_watchlist:
    - BTC-USD
  paper_allowed_symbols:
    - BTC-USD
  live_allowed_symbols:
    - BTC-USD
risk:
  max_trade_amount_usd: 100
  max_daily_loss_usd: 100
  max_open_positions: 10
  max_trades_per_day: 5
  require_cash_available: true
  allow_position_scaling: false
  allow_margin: false
  allow_shorting: false
  max_symbol_allocation_percent: 25
  min_order_cooldown_seconds: 300
  require_live_order_reconciliation: true
orders:
  require_stop_loss: true
  require_take_profit: true
kill_switch:
  stop_file: STOP_TRADING
  env_var: TRADING_ENABLED
""",
        encoding="utf-8",
    )
    (root / "config" / "strategy.yaml").write_text("strategy: {}\n", encoding="utf-8")
    (root / "config" / "intelligence.yaml").write_text(
        yaml.safe_dump(
            {
                "intelligence": {
                    "enabled": True,
                    "use_news_filter": True,
                    "use_macro_filter": True,
                    "use_market_context_filter": True,
                    "use_sentiment_filter": True,
                    "minimum_confidence_to_trade": 60,
                    "scoring": {
                        "micro_signal_weight": 50,
                        "news_sentiment_weight": 20,
                        "macro_risk_weight": 15,
                        "market_context_weight": 15,
                    },
                    "news": {"block_on_severe_negative_news": True, "severe_negative_threshold": -80},
                    "macro": {"risk_off_blocks_new_entries": True},
                    "market_context": {"block_if_market_drawdown_24h_below_percent": -5},
                    "fallback": {"if_intelligence_unavailable": "allow_micro_strategy", "log_missing_data": True},
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    app_main.save_symbol_validation(
        {
            "available_for_live": ["BTC-USD"],
            "unavailable": [],
            "unknown": [],
            "details": {"BTC-USD": {"submitted": False}},
        },
        root,
    )
    return root


def test_collect_intelligence_missing_api_keys_does_not_submit_or_crash(tmp_path: Path) -> None:
    root = make_intelligence_root(tmp_path)

    result = collect_intelligence(root, ["BTC-USD"])

    assert result["submitted"] is False
    assert result["news_status"] == "disabled_missing_api_key"
    assert result["crypto_market_status"] == "disabled_missing_api_key"
    assert result["macro_status"] == "disabled_missing_api_key"


def test_intelligence_status_hides_secrets(tmp_path: Path) -> None:
    root = make_intelligence_root(tmp_path)

    text = json.dumps(intelligence_status(root))

    assert "secret-robinhood-key" not in text
    assert "secret-private-key" not in text
    assert "missing" in text


def test_score_symbol_returns_structured_output_with_missing_data(tmp_path: Path) -> None:
    root = make_intelligence_root(tmp_path)

    score = score_symbol(root, "BTC-USD", "buy", 70)

    assert score["symbol"] == "BTC-USD"
    assert score["submitted"] is False
    assert "combined_intelligence_score" in score
    assert "recommendation" in score
    assert "news_sentiment" in score["missing_data"]


def test_score_all_symbols_handles_missing_data(tmp_path: Path) -> None:
    root = make_intelligence_root(tmp_path)

    result = score_all_symbols(root, ["BTC-USD"])

    assert result["submitted"] is False
    assert len(result["scores"]) == 1


def test_intelligence_score_cannot_override_stop_trading(monkeypatch, tmp_path: Path) -> None:
    root = make_intelligence_root(tmp_path)
    (root / "STOP_TRADING").write_text("stop", encoding="utf-8")
    monkeypatch.setenv("TRADING_ENABLED", "true")
    signal = TradeSignal("BTC-USD", "buy", 0.9, "test", 2, 4, "buy")
    rules = yaml.safe_load((root / "config" / "trading_rules.yaml").read_text(encoding="utf-8"))
    filtered, score = apply_intelligence_filter(root, signal)

    decision = RiskManager(rules, KillSwitch(stop_file=str(root / "STOP_TRADING"))).evaluate(
        signal=filtered,
        mode="live",
        notional=10,
        portfolio=Portfolio(cash_usd=1000),
        daily_summary={"realized_pnl": 0, "trade_count": 0},
        has_api_credentials=True,
        submit_live_order=True,
    )

    assert score["submitted"] is False
    assert not decision.allowed
    assert any("STOP_TRADING" in reason for reason in decision.reasons)


def test_intelligence_score_cannot_override_risk_manager(monkeypatch, tmp_path: Path) -> None:
    root = make_intelligence_root(tmp_path)
    monkeypatch.setenv("TRADING_ENABLED", "true")
    signal = TradeSignal("BTC-USD", "buy", 0.9, "test", 2, 4, "buy")
    rules = yaml.safe_load((root / "config" / "trading_rules.yaml").read_text(encoding="utf-8"))
    filtered, _ = apply_intelligence_filter(root, signal)

    decision = RiskManager(rules, KillSwitch(stop_file=str(root / "STOP_TRADING"))).evaluate(
        signal=filtered,
        mode="live",
        notional=101,
        portfolio=Portfolio(cash_usd=1000),
        daily_summary={"realized_pnl": 0, "trade_count": 0},
        has_api_credentials=True,
        submit_live_order=True,
    )

    assert not decision.allowed
    assert "trade exceeds max trade amount" in decision.reasons


def test_severe_negative_news_blocks_new_entries(tmp_path: Path) -> None:
    root = make_intelligence_root(tmp_path)
    store = IntelligenceStore(root / "data" / "intelligence.db")
    store.save_coin_sentiment("BTC-USD", 0, "negative", 3, {"severe_negative": True})

    score = score_symbol(root, "BTC-USD", "buy", 80)

    assert score["recommendation"] == "block"
    assert any("severe negative news" in reason for reason in score["reasons"])


def test_macro_risk_off_blocks_new_entries(tmp_path: Path) -> None:
    root = make_intelligence_root(tmp_path)
    store = IntelligenceStore(root / "data" / "intelligence.db")
    for series_id in ["FEDFUNDS", "CPIAUCSL", "UNRATE", "DGS10"]:
        store.save_macro_observation({"provider": "fred", "series_id": series_id, "value": 2, "previous_value": 1, "direction": "up"})

    score = score_symbol(root, "BTC-USD", "buy", 80)

    assert score["recommendation"] == "block"
    assert any("macro risk-off" in reason for reason in score["reasons"])


def test_dashboard_intelligence_renders_and_hides_secrets(tmp_path: Path) -> None:
    root = make_intelligence_root(tmp_path)
    client = TestClient(dashboard_app(root))

    text = client.get("/intelligence").text

    assert "Provider Status" in text
    assert "Optional Intelligence API Keys" in text
    assert "CRYPTOPANIC_API_KEY" in text
    assert "COINGECKO_API_KEY" in text
    assert "FRED_API_KEY" in text
    assert "missing" in text
    assert "Last Successful Refresh" in text
    assert "Last Error" in text
    assert "Collect Intelligence Now" in text
    assert "secret-robinhood-key" not in text
    assert "secret-private-key" not in text


def test_live_readiness_and_live_control_show_intelligence_status(tmp_path: Path) -> None:
    root = make_intelligence_root(tmp_path)
    client = TestClient(dashboard_app(root))

    readiness = client.get("/live-readiness").text
    control = client.get("/live-control").text

    assert "Intelligence Enabled" in readiness
    assert "Minimum Confidence To Trade" in readiness
    assert "Intelligence enabled" in control
    assert "Minimum confidence to trade" in control


def test_intelligence_report_exports_without_secrets(tmp_path: Path) -> None:
    root = make_intelligence_root(tmp_path)
    score_symbol(root, "BTC-USD", "buy", 70)

    path = export_intelligence_report(root)
    text = path.read_text(encoding="utf-8")

    assert "Market Intelligence" not in text or "secret-robinhood-key" not in text
    assert "secret-private-key" not in text
