# Market-regime scaling + the liquidity/tradability gate (equities lane)

**Status:** implemented. Config: `config/trading_rules.yaml` →
`equities.market_regime:` and `equities.liquidity:`. Code:
`src/equity_intelligence/market_regime.py`,
`src/equity_intelligence/liquidity.py`, both wired in
`src/equity_runtime.py:run_equity_cycle`; the liquidity half is consumed by
`src/equity_symbols.py:validate_equity_symbols` / `equity_tradability`.

Two enrichments that share one property: **neither can create, enlarge or
approve anything.** Each may only make a position smaller, or take a name out
of consideration.

## D — the market-regime brake

One reading per cycle, about the market rather than about a symbol.
`MassiveBreadthProvider` reads grouped-daily bars for the most recent
**completed** session (`/v2/aggs/grouped/locale/us/market/stocks/{date}`),
walking backwards from yesterday past weekends and any day the vendor returns
nothing for — which is how market holidays are handled without shipping a
holiday calendar. Of the names that moved, the share that closed above their
open is the advance ratio.

| Advance ratio | Regime | Effect on a NEW entry |
|---|---|---|
| ≥ `risk_on_advance_ratio` (0.55) | `risk_on` | sized at `risk_on_size_multiplier` (1.0) of the cap |
| between the two | `neutral` | sized at `neutral_size_multiplier` (0.75) of the cap |
| ≤ `risk_off_advance_ratio` (0.40) | `risk_off` | `on_risk_off`: `block_entries` (shipped) or `scale_down` to `risk_off_size_multiplier` (0.4) |

The scaled figure is passed to `OrderManager.process_signal(amount_usd=...)` as
a **reduced per-trade cap for that one signal**. A scaled entry that lands below
`min_size_usd` is refused with a rationale rather than sent as dust.

The rationale cites the regime. Three audit rows carry it:

* `equity_market_regime` — once per cycle: the session, the counts, the
  advancing share, the regime and the multiplier;
* `equity_regime_blocked` — per refused entry, quoting the breadth and naming
  the rules signal it overrode;
* the order's own `reason` on `paper_order_filled` — the regime clause plus the
  before/after size (`250.00 -> 100.00`).

## E — the liquidity / tradability gate

`validate_equity_symbols` existed but nothing called it: the audit found it dead
code. It is now the gate `run_equity_cycle` runs **every cycle**, in two halves:

1. **connector quote** (unchanged logic, now live) — a name with no quote, a
   `halted`/`delisted`/`inactive`/`closed`/`suspended` state, or no positive
   price is refused;
2. **liquidity** (`equities.liquidity:`) — Massive daily bars over a
   `lookback_days` window. Refused when there are fewer than `min_bars`
   sessions, when the latest bar is older than `max_stale_days` (how a halted or
   delisted name shows up in end-of-day data), or when last close / average
   volume / average dollar volume fall below their floors.

Every skip writes an `equity_symbol_unavailable` row naming which half refused
it and quoting the numbers — `average dollar volume 8,000,000` rather than the
word "illiquid". A cycle-level `equity_symbols_validated` row says how many of
the universe survived and lists what was dropped.

**It costs no extra connector traffic.** `EquityMarketDataService.read_quotes`
performs the cycle's one quote read; the same rows are handed to the gate and to
`prices_from_rows`, which prices only the symbols that survived. A name the gate
dropped never enters the candle ledger, so the strategy never sees it.

## What neither path can do

* **Place an order.** Neither module imports `OrderManager`, `RiskManager`, the
  kill switch or any broker, and neither can name a connector order tool.
  `tests/test_market_regime.py` and `tests/test_equity_liquidity.py` pin that
  structurally over each module's AST.
* **Enlarge anything.** `_reducing_multiplier` clamps every configured
  multiplier to `[0.0, 1.0]` with a note in the rationale when it clamps, and
  `RegimeVerdict.scaled_amount` floors the result against the incoming cap a
  second time — so even a hand-built verdict with an impossible multiplier
  cannot size an entry above `risk.max_trade_amount_usd`.
* **Touch an exit.** The brake applies to `side == "buy"` only. A sell is sized
  and routed exactly as it would be in any market: shrinking one would strand
  part of a position behind a data vendor, which is the opposite of a risk
  control. The gate likewise filters which symbols are *evaluated*; a position
  already open is closed by the strategy's own sell path.
* **Stand in for a risk gate.** Both run BEFORE `RiskManager`, `OrderManager`,
  the kill switch, regular-hours enforcement, the long-only / PDT / settlement
  guards and the two human-gate flags, and remove nothing from any of them. A
  regime-scaled order is still measured against `risk.max_trade_amount_usd`;
  `tests/test_equity_runtime.py::test_a_regime_scaled_entry_still_has_to_clear_every_risk_gate`
  pins that, and
  `::test_a_risk_on_regime_cannot_outrank_the_kill_switch` pins the emergency
  stop winning over the best possible breadth reading.

## Failure posture

Both default to `enabled: false` in code and are switched on in the shipped
config. Both fail **soft** by default and **closed** on request:

| Situation | Default | With `require_breadth` / `require_liquidity` |
|---|---|---|
| Vendor error, missing api key, rate limit | reading unavailable; sizing left at the configured cap / symbol left to the quote check | new entries blocked / symbol skipped |
| Partial grouped-daily response (< `min_symbols`) | not a market-wide reading; sizing unchanged | new entries blocked |

The connector quote read failing is the one case that fails closed regardless:
`run_equity_cycle` hands the gate an **empty** row list (not `None`), so every
symbol is refused with a rationale. Assuming tradable on a failed read is what
turns a data outage into an unsupervised trade.

Grouped-daily and daily bars are end-of-day data. Both are decision inputs,
never execution-timing or pricing inputs — the Robinhood OAuth connector stays
the sole source of execution-time price, and a real order still needs `dry_run`
off **and** `confirm_live_order` on, which is the operator's explicit call.

## Cost note

`run_equity_paper_loop` builds both providers **once** for the whole loop rather
than per cycle. Both read sessions that have already closed, so a reading cannot
change part-way through a bounded same-session run; rebuilding per cycle would
re-poll the vendor once per symbol per iteration for numbers that are, by
construction, identical every time — which the free tier (~5 req/min) would not
survive.
