"""The trade cycle's play source: the analysis-only Options Scout, re-priced now.

An ``OptionPlaySource`` (the contract ``run_option_paper_loop`` consumes) backed by
the SAME analysis-only screen the daily Options Scout email is built from
(``options_scout.analyzer.scout_plays``), re-run at cycle time against a read-only
Massive feed so premiums are fresh. It NEVER touches an order path -- it only
reads market data and ranks contracts, exactly like the scout.

Fail-safe by construction: anything going wrong while sourcing plays -- no Massive
key, a rate limit, an off-hours empty snapshot, an import problem -- yields an
EMPTY play list, not an exception. An empty cycle is a clean no-op (zero fills),
which is the safe direction to fail. A play missing a strike or premium is skipped
(it cannot be priced into a defined-risk candidate) rather than fabricated.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Sequence

from ..option_runtime import OptionPlay

_LOG = logging.getLogger(__name__)

BASIS_SCOUT_REPRICE = "options_scout_reprice"


class ScoutPlaySource:
    """Priced plays from the analysis-only Options Scout, re-run this cycle.

    ``client`` / ``config`` are injection seams for tests; production passes
    nothing and a read-only ``MassiveClient`` + the packaged scout config are
    built lazily. The ``symbols`` the loop offers are ignored on purpose -- the
    scout ranks its own configured universe.
    """

    basis_name = BASIS_SCOUT_REPRICE

    def __init__(self, *, client: Any | None = None, config: dict[str, Any] | None = None) -> None:
        self._client = client
        self._config = config
        self._sourced = 0
        self._skipped = 0
        self._error: str | None = None

    def provenance(self) -> dict[str, Any]:
        prov: dict[str, Any] = {
            "basis": self.basis_name,
            "vendor": "massive",
            "screen": "options_scout.analyzer.scout_plays",
            "priced": self._sourced,
            "skipped_unpriced": self._skipped,
        }
        if self._error:
            prov["error"] = self._error
        return prov

    def describe(self) -> str:
        return (
            "analysis-only Options Scout ranked plays, re-priced at cycle time on a "
            "read-only Massive feed (no order path)"
        )

    def plays(self, symbols: Sequence[str], today: Any) -> list[OptionPlay]:
        self._sourced = 0
        self._skipped = 0
        self._error = None
        try:
            from ..equity_intelligence.massive_client import MassiveClient
            from ..options_scout.analyzer import scout_plays
            from ..options_scout.config import load_scout_config
        except Exception as exc:  # pragma: no cover - import-time safety net
            self._error = f"scout import unavailable: {exc}"
            _LOG.warning("options_scout unavailable; sourcing no plays this cycle", exc_info=True)
            return []

        try:
            config = self._config if self._config is not None else load_scout_config()
            client = self._client if self._client is not None else MassiveClient()
            now = datetime.now(UTC)
            raw = scout_plays(client, config, today=today, now=now)
        except Exception as exc:
            self._error = f"scout_plays failed: {exc}"
            _LOG.warning("scout_plays failed; sourcing no plays this cycle (fail-safe)", exc_info=True)
            return []

        out: list[OptionPlay] = []
        for play in raw or []:
            strike = getattr(play, "strike", None)
            premium = getattr(play, "premium", None)
            if strike is None or premium is None:
                # Unpriced: the scout could not attach a real contract/premium
                # (e.g. greeks/IV null off market hours). It cannot become a
                # defined-risk debit candidate, so skip it -- never fabricate one.
                self._skipped += 1
                continue
            out.append(
                OptionPlay(
                    symbol=play.symbol,
                    direction=play.direction,
                    reference_close=float(play.reference_close),
                    entry=float(play.entry),
                    ceiling=float(play.ceiling),
                    floor=float(play.floor),
                    conviction=float(getattr(play, "conviction", 0.0)),
                    rank_score=float(getattr(play, "rank_score", 0.0)),
                    strike=float(strike),
                    expiry_date=getattr(play, "expiry_date", None),
                    contract_ticker=getattr(play, "contract_ticker", None),
                    premium=float(premium),
                )
            )
        self._sourced = len(out)
        return out
