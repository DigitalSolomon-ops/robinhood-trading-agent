# Small-Cap Scout (analysis-only)

A daily, **analysis-only** momentum screener in the Ross Cameron / Warrior
Trading style. It reads public end-of-day market data (Massive / formerly
Polygon.io), scans the whole US market for the "5 Pillars" of a small-cap
momentum setup, and emails a ranked shares **watchlist**. It **never trades**:
no order is placed, reviewed, or cancelled anywhere in `src/smallcap_scout/`, it
does not touch any trading gate, the crypto lane, or the equities order path.

## Honesty (read this first)

- **End-of-day, not real-time.** The free Massive tier has no real-time or
  pre-market feed, so this runs on the **last completed session**. It is a
  **morning watchlist of the prior session's momentum leaders with a catalyst**
  — **not** a live pre-market gapper scan. A live gapper scan needs paid
  real-time data. The email header says this too.
- **Float is a proxy.** "Float" here is `share_class_shares_outstanding` (falling
  back to `weighted_shares_outstanding`) — the issuer's **shares outstanding**,
  a proxy for true free float, which the free tier does not expose. It is
  labelled as shares-outstanding everywhere it appears.
- **Small-caps are high-risk.** Low-priced, thinly-capitalised, high-volatility
  names can gap against you and lose value fast. The email carries a prominent
  not-financial-advice + substantial-risk disclaimer. It is not advice.

## The 5 Pillars (all config-tunable in `config/smallcap_scout.yaml`)

| # | Pillar | Default | Where applied |
|---|--------|---------|---------------|
| 1 | Big move: daily % change vs prior close | `>= 10%` | market-wide filter |
| 2 | High relative volume: today vol / avg baseline | `>= 5x` | market-wide filter |
| 3 | Price range | `$1–$20` | market-wide filter |
| 4 | Low float (shares-outstanding **proxy**) | `<= 20,000,000` | shortlist enrich |
| 5 | News catalyst (a plus, not a filter) | recent headline | shortlist enrich |

**Pillars 1–3** are computed across every US ticker from **grouped-daily** bars
(all ~12.5k stocks in one call per session). **The RVOL baseline** is the mean
daily volume over the prior `rvol_baseline_days` (default 20) completed
sessions, taken straight from those same grouped-daily days — so it needs **no
per-ticker history call**. A name with fewer than `rvol_min_baseline_days`
(default 5) of baseline history is dropped as too thin to rate honestly.

Only the survivors (the shortlist, capped at `shortlist_max`, default 40) cost a
per-ticker **float** (`get_ticker_details`) and **news** (`get_ticker_news`)
call — which keeps a run inside the rate-limited free tier. Pillar 4 drops a
name only when its float is **known** and above the cap; an **unknown** float is
kept and flagged, never silently dropped. Pillar 5 is a **plus**: a positive
recent headline lifts the rank and its title is shown; it never filters.

## Levels (on the shares)

Long-only, from the baseline-window daily bars already in hand:

- **Entry** = last close (a momentum-continuation entry near the last print; the
  breakout reference — the prior window high — is reported too).
- **Target** = `entry + max(ATR × atr_target_mult, entry × realized_vol × √h)` —
  a measured move from realized vol / ATR over the horizon.
- **Stop** = below the recent `support_lookback`-day support low, tightened so it
  is never further than `ATR × stop_atr_mult` below entry (bounds the risk).

## Ranking

A composite of four normalised factors (weights in `rank_weights`): size of the
move, relative volume, a low-float bonus (smaller shares-outstanding ranks
higher; unknown float is neutral), and a positive-catalyst bonus. The top
`top_n` (default 10) are emailed.

## Run it

Dry-run preview (composes + prints the email, **never** opens SMTP; needs a
Massive key so it can read data — env `MASSIVE_API_KEY` or Secret Manager
`massive-api`):

```
SMALLCAP_SCOUT_DRY_RUN=1 SMALLCAP_SCOUT_TOP=10 .venv/Scripts/python.exe -m src.smallcap_scout
```

Send for real (Gmail SMTP, port 465/SSL):

```
.venv/Scripts/python.exe -m src.smallcap_scout
```

Env: `SMALLCAP_SCOUT_DRY_RUN=1` (print, don't send), `SMALLCAP_SCOUT_TOP=N`.

## Credentials (house env-first-then-Secret-Manager pattern)

- **Massive data key** — `MASSIVE_API_KEY`, else Secret Manager `massive-api`.
- **Gmail app password** — `GMAIL_APP_PASSWORD`, else Secret Manager
  `gmail-app-password` (via `GMAIL_VAULT_NAME`). Never hardcoded, never logged.
- **From / To** — `GMAIL_USER` / `SMALLCAP_SCOUT_TO`, both defaulting to
  `digitalsolomon.com@gmail.com` (also overridable in
  `config/smallcap_scout.yaml`).

## Deploy notes (operator-gated — NOT done here)

Deploying is a separate, human-gated step (`deploy/deploy-smallcap-scout.sh`
builds `Dockerfile.smallcap-scout` into a Cloud Run Job + Cloud Scheduler). It
reuses the existing `massive-api` and `gmail-app-password` secrets and sets
`DS_VAULT_NO_GCLOUD=1` so the container uses ADC/the SDK. Schedule it at a
post-close / pre-open hour so the latest completed session's bars are available.

## Relationship to the Options Scout

This is a **second, independent** daily routine that mirrors the Options Scout's
structure (secret resolution, 465/SSL Gmail SMTP, prominent disclaimer, `--dry-run`
opens no socket) and reuses the shared `MassiveClient` and `summarize_news`. It
does **not** modify the Options Scout, and — unlike it — the deliverable is a
**shares** watchlist, not options (small-caps often lack liquid options; where a
name is optionable that is incidental).
