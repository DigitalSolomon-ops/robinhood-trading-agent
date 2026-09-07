from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
import yaml

from .intelligence import (
    apply_intelligence_filter,
    collect_intelligence as collect_intelligence_layer,
    export_intelligence_report as export_intelligence_report_layer,
    intelligence_status as intelligence_status_layer,
    score_all_symbols as score_all_symbols_layer,
    score_symbol as score_symbol_layer,
)
from .equity_readiness import equity_live_readiness, readiness_markdown
from .kill_switch import KillSwitch
from .live_broker import LiveBroker
from .logger import SQLiteLogger
from .market_data import MarketDataService
from .order_manager import OrderManager
from .paper_broker import PaperBroker
from .portfolio import Portfolio
from .risk_manager import RiskManager
from .robinhood_crypto_client import RobinhoodCryptoClient
from .strategy_engine import StrategyEngine, TradeSignal

ROOT = Path(__file__).resolve().parents[1]
LIVE_SMOKE_CONFIRM_TEXT = "I UNDERSTAND THIS WILL PLACE A REAL $1 CRYPTO ORDER"
LIVE_SINGLE_CONFIRM_TEXT = "I UNDERSTAND THIS WILL PLACE A REAL $1 CRYPTO ORDER"
LIVE_CANCEL_CONFIRM_TEXT = "I UNDERSTAND THIS WILL CANCEL OPEN LIVE CRYPTO ORDERS"
DEFAULT_BOUNDED_LIVE_SYMBOLS = [
    "BTC-USD",
    "ETH-USD",
    "SOL-USD",
    "LINK-USD",
    "AVAX-USD",
    "AAVE-USD",
    "UNI-USD",
    "NEAR-USD",
    "ARB-USD",
    "OP-USD",
    "SUI-USD",
    "INJ-USD",
    "SEI-USD",
    "TAO-USD",
    "RENDER-USD",
    "ONDO-USD",
    "GRT-USD",
    "TIA-USD",
    "KAS-USD",
    "HYPE-USD",
]


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def runtime_mode(command_mode: str | None, rules: dict[str, Any]) -> str:
    env_mode = os.getenv("TRADING_MODE", "").strip() or rules.get("trading", {}).get("mode", "paper")
    return command_mode or env_mode


def load_settings() -> tuple[dict[str, Any], dict[str, Any]]:
    load_dotenv(ROOT / ".env")
    rules = load_yaml(ROOT / "config" / "trading_rules.yaml")
    strategy = load_yaml(ROOT / "config" / "strategy.yaml")
    return rules, strategy


def load_settings_for_root(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    load_dotenv(root / ".env", override=True)
    return load_yaml(root / "config" / "trading_rules.yaml"), load_yaml(root / "config" / "strategy.yaml")


def load_intelligence_settings(root: Path = ROOT) -> dict[str, Any]:
    path = root / "config" / "intelligence.yaml"
    if not path.exists():
        return {"intelligence": {"enabled": False}}
    return load_yaml(path)


def symbol_lists(rules: dict[str, Any]) -> dict[str, list[str]]:
    configured = rules.get("symbols", {})
    trading_symbols = rules.get("trading", {}).get("allowed_symbols", [])
    fallback = [str(symbol).upper() for symbol in (trading_symbols or DEFAULT_BOUNDED_LIVE_SYMBOLS)]
    return {
        "research_watchlist": [str(symbol).upper() for symbol in configured.get("research_watchlist", fallback)],
        "paper_allowed_symbols": [str(symbol).upper() for symbol in configured.get("paper_allowed_symbols", fallback)],
        "live_allowed_symbols": [str(symbol).upper() for symbol in configured.get("live_allowed_symbols", fallback)],
    }


def allowed_symbols_for_mode(mode: str, rules: dict[str, Any]) -> list[str]:
    lists = symbol_lists(rules)
    if mode == "paper":
        return lists["paper_allowed_symbols"]
    if mode in {"live", "live-dry-run"}:
        return lists["live_allowed_symbols"]
    return [str(symbol).upper() for symbol in rules.get("trading", {}).get("allowed_symbols", [])]


def validation_path(root: Path = ROOT) -> Path:
    return root / "data" / "symbol_validation.json"


def load_symbol_validation(root: Path = ROOT) -> dict[str, Any]:
    path = validation_path(root)
    if not path.exists():
        return {"available_for_live": [], "unavailable": [], "unknown": [], "details": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"available_for_live": [], "unavailable": [], "unknown": [], "details": {}}


def save_symbol_validation(result: dict[str, Any], root: Path = ROOT) -> None:
    path = validation_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2), encoding="utf-8")


def validated_live_symbols(rules: dict[str, Any], root: Path = ROOT) -> list[str]:
    live_symbols = set(symbol_lists(rules)["live_allowed_symbols"])
    validation = load_symbol_validation(root)
    return [symbol for symbol in validation.get("available_for_live", []) if symbol in live_symbols]


def make_client() -> RobinhoodCryptoClient:
    return RobinhoodCryptoClient(
        api_key=os.getenv("ROBINHOOD_API_KEY", ""),
        private_key_base64=os.getenv("ROBINHOOD_PRIVATE_KEY", ""),
        base_url=os.getenv("ROBINHOOD_BASE_URL", "https://trading.robinhood.com"),
        api_version=os.getenv("ROBINHOOD_API_VERSION", "v2"),
    )


def kill_switch_for_root(rules: dict[str, Any], root: Path) -> KillSwitch:
    return KillSwitch(
        stop_file=_root_stop_file(rules, root),
        env_var=rules.get("kill_switch", {}).get("env_var", "TRADING_ENABLED"),
    )


def init_project() -> None:
    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "logs").mkdir(exist_ok=True)
    SQLiteLogger(ROOT / "data" / "trading_agent.db")
    PaperBroker(ROOT / "data" / "paper_trades.db")
    env_path = ROOT / ".env"
    if not env_path.exists():
        shutil.copyfile(ROOT / ".env.example", env_path)
    print(f"Initialized {ROOT}")


def stop_trading(rules: dict[str, Any]) -> None:
    # ROOT-anchored so the stop file is CREATED at the same fixed location every
    # loop CHECKS it, regardless of the CWD the CLI was invoked from.
    kill = _project_kill_switch(rules)
    path = kill.create_stop_file()
    print(f"Created {path}. New orders are halted.")


def write_env_allowed(path: Path, updates: dict[str, str]) -> None:
    allowed = {"TRADING_MODE", "TRADING_ENABLED", "POLL_INTERVAL_SECONDS"}
    existing_lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: set[str] = set()
    output: list[str] = []
    for line in existing_lines:
        if not line or line.strip().startswith("#") or "=" not in line:
            output.append(line)
            continue
        key, _ = line.split("=", 1)
        key = key.strip()
        if key in allowed and key in updates:
            output.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            output.append(line)
    for key, value in updates.items():
        if key in allowed and key not in seen:
            output.append(f"{key}={value}")
    path.write_text("\n".join(output) + "\n", encoding="utf-8")


def status() -> None:
    rules, _ = load_settings()
    logger = SQLiteLogger(ROOT / "data" / "trading_agent.db")
    paper_broker = PaperBroker(ROOT / "data" / "paper_trades.db")
    kill = KillSwitch(
        stop_file=rules.get("kill_switch", {}).get("stop_file", "STOP_TRADING"),
        env_var=rules.get("kill_switch", {}).get("env_var", "TRADING_ENABLED"),
    )
    mode = runtime_mode(None, rules)
    summary = logger.get_daily_summary()
    paper_portfolio = paper_broker.get_portfolio()
    external_positions = 0
    client = make_client()
    if client.has_credentials:
        try:
            account_payload = client.get_accounts()
            account_number = select_account_number(account_payload)
            holdings_payload = client.get_holdings(account_number)
            external_positions = Portfolio.from_robinhood(account_payload, holdings_payload).open_position_count
        except Exception as exc:
            logger.log_error("status_external_positions", str(exc))
    print(f"mode={mode}")
    print(f"env_trading_enabled={kill.trading_env_enabled()}")
    print(f"config_trading_enabled={rules.get('trading', {}).get('enabled', False)}")
    print(f"stop_file_exists={kill.stop_file_exists()}")
    print(f"open_paper_positions={paper_portfolio.open_position_count}")
    print(f"open_external_robinhood_positions={external_positions}")
    print(f"daily_trade_count={summary['trade_count']}")
    print(f"daily_realized_pnl={summary['realized_pnl']}")
    print(f"daily_blocked_count={summary['blocked_count']}")
    print(f"last_decision={logger.get_last_decision()}")


def select_account_number(payload: Any) -> str | None:
    if isinstance(payload, dict):
        results = payload.get("results")
        if isinstance(results, list) and results:
            return results[0].get("account_number")
    return None


def _payload_results(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        results = payload.get("results")
        if isinstance(results, list):
            return [item for item in results if isinstance(item, dict)]
        return [payload]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def _best_price_from_payload(payload: Any, symbol: str, side: str) -> float | None:
    preferred_keys = ["ask", "ask_inclusive_of_buy_spread", "price"] if side == "buy" else ["bid", "bid_inclusive_of_sell_spread", "price"]
    for entry in _payload_results(payload):
        if entry.get("symbol") != symbol:
            continue
        for key in preferred_keys:
            if entry.get(key) is not None:
                return float(entry[key])
    return None


def risk_portfolio_for_mode(mode: str, rules: dict[str, Any], paper_portfolio: Portfolio, external_portfolio: Portfolio) -> Portfolio:
    if mode == "paper":
        return paper_portfolio
    if rules.get("risk", {}).get("count_existing_robinhood_holdings", False):
        return external_portfolio
    return Portfolio(cash_usd=external_portfolio.cash_usd, positions={})


def test_connection() -> None:
    load_dotenv(ROOT / ".env")
    logger = SQLiteLogger(ROOT / "data" / "trading_agent.db")
    client = make_client()
    if not client.has_credentials:
        logger.log_decision(None, "test_connection_failed", "Robinhood API credentials are missing")
        raise SystemExit("Missing ROBINHOOD_API_KEY or ROBINHOOD_PRIVATE_KEY in .env")

    try:
        payload = client.get_accounts()
    except Exception as exc:
        logger.log_error("test_connection", str(exc))
        raise SystemExit(f"Read-only account request failed: {exc}") from exc

    accounts = _payload_results(payload)
    print("Read-only account request succeeded.")
    print(f"api_version={client.api_version}")
    print(f"accounts_found={len(accounts)}")
    if accounts:
        account = accounts[0]
        account_number = str(account.get("account_number", ""))
        masked = f"...{account_number[-4:]}" if account_number else "unavailable"
        buying_power = account.get("buying_power") or account.get("cash_available_for_trading") or "unavailable"
        status_value = account.get("status") or account.get("state") or "unavailable"
        print(f"account_number={masked}")
        print(f"status={status_value}")
        print(f"buying_power={buying_power}")
    logger.log_decision(None, "test_connection_succeeded", "read-only account request completed", {"accounts_found": len(accounts)})


def _symbol_available_in_pairs(symbol: str, pairs_payload: Any) -> bool | None:
    rows = _payload_results(pairs_payload)
    if not rows:
        return None
    for row in rows:
        row_symbol = row.get("symbol") or row.get("asset_pair") or row.get("id")
        if row_symbol != symbol:
            continue
        state = str(row.get("state") or row.get("status") or row.get("tradability") or "tradable").lower()
        if state in {"unavailable", "paused", "delisted", "inactive", "disabled"}:
            return False
        return True
    return False


def validate_symbols(root: Path = ROOT) -> dict[str, Any]:
    load_dotenv(root / ".env", override=True)
    rules = load_yaml(root / "config" / "trading_rules.yaml")
    logger = SQLiteLogger(root / "data" / "trading_agent.db")
    client = make_client()
    if not client.has_credentials:
        logger.log_decision(None, "symbol_validation_failed", "Robinhood API credentials are missing")
        raise SystemExit("Missing ROBINHOOD_API_KEY or ROBINHOOD_PRIVATE_KEY in .env")

    live_symbols = symbol_lists(rules)["live_allowed_symbols"]
    available: list[str] = []
    unavailable: list[str] = []
    unknown: list[str] = []
    details: dict[str, Any] = {}

    try:
        client.get_accounts()
    except Exception as exc:
        logger.log_error("symbol_validation_account", str(exc))
        raise SystemExit(f"Read-only account validation failed: {exc}") from exc

    for symbol in live_symbols:
        symbol_detail: dict[str, Any] = {"submitted": False}
        pair_available: bool | None = None
        try:
            pairs_payload = client.get_trading_pairs(symbol)
            pair_available = _symbol_available_in_pairs(symbol, pairs_payload)
            symbol_detail["pair_available"] = pair_available
        except Exception as exc:
            symbol_detail["pair_error"] = str(exc)

        try:
            market_payload = client.get_best_bid_ask(symbol)
            bid = _best_price_from_payload(market_payload, symbol, "sell")
            ask = _best_price_from_payload(market_payload, symbol, "buy")
            symbol_detail["has_bid_ask"] = bool(bid and ask)
            if pair_available is False:
                unavailable.append(symbol)
            elif bid and ask and pair_available is not False:
                available.append(symbol)
            elif pair_available is False:
                unavailable.append(symbol)
            else:
                unknown.append(symbol)
        except Exception as exc:
            symbol_detail["market_error"] = str(exc)
            if pair_available is False:
                unavailable.append(symbol)
            else:
                unknown.append(symbol)
        details[symbol] = symbol_detail

    result = {
        "validated_at": datetime.now(UTC).isoformat(),
        "submitted": False,
        "available_for_live": available,
        "unavailable": unavailable,
        "unknown": unknown,
        "details": details,
    }
    save_symbol_validation(result, root)
    logger.log_decision(None, "symbols_validated", "Robinhood read-only symbol validation completed", _scrub_sensitive(result))
    print(yaml.safe_dump({key: result[key] for key in ("available_for_live", "unavailable", "unknown")}, sort_keys=False))
    return result


def preview_order(symbol: str, side: str, amount_usd: float) -> None:
    rules, _ = load_settings()
    logger = SQLiteLogger(ROOT / "data" / "trading_agent.db")
    client = make_client()
    if not client.has_credentials:
        logger.log_decision(symbol, "preview_order_failed", "Robinhood API credentials are missing")
        raise SystemExit("Missing ROBINHOOD_API_KEY or ROBINHOOD_PRIVATE_KEY in .env")

    try:
        market_payload = client.get_best_bid_ask(symbol)
    except Exception as exc:
        logger.log_error("preview_order_market_data", str(exc), {"symbol": symbol})
        raise SystemExit(f"Market data request failed: {exc}") from exc

    price = _best_price_from_payload(market_payload, symbol, side)
    if price is None:
        logger.log_decision(symbol, "preview_order_failed", "no market price returned")
        raise SystemExit(f"No market price returned for {symbol}")

    account_payload: Any = {}
    holdings_payload: Any = {}
    account_number: str | None = None
    try:
        account_payload = client.get_accounts()
        account_number = select_account_number(account_payload)
        holdings_payload = client.get_holdings(account_number)
    except Exception as exc:
        logger.log_error("preview_order_account_lookup", str(exc), {"symbol": symbol})

    external_portfolio = Portfolio.from_robinhood(account_payload, holdings_payload)
    paper_portfolio = PaperBroker(ROOT / "data" / "paper_trades.db").get_portfolio()
    mode = runtime_mode(None, rules)
    portfolio = risk_portfolio_for_mode(mode, rules, paper_portfolio, external_portfolio)
    kill = KillSwitch(
        stop_file=rules.get("kill_switch", {}).get("stop_file", "STOP_TRADING"),
        env_var=rules.get("kill_switch", {}).get("env_var", "TRADING_ENABLED"),
    )
    signal = TradeSignal(
        symbol=symbol,
        side=side,
        confidence=0.0,
        reason="manual_preview",
        stop_loss_percent=float(rules.get("exits", {}).get("stop_loss_percent", 0) or 0),
        take_profit_percent=float(rules.get("exits", {}).get("take_profit_percent", 0) or 0),
        strategy_signal="manual_preview",
    )
    decision = RiskManager(rules, kill).evaluate(
        signal=signal,
        mode=mode,
        notional=amount_usd,
        portfolio=portfolio,
        daily_summary=logger.get_daily_summary(),
        has_api_credentials=client.has_credentials,
        submit_live_order=False,
        order_quantity=amount_usd / price,
        current_price=price,
        last_order_timestamp=(logger.get_last_order(symbol) or {}).get("timestamp"),
    )
    quantity = amount_usd / price
    order_payload = {
        "client_order_id": str(uuid.uuid4()),
        "symbol": symbol,
        "side": side,
        "type": "limit",
        "limit_order_config": {
            "asset_quantity": f"{quantity:.8f}",
            "limit_price": f"{price:.8f}",
            "time_in_force": rules.get("orders", {}).get("time_in_force", "gtc"),
        },
        "notional_preview_usd": round(amount_usd, 2),
        "account_number_suffix": account_number[-4:] if account_number else None,
    }
    result = {
        "submitted": False,
        "risk_allowed": decision.allowed,
        "risk_reasons": decision.reasons,
        "market_price": price,
        "estimated_quantity": quantity,
        "order_payload": order_payload,
    }
    logger.log_decision(symbol, "preview_order", "preview generated without submitting", result)
    print(json.dumps(result, indent=2))


def dry_run_preview_once() -> None:
    rules, _ = load_settings()
    logger = SQLiteLogger(ROOT / "data" / "trading_agent.db")
    client = make_client()
    if not client.has_credentials:
        logger.log_decision(None, "dry_run_order_preview_failed", "Robinhood API credentials are missing")
        raise SystemExit("Missing ROBINHOOD_API_KEY or ROBINHOOD_PRIVATE_KEY in .env")

    symbols = rules.get("trading", {}).get("allowed_symbols", [])
    if not symbols:
        logger.log_decision(None, "dry_run_order_preview_failed", "no allowed symbols configured")
        raise SystemExit("No allowed symbols configured")

    symbol = symbols[0]
    side = "buy"
    amount_usd = float(rules.get("risk", {}).get("max_trade_amount_usd", 25))

    account_payload = client.get_accounts()
    account_number = select_account_number(account_payload)
    holdings_payload = client.get_holdings(account_number)
    external_portfolio = Portfolio.from_robinhood(account_payload, holdings_payload)
    portfolio = risk_portfolio_for_mode("live-dry-run", rules, PaperBroker(ROOT / "data" / "paper_trades.db").get_portfolio(), external_portfolio)
    market_payload = client.get_best_bid_ask(symbol)
    price = _best_price_from_payload(market_payload, symbol, side)
    if price is None:
        logger.log_decision(symbol, "dry_run_order_preview_failed", "no market price returned")
        raise SystemExit(f"No market price returned for {symbol}")

    kill = KillSwitch(
        stop_file=rules.get("kill_switch", {}).get("stop_file", "STOP_TRADING"),
        env_var=rules.get("kill_switch", {}).get("env_var", "TRADING_ENABLED"),
    )
    signal = TradeSignal(
        symbol=symbol,
        side=side,
        confidence=0.0,
        reason="dry_run_preview",
        stop_loss_percent=float(rules.get("exits", {}).get("stop_loss_percent", 0) or 0),
        take_profit_percent=float(rules.get("exits", {}).get("take_profit_percent", 0) or 0),
        strategy_signal="dry_run_preview",
    )
    decision = RiskManager(rules, kill).evaluate(
        signal=signal,
        mode="live-dry-run",
        notional=amount_usd,
        portfolio=portfolio,
        daily_summary=logger.get_daily_summary(),
        has_api_credentials=client.has_credentials,
        submit_live_order=False,
        order_quantity=amount_usd / price,
        current_price=price,
        last_order_timestamp=(logger.get_last_order(symbol) or {}).get("timestamp"),
    )
    quantity = amount_usd / price
    result = {
        "submitted": False,
        "mode": "live-dry-run",
        "risk_allowed": decision.allowed,
        "risk_reasons": decision.reasons,
        "market_price": price,
        "estimated_quantity": quantity,
        "order_payload": {
            "client_order_id": str(uuid.uuid4()),
            "symbol": symbol,
            "side": side,
            "type": "limit",
            "limit_order_config": {
                "asset_quantity": f"{quantity:.8f}",
                "limit_price": f"{price:.8f}",
                "time_in_force": rules.get("orders", {}).get("time_in_force", "gtc"),
            },
            "notional_preview_usd": round(amount_usd, 2),
            "account_number_suffix": account_number[-4:] if account_number else None,
        },
    }
    logger.log_decision(symbol, "dry_run_order_preview", "dry-run preview generated without submitting", result)
    print(json.dumps(result, indent=2))


def _preview_daily_summary(logger: SQLiteLogger) -> dict[str, Any]:
    summary = logger.get_daily_summary()
    return {
        "realized_pnl": summary["realized_pnl"],
        "trade_count": 0,
        "blocked_count": summary["blocked_count"],
    }


def _manual_preview_signal(rules: dict[str, Any], symbol: str, side: str, reason: str) -> TradeSignal:
    return TradeSignal(
        symbol=symbol,
        side=side,
        confidence=0.0,
        reason=reason,
        stop_loss_percent=float(rules.get("exits", {}).get("stop_loss_percent", 0) or 0),
        take_profit_percent=float(rules.get("exits", {}).get("take_profit_percent", 0) or 0),
        strategy_signal=reason,
    )


def preview_side(symbol: str, side: str, amount_usd: float) -> None:
    rules, _ = load_settings()
    logger = SQLiteLogger(ROOT / "data" / "trading_agent.db")
    client = make_client()
    if not client.has_credentials:
        logger.log_decision(symbol, f"preview_{side}_failed", "Robinhood API credentials are missing")
        raise SystemExit("Missing ROBINHOOD_API_KEY or ROBINHOOD_PRIVATE_KEY in .env")

    try:
        market_payload = client.get_best_bid_ask(symbol)
    except Exception as exc:
        logger.log_error(f"preview_{side}_market_data", str(exc), {"symbol": symbol})
        raise SystemExit(f"Market data request failed: {exc}") from exc

    price = _best_price_from_payload(market_payload, symbol, side)
    if price is None:
        logger.log_decision(symbol, f"preview_{side}_failed", "no market price returned")
        raise SystemExit(f"No market price returned for {symbol}")

    paper_broker = PaperBroker(ROOT / "data" / "paper_trades.db")
    portfolio = paper_broker.get_portfolio()
    kill = KillSwitch(
        stop_file=rules.get("kill_switch", {}).get("stop_file", "STOP_TRADING"),
        env_var=rules.get("kill_switch", {}).get("env_var", "TRADING_ENABLED"),
    )
    risk = RiskManager(rules, kill)
    orders = OrderManager(rules, risk, logger, paper_broker, LiveBroker(client, dry_run=True))
    signal = _manual_preview_signal(rules, symbol, side, f"preview_{side}")
    order = orders.build_limit_order(signal, price, portfolio, amount_usd=amount_usd)
    decision = risk.evaluate(
        signal=signal,
        mode="live-dry-run",
        notional=float(order["notional"]),
        portfolio=portfolio,
        daily_summary=_preview_daily_summary(logger),
        has_api_credentials=client.has_credentials,
        submit_live_order=False,
        order_quantity=float(order["quantity"]),
        current_price=price,
        last_order_timestamp=(logger.get_last_order(symbol) or {}).get("timestamp"),
    )

    result: dict[str, Any] = {
        "submitted": False,
        "mode": "live-dry-run",
        "symbol": symbol,
        "side": side,
        "requested_notional_usd": amount_usd,
        "market_price": price,
        "risk_allowed": decision.allowed,
        "risk_reasons": decision.reasons,
        "paper_position_quantity": portfolio.quantity_for(symbol),
        "order_payload": None,
    }
    if decision.allowed:
        broker_result = LiveBroker(client, dry_run=True).place_limit_order(order)
        result.update(
            {
                "status": "dry_run_order_preview",
                "estimated_quantity": order["quantity"],
                "order_payload": broker_result.get("order_payload"),
            }
        )
        logger.log_decision(symbol, f"preview_{side}", f"{side} preview generated without submitting", result)
    else:
        result["status"] = "blocked"
        reason = "; ".join(decision.reasons)
        logger.log_risk_block(symbol, side, reason, order)
        logger.log_decision(symbol, f"preview_{side}_blocked", reason, result)

    print(json.dumps(result, indent=2))


def seed_paper_position(symbol: str, amount_usd: float) -> None:
    rules, _ = load_settings()
    logger = SQLiteLogger(ROOT / "data" / "trading_agent.db")
    client = make_client()
    if not client.has_credentials:
        logger.log_decision(symbol, "seed_paper_position_failed", "Robinhood API credentials are missing")
        raise SystemExit("Missing ROBINHOOD_API_KEY or ROBINHOOD_PRIVATE_KEY in .env")

    market_payload = client.get_best_bid_ask(symbol)
    price = _best_price_from_payload(market_payload, symbol, "buy")
    if price is None:
        logger.log_decision(symbol, "seed_paper_position_failed", "no market price returned")
        raise SystemExit(f"No market price returned for {symbol}")

    paper_broker = PaperBroker(ROOT / "data" / "paper_trades.db")
    portfolio = paper_broker.get_portfolio()
    kill = KillSwitch(
        stop_file=rules.get("kill_switch", {}).get("stop_file", "STOP_TRADING"),
        env_var=rules.get("kill_switch", {}).get("env_var", "TRADING_ENABLED"),
    )
    risk = RiskManager(rules, kill)
    orders = OrderManager(rules, risk, logger, paper_broker)
    signal = _manual_preview_signal(rules, symbol, "buy", "seed_paper_position")
    order = orders.build_limit_order(signal, price, portfolio, amount_usd=amount_usd)
    decision = risk.evaluate(
        signal=signal,
        mode="paper",
        notional=float(order["notional"]),
        portfolio=portfolio,
        daily_summary=_preview_daily_summary(logger),
        has_api_credentials=client.has_credentials,
        submit_live_order=False,
        order_quantity=float(order["quantity"]),
        current_price=price,
        last_order_timestamp=(logger.get_last_order(symbol) or {}).get("timestamp"),
    )
    if not decision.allowed:
        result = {
            "submitted": False,
            "seeded": False,
            "status": "blocked",
            "symbol": symbol,
            "risk_allowed": False,
            "risk_reasons": decision.reasons,
        }
        reason = "; ".join(decision.reasons)
        logger.log_risk_block(symbol, "buy", reason, order)
        logger.log_decision(symbol, "seed_paper_position_blocked", reason, result)
        print(json.dumps(result, indent=2))
        return

    result = paper_broker.place_order(order)
    logger.log_order(result)
    output = {
        "submitted": False,
        "seeded": True,
        "status": "paper_position_seeded",
        "symbol": symbol,
        "side": "buy",
        "quantity": result["quantity"],
        "price": result["price"],
        "notional": result["notional"],
    }
    logger.log_decision(symbol, "paper_position_seeded", "simulated paper position created", output)
    print(json.dumps(output, indent=2))


def _live_smoke_refuse(logger: SQLiteLogger, symbol: str, reason: str, details: dict[str, Any] | None = None) -> None:
    logger.log_decision(symbol, "live_smoke_refused", reason, details or {})
    raise SystemExit(f"Refusing live smoke order: {reason}")


def _project_stop_file(rules: dict[str, Any]) -> str:
    stop_file = rules.get("kill_switch", {}).get("stop_file", "STOP_TRADING")
    stop_path = Path(stop_file)
    if stop_path.is_absolute():
        return str(stop_path)
    return str(ROOT / stop_path)


def _root_stop_file(rules: dict[str, Any], root: Path) -> str:
    stop_file = rules.get("kill_switch", {}).get("stop_file", "STOP_TRADING")
    stop_path = Path(stop_file)
    if stop_path.is_absolute():
        return str(stop_path)
    return str(root / stop_path)


def _project_kill_switch(rules: dict[str, Any]) -> KillSwitch:
    return KillSwitch(
        stop_file=_project_stop_file(rules),
        env_var=rules.get("kill_switch", {}).get("env_var", "TRADING_ENABLED"),
    )


def _live_smoke_gate_reasons(
    rules: dict[str, Any],
    kill: KillSwitch,
    symbol: str,
    amount_usd: float,
    confirm_live_smoke: bool,
    has_api_credentials: bool,
) -> list[str]:
    reasons: list[str] = []
    trading = rules.get("trading", {})
    if not confirm_live_smoke:
        reasons.append("--confirm-live-smoke is required")
    if amount_usd <= 0:
        reasons.append("amount must be greater than 0")
    if amount_usd > 1:
        reasons.append("live smoke amount is capped at $1")
    if os.getenv("TRADING_MODE") != "live":
        reasons.append(".env TRADING_MODE must be live")
    if os.getenv("TRADING_ENABLED", "").lower() != "true":
        reasons.append(".env TRADING_ENABLED must be true")
    if trading.get("mode") != "live":
        reasons.append("config trading.mode must be live")
    if not trading.get("enabled", False):
        reasons.append("config trading.enabled must be true")
    if kill.stop_file_exists():
        reasons.append(f"{kill.stop_file} exists")
    if symbol not in set(trading.get("allowed_symbols", [])):
        reasons.append(f"symbol not allowlisted: {symbol}")
    if not has_api_credentials:
        reasons.append("API credentials are missing")
    return reasons


def live_smoke_order(symbol: str, side: str, amount_usd: float, confirm_live_smoke: bool, confirmation_reader=input) -> None:
    rules, _ = load_settings()
    logger = SQLiteLogger(ROOT / "data" / "trading_agent.db")
    kill = _project_kill_switch(rules)
    client = make_client()
    gate_reasons = _live_smoke_gate_reasons(rules, kill, symbol, amount_usd, confirm_live_smoke, client.has_credentials)
    if gate_reasons:
        _live_smoke_refuse(logger, symbol, "; ".join(gate_reasons), {"side": side, "amount_usd": amount_usd})

    account_payload = client.get_accounts()
    account_number = select_account_number(account_payload)
    holdings_payload = client.get_holdings(account_number)
    portfolio = Portfolio.from_robinhood(account_payload, holdings_payload)
    if side == "buy" and not portfolio.has_cash_for(amount_usd):
        _live_smoke_refuse(logger, symbol, "order would exceed available cash", {"cash_usd": portfolio.cash_usd, "amount_usd": amount_usd})

    market_payload = client.get_best_bid_ask(symbol)
    price = _best_price_from_payload(market_payload, symbol, side)
    if price is None:
        _live_smoke_refuse(logger, symbol, "no market price returned", {"side": side})

    signal = _manual_preview_signal(rules, symbol, side, "live_smoke")
    risk = RiskManager(rules, kill)
    orders = OrderManager(rules, risk, logger, PaperBroker(ROOT / "data" / "paper_trades.db"))
    order = orders.build_limit_order(signal, price, portfolio, amount_usd=amount_usd)
    decision = risk.evaluate(
        signal=signal,
        mode="live",
        notional=float(order["notional"]),
        portfolio=portfolio,
        daily_summary=_preview_daily_summary(logger),
        has_api_credentials=client.has_credentials,
        submit_live_order=True,
        order_quantity=float(order["quantity"]),
        current_price=price,
        last_order_timestamp=(logger.get_last_order(symbol) or {}).get("timestamp"),
    )
    if not decision.allowed:
        _live_smoke_refuse(logger, symbol, "; ".join(decision.reasons), {"side": side, "amount_usd": amount_usd, "order": order})

    preview = LiveBroker(client, dry_run=True, account_number=account_number).place_limit_order(order)
    preview_result = {
        "submitted": False,
        "mode": "live",
        "status": "live_smoke_preview",
        "symbol": symbol,
        "side": side,
        "amount_usd": amount_usd,
        "market_price": price,
        "order_payload": preview.get("order_payload"),
    }
    logger.log_decision(symbol, "live_smoke_preview", "live smoke payload preview generated before confirmation", preview_result)
    print(json.dumps(preview_result, indent=2))
    print(f"TYPE: {LIVE_SMOKE_CONFIRM_TEXT}")
    typed = confirmation_reader("> ").strip()
    if typed != LIVE_SMOKE_CONFIRM_TEXT:
        _live_smoke_refuse(logger, symbol, "typed confirmation did not match", {"typed_confirmation_matched": False})

    live_broker = LiveBroker(client, dry_run=False, account_number=account_number)
    result = live_broker.place_limit_order(order)
    logger.log_order(result)
    logger.log_decision(symbol, "live_smoke_submitted", "supervised live smoke order submitted", result)
    print(json.dumps(result, indent=2))


def build_live_smoke_preview(symbol: str, side: str, amount_usd: float, root: Path = ROOT) -> dict[str, Any]:
    rules, _ = load_settings_for_root(root)
    logger = SQLiteLogger(root / "data" / "trading_agent.db")
    kill = kill_switch_for_root(rules, root)
    client = make_client()
    if not client.has_credentials:
        return {"submitted": False, "risk_allowed": False, "risk_reasons": ["API credentials are missing"]}
    if symbol not in validated_live_symbols(rules, root):
        return {"submitted": False, "risk_allowed": False, "risk_reasons": [f"symbol is not validated for live: {symbol}"]}
    if amount_usd > float(rules.get("risk", {}).get("max_trade_amount_usd", 0) or 0):
        return {"submitted": False, "risk_allowed": False, "risk_reasons": ["amount exceeds max trade amount"]}
    try:
        account_payload = client.get_accounts()
        account_number = select_account_number(account_payload)
        holdings_payload = client.get_holdings(account_number)
        portfolio = Portfolio.from_robinhood(account_payload, holdings_payload)
        market_payload = client.get_best_bid_ask(symbol)
    except Exception as exc:
        logger.log_error("dashboard_live_smoke_preview", str(exc), {"symbol": symbol})
        return {"submitted": False, "risk_allowed": False, "risk_reasons": [str(exc)]}
    bid = _best_price_from_payload(market_payload, symbol, "sell")
    ask = _best_price_from_payload(market_payload, symbol, "buy")
    price = ask if side == "buy" else bid
    if price is None:
        return {"submitted": False, "risk_allowed": False, "risk_reasons": ["market data unavailable"]}
    signal = _manual_preview_signal(rules, symbol, side, "dashboard_live_smoke")
    risk = RiskManager(rules, kill)
    order = OrderManager(rules, risk, logger, PaperBroker(root / "data" / "paper_trades.db")).build_limit_order(
        signal,
        price,
        portfolio,
        amount_usd=amount_usd,
    )
    decision = risk.evaluate(
        signal=signal,
        mode="live",
        notional=float(order["notional"]),
        portfolio=portfolio,
        daily_summary=logger.get_daily_summary(),
        has_api_credentials=client.has_credentials,
        submit_live_order=True,
        order_quantity=float(order["quantity"]),
        current_price=price,
        last_order_timestamp=(logger.get_last_order(symbol) or {}).get("timestamp"),
    )
    payload = None
    if decision.allowed:
        payload = LiveBroker(client, dry_run=True, account_number=account_number).place_limit_order(order).get("order_payload")
    result = {
        "submitted": False,
        "symbol": symbol,
        "side": side,
        "amount_usd": amount_usd,
        "estimated_quantity": order["quantity"],
        "bid": bid,
        "ask": ask,
        "limit_price": price,
        "risk_allowed": decision.allowed,
        "risk_reasons": decision.reasons,
        "order": order,
        "order_payload": payload,
    }
    logger.log_decision(symbol, "dashboard_live_smoke_preview", "dashboard live smoke preview generated", _scrub_sensitive(result))
    return _scrub_sensitive(result)


def run_live_smoke_test(
    symbol: str,
    side: str,
    amount_usd: float,
    confirmation_checked: bool,
    reviewed_checked: bool,
    preview: dict[str, Any] | None = None,
    root: Path = ROOT,
) -> dict[str, Any]:
    rules, _ = load_settings_for_root(root)
    logger = SQLiteLogger(root / "data" / "trading_agent.db")
    if not confirmation_checked:
        raise SystemExit("Smoke test requires understanding confirmation checkbox")
    if not reviewed_checked:
        raise SystemExit("Smoke test requires reviewed-preview confirmation checkbox")
    readiness = live_launch_readiness(root=root)
    if not readiness.get("ready"):
        raise SystemExit("Live readiness failed: " + "; ".join(readiness.get("gate_reasons", [])))
    preview = preview or build_live_smoke_preview(symbol, side, amount_usd, root)
    if preview.get("submitted") or not preview.get("risk_allowed"):
        raise SystemExit("Smoke test preview is missing or risk-blocked")
    client = make_client()
    account_payload = client.get_accounts()
    account_number = select_account_number(account_payload)
    order = preview.get("order")
    if not isinstance(order, dict):
        raise SystemExit("Smoke test preview did not include an order")
    result = LiveBroker(client, dry_run=False, account_number=account_number).place_limit_order(order)
    logger.log_order(result)
    logger.increment_trade_count()
    logger.log_decision(symbol, "dashboard_live_smoke_submitted", "dashboard live smoke order submitted", _scrub_sensitive(result))
    return _scrub_sensitive({**result, "timestamp": SQLiteLogger.now()})


def run_bounded_live_iteration(confirmation_checked: bool, root: Path = ROOT, test_runner=None) -> dict[str, Any]:
    if not confirmation_checked:
        raise SystemExit("Bounded live iteration requires confirmation checkbox")
    test_runner = test_runner or run_project_tests
    readiness = live_launch_readiness(root=root)
    if not readiness.get("ready"):
        raise SystemExit("Live readiness failed: " + "; ".join(readiness.get("gate_reasons", [])))
    if root != ROOT:
        old_root = globals()["ROOT"]
        globals()["ROOT"] = root
        try:
            run_bounded_live_loop(1, True, test_runner=test_runner)
        finally:
            globals()["ROOT"] = old_root
    else:
        run_bounded_live_loop(1, True, test_runner=test_runner)
    return {"submitted": False, "status": "bounded_live_iteration_completed", "timestamp": SQLiteLogger.now()}


def return_to_paper_mode(root: Path = ROOT) -> dict[str, Any]:
    rules, strategy_config = load_settings_for_root(root)
    rules.setdefault("trading", {})["enabled"] = True
    rules.setdefault("trading", {})["mode"] = "paper"
    write_env_allowed(root / ".env", {"TRADING_MODE": "paper", "TRADING_ENABLED": "true"})
    with (root / "config" / "trading_rules.yaml").open("w", encoding="utf-8") as handle:
        yaml.safe_dump(rules, handle, sort_keys=False)
    with (root / "config" / "strategy.yaml").open("w", encoding="utf-8") as handle:
        yaml.safe_dump(strategy_config, handle, sort_keys=False)
    SQLiteLogger(root / "data" / "trading_agent.db").log_decision(None, "return_to_paper", "dashboard returned env and config to paper mode")
    return {"submitted": False, "status": "paper mode restored"}


def create_stop_trading(root: Path = ROOT) -> dict[str, Any]:
    rules, _ = load_settings_for_root(root)
    path = kill_switch_for_root(rules, root).create_stop_file()
    SQLiteLogger(root / "data" / "trading_agent.db").log_decision(None, "dashboard_stop_trading", "STOP_TRADING created")
    return {"submitted": False, "status": "STOP_TRADING created", "path": str(path)}


def clear_stop_trading(confirmation_checked: bool, root: Path = ROOT) -> dict[str, Any]:
    if not confirmation_checked:
        raise SystemExit("Clear STOP_TRADING requires confirmation checkbox")
    rules, _ = load_settings_for_root(root)
    kill = kill_switch_for_root(rules, root)
    if kill.stop_file_exists():
        Path(kill.stop_file).unlink()
    SQLiteLogger(root / "data" / "trading_agent.db").log_decision(None, "dashboard_clear_stop", "STOP_TRADING cleared")
    return {"submitted": False, "status": "STOP_TRADING cleared"}


def cancel_open_live_orders_dashboard(confirmation_checked: bool, root: Path = ROOT) -> dict[str, Any]:
    if not confirmation_checked:
        raise SystemExit("Cancel open live orders requires confirmation checkbox")
    logger = SQLiteLogger(root / "data" / "trading_agent.db")
    load_dotenv(root / ".env", override=True)
    client = make_client()
    account_payload = client.get_accounts()
    account_number = select_account_number(account_payload)
    orders_payload = client.get_orders(account_number)
    cancelled: list[dict[str, Any]] = []
    for row in _open_order_rows(orders_payload):
        order_id = row.get("id") or row.get("order_id")
        if order_id:
            cancelled.append({"id": order_id, "response": client.cancel_order(str(order_id))})
    result = {"submitted": False, "cancelled_count": len(cancelled), "cancelled": cancelled}
    logger.log_decision(None, "dashboard_open_live_orders_cancelled", "dashboard cancelled open live orders", _scrub_sensitive(result))
    return _scrub_sensitive(result)


def _live_single_refuse(logger: SQLiteLogger, symbol: str | None, reason: str, details: dict[str, Any] | None = None) -> None:
    logger.log_decision(symbol, "live_single_trade_refused", reason, details or {})
    raise SystemExit(f"Refusing live single trade: {reason}")


def _live_single_gate_reasons(
    rules: dict[str, Any],
    kill: KillSwitch,
    symbol: str,
    side: str,
    amount_usd: float,
    confirm_live: bool,
    has_api_credentials: bool,
    daily_summary: dict[str, Any],
) -> list[str]:
    reasons: list[str] = []
    trading = rules.get("trading", {})
    risk = rules.get("risk", {})
    if not confirm_live:
        reasons.append("--confirm-live is required")
    if os.getenv("TRADING_MODE") != "live":
        reasons.append(".env TRADING_MODE must be live")
    if os.getenv("TRADING_ENABLED", "").lower() != "true":
        reasons.append(".env TRADING_ENABLED must be true")
    if trading.get("mode") != "live":
        reasons.append("config trading.mode must be live")
    if not trading.get("enabled", False):
        reasons.append("config trading.enabled must be true")
    if kill.stop_file_exists():
        reasons.append(f"{kill.stop_file} exists")
    if symbol not in set(trading.get("allowed_symbols", [])):
        reasons.append(f"symbol not allowlisted: {symbol}")
    if symbol != "BTC-USD":
        reasons.append("bounded live single trade only supports BTC-USD")
    if side not in {"buy", "sell"}:
        reasons.append("side must be buy or sell")
    if amount_usd <= 0:
        reasons.append("amount must be greater than 0")
    if amount_usd > 1:
        reasons.append("amount must be <= $1")
    if float(risk.get("max_trade_amount_usd", 0)) > 1:
        reasons.append("max_trade_amount_usd must be <= 1")
    if float(risk.get("max_daily_loss_usd", 0)) > 5:
        reasons.append("max_daily_loss_usd must be <= 5")
    if int(risk.get("max_trades_per_day", 0)) > 1:
        reasons.append("max_trades_per_day must be <= 1")
    if float(risk.get("max_symbol_allocation_percent", 0) or 0) <= 0:
        reasons.append("max_symbol_allocation_percent must be configured")
    elif float(risk.get("max_symbol_allocation_percent", 0)) > 5:
        reasons.append("max_symbol_allocation_percent must be <= 5")
    if int(risk.get("min_order_cooldown_seconds", 0) or 0) < 300:
        reasons.append("min_order_cooldown_seconds must be >= 300")
    if not risk.get("require_live_order_reconciliation", False):
        reasons.append("require_live_order_reconciliation must be true")
    if risk.get("allow_margin", False):
        reasons.append("allow_margin must be false")
    if risk.get("allow_shorting", False):
        reasons.append("allow_shorting must be false")
    if int(daily_summary.get("trade_count", 0)) >= 1:
        reasons.append("daily trade count must be below 1")
    if float(daily_summary.get("realized_pnl", 0)) <= -5:
        reasons.append("daily loss must be below $5")
    if not has_api_credentials:
        reasons.append("API credentials are missing")
    return reasons


def _negative_positions(portfolio: Portfolio) -> dict[str, float]:
    return {
        symbol: float(position.quantity)
        for symbol, position in portfolio.positions.items()
        if float(position.quantity) < -1e-12
    }


def live_single_trade(symbol: str, side: str, amount_usd: float, confirm_live: bool, confirmation_reader=input) -> None:
    rules, _ = load_settings()
    logger = SQLiteLogger(ROOT / "data" / "trading_agent.db")
    kill = _project_kill_switch(rules)
    client = make_client()
    daily_summary = logger.get_daily_summary()
    gate_reasons = _live_single_gate_reasons(
        rules,
        kill,
        symbol,
        side,
        amount_usd,
        confirm_live,
        client.has_credentials,
        daily_summary,
    )
    if gate_reasons:
        _live_single_refuse(logger, symbol, "; ".join(gate_reasons), {"side": side, "amount_usd": amount_usd})

    try:
        account_payload = client.get_accounts()
        account_number = select_account_number(account_payload)
        holdings_payload = client.get_holdings(account_number)
        portfolio = Portfolio.from_robinhood(account_payload, holdings_payload)
    except Exception as exc:
        logger.log_error("live_single_trade_account_lookup", str(exc))
        _live_single_refuse(logger, symbol, f"Robinhood connection failed: {exc}")

    negative_positions = _negative_positions(portfolio)
    if negative_positions:
        _live_single_refuse(logger, symbol, "negative positions detected", {"negative_positions": negative_positions})
    if side == "buy" and not portfolio.has_cash_for(amount_usd):
        _live_single_refuse(logger, symbol, "account buying power is not enough", {"cash_usd": portfolio.cash_usd, "amount_usd": amount_usd})

    market_payload = client.get_best_bid_ask(symbol)
    price = _best_price_from_payload(market_payload, symbol, side)
    if price is None:
        _live_single_refuse(logger, symbol, "market data unavailable", {"side": side})

    signal = _manual_preview_signal(rules, symbol, side, "live_single_trade")
    risk = RiskManager(rules, kill)
    order_manager = OrderManager(rules, risk, logger, PaperBroker(ROOT / "data" / "paper_trades.db"))
    order = order_manager.build_limit_order(signal, price, portfolio, amount_usd=amount_usd)
    if order.get("order_type") != "limit":
        _live_single_refuse(logger, symbol, "only limit orders are allowed", {"order": order})
    decision = risk.evaluate(
        signal=signal,
        mode="live",
        notional=float(order["notional"]),
        portfolio=portfolio,
        daily_summary=daily_summary,
        has_api_credentials=client.has_credentials,
        submit_live_order=True,
        order_quantity=float(order["quantity"]),
        current_price=price,
        last_order_timestamp=(logger.get_last_order(symbol) or {}).get("timestamp"),
    )
    if not decision.allowed:
        _live_single_refuse(logger, symbol, "; ".join(decision.reasons), {"order": order, "risk_reasons": decision.reasons})

    preview = LiveBroker(client, dry_run=True, account_number=account_number).place_limit_order(order)
    preview_result = {
        "submitted": False,
        "mode": "live",
        "status": "live_single_trade_preview",
        "symbol": symbol,
        "side": side,
        "amount_usd": amount_usd,
        "market_price": price,
        "order_payload": preview.get("order_payload"),
    }
    logger.log_decision(symbol, "live_single_trade_preview", "bounded live single-trade payload preview generated", preview_result)
    print(json.dumps(preview_result, indent=2))
    print(f"TYPE: {LIVE_SINGLE_CONFIRM_TEXT}")
    typed = confirmation_reader("> ").strip()
    if typed != LIVE_SINGLE_CONFIRM_TEXT:
        _live_single_refuse(logger, symbol, "typed confirmation did not match", {"typed_confirmation_matched": False})

    result = LiveBroker(client, dry_run=False, account_number=account_number).place_limit_order(order)
    logger.log_order(result)
    logger.increment_trade_count()
    logger.log_decision(symbol, "live_single_trade_submitted", "bounded live single trade submitted", result)
    print(json.dumps(result, indent=2))


def _unattended_live_refuse(logger: SQLiteLogger, reason: str, details: dict[str, Any] | None = None) -> None:
    logger.log_decision(None, "unattended_live_refused", reason, details or {})
    raise SystemExit(f"Refusing unattended live loop: {reason}")


def _restricted_unattended_live_reasons(rules: dict[str, Any], kill: KillSwitch, confirm_unattended_live: bool) -> list[str]:
    reasons: list[str] = []
    trading = rules.get("trading", {})
    risk = rules.get("risk", {})
    allowed_symbols = trading.get("allowed_symbols", [])
    if not confirm_unattended_live:
        reasons.append("--confirm-unattended-live is required")
    if os.getenv("TRADING_MODE") != "live":
        reasons.append(".env TRADING_MODE must be live")
    if os.getenv("TRADING_ENABLED", "").lower() != "true":
        reasons.append(".env TRADING_ENABLED must be true")
    if trading.get("mode") != "live":
        reasons.append("config trading.mode must be live")
    if not trading.get("enabled", False):
        reasons.append("config trading.enabled must be true")
    if allowed_symbols != ["BTC-USD"]:
        reasons.append("allowed symbols must be exactly BTC-USD")
    if float(risk.get("max_trade_amount_usd", 0)) > 1:
        reasons.append("max_trade_amount_usd must be <= 1")
    if float(risk.get("max_daily_loss_usd", 0)) > 1:
        reasons.append("max_daily_loss_usd must be <= 1")
    if int(risk.get("max_trades_per_day", 0)) > 1:
        reasons.append("max_trades_per_day must be <= 1")
    if int(risk.get("max_open_positions", 0)) > 1:
        reasons.append("max_open_positions must be <= 1")
    if risk.get("allow_position_scaling", False):
        reasons.append("allow_position_scaling must be false")
    if risk.get("allow_margin", False):
        reasons.append("allow_margin must be false")
    if risk.get("allow_shorting", False):
        reasons.append("allow_shorting must be false")
    if kill.stop_file_exists():
        reasons.append(f"{kill.stop_file} exists")
    return reasons


def _bounded_live_refuse(logger: SQLiteLogger, reason: str, details: dict[str, Any] | None = None) -> None:
    logger.log_decision(None, "bounded_live_refused", reason, details or {})
    raise SystemExit(f"Refusing bounded live loop: {reason}")


def _bounded_live_gate_reasons(
    rules: dict[str, Any],
    kill: KillSwitch,
    confirm_bounded_live: bool,
    paper_portfolio: Portfolio | None = None,
    root: Path = ROOT,
) -> list[str]:
    reasons: list[str] = []
    trading = rules.get("trading", {})
    risk = rules.get("risk", {})
    live_symbols = symbol_lists(rules)["live_allowed_symbols"]
    validation = load_symbol_validation(root)
    validated = set(validation.get("available_for_live", []))
    unavailable = [symbol for symbol in validation.get("unavailable", []) if symbol in live_symbols]
    unknown = [symbol for symbol in validation.get("unknown", []) if symbol in live_symbols]
    unvalidated = [symbol for symbol in live_symbols if symbol not in validated and symbol not in unavailable and symbol not in unknown]
    if not confirm_bounded_live:
        reasons.append("--confirm-bounded-live is required")
    if os.getenv("TRADING_MODE") != "live":
        reasons.append(".env TRADING_MODE must be live")
    if os.getenv("TRADING_ENABLED", "").lower() != "true":
        reasons.append(".env TRADING_ENABLED must be true")
    if trading.get("mode") != "live":
        reasons.append("config trading.mode must be live")
    if not trading.get("enabled", False):
        reasons.append("config trading.enabled must be true")
    if kill.stop_file_exists():
        reasons.append(f"{kill.stop_file} exists")
    if not live_symbols:
        reasons.append("live_allowed_symbols must not be empty")
    if unvalidated:
        reasons.append(f"unvalidated live symbols: {', '.join(unvalidated)}")
    if unavailable:
        reasons.append(f"unsupported live symbols: {', '.join(unavailable)}")
    if unknown:
        reasons.append(f"unknown live symbols: {', '.join(unknown)}")
    if float(risk.get("max_trade_amount_usd", 0)) > 100:
        reasons.append("max_trade_amount_usd must be <= 100")
    if float(risk.get("max_daily_loss_usd", 0)) > 100:
        reasons.append("max_daily_loss_usd must be <= 100")
    if int(risk.get("max_trades_per_day", 0)) > 5:
        reasons.append("max_trades_per_day must be <= 5")
    if float(risk.get("max_symbol_allocation_percent", 0) or 0) <= 0:
        reasons.append("max_symbol_allocation_percent must be configured")
    elif float(risk.get("max_symbol_allocation_percent", 0)) > 25:
        reasons.append("max_symbol_allocation_percent must be <= 25")
    if int(risk.get("min_order_cooldown_seconds", 0) or 0) < 300:
        reasons.append("min_order_cooldown_seconds must be >= 300")
    if not risk.get("require_live_order_reconciliation", False):
        reasons.append("require_live_order_reconciliation must be true")
    if int(risk.get("max_open_positions", 0)) > 10:
        reasons.append("max_open_positions must be <= 10")
    if risk.get("allow_position_scaling", False):
        reasons.append("allow_position_scaling must be false")
    if risk.get("allow_margin", False):
        reasons.append("allow_margin must be false")
    if risk.get("allow_shorting", risk.get("allow_shorts", False)):
        reasons.append("allow_shorting must be false")
    if paper_portfolio and _negative_positions(paper_portfolio):
        reasons.append("negative paper positions detected")
    return reasons


def run_project_tests() -> tuple[bool, str]:
    result = subprocess.run(
        [sys.executable, "-m", "pytest"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    return result.returncode == 0, output[-4000:]


def run_live_loop(hours: float, confirm_unattended_live: bool, test_runner=run_project_tests) -> None:
    rules, strategy_config = load_settings()
    logger = SQLiteLogger(ROOT / "data" / "trading_agent.db")
    kill = _project_kill_switch(rules)
    reasons = _restricted_unattended_live_reasons(rules, kill, confirm_unattended_live)
    if reasons:
        _unattended_live_refuse(logger, "; ".join(reasons), {"hours": hours})

    tests_ok, test_output = test_runner()
    if not tests_ok:
        _unattended_live_refuse(logger, "pytest must pass before unattended live loop", {"pytest_output_tail": test_output})

    client = make_client()
    if not client.has_credentials:
        _unattended_live_refuse(logger, "API credentials are missing")

    try:
        account_payload = client.get_accounts()
        account_number = select_account_number(account_payload)
        holdings_payload = client.get_holdings(account_number)
        portfolio = Portfolio.from_robinhood(account_payload, holdings_payload)
    except Exception as exc:
        logger.log_error("unattended_live_account_lookup", str(exc))
        _unattended_live_refuse(logger, f"Robinhood connection failed: {exc}")

    amount_usd = float(rules.get("risk", {}).get("max_trade_amount_usd", 1))
    if not portfolio.has_cash_for(amount_usd):
        _unattended_live_refuse(logger, "available buying power is not enough", {"cash_usd": portfolio.cash_usd, "amount_usd": amount_usd})

    market_data = MarketDataService(client, ROOT / "data" / "market_data.db")
    prices = market_data.get_latest_prices(["BTC-USD"])
    price = prices.get("BTC-USD")
    if price is None:
        _unattended_live_refuse(logger, "BTC-USD market data unavailable")

    signal = _manual_preview_signal(rules, "BTC-USD", "buy", "unattended_live_preflight")
    order_manager = OrderManager(rules, RiskManager(rules, kill), logger, PaperBroker(ROOT / "data" / "paper_trades.db"))
    order = order_manager.build_limit_order(signal, price, portfolio, amount_usd=amount_usd)
    decision = RiskManager(rules, kill).evaluate(
        signal=signal,
        mode="live",
        notional=float(order["notional"]),
        portfolio=portfolio,
        daily_summary=logger.get_daily_summary(),
        has_api_credentials=client.has_credentials,
        submit_live_order=True,
        order_quantity=float(order["quantity"]),
        current_price=price,
        last_order_timestamp=(logger.get_last_order("BTC-USD") or {}).get("timestamp"),
    )
    if not decision.allowed:
        _unattended_live_refuse(logger, "order preview failed risk checks", {"risk_reasons": decision.reasons, "order": order})

    preview = LiveBroker(client, dry_run=True, account_number=account_number).place_limit_order(order)
    preview_result = {
        "submitted": False,
        "mode": "live",
        "status": "unattended_live_preflight_preview",
        "hours": hours,
        "order_payload": preview.get("order_payload"),
    }
    logger.log_decision("BTC-USD", "unattended_live_preflight_preview", "restricted unattended live preflight passed", preview_result)
    print(json.dumps(preview_result, indent=2))

    interval = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
    deadline = time.monotonic() + (hours * 60 * 60)
    completed = 0
    print(f"Starting restricted unattended live loop hours={hours} poll_interval_seconds={interval}")
    while time.monotonic() < deadline:
        if kill.stop_file_exists():
            logger.log_decision(None, "halted", f"{kill.stop_file} exists", {"mode": "live", "iterations": completed})
            print(f"Stopped: {kill.stop_file} exists")
            return
        run_cycle("live", rules, strategy_config)
        completed += 1
        if time.monotonic() >= deadline:
            break
        time.sleep(interval)
    print(f"Completed restricted unattended live loop hours={hours} iterations={completed}")


def run_bounded_live_loop(iterations: int, confirm_bounded_live: bool, test_runner=run_project_tests) -> None:
    rules, strategy_config = load_settings()
    logger = SQLiteLogger(ROOT / "data" / "trading_agent.db")
    kill = _project_kill_switch(rules)
    paper_portfolio = PaperBroker(ROOT / "data" / "paper_trades.db").get_portfolio()
    reasons = _bounded_live_gate_reasons(rules, kill, confirm_bounded_live, paper_portfolio, ROOT)
    if reasons:
        _bounded_live_refuse(logger, "; ".join(reasons), {"iterations": iterations})

    tests_ok, test_output = test_runner()
    if not tests_ok:
        _bounded_live_refuse(logger, "pytest must pass before bounded live loop", {"pytest_output_tail": test_output})

    client = make_client()
    if not client.has_credentials:
        _bounded_live_refuse(logger, "API credentials are missing")

    try:
        account_payload = client.get_accounts()
        account_number = select_account_number(account_payload)
        holdings_payload = client.get_holdings(account_number)
        portfolio = Portfolio.from_robinhood(account_payload, holdings_payload)
    except Exception as exc:
        logger.log_error("bounded_live_account_lookup", str(exc))
        _bounded_live_refuse(logger, f"Robinhood connection failed: {exc}")

    negative_positions = _negative_positions(portfolio)
    if negative_positions:
        _bounded_live_refuse(logger, "negative positions detected", {"negative_positions": negative_positions})

    live_symbols = symbol_lists(rules)["live_allowed_symbols"]
    validation = load_symbol_validation(ROOT)
    validated_symbols = [symbol for symbol in validation.get("available_for_live", []) if symbol in live_symbols]
    skipped_symbols = [symbol for symbol in live_symbols if symbol not in validated_symbols]
    logger.log_decision(
        None,
        "bounded_live_symbols_scanned",
        "bounded live loop scanned configured live symbols",
        {"scanned": live_symbols, "considered": validated_symbols, "skipped": skipped_symbols, "submitted": False},
    )
    if not validated_symbols:
        _bounded_live_refuse(logger, "no validated live symbols available", {"scanned": live_symbols, "skipped": skipped_symbols})

    amount_usd = float(rules.get("risk", {}).get("max_trade_amount_usd", 1))
    if not portfolio.has_cash_for(amount_usd):
        _bounded_live_refuse(logger, "available buying power is not enough", {"cash_usd": portfolio.cash_usd, "amount_usd": amount_usd})

    market_data = MarketDataService(client, ROOT / "data" / "market_data.db")
    prices = market_data.get_latest_prices(validated_symbols)
    priced_symbols = [symbol for symbol in validated_symbols if prices.get(symbol) is not None]
    logger.log_decision(
        None,
        "bounded_live_symbols_priced",
        "bounded live loop loaded market data for validated symbols",
        {"considered": validated_symbols, "priced": priced_symbols, "skipped": [symbol for symbol in validated_symbols if symbol not in priced_symbols], "submitted": False},
    )
    if not priced_symbols:
        _bounded_live_refuse(logger, "validated live symbols have no market data", {"validated_symbols": validated_symbols})

    preflight_symbol = priced_symbols[0]
    price = prices.get(preflight_symbol)
    if price is None:
        _bounded_live_refuse(logger, f"{preflight_symbol} market data unavailable")

    effective_rules = dict(rules)
    effective_rules["trading"] = dict(rules.get("trading", {}))
    effective_rules["trading"]["allowed_symbols"] = priced_symbols

    signal = _manual_preview_signal(effective_rules, preflight_symbol, "buy", "bounded_live_preflight")
    order_manager = OrderManager(rules, RiskManager(rules, kill), logger, PaperBroker(ROOT / "data" / "paper_trades.db"))
    order = order_manager.build_limit_order(signal, price, portfolio, amount_usd=amount_usd)
    decision = RiskManager(rules, kill).evaluate(
        signal=signal,
        mode="live",
        notional=float(order["notional"]),
        portfolio=portfolio,
        daily_summary=logger.get_daily_summary(),
        has_api_credentials=client.has_credentials,
        submit_live_order=True,
        order_quantity=float(order["quantity"]),
        current_price=price,
        last_order_timestamp=(logger.get_last_order(preflight_symbol) or {}).get("timestamp"),
    )
    if not decision.allowed:
        _bounded_live_refuse(logger, "order preview failed risk checks", {"risk_reasons": decision.reasons, "order": order})

    preview = LiveBroker(client, dry_run=True, account_number=account_number).place_limit_order(order)
    preview_result = {
        "submitted": False,
        "mode": "live",
        "status": "bounded_live_preflight_preview",
        "iterations": iterations,
        "order_payload": preview.get("order_payload"),
    }
    logger.log_decision(preflight_symbol, "bounded_live_preflight_preview", "bounded live preflight passed", preview_result)
    print(json.dumps(preview_result, indent=2))

    completed = 0
    print(f"Starting bounded live loop iterations={iterations}")
    for index in range(iterations):
        if kill.stop_file_exists():
            logger.log_decision(None, "halted", f"{kill.stop_file} exists", {"mode": "live", "iterations": completed})
            print(f"Stopped: {kill.stop_file} exists")
            return
        run_cycle("live", effective_rules, strategy_config)
        completed += 1
    print(f"Completed bounded live loop iterations={completed}")


def _scrub_sensitive(value: Any) -> Any:
    secret_keys = {"api_key", "private_key", "account_number", "account_number_suffix"}
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = key.lower()
            if normalized_key == "provider_key_status" and isinstance(item, dict):
                output[key] = {
                    provider_key: provider_status if provider_status in {"configured", "missing"} else "***"
                    for provider_key, provider_status in item.items()
                }
            elif normalized_key in secret_keys or "key" in normalized_key:
                output[key] = "***"
            else:
                output[key] = _scrub_sensitive(item)
        return output
    if isinstance(value, list):
        return [_scrub_sensitive(item) for item in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return value
        if isinstance(parsed, (dict, list)):
            return json.dumps(_scrub_sensitive(parsed), separators=(",", ":"))
    return value


def _open_order_rows(payload: Any) -> list[dict[str, Any]]:
    open_states = {"open", "queued", "new", "confirmed", "unconfirmed", "partially_filled"}
    rows = _payload_results(payload)
    return [
        row
        for row in rows
        if str(row.get("state") or row.get("status") or "").lower() in open_states
    ]


def live_launch_readiness(run_tests: bool = False, check_connection: bool = False, root: Path = ROOT) -> dict[str, Any]:
    load_dotenv(root / ".env", override=True)
    rules = load_yaml(root / "config" / "trading_rules.yaml")
    logger = SQLiteLogger(root / "data" / "trading_agent.db")
    kill = KillSwitch(
        stop_file=_root_stop_file(rules, root),
        env_var=rules.get("kill_switch", {}).get("env_var", "TRADING_ENABLED"),
    )
    paper_portfolio = PaperBroker(root / "data" / "paper_trades.db").get_portfolio()
    gate_reasons = _bounded_live_gate_reasons(rules, kill, True, paper_portfolio, root)
    intelligence = intelligence_status_layer(root)
    if intelligence.get("last_errors"):
        gate_reasons.append("unresolved intelligence API errors")
    lists = symbol_lists(rules)
    validation = load_symbol_validation(root)
    available = [symbol for symbol in validation.get("available_for_live", []) if symbol in lists["live_allowed_symbols"]]
    unavailable = [symbol for symbol in validation.get("unavailable", []) if symbol in lists["live_allowed_symbols"]]
    unknown = [symbol for symbol in validation.get("unknown", []) if symbol in lists["live_allowed_symbols"]]
    unvalidated = [
        symbol
        for symbol in lists["live_allowed_symbols"]
        if symbol not in set(available) and symbol not in set(unavailable) and symbol not in set(unknown)
    ]
    report: dict[str, Any] = {
        "ready": not gate_reasons,
        "gate_reasons": gate_reasons,
        "mode": runtime_mode(None, rules),
        "env_trading_enabled": os.getenv("TRADING_ENABLED", "").lower() == "true",
        "config_trading_enabled": bool(rules.get("trading", {}).get("enabled", False)),
        "stop_file_exists": kill.stop_file_exists(),
        "risk": {
            "max_trade_amount_usd": rules.get("risk", {}).get("max_trade_amount_usd"),
            "max_daily_loss_usd": rules.get("risk", {}).get("max_daily_loss_usd"),
            "max_trades_per_day": rules.get("risk", {}).get("max_trades_per_day"),
            "max_open_positions": rules.get("risk", {}).get("max_open_positions"),
            "max_symbol_allocation_percent": rules.get("risk", {}).get("max_symbol_allocation_percent"),
            "min_order_cooldown_seconds": rules.get("risk", {}).get("min_order_cooldown_seconds"),
        },
        "live_symbol_count": len(lists["live_allowed_symbols"]),
        "live_allowed_symbols": lists["live_allowed_symbols"],
        "validated_live_symbols": available,
        "unvalidated_live_symbols": unvalidated,
        "unsupported_live_symbols": unavailable,
        "unknown_live_symbols": unknown,
        "submitted_live_orders_today": _submitted_live_decision_count(logger),
        "intelligence": {
            "enabled": intelligence.get("enabled"),
            "news_provider_status": intelligence.get("news_provider_status"),
            "macro_provider_status": intelligence.get("macro_provider_status"),
            "market_context_provider_status": intelligence.get("market_context_provider_status"),
            "minimum_confidence_to_trade": intelligence.get("minimum_confidence_to_trade"),
            "macro_risk": intelligence.get("macro_risk"),
            "market_context_status": intelligence.get("market_context_status"),
            "last_errors": intelligence.get("last_errors"),
        },
    }
    if run_tests:
        tests_ok, output = run_project_tests()
        report["pytest_passed"] = tests_ok
        report["pytest_output_tail"] = output[-1000:]
        report["ready"] = report["ready"] and tests_ok
    if check_connection:
        client = make_client()
        if not client.has_credentials:
            report["connection_passed"] = False
            report["connection_detail"] = "API credentials are missing"
            report["ready"] = False
        else:
            try:
                account_payload = client.get_accounts()
                account_number = select_account_number(account_payload)
                orders_payload = client.get_orders(account_number)
                report["connection_passed"] = True
                report["open_live_orders"] = len(_open_order_rows(orders_payload))
            except Exception as exc:
                report["connection_passed"] = False
                report["connection_detail"] = str(exc)
                report["ready"] = False
    logger.log_decision(None, "live_launch_readiness", "bounded live readiness report generated", _scrub_sensitive(report))
    return _scrub_sensitive(report)


def equity_live_readiness_command(output_format: str = "json", output_path: str | None = None) -> dict[str, Any]:
    """The equities lane's live-readiness gate (src/equity_readiness.py).

    Runs the full test suite, evaluates every equities gate against it, reads
    the audit log and config for the evidence no test can speak to, and writes
    the verdict to the audit log. Read-only -- it never reaches the connector
    and never changes a posture.

    Exits non-zero when the verdict is ready:false, so this can gate a script
    without anyone having to remember to read the JSON.
    """
    report = equity_live_readiness(ROOT, logger=SQLiteLogger(ROOT / "data" / "trading_agent.db"))
    rendered = readiness_markdown(report) if output_format == "markdown" else json.dumps(report, indent=2)
    if output_path:
        path = Path(output_path)
        if not path.is_absolute():
            path = ROOT / path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered + "\n", encoding="utf-8")
        print(f"wrote {path}")
    else:
        print(rendered)
    if not report["ready"]:
        raise SystemExit(1)
    return report


def option_live_readiness_command(output_format: str = "json", output_path: str | None = None) -> dict[str, Any]:
    """The options lane's live-readiness gate (src/option_readiness.py).

    Runs the full test suite, evaluates every options gate against it, reads the
    audit log and config for the evidence no test can speak to (caps, the shared
    account anchor, two clean paper runs on a real basis, lane isolation), and
    writes the verdict to the audit log. Read-only -- it never reaches the
    connector and never changes a posture.

    Exits non-zero when the verdict is ready:false, so this can gate a script
    without anyone having to remember to read the JSON.
    """
    from .option_readiness import option_live_readiness, readiness_markdown as option_readiness_markdown

    report = option_live_readiness(ROOT, logger=SQLiteLogger(ROOT / "data" / "trading_agent.db"))
    rendered = option_readiness_markdown(report) if output_format == "markdown" else json.dumps(report, indent=2)
    if output_path:
        path = Path(output_path)
        if not path.is_absolute():
            path = ROOT / path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered + "\n", encoding="utf-8")
        print(f"wrote {path}")
    else:
        print(rendered)
    if not report["ready"]:
        raise SystemExit(1)
    return report


def collect_intelligence() -> None:
    rules, _ = load_settings()
    symbols = symbol_lists(rules)["live_allowed_symbols"]
    result = collect_intelligence_layer(ROOT, symbols)
    SQLiteLogger(ROOT / "data" / "trading_agent.db").log_decision(
        None,
        "intelligence_collected",
        "market intelligence collection completed",
        result,
    )
    print(json.dumps(_scrub_sensitive(result), indent=2))


def intelligence_status() -> None:
    result = intelligence_status_layer(ROOT)
    print(json.dumps(_scrub_sensitive(result), indent=2))


def score_symbol_command(symbol: str) -> None:
    normalized = symbol.upper()
    result = score_symbol_layer(ROOT, normalized)
    SQLiteLogger(ROOT / "data" / "trading_agent.db").log_decision(normalized, "intelligence_score_symbol", "symbol intelligence scored", result)
    print(json.dumps(_scrub_sensitive(result), indent=2))


def score_all_symbols_command() -> None:
    rules, _ = load_settings()
    symbols = validated_live_symbols(rules, ROOT) or symbol_lists(rules)["live_allowed_symbols"]
    result = score_all_symbols_layer(ROOT, symbols)
    SQLiteLogger(ROOT / "data" / "trading_agent.db").log_decision(None, "intelligence_score_all_symbols", "all live symbols intelligence scored", result)
    print(json.dumps(_scrub_sensitive(result), indent=2))


def intelligence_report(output: str | None = None) -> None:
    path = export_intelligence_report_layer(ROOT, output)
    SQLiteLogger(ROOT / "data" / "trading_agent.db").log_decision(None, "intelligence_report_exported", "intelligence report exported", {"path": str(path)})
    print(str(path))


def _submitted_live_decision_count(logger: SQLiteLogger) -> int:
    with logger.connect() as conn:
        actions = [row[0] for row in conn.execute("SELECT action FROM decisions")]
    targets = {"live_order_submitted", "live_smoke_submitted", "live_single_trade_submitted"}
    return sum(action in targets for action in actions)


def export_live_audit(output: str | None = None, limit: int = 500, root: Path = ROOT) -> Path:
    logger = SQLiteLogger(root / "data" / "trading_agent.db")
    target = Path(output) if output else root / "logs" / f"live_audit_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.json"
    if not target.is_absolute():
        target = root / target
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "submitted_live_orders_total": _submitted_live_decision_count(logger),
        "rows": logger.recent_audit_rows(limit=limit),
    }
    target.write_text(json.dumps(_scrub_sensitive(payload), indent=2), encoding="utf-8")
    logger.log_decision(None, "live_audit_exported", "live audit export written", {"path": str(target), "limit": limit})
    print(str(target))
    return target


def reconcile_live_orders(root: Path = ROOT) -> dict[str, Any]:
    load_dotenv(root / ".env", override=True)
    rules = load_yaml(root / "config" / "trading_rules.yaml")
    logger = SQLiteLogger(root / "data" / "trading_agent.db")
    client = make_client()
    if not client.has_credentials:
        raise SystemExit("Missing ROBINHOOD_API_KEY or ROBINHOOD_PRIVATE_KEY in .env")
    try:
        account_payload = client.get_accounts()
        account_number = select_account_number(account_payload)
        orders_payload = client.get_orders(account_number)
    except Exception as exc:
        logger.log_error("live_order_reconciliation", str(exc))
        raise SystemExit(f"Read-only live order reconciliation failed: {exc}") from exc
    rows = _payload_results(orders_payload)
    summary = {
        "submitted": False,
        "mode": runtime_mode(None, rules),
        "orders_seen": len(rows),
        "open_live_orders": len(_open_order_rows(orders_payload)),
        "statuses": sorted({str(row.get("state") or row.get("status") or "unknown") for row in rows}),
    }
    logger.log_decision(None, "live_order_reconciliation", "read-only live order reconciliation completed", summary)
    print(json.dumps(_scrub_sensitive(summary), indent=2))
    return summary


def cancel_open_live_orders(confirm_cancel_live: bool, confirmation_reader=input) -> dict[str, Any]:
    logger = SQLiteLogger(ROOT / "data" / "trading_agent.db")
    if not confirm_cancel_live:
        logger.log_decision(None, "cancel_open_live_orders_refused", "--confirm-cancel-live is required")
        raise SystemExit("Refusing cancel-open-live-orders: --confirm-cancel-live is required")
    client = make_client()
    if not client.has_credentials:
        raise SystemExit("Missing ROBINHOOD_API_KEY or ROBINHOOD_PRIVATE_KEY in .env")
    print(f"TYPE: {LIVE_CANCEL_CONFIRM_TEXT}")
    typed = confirmation_reader("> ").strip()
    if typed != LIVE_CANCEL_CONFIRM_TEXT:
        logger.log_decision(None, "cancel_open_live_orders_refused", "typed confirmation did not match")
        raise SystemExit("Refusing cancel-open-live-orders: typed confirmation did not match")
    account_payload = client.get_accounts()
    account_number = select_account_number(account_payload)
    orders_payload = client.get_orders(account_number)
    open_orders = _open_order_rows(orders_payload)
    cancelled: list[dict[str, Any]] = []
    for row in open_orders:
        order_id = row.get("id") or row.get("order_id")
        if not order_id:
            continue
        cancelled.append({"id": order_id, "response": client.cancel_order(str(order_id))})
    result = {"submitted": False, "cancelled_count": len(cancelled), "cancelled": cancelled}
    logger.log_decision(None, "open_live_orders_cancelled", "open live crypto orders cancelled by confirmed command", _scrub_sensitive(result))
    print(json.dumps(_scrub_sensitive(result), indent=2))
    return result


def return_to_paper(confirm: bool = False) -> None:
    if not confirm:
        raise SystemExit("Refusing to return to paper without --yes")
    rules, strategy_config = load_settings()
    rules.setdefault("trading", {})["enabled"] = True
    rules.setdefault("trading", {})["mode"] = "paper"
    write_env_allowed(ROOT / ".env", {"TRADING_MODE": "paper", "TRADING_ENABLED": "true"})
    with (ROOT / "config" / "trading_rules.yaml").open("w", encoding="utf-8") as handle:
        yaml.safe_dump(rules, handle, sort_keys=False)
    with (ROOT / "config" / "strategy.yaml").open("w", encoding="utf-8") as handle:
        yaml.safe_dump(strategy_config, handle, sort_keys=False)
    SQLiteLogger(ROOT / "data" / "trading_agent.db").log_decision(None, "return_to_paper", "CLI returned env and config to paper mode")
    print("Returned TRADING_MODE and config trading.mode to paper.")


def signal_details(signal: TradeSignal) -> dict[str, Any]:
    return {
        "symbol": signal.symbol,
        "profile": signal.profile,
        "history_points": signal.history_points,
        "current_mid": signal.current_mid,
        "ema_20": signal.ema_20,
        "ema_50": signal.ema_50,
        "rsi_14": signal.rsi_14,
        "momentum_5": signal.momentum_5,
        "buy_conditions_met": list(signal.buy_conditions_met),
        "sell_conditions_met": list(signal.sell_conditions_met),
        "conditions_failed": list(signal.conditions_failed),
        "final_signal": signal.final_signal,
        "signal": signal.strategy_signal,
        "side": signal.side,
        "confidence": signal.confidence,
        "reason": signal.reason,
    }


def run_cycle(mode: str, rules: dict[str, Any], strategy_config: dict[str, Any]) -> None:
    effective_rules = dict(rules)
    effective_rules["trading"] = dict(rules.get("trading", {}))
    effective_rules["trading"]["allowed_symbols"] = allowed_symbols_for_mode(mode, rules)
    logger = SQLiteLogger(ROOT / "data" / "trading_agent.db")
    # ROOT-anchored: a STOP_TRADING created by the CLI (or anywhere) halts this
    # cycle no matter what CWD the loop is running from.
    kill = _project_kill_switch(effective_rules)
    kill_reasons = kill.halt_reasons()
    if kill_reasons:
        logger.log_decision(None, "halted", "; ".join(kill_reasons))
        return

    client = make_client()
    paper_broker = PaperBroker(ROOT / "data" / "paper_trades.db")
    live_broker: LiveBroker | None = None
    portfolio = paper_broker.get_portfolio() if mode == "paper" else Portfolio()

    if client.has_credentials and mode in {"live-dry-run", "live"}:
        try:
            account_payload = client.get_accounts()
            account_number = select_account_number(account_payload)
            live_broker = LiveBroker(client, dry_run=mode == "live-dry-run", account_number=account_number)
            holdings_payload = client.get_holdings(account_number)
            portfolio = Portfolio.from_robinhood(account_payload, holdings_payload)
            logger.log_decision(None, "account_loaded", "account balances and holdings pulled", {"account_number": account_number})
        except Exception as exc:
            logger.log_error("account_lookup", str(exc))
            logger.log_decision(None, "account_lookup_failed", str(exc))

    market_data = MarketDataService(client if client.has_credentials else None, ROOT / "data" / "market_data.db")
    strategy = StrategyEngine(strategy_config, effective_rules)
    risk = RiskManager(effective_rules, kill)
    orders = OrderManager(effective_rules, risk, logger, paper_broker, live_broker)
    symbols = effective_rules.get("trading", {}).get("allowed_symbols", [])

    prices: dict[str, float] = {}
    try:
        prices = market_data.get_latest_prices(symbols)
        logger.log_decision(None, "market_data_loaded", "market data lookup completed", {"symbols": symbols, "prices_found": list(prices)})
    except Exception as exc:
        logger.log_error("market_data", str(exc))
        logger.log_decision(None, "market_data_failed", str(exc))

    for symbol in symbols:
        kill_reasons = kill.halt_reasons()
        if kill_reasons:
            reason = "; ".join(kill_reasons)
            logger.log_decision(symbol, "halted", reason)
            continue

        price = prices.get(symbol)
        if price is not None:
            history = market_data.history_for(symbol)
        else:
            history = []
        has_open_position = portfolio.quantity_for(symbol) > 0
        allow_position_scaling = bool(rules.get("risk", {}).get("allow_position_scaling", False))
        signal = strategy.generate_signal(
            symbol,
            history,
            has_open_position=has_open_position,
            allow_position_scaling=allow_position_scaling,
        )
        logger.log_decision(symbol, "signal_generated", signal.reason, signal_details(signal))
        if signal.reason == "sell_signal_ignored_no_position":
            logger.log_decision(symbol, "sell_signal_ignored_no_position", signal.reason, signal_details(signal))
        if signal.side in {"buy", "sell"}:
            filtered_signal, intelligence = apply_intelligence_filter(ROOT, signal)
            logger.log_decision(
                symbol,
                "intelligence_scored",
                f"intelligence recommendation={intelligence.get('recommendation', 'n/a')}",
                intelligence,
            )
            if filtered_signal.side != signal.side:
                logger.log_decision(symbol, "intelligence_blocked", filtered_signal.reason, intelligence)
            signal = filtered_signal
        if signal.side in {"buy", "sell"} and price:
            result = orders.process_signal(
                signal=signal,
                limit_price=price,
                mode=mode,
                portfolio=portfolio,
                daily_summary=logger.get_daily_summary(),
                has_api_credentials=client.has_credentials,
            )
            if result and mode == "paper":
                portfolio = paper_broker.get_portfolio()


def run_mode(command_mode: str, once: bool) -> None:
    rules, strategy_config = load_settings()
    mode = runtime_mode(command_mode, rules)
    if command_mode == "live" and os.getenv("TRADING_MODE") != "live":
        raise SystemExit("Refusing live run: .env must contain TRADING_MODE=live")
    print(f"Starting mode={mode}. Press Ctrl+C to stop.")
    while True:
        run_cycle(mode, rules, strategy_config)
        if once:
            return
        time.sleep(int(os.getenv("POLL_INTERVAL_SECONDS", "60")))


def run_paper_loop(iterations: int | None = None, hours: float | None = None) -> None:
    rules, strategy_config = load_settings()
    mode = runtime_mode("paper", rules)
    interval = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
    # ROOT-anchored so the loop's stop-file check resolves the same path the CLI
    # writes, whatever the CWD.
    kill = _project_kill_switch(rules)
    completed = 0
    deadline = time.monotonic() + (hours * 60 * 60) if hours is not None else None
    print(f"Starting paper loop mode={mode}. Press Ctrl+C to stop.")
    if hours is not None:
        print(f"hours={hours} poll_interval_seconds={interval}")
    while True:
        if iterations is not None and completed >= iterations:
            return
        if deadline is not None and time.monotonic() >= deadline:
            print(f"Completed paper loop hours={hours} iterations={completed}")
            return
        if kill.stop_file_exists():
            SQLiteLogger(ROOT / "data" / "trading_agent.db").log_decision(None, "halted", f"{kill.stop_file} exists")
            print(f"Stopped: {kill.stop_file} exists")
            return
        run_cycle("paper", rules, strategy_config)
        completed += 1
        if iterations is not None:
            continue
        if deadline is not None and time.monotonic() >= deadline:
            print(f"Completed paper loop hours={hours} iterations={completed}")
            return
        time.sleep(interval)


def decision_count(logger: SQLiteLogger, action: str) -> int:
    with logger.connect() as conn:
        row = conn.execute("SELECT COUNT(*) FROM decisions WHERE action = ?", (action,)).fetchone()
    return int(row[0] if row else 0)


def submitted_true_count(logger: SQLiteLogger) -> int:
    with logger.connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*)
            FROM decisions
            WHERE action = 'dry_run_order_preview'
              AND details LIKE '%"submitted": true%'
            """
        ).fetchone()
    return int(row[0] if row else 0)


def run_dry_loop(iterations: int) -> None:
    rules, strategy_config = load_settings()
    logger = SQLiteLogger(ROOT / "data" / "trading_agent.db")
    # ROOT-anchored so a STOP_TRADING created by the CLI halts this loop
    # regardless of the CWD it was launched from.
    kill = _project_kill_switch(rules)
    preview_start = decision_count(logger, "dry_run_order_preview")
    blocked_start = decision_count(logger, "dry_run_blocked")
    submitted_true_start = submitted_true_count(logger)
    completed = 0
    print("Starting dry-run loop mode=live-dry-run submitted=false.")
    for index in range(iterations):
        if kill.stop_file_exists():
            logger.log_decision(None, "halted", f"{kill.stop_file} exists", {"iteration": index + 1, "submitted": False})
            print(f"Stopped: {kill.stop_file} exists")
            break
        run_cycle("live-dry-run", rules, strategy_config)
        completed += 1

    summary = {
        "submitted": False,
        "iterations_completed": completed,
        "live_style_order_previews": decision_count(logger, "dry_run_order_preview") - preview_start,
        "blocked_dry_run_signals": decision_count(logger, "dry_run_blocked") - blocked_start,
        "submitted_true_count": submitted_true_count(logger) - submitted_true_start,
    }
    print(json.dumps(summary, indent=2))


def collect_market_data(iterations: int, sleep_seconds: int | None = None) -> None:
    rules, _ = load_settings()
    logger = SQLiteLogger(ROOT / "data" / "trading_agent.db")
    kill = KillSwitch(
        stop_file=rules.get("kill_switch", {}).get("stop_file", "STOP_TRADING"),
        env_var=rules.get("kill_switch", {}).get("env_var", "TRADING_ENABLED"),
    )
    client = make_client()
    if not client.has_credentials:
        logger.log_decision(None, "market_data_collection_failed", "Robinhood API credentials are missing")
        raise SystemExit("Missing ROBINHOOD_API_KEY or ROBINHOOD_PRIVATE_KEY in .env")

    symbols = rules.get("trading", {}).get("allowed_symbols", [])
    market_data = MarketDataService(client, ROOT / "data" / "market_data.db")
    interval = int(sleep_seconds if sleep_seconds is not None else os.getenv("POLL_INTERVAL_SECONDS", "60"))
    for index in range(iterations):
        if kill.stop_file_exists():
            logger.log_decision(None, "market_data_collection_halted", f"{kill.stop_file} exists", {"iteration": index})
            print(f"Stopped: {kill.stop_file} exists")
            return
        prices = market_data.get_latest_prices(symbols)
        logger.log_decision(
            None,
            "market_data_collected",
            "quote snapshots collected",
            {"iteration": index + 1, "symbols": symbols, "prices_found": list(prices)},
        )
        print(f"collected iteration={index + 1} symbols={len(prices)} total_rows={market_data.total_rows()}")
        if index < iterations - 1:
            time.sleep(interval)


def reset_paper(confirm: bool = False) -> None:
    if not confirm:
        raise SystemExit("Refusing to reset paper trades without --yes")
    logger = SQLiteLogger(ROOT / "data" / "trading_agent.db")
    broker = PaperBroker(ROOT / "data" / "paper_trades.db")
    archived_count = broker.reset(archive=True)
    logger.log_decision(None, "paper_reset", "paper trades archived and reset", {"archived_count": archived_count})
    print(f"paper_reset archived_count={archived_count}")


def reset_daily_paper(confirm: bool = False) -> None:
    if not confirm:
        raise SystemExit("Refusing to reset daily paper counters without --yes")
    logger = SQLiteLogger(ROOT / "data" / "trading_agent.db")
    result = logger.reset_daily_summary()
    logger.log_decision(None, "daily_paper_reset", "daily paper counters reset", result)
    print(json.dumps(result, indent=2))


def reconcile_paper(epsilon: float = 0.000001) -> None:
    logger = SQLiteLogger(ROOT / "data" / "trading_agent.db")
    broker = PaperBroker(ROOT / "data" / "paper_trades.db")
    result = broker.reconcile_positions(epsilon=epsilon)
    logger.log_decision(None, "paper_reconcile", "paper positions reconciled", result)
    print(json.dumps(result, indent=2))
    if result["errors"]:
        raise SystemExit("Paper reconciliation found negative positions larger than epsilon; reset paper or inspect trades before continuing.")


def backtest() -> None:
    rules, strategy_config = load_settings()
    engine = StrategyEngine(strategy_config, rules)
    prices = [100 + (index * 0.05) for index in range(220)]
    signal = engine.generate_signal("BTC-USD", prices)
    SQLiteLogger(ROOT / "data" / "trading_agent.db").log_decision("BTC-USD", "backtest_signal", signal.reason, {"side": signal.side})
    print({"symbol": signal.symbol, "side": signal.side, "reason": signal.reason})


def run_dashboard() -> None:
    import uvicorn

    from .dashboard import dashboard_app

    print("Starting dashboard at http://127.0.0.1:8000")
    uvicorn.run(dashboard_app(ROOT), host="127.0.0.1", port=8000)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Rules-based Robinhood Crypto trading agent")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    for name in ("run-paper", "run-dry", "run-live"):
        run_parser = sub.add_parser(name)
        run_parser.add_argument("--once", action="store_true", help="run one poll cycle and exit")
    loop_parser = sub.add_parser("run-paper-loop")
    loop_parser.add_argument("--iterations", type=int, default=None, help="run a limited number of paper cycles")
    loop_parser.add_argument("--hours", type=float, default=None, help="run paper cycles for a limited number of hours")
    dry_loop_parser = sub.add_parser("run-dry-loop")
    dry_loop_parser.add_argument("--iterations", type=int, required=True, help="run a limited number of dry-run cycles")
    collect_parser = sub.add_parser("collect-market-data")
    collect_parser.add_argument("--iterations", type=int, required=True)
    collect_parser.add_argument("--sleep", type=int, default=None)
    reset_parser = sub.add_parser("reset-paper")
    reset_parser.add_argument("--yes", action="store_true")
    reset_daily_parser = sub.add_parser("reset-daily-paper")
    reset_daily_parser.add_argument("--yes", action="store_true")
    return_paper_parser = sub.add_parser("return-paper")
    return_paper_parser.add_argument("--yes", action="store_true")
    reconcile_parser = sub.add_parser("reconcile-paper")
    reconcile_parser.add_argument("--epsilon", type=float, default=0.000001)
    live_readiness_parser = sub.add_parser("live-readiness")
    live_readiness_parser.add_argument("--run-tests", action="store_true")
    live_readiness_parser.add_argument("--check-connection", action="store_true")
    # The equities analog. It always runs the full suite -- an equities gate is
    # only "proven" by tests that just ran, so there is no --run-tests flag to
    # leave off. Read-only: it never touches the connector or a posture.
    equity_readiness_parser = sub.add_parser("equity-live-readiness")
    equity_readiness_parser.add_argument("--format", choices=["json", "markdown"], default="json")
    equity_readiness_parser.add_argument("--output", default=None, help="write the report to this path instead of stdout")
    # The OPTIONS analog of equity-live-readiness (src/option_readiness.py): runs
    # the full suite, proves every options gate against it, reads config + audit
    # log for the evidence no test can speak to, and writes the verdict. Read-only.
    option_readiness_parser = sub.add_parser("option-live-readiness")
    option_readiness_parser.add_argument("--format", choices=["json", "markdown"], default="json")
    option_readiness_parser.add_argument("--output", default=None, help="write the report to this path instead of stdout")
    proving_parser = sub.add_parser(
        "run-equity-proving-run",
        help="A bounded, unattended equities paper proving run priced on REAL Massive history.",
    )
    proving_parser.add_argument("--iterations", type=int, default=60, help="bounded cycle count (default 60)")
    proving_parser.add_argument("--hours", type=float, default=None, help="alternative time bound")
    proving_parser.add_argument("--lookback-days", type=int, default=None, help="Massive history depth per symbol")
    proving_parser.add_argument("--cash", default="10000.00", help="paper starting cash")
    # The OPTIONS analog: a bounded, unattended DEFINED-RISK options paper proving
    # run priced on a REAL basis (real Massive underlying closes x an expected-move
    # fraction). Fresh ledger each run, reconciled clean, basis recorded. Paper
    # only -- it never touches the connector's order path.
    option_proving_parser = sub.add_parser(
        "run-option-proving-run",
        help="A bounded, unattended DEFINED-RISK options paper proving run priced on a REAL basis.",
    )
    option_proving_parser.add_argument("--iterations", type=int, default=60, help="bounded cycle count (default 60)")
    option_proving_parser.add_argument("--hours", type=float, default=None, help="alternative time bound")
    option_proving_parser.add_argument("--lookback-days", type=int, default=None, help="Massive underlying history depth")
    option_proving_parser.add_argument(
        "--expected-move-fraction", type=float, default=None, help="premium as this fraction of the real close"
    )
    option_proving_parser.add_argument("--dte", type=int, default=None, help="days to expiry for framed contracts")
    sub.add_parser("validate-symbols")
    sub.add_parser("collect-intelligence")
    sub.add_parser("intelligence-status")
    score_symbol_parser = sub.add_parser("score-symbol")
    score_symbol_parser.add_argument("symbol")
    sub.add_parser("score-all-symbols")
    intelligence_report_parser = sub.add_parser("intelligence-report")
    intelligence_report_parser.add_argument("--output", default=None)
    audit_parser = sub.add_parser("export-live-audit")
    audit_parser.add_argument("--output", default=None)
    audit_parser.add_argument("--limit", type=int, default=500)
    sub.add_parser("reconcile-live-orders")
    cancel_live_parser = sub.add_parser("cancel-open-live-orders")
    cancel_live_parser.add_argument("--confirm-cancel-live", action="store_true")
    sub.add_parser("status")
    sub.add_parser("dashboard")
    sub.add_parser("stop")
    sub.add_parser("backtest")
    sub.add_parser("test-connection")
    preview_parser = sub.add_parser("preview-order")
    preview_parser.add_argument("symbol")
    preview_parser.add_argument("side", choices=["buy", "sell"])
    preview_parser.add_argument("amount_usd", type=float)
    preview_buy_parser = sub.add_parser("preview-buy")
    preview_buy_parser.add_argument("symbol")
    preview_buy_parser.add_argument("amount_usd", type=float)
    preview_sell_parser = sub.add_parser("preview-sell")
    preview_sell_parser.add_argument("symbol")
    preview_sell_parser.add_argument("amount_usd", type=float)
    seed_parser = sub.add_parser("seed-paper-position")
    seed_parser.add_argument("symbol")
    seed_parser.add_argument("amount_usd", type=float)
    smoke_buy_parser = sub.add_parser("live-smoke-buy")
    smoke_buy_parser.add_argument("symbol")
    smoke_buy_parser.add_argument("amount_usd", type=float)
    smoke_buy_parser.add_argument("--confirm-live-smoke", action="store_true")
    smoke_sell_parser = sub.add_parser("live-smoke-sell")
    smoke_sell_parser.add_argument("symbol")
    smoke_sell_parser.add_argument("amount_usd", type=float)
    smoke_sell_parser.add_argument("--confirm-live-smoke", action="store_true")
    live_loop_parser = sub.add_parser("run-live-loop")
    live_loop_parser.add_argument("--hours", type=float, default=None)
    live_loop_parser.add_argument("--iterations", type=int, default=None)
    live_loop_parser.add_argument("--confirm-unattended-live", action="store_true")
    live_loop_parser.add_argument("--confirm-bounded-live", action="store_true")
    live_single_parser = sub.add_parser("live-single-trade")
    live_single_parser.add_argument("symbol")
    live_single_parser.add_argument("side", choices=["buy", "sell"])
    live_single_parser.add_argument("amount_usd", type=float)
    live_single_parser.add_argument("--confirm-live", action="store_true")
    # ANALYSIS-ONLY daily options screener. Reads market data and composes/sends
    # a ranked email of CANDIDATE plays. It never trades -- no order path.
    scout_parser = sub.add_parser(
        "options-scout-email",
        help="ANALYSIS ONLY: email a ranked list of candidate options plays (never trades).",
    )
    scout_parser.add_argument("--dry-run", action="store_true", help="compose and print the email; do not send")
    scout_parser.add_argument("--top", type=int, default=None, help="how many ranked plays to include")
    scout_parser.add_argument("--out", default=None, help="write the composed HTML to this path")
    # ANALYSIS-ONLY sector-first six-month options research report. Sibling to
    # options-scout-email: same email path, different question, different clock.
    sector_parser = sub.add_parser(
        "sector-scout-email",
        help="ANALYSIS ONLY: email the sector-first six-month options research report (never trades).",
    )
    sector_parser.add_argument("--dry-run", action="store_true", help="compose and print the email; do not send")
    sector_parser.add_argument("--top", type=int, default=None, help="how many segment plays to include")
    sector_parser.add_argument("--out", default=None, help="write the composed HTML to this path")
    sector_parser.add_argument(
        "--rh-snapshot", default=None,
        help="path to a connector-filled Robinhood snapshot JSON (see sector-scout-manifest)",
    )
    # Emits the batched connector-call manifest an agent session services to
    # fill a Robinhood snapshot. ANALYSIS ONLY -- prints JSON, calls nothing.
    manifest_parser = sub.add_parser(
        "sector-scout-manifest",
        help="print the Robinhood snapshot manifest (the connector calls an agent must make)",
    )
    manifest_parser.add_argument("--out", default=None, help="write the manifest JSON to this path")
    # Publishes a connector-filled snapshot to the state store (local + GCS)
    # so the Cloud Run job picks it up. Schema-checked BEFORE the write: a
    # malformed file is refused rather than shadowing a good blob.
    push_parser = sub.add_parser(
        "sector-scout-snapshot-push",
        help="validate a Robinhood snapshot JSON and publish it to the sector-scout state store",
    )
    push_parser.add_argument("--snapshot", required=True, help="path to the snapshot JSON to publish")
    push_parser.add_argument(
        "--force", action="store_true",
        help="overwrite even when the stored blob is newer than this file",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "init":
        init_project()
    elif args.command == "status":
        status()
    elif args.command == "dashboard":
        run_dashboard()
    elif args.command == "stop":
        rules, _ = load_settings()
        stop_trading(rules)
    elif args.command == "backtest":
        backtest()
    elif args.command == "test-connection":
        test_connection()
    elif args.command == "preview-order":
        preview_order(args.symbol.upper(), args.side, args.amount_usd)
    elif args.command == "preview-buy":
        preview_side(args.symbol.upper(), "buy", args.amount_usd)
    elif args.command == "preview-sell":
        preview_side(args.symbol.upper(), "sell", args.amount_usd)
    elif args.command == "seed-paper-position":
        seed_paper_position(args.symbol.upper(), args.amount_usd)
    elif args.command == "live-smoke-buy":
        live_smoke_order(args.symbol.upper(), "buy", args.amount_usd, args.confirm_live_smoke)
    elif args.command == "live-smoke-sell":
        live_smoke_order(args.symbol.upper(), "sell", args.amount_usd, args.confirm_live_smoke)
    elif args.command == "run-live-loop":
        if args.confirm_bounded_live or args.iterations is not None:
            run_bounded_live_loop(args.iterations or 1, args.confirm_bounded_live)
        else:
            if args.hours is None:
                raise SystemExit("run-live-loop requires --hours for unattended live or --iterations with --confirm-bounded-live")
            run_live_loop(args.hours, args.confirm_unattended_live)
    elif args.command == "live-readiness":
        print(json.dumps(live_launch_readiness(args.run_tests, args.check_connection), indent=2))
    elif args.command == "equity-live-readiness":
        equity_live_readiness_command(args.format, args.output)
    elif args.command == "option-live-readiness":
        option_live_readiness_command(args.format, args.output)
    elif args.command == "run-equity-proving-run":
        from src.equity_runtime import run_equity_proving_run, build_paper_proving_connector

        connector = build_paper_proving_connector(ROOT, cash=args.cash)
        kwargs: dict[str, Any] = {}
        if args.iterations is not None:
            kwargs["iterations"] = args.iterations
        if args.hours is not None:
            kwargs["hours"] = args.hours
        if args.lookback_days is not None:
            kwargs["lookback_days"] = args.lookback_days
        summary = run_equity_proving_run(connector, ROOT, **kwargs)
        print(json.dumps(summary, indent=2, default=str))
    elif args.command == "run-option-proving-run":
        from src.option_runtime import build_option_paper_proving_connector, run_option_proving_run

        connector = build_option_paper_proving_connector(ROOT)
        kwargs = {}
        if args.iterations is not None:
            kwargs["iterations"] = args.iterations
        if args.hours is not None:
            kwargs["hours"] = args.hours
        if args.lookback_days is not None:
            kwargs["lookback_days"] = args.lookback_days
        if args.expected_move_fraction is not None:
            kwargs["expected_move_fraction"] = args.expected_move_fraction
        if args.dte is not None:
            kwargs["days_to_expiry"] = args.dte
        summary = run_option_proving_run(connector, ROOT, **kwargs)
        print(json.dumps(summary, indent=2, default=str))
    elif args.command == "validate-symbols":
        validate_symbols()
    elif args.command == "collect-intelligence":
        collect_intelligence()
    elif args.command == "intelligence-status":
        intelligence_status()
    elif args.command == "score-symbol":
        score_symbol_command(args.symbol)
    elif args.command == "score-all-symbols":
        score_all_symbols_command()
    elif args.command == "intelligence-report":
        intelligence_report(args.output)
    elif args.command == "export-live-audit":
        export_live_audit(args.output, args.limit)
    elif args.command == "reconcile-live-orders":
        reconcile_live_orders()
    elif args.command == "cancel-open-live-orders":
        cancel_open_live_orders(args.confirm_cancel_live)
    elif args.command == "live-single-trade":
        live_single_trade(args.symbol.upper(), args.side, args.amount_usd, args.confirm_live)
    elif args.command == "options-scout-email":
        from src.options_scout import run_options_scout_email

        result = run_options_scout_email(dry_run=args.dry_run, top_n=args.top, out_path=args.out)
        if not args.dry_run:
            print(result.detail)
    elif args.command == "sector-scout-email":
        from src.sector_scout import run_sector_scout_email

        result = run_sector_scout_email(
            dry_run=args.dry_run, top_n=args.top, out_path=args.out,
            rh_snapshot_path=args.rh_snapshot,
        )
        if not args.dry_run:
            print(result.detail)
    elif args.command == "sector-scout-manifest":
        import json as _json

        from src.sector_scout.config import load_sector_config
        from src.sector_scout.robinhood_source import build_manifest

        manifest = build_manifest(load_sector_config())
        text = _json.dumps(manifest, indent=1)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as handle:
                handle.write(text)
            print(f"manifest written to {args.out}")
        else:
            print(text)
    elif args.command == "sector-scout-snapshot-push":
        from src.sector_scout.config import load_sector_config, resolve_state_dir
        from src.sector_scout.robinhood_source import load_snapshot
        from src.sector_scout.state import StateStore, resolve_bucket

        snap = load_snapshot(args.snapshot)
        if snap is None:
            print(f"REFUSED: {args.snapshot} is unreadable or fails the schema check; nothing pushed")
            return 1
        config = load_sector_config()
        store = StateStore(
            resolve_state_dir(config),
            resolve_bucket(config),
            (config.get("state") or {}).get("gcs_prefix", "sector-scout"),
        )
        wrote = store.push_rh_snapshot(args.snapshot, force=args.force)
        if wrote.startswith("refused"):
            print(f"REFUSED ({wrote}): nothing pushed"
                  + (" -- pass --force to overwrite a newer stored blob"
                     if wrote == "refused_older_than_stored" else ""))
            return 1
        if wrote == "failed":
            print("FAILED: snapshot not persisted anywhere (local write failed, GCS not reached)")
            return 1
        print(
            f"snapshot pushed ({wrote}); generated_at {snap.generated_at} "
            f"({snap.age_label()}), {len(snap.fundamentals)} fundamentals, "
            f"{len(snap.option_quotes)} option quotes"
        )
        if "gcs" not in wrote:
            print("WARNING: local only -- GCS not reached; the Cloud Run job will not see this push")
            return 1
    elif args.command == "run-paper":
        run_mode("paper", args.once)
    elif args.command == "run-paper-loop":
        run_paper_loop(args.iterations, args.hours)
    elif args.command == "run-dry-loop":
        run_dry_loop(args.iterations)
    elif args.command == "collect-market-data":
        collect_market_data(args.iterations, args.sleep)
    elif args.command == "reset-paper":
        reset_paper(args.yes)
    elif args.command == "reset-daily-paper":
        reset_daily_paper(args.yes)
    elif args.command == "return-paper":
        return_to_paper(args.yes)
    elif args.command == "reconcile-paper":
        reconcile_paper(args.epsilon)
    elif args.command == "run-dry":
        if args.once:
            dry_run_preview_once()
        else:
            run_mode("live-dry-run", args.once)
    elif args.command == "run-live":
        run_mode("live", args.once)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
