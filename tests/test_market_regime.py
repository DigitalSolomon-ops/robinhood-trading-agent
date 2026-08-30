"""The market-regime brake: breadth in, a smaller position (or none) out.

Every test here mocks the Massive client -- nothing reaches a real API, and
nothing in this module can reach an order path at all, which is itself pinned
below. The property that matters most is the one-directional guarantee: a
breadth reading may only make an entry SMALLER or absent, never larger, and it
never touches an exit or excuses a risk gate.
"""

from __future__ import annotations

import ast
from datetime import date
from pathlib import Path

from src.equity_intelligence.market_regime import (
    ALLOW,
    BLOCK_ENTRIES,
    DISABLED,
    NEUTRAL,
    RISK_OFF,
    RISK_ON,
    SCALE_DOWN,
    UNKNOWN,
    MassiveBreadthProvider,
    RegimeSnapshot,
    RegimeVerdict,
    build_regime_provider,
    evaluate_market_regime,
    market_regime_config,
    summarize_breadth,
)
from src.equity_intelligence.massive_client import Bar

SRC = Path(__file__).resolve().parents[1] / "src"


def bar(open_price: float, close: float, ticker: str = "X") -> Bar:
    return Bar(
        timestamp_ms=1_700_000_000_000,
        open=open_price,
        high=max(open_price, close),
        low=min(open_price, close),
        close=close,
        volume=1_000_000.0,
        ticker=ticker,
    )


def breadth_rows(advancers: int, decliners: int, unchanged: int = 0) -> list[Bar]:
    rows = [bar(10.0, 11.0) for _ in range(advancers)]
    rows += [bar(10.0, 9.0) for _ in range(decliners)]
    rows += [bar(10.0, 10.0) for _ in range(unchanged)]
    return rows


def enabled_config(**overrides) -> dict:
    rules = {
        "equities": {
            "market_regime": {
                "enabled": True,
                "min_symbols": 10,
                **overrides,
            }
        }
    }
    return market_regime_config(rules)


# --- counting -----------------------------------------------------------------


def test_breadth_counts_advancers_decliners_and_flat_closes_separately():
    snapshot = summarize_breadth(breadth_rows(60, 30, 10), enabled_config(), "2026-08-28")

    assert (snapshot.advancers, snapshot.decliners, snapshot.unchanged) == (60, 30, 10)
    assert snapshot.counted == 100
    # A flat close is not half a decline: the ratio's denominator is the names
    # that actually moved.
    assert snapshot.moved == 90
    assert snapshot.advance_ratio == 60 / 90


def test_sub_dollar_and_broken_rows_are_excluded_from_the_count():
    rows = breadth_rows(5, 5) + [bar(0.40, 0.60), bar(0.0, 0.0), bar(10.0, 0.0)]

    snapshot = summarize_breadth(rows, enabled_config(min_close_price=1.0), "2026-08-28")

    assert snapshot.counted == 10


def test_the_citation_quotes_the_numbers_rather_than_asserting_a_verdict():
    snapshot = summarize_breadth(breadth_rows(20, 80), enabled_config(), "2026-08-28")

    citation = snapshot.citation()

    assert "2026-08-28" in citation
    assert "20 advancing" in citation
    assert "80 declining" in citation


# --- the verdict --------------------------------------------------------------


def test_a_disabled_config_is_a_no_op_that_leaves_sizing_alone():
    verdict = evaluate_market_regime(summarize_breadth(breadth_rows(1, 99), {}, "d"), market_regime_config({}))

    assert verdict.action == DISABLED
    assert verdict.enabled is False
    assert verdict.size_multiplier == 1.0
    assert verdict.scaled_amount(250.0) == 250.0


def test_a_risk_off_regime_blocks_new_entries_and_cites_the_breadth():
    snapshot = summarize_breadth(breadth_rows(25, 75), enabled_config(), "2026-08-28")

    verdict = evaluate_market_regime(snapshot, enabled_config(on_risk_off="block_entries"))

    assert verdict.regime == RISK_OFF
    assert verdict.action == BLOCK_ENTRIES
    assert verdict.blocks_entries is True
    assert verdict.size_multiplier == 0.0
    assert verdict.scaled_amount(250.0) == 0.0
    rationale = verdict.rationale()
    assert "risk_off" in rationale
    assert "0.25" in rationale, "the rationale must quote the advancing share it acted on"
    assert "25 advancing" in rationale


def test_a_risk_off_regime_can_scale_the_trade_size_down_instead_of_blocking():
    snapshot = summarize_breadth(breadth_rows(25, 75), enabled_config(), "2026-08-28")
    config = enabled_config(on_risk_off="scale_down", risk_off_size_multiplier=0.4)

    verdict = evaluate_market_regime(snapshot, config)

    assert verdict.regime == RISK_OFF
    assert verdict.action == SCALE_DOWN
    assert verdict.blocks_entries is False
    assert verdict.size_multiplier == 0.4
    assert verdict.scaled_amount(250.0) == 100.0
    assert "scaled to x0.40" in verdict.rationale()


def test_a_neutral_market_scales_down_and_a_risk_on_market_does_not():
    config = enabled_config(neutral_size_multiplier=0.75)

    neutral = evaluate_market_regime(summarize_breadth(breadth_rows(48, 52), config, "d"), config)
    risk_on = evaluate_market_regime(summarize_breadth(breadth_rows(70, 30), config, "d"), config)

    assert neutral.regime == NEUTRAL
    assert neutral.action == SCALE_DOWN
    assert neutral.scaled_amount(200.0) == 150.0
    assert risk_on.regime == RISK_ON
    assert risk_on.action == ALLOW
    assert risk_on.scaled_amount(200.0) == 200.0


def test_a_multiplier_above_one_is_clamped_with_a_note_saying_so():
    """The enforcement point for "may only reduce". A config asking for 1.5x
    must not turn a risk brake into a size booster."""
    config = enabled_config(neutral_size_multiplier=1.5)

    verdict = evaluate_market_regime(summarize_breadth(breadth_rows(50, 50), config, "d"), config)

    assert verdict.size_multiplier == 1.0
    assert verdict.scaled_amount(250.0) == 250.0
    assert any("clamped to 1.00" in note for note in verdict.notes)


def test_a_non_numeric_multiplier_degrades_to_no_adjustment():
    config = enabled_config(neutral_size_multiplier="lots")

    verdict = evaluate_market_regime(summarize_breadth(breadth_rows(50, 50), config, "d"), config)

    assert verdict.size_multiplier == 1.0
    assert any("non-numeric" in note for note in verdict.notes)


def test_scaled_amount_is_floored_against_the_base_even_if_a_multiplier_escapes():
    """Reduce-only is enforced twice: at the config clamp and again at the point
    of application. A hand-built verdict with an impossible multiplier still
    cannot size an entry above the configured cap."""
    rogue = RegimeVerdict(RISK_ON, ALLOW, 3.0, (), None)

    assert rogue.scaled_amount(250.0) == 250.0


def test_unreadable_breadth_passes_sizing_through_unless_require_breadth_is_set():
    errored = RegimeSnapshot(error="MassiveAuthError: no api key")

    lenient = evaluate_market_regime(errored, enabled_config())
    strict = evaluate_market_regime(errored, enabled_config(require_breadth=True))

    assert lenient.regime == UNKNOWN
    assert lenient.action == ALLOW
    assert lenient.size_multiplier == 1.0
    assert strict.action == BLOCK_ENTRIES
    assert "required but unavailable" in strict.rationale()


def test_a_partial_session_is_not_treated_as_a_market_wide_reading():
    thin = summarize_breadth(breadth_rows(3, 1), enabled_config(), "2026-08-28")

    verdict = evaluate_market_regime(thin, enabled_config(min_symbols=500))

    assert verdict.regime == UNKNOWN
    assert verdict.action == ALLOW
    assert "below min_symbols 500" in verdict.rationale()


# --- the provider -------------------------------------------------------------


class FakeMassiveBreadthClient:
    """Serves canned grouped-daily rows per session date and records the calls."""

    def __init__(self, sessions: dict[str, list[Bar]], raises: Exception | None = None) -> None:
        self.sessions = sessions
        self.raises = raises
        self.calls: list[str] = []

    def get_grouped_daily(self, date_str: str, **_kwargs) -> list[Bar]:
        self.calls.append(date_str)
        if self.raises is not None:
            raise self.raises
        return self.sessions.get(date_str, [])


def test_the_provider_reads_the_most_recent_completed_session_and_caches_it():
    client = FakeMassiveBreadthClient({"2026-08-27": breadth_rows(70, 30)})
    provider = MassiveBreadthProvider(client, enabled_config(), today=date(2026, 8, 28))

    first = provider.snapshot()
    second = provider.snapshot()

    assert first.session == "2026-08-27"
    assert first.advancers == 70
    assert second is first, "a completed session cannot change; it must not be re-read"
    assert client.calls == ["2026-08-27"]


def test_the_provider_walks_back_past_weekends_and_empty_holiday_sessions():
    # 2026-08-31 is a Monday; the prior Friday returned nothing (a holiday), so
    # the reading comes from the Thursday before it. Saturday and Sunday are
    # never even requested.
    client = FakeMassiveBreadthClient({"2026-08-27": breadth_rows(60, 40), "2026-08-28": []})
    provider = MassiveBreadthProvider(client, enabled_config(), today=date(2026, 8, 31))

    snapshot = provider.snapshot()

    assert snapshot.session == "2026-08-27"
    assert client.calls == ["2026-08-28", "2026-08-27"]


def test_a_vendor_outage_degrades_to_an_errored_snapshot_and_never_raises():
    client = FakeMassiveBreadthClient({}, raises=RuntimeError("429 rate limited"))
    provider = MassiveBreadthProvider(client, enabled_config(), today=date(2026, 8, 28))

    snapshot = provider.snapshot()

    assert snapshot.error is not None
    assert "429" in snapshot.error
    assert evaluate_market_regime(snapshot, enabled_config()).action == ALLOW


def test_no_session_inside_the_lookback_is_an_error_not_a_guess():
    client = FakeMassiveBreadthClient({})
    provider = MassiveBreadthProvider(client, enabled_config(lookback_days=3), today=date(2026, 8, 28))

    snapshot = provider.snapshot()

    assert snapshot.error is not None
    assert "no completed session" in snapshot.error
    assert snapshot.advancers == 0


def test_no_provider_is_built_unless_config_enables_the_brake():
    assert build_regime_provider({}) is None
    assert build_regime_provider({"equities": {"market_regime": {"enabled": False}}}) is None
    built = build_regime_provider(
        {"equities": {"market_regime": {"enabled": True}}}, client_factory=lambda: FakeMassiveBreadthClient({})
    )
    assert isinstance(built, MassiveBreadthProvider)


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


def test_the_regime_module_imports_nothing_from_the_execution_layer():
    """A breadth reading changes how large a question the gates are asked. It
    must not be able to answer one."""
    path = SRC / "equity_intelligence" / "market_regime.py"

    assert not (imported_modules(path) & EXECUTION_MODULES)
    source = path.read_text(encoding="utf-8").lower()
    for tool in ("place_equity_order", "review_equity_order", "cancel_equity_order"):
        assert tool not in source
    assert "broker" not in imported_modules(path)
