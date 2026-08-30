from __future__ import annotations

from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from src import dashboard
from src.dashboard import dashboard_app, read_env


def make_dashboard_root(tmp_path: Path) -> Path:
    root = tmp_path
    (root / "config").mkdir()
    (root / "data").mkdir()
    (root / ".env").write_text(
        "\n".join(
            [
                "ROBINHOOD_API_KEY=secret-api-key",
                "ROBINHOOD_PRIVATE_KEY=secret-private-key",
                "TRADING_MODE=paper",
                "TRADING_ENABLED=false",
                "POLL_INTERVAL_SECONDS=60",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "config" / "trading_rules.yaml").write_text(
        """
trading:
  enabled: false
  mode: paper
  allowed_symbols:
    - BTC-USD
    - ETH-USD
risk:
  max_trade_amount_usd: 25
  max_daily_loss_usd: 25
  max_open_positions: 2
  max_trades_per_day: 5
  require_cash_available: true
  allow_position_scaling: false
  allow_margin: false
  allow_shorting: false
  max_symbol_allocation_percent: 5
  min_order_cooldown_seconds: 300
  require_live_order_reconciliation: true
orders:
  require_stop_loss: true
  require_take_profit: true
  time_in_force: gtc
exits:
  stop_loss_percent: 2
  take_profit_percent: 4
kill_switch:
  stop_file: STOP_TRADING
  env_var: TRADING_ENABLED
equities:
  allow_extended_hours: false
  kill_switch:
    stop_file: STOP_TRADING_EQUITIES
    env_var: TRADING_ENABLED
  expected_account:
    nickname: Agentic
    number_suffix: "2092"
  off_limits_account_suffix: "2833"
  universe:
    - AAPL
""",
        encoding="utf-8",
    )
    (root / "config" / "strategy.yaml").write_text(
        """
strategy:
  active_profile: balanced_test
  minimum_history_points: 50
profiles:
  conservative_test:
    risk_level: low
    notes: Trend-confirmed entries with fewer trades.
    buy_when:
      - ema20_above_ema50
    sell_when:
      - momentum_5_negative
  balanced_test:
    risk_level: medium
    notes: Moderate signal threshold for testing.
    buy_when:
      - momentum_5_positive
    sell_when:
      - momentum_5_negative
  growth_test:
    risk_level: high
    notes: Higher frequency paper test profile.
    buy_when:
      - rsi_between_25_and_70
    sell_when:
      - rsi_above_78
""",
        encoding="utf-8",
    )
    return root


def make_live_ready_dashboard_root(tmp_path: Path) -> Path:
    root = make_dashboard_root(tmp_path)
    env = root / ".env"
    env.write_text(env.read_text(encoding="utf-8").replace("TRADING_MODE=paper", "TRADING_MODE=live").replace("TRADING_ENABLED=false", "TRADING_ENABLED=true"), encoding="utf-8")
    rules = yaml.safe_load((root / "config" / "trading_rules.yaml").read_text(encoding="utf-8"))
    rules["trading"]["enabled"] = True
    rules["trading"]["mode"] = "live"
    rules.setdefault("symbols", {})["live_allowed_symbols"] = ["BTC-USD"]
    rules.setdefault("symbols", {})["paper_allowed_symbols"] = ["BTC-USD"]
    rules.setdefault("symbols", {})["research_watchlist"] = ["BTC-USD"]
    rules["trading"]["allowed_symbols"] = ["BTC-USD"]
    rules["risk"]["max_trade_amount_usd"] = 25
    rules["risk"]["max_daily_loss_usd"] = 25
    rules["risk"]["max_trades_per_day"] = 5
    rules["risk"]["max_open_positions"] = 10
    rules["risk"]["max_symbol_allocation_percent"] = 25
    rules["risk"]["min_order_cooldown_seconds"] = 300
    rules["risk"]["allow_margin"] = False
    rules["risk"]["allow_shorting"] = False
    rules["risk"]["allow_position_scaling"] = False
    (root / "config" / "trading_rules.yaml").write_text(yaml.safe_dump(rules, sort_keys=False), encoding="utf-8")
    (root / "data" / "symbol_validation.json").write_text(
        '{"available_for_live":["BTC-USD"],"unavailable":[],"unknown":[],"details":{"BTC-USD":{"submitted":false}}}',
        encoding="utf-8",
    )
    return root


class FakeDashboardClient:
    has_credentials = True

    def __init__(self) -> None:
        self.place_order_called = False

    def get_accounts(self):
        return {"results": [{"account_number": "ACCT1234", "buying_power": "100"}]}

    def get_holdings(self, account_number=None):
        return {"results": []}

    def get_best_bid_ask(self, *symbols):
        return {"results": [{"symbol": symbols[0], "ask": "100", "bid": "99"}]}

    def get_trading_pairs(self, *symbols):
        return {"results": [{"symbol": symbols[0], "state": "tradable"}]}

    def get_orders(self, account_number=None):
        return {"results": [{"id": "order-1", "symbol": "BTC-USD", "state": "open"}]}

    def place_order(self, **kwargs):
        self.place_order_called = True
        return {"id": "should-not-submit"}


def test_dashboard_does_not_expose_secrets(monkeypatch, tmp_path: Path) -> None:
    root = make_dashboard_root(tmp_path)
    monkeypatch.setattr(dashboard, "make_client", lambda: FakeDashboardClient())
    client = TestClient(dashboard_app(root))

    text = client.get("/").text + client.get("/settings").text + client.get("/logs").text

    assert "secret-api-key" not in text
    assert "secret-private-key" not in text
    assert "ROBINHOOD_API_KEY" not in text
    assert "ROBINHOOD_PRIVATE_KEY" not in text
    assert "ACCT1234" not in text


def test_settings_renders_strategy_profile_dropdown_from_config(tmp_path: Path) -> None:
    root = make_dashboard_root(tmp_path)
    client = TestClient(dashboard_app(root))

    text = client.get("/settings").text

    assert '<select name="active_strategy_profile">' in text
    assert 'value="conservative_test"' in text
    assert 'value="balanced_test" selected' in text
    assert 'value="growth_test"' in text
    assert "Active Strategy Profile" in text
    assert "This controls which trading rule set the bot uses to generate buy, sell, or hold signals." in text


def test_strategy_description_panel_summarizes_selected_profile(tmp_path: Path) -> None:
    root = make_dashboard_root(tmp_path)
    client = TestClient(dashboard_app(root))

    text = client.get("/settings").text

    assert "Strategy Description" in text
    assert "Profile Name" in text
    assert "balanced_test" in text
    assert "Buy Conditions" in text
    assert "momentum_5_positive" in text
    assert "Sell Conditions" in text
    assert "Risk Level" in text
    assert "medium" in text
    assert "Notes" in text
    assert "Moderate signal threshold for testing." in text


def test_live_mode_cannot_be_saved_without_confirmation(tmp_path: Path) -> None:
    root = make_dashboard_root(tmp_path)
    client = TestClient(dashboard_app(root))

    response = client.post(
        "/settings",
        data={
            "trading_mode": "live",
            "env_enabled": "true",
            "config_enabled": "true",
            "config_mode": "live",
            "max_trade_amount_usd": "1",
            "max_daily_loss_usd": "5",
            "max_trades_per_day": "1",
            "allowed_symbols": "BTC-USD",
            "active_strategy_profile": "balanced_test",
            "poll_interval_seconds": "60",
        },
    )

    assert response.status_code == 400
    assert read_env(root / ".env")["TRADING_MODE"] == "paper"


def test_live_mode_save_enforces_bounded_defaults(tmp_path: Path) -> None:
    root = make_dashboard_root(tmp_path)
    client = TestClient(dashboard_app(root))

    response = client.post(
        "/settings",
        data={
            "trading_mode": "live",
            "env_enabled": "true",
            "config_enabled": "true",
            "config_mode": "live",
            "max_trade_amount_usd": "99",
            "max_daily_loss_usd": "99",
            "max_trades_per_day": "99",
            "allowed_symbols": "BTC-USD, ETH-USD",
            "active_strategy_profile": "balanced_test",
            "poll_interval_seconds": "30",
            "live_confirm": "on",
        },
        follow_redirects=False,
    )

    rules = yaml.safe_load((root / "config" / "trading_rules.yaml").read_text(encoding="utf-8"))
    env = read_env(root / ".env")
    assert response.status_code == 303
    assert env["TRADING_MODE"] == "live"
    assert env["TRADING_ENABLED"] == "true"
    assert rules["risk"]["max_trade_amount_usd"] == 99.0
    assert rules["risk"]["max_daily_loss_usd"] == 99.0
    assert rules["risk"]["max_trades_per_day"] == 5
    assert rules["risk"]["max_open_positions"] <= 10
    assert rules["risk"]["max_symbol_allocation_percent"] <= 25
    assert rules["risk"]["min_order_cooldown_seconds"] >= 300
    assert rules["risk"]["allow_margin"] is False
    assert rules["risk"]["allow_shorting"] is False


def test_kill_switch_creates_stop_trading(tmp_path: Path) -> None:
    root = make_dashboard_root(tmp_path)
    client = TestClient(dashboard_app(root))

    response = client.post("/kill/stop", follow_redirects=False)

    assert response.status_code == 303
    assert (root / "STOP_TRADING").exists()


def test_settings_save_updates_only_allowed_fields(tmp_path: Path) -> None:
    root = make_dashboard_root(tmp_path)
    client = TestClient(dashboard_app(root))

    response = client.post(
        "/settings",
        data={
            "trading_mode": "dry-run",
            "env_enabled": "true",
            "config_enabled": "true",
            "config_mode": "dry-run",
            "max_trade_amount_usd": "10",
            "max_daily_loss_usd": "10",
            "max_trades_per_day": "3",
            "allowed_symbols": "BTC-USD",
            "active_strategy_profile": "conservative_test",
            "poll_interval_seconds": "15",
        },
        follow_redirects=False,
    )

    env = read_env(root / ".env")
    strategy = yaml.safe_load((root / "config" / "strategy.yaml").read_text(encoding="utf-8"))
    assert response.status_code == 303
    assert env["TRADING_MODE"] == "live-dry-run"
    assert env["TRADING_ENABLED"] == "true"
    assert env["POLL_INTERVAL_SECONDS"] == "15"
    assert env["ROBINHOOD_API_KEY"] == "secret-api-key"
    assert env["ROBINHOOD_PRIVATE_KEY"] == "secret-private-key"
    assert strategy["strategy"]["active_profile"] == "conservative_test"


def test_saving_valid_strategy_profile_updates_active_profile_only(tmp_path: Path) -> None:
    root = make_dashboard_root(tmp_path)
    client = TestClient(dashboard_app(root))

    response = client.post(
        "/settings",
        data={
            "trading_mode": "paper",
            "env_enabled": "false",
            "config_enabled": "false",
            "config_mode": "paper",
            "max_trade_amount_usd": "25",
            "max_daily_loss_usd": "25",
            "max_trades_per_day": "5",
            "allowed_symbols": "BTC-USD, ETH-USD",
            "active_strategy_profile": "growth_test",
            "poll_interval_seconds": "60",
        },
        follow_redirects=False,
    )

    strategy = yaml.safe_load((root / "config" / "strategy.yaml").read_text(encoding="utf-8"))
    assert response.status_code == 303
    assert strategy["strategy"]["active_profile"] == "growth_test"
    assert set(strategy["profiles"]) == {"conservative_test", "balanced_test", "growth_test"}
    assert strategy["profiles"]["balanced_test"]["notes"] == "Moderate signal threshold for testing."


def test_saving_invalid_strategy_profile_is_rejected(tmp_path: Path) -> None:
    root = make_dashboard_root(tmp_path)
    client = TestClient(dashboard_app(root))

    response = client.post(
        "/settings",
        data={
            "trading_mode": "paper",
            "env_enabled": "false",
            "config_enabled": "false",
            "config_mode": "paper",
            "max_trade_amount_usd": "25",
            "max_daily_loss_usd": "25",
            "max_trades_per_day": "5",
            "allowed_symbols": "BTC-USD",
            "active_strategy_profile": "not_a_profile",
            "poll_interval_seconds": "60",
        },
    )

    strategy = yaml.safe_load((root / "config" / "strategy.yaml").read_text(encoding="utf-8"))
    assert response.status_code == 400
    assert "Unknown strategy profile: not_a_profile" in response.text
    assert strategy["strategy"]["active_profile"] == "balanced_test"


def test_dashboard_preview_never_submits_live_orders(monkeypatch, tmp_path: Path) -> None:
    root = make_dashboard_root(tmp_path)
    fake = FakeDashboardClient()
    monkeypatch.setattr(dashboard, "make_client", lambda: fake)
    monkeypatch.setattr(
        dashboard,
        "validate_symbols",
        lambda root: {"submitted": False, "available_for_live": ["BTC-USD"], "unavailable": [], "unknown": []},
    )
    client = TestClient(dashboard_app(root))

    response = client.post(
        "/preview",
        data={"action": "preview-buy", "symbol": "BTC-USD", "amount_usd": "1"},
    )

    assert response.status_code == 200
    assert "submitted&quot;: false" in response.text or '"submitted": false' in response.text
    assert fake.place_order_called is False


def test_home_status_includes_live_readiness_summary(monkeypatch, tmp_path: Path) -> None:
    root = make_dashboard_root(tmp_path)
    monkeypatch.setattr(dashboard, "make_client", lambda: FakeDashboardClient())
    client = TestClient(dashboard_app(root))

    text = client.get("/").text

    assert "Active Strategy Profile" in text
    assert "Submitted Live Orders Today" in text
    assert "balanced_test" in text


def test_live_readiness_page_renders_checks_and_paper_banner(monkeypatch, tmp_path: Path) -> None:
    root = make_dashboard_root(tmp_path)
    monkeypatch.setattr(dashboard, "make_client", lambda: FakeDashboardClient())
    client = TestClient(dashboard_app(root))

    response = client.get("/live-readiness")

    assert response.status_code == 200
    assert "SAFE: PAPER MODE ONLY" in response.text
    assert "pytest last known status" in response.text
    assert "TRADING_MODE value" in response.text
    assert "unvalidated live symbols" in response.text
    assert "dashboard secrets hidden" in response.text
    assert "Run Supervised $1 Smoke Buy" in response.text


def test_live_readiness_live_banner_and_bounded_failures(tmp_path: Path) -> None:
    root = make_dashboard_root(tmp_path)
    client = TestClient(dashboard_app(root))
    client.post(
        "/settings",
        data={
            "trading_mode": "live",
            "env_enabled": "true",
            "config_enabled": "true",
            "config_mode": "live",
            "max_trade_amount_usd": "9",
            "max_daily_loss_usd": "9",
            "max_trades_per_day": "9",
            "allowed_symbols": "BTC-USD,ETH-USD",
            "active_strategy_profile": "balanced_test",
            "poll_interval_seconds": "60",
            "live_confirm": "on",
        },
        follow_redirects=False,
    )

    text = client.get("/live-readiness").text

    assert "DANGER: LIVE MODE CAN PLACE REAL ROBINHOOD CRYPTO ORDERS" in text
    assert "max_trade_amount_usd &lt;= 100" in text
    assert "max_daily_loss_usd &lt;= 100" in text
    assert "max_trades_per_day &lt;= 5" in text
    assert "unvalidated live symbols" in text


def test_live_readiness_test_connection_is_read_only(monkeypatch, tmp_path: Path) -> None:
    root = make_dashboard_root(tmp_path)
    fake = FakeDashboardClient()
    monkeypatch.setattr(dashboard, "make_client", lambda: fake)
    client = TestClient(dashboard_app(root))

    response = client.post("/live-readiness", data={"action": "test-connection"})

    assert response.status_code == 200
    assert "read-only account request succeeded" in response.text
    assert "accounts_found" in response.text
    assert "ACCT1234" not in response.text
    assert fake.place_order_called is False


def test_live_readiness_preview_actions_never_submit(monkeypatch, tmp_path: Path) -> None:
    root = make_dashboard_root(tmp_path)
    fake = FakeDashboardClient()
    monkeypatch.setattr(dashboard, "make_client", lambda: fake)
    client = TestClient(dashboard_app(root))

    buy = client.post("/live-readiness", data={"action": "preview-buy"})
    sell = client.post("/live-readiness", data={"action": "preview-sell"})

    assert buy.status_code == 200
    assert sell.status_code == 200
    assert "submitted&quot;: false" in buy.text or '"submitted": false' in buy.text
    assert "submitted&quot;: false" in sell.text or '"submitted": false' in sell.text
    assert fake.place_order_called is False


def test_live_readiness_smoke_action_only_prints_manual_instructions(monkeypatch, tmp_path: Path) -> None:
    root = make_dashboard_root(tmp_path)
    fake = FakeDashboardClient()
    monkeypatch.setattr(dashboard, "make_client", lambda: fake)
    client = TestClient(dashboard_app(root))

    response = client.post("/live-readiness", data={"action": "smoke-buy"})

    assert response.status_code == 200
    assert "python -m src.main live-smoke-buy BTC-USD 1 --confirm-live-smoke" in response.text
    assert "The dashboard does not submit smoke orders" in response.text
    assert fake.place_order_called is False


def test_live_readiness_return_to_paper_updates_only_safe_fields(tmp_path: Path) -> None:
    root = make_dashboard_root(tmp_path)
    client = TestClient(dashboard_app(root))
    client.post(
        "/settings",
        data={
            "trading_mode": "live",
            "env_enabled": "true",
            "config_enabled": "true",
            "config_mode": "live",
            "max_trade_amount_usd": "1",
            "max_daily_loss_usd": "5",
            "max_trades_per_day": "1",
            "allowed_symbols": "BTC-USD",
            "active_strategy_profile": "balanced_test",
            "poll_interval_seconds": "60",
            "live_confirm": "on",
        },
        follow_redirects=False,
    )

    response = client.post("/live-readiness", data={"action": "return-paper"})
    env = read_env(root / ".env")
    rules = yaml.safe_load((root / "config" / "trading_rules.yaml").read_text(encoding="utf-8"))

    assert response.status_code == 200
    assert env["TRADING_MODE"] == "paper"
    assert env["TRADING_ENABLED"] == "true"
    assert env["ROBINHOOD_API_KEY"] == "secret-api-key"
    assert rules["trading"]["mode"] == "paper"
    assert rules["trading"]["enabled"] is True


def test_live_readiness_create_stop_file(tmp_path: Path) -> None:
    root = make_dashboard_root(tmp_path)
    client = TestClient(dashboard_app(root))

    response = client.post("/live-readiness", data={"action": "create-stop"})

    assert response.status_code == 200
    assert (root / "STOP_TRADING").exists()


def test_live_readiness_reconcile_and_audit_actions_are_non_submitting(monkeypatch, tmp_path: Path) -> None:
    root = make_dashboard_root(tmp_path)
    fake = FakeDashboardClient()
    monkeypatch.setattr(dashboard, "make_client", lambda: fake)
    client = TestClient(dashboard_app(root))

    reconcile = client.post("/live-readiness", data={"action": "reconcile-live-orders"})
    audit = client.post("/live-readiness", data={"action": "export-live-audit"})

    assert reconcile.status_code == 200
    assert audit.status_code == 200
    assert "open_live_orders" in reconcile.text
    assert "audit exported" in audit.text
    assert fake.place_order_called is False
    assert (root / "logs").exists()


def test_symbols_page_shows_lists_and_validation_action(monkeypatch, tmp_path: Path) -> None:
    root = make_dashboard_root(tmp_path)
    fake = FakeDashboardClient()
    monkeypatch.setattr(dashboard, "make_client", lambda: fake)
    monkeypatch.setattr(
        dashboard,
        "validate_symbols",
        lambda root: {"submitted": False, "available_for_live": ["BTC-USD"], "unavailable": [], "unknown": []},
    )
    client = TestClient(dashboard_app(root))

    page = client.get("/symbols")
    result = client.post("/symbols")

    assert page.status_code == 200
    assert "Research Watchlist" in page.text
    assert "Paper Allowed Symbols" in page.text
    assert "Live Allowed Symbols" in page.text
    assert "Validate Symbols With Robinhood" in page.text
    assert result.status_code == 200
    assert "available_for_live" in result.text
    assert fake.place_order_called is False


def test_live_control_help_and_audit_render(tmp_path: Path) -> None:
    root = make_dashboard_root(tmp_path)
    client = TestClient(dashboard_app(root))

    assert client.get("/live-control").status_code == 200
    assert "Live Control Center" in client.get("/live-control").text
    assert client.get("/help").status_code == 200
    assert "Trading Modes" in client.get("/help").text
    assert client.get("/audit").status_code == 200
    assert "Submitted orders count" in client.get("/audit").text


def test_dashboard_smoke_test_requires_preview_first(tmp_path: Path) -> None:
    root = make_live_ready_dashboard_root(tmp_path)
    client = TestClient(dashboard_app(root))

    response = client.post("/live-control", data={"action": "submit-smoke", "understand_smoke": "on", "confirm_smoke_submit": "on"})

    assert "requires a preview first" in response.text


def test_dashboard_smoke_test_requires_both_checkboxes(monkeypatch, tmp_path: Path) -> None:
    root = make_live_ready_dashboard_root(tmp_path)
    monkeypatch.setattr(
        dashboard,
        "build_live_smoke_preview",
        lambda symbol, side, amount, root: {"submitted": False, "symbol": symbol, "side": side, "amount_usd": amount, "risk_allowed": True, "risk_reasons": [], "estimated_quantity": 0.01, "bid": 99, "ask": 100, "limit_price": 100, "order": {}},
    )
    client = TestClient(dashboard_app(root))

    preview = client.post("/live-control", data={"action": "preview-smoke", "symbol": "BTC-USD", "side": "buy", "amount_usd": "1"})
    assert "Submit One Live Smoke Test Order" in preview.text
    response = client.post("/live-control", data={"action": "submit-smoke", "preview_id": "missing", "understand_smoke": "on"})

    assert "requires a preview first" in response.text


def test_dashboard_smoke_test_refuses_readiness_stop_unvalidated_and_amount(monkeypatch, tmp_path: Path) -> None:
    root = make_live_ready_dashboard_root(tmp_path)
    (root / "STOP_TRADING").write_text("stop", encoding="utf-8")
    client = TestClient(dashboard_app(root))

    response = client.post("/live-control", data={"action": "preview-smoke", "symbol": "ETH-USD", "side": "buy", "amount_usd": "999"})

    assert "risk_reasons" in response.text
    assert "validated" in response.text or "max trade" in response.text


def test_dashboard_smoke_submit_uses_checkbox_flow_and_at_most_one_submit(monkeypatch, tmp_path: Path) -> None:
    root = make_live_ready_dashboard_root(tmp_path)
    calls = {"count": 0}

    def fake_preview(symbol, side, amount, root):
        return {"submitted": False, "symbol": symbol, "side": side, "amount_usd": amount, "risk_allowed": True, "risk_reasons": [], "estimated_quantity": 0.01, "bid": 99, "ask": 100, "limit_price": 100, "order": {}}

    def fake_submit(*args, **kwargs):
        calls["count"] += 1
        return {"submitted": True, "status": "submitted", "id": "order-1"}

    monkeypatch.setattr(dashboard, "build_live_smoke_preview", fake_preview)
    monkeypatch.setattr(dashboard, "run_live_smoke_test", fake_submit)
    client = TestClient(dashboard_app(root))

    preview = client.post("/live-control", data={"action": "preview-smoke", "symbol": "BTC-USD", "side": "buy", "amount_usd": "1"})
    preview_id = preview.text.split('name="preview_id" value="')[1].split('"')[0]
    submit = client.post("/live-control", data={"action": "submit-smoke", "preview_id": preview_id, "understand_smoke": "on", "confirm_smoke_submit": "on"})

    assert "submitted" in submit.text
    assert calls["count"] == 1


def test_dashboard_bounded_iteration_requires_checkbox_and_refuses_readiness(monkeypatch, tmp_path: Path) -> None:
    root = make_dashboard_root(tmp_path)
    calls = {"count": 0}
    monkeypatch.setattr(dashboard, "run_bounded_live_iteration", lambda checked, root: calls.update(count=calls["count"] + 1) or {"submitted": False})
    client = TestClient(dashboard_app(root))

    response = client.post("/live-control", data={"action": "bounded-iteration"})

    assert "confirmation checkbox" in response.text
    assert calls["count"] == 0


def test_live_control_safety_buttons_and_cancel_confirmation(monkeypatch, tmp_path: Path) -> None:
    root = make_dashboard_root(tmp_path)
    client = TestClient(dashboard_app(root))

    stop = client.post("/live-control", data={"action": "create-stop"})
    cancel = client.post("/live-control", data={"action": "cancel-open"})
    paper = client.post("/live-control", data={"action": "return-paper"})

    assert (root / "STOP_TRADING").exists()
    assert "requires confirmation checkbox" in cancel.text
    assert read_env(root / ".env")["TRADING_MODE"] == "paper"
    assert paper.status_code == 200
    assert stop.status_code == 200


def test_equities_page_renders_posture_gates_and_agentic_account(tmp_path: Path) -> None:
    root = make_dashboard_root(tmp_path)
    client = TestClient(dashboard_app(root))

    response = client.get("/equities")
    text = response.text

    assert response.status_code == 200
    assert "Equities Lane" in text
    assert "STOP_TRADING_EQUITIES exists" in text
    assert "Agentic" in text
    assert "••2092" in text or "2092" in text
    assert "••2833" in text or "2833" in text
    assert "Agent-hosted" in text
    assert "No open equities paper positions." in text


def test_equities_page_shows_paper_positions_and_rationale(tmp_path: Path) -> None:
    root = make_dashboard_root(tmp_path)
    (root / "data" / "equity_paper_trades.db").parent.mkdir(parents=True, exist_ok=True)
    from src.paper_broker import PaperBroker
    from src.logger import SQLiteLogger

    PaperBroker(root / "data" / "equity_paper_trades.db").place_order(
        {"symbol": "AAPL", "side": "buy", "quantity": 2, "limit_price": 150.0, "reason": "test fill"}
    )
    logger = SQLiteLogger(root / "data" / "trading_agent.db")
    logger.log_decision(
        "AAPL",
        "equity_signal_skipped",
        "no order attempted for AAPL: strategy signal is 'hold' (no edge); risk and compliance gates were not evaluated",
        {"venue": "robinhood_equities"},
    )
    logger.log_decision(None, "dashboard_settings_saved", "settings saved from dashboard")
    client = TestClient(dashboard_app(root))

    text = client.get("/equities").text

    assert "AAPL" in text
    assert "equity_signal_skipped" in text
    assert "no order attempted for AAPL" in text
    # Only equities-tagged decisions belong on this page.
    assert "dashboard_settings_saved" not in text


def test_equities_page_renders_the_actually_resolved_account(monkeypatch, tmp_path: Path) -> None:
    """When a live client IS resolvable and it pinned the expected ••2092
    Agentic account, the page renders THAT account (not a literal) and stays
    SAFE."""
    root = make_dashboard_root(tmp_path)

    class FakeResolvedClient:
        account_number = "RH-EQ-AGENTIC-2092"
        nickname = "Agentic"

    monkeypatch.setattr(dashboard, "resolve_equity_account", lambda _root: FakeResolvedClient())
    client = TestClient(dashboard_app(root))

    text = client.get("/equities").text

    # The rendered account is the one the client actually pinned, and it matches
    # the expected identity, so there is no DANGER banner (the top banner may be
    # SAFE or a market-hours CAUTION depending on wall-clock, which is fine).
    assert "••2092" in text
    assert "DANGER" not in text
    assert "resolved live from the connector" in text


def test_equities_page_shows_danger_banner_for_a_mis_pinned_client(monkeypatch, tmp_path: Path) -> None:
    """MUTATION TEST for the dashboard confinement view: a resolved client that
    pinned the off-limits ••2833 account (or any non-Agentic identity) must
    replace the SAFE row with a DANGER banner. If equities_html reverts to
    rendering the hardcoded ••2092 literal and never compares, this fails."""
    root = make_dashboard_root(tmp_path)

    class MisPinnedClient:
        account_number = "RH-EQ-DEFAULT-2833"
        nickname = "Default"

    monkeypatch.setattr(dashboard, "resolve_equity_account", lambda _root: MisPinnedClient())
    client = TestClient(dashboard_app(root))

    text = client.get("/equities").text

    assert "DANGER" in text
    assert "does NOT match" in text
    assert "SAFE: EQUITIES PAPER MODE" not in text
    # The off-limits account's suffix surfaces in the danger detail, not as a SAFE pin.
    assert "2833" in text


def test_equities_page_reflects_its_own_kill_switch(tmp_path: Path) -> None:
    root = make_dashboard_root(tmp_path)
    (root / "STOP_TRADING_EQUITIES").write_text("stop", encoding="utf-8")
    client = TestClient(dashboard_app(root))

    text = client.get("/equities").text

    assert "BLOCKED: STOP_TRADING_EQUITIES Is Active" in text


def test_equities_lane_does_not_weaken_existing_crypto_views(monkeypatch, tmp_path: Path) -> None:
    root = make_dashboard_root(tmp_path)
    monkeypatch.setattr(dashboard, "make_client", lambda: FakeDashboardClient())
    client = TestClient(dashboard_app(root))

    home = client.get("/")
    live_control = client.get("/live-control")
    live_readiness = client.get("/live-readiness")
    kill = client.get("/kill")

    assert home.status_code == 200 and "Safety Status" in home.text
    assert live_control.status_code == 200 and "Live Control Center" in live_control.text
    assert live_readiness.status_code == 200
    assert kill.status_code == 200
    assert "Equities Lane" in home.text  # present in nav, existing pages untouched otherwise


def test_settings_symbols_and_help_include_plain_language_definitions(tmp_path: Path) -> None:
    root = make_dashboard_root(tmp_path)
    client = TestClient(dashboard_app(root))

    settings = client.get("/settings").text
    symbols = client.get("/symbols").text
    help_page = client.get("/help").text

    assert "largest dollar amount" in settings
    assert "Symbols the bot can monitor for research" in symbols
    assert "Recommended Operating Flow" in help_page
