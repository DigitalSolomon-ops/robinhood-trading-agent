# Deploy: crypto.digitalsolomon.com (Solomon Trader 007)

**Architecture:** dedicated GCE VM (`crypto-trader`, e2-small, us-central1-a) running the
dashboard on :8000, exposed only through the existing IAP-gated HTTPS load balancer
(`creator-lb` / `creator-https-proxy`, IP `34.102.248.141`). Google login (IAP) is the only
way in from outside, and `src/web_security.py` verifies the IAP assertion in-app so the
health-check ranges and the default VPC cannot reach a mutating route. Robinhood keys live
in Secret Manager, never in git and never in the image. Persistent state lives on a separate
disk that survives instance deletion.

Project `digitalsolomon-creator` · Operator `marcus.barber@digitalsolomon.com`

**The artifact always ships a paper config.** `deploy/package.py` forces
`trading.enabled: false` / `trading.mode: paper` into the tarball and refuses to build
otherwise, regardless of what the working copy says. Arming is done once, on the VM, by the
operator; `startup.sh` preserves `config/` across redeploys so that decision persists without
ever living in the tarball. See [Arming](#8-arming-operator-only).

---

## Operator prerequisites (only you can do these)

1. **Re-authenticate gcloud** (tokens go stale):
   ```
   gcloud auth login
   gcloud config set project digitalsolomon-creator
   ```
2. **DNS (HostGator cPanel, step 7):** add an A record `crypto` → `34.102.248.141`
   (TTL 5 min). DNS is at HostGator (ns4229/ns4230.hostgator.com), not Cloud DNS, so this
   is manual.

Shared values: `ZONE=us-central1-a REGION=us-central1 SA=solomon-trader-sa`
`PROJECT=digitalsolomon-creator BUCKET=gs://digitalsolomon-creator-solomon-trader`

---

## 1. Build and verify the artifact

```
cd agent
.venv/Scripts/python -m pytest -q          # must be green; it gates every live launch
.venv/Scripts/python deploy/package.py     # writes solomon-trader-app.tar.gz
```

`package.py` refuses to produce an artifact that ships a live-mode config, a `.env`, a
`*.db`, or no `tests/` directory. Re-verify any artifact at any time:

```
python deploy/package.py --verify-only solomon-trader-app.tar.gz
```

## 2. Secrets → Secret Manager

```
gcloud secrets create robinhood-api-key     --replication-policy=automatic
gcloud secrets create robinhood-private-key --replication-policy=automatic
# pipe values in from the local .env; never echo them
```

The intelligence layer is enabled in `config/intelligence.yaml` with all four filters on, so
also create `cryptopanic-api-key`, `coingecko-api-key` and `fred-api-key` if those providers
are in use. Alpaca keys only if that lane is in scope (`ALPACA_ENABLED=false` today).

## 3. Service account and IAM

```
gcloud iam service-accounts create solomon-trader-sa --display-name="Solomon Trader VM"
for S in robinhood-api-key robinhood-private-key; do
  gcloud secrets add-iam-policy-binding $S \
    --member="serviceAccount:$SA@$PROJECT.iam.gserviceaccount.com" \
    --role=roles/secretmanager.secretAccessor
done
gcloud projects add-iam-policy-binding $PROJECT \
  --member="serviceAccount:$SA@$PROJECT.iam.gserviceaccount.com" --role=roles/logging.logWriter
gcloud projects add-iam-policy-binding $PROJECT \
  --member="serviceAccount:$SA@$PROJECT.iam.gserviceaccount.com" --role=roles/monitoring.metricWriter
```

## 4. Ship code to GCS

```
gsutil mb -l us-central1 $BUCKET
gsutil cp solomon-trader-app.tar.gz $BUCKET/
```

## 5. State disk and VM

The state disk is created separately and **not** auto-deleted, so `gcloud compute instances
delete` no longer destroys the decision log and audit history.

```
gcloud compute disks create solomon-trader-state --zone=$ZONE --size=10GB --type=pd-balanced

gcloud compute instances create crypto-trader --zone=$ZONE --machine-type=e2-small \
  --image-family=ubuntu-2204-lts --image-project=ubuntu-os-cloud \
  --service-account=$SA@$PROJECT.iam.gserviceaccount.com --scopes=cloud-platform \
  --tags=crypto-trader \
  --disk=name=solomon-trader-state,device-name=state,mode=rw,auto-delete=no \
  --metadata=CODE_BUCKET=$BUCKET/solomon-trader-app.tar.gz \
  --metadata-from-file=startup-script=deploy/startup.sh
```

On first boot, format and mount the state disk at `/opt/solomon-trader/state` and add it to
`/etc/fstab` before the startup script runs, or re-run the startup script after mounting.
`startup.sh` symlinks `app/data` to `$STATE_DIR/data`; it never mounts anything at
`app/data` itself, because `rm -rf` on a mount point would abort the redeploy under
`set -euo pipefail`.

**`startup.sh` accepts an optional `TRADING_POSTURE` metadata value (`paper` by default).**
Leave it unset here. Arming is step 8.

## 6. Firewall

```
gcloud compute firewall-rules create allow-lb-to-crypto-trader \
  --direction=INGRESS --action=ALLOW --rules=tcp:8000 \
  --source-ranges=35.191.0.0/16,130.211.0.0/22 --target-tags=crypto-trader
```

Verify those are still Google's published health-check ranges before relying on them. Note
that the `default` VPC also ships `default-allow-internal`, so every VM in the project can
reach :8000 regardless of this rule. That is why the in-app IAP verification exists; audit
or narrow `default-allow-internal` as well if you want defence in depth.

## 7. NEG, health check, IAP backend, LB routing, DNS

```
gcloud compute network-endpoint-groups create crypto-trader-neg --zone=$ZONE \
  --network-endpoint-type=GCE_VM_IP_PORT --default-port=8000 --network=default --subnet=default
gcloud compute network-endpoint-groups update crypto-trader-neg --zone=$ZONE \
  --add-endpoint=instance=crypto-trader,port=8000

# /healthz, NOT /. The home page signs a Robinhood API call on every request.
gcloud compute health-checks create http crypto-trader-hc --port=8000 --request-path=/healthz

gcloud compute backend-services create crypto-trader-be --global \
  --load-balancing-scheme=EXTERNAL_MANAGED --protocol=HTTP --health-checks=crypto-trader-hc
gcloud compute backend-services add-backend crypto-trader-be --global \
  --network-endpoint-group=crypto-trader-neg --network-endpoint-group-zone=$ZONE \
  --balancing-mode=RATE --max-rate-per-endpoint=100

gcloud iap web enable --resource-type=backend-services --service=crypto-trader-be
gcloud iap web add-iam-policy-binding --resource-type=backend-services --service=crypto-trader-be \
  --member=user:marcus.barber@digitalsolomon.com --role=roles/iap.httpsResourceAccessor

# Check creator-lb has no conflicting crypto host rule first.
gcloud compute url-maps add-path-matcher creator-lb --path-matcher-name=crypto-pm \
  --default-service=crypto-trader-be --new-hosts=crypto.digitalsolomon.com
# Create a managed cert covering all hosts incl. crypto, attach alongside the existing one,
# prune the old one once ACTIVE.
```

Then set the IAP audience on the VM so in-app verification can run. Read it from the backend
service and append to `/opt/solomon-trader/app/.env`:

```
IAP_AUDIENCE=/projects/<PROJECT_NUMBER>/global/backendServices/<BACKEND_SERVICE_ID>
PUBLIC_ORIGIN=https://crypto.digitalsolomon.com
```

`PUBLIC_ORIGIN` is written by `startup.sh`; `IAP_AUDIENCE` must be added once the backend
service exists. Restart `solomon-trader-dashboard` afterwards.

**DNS (you, at HostGator):** A record `crypto` → `34.102.248.141`.

### Verify before going further

- `gcloud compute ssl-certificates describe <cert>` → status ACTIVE
- `https://crypto.digitalsolomon.com` → Google IAP login → dashboard
- The banner reads **paper mode**
- `curl http://<vm-internal-ip>:8000/settings` from another project VM → **401**
- `curl http://<vm-internal-ip>:8000/healthz` → `{"status":"ok"}`
- `systemctl is-enabled solomon-trader-loop.timer` → **disabled**

## 8. Arming (operator only)

Nothing above places an order. Arming is a separate, deliberate act, and it is yours alone
under `project.yaml`'s `paper-to-live-is-human` gate.

On the VM, as `solomon`, in `/opt/solomon-trader/app`:

1. `python -m src.main init`
2. `python -m src.main test-connection` — read-only; proves the Secret Manager credentials
   work from this egress IP
3. `python -m src.main validate-symbols` — regenerates `data/symbol_validation.json`. **The
   bounded gate refuses every symbol until this has run on this VM.**
4. `python -m pytest` — proves the suite passes in this environment; it gates every launch
5. Decide the strategy profile. `strategy.yaml` ships `active_profile: growth_test`, whose
   own note says "paper testing only".
6. Decide which of `trading_rules.yaml risk.max_trade_amount_usd` (100) and
   `strategy.yaml position_sizing.amount_usd` (25) governs an in-cycle order, and set both.
7. Set every cap deliberately. The bounded gate refuses on any of:
   `max_trade_amount_usd > 100`, `max_daily_loss_usd > 100`, `max_trades_per_day > 5`,
   `max_open_positions > 10`, `max_symbol_allocation_percent` unset or `> 25`,
   `min_order_cooldown_seconds < 300`, `require_live_order_reconciliation` false, any
   `allow_*` flag true, or negative paper positions.
8. Edit `config/trading_rules.yaml` on the VM → `enabled: true`, `mode: live`. This is the
   step the artifact deliberately cannot do for you. `startup.sh` preserves it from here on.
9. Set `.env` → `TRADING_MODE=live`, `TRADING_ENABLED=true` (or use the dashboard's Live
   Control page, which logs the change).
10. `python -m src.main live-readiness` → every row PASS.
11. **One bounded run, watched, from `/opt/solomon-trader/app`:**
    ```
    python -m src.main run-live-loop --iterations 1 --confirm-bounded-live
    ```
    Refusal reasons are precise; read them rather than working around them. Verify the fill
    in the Robinhood app yourself.
12. `python -m src.main reconcile-live-orders && python -m src.main export-live-audit`
13. Only then: `systemctl enable --now solomon-trader-loop.timer`

**Never put `run-live` in a unit file.** It loops forever with no gate function, no test run
and no cap ceiling. `tests/test_deploy_units.py` fails the build if it ever appears there.

## 9. Stopping

Any one of these halts new orders:

```
systemctl stop solomon-trader-loop.timer
# or, in /opt/solomon-trader/app:
python -m src.main stop            # writes STOP_TRADING relative to the CWD
# or set TRADING_ENABLED=false in .env
# or use the dashboard Kill page
```

`python -m src.main stop` resolves `STOP_TRADING` against the **process working directory**,
so run it from `/opt/solomon-trader/app`. From a home directory it writes a file nothing
checks. None of these close an open position; that is manual, in the Robinhood app.

## 10. Redeploy

```
cd agent && .venv/Scripts/python -m pytest -q && .venv/Scripts/python deploy/package.py
gsutil cp solomon-trader-app.tar.gz $BUCKET/
gcloud compute instances reset crypto-trader     # or re-run the startup script
```

`.env`, `config/` and the state disk all persist. The artifact's paper config does **not**
overwrite an armed VM, because `startup.sh` preserves `config/` and lets an existing `.env`
win over `TRADING_POSTURE`.

## 11. Backups and alerting

```
# nightly, via cron on the VM
gsutil -m rsync -r /opt/solomon-trader/state/data $BUCKET/backups/$(date -I)/
```

Set a 30-day lifecycle rule on the backups prefix. Add a Cloud Monitoring uptime check on
`/healthz`, an alert on `solomon-trader-dashboard.service` restarts, and a log-based metric
on the string `refuse` so every live-gate refusal reaches you.

## 12. Rollback

| Step | Undo |
|---|---|
| Live armed | `systemctl stop solomon-trader-loop.timer`; `TRADING_ENABLED=false`; positions closed manually |
| Timer enabled | `systemctl disable --now solomon-trader-loop.timer` |
| DNS | Remove the `crypto` A record at HostGator |
| LB routing | Delete the `crypto-pm` path matcher and the `crypto-trader-be` backend |
| VM | `gcloud compute instances delete crypto-trader` — the state disk survives (`auto-delete=no`) |
| Secrets | `gcloud secrets delete` each |

No other DigitalSolomon surface is affected. `creator-lb` gains a path matcher and loses it
cleanly.
