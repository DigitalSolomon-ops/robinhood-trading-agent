"""The Robinhood data source for Sector Scout: a point-in-time SNAPSHOT an
agent session fills through the authorized MCP connector, consumed here as
plain data.

WHY A SNAPSHOT AND NOT A CLIENT. The repo's own binding decision
(docs/rh-equities-binding.md, re-verified live 2026-09-07) is that the
equities/options surface exists only through the OAuth MCP connector, which
is session-bound to a Claude agent: a headless job cannot call it, there is
no official equities/options REST API, and a reverse-engineered login is
rejected. The official signed API (auth.py, trading.robinhood.com) verifies
headless but covers crypto only. So the wiring is: an agent session (which
holds the connector) fills the MANIFEST this module emits, saves the
snapshot JSON, and the lane reads it like any other input. Field names
mirror the connector's tool payloads verbatim (pe_ratio, high_fill_rate_buy_price,
chance_of_profit_long, ...) so the snapshot is a recording, not a translation.

Staleness is a first-class property: every consumer states the snapshot age,
and the analyzer degrades per-field (n/a, never a stale number presented as
live) when the snapshot is old or absent.

ANALYSIS ONLY. This module holds no credential, opens no socket, and imports
no client. The broker-guard AST test covers it like every other module here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SNAPSHOT_SCHEMA_VERSION = 1

# Connector batch limits (from the tool contracts, verified live 2026-09-07).
FUNDAMENTALS_BATCH = 10
OPTION_QUOTES_BATCH = 20   # above 20 the official closes drop from the response
HISTORICALS_BATCH = 10


@dataclass
class RhSnapshot:
    """One filled snapshot. Dict-shaped throughout: the values are the
    connector payload rows, untranslated."""

    generated_at: str
    fundamentals: dict[str, dict[str, Any]] = field(default_factory=dict)
    fundamentals_not_found: list[str] = field(default_factory=list)
    indicators: dict[str, dict[str, Any]] = field(default_factory=dict)
    equity_quotes: dict[str, dict[str, Any]] = field(default_factory=dict)
    chains: dict[str, dict[str, Any]] = field(default_factory=dict)
    instruments: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    option_quotes: dict[str, dict[str, Any]] = field(default_factory=dict)
    earnings: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    constituent_weekly_closes: dict[str, list[float]] = field(default_factory=dict)

    def age(self, now: datetime | None = None) -> timedelta:
        now = now or datetime.now(UTC)
        try:
            gen = datetime.fromisoformat(self.generated_at)
            if gen.tzinfo is None:
                gen = gen.replace(tzinfo=UTC)
        except ValueError:
            return timedelta(days=999)
        return now - gen

    def is_fresh(self, max_age_hours: float, now: datetime | None = None) -> bool:
        return self.age(now) <= timedelta(hours=max_age_hours)

    def age_label(self, now: datetime | None = None) -> str:
        hours = self.age(now).total_seconds() / 3600.0
        if hours < 1.5:
            return f"{hours * 60:.0f} minutes old"
        return f"{hours:.1f} hours old"

    # --- typed getters (None, never a fabricated value) ------------------------

    def fundamental(self, symbol: str, key: str) -> float | None:
        row = self.fundamentals.get(symbol.upper())
        if not row:
            return None
        raw = row.get(key)
        try:
            return float(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    def fundamental_str(self, symbol: str, key: str) -> str | None:
        row = self.fundamentals.get(symbol.upper())
        value = row.get(key) if row else None
        return str(value) if value not in (None, "") else None

    def indicator(self, symbol: str, key: str) -> float | None:
        row = self.indicators.get(symbol.upper())
        if not row:
            return None
        raw = row.get(key)
        try:
            return float(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    def quote_field(self, instrument_id: str, key: str) -> Any:
        row = self.option_quotes.get(instrument_id)
        return row.get(key) if row else None


def load_snapshot(path: str | Path) -> RhSnapshot | None:
    """Load and shape-check a snapshot file. Unreadable or wrong-version
    snapshots return None -- the lane then runs Robinhood-less and says so."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        return None
    if not raw.get("generated_at"):
        return None
    return RhSnapshot(
        generated_at=str(raw["generated_at"]),
        fundamentals={str(k).upper(): v for k, v in (raw.get("fundamentals") or {}).items() if isinstance(v, dict)},
        fundamentals_not_found=[str(s) for s in raw.get("fundamentals_not_found") or []],
        indicators={str(k).upper(): v for k, v in (raw.get("indicators") or {}).items() if isinstance(v, dict)},
        equity_quotes={str(k).upper(): v for k, v in (raw.get("equity_quotes") or {}).items() if isinstance(v, dict)},
        chains={str(k).upper(): v for k, v in (raw.get("chains") or {}).items() if isinstance(v, dict)},
        instruments={str(k).upper(): list(v) for k, v in (raw.get("instruments") or {}).items() if isinstance(v, list)},
        option_quotes={str(k): v for k, v in (raw.get("option_quotes") or {}).items() if isinstance(v, dict)},
        earnings={str(k).upper(): list(v) for k, v in (raw.get("earnings") or {}).items() if isinstance(v, list)},
        constituent_weekly_closes={
            str(k).upper(): [float(x) for x in v]
            for k, v in (raw.get("constituent_weekly_closes") or {}).items()
            if isinstance(v, list)
        },
    )


def save_snapshot(payload: dict[str, Any], path: str | Path) -> None:
    payload = {**payload, "schema_version": SNAPSHOT_SCHEMA_VERSION}
    Path(path).write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")


# --- the manifest: what an agent session must fetch ---------------------------


def _chunk(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def build_manifest(config: dict[str, Any]) -> dict[str, Any]:
    """The exact, batched connector calls that fill a snapshot. Emitted as
    data so any agent session (or a scheduled routine) can service it
    mechanically and verbatim. Batch sizes are the verified tool limits."""
    uni = config.get("universe", {}) or {}
    funds = list(uni.get("sectors") or []) + list(uni.get("industries") or [])
    seeds_map: dict[str, list[str]] = config.get("seed_leaders", {}) or {}
    constituents = sorted({t for seeds in seeds_map.values() for t in seeds})
    st = config.get("structures", {}) or {}

    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "instructions": (
            "Fill each request with the named connector tool; write results into a "
            "snapshot JSON whose top-level keys are: generated_at (ISO, UTC, the time "
            "the LAST call finished), fundamentals (symbol -> row), "
            "fundamentals_not_found (list), indicators (symbol -> {sma_50, sma_200, "
            "rsi_14_weekly}), equity_quotes (symbol -> quote row), chains (symbol -> "
            "{expiration_dates: [...]}), instruments (symbol -> instrument rows for "
            "the chosen expiry), option_quotes (instrument id -> quote row, fields "
            "verbatim), earnings (symbol -> rows), constituent_weekly_closes "
            "(symbol -> ~104 weekly closes, optional). Never rename a field."
        ),
        "requests": {
            "fundamentals": {
                "tool": "get_equity_fundamentals",
                "batches": _chunk(sorted(set(funds + constituents)), FUNDAMENTALS_BATCH),
                "note": "covers funds AND all seed leaders; keep not_found",
            },
            "equity_quotes": {
                "tool": "get_equity_quotes",
                "batches": _chunk(funds, OPTION_QUOTES_BATCH),
            },
            "indicators": {
                "tool": "get_equity_technical_indicators",
                "per_symbol": funds,
                "calls": [
                    {"type": "sma", "period": 50, "interval": "day", "output": "latest"},
                    {"type": "sma", "period": 200, "interval": "day", "output": "latest"},
                    {"type": "rsi", "period": 14, "interval": "week", "output": "latest"},
                ],
                "note": "store as sma_50 / sma_200 / rsi_14_weekly per symbol",
            },
            "chains": {
                "tool": "get_option_chains",
                "per_symbol": funds,
                "note": "store the expiration_dates list per fund",
            },
            "instruments_and_quotes": {
                "tools": ["get_option_instruments", "get_option_quotes"],
                "note": (
                    "For each SELECTED fund (the lane prints the shortlist when run "
                    f"with --manifest): pick the monthly expiry nearest {st.get('target_dte', 180)} "
                    f"DTE inside {st.get('dte_min', 150)}-{st.get('dte_max', 240)} from the chain's "
                    "real expiration list; fetch instruments for that expiry (both types), "
                    "then quotes in batches of at most "
                    f"{OPTION_QUOTES_BATCH} so official closes are retained. Include the "
                    "ATM call AND put (straddle) for the implied-move computation."
                ),
            },
            "earnings": {
                "tool": "get_earnings_results",
                "per_symbol": "top-4 confirmed leaders of each selected fund",
            },
            "constituent_weekly_closes": {
                "tool": "get_equity_historicals",
                "batches": _chunk(constituents, HISTORICALS_BATCH),
                "params": {"interval": "week", "start_time": "2 years back"},
                "note": "optional accelerator for breadth; the Massive cache remains the fallback",
            },
        },
    }
