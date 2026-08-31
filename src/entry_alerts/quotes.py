"""Current (delayed) underlying price per symbol for the entry-hit alerter.

NOTIFICATION ONLY. This is a thin read over MassiveClient.get_current_price --
a price on the UNDERLYING used to decide whether to send an ALERT email. It has
no order path and never sizes or times a trade.

The price is the latest ~15-minute-delayed MINUTE bar close on the paid Massive
plan, which is why a ~10-min poll fits with no websocket. Stock minute
aggregates need a PAID (Starter+) entitlement; on the free (EOD-only) tier they
403 (surfaced as an exception here, caught -- the alerter reports the symbol as
"no price this cycle" rather than firing). The single-ticker snapshot/last-trade
endpoints are NOT authorized on the Options plan this runs under, so the client
reads minute aggregates instead.
"""

from __future__ import annotations

from typing import Any


def fetch_prices(client: Any, symbols: list[str], on_date: str | None = None) -> dict[str, float]:
    """Best-effort current price per symbol (latest delayed minute bar close). A
    symbol whose fetch fails or has no usable price is simply omitted from the
    result (never guessed), so a data hiccup on one name cannot fire or suppress
    an alert on another. `on_date` pins the intraday day to the store's day."""
    prices: dict[str, float] = {}
    for symbol in dict.fromkeys(symbols):  # de-dup, preserve order
        try:
            price = client.get_current_price(symbol, on_date=on_date)
        except Exception:
            continue
        if price is not None and price > 0:
            prices[symbol] = float(price)
    return prices
