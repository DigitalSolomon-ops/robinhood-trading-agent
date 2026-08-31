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
from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol

import yaml
from dotenv import load_dotenv

from .equity_intelligence.indicator_signals import IndicatorProvider, build_indicator_provider
from .equity_intelligence.liquidity import LiquidityProvider, build_liquidity_provider
from .equity_intelligence.market_regime import (
    RegimeProvider,
    RegimeSnapshot,
    RegimeVerdict,
    build_regime_provider,
    evaluate_market_regime,
    market_regime_config,
)
from .equity_intelligence.massive_client import MIN_REQUEST_INTERVAL_SECONDS, MassiveClient
from .equity_intelligence.massive_history import (
    MAX_LOOKBACK_DAYS,
    QUOTE_SOURCE as MASSIVE_QUOTE_SOURCE,
    MassiveHistoryFeed,
)
from .equity_intelligence.news_sentiment import SentimentProvider, build_sentiment_provider
from .equity_market_data import CONNECTOR_QUOTE_SOURCE, EquityMarketDataService
from .equity_symbols import equities_universe, equity_tradability
from .kill_switch import KillSwitch
from .logger import SQLiteLogger
from .order_manager import OrderManager
from .paper_broker import PaperBroker
from .risk_manager import RiskManager
from .robinhood_equity_broker import RobinhoodEquityBroker
from .robinhood_equity_client import EquityConnector, RobinhoodEquityClient
from .strategy_engine import StrategyEngine

VENUE = "robinhood_equities"


class QuoteSource(Protocol):
    """Where a cycle's prices come from, and what that source is called.

    Two implementations exist and they are not interchangeable in posture:
    EquityMarketDataService reads the connector's live quote (the EXECUTION
    price), and MassiveHistoryFeed replays real historical daily bars (the
    PROVING/backtest price). The name travels with the prices so no run can
    later be mistaken for the other kind.
    """

    quote_source_name: str

    def get_prices(self, symbols: list[str], logger: SQLiteLogger | None = None) -> dict[str, float]: ...


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


def equity_market_regime(
    rules: dict[str, Any],
    provider: RegimeProvider | None,
    logger: SQLiteLogger | None = None,
) -> RegimeVerdict:
    """This cycle's market-regime verdict, with its rationale written down.

    One reading per cycle, about the market rather than a symbol. A provider
    that raises degrades to an errored snapshot -- a breadth feed going down
    must never take the lane down with it -- and with the section disabled the
    verdict is a no-op that logs nothing and leaves sizing exactly as it was.
    """
    config = market_regime_config(rules)
    snapshot: RegimeSnapshot | None = None
    if provider is not None:
        try:
            snapshot = provider.snapshot()
        except Exception as exc:  # noqa: BLE001 -- degrade, never crash
            snapshot = RegimeSnapshot(error=f"{type(exc).__name__}: {exc}")
    verdict = evaluate_market_regime(snapshot, config)
    if logger is not None and verdict.enabled:
        logger.log_decision(
            None,
            "equity_market_regime",
            verdict.rationale(),
            {
                "venue": VENUE,
                "regime": verdict.regime,
                "action": verdict.action,
                "size_multiplier": verdict.size_multiplier,
                "session": snapshot.session if snapshot is not None else "",
                "advance_ratio": snapshot.advance_ratio if snapshot is not None else 0.0,
                "counts": {name: value for name, value in (snapshot.counts() if snapshot is not None else ())},
                "notes": list(verdict.notes),
            },
        )
    return verdict


def _all_symbols_untradable(symbols: list[str], detail: str) -> dict[str, Any]:
    """A fail-CLOSED tradability report: the gate could not read, so nothing is
    evaluated. The opposite default (assume tradable when the check fails) is
    what turns a data outage into an unsupervised trade."""
    return {
        "universe": list(symbols),
        "available": [],
        "unavailable": list(symbols),
        "details": {symbol: {"active": False, "reason": detail} for symbol in symbols},
        "rationales": {
            symbol: (
                f"{symbol} is not tradable this cycle: the tradability gate could not be "
                f"evaluated ({detail}); strategy not evaluated"
            )
            for symbol in symbols
        },
    }


def run_equity_cycle(
    connector: EquityConnector,
    root: Path,
    logger: SQLiteLogger | None = None,
    indicator_provider: IndicatorProvider | None = None,
    quote_source: QuoteSource | None = None,
    sentiment_provider: SentimentProvider | None = None,
    liquidity_provider: LiquidityProvider | None = None,
    regime_provider: RegimeProvider | None = None,
) -> dict[str, Any]:
    """One regular-hours-agnostic paper pass over the equities universe:
    read quotes, generate a signal per symbol, and either simulate a paper
    fill or log a readable skip rationale. Always paper -- mode is hardcoded.

    `indicator_provider` supplies the Massive EOD readings the strategy
    modulates its signal with; when omitted, one is built from
    `equity_indicators:` in config/strategy.yaml (and is None unless that
    section is enabled). It is a READ-ONLY data source: it can only make an
    entry weaker or absent, and every risk gate still runs after it.

    `sentiment_provider` supplies the recent Massive news + per-ticker
    sentiment the PRE-TRADE RISK FILTER vetoes on; when omitted, one is built
    from `equities.news_sentiment:` in config/trading_rules.yaml (and is None
    unless that section is enabled). Same posture as the indicators: read-only,
    block-or-reduce only, and no substitute for a risk gate.

    `quote_source` is where this cycle's prices come from. Left None it is the
    connector's own live quote read (EquityMarketDataService) -- the execution
    price source, unchanged. A PROVING run passes a MassiveHistoryFeed instead,
    which replays real historical daily bars. Either way the source names
    itself, the name is written into every price row and into the cycle's audit
    rationale, and src/equity_readiness.py refuses to count a proving run whose
    recorded source is not the real Massive feed.

    `liquidity_provider` is the second half of the TRADABILITY GATE
    (src/equity_symbols.py, config section `equities.liquidity:`), which this
    cycle now runs on every pass -- it was dead code until this build. The
    connector-quote half refuses a halted/delisted/unquoted name; the liquidity
    half refuses one whose recent Massive daily bars are stale, thin or absent.
    Either refusal writes a rationale and removes the symbol from evaluation.
    It can only ever REMOVE a name, never add one.

    `regime_provider` supplies the market-wide advance/decline breadth the
    MARKET-REGIME BRAKE reads (config section `equities.market_regime:`). In a
    risk-off market it either blocks NEW ENTRIES outright or scales the
    per-trade cap DOWN, citing the breadth in the rationale either way. It never
    touches an exit and it never enlarges anything: the scaled cap is floored
    against the configured risk.max_trade_amount_usd, which RiskManager then
    checks again as usual.

    None of these four inputs is a substitute for a risk gate. Every one of them
    runs BEFORE RiskManager, OrderManager, the kill switch and the human
    confirm-flag, and removes nothing from any of them.
    """
    rules, strategy_config = load_equity_settings(root)
    logger = logger or SQLiteLogger(root / "data" / "trading_agent.db")
    kill = equity_kill_switch(rules, root)
    kill_reasons = kill.halt_reasons()
    if kill_reasons:
        logger.log_decision(None, "equity_halted", "; ".join(kill_reasons), {"venue": VENUE})
        return {"halted": True, "reasons": kill_reasons, "results": {}}

    client = RobinhoodEquityClient(connector, config_root=root)
    paper_broker = PaperBroker(root / "data" / "equity_paper_trades.db")
    market_data = EquityMarketDataService(client, root / "data" / "equity_market_data.db")
    effective_rules = equity_effective_rules(rules)
    strategy = StrategyEngine(strategy_config, effective_rules)
    risk = RiskManager(effective_rules, kill)
    # The broker's own universe gate reads the SAME list the shared RiskManager
    # gets as its allowlist (equity_effective_rules above), passed explicitly so
    # it is anchored to the `root` this cycle was given rather than the module's
    # ROOT default.
    equity_broker = RobinhoodEquityBroker(
        client,
        dry_run=True,
        confirm_live_order=False,
        kill_switch=kill,
        logger=logger,
        universe=equities_universe(rules),
    )
    order_manager = OrderManager(effective_rules, risk, logger, paper_broker, equity_broker)
    portfolio = paper_broker.get_portfolio()
    symbols = effective_rules["trading"]["allowed_symbols"]
    if indicator_provider is None:
        indicator_provider = build_indicator_provider(strategy_config)
    if sentiment_provider is None:
        sentiment_provider = build_sentiment_provider(effective_rules)
    if liquidity_provider is None:
        liquidity_provider = build_liquidity_provider(effective_rules)
    if regime_provider is None:
        regime_provider = build_regime_provider(effective_rules)

    source = quote_source if quote_source is not None else market_data
    source_name = getattr(source, "quote_source_name", CONNECTOR_QUOTE_SOURCE)
    connector_priced = source is market_data

    # ONE connector quote read per cycle, shared by the tradability gate and the
    # price ledger below, so wiring the gate in costs no extra poll. An empty
    # list (rather than None) after a failure means "the read happened and
    # returned nothing", which the gate fails closed on.
    quote_rows: list[dict[str, Any]] | None = None
    read_error: str | None = None
    if connector_priced:
        try:
            quote_rows = market_data.read_quotes(symbols)
        except Exception as exc:
            quote_rows, read_error = [], str(exc)
            logger.log_error("equity_market_data", str(exc), {"venue": VENUE, "quote_source": source_name})
            logger.log_decision(None, "equity_market_data_failed", str(exc), {"venue": VENUE, "quote_source": source_name})

    # --- tradability gate ---------------------------------------------------
    # Formerly dead code (validate_equity_symbols was never called by the
    # runtime). It now runs every cycle: a halted, delisted, unquoted, stale or
    # illiquid name is dropped here, with its own logged rationale, before the
    # strategy is ever asked about it.
    try:
        tradability = equity_tradability(
            client,
            effective_rules,
            logger=logger,
            quote_rows=quote_rows,
            liquidity_provider=liquidity_provider,
        )
    except Exception as exc:
        logger.log_error("equity_tradability", str(exc), {"venue": VENUE})
        tradability = _all_symbols_untradable(symbols, str(exc))
        logger.log_decision(
            None,
            "equity_symbols_validated",
            f"tradability gate failed to read ({exc}); no symbol is evaluated this cycle",
            {"venue": VENUE, "universe": symbols, "available": [], "unavailable": symbols},
        )
    tradable = list(tradability["available"])

    # --- market-regime brake -------------------------------------------------
    regime = equity_market_regime(effective_rules, regime_provider, logger)
    base_amount = float(effective_rules.get("risk", {}).get("max_trade_amount_usd", 0) or 0)
    min_size_usd = float(market_regime_config(effective_rules).get("min_size_usd", 0) or 0)

    prices: dict[str, float] = {}
    try:
        if connector_priced:
            prices = market_data.prices_from_rows(quote_rows or [], logger=logger, allowed=tradable)
        else:
            prices = source.get_prices(tradable, logger=logger)
            # A price that did not come from this service's own connector read
            # is persisted under the name of the source it DID come from, so
            # the candle ledger never carries a connector quote it never made.
            market_data.save_prices(prices, source=source_name)
        if read_error is None:
            logger.log_decision(
                None,
                "equity_market_data_loaded",
                f"price read completed from {source_name} for {len(tradable)} tradable symbol(s)",
                {
                    "venue": VENUE,
                    "symbols": symbols,
                    "tradable_symbols": tradable,
                    "prices_found": sorted(prices),
                    "quote_source": source_name,
                },
            )
    except Exception as exc:
        prices = {}
        logger.log_error("equity_market_data", str(exc), {"venue": VENUE, "quote_source": source_name})
        logger.log_decision(None, "equity_market_data_failed", str(exc), {"venue": VENUE, "quote_source": source_name})

    tradable_symbols = set(tradable)
    results: dict[str, Any] = {}
    for position, symbol in enumerate(symbols):
        halt_reasons = kill.halt_reasons()
        if halt_reasons:
            # A kill-switch trip between the pre-loop check and here (or partway
            # through the loop) must not leave the remaining symbols silently
            # unevaluated: 'every decision writes a rationale' means the halt is
            # itself a logged decision naming what it stopped.
            remaining = symbols[position:]
            logger.log_decision(
                None,
                "equity_halted",
                f"kill switch tripped mid-cycle ({'; '.join(halt_reasons)}); "
                f"{len(remaining)} symbol(s) not evaluated: {', '.join(remaining)}",
                {"venue": VENUE, "remaining_symbols": remaining},
            )
            break
        if symbol not in tradable_symbols:
            # The tradability gate above already wrote this symbol's rationale
            # naming exactly which half refused it; a second row here would only
            # repeat it.
            results[symbol] = None
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
        signal = strategy.generate_equity_signal(
            symbol,
            history,
            has_open_position=has_open_position,
            indicator_provider=indicator_provider,
            sentiment_provider=sentiment_provider,
        )
        if signal.sentiment_action:
            # The structured half of the news filter's rationale: the counts,
            # the negative share and the headline itself, alongside the
            # human-readable clause already inside signal.reason.
            counts = ", ".join(f"{name}={value}" for name, value in signal.sentiment_counts)
            logger.log_decision(
                symbol,
                "equity_sentiment_context",
                f"{counts}, negative_share={signal.sentiment_negative_ratio:.2f} "
                f"| headline: {signal.sentiment_headline or 'none'} "
                f"-> {'; '.join(signal.sentiment_notes) or 'no interpretation applied'}",
                {
                    "venue": VENUE,
                    "source": signal.sentiment_source,
                    "action": signal.sentiment_action,
                    "counts": {name: value for name, value in signal.sentiment_counts},
                    "negative_ratio": signal.sentiment_negative_ratio,
                    "headline": signal.sentiment_headline,
                    "confidence_multiplier": signal.sentiment_confidence_multiplier,
                    "rules_signal": signal.strategy_signal,
                    "final_signal": signal.final_signal,
                },
            )
        if signal.indicator_action:
            # The structured half of the rationale: the values themselves,
            # alongside the human-readable clause already inside signal.reason.
            cited = ", ".join(f"{name}={value:.2f}" for name, value in signal.indicator_values) or "no values read"
            logger.log_decision(
                symbol,
                "equity_indicator_context",
                f"{cited} -> {'; '.join(signal.indicator_notes) or 'no interpretation applied'}",
                {
                    "venue": VENUE,
                    "source": signal.indicator_source,
                    "action": signal.indicator_action,
                    "values": {name: value for name, value in signal.indicator_values},
                    "confidence_multiplier": signal.indicator_confidence_multiplier,
                    "rules_signal": signal.strategy_signal,
                    "final_signal": signal.final_signal,
                },
            )
        # --- market-regime sizing, entries only ----------------------------
        # An EXIT is never blocked and never scaled: shrinking a sell would
        # strand part of a position behind a breadth feed, which is the
        # opposite of a risk control. So amount_usd stays None for a sell and
        # OrderManager sizes it exactly as it always did.
        amount_usd: float | None = None
        if signal.side == "buy" and regime.enabled:
            regime_details = {
                "venue": VENUE,
                "regime": regime.regime,
                "action": regime.action,
                "size_multiplier": regime.size_multiplier,
                "rules_signal": signal.strategy_signal,
                "notes": list(regime.notes),
            }
            if regime.blocks_entries:
                logger.log_decision(
                    symbol,
                    "equity_regime_blocked",
                    f"entry skipped for {symbol}: {regime.rationale()} "
                    f"| rules signal was '{signal.strategy_signal}' ({signal.reason})",
                    regime_details,
                )
                results[symbol] = None
                continue
            amount_usd = regime.scaled_amount(base_amount)
            if amount_usd <= 0 or amount_usd < min_size_usd:
                logger.log_decision(
                    symbol,
                    "equity_regime_blocked",
                    f"entry skipped for {symbol}: regime-scaled trade size {amount_usd:.2f} is below "
                    f"min_size_usd {min_size_usd:.2f} | {regime.rationale()}",
                    {**regime_details, "scaled_amount_usd": amount_usd, "min_size_usd": min_size_usd},
                )
                results[symbol] = None
                continue
            # The rationale on the order itself cites the regime and the
            # before/after size, so a filled entry says why it was that big.
            signal = replace(
                signal,
                reason=(
                    f"{signal.reason} | {regime.rationale()} "
                    f"| trade size {base_amount:.2f} -> {amount_usd:.2f}"
                ),
            )

        result = equity_broker.submit_signal(
            order_manager,
            signal,
            limit_price=price,
            mode="paper",
            portfolio=portfolio,
            daily_summary=logger.get_daily_summary(),
            amount_usd=amount_usd,
        )
        results[symbol] = result
        if result is not None:
            portfolio = paper_broker.get_portfolio()
    return {
        "halted": False,
        "results": results,
        "quote_source": source_name,
        "tradable_symbols": tradable,
        "skipped_symbols": list(tradability["unavailable"]),
        "regime": regime.regime,
        "regime_action": regime.action,
        "regime_size_multiplier": regime.size_multiplier,
    }


def run_equity_paper_loop(
    connector: EquityConnector,
    root: Path,
    iterations: int | None = None,
    hours: float | None = None,
    poll_interval_seconds: int = 60,
    sleep=time.sleep,
    indicator_provider: IndicatorProvider | None = None,
    quote_source: QuoteSource | None = None,
    sentiment_provider: SentimentProvider | None = None,
    liquidity_provider: LiquidityProvider | None = None,
    regime_provider: RegimeProvider | None = None,
    massive_client: MassiveClient | None = None,
) -> dict[str, Any]:
    """A BOUNDED, unattended paper loop -- iterations and/or hours cap it so
    it always terminates on its own rather than needing a human Ctrl+C, and
    the equities kill switch (not the crypto one) can still stop it early.

    The loop RECORDS which price source it ran on, in the completion decision
    the readiness gate reads. A proving run passes the real Massive history
    feed (run_equity_proving_run does this for you); a loop left on the default
    connector quote records that instead, and src/equity_readiness.py will not
    count it toward the paper-proving gate.
    """
    if iterations is None and hours is None:
        raise ValueError("run_equity_paper_loop requires iterations and/or hours -- an unbounded loop is refused")
    rules, strategy_config = load_equity_settings(root)
    logger = SQLiteLogger(root / "data" / "trading_agent.db")
    kill = equity_kill_switch(rules, root)
    # ONE shared, THROTTLED Massive client for the whole loop. Every provider
    # (history, indicators, sentiment, liquidity, regime) draws from a single
    # request budget with a shared cache, instead of each spinning its own client
    # and collectively blowing the free tier's ~5/min limit. Built ONCE, not per
    # cycle: the readings are end-of-day for closed sessions, identical every
    # iteration, so re-polling per cycle would only burn the budget.
    shared_massive = massive_client or MassiveClient(min_interval=MIN_REQUEST_INTERVAL_SECONDS)

    def _massive_factory() -> MassiveClient:
        return shared_massive

    if liquidity_provider is None:
        liquidity_provider = build_liquidity_provider(rules, client_factory=_massive_factory)
    if regime_provider is None:
        regime_provider = build_regime_provider(rules, client_factory=_massive_factory)
    if indicator_provider is None:
        indicator_provider = build_indicator_provider(strategy_config, client_factory=_massive_factory)
    if sentiment_provider is None:
        sentiment_provider = build_sentiment_provider(rules, client_factory=_massive_factory)
    source_name = getattr(quote_source, "quote_source_name", CONNECTOR_QUOTE_SOURCE)
    provenance: dict[str, Any] = {"quote_source": source_name}
    if quote_source is not None and hasattr(quote_source, "provenance"):
        provenance = quote_source.provenance()
        logger.log_decision(
            None,
            "equity_quote_source",
            quote_source.describe() if hasattr(quote_source, "describe") else f"price source: {source_name}",
            {"venue": VENUE, **provenance},
        )
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
        run_equity_cycle(
            connector,
            root,
            logger,
            indicator_provider=indicator_provider,
            quote_source=quote_source,
            sentiment_provider=sentiment_provider,
            liquidity_provider=liquidity_provider,
            regime_provider=regime_provider,
        )
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
            f"bounded paper loop finished after {completed} iteration(s), priced from {source_name}",
            {
                "venue": VENUE,
                "iterations_completed": completed,
                # The claim the readiness gate audits. A run that did not price
                # itself off the real feed says so here, and is not counted.
                "quote_source": source_name,
                "quote_source_provenance": provenance,
            },
        )
    return {"iterations_completed": completed, "halted": halted, "quote_source": source_name}


def build_massive_quote_source(
    root: Path,
    client: MassiveClient | None = None,
    symbols: list[str] | None = None,
    lookback_days: int = MAX_LOOKBACK_DAYS,
) -> MassiveHistoryFeed:
    """The proving/backtest price source: REAL Massive historical daily bars
    for this lane's configured universe.

    Deliberately has no fallback. If the Massive key is absent or a symbol
    returns no bars, this raises and the proving run does not happen -- which
    is the point. A run that quietly substituted a generated series is exactly
    what the readiness gate now refuses to count.
    """
    rules, _ = load_equity_settings(root)
    universe = symbols if symbols is not None else equities_universe(rules)
    return MassiveHistoryFeed(client or MassiveClient(), universe, lookback_days=lookback_days)


class PaperProvingConnector:
    """Headless connector for a PAPER proving run.

    It exposes ONLY the agent-tradable account identity (read from
    config/trading_rules.yaml's equities.expected_account) so the client can
    resolve and identity-check the account pin, holds no real credential, and
    REFUSES every order path. A proving run prices on real Massive history and
    never places, reviews, or cancels a real order -- the connector is held for
    shape, not to trade. (The live Robinhood MCP connector is session-bound and
    unavailable to a headless run; this stands in for it in paper mode only.)
    """

    def __init__(self, expected: dict[str, str], cash: str = "10000.00") -> None:
        self._account = {
            "account_number": f"PAPER-AGENTIC-{expected['number_suffix']}",
            "nickname": expected["nickname"],
            "agentic_allowed": True,
            "cash_available_for_trading": cash,
        }

    def get_accounts(self) -> Any:
        return {"accounts": [self._account]}

    def get_equity_positions(self, account_number: str | None = None) -> Any:
        return {"positions": []}

    def get_equity_quotes(self, symbols: list[str]) -> Any:
        # A proving run is PRICED from the Massive history feed; the connector's
        # only role here is the tradability gate's "is this symbol quotable and
        # active" check (src/equity_symbols.py _quote_is_active: a non-inactive
        # state + a positive price). Return an active, positively-priced
        # placeholder per symbol so a headless proving run (no live connector) is
        # not falsely marked untradable. This value NEVER prices a fill -- the
        # quote_source (Massive) does, and the liquidity gate reads real Massive
        # volume separately.
        return {"quotes": [{"symbol": s, "state": "active", "price": "1.00"} for s in symbols]}

    def review_equity_order(self, **kwargs: Any) -> Any:
        raise RuntimeError("PaperProvingConnector: a paper proving run reviews no real order")

    def place_equity_order(self, **kwargs: Any) -> Any:
        raise RuntimeError("PaperProvingConnector: a paper proving run places no real order")

    def cancel_equity_order(self, order_id: str, account_number: str | None = None) -> Any:
        raise RuntimeError("PaperProvingConnector: a paper proving run cancels no real order")


def build_paper_proving_connector(root: Path, cash: str = "10000.00") -> PaperProvingConnector:
    """Build the headless paper-proving connector from the configured agent-account identity."""
    from src.robinhood_equity_client import _load_expected_account

    return PaperProvingConnector(_load_expected_account(root), cash=cash)


def run_equity_proving_run(
    connector: EquityConnector,
    root: Path,
    iterations: int | None = None,
    hours: float | None = None,
    poll_interval_seconds: int = 0,
    sleep=time.sleep,
    indicator_provider: IndicatorProvider | None = None,
    client: MassiveClient | None = None,
    lookback_days: int = MAX_LOOKBACK_DAYS,
    sentiment_provider: SentimentProvider | None = None,
    liquidity_provider: LiquidityProvider | None = None,
    regime_provider: RegimeProvider | None = None,
) -> dict[str, Any]:
    """A bounded paper proving run priced on REAL Massive historical bars.

    This is the entry point an agent-hosted proving run should call. It is the
    ordinary bounded paper loop with the price series swapped for real history
    -- the connector is still held (it is what a live order would go through),
    but no order is placed and no connector quote is used for pricing, so the
    run is reproducible against a market that actually happened.
    """
    # ONE shared, throttled client feeds the quote source AND every provider the
    # loop builds, so the whole proving run stays inside the free tier.
    shared_massive = client or MassiveClient(min_interval=MIN_REQUEST_INTERVAL_SECONDS)
    quote_source = build_massive_quote_source(root, client=shared_massive, lookback_days=lookback_days)
    summary = run_equity_paper_loop(
        connector,
        root,
        iterations=iterations,
        hours=hours,
        poll_interval_seconds=poll_interval_seconds,
        sleep=sleep,
        indicator_provider=indicator_provider,
        quote_source=quote_source,
        sentiment_provider=sentiment_provider,
        liquidity_provider=liquidity_provider,
        regime_provider=regime_provider,
        massive_client=shared_massive,
    )
    return {**summary, "quote_source": MASSIVE_QUOTE_SOURCE, "provenance": quote_source.provenance()}


def equity_backtest_series(
    root: Path,
    client: MassiveClient | None = None,
    symbols: list[str] | None = None,
    lookback_days: int = MAX_LOOKBACK_DAYS,
) -> tuple[dict[str, list[float]], dict[str, Any]]:
    """Real close series per configured symbol, with the provenance that names
    where they came from -- the whole-window view a backtest walks, as opposed
    to the one-bar-per-cycle replay a proving run reads."""
    feed = build_massive_quote_source(root, client=client, symbols=symbols, lookback_days=lookback_days)
    return feed.backtest_series(), feed.provenance()


def reconcile_equity_paper(root: Path, epsilon: float = 0.000001) -> dict[str, Any]:
    """Read-only-safe reconciliation of the equities paper ledger -- mirrors
    reconcile_paper() for the crypto lane, over data/equity_paper_trades.db."""
    logger = SQLiteLogger(root / "data" / "trading_agent.db")
    broker = PaperBroker(root / "data" / "equity_paper_trades.db")
    result = broker.reconcile_positions(epsilon=epsilon)
    logger.log_decision(None, "equity_paper_reconcile", "equities paper positions reconciled", {**result, "venue": VENUE})
    return result
