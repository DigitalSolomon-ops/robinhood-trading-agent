"""The build must refuse to ship an artifact that would arm trading.

These run as part of the suite, and the suite runs before every gated live
launch, so a regression here stops trading rather than shipping.
"""

from __future__ import annotations

import tarfile
from pathlib import Path

import pytest
import yaml

from deploy.package import (
    CONFIG_MEMBER,
    UnsafeArtifact,
    build,
    config_is_paper,
    force_paper_config,
    verify_artifact,
)

LIVE_CONFIG = """
trading:
  enabled: true
  mode: live
  allowed_symbols:
  - BTC-USD
  - ETH-USD
risk:
  max_trade_amount_usd: 100.0
  max_daily_loss_usd: 100.0
orders:
  order_type: limit
"""


# --- forcing ---------------------------------------------------------------


def test_force_paper_flips_both_arming_keys() -> None:
    result = yaml.safe_load(force_paper_config(LIVE_CONFIG))
    assert result["trading"]["enabled"] is False
    assert result["trading"]["mode"] == "paper"


def test_force_paper_preserves_everything_else() -> None:
    original = yaml.safe_load(LIVE_CONFIG)
    result = yaml.safe_load(force_paper_config(LIVE_CONFIG))
    assert result["trading"]["allowed_symbols"] == original["trading"]["allowed_symbols"]
    assert result["risk"] == original["risk"]
    assert result["orders"] == original["orders"]


def test_force_paper_is_idempotent() -> None:
    once = force_paper_config(LIVE_CONFIG)
    assert force_paper_config(once) == once


def test_force_paper_adds_the_keys_when_absent() -> None:
    result = yaml.safe_load(force_paper_config("risk:\n  max_trade_amount_usd: 1.0\n"))
    assert result["trading"] == {"enabled": False, "mode": "paper"}


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("trading:\n  enabled: false\n  mode: paper\n", True),
        ("trading:\n  enabled: true\n  mode: paper\n", False),
        ("trading:\n  enabled: false\n  mode: live\n", False),
        ("trading:\n  enabled: true\n  mode: live\n", False),
        ("risk: {}\n", False),
    ],
)
def test_config_is_paper(raw: str, expected: bool) -> None:
    assert config_is_paper(raw) is expected


# --- artifact verification -------------------------------------------------


def _artifact(tmp_path: Path, config: str, *, with_tests: bool = True,
              extra: dict[str, str] | None = None) -> Path:
    out = tmp_path / "artifact.tar.gz"
    files = {CONFIG_MEMBER: config, "src/main.py": "# app\n"}
    if with_tests:
        files["tests/test_safety.py"] = "def test_ok():\n    assert True\n"
    files.update(extra or {})
    with tarfile.open(out, "w:gz") as tar:
        for name, body in files.items():
            path = tmp_path / "stage" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
            tar.add(path, arcname=name)
    return out


def test_verify_accepts_a_paper_artifact(tmp_path: Path) -> None:
    verify_artifact(_artifact(tmp_path, "trading:\n  enabled: false\n  mode: paper\n"))


def test_verify_refuses_a_live_artifact(tmp_path: Path) -> None:
    with pytest.raises(UnsafeArtifact, match="live-mode"):
        verify_artifact(_artifact(tmp_path, LIVE_CONFIG))


def test_verify_refuses_an_enabled_paper_artifact(tmp_path: Path) -> None:
    """enabled: true with mode: paper is still not the shipped posture."""
    with pytest.raises(UnsafeArtifact):
        verify_artifact(_artifact(tmp_path, "trading:\n  enabled: true\n  mode: paper\n"))


def test_verify_refuses_an_artifact_carrying_a_dotenv(tmp_path: Path) -> None:
    with pytest.raises(UnsafeArtifact, match="secrets or state"):
        verify_artifact(
            _artifact(
                tmp_path,
                "trading:\n  enabled: false\n  mode: paper\n",
                extra={".env": "ROBINHOOD_API_KEY=leaked\n"},
            )
        )


def test_verify_refuses_an_artifact_carrying_a_database(tmp_path: Path) -> None:
    with pytest.raises(UnsafeArtifact, match="secrets or state"):
        verify_artifact(
            _artifact(
                tmp_path,
                "trading:\n  enabled: false\n  mode: paper\n",
                extra={"data/trading_agent.db": "sqlite\n"},
            )
        )


def test_verify_refuses_an_artifact_without_tests(tmp_path: Path) -> None:
    """run_project_tests() gates every live launch; no tests means no trading."""
    with pytest.raises(UnsafeArtifact, match="no tests"):
        verify_artifact(
            _artifact(tmp_path, "trading:\n  enabled: false\n  mode: paper\n",
                      with_tests=False)
        )


def test_verify_refuses_an_artifact_without_a_config(tmp_path: Path) -> None:
    out = tmp_path / "empty.tar.gz"
    src = tmp_path / "src.py"
    src.write_text("# app\n", encoding="utf-8")
    with tarfile.open(out, "w:gz") as tar:
        tar.add(src, arcname="src/main.py")
    with pytest.raises(UnsafeArtifact, match="no config"):
        verify_artifact(out)


# --- end to end ------------------------------------------------------------


def test_build_forces_paper_even_from_a_live_working_copy(tmp_path: Path) -> None:
    """The real repo config is live today. The artifact must not be."""
    root = Path(__file__).resolve().parents[1]
    out = build(tmp_path / "solomon-trader-app.tar.gz", root=root)

    with tarfile.open(out, "r:gz") as tar:
        member = tar.extractfile(CONFIG_MEMBER)
        assert member is not None
        shipped = yaml.safe_load(member.read().decode("utf-8"))

    assert shipped["trading"]["enabled"] is False
    assert shipped["trading"]["mode"] == "paper"
    # build() calls verify_artifact() itself, so reaching here means the
    # secrets, state and tests checks all passed too.


def test_build_leaves_the_working_copy_untouched(tmp_path: Path) -> None:
    """Packaging must never rewrite the operator's local posture."""
    root = Path(__file__).resolve().parents[1]
    config = root / CONFIG_MEMBER
    before = config.read_text(encoding="utf-8")
    build(tmp_path / "artifact.tar.gz", root=root)
    assert config.read_text(encoding="utf-8") == before
