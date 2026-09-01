"""Robinhood OPTIONS client backed by the authorized OAuth MCP connector.

Mirrors src/robinhood_equity_client.py: account / positions / chain / quote reads,
a non-committal review path, and a place path that BUILDS a payload but reaches
the connector only under an explicit double human gate. Unlike the equities
client this lane also carries the OPTIONS-specific safety property -- DEFINED
RISK ONLY, no naked short -- enforced at construction of every order payload.

Three things make an option order irreversible-safe here, and all three sit with
the submit call inside place_option_order, never in a trusting caller:

  1. ACCOUNT. The same single agent-tradable account as the equities lane is
     resolved and pinned. The connector self-declares it with `agentic_allowed`,
     which is necessary but NOT sufficient: the flag-selected account is
     cross-checked against the out-of-band expected identity (nickname AND
     account-number suffix) loaded from config/trading_rules.yaml
     (equities.expected_account -- the SAME anchor, reused verbatim). A flipped
     flag on the off-limits default account is refused, never pinned.

  2. DOUBLE GATE + ARM. place_option_order submits ONLY when dry_run is exactly
     False AND confirm_live_order is exactly True AND the shared ArmStore reports
     the OPTIONS lane armed. Every other combination -- either flag alone, a
     truthy-but-not-True confirm, a disarmed lane -- returns the same unsubmitted
     preview and never touches the connector. This matches the lane's hard rule:
     a real order requires BOTH confirm_live=True AND the options lane armed.

  3. DEFINED RISK. Every order payload is validated before it is built by the
     SHARED coverage-aware validator (assert_defined_risk, used verbatim by the
     broker too). Proving a short leg is genuinely covered -- same underlying,
     matching option type, a bounded strike width -- needs strike-aware spread
     support this lane does not yet have, and a presence-only "there is also a
     buy leg" check cannot tell a real vertical from a ratio, a call 'covered'
     by a put, or a short of one underlying 'covered' by a long of another. So
     until that support lands, the ONLY provably-defined-risk opening shape is a
     long (buy-to-open) leg, and ANY sell-to-open leg is refused outright at
     build time -- before the payload exists -- so an uncovered short can never
     reach the connector or the static order-safety guard. The automated path
     emits single-leg longs only, so this loses no real capability.

There is no API key and no base URL: the connector object IS the credential
(session-bound OAuth), and this class only ever calls its named tool methods.
"""

from __future__ import annotations

import json
import math
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

import yaml

# The options-lane risk caps (debit / total-at-risk / DTE / contracts / approval
# level). The place path runs these BEFORE the connector call, so a fully
# gated + armed submit still cannot push a 0DTE / over-contract / over-debit /
# over-level order past the caps -- the runtime mirror of what the strategy
# mapper already gates at framing time.
from .option_risk_gates import (
    GateName,
    OptionOrderProposal,
    OptionRiskConfig,
    caps_direction,
    evaluate_option_order,
    resolve_granted_level,
)

# The options-lane emergency stop. place_option_order re-reads it at the
# irreversible moment (STOP_TRADING_OPTIONS + TRADING_ENABLED), mirroring the
# broker -- a direct client submit must honor the kill switch too, not only a
# submit routed through the broker.
from .kill_switch import KillSwitch

# Reuse the equities lane's account-identity anchor verbatim -- SAME account,
# SAME out-of-band expected identity, SAME exceptions. The options lane must not
# fork the anchor: a second copy is a second thing to keep in sync.
from .robinhood_equity_client import (
    _DEFAULT_ROOT,
    AgentAccountIdentityError,
    AgentAccountMismatchError,
    NoAgentTradableAccountError,
    _load_expected_account,
)

# The lane this client's arm gate reads from the shared ArmStore.
OPTIONS_LANE = "options"

# The lane repo root, so a client built with no explicit kill switch resolves its
# fail-closed default to the options lane's OWN ROOT-anchored stop file regardless
# of the process CWD. Same anchor the broker uses (src/ -> agent root).
_ROOT = _DEFAULT_ROOT


def _default_options_kill_switch() -> KillSwitch:
    """Fail-closed default for a client built without an explicit kill switch.

    A missing kill switch must NEVER turn the client's submit path into an
    unguarded one. Fall back to the options lane's OWN ROOT-anchored switch --
    STOP_TRADING_OPTIONS plus TRADING_ENABLED -- so the emergency stop is honored
    on a direct client submit exactly as it is via the broker. Never the crypto
    lane's STOP_TRADING or the equities lane's STOP_TRADING_EQUITIES."""
    return KillSwitch(stop_file=str(_ROOT / "STOP_TRADING_OPTIONS"), env_var="TRADING_ENABLED")


def _as_position_list(payload: Any) -> list[Any]:
    """Normalize a connector get_option_positions payload to a list of positions,
    across the loosely-known shapes it may take (a bare list, or a mapping under
    positions / results / option_positions / data). Shared with the broker so the
    standing-at-risk sourcing reads positions identically on both paths."""
    if isinstance(payload, Mapping):
        for key in ("positions", "results", "option_positions", "data"):
            value = payload.get(key)
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                return list(value)
        return []
    if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes)):
        return list(payload)
    return []


def _first_float(mapping: Mapping[str, Any], keys: Sequence[str]) -> float | None:
    """The first of `keys` present in `mapping` with a numeric value, as a float,
    or None when none is readable. Shared with the broker."""
    for key in keys:
        if key in mapping and mapping[key] is not None:
            try:
                return float(mapping[key])
            except (TypeError, ValueError):
                continue
    return None


def _effective_max_loss_per_contract(
    legs: Sequence[Mapping[str, Any]],
    premium_per_share: float,
    multiplier: int,
    caller_max_loss_per_contract_usd: float | None,
) -> float | None:
    """The per-contract max loss the total-at-risk cap must see.

    Any order carrying a BUY leg PAYS premium, so its per-contract max loss is at
    least the debit paid (premium-per-share x contract multiplier). A caller
    max_loss_per_contract_usd LOWER than that debit -- or None -- is ignored in
    favour of the debit, so a tiny caller figure cannot under-count the cap. An
    all-sell (sell-to-close reducing) order carries no opening debit, so its
    caller figure is passed through unchanged. Shared by the client and the broker
    so both caps paths derive at-risk identically."""
    has_buy = any(
        isinstance(leg, Mapping) and str(leg.get("side")).strip().lower() == "buy" for leg in (legs or [])
    )
    if not has_buy:
        return caller_max_loss_per_contract_usd
    debit_per_contract = max(float(premium_per_share), 0.0) * multiplier
    if caller_max_loss_per_contract_usd is None:
        return debit_per_contract
    try:
        return max(float(caller_max_loss_per_contract_usd), debit_per_contract)
    except (TypeError, ValueError):
        return debit_per_contract


# Connector-response statuses that signal the venue ACCEPTED the order, and those
# that signal an explicit rejection. place_option_order reports submitted=True only
# after seeing an acceptance signal (an order id or an accepting status) in the
# response -- never merely because the connector call did not raise.
_ACCEPTED_STATUSES = frozenset(
    {"accepted", "confirmed", "queued", "filled", "partially_filled", "pending", "new", "submitted", "ok", "placed"}
)
_REJECTED_STATUSES = frozenset({"rejected", "cancelled", "canceled", "failed", "denied", "error", "unconfirmed"})


def _response_accepted(response: Any) -> bool:
    """True only when the connector's place response carries an acceptance signal.

    An acceptance is an explicit accepting status/state, or an order id the venue
    minted for the order. An explicit rejecting status wins over everything. A
    non-mapping response, an empty mapping, or one with neither an id nor an
    accepting status is NOT an acceptance -- reporting submitted=True on it would
    be a phantom fill (a claimed order the venue never acknowledged)."""
    if not isinstance(response, Mapping):
        return False
    for key in ("status", "state", "order_status"):
        value = response.get(key)
        if value is not None:
            token = str(value).strip().lower()
            if token in _REJECTED_STATUSES:
                return False
            if token in _ACCEPTED_STATUSES:
                return True
    for key in ("id", "order_id"):
        value = response.get(key)
        if value is not None and str(value).strip():
            return True
    return False

# Re-exported so callers can catch them from this module too.
__all__ = [
    "AgentAccountIdentityError",
    "AgentAccountMismatchError",
    "NoAgentTradableAccountError",
    "DefinedRiskViolationError",
    "OptionConnector",
    "RobinhoodOptionClient",
    "assert_defined_risk",
    "OPTIONS_LANE",
]


class OptionConnector(Protocol):
    """Duck-typed shape of the authorized Robinhood OPTIONS MCP connector.

    A test satisfies this with a plain stub that records calls; nothing here is
    an HTTP client and there is no key to hold.
    """

    def get_option_chains(self, **kwargs: Any) -> Any: ...
    def get_option_quotes(self, **kwargs: Any) -> Any: ...
    def get_option_positions(self, account_number: str | None = None) -> Any: ...
    def get_option_level_upgrade_info(self, **kwargs: Any) -> Any: ...
    def get_accounts(self) -> Any: ...
    def review_option_order(self, **kwargs: Any) -> Any: ...
    def place_option_order(self, **kwargs: Any) -> Any: ...
    def cancel_option_order(self, order_id: str, account_number: str | None = None) -> Any: ...


class _ArmStore(Protocol):
    def is_armed(self, lane: str) -> bool: ...


class DefinedRiskViolationError(RuntimeError):
    """An order payload would open an uncovered / undefined-risk SELL leg.

    DEFINED RISK is the lane's floor. This is raised at BUILD time, before the
    payload exists, so an undefined-risk order can never be handed to the
    connector or reach the static order-safety guard as a literal.
    """


def assert_defined_risk(legs: Sequence[Mapping[str, Any]], direction: str) -> None:
    """The ONE shared coverage-aware defined-risk validator, used verbatim by
    BOTH the client and the broker so there is no second copy to drift.

    Proving a short leg is genuinely COVERED -- a long of the same underlying,
    matching option type, and a bounded strike width -- needs the strike /
    underlying / expiry-aware spread support this lane does not have yet. A
    presence-only "there is also some buy leg" check (what the earlier version
    did) cannot tell a real 1:1 vertical from:

      * a RATIO (buy 1 call, sell 3 calls) -- the extra shorts are uncovered;
      * a short call 'covered' by a long PUT -- the put does not cover the call;
      * a short of one underlying 'covered' by a long of ANOTHER -- unrelated.

    Each of those is undefined risk, and each passed the old check and reached
    the live connector. Until real strike-aware spread support exists, the ONLY
    provably-defined-risk opening shape is a long (buy-to-open) leg, so ANY
    sell-to-open leg is refused outright here. The automated path emits
    single-leg longs only, so this closes the hole without losing capability.

    A sell-to-CLOSE leg (exiting a held long) is NOT an opening short and is
    allowed -- refusing it would trap open positions. `direction` is accepted
    for signature parity with the caller and to keep the refusal auditable.
    """
    if not legs:
        raise DefinedRiskViolationError("refusing an option order with no legs")
    for leg in legs:
        side = str(leg.get("side")).strip().lower()
        effect = str(leg.get("position_effect")).strip().lower()
        # FAIL-CLOSED polarity: a SELL leg is permitted ONLY when it is provably
        # sell-to-CLOSE (exiting a held long). Any sell whose position_effect is
        # opening, MISSING, blank, or unknown ("none") is refused -- checking only
        # for effect=="open" let a naked short with an absent effect slip through.
        # This lane places no opening short until strike-aware spread support exists.
        if side == "sell" and effect != "close":
            raise DefinedRiskViolationError(
                "refusing a sell leg that is not provably sell-to-close "
                f"(side={side!r}, position_effect={effect!r}, direction="
                f"{str(direction).strip().lower()!r}): this lane is defined-risk only and "
                "places no opening short, so any such sell is an uncovered short -- naked, "
                "a ratio, or a mismatched 'cover' -- and is refused"
            )


# The ONLY keys the real Robinhood options MCP order schema accepts on a leg
# (additionalProperties:false). The build path retains extra contract-identifying
# fields (option_type / underlying / strike / expiration) for the caps/DTE math
# and a future coverage check, but the connector REJECTS any key outside this
# set, so the leg handed to the wire is projected down to exactly these.
_CONNECTOR_LEG_KEYS = ("option_id", "side", "position_effect", "ratio_quantity")


def _connector_leg(leg: Mapping[str, Any]) -> dict[str, Any]:
    """Project one normalized leg down to only the connector's allowed key set."""
    return {key: leg[key] for key in _CONNECTOR_LEG_KEYS if key in leg}


# A fixed namespace so the derived ref_id is a stable function of the order's
# content (and of nothing else -- no wall clock, no randomness). Two processes
# building the SAME order therefore mint the SAME key.
_REF_ID_NAMESPACE = uuid.UUID("6f2a9d3e-8b41-5c76-9a0e-4d7f1c2b3e58")


def _deterministic_ref_id(payload: Mapping[str, Any]) -> str:
    """A stable client-order-id (`ref_id`) derived ONLY from the order's
    identifying content, so a RETRY of the same logical order carries the SAME
    key and the venue de-dupes it instead of double-filling. Any change to the
    account, legs, quantity, type, price, time-in-force, or direction yields a
    different key, so distinct orders never collide.

    Derived from the connector-facing leg projection (the same keys the wire
    carries), so it does not shift when the extra contract-identifying fields are
    stripped at the wire."""
    material = json.dumps(
        {
            "account_number": payload.get("account_number"),
            "direction": payload.get("direction"),
            "legs": [_connector_leg(leg) for leg in payload.get("legs", [])],
            "quantity": payload.get("quantity"),
            "type": payload.get("type"),
            "time_in_force": payload.get("time_in_force"),
            "price": payload.get("price"),
        },
        sort_keys=True,
        default=str,
    )
    return str(uuid.uuid5(_REF_ID_NAMESPACE, material))


def _positive_limit_price(price: Any) -> float | None:
    """A limit price coerced to a positive, finite float, or None when it is
    missing, non-numeric, non-finite, or <= 0.

    A live limit order priced at None / 0 / a negative / a non-number makes BOTH
    dollar caps read $0 of new risk -- the debit cap sees max(premium, 0) = 0 and
    the incremental at-risk figure is likewise 0 -- so neither cap can bind. Such
    an order must be REFUSED at the irreversible submit rather than priced as
    free; this is the predicate the submit path checks before it will place.
    """
    try:
        value = float(price)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return value


def _load_rules(config_root: Path | str | None) -> dict[str, Any]:
    """Parse config/trading_rules.yaml (for the options risk caps), or {} when it
    cannot be read -- a missing config yields the conservative defaults baked into
    OptionRiskConfig, never a permissive blank."""
    base = Path(config_root) if config_root is not None else _DEFAULT_ROOT
    path = base / "config" / "trading_rules.yaml"
    try:
        with path.open("r", encoding="utf-8") as handle:
            rules = yaml.safe_load(handle) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return rules if isinstance(rules, dict) else {}


def _dte_from_legs(legs: Sequence[Mapping[str, Any]]) -> int | None:
    """Calendar days to the nearest expiry stamped on a leg, or None when no leg
    carries a readable expiry. Lets the DTE cap bind on an order that names its
    expiry on the leg even when the caller passes no explicit days_to_expiry."""
    today = datetime.now(UTC).date()
    for leg in legs or []:
        if not isinstance(leg, Mapping):
            continue
        for key in ("expiration_date", "expiry", "expiry_date", "expiration"):
            raw = leg.get(key)
            if not raw:
                continue
            try:
                expiry = date.fromisoformat(str(raw)[:10])
            except (TypeError, ValueError):
                continue
            return (expiry - today).days
    return None


class RobinhoodOptionClient:
    """Robinhood options client over the authorized OAuth MCP connector.

    Resolves and pins the single agent-tradable account on construction, exposes
    read-only chain/quote/position/account tools, and gates every order path on
    the pinned account plus -- for a live submit -- the double confirm/arm gate.
    """

    def __init__(
        self,
        connector: OptionConnector,
        arm_store: _ArmStore | None = None,
        lane: str = OPTIONS_LANE,
        expected_account: Mapping[str, str] | None = None,
        config_root: Path | str | None = None,
        kill_switch: KillSwitch | None = None,
    ) -> None:
        self._connector = connector
        # Fail closed: a client built with no kill switch still gets the options
        # lane's own ROOT-anchored switch, so a direct submit re-checks the
        # emergency stop at the irreversible moment exactly as the broker does.
        self._kill_switch = kill_switch if kill_switch is not None else _default_options_kill_switch()
        # A missing arm store reads as DISARMED (fail safe): a real submit then
        # can never fire, exactly as a disarmed lane. The store is the shared
        # ArmStore (src.shared_state.build_arm_store) in production.
        self._arm_store = arm_store
        # Pin the arm-gate lane to the POSITIVE, fail-closed options marker. Any
        # other lane (crypto/equities) is arm-tracked by ABSENCE of a stop file,
        # which reads ARMED by default -- consulting one from the leveraged options
        # client would silently restore the exact fail-OPEN semantics the positive
        # marker exists to close. There is no legitimate non-options lane here.
        if lane != OPTIONS_LANE:
            raise ValueError(
                f"RobinhoodOptionClient arm lane must be {OPTIONS_LANE!r} (the positive, "
                f"fail-closed marker); {lane!r} is absence-armed and would fail OPEN"
            )
        self._lane = lane
        self._config_root = config_root
        # The options risk caps, read from config lazily (only a genuine submit
        # needs them) and cached. Defaults are conservative when config is absent.
        self._cached_risk_config: OptionRiskConfig | None = None
        self._expected_account = (
            dict(expected_account) if expected_account is not None else _load_expected_account(config_root)
        )
        self.account = self._resolve_agent_account()
        self.account_number = self.account["account_number"]
        self.nickname = self.account.get("nickname")

    # --- account resolution (identical anchor to the equities client) ---------

    def _resolve_agent_account(self) -> dict[str, Any]:
        payload = self._connector.get_accounts()
        if isinstance(payload, dict):
            accounts = payload.get("accounts", payload.get("results", []))
        else:
            accounts = payload or []
        agentic = [account for account in accounts if account.get("agentic_allowed")]
        if len(agentic) != 1:
            raise NoAgentTradableAccountError(
                f"connector reports {len(agentic)} agentic-allowed accounts; expected exactly 1"
            )
        account = agentic[0]
        self._assert_expected_identity(account)
        return account

    def _assert_expected_identity(self, account: Mapping[str, Any]) -> None:
        expected_nickname = self._expected_account["nickname"]
        expected_suffix = self._expected_account["number_suffix"]
        nickname = account.get("nickname")
        number = str(account.get("account_number", ""))
        if nickname != expected_nickname or not number.endswith(expected_suffix):
            raise AgentAccountIdentityError(
                f"agentic_allowed account {number!r} (nickname {nickname!r}) does not match the "
                f"expected agent identity (nickname {expected_nickname!r}, number ending {expected_suffix!r}); "
                "the self-declared agentic_allowed flag is necessary but not sufficient"
            )

    def _assert_agent_account(self, account_number: str | None) -> str:
        target = account_number or self.account_number
        if target != self.account_number:
            raise AgentAccountMismatchError(
                f"refusing option order for account {target!r}; the agent may only trade {self.account_number!r}"
            )
        return target

    def _is_options_lane_armed(self) -> bool:
        """True only when a shared ArmStore reports THIS lane armed. No store,
        or a store that fails to answer, reads as disarmed (fail safe)."""
        store = self._arm_store
        if store is None:
            return False
        try:
            return bool(store.is_armed(self._lane))
        except Exception:
            return False

    def _risk_config(self) -> OptionRiskConfig:
        """The options risk caps, loaded from config once and cached."""
        if self._cached_risk_config is None:
            self._cached_risk_config = OptionRiskConfig.from_rules(_load_rules(self._config_root))
        return self._cached_risk_config

    def _kill_switch_halt_reason(self) -> str | None:
        """The reason the options kill switch is engaged, or None when it is open.

        Re-read at the irreversible moment, never trusted from a caller. Fail
        CLOSED: an unreadable switch reads as HALTED, so a broken emergency stop
        can never leave the submit path unguarded."""
        try:
            halts = self._kill_switch.halt_reasons()
        except Exception:
            return "kill switch is unreadable (fail closed)"
        if halts:
            return "kill switch is engaged: " + "; ".join(halts)
        return None

    def _open_premium_at_risk_from_positions(self) -> float:
        """Premium currently at risk in OPEN option positions, in dollars, sourced
        from the connector -- mirrors the broker's method so a DIRECT client submit
        cannot under-count the portfolio total-at-risk cap by defaulting the
        standing exposure to 0.0.

        For each held position the standing risk is its RECORDED max loss when the
        connector reports one, else premium x contract-multiplier x contracts
        (every position this lane opens is a long, whose max loss is the debit
        paid). Best-effort and fail-soft on the READ: an unreadable positions
        payload contributes 0.0 rather than crashing the submit."""
        try:
            raw = self.get_option_positions()
        except Exception:
            return 0.0
        multiplier = self._risk_config().contract_multiplier
        total = 0.0
        for position in _as_position_list(raw):
            if not isinstance(position, Mapping):
                continue
            qty = _first_float(position, ("quantity", "contracts", "open_quantity", "long_quantity"))
            if qty is None or qty <= 0:
                continue
            max_loss = _first_float(position, ("max_loss_usd", "max_loss", "recorded_max_loss"))
            if max_loss is not None and max_loss > 0:
                total += max_loss
                continue
            premium = _first_float(
                position, ("average_open_price", "average_price", "average_buy_price", "price", "premium")
            )
            if premium is None or premium <= 0:
                continue
            total += premium * multiplier * qty
        return round(total, 2)

    def _option_caps_block(
        self,
        legs: Sequence[Mapping[str, Any]],
        direction: str,
        quantity: str,
        price: str | None,
        days_to_expiry: int | None,
        open_premium_at_risk_usd: float,
        max_loss_per_contract_usd: float | None,
    ) -> str | None:
        """Run the options risk caps over this order; return a single blocking
        reason string, or None when every applicable cap passes.

        The debit / total-at-risk / contract-count / approval-level caps are
        ALWAYS enforced here, immediately before the connector call. The DTE floor
        binds whenever the expiry is known (an explicit days_to_expiry or one
        stamped on a leg). When the expiry is genuinely UNKNOWN -- neither supplied
        nor stamped on a leg -- a live order is REFUSED (fail closed) rather than
        having the DTE gates stripped, so a known 0DTE / short-dated long cannot
        slip through the manual place/build helpers by simply omitting the DTE.
        """
        config = self._risk_config()
        dte = days_to_expiry if days_to_expiry is not None else _dte_from_legs(legs)
        if dte is None:
            return (
                f"[{GateName.MIN_DTE}] a live option order requires a known days-to-expiry "
                "(an explicit days_to_expiry or a leg-stamped expiration); refusing (fail closed)"
            )
        try:
            qty_int = int(quantity)
        except (TypeError, ValueError):
            qty_int = 0
        try:
            premium = float(price) if price is not None else 0.0
        except (TypeError, ValueError):
            premium = 0.0
        proposal = OptionOrderProposal(
            legs=legs,
            net_premium_per_contract=premium,
            quantity=qty_int,
            days_to_expiry=dte,
            # Cap under a direction derived from the leg polarity, never trusted
            # from the caller's label: a 'credit' tag on an order that BUYS a leg
            # would otherwise void the per-trade debit cap (debit_premium_usd
            # returns $0 for credit), letting a long submit at up to the looser
            # total-at-risk cap. At the submit path defined risk has already
            # refused every opening short, so any buy leg here is a real debit.
            direction=caps_direction(legs, direction),
            # LONG AT-RISK FROM DEBIT: any order carrying a BUY leg is a real debit
            # whose per-contract max loss IS the debit paid (premium x multiplier).
            # Ignore a caller max_loss_per_contract_usd LOWER than that, so a tiny
            # caller figure cannot under-count the total-at-risk cap. Defined risk
            # has already refused every opening short, so no genuine credit spread
            # (whose max loss is width-minus-credit, below the debit) reaches here.
            max_loss_per_contract_usd=_effective_max_loss_per_contract(
                legs, premium, config.contract_multiplier, max_loss_per_contract_usd
            ),
        )
        decision = evaluate_option_order(
            config, proposal, resolve_granted_level(self), max(float(open_premium_at_risk_usd), 0.0)
        )
        blocking = decision.blocking
        if blocking:
            return "; ".join(f"[{result.name}] {result.reason}" for result in blocking)
        return None

    # --- reads ----------------------------------------------------------------

    def get_option_chains(self, symbol: str, **kwargs: Any) -> Any:
        return self._connector.get_option_chains(symbol=symbol, **kwargs)

    def get_option_quotes(self, *contract_ids: str, **kwargs: Any) -> Any:
        # The Robinhood options MCP tool names this argument `instrument_ids`
        # (option instrument UUIDs), NOT `ids`. A wrong key silently returned no
        # quotes against a permissive stub; grounded against the real schema here.
        return self._connector.get_option_quotes(instrument_ids=list(contract_ids), **kwargs)

    def get_option_positions(self) -> Any:
        return self._connector.get_option_positions(account_number=self.account_number)

    def get_option_level_upgrade_info(self, **kwargs: Any) -> Any:
        return self._connector.get_option_level_upgrade_info(**kwargs)

    def get_accounts(self) -> Any:
        return self._connector.get_accounts()

    def get_account(self) -> dict[str, Any]:
        """The resolved, pinned agent-tradable account (cached from init)."""
        return self.account

    # --- leg / payload construction (DEFINED RISK ONLY) -----------------------

    @staticmethod
    def _normalize_leg(leg: Mapping[str, Any]) -> dict[str, Any]:
        """Normalize one leg to the connector's shape, lower-casing the two
        classifying fields so the defined-risk check reads them uniformly, and
        RETAINING the contract-identifying fields (option_type / underlying /
        expiration / strike) untouched when present.

        The contract reference is emitted under the key the real Robinhood options
        MCP order schema requires -- `option_id` (the option instrument UUID) --
        regardless of which alias the caller supplied (option / option_id /
        instrument / contract_ticker). The earlier version emitted `option`, a key
        the connector does not read, so a fully gated live order would have been
        rejected (or silently mis-filled) at the venue; grounded against the real
        schema here.

        The earlier version also DROPPED the identifying fields, which is
        precisely what made every short look 'covered' by any long: with no
        underlying, type or strike to compare, a coverage check has nothing to
        reason over. They are carried through here so a future strike-aware
        validator can prove real coverage -- and so the payload the connector
        receives is faithful."""
        side = leg.get("side")
        effect = leg.get("position_effect")
        normalized: dict[str, Any] = {
            "side": str(side).strip().lower() if side is not None else side,
            "position_effect": str(effect).strip().lower() if effect is not None else effect,
            "ratio_quantity": leg.get("ratio_quantity", 1),
        }
        # The contract reference under whichever alias the caller supplied, always
        # re-keyed to the schema's `option_id`.
        for key in ("option_id", "option", "instrument", "contract_ticker"):
            if key in leg and leg[key] is not None:
                normalized["option_id"] = leg[key]
                break
        # Retain the contract-identifying fields, untouched, when the caller
        # supplied them -- absent fields are simply not carried.
        for key in (
            "option_type",
            "underlying",
            "underlying_symbol",
            "expiration_date",
            "expiry",
            "strike_price",
            "strike",
        ):
            if key in leg and leg[key] is not None:
                normalized[key] = leg[key]
        return normalized

    def _assert_defined_risk(self, legs: Sequence[Mapping[str, Any]], direction: str) -> None:
        """Refuse any leg set that is not provably defined-risk, via the ONE
        shared validator (module-level assert_defined_risk) the broker also
        uses -- there is no second, drifting copy of the coverage rule."""
        assert_defined_risk(legs, direction)

    def build_option_order(
        self,
        legs: Sequence[Mapping[str, Any]],
        direction: str = "debit",
        quantity: str = "1",
        order_type: str = "limit",
        price: str | None = None,
        time_in_force: str = "gtc",
        account_number: str | None = None,
        ref_id: str | None = None,
    ) -> dict[str, Any]:
        """Validate + assemble an option order payload WITHOUT submitting it.

        This is the review/build path. It pins the account, refuses an
        undefined-risk leg set, and returns the connector-shaped payload. Callers
        that only want a preview use this or review_order; nothing here can reach
        place_option_order.

        The payload carries a `ref_id` idempotency key: an explicit one when the
        caller supplies it, otherwise one derived deterministically from the
        order's content. A retried submit therefore reuses the SAME key, so the
        venue de-dupes it and a retry cannot double-submit.
        """
        target = self._assert_agent_account(account_number)
        if not legs:
            raise DefinedRiskViolationError("refusing an option order with no legs")
        normalized = [self._normalize_leg(leg) for leg in legs]
        self._assert_defined_risk(normalized, direction)
        payload: dict[str, Any] = {
            "account_number": target,
            "direction": str(direction).strip().lower(),
            "legs": normalized,
            "quantity": str(quantity),
            "type": order_type,
            "time_in_force": time_in_force,
        }
        if price is not None:
            payload["price"] = str(price)
        payload["ref_id"] = str(ref_id) if ref_id is not None else _deterministic_ref_id(payload)
        return payload

    def build_single_leg_long(
        self,
        contract: str,
        quantity: str = "1",
        order_type: str = "limit",
        price: str | None = None,
        time_in_force: str = "gtc",
        account_number: str | None = None,
    ) -> dict[str, Any]:
        """The scout's bread-and-butter play: BUY-to-open a single call or put.

        A long option is defined-risk by construction (max loss = premium paid),
        so this is the safest order the lane places. The direction is a debit and
        the one leg is a buy-to-open; no short risk is opened.
        """
        long_leg = {"side": "buy", "position_effect": "open", "ratio_quantity": 1, "option": contract}
        return self.build_option_order(
            legs=[long_leg],
            direction="debit",
            quantity=quantity,
            order_type=order_type,
            price=price,
            time_in_force=time_in_force,
            account_number=account_number,
        )

    # --- review (non-committal, always reaches the connector) -----------------

    def review_order(
        self,
        legs: Sequence[Mapping[str, Any]],
        direction: str = "debit",
        quantity: str = "1",
        order_type: str = "limit",
        price: str | None = None,
        time_in_force: str = "gtc",
        account_number: str | None = None,
    ) -> Any:
        """Ask the connector to PREVIEW an order. Robinhood's review step is
        itself non-committal, so this always reaches review_option_order -- the
        human gate below applies to place_order, not to a preview. The payload
        is still built through the defined-risk validator first."""
        payload = self.build_option_order(
            legs, direction, quantity, order_type, price, time_in_force, account_number
        )
        return self._connector.review_option_order(**self._wire_payload(payload))

    @staticmethod
    def _wire_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
        """The payload actually handed to the connector: a copy of the built
        payload with each leg stripped to the connector's allowed key set. The
        built payload keeps the extra contract-identifying fields for the caps /
        DTE math and a future coverage check; the connector's schema forbids
        them, so only here, at the wire, are the legs projected down.

        `direction` (debit/credit) is a LANE-INTERNAL hint the caps/DTE math read,
        not a field the connector's single-leg order schema takes -- a single
        buy-to-open long is unambiguously a debit -- so it is dropped from the wire
        for a single-leg order. It is retained for a multi-leg order, where a
        spread's net direction is meaningful. `ref_id` (the idempotency key) is
        carried through unchanged so a retry de-dupes at the venue."""
        wire = dict(payload)
        legs = [_connector_leg(leg) for leg in payload.get("legs", [])]
        wire["legs"] = legs
        if len(legs) == 1:
            wire.pop("direction", None)
        return wire

    # --- place (double gate + arm; the only path that can submit) -------------

    def place_option_order(
        self,
        legs: Sequence[Mapping[str, Any]],
        direction: str = "debit",
        quantity: str = "1",
        order_type: str = "limit",
        price: str | None = None,
        time_in_force: str = "gtc",
        account_number: str | None = None,
        dry_run: bool = True,
        confirm_live_order: bool = False,
        days_to_expiry: int | None = None,
        open_premium_at_risk_usd: float = 0.0,
        max_loss_per_contract_usd: float | None = None,
        ref_id: str | None = None,
    ) -> dict[str, Any]:
        """Build a DEFINED-RISK option order; submit it only on the full gate.

        READ-ONLY is the default posture. The connector's place_option_order is
        reached ONLY when dry_run is exactly False AND confirm_live_order is
        exactly True AND the shared ArmStore reports the options lane armed AND
        the order clears the options risk caps (debit / total-at-risk / DTE /
        contracts / approval level). Every other combination returns an
        unsubmitted preview.

        Identity, not truthiness: `dry_run is False and confirm_live_order is
        True` -- a truthy non-boolean confirm ("yes") or a falsy non-boolean
        dry_run (0) must NOT arm the lane.
        """
        # Build (and defined-risk-validate) the payload first -- a naked short is
        # refused here, before any gate is even consulted. The payload carries a
        # stable ref_id (idempotency key) so a retried submit de-dupes at the venue.
        payload = self.build_option_order(
            legs, direction, quantity, order_type, price, time_in_force, account_number, ref_id=ref_id
        )
        if not (dry_run is False and confirm_live_order is True):
            return {
                "submitted": False,
                "status": "dry_run_order_preview",
                "venue": "robinhood_options",
                "order_payload": payload,
            }
        # Second, independent fact: the options lane must be ARMED. Kept beside
        # the irreversible call, never trusted from a caller. A disarmed (or
        # absent) store returns the same preview -- no connector call.
        if not self._is_options_lane_armed():
            return {
                "submitted": False,
                "status": "options_lane_disarmed",
                "venue": "robinhood_options",
                "order_payload": payload,
            }
        # Well-formed price, beside the irreversible call: a live limit order with
        # a None / non-numeric / <= 0 price makes both dollar caps read $0 of new
        # risk, voiding them at submit. Refuse it -- never place an unpriced order.
        if _positive_limit_price(price) is None:
            return {
                "submitted": False,
                "status": "invalid_limit_price",
                "venue": "robinhood_options",
                "order_payload": payload,
            }
        # Standing exposure, sourced at the irreversible moment (mirrors the
        # broker): the premium already at risk in open option positions, so a
        # DIRECT client submit cannot under-count the portfolio total-at-risk cap
        # by passing the 0.0 default. max() with any caller-supplied figure keeps
        # the cap from being reset downward and avoids double-counting.
        effective_open_at_risk = max(
            float(open_premium_at_risk_usd or 0.0), self._open_premium_at_risk_from_positions()
        )
        # Third fact, mirrored from the broker (defense in depth): the order must
        # clear the options risk caps. A cap-blocking order -- 0DTE, an unknown
        # DTE, over the contract count, over the debit or total-at-risk cap, or
        # above the granted approval level -- returns a preview and never reaches
        # the connector, even fully gated and armed.
        caps_block = self._option_caps_block(
            payload["legs"],
            payload["direction"],
            quantity,
            price,
            days_to_expiry,
            effective_open_at_risk,
            max_loss_per_contract_usd,
        )
        if caps_block is not None:
            return {
                "submitted": False,
                "status": "options_risk_gate_blocked",
                "venue": "robinhood_options",
                "order_payload": payload,
                "risk_gate": caps_block,
            }
        # Past this point the next call is irreversible: re-read the options kill
        # switch (STOP_TRADING_OPTIONS + TRADING_ENABLED), mirroring the broker. A
        # halted (or unreadable) switch returns a preview -- no connector call --
        # so a direct client submit honors the emergency stop too.
        halt = self._kill_switch_halt_reason()
        if halt is not None:
            return {
                "submitted": False,
                "status": "kill_switch_engaged",
                "venue": "robinhood_options",
                "order_payload": payload,
                "kill_switch": halt,
            }
        response = self._connector.place_option_order(**self._wire_payload(payload))
        # Report submitted=True ONLY after inspecting the connector response for an
        # acceptance signal (an order id / accepting status). A call that merely
        # did not raise but returned nothing venue-acknowledged is NOT a fill --
        # claiming submitted=True on it would be a phantom order. Otherwise carry
        # the preview shape back with the raw response for the caller to inspect.
        if not _response_accepted(response):
            return {
                "submitted": False,
                "status": "order_not_accepted",
                "venue": "robinhood_options",
                "order_payload": payload,
                "response": response,
            }
        return {
            "submitted": True,
            "status": "submitted",
            "venue": "robinhood_options",
            "order_payload": payload,
            "response": response,
        }

    def cancel_order(
        self,
        order_id: str,
        account_number: str | None = None,
        dry_run: bool = True,
        confirm_live_order: bool = False,
    ) -> Any:
        """Cancel an open option order. Cancelling REDUCES exposure, so it is not
        held to the ARM fact (requiring the lane armed to cancel would lock open
        orders in when disarmed -- the opposite of a kill switch). It still
        passes the same dry_run/confirm gate at runtime."""
        target = self._assert_agent_account(account_number)
        if not (dry_run is False and confirm_live_order is True):
            return {"id": order_id, "account_number": target, "status": "dry_run_cancel_prepared"}
        return self._connector.cancel_option_order(order_id=order_id, account_number=target)
