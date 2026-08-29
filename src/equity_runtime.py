"""The equities lane's bounded, unattended paper-proving runtime.

The equities lane is AGENT-HOSTED (agent/docs/rh-equities-binding.md): the
Robinhood connector is session-bound, so there is no VM-daemon entry point
here the way run-paper-loop is for the crypto lane. Instead this module
exposes plain functions that take an already-resolved connector -- whatever
harness/Claude agent session holds the OAuth connector calls these directly.

Every function here is paper-only. Nothing in this module can place a real
order: run_equity_cycle always builds its RobinhoodEquityBroker with
dry_run=True, confirm_live_order=False, and routes every signal through
OrderManager in mode="paper", which never touches the broker's order path at
all -- fills are simulated by the local PaperBroker, unchanged.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from .equity_market_data import EquityMarketDataService
from .equity_symbols import equities_universe
from .kill_switch import KillSwitch
from .logger import SQLiteLogger
from .order_manager import OrderManager
from .paper_broker import PaperBroker
from .risk_manager import RiskManager
from .robinhood_equity_broker import RobinhoodEquityBroker
from .robinhood_equity_client import EquityConnector, RobinhoodEquityClient
from .strategy_engine import StrategyEngine

VENUE = "robinhood_equities"


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def load_equity_settings(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Loads .env the same way main.py's load_settings() does -- the kill
    switch's TRADING_ENABLED read is a process env var, not a config value,
    so a caller that skips this (an isolated script, a fresh interpreter)
    would otherwise see every cycle halt with TRADING_ENABLED=false."""
    load_dotenv(root / ".env", override=False)
    return _load_yaml(root / "config" / "trading_rules.yaml"), _load_yaml(root / "config" / "strategy.yaml")


def equity_kill_switch(rules: dict[str, Any], root: Path) -> KillSwitch:
    """The equities lane's OWN kill switch instance -- a separate stop file
    from the crypto lane's STOP_TRADING (config's top-level kill_switch:),
    so the crypto disarm in place for this build neither blocks nor is
    touched by equities paper proving. Same KillSwitch class, own file."""
    equities_kill = rules.get("equities", {}).get("kill_switch", {})
    stop_file = equities_kill.get("stop_file", "STOP_TRADING_EQUITIES")
    stop_path = Path(stop_file)
    if not stop_path.is_absolute():
        stop_path = root / stop_path
    return KillSwitch(stop_file=str(stop_path), env_var=equities_kill.get("env_var", "TRADING_ENABLED"))


def equity_effective_rules(rules: dict[str, Any]) -> dict[str, Any]:
    """The shared RiskManager reads trading.allowed_symbols; the equities
    lane's allowlist is config's equities.universe, never trading.allowed_symbols
    or symbols.* (the crypto lane's own lists)."""
    effective = dict(rules)
    effective["trading"] = dict(rules.get("trading", {}))
    effective["trading"]["allowed_symbols"] = equities_universe(rules)
    return effective


def run_equity_cycle(connector: EquityConnector, root: Path, logger: SQLiteLogger | None = None) -> dict[str, Any]:
    """One regular-hours-agnostic paper pass over the equities universe:
    read quotes, generate a signal per symbol, and either simulate a paper
    fill or log a readable skip rationale. Always paper -- mode is hardcoded.
    """
    rules, strategy_config = load_equity_settings(root)
    logger = logger or SQLiteLogger(root / "data" / "trading_agent.db")
    kill = equity_kill_switch(rules, root)
    kill_reasons = kill.halt_reasons()
    if kill_reasons:
        logger.log_decision(None, "equity_halted", "; ".join(kill_reasons), {"venue": VENUE})
        return {"halted": True, "reasons": kill_reasons, "results": {}}

    client = RobinhoodEquityClient(connector)
    paper_broker = PaperBroker(root / "data" / "equity_paper_trades.db")
    market_data = EquityMarketDataService(client, root / "data" / "equity_market_data.db")
    effective_rules = equity_effective_rules(rules)
    strategy = StrategyEngine(strategy_config, effective_rules)
    risk = RiskManager(effective_rules, kill)
    equity_broker = RobinhoodEquityBroker(client, dry_run=True, confirm_live_order=False, kill_switch=kill, logger=logger)
    order_manager = OrderManager(effective_rules, risk, logger, paper_broker, equity_broker)
    portfolio = paper_broker.get_portfolio()
    symbols = effective_rules["trading"]["allowed_symbols"]

    try:
        prices = market_data.get_latest_prices(symbols)
        logger.log_decision(
            None,
            "equity_market_data_loaded",
            "connector quote read completed",
            {"venue": VENUE, "symbols": symbols, "prices_found": sorted(prices)},
        )
    except Exception as exc:
        prices = {}
        logger.log_error("equity_market_data", str(exc), {"venue": VENUE})
        logger.log_decision(None, "equity_market_data_failed", str(exc), {"venue": VENUE})

    results: dict[str, Any] = {}
    for symbol in symbols:
        if kill.halt_reasons():
            continue
        price = prices.get(symbol)
        if price is None:
            logger.log_decision(
                symbol,
                "equity_signal_skipped",
                f"no quote available for {symbol} this cycle; strategy not evaluated",
                {"venue": VENUE},
            )
            results[symbol] = None
            continue
        history = market_data.recent_history(symbol)
        has_open_position = portfolio.quantity_for(symbol) > 0
        signal = strategy.generate_equity_signal(symbol, history, has_open_position=has_open_position)
        result = equity_broker.submit_signal(
            order_manager,
            signal,
            limit_price=price,
            mode="paper",
            portfolio=portfolio,
            daily_summary=logger.get_daily_summary(),
        )
        results[symbol] = result
        if result is not None:
            portfolio = paper_broker.get_portfolio()
    return {"halted": False, "results": results}


def run_equity_paper_loop(
    connector: EquityConnector,
    root: Path,
    iterations: int | None = None,
    hours: float | None = None,
    poll_interval_seconds: int = 60,
    sleep=time.sleep,
) -> dict[str, Any]:
    """A BOUNDED, unattended paper loop -- iterations and/or hours cap it so
    it always terminates on its own rather than needing a human Ctrl+C, and
    the equities kill switch (not the crypto one) can still stop it early."""
    if iterations is None and hours is None:
        raise ValueError("run_equity_paper_loop requires iterations and/or hours -- an unbounded loop is refused")
    rules, _ = load_equity_settings(root)
    logger = SQLiteLogger(root / "data" / "trading_agent.db")
    kill = equity_kill_switch(rules, root)
    completed = 0
    halted = False
    deadline = time.monotonic() + (hours * 60 * 60) if hours is not None else None
    while True:
        if iterations is not None and completed >= iterations:
            break
        if deadline is not None and time.monotonic() >= deadline:
            break
        if kill.stop_file_exists():
            logger.log_decision(None, "equity_halted", f"{kill.stop_file} exists", {"venue": VENUE})
            halted = True
            break
        run_equity_cycle(connector, root, logger)
        completed += 1
        if iterations is not None and completed >= iterations:
            break
        if deadline is not None and time.monotonic() >= deadline:
            break
        sleep(poll_interval_seconds)
    if not halted:
        logger.log_decision(
            None,
            "equity_paper_loop_completed",
            f"bounded paper loop finished after {completed} iteration(s)",
            {"venue": VENUE, "iterations_completed": completed},
        )
    return {"iterations_completed": completed, "halted": halted}


def reconcile_equity_paper(root: Path, epsilon: float = 0.000001) -> dict[str, Any]:
    """Read-only-safe reconciliation of the equities paper ledger -- mirrors
    reconcile_paper() for the crypto lane, over data/equity_paper_trades.db."""
    logger = SQLiteLogger(root / "data" / "trading_agent.db")
    broker = PaperBroker(root / "data" / "equity_paper_trades.db")
    result = broker.reconcile_positions(epsilon=epsilon)
    logger.log_decision(None, "equity_paper_reconcile", "equities paper positions reconciled", {**result, "venue": VENUE})
    return result
