# Deploy: crypto.digitalsolomon.com (Solomon Trader 007)

**Architecture:** dedicated GCE VM (`crypto-trader`, e2-small, us-central1-a)
running the dashboard on :8000, exposed only through the existing
IAP-gated HTTPS load balancer (`creator-lb` / `creator-https-proxy`,
IP `34.102.248.141`). Google login (IAP) is the only way in. Robinhood keys
live in Secret Manager, never in git or the image. App stays in **paper mode**.

Project `digitalsolomon-creator` · Operator `marcus.barber@digitalsolomon.com`.

---

## Operator prerequisites (only you can do these)

1. **Re-authenticate gcloud** (tokens are stale):
   ```
   gcloud auth login
   gcloud config set project digitalsolomon-creator
   ```
2. **DNS (HostGator cPanel, near the end):** add an A record
   `crypto` → `34.102.248.141` (TTL 5 min). DNS is at HostGator
   (ns4229/ns4230.hostgator.com), not Cloud DNS, so this is manual.

---

## Provisioning steps (I drive these after you re-auth)

Values: `ZONE=us-central1-a REGION=us-central1 SA=solomon-trader-sa`

### 1. Secrets → Secret Manager (from local .env)
```
gcloud secrets create robinhood-api-key     --replication-policy=automatic
gcloud secrets create robinhood-private-key --replication-policy=automatic
# pipe the values from the local .env (never echoed to logs)
```

### 2. Service account + secret access
```
gcloud iam service-accounts create solomon-trader-sa --display-name="Solomon Trader VM"
gcloud secrets add-iam-policy-binding robinhood-api-key     --member="serviceAccount:$SA@..." --role=roles/secretmanager.secretAccessor
gcloud secrets add-iam-policy-binding robinhood-private-key --member="serviceAccount:$SA@..." --role=roles/secretmanager.secretAccessor
```

### 3. Ship code to GCS
```
gsutil mb -l us-central1 gs://digitalsolomon-creator-solomon-trader
gsutil cp solomon-trader-app.tar.gz gs://digitalsolomon-creator-solomon-trader/
```

### 4. VM (external IP for egress only; inbound firewalled)
```
gcloud compute instances create crypto-trader --zone=$ZONE --machine-type=e2-small \
  --image-family=ubuntu-2204-lts --image-project=ubuntu-os-cloud \
  --service-account=$SA@... --scopes=cloud-platform --tags=crypto-trader \
  --metadata=CODE_BUCKET=gs://digitalsolomon-creator-solomon-trader/solomon-trader-app.tar.gz \
  --metadata-from-file=startup-script=deploy/startup.sh
```

### 5. Firewall — only Google LB/health-check ranges reach :8000
```
gcloud compute firewall-rules create allow-lb-to-crypto-trader \
  --direction=INGRESS --action=ALLOW --rules=tcp:8000 \
  --source-ranges=35.191.0.0/16,130.211.0.0/22 --target-tags=crypto-trader
```

### 6. Zonal NEG + health check + IAP'd backend
```
gcloud compute network-endpoint-groups create crypto-trader-neg --zone=$ZONE \
  --network-endpoint-type=GCE_VM_IP_PORT --default-port=8000 --network=default --subnet=default
gcloud compute network-endpoint-groups update crypto-trader-neg --zone=$ZONE \
  --add-endpoint=instance=crypto-trader,port=8000
gcloud compute health-checks create http crypto-trader-hc --port=8000 --request-path=/
gcloud compute backend-services create crypto-trader-be --global \
  --load-balancing-scheme=EXTERNAL_MANAGED --protocol=HTTP --health-checks=crypto-trader-hc
gcloud compute backend-services add-backend crypto-trader-be --global \
  --network-endpoint-group=crypto-trader-neg --network-endpoint-group-zone=$ZONE \
  --balancing-mode=RATE --max-rate-per-endpoint=100
gcloud iap web enable --resource-type=backend-services --service=crypto-trader-be
gcloud iap web add-iam-policy-binding --resource-type=backend-services --service=crypto-trader-be \
  --member=user:marcus.barber@digitalsolomon.com --role=roles/iap.httpsResourceAccessor
```

### 7. Route the host on the existing LB + extend the managed cert
```
gcloud compute url-maps add-path-matcher creator-lb --path-matcher-name=crypto-pm \
  --default-service=crypto-trader-be --new-hosts=crypto.digitalsolomon.com
# create new managed cert covering all hosts incl. crypto, attach alongside existing, prune later
```

### 8. DNS (you, at HostGator): A `crypto` → 34.102.248.141

### 9. Verify
- `gcloud compute ssl-certificates describe <cert>` → status ACTIVE
- Browse https://crypto.digitalsolomon.com → Google IAP login → dashboard
- Confirm banner reads **SAFE: PAPER MODE ONLY**

---

## Rollback
`gcloud compute instances delete crypto-trader`, delete the backend/NEG/host-rule,
remove the HostGator A record. No other surface is affected.
