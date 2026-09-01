"""Safety properties of the options TRADER runtime (run_option_cycle).

The runtime's whole reason to exist is the ARM GATE the paper loop does not apply
itself. These tests pin, to FAIL if reverted:
  * DISARMED (the default / fresh-stand-up state) -> a no-op cycle, ZERO fills,
    even when a live posture is requested -- arming is the only thing that lets a
    cycle run, and OPTIONS_TRADER_LIVE cannot bypass it;
  * a broken/undecidable arm backend (DisarmedArmStore) -> no-op (fail-closed);
  * ARMED + kill switch engaged -> halted no-op, no cycle;
  * ARMED + open -> exactly ONE PAPER cycle (simulated fills), mode stays paper;
  * the ScoutPlaySource fails SAFE (empty list, never an exception) and skips a
    play it cannot price;
  * the deploy assets never set OPTIONS_TRADER_LIVE and bind no Robinhood secret.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from src.option_runtime import OptionPlay
from src.option_trader.cycle import live_requested, run_option_cycle
from src.option_trader.scout_play_source import ScoutPlaySource
from src.shared_state import DisarmedArmStore


# --- fixtures ----------------------------------------------------------------


class FakeArmStore:
    """Minimal ArmStore: is_armed('options') returns the constructed state."""

    def __init__(self, armed: bool) -> None:
        self._armed = armed

    def is_armed(self, lane: str) -> bool:
        return self._armed and lane == "options"

    def set_armed(self, lane: str, armed: bool, by: str = "operator") -> None:
        self._armed = armed


class PricedPlaySource:
    """One defined-risk long-call play that clears every option risk gate."""

    basis_name = "test_priced"

    def provenance(self):
        return {"basis": self.basis_name}

    def describe(self):
        return "test priced plays"

    def plays(self, symbols, today):
        return [
            OptionPlay(
                symbol="AAPL",
                direction="call",
                reference_close=101.0,
                entry=101.0,
                ceiling=106.0,
                floor=98.0,
                conviction=70.0,
                rank_score=1.0,
                strike=101.0,
                expiry_date="2099-01-15",
                contract_ticker="AAPL-C-1",
                premium=2.0,
            )
        ]


def write_config(root: Path) -> None:
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
            "universe": ["AAPL"],
        },
    }
    (root / "config" / "trading_rules.yaml").write_text(yaml.safe_dump(rules, sort_keys=False), encoding="utf-8")


# --- the fail-closed arm gate ------------------------------------------------


def test_disarmed_is_a_noop_zero_fills(tmp_path, monkeypatch):
    write_config(tmp_path)
    monkeypatch.setenv("TRADING_ENABLED", "true")  # even fully enabled...
    result = run_option_cycle(tmp_path, play_source=PricedPlaySource(), arm_store=FakeArmStore(armed=False))
    assert result["armed"] is False
    assert result["status"] == "disarmed_noop"
    assert result["fills"] == 0
    assert result["cycles"] == 0


def test_live_request_cannot_bypass_the_arm_gate(tmp_path, monkeypatch):
    """OPTIONS_TRADER_LIVE=1 is recorded in the posture but NEVER lets a disarmed
    lane place -- the arm gate is checked regardless of the live request."""
    write_config(tmp_path)
    monkeypatch.setenv("TRADING_ENABLED", "true")
    monkeypatch.setenv("OPTIONS_TRADER_LIVE", "1")
    result = run_option_cycle(tmp_path, play_source=PricedPlaySource(), arm_store=FakeArmStore(armed=False))
    assert result["status"] == "disarmed_noop"
    assert result["fills"] == 0
    assert result["mode"] == "live-requested-but-paper-only"


def test_disarmed_arm_store_fails_closed(tmp_path, monkeypatch):
    """A requested-but-unbuildable cloud arm backend yields DisarmedArmStore; the
    cycle must read it as DISARMED and no-op, never fall open."""
    write_config(tmp_path)
    monkeypatch.setenv("TRADING_ENABLED", "true")
    result = run_option_cycle(tmp_path, play_source=PricedPlaySource(), arm_store=DisarmedArmStore("test"))
    assert result["status"] == "disarmed_noop"
    assert result["fills"] == 0


# --- kill switch on an armed lane --------------------------------------------


def test_armed_but_env_disabled_halts(tmp_path, monkeypatch):
    write_config(tmp_path)
    monkeypatch.delenv("TRADING_ENABLED", raising=False)  # -> TRADING_ENABLED=false
    result = run_option_cycle(tmp_path, play_source=PricedPlaySource(), arm_store=FakeArmStore(armed=True))
    assert result["armed"] is True
    assert result["status"] == "halted_noop"
    assert result["fills"] == 0
    assert any("TRADING_ENABLED" in r for r in result["halt_reasons"])


def test_armed_but_stop_file_halts(tmp_path, monkeypatch):
    write_config(tmp_path)
    monkeypatch.setenv("TRADING_ENABLED", "true")
    (tmp_path / "STOP_TRADING_OPTIONS").write_text("halted by operator", encoding="utf-8")
    result = run_option_cycle(tmp_path, play_source=PricedPlaySource(), arm_store=FakeArmStore(armed=True))
    assert result["status"] == "halted_noop"
    assert result["fills"] == 0
    assert any("STOP_TRADING_OPTIONS" in r for r in result["halt_reasons"])


# --- armed + open: exactly one PAPER cycle -----------------------------------


def test_armed_and_open_runs_one_paper_cycle(tmp_path, monkeypatch):
    write_config(tmp_path)
    monkeypatch.setenv("TRADING_ENABLED", "true")
    monkeypatch.delenv("OPTIONS_TRADER_LIVE", raising=False)
    result = run_option_cycle(tmp_path, play_source=PricedPlaySource(), arm_store=FakeArmStore(armed=True))
    assert result["armed"] is True
    assert result["status"] == "paper_cycle"
    assert result["mode"] == "paper"  # paper-first: no live posture requested
    assert result["cycles"] == 1
    assert result["fills"] >= 1  # the defined-risk long filled in the paper broker
    assert result["reconcile_errors"] == []  # ledger reconciles clean vs the audit trail


# --- ScoutPlaySource: fail-safe + skips unpriced -----------------------------


def _play(**over):
    base = dict(
        symbol="AAPL",
        direction="call",
        reference_close=101.0,
        entry=101.0,
        ceiling=106.0,
        floor=98.0,
        conviction=70.0,
        rank_score=1.0,
        strike=101.0,
        expiry_date="2099-01-15",
        contract_ticker="AAPL-C-1",
        premium=2.0,
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_scout_source_skips_a_play_missing_strike_or_premium(monkeypatch):
    priced = _play()
    no_strike = _play(strike=None)
    no_premium = _play(premium=None)
    monkeypatch.setattr(
        "src.options_scout.analyzer.scout_plays",
        lambda client, config, today, now: [priced, no_strike, no_premium],
    )
    source = ScoutPlaySource(client=object(), config={})
    out = source.plays(["AAPL"], today=None)
    assert len(out) == 1  # only the fully priced play survives
    assert source._skipped == 2
    assert source.provenance()["priced"] == 1


def test_scout_source_fails_safe_to_empty_on_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("massive down")

    monkeypatch.setattr("src.options_scout.analyzer.scout_plays", boom)
    source = ScoutPlaySource(client=object(), config={})
    out = source.plays(["AAPL"], today=None)
    assert out == []  # no exception escapes -- an empty cycle is the safe failure
    assert "massive down" in (source.provenance().get("error") or "")


def test_live_requested_reads_exact_one(monkeypatch):
    monkeypatch.delenv("OPTIONS_TRADER_LIVE", raising=False)
    assert live_requested() is False
    monkeypatch.setenv("OPTIONS_TRADER_LIVE", "true")  # truthy but not "1"
    assert live_requested() is False
    monkeypatch.setenv("OPTIONS_TRADER_LIVE", "1")
    assert live_requested() is True


# --- deploy-asset safety ------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]


def test_trader_dockerfile_binds_no_live_switch():
    body = (ROOT / "Dockerfile.options-trader").read_text(encoding="utf-8")
    # No ENV directive enables a live posture (comments may reassure that none is
    # set -- assert on the actual ENV lines + the enabling value, not prose).
    env_lines = [line for line in body.splitlines() if line.strip().startswith("ENV")]
    assert all("OPTIONS_TRADER_LIVE" not in line for line in env_lines)
    assert "OPTIONS_TRADER_LIVE=1" not in body
    assert 'ENTRYPOINT ["python", "-m", "src.option_trader"]' in body


def test_trader_deploy_script_is_paper_and_fail_closed():
    body = (ROOT / "deploy" / "deploy-options-trader.sh").read_text(encoding="utf-8")
    lines = body.splitlines()
    # Never sets the live-enabling value; the env/secret DIRECTIVES (not the
    # reassuring comments) are what must stay clean.
    assert "OPTIONS_TRADER_LIVE=1" not in body
    env_directives = [line for line in lines if "--set-env-vars" in line]
    assert env_directives and all("OPTIONS_TRADER_LIVE" not in line for line in env_directives)
    # The only secret bound is the read-only Massive key -- no Robinhood credential.
    secret_directives = [line for line in lines if "--set-secrets" in line]
    assert secret_directives
    assert all("massive-api:latest" in line and "robinhood" not in line.lower() for line in secret_directives)
    # Reads the SHARED arm store and grants only what a paper reader needs.
    assert "TRADER_ARM_FIRESTORE_PROJECT=" in body
    assert "roles/datastore.user" in body
    # Kill switch held OPEN so the Firestore ARM MARKER is the effective control
    # (without it an armed lane would silently halt every cycle). Safe: paper-only
    # job -> cannot yield a live order. A removal that breaks arming fails here.
    assert "TRADING_ENABLED=true" in body
    # The project has conditional bindings -> the grant must be unconditional.
    assert "--condition=None" in body
    # A Job, bounded and non-concurrent -- never an unbounded/daemon service.
    assert "run jobs deploy" in body
    assert "--max-retries=1" in body
    assert "--parallelism=1" in body
