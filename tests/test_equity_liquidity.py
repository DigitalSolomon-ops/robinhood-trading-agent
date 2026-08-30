"""The liquidity / still-trading half of the equities tradability gate.

Covers the module on its own, and then the thing the audit actually asked for:
that `validate_equity_symbols` -- which was dead code -- now refuses a symbol
that is quoted but not actually trading, and that every refusal writes a
readable rationale. The Massive client is mocked throughout; no test reaches a
real api, and nothing in this module can reach an order path at all.
"""

from __future__ import annotations

import ast
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from src.equity_intelligence.liquidity import (
    LiquiditySnapshot,
    MassiveLiquidityProvider,
    build_liquidity_provider,
    evaluate_liquidity,
    liquidity_config,
    summarize_liquidity,
)
from src.equity_intelligence.massive_client import Bar
from src.equity_symbols import equity_tradability, validate_equity_symbols
from src.robinhood_equity_client import RobinhoodEquityClient

SRC = Path(__file__).resolve().parents[1] / "src"

TODAY = date(2026, 8, 28)
DAY_MS = 86_400_000

AGENT_ACCOUNT = {"account_number": "RH-EQ-AGENTIC-2092", "nickname": "Agentic", "agentic_allowed": True}


def _stamp(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp() * 1000)


def daily_bars(
    sessions: int = 30,
    close: float = 200.0,
    volume: float = 5_000_000.0,
    last_day: date = TODAY - timedelta(days=1),
) -> list[Bar]:
    """`sessions` consecutive daily bars ending on `last_day`, newest last."""
    return [
        Bar(
            timestamp_ms=_stamp(last_day - timedelta(days=sessions - 1 - index)),
            open=close,
            high=close,
            low=close,
            close=close,
            volume=volume,
        )
        for index in range(sessions)
    ]


def enabled_config(**overrides) -> dict:
    return liquidity_config({"equities": {"liquidity": {"enabled": True, **overrides}}})


class FakeMassiveBarsClient:
    def __init__(self, bars_by_symbol: dict[str, list[Bar]], raises: Exception | None = None) -> None:
        self.bars_by_symbol = bars_by_symbol
        self.raises = raises
        self.calls: list[tuple[str, str, str]] = []

    def get_daily_bars(self, ticker: str, from_date: str, to_date: str, adjusted: bool = True) -> list[Bar]:
        self.calls.append((ticker, from_date, to_date))
        if self.raises is not None:
            raise self.raises
        return self.bars_by_symbol.get(ticker, [])


class StubLiquidityProvider:
    """Hands back canned snapshots; records which symbols were asked about."""

    def __init__(self, snapshots: dict[str, LiquiditySnapshot]) -> None:
        self.snapshots = snapshots
        self.asked: list[str] = []

    def snapshot(self, symbol: str) -> LiquiditySnapshot:
        self.asked.append(symbol)
        return self.snapshots.get(symbol, LiquiditySnapshot(symbol=symbol, error="no stub configured"))


class FakeConnector:
    def __init__(self, quotes: dict[str, dict] | None = None) -> None:
        self.quotes = quotes or {}
        self.quote_calls: list[list[str]] = []

    def get_accounts(self):
        return {"accounts": [AGENT_ACCOUNT]}

    def get_equity_quotes(self, symbols):
        self.quote_calls.append(list(symbols))
        return {"quotes": [{"symbol": s, **self.quotes[s]} for s in symbols if s in self.quotes]}

    def get_equity_positions(self, account_number=None):
        return {"positions": []}


class RecordingLogger:
    def __init__(self) -> None:
        self.rows: list[tuple] = []

    def log_decision(self, symbol, action, reason, details=None):
        self.rows.append((symbol, action, reason, details or {}))

    def rationales_for(self, action: str) -> dict[str, str]:
        return {row[0]: row[2] for row in self.rows if row[1] == action}


# --- summarizing real bars ----------------------------------------------------


def test_summary_averages_volume_and_dollar_volume_over_the_window():
    snapshot = summarize_liquidity("AAPL", daily_bars(sessions=10, close=200.0, volume=1_000_000.0), today=TODAY)

    assert snapshot.bars == 10
    assert snapshot.average_volume == 1_000_000.0
    assert snapshot.average_dollar_volume == 200_000_000.0
    assert snapshot.last_close == 200.0
    assert snapshot.stale_days == 1


def test_a_stale_last_bar_is_measured_from_the_most_recent_real_session():
    bars = daily_bars(sessions=12, last_day=TODAY - timedelta(days=40))

    snapshot = summarize_liquidity("AAPL", bars, today=TODAY)

    assert snapshot.stale_days == 40
    assert snapshot.last_day == (TODAY - timedelta(days=40)).isoformat()


def test_bars_with_no_close_are_not_sessions_the_name_traded_in():
    broken = [Bar(timestamp_ms=_stamp(TODAY), open=0.0, high=0.0, low=0.0, close=0.0, volume=0.0)]

    assert summarize_liquidity("AAPL", broken, today=TODAY).bars == 0
    assert summarize_liquidity("AAPL", [], today=TODAY).bars == 0


# --- the verdict --------------------------------------------------------------


def test_a_disabled_screen_leaves_every_symbol_to_the_connector_quote_check():
    snapshot = summarize_liquidity("AAPL", daily_bars(sessions=1, volume=1.0), today=TODAY)

    verdict = evaluate_liquidity(snapshot, liquidity_config({}))

    assert verdict.tradable is True
    assert "disabled in config" in verdict.rationale()


def test_a_healthy_large_cap_passes_and_the_rationale_quotes_the_numbers():
    snapshot = summarize_liquidity("AAPL", daily_bars(), today=TODAY)

    verdict = evaluate_liquidity(snapshot, enabled_config())

    assert verdict.tradable is True
    assert "actively trading and liquid" in verdict.rationale()
    assert "1,000,000,000" in verdict.rationale(), "the average dollar volume must be quoted, not asserted"


def test_a_name_whose_bars_stopped_is_refused_as_halted_or_delisted():
    snapshot = summarize_liquidity("ZZZQ", daily_bars(last_day=TODAY - timedelta(days=40)), today=TODAY)

    verdict = evaluate_liquidity(snapshot, enabled_config(max_stale_days=5))

    assert verdict.tradable is False
    rationale = verdict.rationale()
    assert "beyond max_stale_days 5" in rationale
    assert "delisted" in rationale


def test_a_thin_name_is_refused_and_the_rationale_names_the_shortfall():
    snapshot = summarize_liquidity("ZZZQ", daily_bars(close=4.0, volume=1_000.0), today=TODAY)

    verdict = evaluate_liquidity(snapshot, enabled_config())

    assert verdict.tradable is False
    rationale = verdict.rationale()
    assert "below min_average_volume" in rationale
    assert "below min_average_dollar_volume" in rationale


def test_too_few_sessions_reads_as_not_actively_trading():
    snapshot = summarize_liquidity("ZZZQ", daily_bars(sessions=3), today=TODAY)

    verdict = evaluate_liquidity(snapshot, enabled_config(min_bars=10))

    assert verdict.tradable is False
    assert "below min_bars 10" in verdict.rationale()


def test_a_sub_threshold_price_is_refused():
    snapshot = summarize_liquidity("ZZZQ", daily_bars(close=1.5, volume=100_000_000.0), today=TODAY)

    verdict = evaluate_liquidity(snapshot, enabled_config(min_close_price=3.0))

    assert verdict.tradable is False
    assert "below min_close_price" in verdict.rationale()


def test_unreadable_bars_pass_through_unless_require_liquidity_is_set():
    errored = LiquiditySnapshot(symbol="AAPL", error="MassiveAuthError: no api key")

    assert evaluate_liquidity(errored, enabled_config()).tradable is True
    strict = evaluate_liquidity(errored, enabled_config(require_liquidity=True))
    assert strict.tradable is False
    assert "required but unavailable" in strict.rationale()


# --- the provider -------------------------------------------------------------


def test_the_provider_reads_one_window_per_symbol_and_caches_it():
    client = FakeMassiveBarsClient({"AAPL": daily_bars()})
    provider = MassiveLiquidityProvider(client, enabled_config(lookback_days=45), today=TODAY)

    first = provider.snapshot("AAPL")
    second = provider.snapshot("AAPL")

    assert second is first
    assert client.calls == [("AAPL", (TODAY - timedelta(days=45)).isoformat(), TODAY.isoformat())]


def test_a_vendor_outage_degrades_to_an_errored_snapshot_and_never_raises():
    provider = MassiveLiquidityProvider(
        FakeMassiveBarsClient({}, raises=RuntimeError("429 rate limited")), enabled_config(), today=TODAY
    )

    snapshot = provider.snapshot("AAPL")

    assert snapshot.error is not None and "429" in snapshot.error
    assert evaluate_liquidity(snapshot, enabled_config()).tradable is True


def test_no_provider_is_built_unless_config_enables_the_screen():
    assert build_liquidity_provider({}) is None
    built = build_liquidity_provider(
        {"equities": {"liquidity": {"enabled": True}}}, client_factory=lambda: FakeMassiveBarsClient({})
    )
    assert isinstance(built, MassiveLiquidityProvider)


# --- the formerly-dead tradability gate ---------------------------------------


def test_a_quoted_but_illiquid_symbol_is_refused_by_the_gate():
    """The whole point of the wiring: MSFT quotes fine and is not halted, so the
    connector-quote half passes it. The liquidity half sees bars that stopped 40
    days ago and refuses it anyway."""
    rules = {
        "equities": {
            "universe": ["AAPL", "MSFT"],
            "liquidity": {"enabled": True, "max_stale_days": 5},
        }
    }
    connector = FakeConnector({"AAPL": {"price": "225.10"}, "MSFT": {"price": "410.00"}})
    provider = StubLiquidityProvider(
        {
            "AAPL": summarize_liquidity("AAPL", daily_bars(), today=TODAY),
            "MSFT": summarize_liquidity("MSFT", daily_bars(last_day=TODAY - timedelta(days=40)), today=TODAY),
        }
    )

    report = validate_equity_symbols(RobinhoodEquityClient(connector), rules, liquidity_provider=provider)

    assert report["available"] == ["AAPL"]
    assert report["unavailable"] == ["MSFT"]
    assert "not tradable" in report["rationales"]["MSFT"]
    assert "max_stale_days" in report["rationales"]["MSFT"]
    assert report["details"]["MSFT"]["liquidity"]["tradable"] is False


def test_a_halted_quote_is_refused_before_the_liquidity_screen_is_even_asked():
    rules = {"equities": {"universe": ["MSFT"], "liquidity": {"enabled": True}}}
    connector = FakeConnector({"MSFT": {"price": "410.00", "state": "halted"}})
    provider = StubLiquidityProvider({})

    report = validate_equity_symbols(RobinhoodEquityClient(connector), rules, liquidity_provider=provider)

    assert report["unavailable"] == ["MSFT"]
    assert "halted" in report["rationales"]["MSFT"]
    assert provider.asked == [], "a halted name needs no liquidity read to be refused"


def test_the_gate_writes_a_rationale_for_every_symbol_it_removes():
    rules = {
        "equities": {
            "universe": ["AAPL", "MSFT", "NVDA"],
            "liquidity": {"enabled": True, "max_stale_days": 5},
        }
    }
    connector = FakeConnector({"AAPL": {"price": "225.10"}, "MSFT": {"price": "410.00", "state": "halted"}})
    provider = StubLiquidityProvider(
        {
            "AAPL": summarize_liquidity("AAPL", daily_bars(), today=TODAY),
            "NVDA": summarize_liquidity("NVDA", daily_bars(), today=TODAY),
        }
    )
    logger = RecordingLogger()

    report = equity_tradability(
        RobinhoodEquityClient(connector), rules, logger=logger, liquidity_provider=provider
    )

    assert report["available"] == ["AAPL"]
    skipped = logger.rationales_for("equity_symbol_unavailable")
    # NVDA is quoted nowhere; MSFT is quoted but halted. Both get their own row,
    # and each row says which half refused it.
    assert set(skipped) == {"MSFT", "NVDA"}
    assert "halted" in skipped["MSFT"]
    assert "no quote available" in skipped["NVDA"]
    summary = logger.rationales_for("equity_symbols_validated")[None]
    assert "1/3" in summary
    assert "MSFT" in summary and "NVDA" in summary


def test_an_empty_quote_read_fails_every_symbol_closed():
    """`quote_rows=[]` means the read happened and returned nothing. Assuming
    tradable on a failed read is what turns a data outage into a trade."""
    rules = {"equities": {"universe": ["AAPL", "MSFT"]}}

    report = validate_equity_symbols(RobinhoodEquityClient(FakeConnector()), rules, quote_rows=[])

    assert report["available"] == []
    assert sorted(report["unavailable"]) == ["AAPL", "MSFT"]


def test_prefetched_quote_rows_cost_the_gate_no_extra_connector_call():
    rules = {"equities": {"universe": ["AAPL"]}}
    connector = FakeConnector({"AAPL": {"price": "225.10"}})

    report = validate_equity_symbols(
        RobinhoodEquityClient(connector), rules, quote_rows=[{"symbol": "AAPL", "price": "225.10"}]
    )

    assert report["available"] == ["AAPL"]
    assert connector.quote_calls == [], "the caller already read the quotes; the gate must reuse them"


# --- the module cannot become an execution path -------------------------------


EXECUTION_MODULES = {"order_manager", "risk_manager", "kill_switch", "live_broker", "paper_broker"}


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[-1] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module.split(".")[-1])
            names.update(alias.name.split(".")[-1] for alias in node.names)
    return names


def test_the_liquidity_module_imports_nothing_from_the_execution_layer():
    path = SRC / "equity_intelligence" / "liquidity.py"

    assert not (imported_modules(path) & EXECUTION_MODULES)
    source = path.read_text(encoding="utf-8").lower()
    for tool in ("place_equity_order", "review_equity_order", "cancel_equity_order"):
        assert tool not in source
