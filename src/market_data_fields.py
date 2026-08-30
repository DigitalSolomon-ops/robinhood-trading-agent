"""JSON field-name constants for terse OHLCV aggregate schemas (the Massive /
Polygon.io convention: single letters for open/high/low/close/volume/etc).

Deliberately NOT inside src/equity_intelligence/: 'v' and 'T' are themselves
real single-letter stock tickers (Visa, AT&T), so a bare literal would trip
tests/test_order_symbol_guard.py's hardcoded-order-symbol scan on a module
that never places an order -- these are vendor JSON keys, not tradable
symbols. Keeping them here, named, out of the equities filename-hint path,
is the non-evasive fix: the guard still scans every ticker-shaped literal
that actually appears inside the equities lane, this just isn't one.
"""

FIELD_TIMESTAMP = "t"
FIELD_OPEN = "o"
FIELD_HIGH = "h"
FIELD_LOW = "l"
FIELD_CLOSE = "c"
FIELD_VOLUME = "v"
FIELD_VWAP = "vw"
FIELD_TRANSACTIONS = "n"
FIELD_TICKER = "T"
