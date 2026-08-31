"""Options-lane risk gates -- config-driven caps that only make sense for
DEFINED-RISK options, sitting alongside the shared RiskManager, kill switch and
arm gate rather than replacing any of them.

WHAT THIS ADDS OVER THE SHARED RISK LAYER
    The equities/crypto RiskManager reasons in dollars-of-notional per equity
    trade. An option order is priced differently -- a per-share premium times a
    100-share contract multiplier times a contract count, with an expiry and a
    Robinhood approval LEVEL attached -- so it needs a handful of caps the
    shared layer cannot express. Each gate below is one such cap:

      * MAX DEBIT PREMIUM PER TRADE. The net premium a single order may PAY. For
        a long option or a debit spread that is exactly the trade's max loss.
      * MAX TOTAL PREMIUM AT RISK. A ceiling on this order's max loss PLUS the
        premium already at risk in open option positions -- a portfolio cap, not
        a per-trade one.
      * MIN DTE FLOOR. An order expiring in fewer sessions than the floor is
        refused; 0DTE (same-day expiry) is blocked by default regardless of the
        floor, and enabling it is a deliberate opt-in.
      * MAX CONTRACTS PER ORDER. A blunt size cap on contract count.
      * OPTION APPROVAL LEVEL. The account's granted Robinhood options level is
        read from get_option_level_upgrade_info; a strategy that needs a HIGHER
        level than was granted is refused. An unknown/unreadable level fails
        CLOSED (refuse), never open.

    Every gate BLOCKS its own violation with a NAMED reason (the GateName
    constants below), so a refusal says exactly which cap stopped it.

WHAT THIS IS NOT
    Not a submit path, and not the defined-risk guard. This module never touches
    the connector and never builds an order payload -- it takes a proposal and
    the config and returns a verdict. Naked/uncovered shorts are refused earlier,
    at build time, by RobinhoodOptionClient / RobinhoodOptionBroker; the level
    gate here classifies only the defined-risk strategies the lane actually
    places, and treats anything it cannot classify as defined-risk as needing a
    level the lane never grants (fail closed).

CONFIG-DRIVEN
    Everything tunable is read from config/trading_rules.yaml `options.risk`
    via OptionRiskConfig.from_rules. A missing section yields the conservative
    defaults below rather than a crash or a permissive blank.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

# One equity option contract controls this many shares unless config overrides.
DEFAULT_CONTRACT_MULTIPLIER = 100

# Conservative first-proving defaults, used when a key is absent from config.
_DEFAULT_MAX_DEBIT_PREMIUM_USD = 500.0
_DEFAULT_MAX_TOTAL_PREMIUM_USD = 1500.0
_DEFAULT_MIN_DTE = 2
_DEFAULT_MAX_CONTRACTS = 5

# The lane-internal strategy classes and the option level each needs. Deliberately
# NOT Robinhood strategy names (no "covered call", no naked vocabulary) so this
# table never trips the order-safety guard's forbidden-strategy scan.
STRATEGY_REDUCING = "reducing"
STRATEGY_SINGLE_LEG_LONG = "single_leg_long"
STRATEGY_LONG_MULTI_LEG = "long_multi_leg"
# Retained for config back-compat (the level map below still carries it), but
# classify_strategy NEVER returns it any more: until strike-aware spread support
# exists the lane cannot prove a short is covered, so every opening sell is
# UNSUPPORTED rather than a defined-risk spread.
STRATEGY_DEFINED_RISK_SPREAD = "defined_risk_spread"
# A leg set the lane does not support as defined risk (ANY opening sell). The
# shared defined-risk validator refuses it earlier; here it is assigned a level
# the lane never grants so the level gate also fails closed.
STRATEGY_UNSUPPORTED = "unsupported"

_DEFAULT_STRATEGY_MIN_LEVEL: dict[str, int] = {
    STRATEGY_REDUCING: 0,
    STRATEGY_SINGLE_LEG_LONG: 2,
    STRATEGY_LONG_MULTI_LEG: 3,
    STRATEGY_DEFINED_RISK_SPREAD: 3,
}

# The level demanded of a strategy class with no configured minimum -- higher
# than any level this lane ever trades, so an unclassifiable order is refused.
_UNSUPPORTED_MIN_LEVEL = 4


class GateName:
    """Stable names a refusal is reported under -- one per cap."""

    MAX_DEBIT_PREMIUM = "max_debit_premium_per_trade"
    MAX_TOTAL_PREMIUM_AT_RISK = "max_total_premium_at_risk"
    ZERO_DTE = "zero_dte_blocked"
    MIN_DTE = "min_days_to_expiry"
    EXPIRED = "already_expired"
    MAX_CONTRACTS = "max_contracts_per_order"
    OPTION_APPROVAL_LEVEL = "option_approval_level"
    INVALID_PROPOSAL = "invalid_proposal"


@dataclass(frozen=True)
class OptionRiskConfig:
    """The options-lane caps, read from `options.risk` in trading_rules.yaml."""

    max_debit_premium_per_trade_usd: float = _DEFAULT_MAX_DEBIT_PREMIUM_USD
    max_total_premium_at_risk_usd: float = _DEFAULT_MAX_TOTAL_PREMIUM_USD
    contract_multiplier: int = DEFAULT_CONTRACT_MULTIPLIER
    min_days_to_expiry: int = _DEFAULT_MIN_DTE
    allow_zero_dte: bool = False
    max_contracts_per_order: int = _DEFAULT_MAX_CONTRACTS
    strategy_min_option_level: Mapping[str, int] = field(
        default_factory=lambda: dict(_DEFAULT_STRATEGY_MIN_LEVEL)
    )

    @classmethod
    def from_rules(cls, rules: Mapping[str, Any] | None) -> "OptionRiskConfig":
        """Build the config from parsed trading_rules.yaml.

        A missing `options.risk` section yields the conservative defaults, never
        a permissive blank: an absent cap is treated as the default, not as "no
        cap". Bad types fall back to the default for that one key.
        """
        section: Mapping[str, Any] = {}
        if isinstance(rules, Mapping):
            options = rules.get("options")
            if isinstance(options, Mapping) and isinstance(options.get("risk"), Mapping):
                section = options["risk"]

        levels = dict(_DEFAULT_STRATEGY_MIN_LEVEL)
        configured_levels = section.get("strategy_min_option_level")
        if isinstance(configured_levels, Mapping):
            for name, value in configured_levels.items():
                parsed = _as_int(value)
                if parsed is not None:
                    levels[str(name)] = parsed

        return cls(
            max_debit_premium_per_trade_usd=_as_float(
                section.get("max_debit_premium_per_trade_usd"), _DEFAULT_MAX_DEBIT_PREMIUM_USD
            ),
            max_total_premium_at_risk_usd=_as_float(
                section.get("max_total_premium_at_risk_usd"), _DEFAULT_MAX_TOTAL_PREMIUM_USD
            ),
            contract_multiplier=_as_int(section.get("contract_multiplier")) or DEFAULT_CONTRACT_MULTIPLIER,
            min_days_to_expiry=_as_int(section.get("min_days_to_expiry"))
            if _as_int(section.get("min_days_to_expiry")) is not None
            else _DEFAULT_MIN_DTE,
            allow_zero_dte=bool(section.get("allow_zero_dte", False)),
            max_contracts_per_order=_as_int(section.get("max_contracts_per_order")) or _DEFAULT_MAX_CONTRACTS,
            strategy_min_option_level=levels,
        )

    def min_level_for(self, strategy: str) -> int:
        """The option level a strategy class needs. An unmapped class demands a
        level the lane never grants, so it fails closed at the level gate."""
        return int(self.strategy_min_option_level.get(strategy, _UNSUPPORTED_MIN_LEVEL))


@dataclass(frozen=True)
class OptionOrderProposal:
    """A proposed option order, as far as the risk gates need to see it.

    `net_premium_per_contract` is the per-share premium of the order (its limit
    price): positive for a debit (paying), which for a long or a debit spread is
    the per-contract max loss. `max_loss_per_contract_usd` lets a caller state a
    max loss the premium does not equal -- a credit vertical's (width - credit) --
    otherwise the at-risk figure is derived from the debit paid.
    """

    legs: Sequence[Mapping[str, Any]]
    net_premium_per_contract: float
    quantity: int
    days_to_expiry: int
    direction: str = "debit"
    max_loss_per_contract_usd: float | None = None

    def debit_premium_usd(self, multiplier: int) -> float:
        """Total net premium PAID by this order, in dollars. Zero for a credit
        order (it collects premium; the debit-premium cap does not apply)."""
        if str(self.direction).strip().lower() == "credit":
            return 0.0
        per_share = max(float(self.net_premium_per_contract), 0.0)
        return per_share * multiplier * max(int(self.quantity), 0)

    def premium_at_risk_usd(self, multiplier: int) -> float:
        """Total dollars this order puts at risk (its max loss).

        Uses an explicit per-contract max loss when the caller supplied one (a
        credit spread's width-minus-credit); otherwise the debit paid, which is
        the max loss of a long option or a debit spread.
        """
        if self.max_loss_per_contract_usd is not None:
            per_contract = max(float(self.max_loss_per_contract_usd), 0.0)
        else:
            per_contract = max(float(self.net_premium_per_contract), 0.0) * multiplier
        return per_contract * max(int(self.quantity), 0)


@dataclass(frozen=True)
class GateResult:
    """One gate's verdict."""

    name: str
    passed: bool
    reason: str
    observed: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OptionRiskDecision:
    """The combined verdict of every options risk gate over one proposal."""

    allowed: bool
    strategy: str
    results: tuple[GateResult, ...]

    @property
    def blocking(self) -> list[GateResult]:
        return [result for result in self.results if not result.passed]

    @property
    def blocking_names(self) -> list[str]:
        return [result.name for result in self.blocking]

    @property
    def reason(self) -> str:
        """A single human-readable reason line for the audit log."""
        blocking = self.blocking
        if not blocking:
            return "all options risk gates passed"
        return "; ".join(f"[{result.name}] {result.reason}" for result in blocking)


# --- small parsing helpers ----------------------------------------------------


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any) -> int | None:
    try:
        if isinstance(value, bool):  # bool is an int subclass; never a count here
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


_LEVEL_DIGITS = re.compile(r"(\d+)")

# Keys, in priority order, under which a connector reports the granted level.
_LEVEL_KEYS = (
    "option_level",
    "current_option_level",
    "options_level",
    "current_level",
    "max_option_level",
    "level",
    "approved_level",
)


def parse_option_level(info: Any) -> int | None:
    """The granted options approval level as an int, from a get_option_level_
    upgrade_info payload of loosely-known shape, or None if none can be read.

    Handles `{"option_level": "level_3"}`, `{"option_level": 3}`,
    `{"current_option_level": "3"}`, a nested `{"account": {...}}`, and a bare
    int/str. Returns None when nothing level-shaped is present -- the caller
    treats an unknown level as fail-closed.
    """
    if info is None:
        return None
    if isinstance(info, bool):
        return None
    if isinstance(info, int):
        return info
    if isinstance(info, str):
        match = _LEVEL_DIGITS.search(info)
        return int(match.group(1)) if match else None
    if isinstance(info, Mapping):
        for key in _LEVEL_KEYS:
            if key in info:
                parsed = parse_option_level(info[key])
                if parsed is not None:
                    return parsed
        # Fall back to a shallow scan of nested mappings (e.g. an "account" wrap).
        for value in info.values():
            if isinstance(value, Mapping):
                parsed = parse_option_level(value)
                if parsed is not None:
                    return parsed
        return None
    return None


def resolve_granted_level(source: Any) -> int | None:
    """The granted option level from a raw info mapping, or from any object that
    exposes get_option_level_upgrade_info() / get_level_upgrade_info() (a client
    or broker). Returns None when it cannot be read -- fail closed."""
    if source is None:
        return None
    if isinstance(source, (Mapping, str, int)) and not isinstance(source, bool):
        return parse_option_level(source)
    for method in ("get_option_level_upgrade_info", "get_level_upgrade_info"):
        fetch = getattr(source, method, None)
        if callable(fetch):
            try:
                return parse_option_level(fetch())
            except Exception:
                return None
    return None


# --- strategy classification --------------------------------------------------


def _leg_role(leg: Mapping[str, Any]) -> tuple[str, str]:
    side = str(leg.get("side")).strip().lower()
    effect = str(leg.get("position_effect")).strip().lower()
    return side, effect


def classify_strategy(legs: Sequence[Mapping[str, Any]]) -> str:
    """Assign a leg set to a lane-internal strategy class for the level gate.

    Only the defined-risk shapes the lane actually places are named:
      * a single opening buy               -> a single long option;
      * several opening buys, no sells     -> a long multi-leg (e.g. a long
                                              straddle) -- still defined risk;
      * only closing legs                  -> reducing exposure.
    ANY opening SELL leg is UNSUPPORTED and demands a level the lane never
    grants, so the level gate fails closed. This mirrors the shared defined-risk
    validator (assert_defined_risk), which refuses any sell-to-open leg outright
    until strike-aware spread support exists: without it a short cannot be shown
    to be genuinely covered, so a ratio, a type-mismatched 'cover', and a
    cross-underlying 'cover' are all as uncovered as a lone naked short. This
    function therefore never returns defined_risk_spread.
    """
    if not legs:
        return STRATEGY_UNSUPPORTED
    opening = [(_leg_role(leg)) for leg in legs]
    # FAIL-CLOSED polarity, mirroring assert_defined_risk: a SELL leg is safe ONLY
    # when provably sell-to-close. Any sell whose effect is opening, MISSING, blank,
    # or unknown is an uncovered short this lane cannot support -- matching only
    # ("sell","open") let an effect-less short fall through to STRATEGY_REDUCING.
    if any(side == "sell" and effect != "close" for side, effect in opening):
        return STRATEGY_UNSUPPORTED
    opening_buys = [role for role in opening if role == ("buy", "open")]
    any_opening = [role for role in opening if role[1] == "open"]

    if opening_buys:
        return STRATEGY_SINGLE_LEG_LONG if len(opening_buys) == 1 else STRATEGY_LONG_MULTI_LEG
    if not any_opening:
        return STRATEGY_REDUCING
    return STRATEGY_UNSUPPORTED


# --- the gates ----------------------------------------------------------------


def _max_debit_gate(config: OptionRiskConfig, proposal: OptionOrderProposal) -> GateResult:
    paid = proposal.debit_premium_usd(config.contract_multiplier)
    cap = config.max_debit_premium_per_trade_usd
    if paid > cap:
        return GateResult(
            GateName.MAX_DEBIT_PREMIUM,
            False,
            f"order pays ${paid:,.2f} in premium, over the ${cap:,.2f} per-trade debit cap",
            {"debit_premium_usd": paid, "cap_usd": cap},
        )
    return GateResult(
        GateName.MAX_DEBIT_PREMIUM,
        True,
        f"${paid:,.2f} premium within the ${cap:,.2f} per-trade debit cap",
        {"debit_premium_usd": paid, "cap_usd": cap},
    )


def _max_total_at_risk_gate(
    config: OptionRiskConfig, proposal: OptionOrderProposal, open_premium_at_risk_usd: float
) -> GateResult:
    incremental = proposal.premium_at_risk_usd(config.contract_multiplier)
    existing = max(float(open_premium_at_risk_usd), 0.0)
    total = existing + incremental
    cap = config.max_total_premium_at_risk_usd
    observed = {
        "incremental_at_risk_usd": incremental,
        "open_at_risk_usd": existing,
        "total_at_risk_usd": total,
        "cap_usd": cap,
    }
    if total > cap:
        return GateResult(
            GateName.MAX_TOTAL_PREMIUM_AT_RISK,
            False,
            f"total premium at risk would be ${total:,.2f} (${existing:,.2f} open + "
            f"${incremental:,.2f} new), over the ${cap:,.2f} cap",
            observed,
        )
    return GateResult(
        GateName.MAX_TOTAL_PREMIUM_AT_RISK,
        True,
        f"total premium at risk ${total:,.2f} within the ${cap:,.2f} cap",
        observed,
    )


def _dte_gate(config: OptionRiskConfig, proposal: OptionOrderProposal) -> GateResult:
    dte = proposal.days_to_expiry
    observed = {
        "days_to_expiry": dte,
        "min_days_to_expiry": config.min_days_to_expiry,
        "allow_zero_dte": config.allow_zero_dte,
    }
    try:
        dte_int = int(dte)
    except (TypeError, ValueError):
        return GateResult(
            GateName.MIN_DTE, False, f"days_to_expiry {dte!r} is not a number", observed
        )
    if dte_int < 0:
        return GateResult(
            GateName.EXPIRED, False, f"contract already expired ({dte_int} days to expiry)", observed
        )
    if dte_int == 0 and not config.allow_zero_dte:
        return GateResult(
            GateName.ZERO_DTE,
            False,
            "0DTE (same-day expiry) is blocked; enable options.risk.allow_zero_dte to opt in",
            observed,
        )
    if dte_int < config.min_days_to_expiry:
        return GateResult(
            GateName.MIN_DTE,
            False,
            f"{dte_int} day(s) to expiry is under the {config.min_days_to_expiry}-day floor",
            observed,
        )
    return GateResult(
        GateName.MIN_DTE,
        True,
        f"{dte_int} day(s) to expiry clears the {config.min_days_to_expiry}-day floor",
        observed,
    )


def _max_contracts_gate(config: OptionRiskConfig, proposal: OptionOrderProposal) -> GateResult:
    quantity = _as_int(proposal.quantity)
    cap = config.max_contracts_per_order
    observed = {"quantity": proposal.quantity, "max_contracts_per_order": cap}
    if quantity is None or quantity <= 0:
        return GateResult(
            GateName.INVALID_PROPOSAL,
            False,
            f"order quantity {proposal.quantity!r} is not a positive contract count",
            observed,
        )
    if quantity > cap:
        return GateResult(
            GateName.MAX_CONTRACTS,
            False,
            f"{quantity} contracts is over the {cap}-contract per-order cap",
            observed,
        )
    return GateResult(
        GateName.MAX_CONTRACTS,
        True,
        f"{quantity} contracts within the {cap}-contract per-order cap",
        observed,
    )


def _option_level_gate(config: OptionRiskConfig, strategy: str, granted_level: int | None) -> GateResult:
    required = config.min_level_for(strategy)
    observed = {"strategy": strategy, "required_level": required, "granted_level": granted_level}
    if granted_level is None:
        return GateResult(
            GateName.OPTION_APPROVAL_LEVEL,
            False,
            "the account's granted options level could not be read; refusing (fail closed)",
            observed,
        )
    if granted_level < required:
        return GateResult(
            GateName.OPTION_APPROVAL_LEVEL,
            False,
            f"strategy '{strategy}' needs options level {required}, but the account is granted "
            f"level {granted_level}",
            observed,
        )
    return GateResult(
        GateName.OPTION_APPROVAL_LEVEL,
        True,
        f"strategy '{strategy}' (needs level {required}) is within the granted level {granted_level}",
        observed,
    )


def evaluate_option_order(
    config: OptionRiskConfig,
    proposal: OptionOrderProposal,
    granted_level: int | None,
    open_premium_at_risk_usd: float = 0.0,
) -> OptionRiskDecision:
    """Run every options risk gate over one proposal and combine the verdicts.

    `granted_level` is the account's Robinhood options approval level (parse it
    from get_option_level_upgrade_info via resolve_granted_level). None means the
    level is unknown, which the level gate treats as fail-closed. The order is
    allowed only when EVERY gate passes; each blocking gate carries its own named
    reason so the refusal says exactly which cap stopped it.
    """
    strategy = classify_strategy(proposal.legs)
    results = (
        _max_debit_gate(config, proposal),
        _max_total_at_risk_gate(config, proposal, open_premium_at_risk_usd),
        _dte_gate(config, proposal),
        _max_contracts_gate(config, proposal),
        _option_level_gate(config, strategy, granted_level),
    )
    allowed = all(result.passed for result in results)
    return OptionRiskDecision(allowed=allowed, strategy=strategy, results=results)
