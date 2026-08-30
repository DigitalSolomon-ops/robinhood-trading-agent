# Options Scout (analysis-only)

A daily, **analysis-only** screener. It reads public market data (Massive /
formerly Polygon.io), ranks candidate single-leg options plays for the day, and
emails them. It **never trades**: no order is placed, reviewed, or cancelled
anywhere in `src/options_scout/`, it does not touch any trading gate, the crypto
lane, or the equities order path. Every price level is on the **underlying** --
the free data tier does not authorize option quotes, greeks, or IV, so the
reader maps the level onto the listed contract themselves.

## What it does

1. For each symbol in `config/options_scout.yaml:universe`, pulls ~2yr of daily
   bars, computes a directional thesis (EMA20 vs EMA50/SMA200, MACD, RSI), and a
   vol-scaled ENTRY / CEILING / FLOOR on the underlying plus a suggested expiry
   horizon and a strike picked from the options-contract **reference** list.
2. Backtests the *same* trigger rule over the 2yr history for an honest empirical
   hit-rate **with its sample size** (a small sample is flagged low-confidence).
3. Ranks by `(conviction) x (hit-rate x sample-size confidence)` and emails the
   top N with a prominent not-financial-advice disclaimer.

## Run it

Dry-run preview (composes + prints the email, **never** opens SMTP; needs a
Massive key so it can read data -- env `MASSIVE_API_KEY` or Secret Manager
`massive-api`):

```
.venv/Scripts/python.exe -m src.main options-scout-email --dry-run --top 5 --out scout.html
```

Send for real (Gmail SMTP):

```
.venv/Scripts/python.exe -m src.main options-scout-email --top 5
```

Flags: `--dry-run` (print, don't send), `--top N`, `--out PATH` (write the HTML).

## Credentials (house env-first-then-Secret-Manager pattern)

- **Massive data key** -- `MASSIVE_API_KEY`, else Secret Manager `massive-api`.
- **Gmail app password** -- `GMAIL_APP_PASSWORD`, else Secret Manager
  `gmail-app-password`. Never hardcoded, never logged.
- **From / To** -- `GMAIL_USER` / `OPTIONS_SCOUT_TO`, both defaulting to
  `digitalsolomon.com@gmail.com` (also overridable in `config/options_scout.yaml`).

## Deploy notes (operator-gated -- NOT done here)

Deploying is a separate, human-gated step. When it happens it will need:

1. **A new Secret Manager secret `gmail-app-password`** in project
   `digitalsolomon-creator`, holding a Gmail **app password** (not the account
   password; requires 2FA on the account, then create an app password). Add the
   `gmail-app-password` -> `GMAIL_APP_PASSWORD` mapping in
   `_tools/vault/vault.map.json` if the vault reader is used.
2. The `massive-api` secret already exists (used by the equities lane).
3. On Cloud Run, grant the runtime service account `secretAccessor` on both
   secrets and set `DS_VAULT_NO_GCLOUD=1` so it uses ADC/the SDK (gcloud is not
   installed there); locally, the daily-authenticated gcloud CLI is the fallback.
4. Schedule the daily run (e.g. Cloud Scheduler -> the container, or a local
   scheduled task) at a post-close hour so the latest completed session's bars
   and breadth are available.

## Methodology honesty (read before trusting a number)

- **One rule, live and historical.** The directional score used for today's
  thesis and the score replayed in the backtest are the *same* function over the
  *same* indicator series (`indicators.directional_score_at`). There is no
  separate, kinder rule for the backtest.
- **No lookahead in the backtest.** Each historical target is derived from that
  day's realized vol; a "hit" is a real high/low reaching the target within the
  horizon, not a modelled estimate.
- **The hit-rate always carries its sample size** and is shrunk by
  `occurrences / (occurrences + min_occurrences)` when ranking, so a lucky
  small-N fraction cannot out-rank a well-sampled setup. Below `min_occurrences`
  (default 10) it is labelled LOW CONFIDENCE in the email.
- **It is a base rate, never a guarantee.** The disclaimer says so, prominently.
- **Indicators are computed locally**, not fetched from the Massive indicator
  endpoints, precisely so the live and historical rules cannot drift apart. The
  Massive client is still reused for bars, news, breadth, and the contract
  reference list.
