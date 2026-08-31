"""End-to-end proving-run coverage for the OPTIONS paper runtime.

Exercises run_option_paper_loop / run_option_proving_run / reconcile_option_paper
the way an agent-hosted options proving run does: a bounded loop over a MOCKED
connector (no real Robinhood option tool is reachable in a test), consuming
ranked plays, filling DEFINED-RISK single-leg longs into a fresh paper ledger,
recording the basis it priced on, and reconciling clean.

Every property is written to FAIL if the guard it exercises is reverted:
  * an unbounded loop is refused;
  * the options kill switch is its own file, and halts the loop;
  * a fill is a DEFINED-RISK long -- an undefined-risk leg never books;
  * a proving run prices on a REAL basis, records it, and refuses to fall back
    when no real underlying bars exist;
  * a run counts only when priced on a real basis, filled >=1, reconciled clean.

The connector is MOCKED throughout; no live Robinhood or Massive call is made.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
import yaml

from src.equity_intelligence.massive_client import Bar
from src.equity_intelligence.massive_history import MassiveHistoryUnavailable
from src.logger import SQLiteLogger
from src.option_risk_gates import OptionOrderProposal
from src.option_strategy import OptionOrderCandidate
from src.option_runtime import (
    BASIS_CONNECTOR_QUOTE,
    BASIS_EXPECTED_MOVE,
    ConnectorOptionQuotePlaySource,
    ConnectorQuoteTarget,
    OptionPlay,
    PaperProvingOptionConnector,
    _expected_positions_from_audit,
    _fill_candidate,
    _open_premium_at_risk,
    _provenance_supports_basis,
    build_option_paper_proving_connector,
    option_kill_switch,
    option_paper_proving_runs,
    load_option_settings,
    reconcile_option_paper,
    run_option_paper_loop,
    run_option_proving_run,
)
from src.paper_broker import PaperBroker
from src.robinhood_option_client import DefinedRiskViolationError, RobinhoodOptionClient

AGENT_ACCOUNT = {"account_number": "RH-OPT-AGENTIC-2092", "nickname": "Agentic", "agentic_allowed": True}
SYMBOL = "AAPL"


def uptrend_closes(count: int = 40) -> list[float]:
    return [100 + i * 0.3 for i in range(count)]


class FakeOptionConnector:
    """Records order-path calls instead of reaching the real options connector.
    A proving run must never touch place/review/cancel -- these lists prove it."""

    def __init__(self) -> None:
        self.place_calls: list[dict] = []
        self.review_calls: list[dict] = []
        self.cancel_calls: list[dict] = []

    def get_accounts(self):
        return {"accounts": [AGENT_ACCOUNT]}

    def get_option_positions(self, account_number=None):
        return {"positions": []}

    def get_option_level_upgrade_info(self, **kwargs):
        return {"option_level": "level_3"}

    def get_option_chains(self, **kwargs):
        return {"chains": []}

    def get_option_quotes(self, **kwargs):
        return {"quotes": [{"mark_price": "2.50"}]}

    def review_option_order(self, **kwargs):
        self.review_calls.append(kwargs)
        return {"reviewed": True}

    def place_option_order(self, **kwargs):
        self.place_calls.append(kwargs)
        return {"order_id": "opt-1", "status": "accepted"}

    def cancel_option_order(self, order_id, account_number=None):
        self.cancel_calls.append({"order_id": order_id})
        return {"status": "cancel_requested"}


class FakeMassiveClient:
    """Stands in for MassiveClient's historical-bars read; no real API is hit."""

    DAY_MS = 86_400_000

    def __init__(self, closes_by_symbol: dict[str, list[float]]) -> None:
        self.closes_by_symbol = closes_by_symbol
        self.calls: list[dict] = []

    def get_daily_bars(self, ticker, from_date, to_date, adjusted=True):
        self.calls.append({"ticker": ticker, "from_date": from_date, "to_date": to_date})
        return [
            Bar(timestamp_ms=1_700_000_000_000 + i * self.DAY_MS, open=c, high=c, low=c, close=c, volume=1_000_000.0)
            for i, c in enumerate(self.closes_by_symbol.get(ticker, []))
        ]


class FixedPlaySource:
    """A basis that hands the loop one priced long-call play per cycle, on a fresh
    contract each cycle. Names itself with a REAL basis so the run can count."""

    basis_name = BASIS_CONNECTOR_QUOTE

    def __init__(self, premium: float = 2.0) -> None:
        self.premium = premium
        self._cycle = 0

    def provenance(self):
        return {"basis": self.basis_name, "vendor": "test"}

    def describe(self):
        return f"priced on {self.basis_name} (test)"

    def plays(self, symbols, today: date):
        self._cycle += 1
        expiry = "2099-01-15"
        strike = 100.0 + self._cycle
        return [
            OptionPlay(
                symbol=SYMBOL,
                direction="call",
                reference_close=strike,
                entry=strike,
                ceiling=strike * 1.05,
                floor=strike * 0.97,
                conviction=70.0,
                rank_score=1.0,
                strike=strike,
                expiry_date=expiry,
                contract_ticker=f"{SYMBOL}-C-{self._cycle}",
                premium=self.premium,
            )
        ]


def write_config(root: Path, universe: list[str] | None = None) -> None:
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir(parents=True, exist_ok=True)
    rules = {
        "trading": {"enabled": True, "mode": "paper"},
        "risk": {"max_trade_amount_usd": 100.0},
        "kill_switch": {"stop_file": "STOP_TRADING", "env_var": "TRADING_ENABLED"},
        "options": {
            "risk": {
                "max_debit_premium_per_trade_usd": 500.0,
                "max_total_premium_at_risk_usd": 100000.0,
                "contract_multiplier": 100,
                "min_days_to_expiry": 2,
                "allow_zero_dte": False,
                "max_contracts_per_order": 5,
                "strategy_min_option_level": {"single_leg_long": 2, "defined_risk_spread": 3},
            },
            "strategy": {"min_conviction": 50.0, "min_rank_score": 0.0, "contracts_per_order": 1},
            "kill_switch": {"stop_file": "STOP_TRADING_OPTIONS", "env_var": "TRADING_ENABLED"},
        },
        "equities": {
            "expected_account": {"nickname": "Agentic", "number_suffix": "2092"},
            "universe": universe if universe is not None else [SYMBOL],
        },
    }
    (root / "config" / "trading_rules.yaml").write_text(yaml.safe_dump(rules, sort_keys=False), encoding="utf-8")


def decisions(root: Path) -> list[dict]:
    return SQLiteLogger(root / "data" / "trading_agent.db").recent_audit_rows(limit=2000)["decisions"]


# --- the bounded loop, end to end --------------------------------------------


def test_bounded_paper_loop_fills_defined_risk_and_reconciles_clean(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "true")
    write_config(tmp_path)
    connector = FakeOptionConnector()

    summary = run_option_paper_loop(
        connector, tmp_path, play_source=FixedPlaySource(), iterations=5, poll_interval_seconds=0, sleep=lambda _s: None
    )

    assert summary["iterations_completed"] == 5
    assert summary["fills"] >= 1
    assert summary["quote_basis"] == BASIS_CONNECTOR_QUOTE
    # Paper only -- the connector's order path is never touched.
    assert connector.place_calls == [] and connector.review_calls == [] and connector.cancel_calls == []

    reconcile = reconcile_option_paper(tmp_path)
    assert reconcile["errors"] == []
    assert reconcile["cash"] > 0

    ledger = PaperBroker(tmp_path / "data" / "option_paper_trades.db").get_portfolio()
    assert sum(p.quantity for p in ledger.positions.values()) > 0

    actions = {row["action"] for row in decisions(tmp_path)}
    assert "option_paper_order_filled" in actions
    assert "option_paper_loop_completed" in actions
    assert "option_quote_basis" in actions
    filled = next(row for row in decisions(tmp_path) if row["action"] == "option_paper_order_filled")
    assert filled["reason"], "a filled option order must carry a non-empty rationale"
    assert json.loads(filled["details"])["contract"], "the fill records the contract it booked"


def test_loop_requires_an_explicit_bound(tmp_path: Path) -> None:
    write_config(tmp_path)
    with pytest.raises(ValueError) as exc:
        run_option_paper_loop(FakeOptionConnector(), tmp_path, play_source=FixedPlaySource())
    assert "bounded" in str(exc.value)


def test_options_kill_switch_is_its_own_file_not_the_crypto_or_equities_stop(monkeypatch, tmp_path: Path) -> None:
    """The crypto STOP_TRADING must neither block nor be touched by an options
    paper run -- the two lanes' kill switches are separate files by construction."""
    monkeypatch.setenv("TRADING_ENABLED", "true")
    write_config(tmp_path)
    (tmp_path / "STOP_TRADING").write_text("crypto disarmed", encoding="utf-8")
    (tmp_path / "STOP_TRADING_EQUITIES").write_text("equities disarmed", encoding="utf-8")

    summary = run_option_paper_loop(
        FakeOptionConnector(), tmp_path, play_source=FixedPlaySource(), iterations=3, poll_interval_seconds=0, sleep=lambda _s: None
    )

    assert summary["iterations_completed"] == 3
    assert summary["fills"] >= 1
    assert (tmp_path / "STOP_TRADING").read_text(encoding="utf-8") == "crypto disarmed"


def test_options_stop_file_halts_the_loop_immediately(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "true")
    write_config(tmp_path)
    rules = load_option_settings(tmp_path)
    kill = option_kill_switch(rules, tmp_path)
    Path(kill.stop_file).write_text("stop", encoding="utf-8")

    summary = run_option_paper_loop(
        FakeOptionConnector(), tmp_path, play_source=FixedPlaySource(), iterations=5, poll_interval_seconds=0, sleep=lambda _s: None
    )

    assert summary["iterations_completed"] == 0
    assert summary["halted"] is True
    last = SQLiteLogger(tmp_path / "data" / "trading_agent.db").get_last_decision()
    assert last["action"] == "option_halted"
    assert "STOP_TRADING_OPTIONS" in last["reason"]


def test_trading_enabled_false_halts_the_loop(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "false")
    write_config(tmp_path)

    summary = run_option_paper_loop(
        FakeOptionConnector(), tmp_path, play_source=FixedPlaySource(), iterations=5, poll_interval_seconds=0, sleep=lambda _s: None
    )

    assert summary["iterations_completed"] == 0
    assert summary["halted"] is True


# --- defined-risk-only at the fill -------------------------------------------


def _short_candidate() -> OptionOrderCandidate:
    short_leg = {"side": "sell", "position_effect": "open", "ratio_quantity": 1, "option": "OPT-SHORT"}
    return OptionOrderCandidate(
        symbol="XYZ",
        direction="call",
        order_direction="credit",
        contract_ticker="OPT-SHORT",
        quantity=1,
        limit_price=2.0,
        max_loss_usd=200.0,
        leg=short_leg,
        proposal=OptionOrderProposal(legs=[short_leg], net_premium_per_contract=2.0, quantity=1, days_to_expiry=30),
    )


def test_an_undefined_risk_leg_never_books_a_paper_fill(monkeypatch, tmp_path: Path) -> None:
    """The fill path re-validates the leg through the client's defined-risk build.
    A naked/uncovered short is refused before it can book -- reverting that
    re-validation would let the short fill and this test would fail."""
    monkeypatch.setenv("TRADING_ENABLED", "true")
    write_config(tmp_path)
    connector = FakeOptionConnector()
    client = RobinhoodOptionClient(connector, config_root=tmp_path)
    paper = PaperBroker(tmp_path / "data" / "option_paper_trades.db")
    logger = SQLiteLogger(tmp_path / "data" / "trading_agent.db")

    with pytest.raises(DefinedRiskViolationError):
        _fill_candidate(client, paper, logger, _short_candidate(), "should never book", notional=200.0, basis_name=BASIS_CONNECTOR_QUOTE)

    assert paper.get_portfolio().positions == {}, "an undefined-risk leg must not book a paper position"


# --- the proving run, priced on a real basis ---------------------------------


def test_a_proving_run_prices_on_real_underlying_bars_and_records_the_basis(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "true")
    write_config(tmp_path)
    connector = build_option_paper_proving_connector(tmp_path)
    client = FakeMassiveClient({SYMBOL: uptrend_closes()})

    summary = run_option_proving_run(
        connector, tmp_path, iterations=10, poll_interval_seconds=0, sleep=lambda _s: None, client=client
    )

    assert summary["iterations_completed"] == 10
    assert summary["quote_basis"] == BASIS_EXPECTED_MOVE
    assert summary["fills"] >= 1
    assert summary["counts"] is True
    assert summary["reconcile"]["errors"] == []
    # The underlying bars were pulled from the real historical endpoint, once.
    assert [call["ticker"] for call in client.calls] == [SYMBOL]
    assert summary["provenance"]["underlying_source"]["vendor"].startswith("Massive")

    completed = [row for row in decisions(tmp_path) if row["action"] == "option_paper_loop_completed"]
    assert completed
    details = json.loads(completed[-1]["details"])
    assert details["quote_basis"] == BASIS_EXPECTED_MOVE
    assert details["basis_provenance"]["basis"] == BASIS_EXPECTED_MOVE

    # And the run counts by the audit-reading counting logic too.
    runs = option_paper_proving_runs(tmp_path)
    assert runs and runs[-1]["clean"] is True


def test_a_proving_run_refuses_to_fall_back_when_no_real_bars_exist(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "true")
    write_config(tmp_path)
    connector = build_option_paper_proving_connector(tmp_path)

    with pytest.raises(MassiveHistoryUnavailable):
        run_option_proving_run(
            connector, tmp_path, iterations=5, poll_interval_seconds=0, sleep=lambda _s: None,
            client=FakeMassiveClient({SYMBOL: []}),
        )

    completed = [row for row in decisions(tmp_path) if row["action"] == "option_paper_loop_completed"]
    assert completed == [], "a run that could not read real bars must not record a completed proving run"


def test_a_fresh_ledger_each_run_makes_runs_independent(monkeypatch, tmp_path: Path) -> None:
    """Two proving runs back to back: the second archives-and-resets the ledger,
    so it trades from starting cash on its own rather than inheriting the first
    run's contracts. Both count."""
    monkeypatch.setenv("TRADING_ENABLED", "true")
    write_config(tmp_path)
    connector = build_option_paper_proving_connector(tmp_path)

    first = run_option_proving_run(
        connector, tmp_path, iterations=6, poll_interval_seconds=0, sleep=lambda _s: None,
        client=FakeMassiveClient({SYMBOL: uptrend_closes()}),
    )
    second = run_option_proving_run(
        connector, tmp_path, iterations=6, poll_interval_seconds=0, sleep=lambda _s: None,
        client=FakeMassiveClient({SYMBOL: uptrend_closes()}),
    )

    assert first["counts"] is True and second["counts"] is True
    clean = [run for run in option_paper_proving_runs(tmp_path) if run["clean"]]
    assert len(clean) >= 2, "two independent, clean runs on a real basis must both count"

    # The fresh-ledger reset is load-bearing: the live ledger must hold ONLY the
    # second run's fills (the first run's were archived, not carried forward).
    # Reverting the reset leaves both runs' fills in the live ledger and fails this.
    import sqlite3

    db = tmp_path / "data" / "option_paper_trades.db"
    with sqlite3.connect(db) as conn:
        live_rows = conn.execute("SELECT COUNT(*) FROM paper_trades WHERE status='filled'").fetchone()[0]
        archived_rows = conn.execute("SELECT COUNT(*) FROM paper_trades_archive").fetchone()[0]
    assert live_rows == second["fills"], "the live ledger must hold only this run's fills"
    assert archived_rows >= first["fills"], "the prior run's fills must be archived, not carried forward"


# --- the counting logic is falsifiable ---------------------------------------


def test_a_run_priced_on_a_made_up_basis_is_not_counted(monkeypatch, tmp_path: Path) -> None:
    """A run recorded with a basis that is not one of the REAL bases does not
    count -- this is what makes the recorded basis load-bearing rather than
    decorative."""
    monkeypatch.setenv("TRADING_ENABLED", "true")
    write_config(tmp_path)

    class SyntheticSource(FixedPlaySource):
        basis_name = "made_up_series"

    run_option_paper_loop(
        FakeOptionConnector(), tmp_path, play_source=SyntheticSource(), iterations=4, poll_interval_seconds=0, sleep=lambda _s: None
    )
    reconcile_option_paper(tmp_path)

    runs = option_paper_proving_runs(tmp_path)
    assert runs and runs[-1]["fills"] >= 1
    assert runs[-1]["basis_is_real"] is False
    assert runs[-1]["clean"] is False, "a made-up basis must not produce a clean, counted run"


def test_a_zero_fill_run_is_not_counted(monkeypatch, tmp_path: Path) -> None:
    """A run that filled nothing proves the loop can idle, not that the lane can
    trade. It must not count even priced on a real basis and reconciled clean."""
    monkeypatch.setenv("TRADING_ENABLED", "true")
    write_config(tmp_path)

    class EmptySource(FixedPlaySource):
        def plays(self, symbols, today):
            return []  # never offers a play, so nothing ever fills

    run_option_paper_loop(
        FakeOptionConnector(), tmp_path, play_source=EmptySource(), iterations=4, poll_interval_seconds=0, sleep=lambda _s: None
    )
    reconcile_option_paper(tmp_path)

    runs = option_paper_proving_runs(tmp_path)
    assert runs and runs[-1]["fills"] == 0
    assert runs[-1]["clean"] is False


def test_the_headless_proving_connector_refuses_every_order_path(tmp_path: Path) -> None:
    """The paper proving connector is held for shape, not to trade: every order
    path raises, so a proving run cannot place, review or cancel a real order
    even by mistake."""
    write_config(tmp_path)
    connector = PaperProvingOptionConnector({"nickname": "Agentic", "number_suffix": "2092"})
    assert connector.get_accounts()["accounts"][0]["agentic_allowed"] is True
    for call in (
        lambda: connector.place_option_order(legs=[]),
        lambda: connector.review_option_order(legs=[]),
        lambda: connector.cancel_option_order("x"),
    ):
        with pytest.raises(RuntimeError):
            call()


# --- provenance must SUBSTANTIATE the declared basis (proving honesty) ---------


def _tighten_total_at_risk_cap(root: Path, cap_usd: float) -> None:
    path = root / "config" / "trading_rules.yaml"
    rules = yaml.safe_load(path.read_text(encoding="utf-8"))
    rules["options"]["risk"]["max_total_premium_at_risk_usd"] = cap_usd
    path.write_text(yaml.safe_dump(rules, sort_keys=False), encoding="utf-8")


def test_provenance_gate_accepts_a_real_massive_window_and_a_real_connector_quote() -> None:
    """The two real bases are accepted when their provenance backs them."""
    assert _provenance_supports_basis(
        BASIS_EXPECTED_MOVE,
        {"basis": BASIS_EXPECTED_MOVE, "underlying_source": {"quote_source": "massive", "total_bars": 40, "from_date": "2024-01-01", "to_date": "2024-03-01"}},
    )
    assert _provenance_supports_basis(
        BASIS_CONNECTOR_QUOTE,
        {"basis": BASIS_CONNECTOR_QUOTE, "recorded_quotes": [{"contract": "AAPL-C-1", "premium": 2.5}]},
    )
    # And it REJECTS a basis whose provenance does not substantiate it.
    assert not _provenance_supports_basis(BASIS_EXPECTED_MOVE, {"underlying_source": {"quote_source": "fabricated", "total_bars": 0}})
    assert not _provenance_supports_basis(BASIS_CONNECTOR_QUOTE, {"recorded_quotes": []})
    assert not _provenance_supports_basis(BASIS_EXPECTED_MOVE, None)


def test_an_expected_move_run_with_unsubstantiated_provenance_is_not_counted(monkeypatch, tmp_path: Path) -> None:
    """MUTATION TEST. A run that DECLARES the real expected-move basis but records
    provenance that does not back it (no massive underlying window) must not
    count. Drop the provenance check in the counting logic and this scans clean --
    a self-declared 'real basis' would be taken verbatim again."""
    monkeypatch.setenv("TRADING_ENABLED", "true")
    write_config(tmp_path)

    class BogusExpectedMoveSource(FixedPlaySource):
        basis_name = BASIS_EXPECTED_MOVE

        def provenance(self):
            # Names the real basis, but the underlying series is fabricated.
            return {"basis": BASIS_EXPECTED_MOVE, "underlying_source": {"quote_source": "fabricated", "total_bars": 0}}

    run_option_paper_loop(
        FakeOptionConnector(), tmp_path, play_source=BogusExpectedMoveSource(), iterations=4, poll_interval_seconds=0, sleep=lambda _s: None
    )
    reconcile_option_paper(tmp_path)

    runs = option_paper_proving_runs(tmp_path)
    assert runs and runs[-1]["fills"] >= 1
    assert runs[-1]["basis_is_real"] is True
    assert runs[-1]["basis_substantiated"] is False
    assert runs[-1]["clean"] is False, "a basis the provenance does not substantiate must not count"


def test_a_connector_quote_run_records_its_real_quotes_and_counts(monkeypatch, tmp_path: Path) -> None:
    """The connector-quote basis records the REAL quotes it read, and a run priced
    on it counts when those quotes are present and it reconciles clean."""
    monkeypatch.setenv("TRADING_ENABLED", "true")
    write_config(tmp_path)
    connector = FakeOptionConnector()  # get_option_quotes returns a real mark of 2.50
    client = RobinhoodOptionClient(connector, config_root=tmp_path)
    source = ConnectorOptionQuotePlaySource(
        client,
        [ConnectorQuoteTarget(symbol=SYMBOL, contract_ticker="AAPL-C-1", strike=100.0, expiry_date="2099-01-15", reference_close=100.0)],
    )

    run_option_paper_loop(
        connector, tmp_path, play_source=source, iterations=3, poll_interval_seconds=0, sleep=lambda _s: None
    )
    reconcile_option_paper(tmp_path)

    assert source.provenance()["recorded_quotes"], "the basis must record the real quotes it read"
    runs = option_paper_proving_runs(tmp_path)
    assert runs and runs[-1]["quote_basis"] == BASIS_CONNECTOR_QUOTE
    assert runs[-1]["basis_substantiated"] is True
    assert runs[-1]["clean"] is True


def test_a_connector_quote_run_with_no_recorded_quote_is_not_counted(monkeypatch, tmp_path: Path) -> None:
    """MUTATION TEST. FixedPlaySource declares the connector-quote basis but its
    provenance records no real quote. The run fills and reconciles clean, yet must
    not count -- revert the provenance check and it counts, which is the exact
    self-declared-basis dishonesty this slice closes."""
    monkeypatch.setenv("TRADING_ENABLED", "true")
    write_config(tmp_path)

    run_option_paper_loop(
        FakeOptionConnector(), tmp_path, play_source=FixedPlaySource(), iterations=3, poll_interval_seconds=0, sleep=lambda _s: None
    )
    reconcile_option_paper(tmp_path)

    runs = option_paper_proving_runs(tmp_path)
    assert runs and runs[-1]["quote_basis"] == BASIS_CONNECTOR_QUOTE
    assert runs[-1]["basis_is_real"] is True
    assert runs[-1]["basis_substantiated"] is False
    assert runs[-1]["clean"] is False


# --- the at-risk cap must carry OPEN exposure across cycles --------------------


def test_open_premium_at_risk_is_read_from_the_ledger_and_capped_across_cycles(monkeypatch, tmp_path: Path) -> None:
    """MUTATION TEST. With the total-at-risk cap set so exactly ONE 200-at-risk
    fill fits, only the first cycle may fill: the second cycle must see the first
    cycle's open premium-at-risk (200) and be blocked (200 open + 200 new > 250).
    Leave open_premium_at_risk at 0.0 each cycle and every cycle fills again."""
    monkeypatch.setenv("TRADING_ENABLED", "true")
    write_config(tmp_path)
    _tighten_total_at_risk_cap(tmp_path, 250.0)  # one 200 fill fits; a second does not

    summary = run_option_paper_loop(
        FakeOptionConnector(), tmp_path, play_source=FixedPlaySource(premium=2.0), iterations=4, poll_interval_seconds=0, sleep=lambda _s: None
    )

    assert summary["fills"] == 1, "the open at-risk from cycle 1 must block later cycles"
    blocked = [
        row for row in decisions(tmp_path)
        if row["action"] == "option_play_skipped" and "at risk" in (row["reason"] or "")
    ]
    assert blocked, "the later cycles must be blocked by the total-at-risk gate, not silently dropped"


def test_open_premium_at_risk_helper_sums_open_long_positions(tmp_path: Path) -> None:
    """The helper reads open premium-at-risk straight off the ledger: contracts x
    average premium x the 100x multiplier."""
    write_config(tmp_path)
    paper = PaperBroker(tmp_path / "data" / "option_paper_trades.db")
    assert _open_premium_at_risk(paper, 100) == 0.0
    paper.place_order({"symbol": "AAPL-C-1", "side": "buy", "quantity": 2.0, "limit_price": 3.0, "notional": 600.0, "strategy_signal": "option_single_leg_long"})
    assert _open_premium_at_risk(paper, 100) == 600.0  # 2 contracts x 3.00 x 100


# --- reconcile has an INDEPENDENT expected-position source ---------------------


def test_reconcile_flags_a_ledger_that_disagrees_with_the_audit_trail(monkeypatch, tmp_path: Path) -> None:
    """MUTATION TEST. A position booked into the paper ledger with NO matching
    ACTION_FILLED audit row is a ledger that disagrees with the independent record
    of what filled. Reconcile must report an expected_position_mismatch. Revert to
    reconciling the ledger only against a recomputation of itself and the phantom
    position passes clean."""
    monkeypatch.setenv("TRADING_ENABLED", "true")
    write_config(tmp_path)
    paper = PaperBroker(tmp_path / "data" / "option_paper_trades.db")
    # Book a position the audit trail never recorded a fill for.
    paper.place_order({"symbol": "AAPL-C-PHANTOM", "side": "buy", "quantity": 1.0, "limit_price": 2.0, "notional": 200.0, "strategy_signal": "option_single_leg_long"})

    result = reconcile_option_paper(tmp_path)

    issues = {error["issue"] for error in result["errors"]}
    assert "expected_position_mismatch" in issues, "a ledger the audit trail does not back must not reconcile clean"


def test_expected_positions_scope_to_fills_after_the_last_reconcile(tmp_path: Path) -> None:
    """MUTATION TEST for the scoping. The reconstruction counts only ACTION_FILLED
    rows AFTER the most recent reconcile, so a prior run's (archived-ledger) fills
    do not bleed into the current reconcile. Drop the id-boundary guard and the
    earlier run's fill is counted too."""
    write_config(tmp_path)
    db = tmp_path / "data" / "trading_agent.db"
    logger = SQLiteLogger(db)
    logger.log_decision("AAPL", "option_paper_order_filled", "run 1 fill", {"contract": "AAPL-C-1", "quantity": 1})
    logger.log_decision(None, "option_paper_reconcile", "run 1 reconciled", {"errors": []})
    logger.log_decision("AAPL", "option_paper_order_filled", "run 2 fill", {"contract": "AAPL-C-2", "quantity": 2})

    assert _expected_positions_from_audit(db) == {"AAPL-C-2": 2.0}


def test_two_proving_runs_back_to_back_both_reconcile_clean(monkeypatch, tmp_path: Path) -> None:
    """The scoping is load-bearing end to end: because the second run archives and
    resets the ledger but the audit trail keeps every fill, an unscoped
    reconstruction would double-count the shared contracts and fail the second
    reconcile. Both reconciles being clean proves the boundary holds."""
    monkeypatch.setenv("TRADING_ENABLED", "true")
    write_config(tmp_path)
    connector = build_option_paper_proving_connector(tmp_path)

    first = run_option_proving_run(
        connector, tmp_path, iterations=6, poll_interval_seconds=0, sleep=lambda _s: None,
        client=FakeMassiveClient({SYMBOL: uptrend_closes()}),
    )
    second = run_option_proving_run(
        connector, tmp_path, iterations=6, poll_interval_seconds=0, sleep=lambda _s: None,
        client=FakeMassiveClient({SYMBOL: uptrend_closes()}),
    )

    assert first["reconcile"]["errors"] == [] and second["reconcile"]["errors"] == []
    assert first["counts"] is True and second["counts"] is True
