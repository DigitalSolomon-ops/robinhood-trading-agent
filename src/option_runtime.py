"""The OPTIONS lane's bounded, unattended paper-proving runtime.

The direct analog of src/equity_runtime.py's run_equity_proving_run, for the
DEFINED-RISK options lane. Like the equities lane the options lane is
AGENT-HOSTED (the Robinhood OAuth connector is session-bound), so there is no
VM-daemon entry point here: this module exposes plain functions that take an
already-resolved connector, and a headless PaperProvingOptionConnector stands
in for the live one in paper mode.

Every function here is paper-only and PROVES the same three things the equities
proving run does, adapted to options:

  1. FRESH LEDGER each run. run_option_proving_run archives-and-resets the
     options paper ledger (data/option_paper_trades.db) before it starts, so a
     run trades from starting cash on its own rather than inheriting the prior
     run's open contracts -- which is what makes two runs genuinely independent.

  2. A REAL BASIS, recorded. The premiums a run fills at are priced on a real
     basis, and the basis NAMES itself in the completion decision the counting
     logic reads. Two real bases exist: `connector_option_quote` (the live
     Robinhood option quote, the execution basis) and `underlying_expected_move`
     (a premium derived from REAL Massive underlying closes plus an expected-move
     fraction -- the headless proving basis, grounded in a market that actually
     happened). A run that recorded no basis, or a made-up one, does not count.

  3. FILLED and RECONCILED CLEAN. A run counts only if it filled at least one
     DEFINED-RISK order (a single-leg long call/put -- max loss = premium paid)
     AND the reconcile that followed it reported no errors. A zero-fill run
     proves the loop can idle, not that the lane can trade and reconcile.

Nothing here can place a real order. The loop never touches the connector's
order path: it consumes the analysis-only Options Scout's ranked plays (mapped
to defined-risk candidates by src/option_strategy.py, gated by the options
RiskManager in src/option_risk_gates.py), then SIMULATES each fill in the local
paper ledger. Every candidate's leg is re-validated as defined-risk through
RobinhoodOptionClient before it can fill, so an undefined-risk leg can never
reach the ledger. The options kill switch (STOP_TRADING_OPTIONS, its own file)
can halt the loop early.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Protocol, Sequence

from dotenv import load_dotenv

from .equity_intelligence.massive_client import MIN_REQUEST_INTERVAL_SECONDS, MassiveClient
from .equity_intelligence.massive_history import MAX_LOOKBACK_DAYS, QUOTE_SOURCE, MassiveHistoryFeed
from .kill_switch import KillSwitch
from .logger import SQLiteLogger
from .option_risk_gates import OptionRiskConfig, resolve_granted_level
from .option_strategy import OptionOrderCandidate, plan_from_rules
from .paper_broker import PaperBroker
from .robinhood_option_client import (
    DefinedRiskViolationError,
    OptionConnector,
    RobinhoodOptionClient,
    _load_expected_account,
)

VENUE = "robinhood_options"

# The options lane's own paper ledger -- separate from the crypto and equities
# ledgers, so an options proving run never mixes its fills with theirs.
OPTION_PAPER_LEDGER = Path("data") / "option_paper_trades.db"

# The pricing bases a proving run may record. Both are REAL: the first is the
# live Robinhood option quote (the execution basis), the second is a premium
# derived from REAL Massive underlying closes plus an expected-move fraction
# (the headless proving basis). The counting logic accepts only these two.
BASIS_CONNECTOR_QUOTE = "connector_option_quote"
BASIS_EXPECTED_MOVE = "underlying_expected_move"
REAL_OPTION_BASES: tuple[str, ...] = (BASIS_CONNECTOR_QUOTE, BASIS_EXPECTED_MOVE)

# What a run records when it never said what it was priced on. Reported by name
# so the counting logic can say why an unrecorded run does not count.
UNRECORDED_BASIS = "unrecorded"


def _provenance_supports_basis(basis: str, provenance: Any) -> bool:
    """Does the recorded provenance actually SUBSTANTIATE the declared basis?

    The basis name is self-declared by the play source; a run that merely names a
    real basis while recording provenance that does not back it is not evidence
    about a real market. This checks the two real bases structurally:

      - underlying_expected_move: the underlying series must be REAL Massive
        history -- an `underlying_source` whose quote_source is 'massive', with a
        non-empty bar window (total_bars > 0 across a from/to date range);
      - connector_option_quote: at least one REAL option quote must have been
        recorded -- a positive premium actually read from the connector.

    Anything else (a made-up basis, a missing or fabricated provenance) fails.
    """
    if not isinstance(provenance, dict):
        return False
    if basis == BASIS_EXPECTED_MOVE:
        underlying = provenance.get("underlying_source")
        if not isinstance(underlying, dict):
            return False
        if str(underlying.get("quote_source", "")).strip().lower() != QUOTE_SOURCE:
            return False
        try:
            total_bars = int(underlying.get("total_bars") or 0)
        except (TypeError, ValueError):
            return False
        return total_bars > 0 and bool(underlying.get("from_date")) and bool(underlying.get("to_date"))
    if basis == BASIS_CONNECTOR_QUOTE:
        quotes = provenance.get("recorded_quotes")
        if not isinstance(quotes, list) or not quotes:
            return False
        for quote in quotes:
            if not isinstance(quote, dict):
                continue
            try:
                if float(quote.get("premium")) > 0:
                    return True
            except (TypeError, ValueError):
                continue
        return False
    return False

# Audit-log action names, kept distinct so act/fill/completion/reconcile are
# trivially separable by a reader (and by the counting logic below).
ACTION_FILLED = "option_paper_order_filled"
ACTION_COMPLETED = "option_paper_loop_completed"
ACTION_RECONCILE = "option_paper_reconcile"
ACTION_BASIS = "option_quote_basis"
ACTION_HALTED = "option_halted"

# A run's ledger has to have actually MOVED by at least this much (summed
# absolute fill notional in the window) to count -- a run that filled nothing
# proves the loop can idle, not that the lane can trade and reconcile.
_LEDGER_DELTA_EPSILON = 1e-9


# ---------------------------------------------------------------------------
# settings, kill switch, universe
# ---------------------------------------------------------------------------


def _load_yaml(path: Path) -> dict[str, Any]:
    import yaml

    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def load_option_settings(root: Path) -> dict[str, Any]:
    """Load .env (so the kill switch's TRADING_ENABLED read and any MASSIVE_API_KEY
    are seen) and return parsed config/trading_rules.yaml. Mirrors
    equity_runtime.load_equity_settings, minus the separate strategy.yaml the
    options lane does not read."""
    load_dotenv(root / ".env", override=False)
    return _load_yaml(root / "config" / "trading_rules.yaml")


def option_kill_switch(rules: dict[str, Any], root: Path) -> KillSwitch:
    """The options lane's OWN kill switch -- a separate stop file
    (STOP_TRADING_OPTIONS) from the crypto lane's STOP_TRADING and the equities
    lane's STOP_TRADING_EQUITIES, so disarming one lane never touches another.
    Same KillSwitch class, own file; config may override under options.kill_switch."""
    options_kill = (rules.get("options") or {}).get("kill_switch", {})
    stop_file = options_kill.get("stop_file", "STOP_TRADING_OPTIONS")
    stop_path = Path(stop_file)
    if not stop_path.is_absolute():
        stop_path = root / stop_path
    return KillSwitch(stop_file=str(stop_path), env_var=options_kill.get("env_var", "TRADING_ENABLED"))


def option_underlyings(rules: dict[str, Any]) -> list[str]:
    """The underlying symbols a proving run frames option plays on. Reads
    options.universe when present; otherwise falls back to the equities lane's
    universe (the same large-cap liquid names -- sensible option underlyings)
    rather than an empty list, so a proving run always has something to trade."""
    options = rules.get("options") or {}
    universe = options.get("universe")
    if isinstance(universe, list) and universe:
        return [str(symbol).upper() for symbol in universe]
    equities = (rules.get("equities") or {}).get("universe") or []
    return [str(symbol).upper() for symbol in equities]


# ---------------------------------------------------------------------------
# the play source (where a cycle's priced plays come from, and its basis)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OptionPlay:
    """A single ranked play the mapper reads (satisfies option_strategy.ScoutPlay).

    A concrete, inert value object: the same shape the analysis-only Options
    Scout emits, carrying the priced contract this cycle wants to consider. The
    mapper turns it into a defined-risk candidate (or a logged skip); nothing
    here executes.
    """

    symbol: str
    direction: str  # "call" | "put"
    reference_close: float
    entry: float
    ceiling: float
    floor: float
    conviction: float
    rank_score: float
    strike: float | None
    expiry_date: str | None
    contract_ticker: str | None
    premium: float | None


class OptionPlaySource(Protocol):
    """Where a cycle's priced plays come from, and what basis priced them.

    The basis NAME travels with the plays so the run records what it was priced
    on -- exactly the role equity_runtime's QuoteSource plays for the equities
    lane, adapted to option premiums.
    """

    basis_name: str

    def provenance(self) -> dict[str, Any]: ...
    def describe(self) -> str: ...
    def plays(self, symbols: Sequence[str], today: date) -> list[OptionPlay]: ...


def _occ_like_ticker(symbol: str, expiry: str, direction: str, strike: float) -> str:
    """A stable, readable contract id for a proving play. Not a real OCC symbol
    (a proving run does not route it anywhere); enough to key a paper fill and
    read the play in the audit log."""
    ymd = expiry.replace("-", "")
    cp = "C" if direction == "call" else "P"
    return f"{symbol}{ymd}{cp}{int(round(strike * 1000)):08d}"


class ExpectedMovePlaySource:
    """Prices plays from REAL underlying closes plus an expected-move fraction.

    The headless proving basis, grounded in a market that actually happened: the
    reference close per cycle is the next REAL Massive daily bar for the
    underlying (one bar per cycle, exactly the cadence the equities proving run
    walks its series), and the premium is that close times an expected-move
    fraction. It records itself as `underlying_expected_move` and carries the
    Massive provenance of the underlying series, so a reader sees the real vendor,
    endpoint and date range behind the premiums -- not just the word "real".

    It deliberately has NO fallback: the feed raises MassiveHistoryUnavailable
    when a symbol has no real bars, so a proving run with no real underlying
    history stops rather than pricing on a made-up series.
    """

    basis_name = BASIS_EXPECTED_MOVE

    def __init__(
        self,
        feed: MassiveHistoryFeed,
        expected_move_fraction: float = 0.03,
        days_to_expiry: int = 30,
        conviction: float = 70.0,
        rank_score: float = 1.0,
        direction: str = "call",
        max_premium: float | None = None,
    ) -> None:
        if expected_move_fraction <= 0:
            raise ValueError("expected_move_fraction must be positive")
        self.feed = feed
        self.expected_move_fraction = float(expected_move_fraction)
        self.days_to_expiry = int(days_to_expiry)
        self.conviction = float(conviction)
        self.rank_score = float(rank_score)
        self.direction = direction
        # A ceiling on the derived premium so the resulting debit stays under the
        # trade's own debit cap regardless of how highly priced the underlying is
        # -- a high-priced name (a $500 underlying x a 3% move) would otherwise
        # price a debit over the cap and never fill. The premium stays a function
        # of the real close, only bounded by the trade's own risk cap.
        self.max_premium = float(max_premium) if max_premium is not None else None

    def provenance(self) -> dict[str, Any]:
        return {
            "basis": self.basis_name,
            "expected_move_fraction": self.expected_move_fraction,
            "days_to_expiry": self.days_to_expiry,
            "max_premium": self.max_premium,
            "underlying_source": self.feed.provenance(),
            "note": (
                "premium = real underlying close x expected-move fraction (bounded by the trade's debit "
                "cap); a proving basis grounded in real Massive underlying bars, never an execution price"
            ),
        }

    def describe(self) -> str:
        return (
            f"priced on {self.basis_name}: real Massive underlying closes x "
            f"{self.expected_move_fraction:.3f} expected-move fraction, {self.days_to_expiry}d to expiry"
        )

    def plays(self, symbols: Sequence[str], today: date) -> list[OptionPlay]:
        prices = self.feed.get_prices(symbols)
        expiry = (today + timedelta(days=self.days_to_expiry)).isoformat()
        plays: list[OptionPlay] = []
        for symbol in symbols:
            reference = prices.get(str(symbol).upper())
            if reference is None or reference <= 0:
                continue
            premium = round(float(reference) * self.expected_move_fraction, 2)
            if self.max_premium is not None:
                premium = min(premium, round(self.max_premium, 2))
            if premium <= 0:
                continue
            strike = round(float(reference), 2)
            plays.append(
                OptionPlay(
                    symbol=str(symbol).upper(),
                    direction=self.direction,
                    reference_close=float(reference),
                    entry=float(reference),
                    ceiling=round(float(reference) * 1.05, 2),
                    floor=round(float(reference) * 0.97, 2),
                    conviction=self.conviction,
                    rank_score=self.rank_score,
                    strike=strike,
                    expiry_date=expiry,
                    contract_ticker=_occ_like_ticker(str(symbol).upper(), expiry, self.direction, strike),
                    premium=premium,
                )
            )
        return plays


@dataclass(frozen=True)
class ConnectorQuoteTarget:
    """One pre-resolved option the connector-quote basis prices. The underlying
    thesis and the contract identity are resolved upstream (by the Options Scout,
    agent-hosted); this basis only reads the REAL connector premium for it."""

    symbol: str
    contract_ticker: str
    strike: float
    expiry_date: str
    reference_close: float
    direction: str = "call"
    conviction: float = 70.0
    rank_score: float = 1.0


class ConnectorOptionQuotePlaySource:
    """Prices plays from the LIVE Robinhood option quote -- the execution basis.

    Used by an agent-hosted proving run that DOES hold the session-bound
    connector: for each pre-resolved target it reads the real option quote
    (client.get_option_quotes) and uses its mark/mid/last as the premium. Records
    itself as `connector_option_quote`. A target whose quote cannot be read is
    dropped for that cycle rather than priced on a guess.
    """

    basis_name = BASIS_CONNECTOR_QUOTE

    _PRICE_KEYS = ("mark_price", "adjusted_mark_price", "mid", "midpoint", "last_trade_price", "last_price", "price")

    def __init__(self, client: RobinhoodOptionClient, targets: Sequence[ConnectorQuoteTarget]) -> None:
        self.client = client
        self.targets = list(targets)
        # The REAL quotes this basis actually read, recorded as it prices each
        # cycle. The counting logic validates the connector-quote basis against
        # this: a run that claims the execution basis but recorded no real quote
        # is not evidence about a real market and does not count.
        self._recorded_quotes: list[dict[str, Any]] = []

    def provenance(self) -> dict[str, Any]:
        return {
            "basis": self.basis_name,
            "vendor": "Robinhood options OAuth connector",
            "contracts": [target.contract_ticker for target in self.targets],
            "recorded_quotes": list(self._recorded_quotes),
            "note": "premium = the live Robinhood option quote read at cycle time (the execution basis)",
        }

    def describe(self) -> str:
        return f"priced on {self.basis_name}: the live Robinhood option quote for {len(self.targets)} target(s)"

    @classmethod
    def _premium_from_quote(cls, payload: Any) -> float | None:
        rows: Iterable[Any]
        if isinstance(payload, dict):
            rows = payload.get("quotes", payload.get("results", [payload]))
        else:
            rows = payload or []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            for key in cls._PRICE_KEYS:
                if row.get(key) is not None:
                    try:
                        value = float(row[key])
                    except (TypeError, ValueError):
                        continue
                    if value > 0:
                        return value
        return None

    def plays(self, symbols: Sequence[str], today: date) -> list[OptionPlay]:
        wanted = {str(symbol).upper() for symbol in symbols}
        plays: list[OptionPlay] = []
        for target in self.targets:
            if target.symbol.upper() not in wanted:
                continue
            premium = self._premium_from_quote(self.client.get_option_quotes(target.contract_ticker))
            if premium is None:
                continue
            self._recorded_quotes.append(
                {"contract": target.contract_ticker, "premium": float(premium), "read_at": datetime.now(UTC).isoformat()}
            )
            plays.append(
                OptionPlay(
                    symbol=target.symbol.upper(),
                    direction=target.direction,
                    reference_close=float(target.reference_close),
                    entry=float(target.reference_close),
                    ceiling=round(float(target.reference_close) * 1.05, 2),
                    floor=round(float(target.reference_close) * 0.97, 2),
                    conviction=float(target.conviction),
                    rank_score=float(target.rank_score),
                    strike=float(target.strike),
                    expiry_date=target.expiry_date,
                    contract_ticker=target.contract_ticker,
                    premium=premium,
                )
            )
        return plays


# ---------------------------------------------------------------------------
# the bounded, unattended paper loop
# ---------------------------------------------------------------------------


def _fill_candidate(
    client: RobinhoodOptionClient,
    paper: PaperBroker,
    logger: SQLiteLogger,
    candidate: OptionOrderCandidate,
    rationale: str,
    notional: float,
    basis_name: str,
) -> dict[str, Any]:
    """Simulate ONE defined-risk paper fill, after re-validating the leg.

    The candidate's leg is re-run through the client's defined-risk build path
    (which also pins the agent account): a leg that is not defined-risk raises
    DefinedRiskViolationError and never fills. This ties the client's property-B
    guard to the paper loop, so no undefined-risk order can ever reach the ledger
    even in simulation. Then the debit paid (premium x multiplier x contracts,
    computed by the caller) is booked as a buy-to-open fill keyed by the contract.
    """
    # Re-validate defined risk + account. Raises on a naked/uncovered short.
    client.build_option_order(
        [candidate.leg],
        direction=candidate.order_direction,
        quantity=str(candidate.quantity),
        price=str(candidate.limit_price),
    )
    fill = paper.place_order(
        {
            "symbol": candidate.contract_ticker,
            "side": "buy",
            "quantity": float(candidate.quantity),
            "limit_price": float(candidate.limit_price),
            "notional": notional,
            "reason": rationale,
            "strategy_signal": "option_single_leg_long",
        }
    )
    logger.log_order(fill)
    logger.log_decision(
        candidate.symbol,
        ACTION_FILLED,
        rationale,
        {
            "venue": VENUE,
            "contract": candidate.contract_ticker,
            "underlying": candidate.symbol,
            "quantity": candidate.quantity,
            "premium": candidate.limit_price,
            "notional": notional,
            "max_loss_usd": candidate.max_loss_usd,
            "basis": basis_name,
        },
    )
    return fill


def _open_premium_at_risk(paper: PaperBroker, multiplier: int) -> float:
    """The premium-at-risk currently OPEN in the paper ledger, in dollars.

    Every fill in this lane is a single-leg long (max loss = debit paid), so a
    position's dollars-at-risk is its contract count x average premium x the
    contract multiplier. Summed over the ledger's open (net long) positions this
    is what the total-at-risk cap must see at the top of each cycle -- without it
    the cap resets to zero every cycle and a run could open unbounded cumulative
    risk one cycle at a time.
    """
    total = 0.0
    for position in paper.get_portfolio().positions.values():
        quantity = float(position.quantity)
        if quantity <= 0:
            continue
        total += quantity * float(position.average_price) * multiplier
    return round(total, 2)


def run_option_paper_loop(
    connector: OptionConnector,
    root: Path,
    play_source: OptionPlaySource,
    iterations: int | None = None,
    hours: float | None = None,
    poll_interval_seconds: int = 0,
    sleep=time.sleep,
    arm_store: Any | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """A BOUNDED, unattended options paper loop -- iterations and/or hours cap it
    so it always terminates on its own, and the options kill switch (its own
    STOP_TRADING_OPTIONS file, not crypto's or equities') can stop it early.

    Each cycle: read this cycle's priced plays from `play_source`, map them to
    DEFINED-RISK candidates through the options RiskManager (option_strategy /
    option_risk_gates, which logs every act/skip rationale), and simulate a paper
    fill for each actionable candidate. Paper only: the connector's order path is
    never touched. The loop RECORDS which basis it priced on, in the completion
    decision the counting logic reads.
    """
    if iterations is None and hours is None:
        raise ValueError("run_option_paper_loop requires iterations and/or hours -- an unbounded loop is refused")
    today = today or datetime.now(UTC).date()
    rules = load_option_settings(root)
    logger = SQLiteLogger(root / "data" / "trading_agent.db")
    kill = option_kill_switch(rules, root)
    client = RobinhoodOptionClient(connector, arm_store=arm_store, config_root=root)
    paper = PaperBroker(root / OPTION_PAPER_LEDGER)
    symbols = option_underlyings(rules)
    granted_level = resolve_granted_level(client)
    multiplier = OptionRiskConfig.from_rules(rules).contract_multiplier

    basis_name = getattr(play_source, "basis_name", UNRECORDED_BASIS)
    provenance = play_source.provenance() if hasattr(play_source, "provenance") else {"basis": basis_name}
    logger.log_decision(
        None,
        ACTION_BASIS,
        play_source.describe() if hasattr(play_source, "describe") else f"price basis: {basis_name}",
        {"venue": VENUE, **provenance},
    )

    completed = 0
    fills = 0
    halted = False
    deadline = time.monotonic() + (hours * 60 * 60) if hours is not None else None
    while True:
        if iterations is not None and completed >= iterations:
            break
        if deadline is not None and time.monotonic() >= deadline:
            break
        halt_reasons = kill.halt_reasons()
        if halt_reasons:
            logger.log_decision(None, ACTION_HALTED, "; ".join(halt_reasons), {"venue": VENUE})
            halted = True
            break

        plays = play_source.plays(symbols, today)
        # Compute the premium-at-risk already OPEN in the ledger at the top of the
        # cycle and thread it into the RiskManager, so the total-at-risk cap sees
        # exposure this run's prior cycles committed to -- not a fresh zero each
        # cycle (which would let cumulative risk grow unbounded one cycle at a time).
        open_at_risk = _open_premium_at_risk(paper, multiplier)
        decisions = plan_from_rules(
            plays, rules, granted_level, logger=logger, open_premium_at_risk_usd=open_at_risk, today=today
        )
        # Refresh the paper cash at the top of the cycle and decrement it as fills
        # book, so the loop never spends cash it does not have -- a cash account,
        # settled funds only. Without this the loop would drive cash negative over
        # many cycles and the reconcile would (correctly) refuse to come back clean.
        available = float(paper.get_portfolio().cash_usd)
        for decision in decisions:
            if not (decision.acted and decision.candidate is not None):
                continue
            candidate = decision.candidate
            notional = round(float(candidate.limit_price) * multiplier * int(candidate.quantity), 2)
            fees = round(notional * 0.001, 8)  # matches PaperBroker.place_order
            if notional + fees > available:
                logger.log_decision(
                    candidate.symbol,
                    "option_paper_fill_skipped",
                    f"insufficient paper cash for {candidate.contract_ticker}: needs "
                    f"{notional + fees:.2f}, only {available:.2f} available (cash account, settled funds only)",
                    {"venue": VENUE, "contract": candidate.contract_ticker, "notional": notional},
                )
                continue
            try:
                _fill_candidate(client, paper, logger, candidate, decision.rationale, notional, basis_name)
                available -= notional + fees
                fills += 1
            except DefinedRiskViolationError as exc:
                # Belt-and-suspenders: the mapper only frames long options, so
                # this cannot fire on a real candidate -- but if it ever did,
                # the fill is refused with a rationale, never booked.
                logger.log_decision(
                    candidate.symbol,
                    "option_paper_fill_refused",
                    f"defined-risk validation refused the fill: {exc}",
                    {"venue": VENUE, "contract": candidate.contract_ticker},
                )

        completed += 1
        if iterations is not None and completed >= iterations:
            break
        if deadline is not None and time.monotonic() >= deadline:
            break
        sleep(poll_interval_seconds)

    if not halted:
        # Re-read the provenance at completion (not the top-of-loop snapshot) so
        # what the basis ACTUALLY recorded while it priced -- the real connector
        # quotes it read, the underlying bars it loaded -- is what the counting
        # logic audits against the declared basis name.
        final_provenance = play_source.provenance() if hasattr(play_source, "provenance") else {"basis": basis_name}
        logger.log_decision(
            None,
            ACTION_COMPLETED,
            f"bounded options paper loop finished after {completed} iteration(s), priced on {basis_name}, "
            f"{fills} defined-risk fill(s)",
            {
                "venue": VENUE,
                "iterations_completed": completed,
                "fills": fills,
                # The claim the counting logic audits. A run priced on anything
                # but a real basis -- or one whose provenance does not substantiate
                # the declared basis -- says so here, and is not counted.
                "quote_basis": basis_name,
                "basis_provenance": final_provenance,
            },
        )
    return {"iterations_completed": completed, "halted": halted, "quote_basis": basis_name, "fills": fills}


# ---------------------------------------------------------------------------
# reconcile + the headless proving connector + the proving run entry point
# ---------------------------------------------------------------------------


# The paper broker charges this fee per fill (round(notional * rate, 8)); mirror
# it here so the cash the audit trail EXPECTS matches the ledger's cash exactly.
_PAPER_FEE_RATE = 0.001


def _expected_from_audit(db_path: Path) -> dict[str, Any]:
    """Reconstruct the options ledger's expected open state from an INDEPENDENT
    source -- the ACTION_FILLED decision rows in trading_agent.db, a separate
    store from the paper ledger the reconcile is checking.

    Returns per-contract expected quantities AND notionals plus the total cash
    outlay (premium notional + the paper broker's per-fill fee). Only fills SINCE
    the most recent reconcile are counted, so the reconstruction is scoped to the
    current, freshly-reset ledger and does not carry a prior run's (archived)
    fills. This is what lets the reconcile be falsifiable across THREE independent
    hooks -- quantity, notional/premium, and cash -- rather than the ledger being
    checked only against a recomputation of itself: right contracts booked at the
    wrong premium (a quantity match, a notional mismatch) is caught too.
    """
    rows = _decision_rows(db_path, (ACTION_FILLED, ACTION_RECONCILE))
    last_reconcile_id = max((row["id"] for row in rows if row["action"] == ACTION_RECONCILE), default=0)
    quantities: dict[str, float] = {}
    notionals: dict[str, float] = {}
    cash_outlay = 0.0
    for row in rows:
        if row["action"] != ACTION_FILLED or row["id"] <= last_reconcile_id:
            continue
        contract = row["details"].get("contract")
        if not contract:
            continue
        try:
            quantity = float(row["details"].get("quantity") or 0.0)
        except (TypeError, ValueError):
            continue
        try:
            notional = float(row["details"].get("notional") or 0.0)
        except (TypeError, ValueError):
            notional = 0.0
        contract = str(contract)
        quantities[contract] = quantities.get(contract, 0.0) + quantity
        notionals[contract] = round(notionals.get(contract, 0.0) + notional, 2)
        cash_outlay += notional + round(notional * _PAPER_FEE_RATE, 8)
    return {"quantities": quantities, "notionals": notionals, "cash_outlay": round(cash_outlay, 2)}


def _expected_positions_from_audit(db_path: Path) -> dict[str, float]:
    """The per-contract expected open quantities from the ACTION_FILLED audit
    trail (see _expected_from_audit for the independence and scoping rationale)."""
    return _expected_from_audit(db_path)["quantities"]


def reconcile_option_paper(root: Path, epsilon: float = 0.000001) -> dict[str, Any]:
    """Reconcile the options paper ledger against an INDEPENDENT expected-position
    source -- the ACTION_FILLED audit trail -- not just a recomputation of the
    same ledger. Mirrors reconcile_equity_paper over data/option_paper_trades.db,
    and asserts cash conservation and position consistency on top; 'reconciled
    clean' (errors == []) is a real, falsifiable claim because a ledger that
    disagrees with what the audit says was filled reports an error."""
    logger = SQLiteLogger(root / "data" / "trading_agent.db")
    broker = PaperBroker(root / OPTION_PAPER_LEDGER)
    expected = _expected_from_audit(root / "data" / "trading_agent.db")
    expected_positions = expected["quantities"]
    result = broker.reconcile_positions(epsilon=epsilon, expected_positions=expected_positions)

    # On top of the quantity check, cross-check the ledger's NOTIONAL/PREMIUM and
    # CASH against the SAME independent audit trail. A quantity match alone cannot
    # catch a fill booked at the wrong premium -- the right contract count for the
    # wrong dollars -- and cash conservation against the audit is a second,
    # causally independent hook (the ledger's own cash-vs-ledger check compares a
    # store against itself). Both are additive: they only ever ADD errors, so a
    # genuinely clean run still reports errors == [].
    notional_tolerance = max(epsilon, 0.01)
    multiplier = OptionRiskConfig.from_rules(load_option_settings(root)).contract_multiplier
    portfolio = broker.get_portfolio()
    observed_notional: dict[str, float] = {}
    for symbol, position in portfolio.positions.items():
        quantity = float(position.quantity)
        if quantity <= 0:
            continue
        observed_notional[symbol] = round(quantity * float(position.average_price) * multiplier, 2)
    for contract in set(expected["notionals"]) | set(observed_notional):
        expected_n = round(float(expected["notionals"].get(contract, 0.0)), 2)
        actual_n = round(float(observed_notional.get(contract, 0.0)), 2)
        if abs(expected_n - actual_n) > notional_tolerance:
            result["errors"].append(
                {"issue": "expected_notional_mismatch", "contract": contract, "expected": expected_n, "actual": actual_n}
            )
    # Cash: starting cash minus the audit's total outlay (premium + the paper
    # broker's per-fill fee) must equal the ledger's remaining cash.
    expected_cash = round(float(broker.starting_cash) - float(expected["cash_outlay"]), 2)
    actual_cash = round(float(portfolio.cash_usd), 2)
    if abs(expected_cash - actual_cash) > notional_tolerance:
        result["errors"].append(
            {"issue": "expected_cash_mismatch", "expected": expected_cash, "actual": actual_cash}
        )

    logger.log_decision(
        None,
        ACTION_RECONCILE,
        "options paper positions reconciled against the ACTION_FILLED audit trail",
        {**result, "venue": VENUE, "expected_positions": expected_positions},
    )
    return result


class PaperProvingOptionConnector:
    """Headless connector for a PAPER options proving run.

    Exposes ONLY the agent-tradable account identity (from config's
    equities.expected_account -- the SAME anchor the equities and options clients
    pin), reports a granted options level so the level gate passes, holds no real
    credential, and REFUSES every order path. A proving run prices on a real basis
    and never places, reviews, or cancels a real order; the connector is held for
    shape (what a live order WOULD go through), not to trade. (The live Robinhood
    MCP connector is session-bound and unavailable to a headless run.)
    """

    def __init__(self, expected: dict[str, str], granted_level: str = "level_3") -> None:
        self._account = {
            "account_number": f"PAPER-AGENTIC-{expected['number_suffix']}",
            "nickname": expected["nickname"],
            "agentic_allowed": True,
            "cash_available_for_trading": "10000.00",
        }
        self._granted_level = granted_level

    def get_accounts(self) -> Any:
        return {"accounts": [self._account]}

    def get_option_positions(self, account_number: str | None = None) -> Any:
        return {"positions": []}

    def get_option_level_upgrade_info(self, **kwargs: Any) -> Any:
        return {"option_level": self._granted_level}

    def get_option_chains(self, **kwargs: Any) -> Any:
        return {"chains": []}

    def get_option_quotes(self, **kwargs: Any) -> Any:
        # A proving run is PRICED from the basis (Massive underlying closes for
        # the default expected-move basis), not from this method; it exists only
        # to satisfy the connector shape.
        return {"quotes": []}

    def review_option_order(self, **kwargs: Any) -> Any:
        raise RuntimeError("PaperProvingOptionConnector: a paper proving run reviews no real order")

    def place_option_order(self, **kwargs: Any) -> Any:
        raise RuntimeError("PaperProvingOptionConnector: a paper proving run places no real order")

    def cancel_option_order(self, order_id: str, account_number: str | None = None) -> Any:
        raise RuntimeError("PaperProvingOptionConnector: a paper proving run cancels no real order")


def build_option_paper_proving_connector(root: Path) -> PaperProvingOptionConnector:
    """Build the headless options paper-proving connector from the configured
    agent-account identity (equities.expected_account, reused verbatim)."""
    return PaperProvingOptionConnector(_load_expected_account(root))


def run_option_proving_run(
    connector: OptionConnector,
    root: Path,
    iterations: int | None = None,
    hours: float | None = None,
    poll_interval_seconds: int = 0,
    sleep=time.sleep,
    client: MassiveClient | None = None,
    lookback_days: int = MAX_LOOKBACK_DAYS,
    expected_move_fraction: float = 0.03,
    days_to_expiry: int = 30,
    underlyings: Sequence[str] | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """A bounded options paper proving run priced on a REAL basis.

    The default basis is `underlying_expected_move`: premiums derived from REAL
    Massive underlying closes (the same real-history plumbing the equities proving
    run uses) times an expected-move fraction. The connector is still held (it is
    what a live order would go through), but no order is placed and no connector
    quote prices a fill, so the run is reproducible against a market that actually
    happened.

    This is the entry point an agent-hosted options proving run should call, and
    the target of the `run-option-proving-run` CLI command.
    """
    # .env first so a MASSIVE_API_KEY there is seen before the client resolves its
    # key -- a proving run should not need a live gcloud/ADC session when the key
    # is already configured locally.
    load_dotenv(root / ".env", override=False)
    rules = load_option_settings(root)
    symbols = [str(symbol).upper() for symbol in underlyings] if underlyings else option_underlyings(rules)
    # FRESH ledger (archived, not destroyed) so the runs are GENUINELY INDEPENDENT:
    # a run trades on its own from starting cash, not inheriting the prior run's
    # open contracts -- which would leave it with no fill in its window, and a
    # zero-fill run does not count.
    PaperBroker(root / OPTION_PAPER_LEDGER).reset(archive=True)
    shared_massive = client or MassiveClient(min_interval=MIN_REQUEST_INTERVAL_SECONDS)
    feed = MassiveHistoryFeed(shared_massive, symbols, lookback_days=lookback_days, today=today)
    # Bound the derived premium so every fill's debit clears the configured
    # per-trade debit cap regardless of the underlying's price -- otherwise a
    # highly-priced underlying would price a debit over the cap and the run would
    # fill nothing (and a zero-fill run does not count).
    risk_config = OptionRiskConfig.from_rules(rules)
    max_premium = (risk_config.max_debit_premium_per_trade_usd / max(risk_config.contract_multiplier, 1)) * 0.9
    play_source = ExpectedMovePlaySource(
        feed,
        expected_move_fraction=expected_move_fraction,
        days_to_expiry=days_to_expiry,
        max_premium=max_premium,
    )
    summary = run_option_paper_loop(
        connector,
        root,
        play_source=play_source,
        iterations=iterations,
        hours=hours,
        poll_interval_seconds=poll_interval_seconds,
        sleep=sleep,
        today=today,
    )
    # Reconcile AFTER the loop and log it, so the counting logic pairs this run's
    # completion with a following clean reconcile.
    reconcile = reconcile_option_paper(root)
    counts = (
        summary["quote_basis"] in REAL_OPTION_BASES
        and _provenance_supports_basis(summary["quote_basis"], play_source.provenance())
        and not summary["halted"]
        and int(summary["iterations_completed"]) > 0
        and int(summary["fills"]) >= 1
        and reconcile.get("errors") == []
    )
    return {
        **summary,
        "quote_basis": BASIS_EXPECTED_MOVE,
        "provenance": play_source.provenance(),
        "reconcile": reconcile,
        "counts": counts,
    }


# ---------------------------------------------------------------------------
# counting: which recorded runs actually count
# ---------------------------------------------------------------------------


def _decision_rows(db_path: Path, actions: tuple[str, ...]) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    placeholders = ", ".join("?" for _ in actions)
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT id, timestamp, action, reason, details FROM decisions WHERE action IN ({placeholders}) ORDER BY id ASC",
            actions,
        ).fetchall()
    parsed: list[dict[str, Any]] = []
    for row in rows:
        try:
            details = json.loads(row[4]) if row[4] else {}
        except json.JSONDecodeError:
            details = {}
        parsed.append({"id": row[0], "timestamp": row[1], "action": row[2], "reason": row[3], "details": details})
    return parsed


def option_paper_proving_runs(root: Path) -> list[dict[str, Any]]:
    """Completed unattended options paper runs, each paired with the reconcile
    that followed it and judged clean against what actually happened in its window.

    A run is `clean` only if ALL of the following hold, so that a run being clean
    is a real, falsifiable claim rather than a tautology:

      - the run recorded its basis, that basis is one of the REAL bases, AND the
        provenance it recorded substantiates that basis (expected-move must carry
        a real Massive underlying window; connector-quote must carry a real
        recorded quote) -- a made-up, unrecorded, or unsubstantiated basis is not
        evidence about a real market;
      - a reconcile falls in this run's OWN window (after this completion and
        before the next), consumed by at most one run, and it reported no errors;
      - the loop actually iterated;
      - the window holds at least one DEFINED-RISK fill AND the summed fill
        notional moved the ledger non-trivially -- a zero-fill run proves nothing.
    """
    db_path = root / "data" / "trading_agent.db"
    rows = _decision_rows(db_path, (ACTION_COMPLETED, ACTION_RECONCILE, ACTION_FILLED, ACTION_HALTED))
    completions = [row for row in rows if row["action"] == ACTION_COMPLETED]
    reconciles = [row for row in rows if row["action"] == ACTION_RECONCILE]
    fills = [row for row in rows if row["action"] == ACTION_FILLED]
    # Every event that ENDS a run: a clean completion, the reconcile that follows
    # a run, or a mid-run halt. A run's fill window starts at the previous such
    # boundary -- NOT merely the previous completion -- because a halted run logs
    # no completion but still ends a run (and still reconciles). Anchoring on the
    # previous completion alone would sweep a halted run's fills forward into the
    # next completed run's window, orphaning them onto the wrong run.
    boundary_ids = sorted(
        row["id"] for row in rows if row["action"] in (ACTION_COMPLETED, ACTION_RECONCILE, ACTION_HALTED)
    )

    consumed: set[int] = set()
    runs: list[dict[str, Any]] = []
    for index, completion in enumerate(completions):
        completion_id = completion["id"]
        # The latest run-ending boundary strictly before this completion. The
        # reconcile that follows THIS completion has a higher id, so it is never
        # picked as the lower bound.
        lower = max((bid for bid in boundary_ids if bid < completion_id), default=0)
        next_id = completions[index + 1]["id"] if index + 1 < len(completions) else None

        following = None
        for rec in reconciles:
            if rec["id"] in consumed:
                continue
            if rec["id"] > completion_id and (next_id is None or rec["id"] < next_id):
                following = rec
                consumed.add(rec["id"])
                break

        window_fills = [fill for fill in fills if lower < fill["id"] <= completion_id]
        ledger_delta = sum(abs(float(fill["details"].get("notional") or 0.0)) for fill in window_fills)
        iterations = int(completion["details"].get("iterations_completed") or 0)
        basis = str(completion["details"].get("quote_basis") or UNRECORDED_BASIS)
        provenance = completion["details"].get("basis_provenance")
        basis_substantiated = _provenance_supports_basis(basis, provenance)
        errors = (following or {}).get("details", {}).get("errors", None)
        runs.append(
            {
                "completed_at": completion["timestamp"],
                "quote_basis": basis,
                "basis_is_real": basis in REAL_OPTION_BASES,
                "basis_substantiated": basis_substantiated,
                "iterations_completed": iterations,
                "reconciled": following is not None,
                "reconcile_errors": errors,
                "fills": len(window_fills),
                "ledger_delta": ledger_delta,
                "clean": (
                    basis in REAL_OPTION_BASES
                    and basis_substantiated
                    and following is not None
                    and errors == []
                    and iterations > 0
                    and len(window_fills) >= 1
                    and ledger_delta > _LEDGER_DELTA_EPSILON
                ),
            }
        )
    return runs
