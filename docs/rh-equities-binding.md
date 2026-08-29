# Robinhood equities execution binding (rh-exec-spike)

**Decided 2026-08-28** by inspecting the connected, authorized Robinhood equities
surface with read-only calls. This is the decision doc the plan's `rh-exec-spike`
task requires — no code is written here.

## The surface

Robinhood equities is reached through the **authorized Robinhood MCP connector**
(the `get_equity_quotes` / `get_equity_positions` / `get_accounts` /
`review_equity_order` / `place_equity_order` toolset), OAuth-connected to the
operator's Robinhood account. Confirmed live 2026-08-28: a real-time AAPL quote
returned, and the account list resolved.

There is **no official public Robinhood equities REST API and no API key to mint**.
The connector *is* the credential. This is the sanctioned path; a reverse-engineered
library (robin_stocks, login+MFA) is explicitly rejected — against ToS, fragile.

## The credential / auth model

- **Auth = the OAuth connector**, session-bound to a Claude agent. There is nothing
  to paste, and **nothing to store in Secret Manager**. `vault.map.json` gets no
  Robinhood-equities entry.
- **Read vs. order capability is gated by Robinhood, per account** — not by separate
  credentials. The account list carries a per-account agent-trading flag.
- The operator's Robinhood profile has **two individual cash accounts**:
  - `••2833` (default) — **not** agent-tradable; option level 2; $1,720.88 unsettled.
  - `••2092` (nickname "Agentic") — **the one account Robinhood permits the agent to
    trade**; no options; **$0 balance**.
- So Robinhood natively enforces the "agent may only trade the designated account"
  boundary. Every `place_equity_order` in this lane targets `••2092` and nothing else.

## Read-only vs. order-capable separation (the gate-2 question)

**Already separated, natively.** Read access (quotes/positions/account) is available
now through the connector; order capability is confined by Robinhood to the "Agentic"
account. There is no separate read-only credential to issue — so the planned
`rh-readonly-creds` gate has **no key to create**. It collapses to "the connector is
authorized" (already true) plus, for live, "the Agentic account is funded" (a
non-credential operator step; the account is currently empty).

## Architectural consequence (important — revisits a plan assumption)

The connector is **session-bound to a Claude agent**; a headless always-on Python VM
daemon cannot call it. That contradicts the crypto lane's shape (a 24/7 VM loop on a
real Ed25519 API key). Therefore, for **equities**:

- **Live execution** runs inside a **Claude/harness agent session** that has the
  Robinhood connector, placing human-gated orders into the `••2092` account — not from
  a VM daemon.
- **Paper proving** (the local `paper_broker` simulating fills) needs a quote feed. It
  can use the connector **only while running as a Claude agent**. For an unattended
  daemon loop, a separate quotes source is required. Decision for first proving: run the
  paper loop **as a harness/Claude agent** (connector available), deferring any
  standalone quotes-provider integration until/if a true 24/7 daemon is wanted.

This means the equities lane is **agent-hosted, not VM-hosted** — the opposite of the
crypto lane. Downstream plan tasks (`equity-client`, `equity-broker`, deploy) should be
read in that light: the "client" wraps the connector tools, not an HTTP endpoint, and
"deploy" is an agent/runtime concern, not a VM service.

## Rate limits / operational notes

- Quote and account reads returned immediately; no key rotation to manage (OAuth).
- Long-only, no options in this lane (the Agentic account has no options level anyway).
- The `••2092` account is empty — funding it is a human, pre-live step, tracked with the
  live-arm gate, never an agent action.
