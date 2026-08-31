"""Map the Options Scout's ranked plays onto DEFINED-RISK order candidates the
options RiskManager evaluates -- the bridge between analysis and the gated
execution lane, and nothing more.

WHERE THIS SITS
    options_scout (analysis-only) ranks candidate Plays. The RobinhoodOptionBroker
    executes gated, defined-risk orders. This module is the seam between them: it
    takes the scout's ranked Plays and, for each, either produces ONE defined-risk
    order candidate (a single-leg long call/put -- max loss = premium paid) or
    SKIPS the play, and it runs every candidate through the options risk gates
    (src.option_risk_gates.evaluate_option_order -- the RiskManager for this lane)
    before calling it actionable.

    It NEVER submits, reviews, or cancels an order and never touches the connector.
    The scout stays analysis-only; this mapper stays decision-only. The candidate
    it emits is an inert dataclass a caller hands to the broker, which re-runs its
    own account / arm / kill-switch / defined-risk gates at the irreversible moment.

WHAT IS DECIDED, AND WHY EACH SKIP HAPPENS
    A play is SKIPPED, with a named, logged reason, when:
      * it is not priceable/tradable -- no listed contract, no premium, or no
        readable expiry (the mapper cannot size or price an order it cannot see);
      * its conviction is below the configured floor (a weak edge is not traded);
      * its rank score is below the configured floor;
      * ANY options risk gate blocks it (debit cap, total-at-risk cap, DTE floor,
        0DTE, contract cap, or approval level) -- the gate's own named reason is
        carried through verbatim.
    A play is ACTED ON only when it is priceable, clears the conviction/rank
    floors, AND every risk gate passes.

AUDIT
    EVERY decision -- act or skip -- writes a readable rationale to the audit log
    (SQLiteLogger.log_decision): the option (contract, direction, strike, expiry),
    the underlying levels (entry / target / stop / reference), the conviction, and
    exactly why the play was allowed or blocked. A skip is never silent.

CONFIG-DRIVEN
    The mapping floors are read from `options.strategy` in trading_rules.yaml via
    OptionStrategyConfig.from_rules; the risk caps from `options.risk` via
    OptionRiskConfig.from_rules. A missing section yields conservative defaults,
    never a permissive blank.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Protocol

from .option_risk_gates import (
    OptionOrderProposal,
    OptionRiskConfig,
    OptionRiskDecision,
    classify_strategy,
    evaluate_option_order,
)

# Conservative first-proving defaults for the mapping floors, used when the
# `options.strategy` section is absent from config.
_DEFAULT_MIN_CONVICTION = 50.0
_DEFAULT_MIN_RANK_SCORE = 0.0
_DEFAULT_CONTRACTS_PER_ORDER = 1


class SkipReason:
    """Stable machine-readable tags a skip is recorded under -- one per cause, so
    a downstream reader can group skips without parsing the human sentence."""

    NO_CONTRACT = "no_listed_contract"
    NO_PREMIUM = "no_premium"
    NO_EXPIRY = "no_readable_expiry"
    LOW_CONVICTION = "below_conviction_floor"
    LOW_RANK = "below_rank_floor"
    RISK_GATE = "risk_gate_blocked"
    BAD_DIRECTION = "unsupported_direction"


# Audit-log action names. Kept distinct so act and skip are trivially separable.
ACTION_SELECTED = "option_play_selected"
ACTION_SKIPPED = "option_play_skipped"


class ScoutPlay(Protocol):
    """The subset of an options_scout Play this mapper reads. Duck-typed so the
    mapper never depends on the analyzer module (analysis stays one-way): any
    object carrying these attributes is a valid input."""

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


@dataclass(frozen=True)
class OptionStrategyConfig:
    """The mapping floors, read from `options.strategy` in trading_rules.yaml."""

    min_conviction: float = _DEFAULT_MIN_CONVICTION
    min_rank_score: float = _DEFAULT_MIN_RANK_SCORE
    contracts_per_order: int = _DEFAULT_CONTRACTS_PER_ORDER

    @classmethod
    def from_rules(cls, rules: Mapping[str, Any] | None) -> "OptionStrategyConfig":
        """Build from parsed trading_rules.yaml. A missing `options.strategy`
        section yields the conservative defaults; a bad type on one key falls
        back to that key's default rather than crashing."""
        section: Mapping[str, Any] = {}
        if isinstance(rules, Mapping):
            options = rules.get("options")
            if isinstance(options, Mapping) and isinstance(options.get("strategy"), Mapping):
                section = options["strategy"]
        contracts = _as_int(section.get("contracts_per_order"))
        return cls(
            min_conviction=_as_float(section.get("min_conviction"), _DEFAULT_MIN_CONVICTION),
            min_rank_score=_as_float(section.get("min_rank_score"), _DEFAULT_MIN_RANK_SCORE),
            contracts_per_order=contracts if contracts and contracts > 0 else _DEFAULT_CONTRACTS_PER_ORDER,
        )


@dataclass(frozen=True)
class OptionOrderCandidate:
    """A DEFINED-RISK order candidate mapped from one play -- a single-leg long
    call/put (max loss = premium paid). Inert: it carries the connector-shaped
    leg, the sizing and the risk proposal, but never submits anything. A caller
    hands it to RobinhoodOptionBroker, which re-runs its own gates."""

    symbol: str
    direction: str  # "call" | "put" (the underlying thesis direction)
    order_direction: str  # always "debit" for a long option
    contract_ticker: str
    quantity: int
    limit_price: float  # per-share premium (x100 x qty = total debit)
    max_loss_usd: float
    leg: Mapping[str, Any]
    proposal: OptionOrderProposal


@dataclass(frozen=True)
class StrategyDecision:
    """The mapper's verdict on ONE play. `action` is "act" or "skip"; the
    rationale is the same human line written to the audit log."""

    symbol: str
    action: str  # "act" | "skip"
    reason_code: str
    rationale: str
    conviction: float
    candidate: OptionOrderCandidate | None = None
    risk_decision: OptionRiskDecision | None = None
    blocking_gates: tuple[str, ...] = field(default_factory=tuple)

    @property
    def acted(self) -> bool:
        return self.action == "act"


# --- small parsing helpers (same posture as option_risk_gates) ----------------


def _as_float(value: Any, default: float) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any) -> int | None:
    try:
        if isinstance(value, bool):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _days_to_expiry(expiry_date: str | None, today: date) -> int | None:
    """Calendar days from `today` to the contract expiry, or None when the
    expiry string is absent or unparseable (the DTE gate cannot run without it)."""
    if not expiry_date:
        return None
    try:
        expiry = date.fromisoformat(str(expiry_date)[:10])
    except (TypeError, ValueError):
        return None
    return (expiry - today).days


def _levels_phrase(play: ScoutPlay) -> str:
    """The underlying-level context every rationale carries: entry, the thesis
    target, and the stop, plus the reference close the levels hang off."""
    if play.direction == "call":
        target, stop = play.ceiling, play.floor
    else:
        target, stop = play.floor, play.ceiling
    return (
        f"underlying ref {play.reference_close:.2f}, entry {play.entry:.2f}, "
        f"target {target:.2f}, stop {stop:.2f}"
    )


def _contract_phrase(play: ScoutPlay) -> str:
    """The option-identifying context every rationale carries."""
    strike = f"{play.strike:.2f}" if play.strike is not None else "n/a"
    expiry = play.expiry_date or "n/a"
    ticker = play.contract_ticker or "n/a"
    return f"{play.direction.upper()} {play.symbol} strike {strike} exp {expiry} ({ticker})"


def _skip(
    play: ScoutPlay,
    reason_code: str,
    why: str,
    *,
    risk_decision: OptionRiskDecision | None = None,
    blocking: tuple[str, ...] = (),
) -> StrategyDecision:
    rationale = (
        f"SKIP {_contract_phrase(play)}: {why}. "
        f"Conviction {play.conviction:.1f}, rank {play.rank_score:.3f}. {_levels_phrase(play)}."
    )
    return StrategyDecision(
        symbol=play.symbol,
        action="skip",
        reason_code=reason_code,
        rationale=rationale,
        conviction=float(play.conviction),
        risk_decision=risk_decision,
        blocking_gates=blocking,
    )


def _long_leg(contract_ticker: str) -> dict[str, Any]:
    """A single BUY-to-open leg -- defined-risk by construction (max loss =
    premium paid). classify_strategy reads this as single_leg_long (level 2)."""
    return {"side": "buy", "position_effect": "open", "ratio_quantity": 1, "option": contract_ticker}


def map_play(
    play: ScoutPlay,
    risk_config: OptionRiskConfig,
    strategy_config: OptionStrategyConfig,
    granted_level: int | None,
    *,
    open_premium_at_risk_usd: float = 0.0,
    today: date | None = None,
) -> StrategyDecision:
    """Decide ONE play: build a defined-risk candidate and gate it, or skip it.

    The order of checks is deliberate -- cheapest and most fundamental first, so
    a skip is attributed to its real, single cause:
      1. direction must be a long call/put (the only shape this lane frames);
      2. the play must be priceable/tradable (contract, premium, readable expiry);
      3. conviction and rank must clear their floors;
      4. every options risk gate must pass (debit, total-at-risk, DTE, contracts,
         approval level -- granted_level None fails the level gate CLOSED).
    Only a play that clears all four becomes an actionable candidate.
    """
    today = today or datetime.now(UTC).date()

    if play.direction not in {"call", "put"}:
        return _skip(
            play, SkipReason.BAD_DIRECTION,
            f"direction {play.direction!r} is not a long call/put; this lane frames long options only",
        )

    # Priceable / tradable: without a listed contract, a premium and an expiry the
    # mapper cannot size, price, or DTE-gate an order.
    if not play.contract_ticker:
        return _skip(play, SkipReason.NO_CONTRACT, "no listed option contract was resolved for this play")
    if play.premium is None or float(play.premium) <= 0.0:
        return _skip(play, SkipReason.NO_PREMIUM, "no positive option premium is available to price the order")
    dte = _days_to_expiry(play.expiry_date, today)
    if dte is None:
        return _skip(play, SkipReason.NO_EXPIRY, "the contract expiry could not be read, so DTE cannot be checked")

    # Conviction / rank floors -- a weak edge is not traded, and the reason says so.
    if float(play.conviction) < strategy_config.min_conviction:
        return _skip(
            play, SkipReason.LOW_CONVICTION,
            f"conviction {play.conviction:.1f} is below the {strategy_config.min_conviction:.1f} floor",
        )
    if float(play.rank_score) < strategy_config.min_rank_score:
        return _skip(
            play, SkipReason.LOW_RANK,
            f"rank score {play.rank_score:.3f} is below the {strategy_config.min_rank_score:.3f} floor",
        )

    # Build the defined-risk proposal and run the options RiskManager over it.
    quantity = max(int(strategy_config.contracts_per_order), 1)
    premium = float(play.premium)
    leg = _long_leg(play.contract_ticker)
    proposal = OptionOrderProposal(
        legs=[leg],
        net_premium_per_contract=premium,
        quantity=quantity,
        days_to_expiry=dte,
        direction="debit",
    )
    decision = evaluate_option_order(
        risk_config, proposal, granted_level, open_premium_at_risk_usd=open_premium_at_risk_usd
    )
    if not decision.allowed:
        return _skip(
            play, SkipReason.RISK_GATE,
            f"blocked by options risk gate(s): {decision.reason}",
            risk_decision=decision,
            blocking=tuple(decision.blocking_names),
        )

    max_loss = proposal.premium_at_risk_usd(risk_config.contract_multiplier)
    candidate = OptionOrderCandidate(
        symbol=play.symbol,
        direction=play.direction,
        order_direction="debit",
        contract_ticker=play.contract_ticker,
        quantity=quantity,
        limit_price=premium,
        max_loss_usd=max_loss,
        leg=leg,
        proposal=proposal,
    )
    rationale = (
        f"ACT {_contract_phrase(play)}: {quantity}x long, limit {premium:.2f}/share, "
        f"max loss ${max_loss:,.2f} ({classify_strategy([leg])}, level ok). "
        f"Conviction {play.conviction:.1f}, rank {play.rank_score:.3f}, {dte}d to expiry. "
        f"{_levels_phrase(play)}. All options risk gates passed."
    )
    return StrategyDecision(
        symbol=play.symbol,
        action="act",
        reason_code="all_gates_passed",
        rationale=rationale,
        conviction=float(play.conviction),
        candidate=candidate,
        risk_decision=decision,
    )


def _log_decision(logger: Any, decision: StrategyDecision) -> None:
    """Write one decision to the audit log. A None logger is a no-op (a caller
    that only wants the decisions in-memory), but a real run always passes one so
    no act/skip is silent."""
    if logger is None:
        return
    action = ACTION_SELECTED if decision.acted else ACTION_SKIPPED
    details: dict[str, Any] = {
        "venue": "robinhood_options",
        "reason_code": decision.reason_code,
        "conviction": decision.conviction,
        "action": decision.action,
    }
    if decision.blocking_gates:
        details["blocking_gates"] = list(decision.blocking_gates)
    if decision.candidate is not None:
        candidate = decision.candidate
        details["contract"] = candidate.contract_ticker
        details["quantity"] = candidate.quantity
        details["limit_price"] = candidate.limit_price
        details["max_loss_usd"] = candidate.max_loss_usd
    logger.log_decision(decision.symbol, action, decision.rationale, details)


def plan_option_orders(
    plays: Iterable[ScoutPlay],
    risk_config: OptionRiskConfig,
    strategy_config: OptionStrategyConfig,
    granted_level: int | None,
    *,
    logger: Any = None,
    open_premium_at_risk_usd: float = 0.0,
    today: date | None = None,
) -> list[StrategyDecision]:
    """Decide every scout play, logging a rationale for each act/skip.

    Returns one StrategyDecision per play, in input (ranked) order. Actionable
    plays carry an OptionOrderCandidate; skipped plays carry the named reason.
    This function never submits an order -- it maps and gates only. The running
    total of premium at risk is threaded forward across ACTED candidates so the
    total-at-risk cap sees the exposure THIS batch has already committed to, not
    just what was open when the batch began.
    """
    decisions: list[StrategyDecision] = []
    committed_at_risk = max(float(open_premium_at_risk_usd), 0.0)
    for play in plays:
        decision = map_play(
            play,
            risk_config,
            strategy_config,
            granted_level,
            open_premium_at_risk_usd=committed_at_risk,
            today=today,
        )
        _log_decision(logger, decision)
        if decision.acted and decision.candidate is not None:
            committed_at_risk += decision.candidate.max_loss_usd
        decisions.append(decision)
    return decisions


def plan_from_rules(
    plays: Iterable[ScoutPlay],
    rules: Mapping[str, Any] | None,
    granted_level: int | None,
    *,
    logger: Any = None,
    open_premium_at_risk_usd: float = 0.0,
    today: date | None = None,
) -> list[StrategyDecision]:
    """Convenience entry: build both configs from parsed trading_rules.yaml, then
    plan. `granted_level` is the account's Robinhood options approval level
    (resolve it from get_option_level_upgrade_info via
    option_risk_gates.resolve_granted_level); None fails the level gate closed."""
    return plan_option_orders(
        plays,
        OptionRiskConfig.from_rules(rules),
        OptionStrategyConfig.from_rules(rules),
        granted_level,
        logger=logger,
        open_premium_at_risk_usd=open_premium_at_risk_usd,
        today=today,
    )


def actionable_candidates(decisions: Sequence[StrategyDecision]) -> list[OptionOrderCandidate]:
    """The order candidates from the acted decisions, ranked order preserved."""
    return [d.candidate for d in decisions if d.acted and d.candidate is not None]
