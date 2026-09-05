"""Sector Scout config: the real YAML loads with the full universe, the
recipient defaults to the operator address, and the throttle resolves."""

from __future__ import annotations

from src.sector_scout.config import (
    load_sector_config,
    resolve_min_interval,
    resolve_to_addr,
)


def base_config() -> dict:
    config = load_sector_config()
    assert config, "config/sector_scout.yaml must exist and load"
    return config


def test_universe_shape() -> None:
    config = base_config()
    sectors = config["universe"]["sectors"]
    industries = config["universe"]["industries"]
    assert len(sectors) == 11
    assert len(industries) == 23
    seeds = config["seed_leaders"]
    for fund in sectors + industries:
        assert fund in seeds, f"{fund} missing seed leaders"
        assert len(seeds[fund]) == 8, f"{fund} must carry 8 seed leaders"


def test_recipient_is_operator_only(monkeypatch) -> None:
    monkeypatch.delenv("SECTOR_SCOUT_TO", raising=False)
    config = base_config()
    assert resolve_to_addr(config) == "digitalsolomon.com@gmail.com"
    monkeypatch.setenv("SECTOR_SCOUT_TO", "override@example.com")
    assert resolve_to_addr(config) == "override@example.com"


def test_min_interval_env_wins(monkeypatch) -> None:
    config = base_config()
    monkeypatch.delenv("MASSIVE_MIN_INTERVAL_SECONDS", raising=False)
    assert resolve_min_interval(config) == 13.0
    monkeypatch.setenv("MASSIVE_MIN_INTERVAL_SECONDS", "0")
    assert resolve_min_interval(config) == 0.0


def test_every_tunable_lives_in_config() -> None:
    config = base_config()
    for section in ("extremes", "continuation", "probability", "base_rate",
                    "structures", "factors", "selection", "state", "email"):
        assert section in config, f"{section} missing from sector_scout.yaml"
    assert config["structures"]["dte_min"] == 150
    assert config["structures"]["dte_max"] == 240
    assert config["factor_weights_earned"] == {}


def test_iv_rank_trailing_window_is_by_date_not_value() -> None:
    """Review regression (2026-09-04): the IV-rank window must be the most
    RECENT window_days by date, not the largest values ever retained."""
    from src.sector_scout.factors import iv_rank_read

    # Old regime (beyond the window): sky-high IV. Trailing window: low IV.
    history = {f"2024-01-{d:02d}": 0.90 for d in range(1, 29)}
    history.update({f"2026-08-{d:02d}": 0.20 for d in range(1, 29)})
    read = iv_rank_read(history, 0.30, window_days=28, min_days=10,
                        buy_max=30, sell_min=70)
    # Within the trailing 28 days (all 0.20), 0.30 ranks at the top.
    assert read.iv_rank == 100.0
    assert read.regime == "sell_premium"
