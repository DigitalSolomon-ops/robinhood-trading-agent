"""Sector Scout: sector-first options research on a ~six-month horizon.

ANALYSIS ONLY. This package reads public market data (Massive) and composes a
daily report: which market segment, which leaders inside it, which dated
options structure, with a calculated probability of profit and expected value.
It never places, reviews, previews, or cancels an order and never touches a
trading gate, the crypto lane, or the equities order path.

Sibling to options_scout (10 trading-day, single-name horizon); this lane
answers the six-month, segment-first question. Same email path, same
analysis-only discipline, different question and a different clock.
"""

from .runner import run_sector_scout_email

__all__ = ["run_sector_scout_email"]
