"""Build-time safety properties of the deployment assets.

These are cheap string assertions on purpose. They exist so that a change that
would quietly convert a gated, finite live run into an unsupervised continuous
one fails the suite instead of reaching a VM.

They also gate the live loop itself: run_project_tests() runs the whole suite
before every gated live launch, so a deploy asset edited into an unsafe shape
refuses the run rather than trading under it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
LOOP_SERVICE = DEPLOY / "solomon-trader-loop.service"
LOOP_TIMER = DEPLOY / "solomon-trader-loop.timer"
DASHBOARD_SERVICE = DEPLOY / "solomon-trader-dashboard.service"
STARTUP = DEPLOY / "startup.sh"


def _exec_start(unit: Path) -> str:
    for line in unit.read_text(encoding="utf-8").splitlines():
        if line.startswith("ExecStart="):
            return line.split("=", 1)[1]
    raise AssertionError(f"{unit.name} has no ExecStart")


@pytest.mark.parametrize("unit", [LOOP_SERVICE, LOOP_TIMER, DASHBOARD_SERVICE, STARTUP])
def test_deploy_asset_exists(unit: Path) -> None:
    assert unit.is_file(), f"{unit.name} is missing"


def test_loop_unit_never_invokes_the_ungated_live_command() -> None:
    """`run-live` loops forever with no gate function and no test run.

    Only `run-live-loop` consults _bounded_live_gate_reasons /
    _restricted_unattended_live_reasons and re-runs pytest. If `run-live` ever
    appears in a unit file, the entire gate stack is bypassed on the VM.
    """
    command = _exec_start(LOOP_SERVICE)
    assert not re.search(r"\brun-live(?!-loop)\b", command), (
        "loop unit must not invoke the ungated run-live command"
    )
    assert "run-live-loop" in command


def test_loop_unit_is_bounded_and_confirmed() -> None:
    command = _exec_start(LOOP_SERVICE)
    assert "--confirm-bounded-live" in command
    assert "--iterations" in command
    assert "--confirm-unattended-live" not in command


def test_loop_unit_does_not_restart_itself() -> None:
    """Restart=always around a bounded loop defeats the gate it is bounded by."""
    body = LOOP_SERVICE.read_text(encoding="utf-8")
    assert "Restart=no" in body
    assert "Restart=always" not in body


def test_loop_unit_sets_working_directory() -> None:
    """The kill switch resolves STOP_TRADING against the process cwd.

    systemd defaults system units to "/", where the kill file would never be
    found and the operator's stop command would be silently ineffective.
    """
    body = LOOP_SERVICE.read_text(encoding="utf-8")
    assert "WorkingDirectory=/opt/solomon-trader/app" in body


def test_startup_script_does_not_write_dead_risk_keys() -> None:
    """MAX_* env keys are read by no code; the real caps are in trading_rules.yaml."""
    body = STARTUP.read_text(encoding="utf-8")
    for key in ("MAX_DAILY_LOSS_USD=", "MAX_TRADE_AMOUNT_USD=", "MAX_OPEN_POSITIONS="):
        assert key not in body, f"startup.sh writes the dead key {key}"


def test_startup_script_leaves_the_trading_timer_disabled() -> None:
    """Provisioning must never arm trading."""
    body = STARTUP.read_text(encoding="utf-8")
    assert "systemctl disable solomon-trader-loop.timer" in body
    assert "systemctl enable solomon-trader-loop.timer" not in body
    assert "systemctl start solomon-trader-loop.timer" not in body


def test_startup_script_defaults_to_paper_posture() -> None:
    """Arming must be an explicit metadata choice, never the default."""
    body = STARTUP.read_text(encoding="utf-8")
    assert 'TRADING_POSTURE="${TRADING_POSTURE:-paper}"' in body


def test_startup_script_lets_an_existing_env_win() -> None:
    """TRADING_POSTURE seeds a first boot only.

    If a redeploy could re-apply it, the metadata value would silently re-arm
    (or disarm) a running deployment.
    """
    body = STARTUP.read_text(encoding="utf-8")
    assert 'read_existing TRADING_MODE "$FIRST_BOOT_MODE"' in body
    assert 'read_existing TRADING_ENABLED "$FIRST_BOOT_ENABLED"' in body


def test_startup_script_rejects_an_unknown_posture() -> None:
    body = STARTUP.read_text(encoding="utf-8")
    assert "TRADING_POSTURE must be 'paper' or 'live'" in body


def test_startup_script_preserves_operator_owned_config() -> None:
    """A redeploy must not restore the repo's trading_rules.yaml over the VM's."""
    body = STARTUP.read_text(encoding="utf-8")
    assert '"$APP_DIR/config/."' in body


def test_startup_script_refreshes_secrets_every_boot() -> None:
    """Writing .env only when absent means a rotated credential never lands."""
    body = STARTUP.read_text(encoding="utf-8")
    assert 'if [ ! -f "$APP_DIR/.env" ]' not in body
    assert "gcloud secrets versions access latest" in body


def test_env_example_does_not_reintroduce_dead_risk_keys() -> None:
    example = (Path(__file__).resolve().parents[1] / ".env.example").read_text(
        encoding="utf-8"
    )
    for key in ("MAX_DAILY_LOSS_USD=", "MAX_TRADE_AMOUNT_USD=", "MAX_OPEN_POSITIONS="):
        assert key not in example


def test_readme_does_not_document_dead_risk_keys() -> None:
    """The README's .env block re-creates the confusion .env.example removed."""
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(
        encoding="utf-8"
    )
    for key in ("MAX_DAILY_LOSS_USD=", "MAX_TRADE_AMOUNT_USD=", "MAX_OPEN_POSITIONS="):
        assert key not in readme, f"README.md still documents the dead key {key}"
